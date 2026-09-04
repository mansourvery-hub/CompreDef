"""
gui.py - Configuration GUI for the CompreDef Anki add-on.

Provides a PyQt dialog allowing users to:
- Select the Target Note Type and map Target Word & Definition fields
  with intelligent automatic field matching.
- Configure and order the Dictionary Ladder (drag-and-drop or Move Up/Down
  buttons). Order is pure user preference — dictionaries are tried top to
  bottom and the first fully comprehensible definition wins (early exit).
  Recommended: richest dictionary you can comfortably read at the top.
"""

import os
from typing import Optional, Dict, Any, List
from aqt import mw
from aqt.utils import tooltip
from aqt.qt import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QComboBox,
    QPushButton,
    QCheckBox,
    QFileDialog,
    QDialogButtonBox,
    QWidget,
    QListWidget,
    QListWidgetItem,
    QAbstractItemView,
    QLabel,
    QGroupBox,
    QTextEdit,
    QLineEdit,
    Qt,
)

from .core import get_provider
from .anki import knowledge_summary_text
from .utils import (
    find_dictionary_folders,
    is_zip_dictionary,
)
from .provider import IndexingError
from .parser import get_single_dictionary


# Cross-version PyQt5/PyQt6 enum helpers
def _user_role() -> int:
    """Returns Qt.UserRole / Qt.ItemDataRole.UserRole across PyQt5 and PyQt6."""
    if hasattr(Qt, "ItemDataRole") and hasattr(Qt.ItemDataRole, "UserRole"):
        return Qt.ItemDataRole.UserRole
    return Qt.UserRole


def _internal_move_mode() -> Any:
    """Returns QAbstractItemView.DragDropMode.InternalMove across PyQt5 and PyQt6."""
    if hasattr(QAbstractItemView, "DragDropMode") and hasattr(QAbstractItemView.DragDropMode, "InternalMove"):
        return QAbstractItemView.DragDropMode.InternalMove
    return QAbstractItemView.InternalMove


def _user_checkable_flag() -> Any:
    """Returns Qt.ItemIsUserCheckable across PyQt5 and PyQt6.

    PyQt6 scoped the enums (Qt.ItemFlag.ItemIsUserCheckable); the raw
    top-level spelling crashed the config dialog on Anki's Qt6 builds
    (production bug, v1.0.14).
    """
    if hasattr(Qt, "ItemFlag") and hasattr(Qt.ItemFlag, "ItemIsUserCheckable"):
        return Qt.ItemFlag.ItemIsUserCheckable
    return Qt.ItemIsUserCheckable


def _size_button(btn: QPushButton) -> QPushButton:
    """Applies uniform comfortable sizing so buttons never render cramped.

    Without explicit minimums, Qt compresses buttons below their natural
    height whenever the dialog runs out of vertical space (the 'cramped
    buttons' problem). Minimums make the layout grow the dialog instead
    of squashing the widgets.
    """
    btn.setMinimumWidth(140)
    btn.setMinimumHeight(30)
    return btn


# Keyword lists for auto-matching target word and definition fields
_TARGET_WORD_KEYWORDS = [
    "word", "expression", "kanji", "reading", "furigana",
    "hiragana", "romaji", "katakana", "jp", "japanese", "ja"
]

# Fields that may carry the word's kana reading (furigana markup or plain
# kana). The engine parses these to disambiguate homographs like
# 先ず(まず) vs 先ず(せんず).
_READING_FIELD_KEYWORDS = [
    "furigana", "reading", "kana", "hiragana", "katakana",
    "readingfield", "pronunciation", "yomi", "読み",
]

# Fields that are the word itself with readings embedded (e.g. 先[ま]ず
# inside the Expression field) also count as a reading source.
_READING_KEYWORDS_SECONDARY = ["expression", "word", "front"]

_DEFINITION_KEYWORDS = [
    "definition", "meaning", "glossary", "translation",
    "translation_", "explanation", "sense", "desc"
]


def _get_addon_name() -> str:
    """
    Safely retrieves the root Anki add-on name for config persistence.
    """
    if hasattr(mw, 'addonManager'):
        root_name = mw.addonManager.addonFromModule(__name__)
        if root_name:
            return root_name
    return __name__.split('.')[0]


def _find_best_field_match(fields: List[str], keywords: List[str], fallback: str = None) -> Optional[str]:
    """
    Finds the best matching field name based on keyword similarity.
    """
    if not fields or not keywords:
        return fallback

    fields_lower = [f.lower().replace("_", " ").replace("-", " ") for f in fields]

    for keyword in keywords:
        for i, f_lower in enumerate(fields_lower):
            if f_lower == keyword.lower():
                return fields[i]
        for i, f_lower in enumerate(fields_lower):
            if keyword.lower() in f_lower:
                return fields[i]

    return fallback


class ConfigDialog(QDialog):
    """
    Dialog for configuring CompreDef add-on options.

    Allows mapping note types and fields, and ordering dictionaries in the Ladder.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("CompreDef Configuration")
        self.resize(660, 680)

        self.addon_name = _get_addon_name()
        self.config: Dict[str, Any] = mw.addonManager.getConfig(self.addon_name) or {}

        # Paths the user unchecked: kept in config (order preserved) but
        # skipped during generation. Stored as a set for O(1) toggling.
        self.disabled_dicts: set = set()

        self._init_ui()
        self._load_config()

    def _init_ui(self) -> None:
        """Sets up the form controls and layout structure."""
        main_layout = QVBoxLayout()
        self.setLayout(main_layout)
        # Airier separation between groups prevents a "wall of widgets".
        main_layout.setSpacing(10)

        # -------------------------------------------------------------
        # Note Types & Field Mappings Group
        # -------------------------------------------------------------
        mapping_group = QGroupBox("Note Types & Field Mappings")
        outer_mapping_layout = QVBoxLayout()
        mapping_group.setLayout(outer_mapping_layout)

        intro = QLabel(
            "Check every note type CompreDef should generate definitions for,\n"
            "then map its fields below. Unchecked types are ignored."
        )
        intro.setStyleSheet("color: gray; font-size: 11px;")
        intro.setWordWrap(True)
        outer_mapping_layout.addWidget(intro)

        # Checkable list of all note types (same interaction pattern as
        # the dictionary ladder: checkbox = enabled, click = configure).
        self.note_types_list = QListWidget()
        # Minimum visible rows even when the dialog is resized small.
        self.note_types_list.setMinimumHeight(96)
        self.note_types_list.setToolTip(
            "Check a note type to make it a generation target.\n"
            "Select a row to view/edit its field mapping below."
        )
        self.note_types_list.itemChanged.connect(self._on_type_item_changed)
        self.note_types_list.currentRowChanged.connect(
            lambda _row: self._on_type_selected()
        )
        outer_mapping_layout.addWidget(self.note_types_list)

        mapping_layout = QFormLayout()
        outer_mapping_layout.addLayout(mapping_layout)

        self.word_field_combo = QComboBox()
        mapping_layout.addRow("Target Word Field:", self.word_field_combo)

        self.reading_field_combo = QComboBox()
        self.reading_field_combo.setToolTip(
            "Optional. Field containing the word's reading (plain kana or furigana\n"
            "markup like 先[ま]ず). Used to pick the correct definition for words\n"
            "with multiple readings (e.g. 先ず read まず vs せんず)."
        )
        mapping_layout.addRow("Reading Field (optional):", self.reading_field_combo)

        self.definition_field_combo = QComboBox()
        mapping_layout.addRow("Definition Field:", self.definition_field_combo)

        main_layout.addWidget(mapping_group)

        # -------------------------------------------------------------
        # Dictionary Ladder Group
        # -------------------------------------------------------------
        ladder_group = QGroupBox("Dictionary Ladder (Order of Preference)")
        ladder_layout = QVBoxLayout()
        ladder_group.setLayout(ladder_layout)

        # --- Dictionary Source selector (Local vs Yomitan) ---
        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("Dictionary Source:"))
        self.source_combo = QComboBox()
        self.source_combo.addItem("Local dictionaries (indexed)", "local")
        self.source_combo.addItem("Yomitan API (live, all Yomitan dictionaries)", "yomitan")
        self.source_combo.setToolTip(
            "Local: use indexed dictionaries below (fast, offline).\n"
            "Yomitan API: borrow Yomitan's dictionaries live via http://127.0.0.1:19633\n"
            "(browser must be open + Yomitan API enabled + one-time 'python install_yomitan_api.py').\n"
            "Without the bridge the Test below will stay red — Local is simpler for most users."
        )
        # Restore at creation for crash-safety (same as tab_generate)
        _src = str(self.config.get("dictionary_source") or "local").strip().lower()
        if _src not in ("local", "yomitan"):
            _src = "local"
        for i in range(self.source_combo.count()):
            if self.source_combo.itemData(i) == _src:
                self.source_combo.setCurrentIndex(i)
                break
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        source_row.addWidget(self.source_combo)
        source_row.addStretch()
        ladder_layout.addLayout(source_row)

        # --- Yomitan settings row (visible only when Yomitan selected) ---
        self.yomitan_settings_widget = QWidget()
        yomitan_layout = QHBoxLayout()
        yomitan_layout.setContentsMargins(0, 0, 0, 0)
        self.yomitan_settings_widget.setLayout(yomitan_layout)
        yomitan_layout.addWidget(QLabel("Yomitan URL:"))
        self.yomitan_url_edit = QLineEdit()
        self.yomitan_url_edit.setPlaceholderText("http://127.0.0.1:19633")
        self.yomitan_url_edit.setText(str(self.config.get("yomitan_url") or "http://127.0.0.1:19633"))
        self.yomitan_url_edit.setToolTip("Yomitan API bridge URL (default 127.0.0.1:19633). Change only if you edited yomitan_api.py ADDR/PORT.")
        yomitan_layout.addWidget(self.yomitan_url_edit, stretch=1)
        self.yomitan_install_btn = _size_button(QPushButton("Install / Repair Bridge"))
        self.yomitan_install_btn.setToolTip(
            "One-click install of the Yomitan bridge (native messaging host).\n"
            "Writes the manifest for Chrome/Firefox/Brave/Edge so Yomitan's\n"
            "'Enable Yomitan API' actually exposes http://127.0.0.1:19633.\n"
            "No terminal needed — just click, then restart browser."
        )
        self.yomitan_install_btn.clicked.connect(self._on_install_yomitan_bridge)
        yomitan_layout.addWidget(self.yomitan_install_btn)
        self.yomitan_test_btn = _size_button(QPushButton("Test"))
        self.yomitan_test_btn.setToolTip("Ping Yomitan API (/serverVersion). Browser must be open + Yomitan API enabled + bridge installed.")
        self.yomitan_test_btn.clicked.connect(self._on_test_yomitan)
        yomitan_layout.addWidget(self.yomitan_test_btn)
        ladder_layout.addWidget(self.yomitan_settings_widget)

        self.yomitan_status_label = QLabel("")
        self.yomitan_status_label.setStyleSheet("color: gray; font-size: 11px;")
        self.yomitan_status_label.setWordWrap(True)
        ladder_layout.addWidget(self.yomitan_status_label)

        desc_label = QLabel(
            "Dictionaries are tried top to bottom; the first definition you can\n"
            "fully read (100% known kanji) wins. Recommended: put the richest\n"
            "dictionary you can comfortably read at the top. The gate counts\n"
            "kanji only — kana is never checked."
        )
        desc_label.setStyleSheet("color: gray; font-size: 11px;")
        desc_label.setWordWrap(True)
        ladder_layout.addWidget(desc_label)

        # List Widget with drag-and-drop reordering
        list_and_buttons_layout = QHBoxLayout()

        # --- Left/center: the ladder list itself ---
        list_column = QVBoxLayout()

        # Add-actions sit in ONE compact row ABOVE the list (they were a
        # 3-button stack in the side column, which wasted vertical budget
        # and made the dialog cram everything below it).
        add_row = QHBoxLayout()
        self.add_zip_btn = _size_button(QPushButton("Add Zip..."))
        self.add_zip_btn.setToolTip("Select a Yomitan dictionary .zip file")
        self.add_zip_btn.clicked.connect(self._on_add_zip)
        add_row.addWidget(self.add_zip_btn)

        self.add_dict_btn = _size_button(QPushButton("Add Folder..."))
        self.add_dict_btn.setToolTip("Select a single unzipped dictionary folder")
        self.add_dict_btn.clicked.connect(self._on_add_dictionary)
        add_row.addWidget(self.add_dict_btn)

        self.add_folder_btn = _size_button(QPushButton("Scan Folder..."))
        self.add_folder_btn.setToolTip(
            "Scan a parent folder to automatically find and add all "
            "dictionary archives (.zip) and subfolders"
        )
        self.add_folder_btn.clicked.connect(self._on_scan_folder)
        add_row.addWidget(self.add_folder_btn)

        add_row.addStretch()
        list_column.addLayout(add_row)

        self.dict_list = QListWidget()
        self.dict_list.setDragDropMode(_internal_move_mode())
        self.dict_list.setMinimumHeight(120)
        # Track checkbox changes to update disabled_dicts
        self.dict_list.itemChanged.connect(self._on_item_changed)
        self.dict_list.model().rowsMoved.connect(lambda *_: self._refresh_item_labels())
        list_column.addWidget(self.dict_list)
        list_and_buttons_layout.addLayout(list_column, stretch=1)

        # --- Right: list-management actions only ---
        buttons_vbox = QVBoxLayout()

        self.move_up_btn = _size_button(QPushButton("Move Up ↑"))
        self.move_up_btn.setToolTip("Move selected dictionary earlier in ladder (tried earlier)")
        self.move_up_btn.clicked.connect(self._on_move_up)
        buttons_vbox.addWidget(self.move_up_btn)

        self.move_down_btn = _size_button(QPushButton("Move Down ↓"))
        self.move_down_btn.setToolTip("Move selected dictionary later in ladder (more advanced)")
        self.move_down_btn.clicked.connect(self._on_move_down)
        buttons_vbox.addWidget(self.move_down_btn)

        buttons_vbox.addSpacing(10)

        self.remove_btn = _size_button(QPushButton("Remove"))
        self.remove_btn.setToolTip("Remove selected dictionary from ladder (and its index)")
        self.remove_btn.clicked.connect(self._on_remove_dictionary)
        buttons_vbox.addWidget(self.remove_btn)

        self.reindex_btn = _size_button(QPushButton("Reinstall / Update"))
        self.reindex_btn.setToolTip(
            "Re-parse the selected dictionary and rebuild its index.\n"
            "Use this after replacing a dictionary's files on disk.\n"
            "Runs in the background with progress."
        )
        self.reindex_btn.clicked.connect(self._on_reindex_dictionary)
        buttons_vbox.addWidget(self.reindex_btn)

        buttons_vbox.addStretch()
        list_and_buttons_layout.addLayout(buttons_vbox)

        ladder_layout.addLayout(list_and_buttons_layout)
        main_layout.addWidget(ladder_group)

        # -------------------------------------------------------------
        # Generation Group (Tab-to-Generate toggle)
        # -------------------------------------------------------------
        generation_group = QGroupBox("Generation")
        generation_layout = QVBoxLayout()
        generation_group.setLayout(generation_layout)

        # Tab-to-Generate: auto-fill the definition when the word field is
        # unfocused with an empty definition (restored feature — see
        # editor_browser.py for the stability contract).
        self.tab_generate_check = QCheckBox(
            "Tab-to-Generate: fill empty definition when leaving the word field "
            "(Tab / clicking away)"
        )
        self.tab_generate_check.setToolTip(
            "When enabled, unfocusing the word field automatically generates a "
            "definition\nif (and only if) the definition field is empty. Existing "
            "definitions are never\noverwritten — use the CD toolbar button for that."
        )
        # CRITICAL: restore the saved state AT CREATION TIME, before any
        # _save_config_now() can fire. _load_config() restores the dictionary
        # ladder AFTER _init_ui(), and each added dictionary persists the
        # dialog state immediately (crash-safety design). With the default
        # unchecked Qt state, merely OPENING the dialog used to write
        # tab_generate=False to disk before the real value was ever shown.
        self.tab_generate_check.setChecked(bool(self.config.get("tab_generate", True)))
        generation_layout.addWidget(self.tab_generate_check)

        self.plain_text_check = QCheckBox(
            "Plain-text definitions: store plain text instead of Yomitan HTML"
        )
        self.plain_text_check.setToolTip(
            "When enabled, NEW dictionary installs render definitions as plain\n"
            "text (no <ruby>, <span>, data-sc-* or inline CSS). Existing\n"
            "indexed entries are converted on-the-fly via cheap HTML stripping,\n"
            "so toggling does not require a full re-index — but new installs\n"
            "skip HTML generation entirely (saves the costly HTML build)."
        )
        # Same crash-safety timing as tab_generate: restore at creation.
        self.plain_text_check.setChecked(bool(self.config.get("plain_text_definitions", False)))
        generation_layout.addWidget(self.plain_text_check)

        main_layout.addWidget(generation_group)

        # -------------------------------------------------------------
        # OK / Cancel Dialog Buttons
        # -------------------------------------------------------------
        if hasattr(QDialogButtonBox, "StandardButton"):
            ok_flag = QDialogButtonBox.StandardButton.Ok
            cancel_flag = QDialogButtonBox.StandardButton.Cancel
        else:
            ok_flag = QDialogButtonBox.Ok
            cancel_flag = QDialogButtonBox.Cancel

        button_box = QDialogButtonBox(ok_flag | cancel_flag)
        button_box.accepted.connect(self._save_and_accept)
        button_box.rejected.connect(self.reject)
        main_layout.addWidget(button_box)

    def _get_field_names(self, note_type_name: str) -> List[str]:
        """Retrieves field names for the given note type using mw.col.models."""
        if not mw or not mw.col or not note_type_name:
            return []
        model = mw.col.models.by_name(note_type_name)
        if not model or "flds" not in model:
            return []
        return [field["name"] for field in model["flds"]]

    def _on_note_type_changed(self, note_type_name: str) -> None:
        """Populates the field dropdowns for a note type and auto-matches.

        Called when a type's row is selected or checked for the first
        time. Auto-match runs only for types without saved mappings —
        saved mappings survive every re-selection untouched.
        """
        fields = self._get_field_names(note_type_name)

        # Store previous selections
        prev_word = self.word_field_combo.currentText()
        prev_reading = self.reading_field_combo.currentText()
        prev_def = self.definition_field_combo.currentText()

        # Update dropdown items
        self.word_field_combo.clear()
        self.word_field_combo.addItems(fields)

        self.reading_field_combo.blockSignals(True)
        self.reading_field_combo.clear()
        self.reading_field_combo.addItem("")
        self.reading_field_combo.addItems(fields)
        self.reading_field_combo.blockSignals(False)

        self.definition_field_combo.clear()
        self.definition_field_combo.addItems(fields)

        # Compute best matches for new fields
        auto_word = _find_best_field_match(fields, _TARGET_WORD_KEYWORDS)

        # Prefer dedicated reading/furigana field, else word field itself
        auto_reading = _find_best_field_match(fields, _READING_FIELD_KEYWORDS) or \
                       (auto_word if auto_word else "")

        # Definition usually not the word field
        remaining_fields = [f for f in fields if f != auto_word]
        auto_def = _find_best_field_match(remaining_fields, _DEFINITION_KEYWORDS)

        # Restore previous or set auto-match
        self.word_field_combo.setCurrentText(prev_word if prev_word in fields else (auto_word or ""))
        self.reading_field_combo.setCurrentText(prev_reading if prev_reading in fields else (auto_reading or ""))
        self.definition_field_combo.setCurrentText(prev_def if prev_def in fields else (auto_def or ""))

    # ------------------------------------------------------------------
    # Multi-note-type state: {type_name: {'word_field', 'reading_field',
    # 'definition_field', '_auto': True until user edits anything}}
    # ------------------------------------------------------------------

    def _load_type_mappings(self) -> None:
        """
        Populates the checkable note-type list and restores mappings
        from config (multi-type 'targets' with legacy fallback).
        """
        self.type_mappings: Dict[str, Dict[str, str]] = {}
        saved_targets = self.config.get("targets")
        if isinstance(saved_targets, dict) and saved_targets:
            for type_name, mapping in saved_targets.items():
                if isinstance(mapping, dict):
                    self.type_mappings[str(type_name)] = {
                        "word_field": str(mapping.get("word_field", "") or ""),
                        "reading_field": str(mapping.get("reading_field", "") or ""),
                        "definition_field": str(mapping.get("definition_field", "") or ""),
                        "_auto": False,
                    }
        elif self.config.get("note_type"):
            # Legacy single-type config: one target, fields as configured.
            self.type_mappings[str(self.config["note_type"])] = {
                "word_field": str(self.config.get("word_field", "") or ""),
                "reading_field": str(self.config.get("reading_field", "") or ""),
                "definition_field": str(self.config.get("definition_field", "") or ""),
                "_auto": False,
            }

        role = _user_role()
        self.note_types_list.blockSignals(True)
        self.note_types_list.clear()
        if mw and mw.col:
            for name in mw.col.models.all_names():
                item = QListWidgetItem(name)
                item.setFlags(item.flags() | _user_checkable_flag())
                checked = name in self.type_mappings
                # Qt.CheckState works unscoped (PyQt5) AND scoped (PyQt6)
                item.setCheckState(
                    Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
                )
                item.setData(role, name)
                self.note_types_list.addItem(item)
        self.note_types_list.blockSignals(False)

    def _on_type_item_changed(self, item: QListWidgetItem) -> None:
        """Checkbox toggled: track mapping edits per type."""
        role = _user_role()
        type_name = item.data(role)
        if not type_name:
            return
        is_checked = item.checkState() == Qt.CheckState.Checked
        if is_checked and type_name not in self.type_mappings:
            # First check: seed the mapping via auto-match (fields
            # populate when the row is selected).
            self.type_mappings[type_name] = {
                "word_field": "", "reading_field": "",
                "definition_field": "", "_auto": True,
            }
        elif not is_checked and type_name in self.type_mappings:
            # Unchecking removes the mapping immediately (re-checking
            # re-runs auto-match; the user said 'not this type').
            del self.type_mappings[type_name]

    def _on_type_selected(self) -> None:
        """A row was selected: load that type's mapping into the form."""
        item = self.note_types_list.currentItem()
        if item is None:
            return
        role = _user_role()
        type_name = item.data(role)
        if not type_name:
            return
        # Remember the previously edited type's dropdown values.
        self._stash_current_mapping()
        self._active_type = type_name
        self._on_note_type_changed(type_name)
        mapping = self.type_mappings.get(type_name, {})
        if mapping and not mapping.get("_auto"):
            # Saved mapping wins over auto-match.
            self.word_field_combo.setCurrentText(mapping.get("word_field", ""))
            self.reading_field_combo.setCurrentText(mapping.get("reading_field", ""))
            self.definition_field_combo.setCurrentText(mapping.get("definition_field", ""))

    def _stash_current_mapping(self) -> None:
        """Saves the visible dropdowns into the active type's mapping."""
        active = getattr(self, "_active_type", None)
        if not active or active not in self.type_mappings:
            return
        self.type_mappings[active].update({
            "word_field": self.word_field_combo.currentText().strip(),
            "reading_field": self.reading_field_combo.currentText().strip(),
            "definition_field": self.definition_field_combo.currentText().strip(),
            "_auto": False,
        })

    def _refresh_item_labels(self) -> None:
        """Updates labels: '[n] Title ✓' and syncs checkbox state."""
        # When Yomitan is active the local list is disabled — keep it as-is
        try:
            if hasattr(self, "source_combo") and self.source_combo.currentData() == "yomitan":
                return
        except Exception:
            pass
        role = _user_role()
        # Prepare a local provider once for title/count (Yomitan provider would return "Yomitan" for any path)
        _local_prov = None
        try:
            from .provider import LocalSQLiteProvider as _Local
            import os as _os
            _addon_dir = _os.path.dirname(_os.path.abspath(__file__))
            _cache_dir = _os.path.join(_addon_dir, "user_files", "cache")
            _os.makedirs(_cache_dir, exist_ok=True)
            _local_prov = _Local(_cache_dir)
        except Exception:
            pass
        # Block signals to prevent itemChanged from triggering a loop during refresh
        self.dict_list.blockSignals(True)
        for i in range(self.dict_list.count()):
            item = self.dict_list.item(i)
            path = item.data(role)
            try:
                if _local_prov is not None:
                    title = _local_prov.get_title(path)
                    if _local_prov.is_installed(path):
                        count = _local_prov.get_entry_count(path)
                        status = f"✓ ({count:,} entries)"
                    else:
                        status = "⚠ not indexed — re-add to install"
                else:
                    title = get_provider().get_title(path)
                    provider = get_provider()
                    if provider.is_installed(path):
                        count = provider.get_entry_count(path)
                        status = f"✓ ({count:,} entries)"
                    else:
                        status = "⚠ not indexed — re-add to install"
            except Exception:
                title = path
                status = "⚠ error"
            item.setText(f"[{i + 1}] {title} {status}")
            item.setCheckState(Qt.CheckState.Checked if path not in self.disabled_dicts else Qt.CheckState.Unchecked)
            item.setToolTip(
                path + ("\n(disabled — skipped during generation)" if path in self.disabled_dicts else "")
            )
        self.dict_list.blockSignals(False)

    def _on_source_changed(self, _idx: int = 0, save: bool = True) -> None:
        """Toggles Yomitan vs Local UI and persists immediately.

        save=False is used during initial load to avoid overwriting the
        ladder with an empty list before _load_config populates it
        (the v1.0.20 'disappearing dictionaries' bug).
        """
        is_yomitan = self.source_combo.currentData() == "yomitan"
        # Show/hide Yomitan URL row + status
        self.yomitan_settings_widget.setVisible(is_yomitan)
        self.yomitan_status_label.setVisible(is_yomitan)
        if is_yomitan:
            self.yomitan_status_label.setText(
                "Yomitan mode: live dictionaries from browser (no indexing needed).\n"
                "Click 'Install / Repair Bridge' once (no terminal), restart browser,\n"
                "then enable Yomitan → Settings → Advanced → General → Enable Yomitan API.\n"
                "After that 'Test' should turn green."
            )
        # Grey out local ladder when Yomitan is active (still visible for reference)
        for w in (self.dict_list, self.add_zip_btn, self.add_dict_btn, self.add_folder_btn,
                  self.move_up_btn, self.move_down_btn, self.remove_btn, self.reindex_btn):
            w.setEnabled(not is_yomitan)
        if save:
            # Persist instantly so closing dialog keeps choice (same crash-safety as others)
            try:
                self._save_config_now()
            except Exception:
                pass
            # Reset provider singleton so next get_provider() reads new source
            try:
                from .core import reset_provider_cache  # type: ignore
                reset_provider_cache()
            except Exception:
                try:
                    from core import reset_provider_cache  # type: ignore
                    reset_provider_cache()
                except Exception:
                    pass

    def _on_test_yomitan(self) -> None:
        """Pings Yomitan API and shows result in status label + tooltip."""
        url = self.yomitan_url_edit.text().strip() or "http://127.0.0.1:19633"
        self.yomitan_status_label.setText(f"Testing {url} ...")
        self.yomitan_status_label.setStyleSheet("color: gray; font-size: 11px;")
        # Save URL first so test uses what user typed
        try:
            self._save_config_now()
        except Exception:
            pass
        def task():
            import json, urllib.request, urllib.error
            # Step 1: Check bridge HTTP server (serverVersion) — does not need Yomitan
            bridge_data = None
            bridge_err = None
            for path in ("/serverVersion",):
                try:
                    req = urllib.request.Request(url.rstrip("/") + path, data=b"{}", headers={"Content-Type":"application/json"}, method="POST")
                    with urllib.request.urlopen(req, timeout=2.0) as resp:
                        body = resp.read().decode("utf-8")
                        bridge_data = json.loads(body) if body else {}
                        bridge_err = None
                        break
                except Exception as e:
                    bridge_err = e
                    continue
            if bridge_data is None:
                return (None, None, bridge_err, None)
            # Step 2: Check Yomitan dictionaries via ankiFields (needs browser + Yomitan + dictionaries)
            # Use a simple kanji "口" that should exist in any Japanese dict
            try:
                payload = json.dumps({"text": "口", "type": "term", "markers": ["glossary"], "maxEntries": 1, "includeMedia": False}).encode()
                req = urllib.request.Request(url.rstrip("/") + "/ankiFields", data=payload, headers={"Content-Type":"application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    body = resp.read().decode("utf-8")
                    data = json.loads(body) if body else {}
                    fields = data.get("fields") if isinstance(data, dict) else None
                    if isinstance(fields, list) and fields:
                        return ("ankiFields", {"yomitan_fields": len(fields)}, None, bridge_data)
                    else:
                        # Bridge is up but Yomitan returned no fields — could be no dict or Yomitan not enabled
                        return ("ankiFields", None, "Yomitan returned no fields for '口' (is Yomitan enabled with dictionaries?)", bridge_data)
            except urllib.error.HTTPError as he:
                try:
                    body = he.read().decode("utf-8")
                    err_data = json.loads(body) if body else {}
                    err_msg = err_data.get("error") if isinstance(err_data, dict) else str(err_data)
                except Exception:
                    err_msg = str(he)
                return (None, None, f"Yomitan not connected (HTTP {he.code}): {err_msg}. Browser must be open + Yomitan API enabled.", bridge_data)
            except Exception as e:
                return (None, None, e, bridge_data)
            return ("/serverVersion", bridge_data, None, None)
        def on_done(future):
            try:
                path, data, err, bridge_data = future.result()
                if data is not None and bridge_data is not None and path == "ankiFields":
                    # Full success: bridge + Yomitan dictionaries
                    self.yomitan_status_label.setText(f"✓ Yomitan OK (bridge {bridge_data} + dictionaries: {data}) — ready for '口'.")
                    self.yomitan_status_label.setStyleSheet("color: green; font-size: 11px;")
                    tooltip(f"Yomitan OK: {data}")
                elif data is not None and path == "/serverVersion":
                    # Bridge is up but Yomitan dictionaries not yet available — show yellow warning
                    # This happens when bridge was manually started but Yomitan not yet launched it
                    self.yomitan_status_label.setText(f"⚠ Bridge running at {url} ({data}) but Yomitan not yet connected — restart browser and enable Yomitan → Settings → Advanced → General → Enable Yomitan API, then Test again.")
                    self.yomitan_status_label.setStyleSheet("color: orange; font-size: 11px;")
                    tooltip(f"Bridge OK but Yomitan not connected: {data}")
                elif data is not None:
                    self.yomitan_status_label.setText(f"✓ Yomitan OK ({path}: {data}) — all Yomitan dictionaries available.")
                    self.yomitan_status_label.setStyleSheet("color: green; font-size: 11px;")
                    tooltip(f"Yomitan OK: {data}")
                else:
                    # Check if bridge_data is available (bridge is running) but Yomitan failed
                    if bridge_data is not None:
                        msg = (
                            f"⚠ Bridge running at {url} ({bridge_data}) but Yomitan not reachable ({err}).\n"
                            f"Browser must be open, Yomitan → Settings → Advanced → General → Enable Yomitan API must be ON (restart browser after enabling).\n"
                            f"Without Yomitan, CompreDef cannot fetch '口' — switch to Local if you don't want the bridge."
                        )
                        self.yomitan_status_label.setText(msg)
                        self.yomitan_status_label.setStyleSheet("color: orange; font-size: 11px;")
                    else:
                        msg = (
                            f"✗ Yomitan not reachable at {url} ({err}).\n"
                            f"Browser must be open, Yomitan → Settings → Advanced → General → Enable Yomitan API must be ON,\n"
                            f"and click 'Install / Repair Bridge' above once (no terminal) then restart browser.\n"
                            f"Without that bridge CompreDef cannot see Yomitan's dictionaries — switch to Local if you don't want the extra step."
                        )
                        self.yomitan_status_label.setText(msg)
                    self.yomitan_status_label.setStyleSheet("color: red; font-size: 11px;")
                    print(f"CompreDef: Yomitan test failed: {err}")
                    tooltip(msg, parent=self)
            except Exception as e:
                import traceback
                self.yomitan_status_label.setText(f"✗ Test error: {e}")
                self.yomitan_status_label.setStyleSheet("color: red; font-size: 11px;")
                print(traceback.format_exc())
        try:
            mw.taskman.run_in_background(task, on_done)
        except Exception:
            # Fallback synchronous for tests
            task()

    def _on_install_yomitan_bridge(self) -> None:
        """One-click install of the Yomitan native host — no terminal needed."""
        self.yomitan_status_label.setText("Installing Yomitan bridge ...")
        self.yomitan_status_label.setStyleSheet("color: gray; font-size: 11px;")
        try:
            self._save_config_now()
        except Exception:
            pass
        def task():
            try:
                if __package__:
                    from .yomitan_installer import install_bridge
                else:
                    from yomitan_installer import install_bridge
                results = install_bridge()
                return (results, None)
            except Exception as e:
                import traceback
                return (None, traceback.format_exc())

        def on_done(future):
            try:
                results, err = future.result()
                if err:
                    self.yomitan_status_label.setText(f"✗ Install failed: {err}")
                    self.yomitan_status_label.setStyleSheet("color: red; font-size: 11px;")
                    print(f"CompreDef: Yomitan bridge install failed:\n{err}")
                    tooltip(f"Yomitan bridge install failed — see console", parent=self)
                    return
                # Clear Yomitan negative cache so Test retries immediately
                try:
                    if __package__:
                        from .yomitan import clear_yomitan_cache
                    else:
                        from yomitan import clear_yomitan_cache
                    clear_yomitan_cache()
                except Exception:
                    pass
                # Summarize per-browser (exclude _bridge)
                if isinstance(results, dict) and results:
                    # _bridge is the HTTP server, not a browser
                    bridge_ok, bridge_msg = results.get("_bridge", (False, "")) if "_bridge" in results else (False, "")
                    browser_results = {k: v for k, v in results.items() if not k.startswith("_")}
                    ok = [b for b, (s, _) in browser_results.items() if s]
                    fail = [(b, m) for b, (s, m) in browser_results.items() if not s]
                    if ok:
                        if bridge_ok:
                            msg = f"✓ Bridge installed for: {', '.join(ok)} and started ({bridge_msg}). Click Test — should turn green (if Yomitan API enabled, else enable it, no restart needed for Test)."
                        else:
                            msg = f"✓ Bridge installed for: {', '.join(ok)}. Restart browser, then enable Yomitan → Settings → Advanced → General → Enable Yomitan API, then click Test."
                            if bridge_msg:
                                msg += f" (bridge start: {bridge_msg})"
                        self.yomitan_status_label.setText(msg)
                        self.yomitan_status_label.setStyleSheet("color: green; font-size: 11px;")
                        print(f"CompreDef: Yomitan bridge install results: {results}")
                        tooltip(f"Yomitan bridge installed — {bridge_msg}", parent=self)
                    if fail:
                        print(f"CompreDef: Yomitan bridge partial failures: {fail}")
                        if not ok:
                            self.yomitan_status_label.setText(f"✗ Bridge install failed for all browsers: {fail} | bridge: {bridge_msg}")
                            self.yomitan_status_label.setStyleSheet("color: red; font-size: 11px;")
                    if not ok and not fail and bridge_ok:
                        # Only bridge started, browsers may have failed but bridge is running for Test
                        self.yomitan_status_label.setText(f"✓ Bridge started ({bridge_msg}). Click Test.")
                        self.yomitan_status_label.setStyleSheet("color: green; font-size: 11px;")
                else:
                    self.yomitan_status_label.setText("✗ Bridge install returned no results — see console")
                    self.yomitan_status_label.setStyleSheet("color: red; font-size: 11px;")
                # Auto-test after install (give bridge a moment)
                try:
                    from aqt.qt import QTimer
                    QTimer.singleShot(1200, self._on_test_yomitan)
                except Exception:
                    try:
                        self._on_test_yomitan()
                    except Exception:
                        pass
            except Exception as e:
                import traceback
                self.yomitan_status_label.setText(f"✗ Install error: {e}")
                self.yomitan_status_label.setStyleSheet("color: red; font-size: 11px;")
                print(traceback.format_exc())

        try:
            mw.taskman.run_in_background(task, on_done)
        except Exception:
            # Fallback synchronous
            res = task()
            class FakeFuture:
                def result(self): return res
            on_done(FakeFuture())

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        """Handles checkbox toggles to update disabled_dicts set."""
        role = _user_role()
        path = item.data(role)
        if not path:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self.disabled_dicts.discard(path)
        else:
            self.disabled_dicts.add(path)

    def _on_toggle_dictionary(self) -> None:
        """Deprecated: handled by checkboxes now."""
        pass

    def _add_dict_path(self, path: str) -> bool:
        """Adds a dictionary path (zip or folder) to the ladder list if not already present."""
        if not path:
            return False

        norm_path = os.path.realpath(os.path.expanduser(path))
        is_zip = is_zip_dictionary(norm_path)

        if not is_zip and not os.path.isdir(norm_path):
            return False

        role = _user_role()

        # Check for duplicates
        for i in range(self.dict_list.count()):
            existing_path = self.dict_list.item(i).data(role)
            if existing_path == norm_path:
                return False

        item = QListWidgetItem()
        item.setData(role, norm_path)
        self.dict_list.addItem(item)
        self._refresh_item_labels()

        # Persist the addition NOW, BEFORE the install starts: if Anki
        # crashes mid-install or the user closes the dialog, the new
        # dictionary list must survive (a crash once silently reverted a
        # completed 'remove all + re-import' to the old list).
        self._save_config_now()

        # INSTALL-TIME INDEXING: a newly added dictionary is parsed and
        # indexed exactly once, here, as a background operation with a
        # progress dialog. Normal lookups never parse it again.
        self._install_dictionary_in_background(norm_path)
        return True

    def _install_dictionary_in_background(self, path: str) -> None:
        """
        Installs (indexes) one dictionary in the background with progress.

        Anki stays fully responsive — indexing runs on a background thread
        and the progress dialog shows 'Indexing <title>... N%'. When it
        completes, the dictionary's index persists in SQLite and every
        future lookup is a pure database query.
        """
        dict_obj = get_single_dictionary(path)

        # Already indexed from exactly these files? Nothing to do.
        try:
            if dict_obj.is_indexed() and dict_obj.index_is_current():
                return
        except Exception:
            pass  # fall through and (re-)install

        title = dict_obj.title or os.path.basename(path)

        # Simple non-blocking progress dialog the user can cancel.
        from aqt.qt import QTimer, QDialog, QLabel, QVBoxLayout, QPushButton
        state = {"done": 0, "total": 0, "finished": False, "error": None}
        cancelled = {"flag": False}

        progress = QDialog(self)
        progress.setWindowTitle("CompreDef")
        progress.setModal(True)
        label = QLabel(f"Indexing {title}...\nPreparing...")
        layout = QVBoxLayout()
        layout.addWidget(label)
        bar = QLabel("")  # textual percent; avoids QProgressBar API drift
        layout.addWidget(bar)
        cancel_btn = QPushButton("Cancel")
        layout.addWidget(cancel_btn)
        progress.setLayout(layout)
        cancel_btn.clicked.connect(lambda: cancelled.__setitem__("flag", True))

        def poll() -> None:
            # Update from the latest shared state written by the worker.
            done, total = state.get("done", 0), state.get("total", 0)
            if total:
                pct = min(100, done * 100 // total)
                bar.setText(f"{pct}%  ({done:,} / {total:,} entries)")
            if state.get("finished"):
                progress.accept()

        timer = QTimer(progress)
        timer.timeout.connect(poll)

        def progress_cb(done: int, total: int) -> None:
            # Called on the worker thread: just record numbers; the Qt
            # timer on the main thread does the actual UI update.
            state["done"], state["total"] = done, total

        def cancel_check() -> bool:
            return cancelled["flag"]

        def task() -> int:
            return get_provider().install(path, progress_cb, cancel_check)

        def on_done(future) -> None:
            timer.stop()
            state["finished"] = True
            try:
                count = future.result()
                msg = f"CompreDef: '{title}' indexed ({count:,} entries)."
                print(msg)
                tooltip(msg)
            except IndexingError as e:
                state["error"] = str(e)
                print(f"CompreDef: Indexing '{title}' FAILED: {e}")
                tooltip(f"CompreDef: Indexing '{title}' failed: {e}")
            except Exception:
                import traceback
                err = traceback.format_exc()
                print(f"CompreDef: Indexing '{title}' crashed:\n{err}")
                tooltip(f"CompreDef: Indexing '{title}' crashed — see console.")
            finally:
                try:
                    progress.accept()
                    self._refresh_item_labels()
                except Exception:
                    import traceback
                    print(f"CompreDef: install-dialog cleanup error:\n{traceback.format_exc()}")

        # Show dialog and start worker; dialog closes itself on completion.
        timer.start(250)
        mw.taskman.run_in_background(task, on_done)
        progress.exec()

    def _on_add_dictionary(self) -> None:
        """Opens folder dialog to add an individual unzipped dictionary folder."""
        folder = QFileDialog.getExistingDirectory(self, "Select Dictionary Directory")
        if folder:
            self._add_dict_path(folder)

    def _on_add_zip(self) -> None:
        """Opens file dialog to add a Yomitan dictionary .zip archive."""
        zip_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Dictionary Zip File",
            "",
            "Yomitan Dictionaries (*.zip);;All Files (*)"
        )
        if zip_path:
            self._add_dict_path(zip_path)

    def _on_scan_folder(self) -> None:
        """Opens folder dialog and scans for all dictionary subfolders."""
        parent_folder = QFileDialog.getExistingDirectory(self, "Select Parent Folder with Dictionaries")
        if parent_folder:
            subfolders = find_dictionary_folders(parent_folder)
            for sub in subfolders:
                self._add_dict_path(sub)

    def _on_move_up(self) -> None:
        """Moves the currently selected dictionary up in ladder order."""
        curr_row = self.dict_list.currentRow()
        if curr_row > 0:
            item = self.dict_list.takeItem(curr_row)
            self.dict_list.insertItem(curr_row - 1, item)
            self.dict_list.setCurrentRow(curr_row - 1)
            self._refresh_item_labels()

    def _on_move_down(self) -> None:
        """Moves the currently selected dictionary down in ladder order."""
        curr_row = self.dict_list.currentRow()
        if curr_row >= 0 and curr_row < self.dict_list.count() - 1:
            item = self.dict_list.takeItem(curr_row)
            self.dict_list.insertItem(curr_row + 1, item)
            self.dict_list.setCurrentRow(curr_row + 1)
            self._refresh_item_labels()

    def _on_remove_dictionary(self) -> None:
        """
        Removes the selected dictionary from the ladder AND deletes its
        SQLite index, persisting the config immediately so a crash cannot
        silently revert the removal.
        """
        curr_row = self.dict_list.currentRow()
        if curr_row < 0:
            return
        role = _user_role()
        item = self.dict_list.item(curr_row)
        path = item.data(role)

        removed = self.dict_list.takeItem(curr_row)
        if removed is not None:
            self._refresh_item_labels()

        # Persist the removal NOW (crash-proofing the dialog state).
        self._save_config_now()

        # Drop the index too (background: deleting hundreds of thousands of
        # rows must never block the UI thread).
        if path:
            def task() -> None:
                get_provider().uninstall(path)

            def on_done(_future) -> None:
                self._refresh_item_labels()

            try:
                mw.taskman.run_in_background(task, on_done)
            except Exception:
                import traceback
                print(f"CompreDef: index removal failed:\n{traceback.format_exc()}")

    def _on_reindex_dictionary(self) -> None:
        """
        Explicitly reinstalls (re-indexes) the selected dictionary.

        This is the ONLY path (besides adding a new dictionary) that parses
        dictionary files — replacing a dictionary's files on disk is exactly
        the 'user explicitly installs/replaces' case from the architecture.
        """
        curr_row = self.dict_list.currentRow()
        if curr_row < 0:
            tooltip("Select a dictionary first.")
            return
        role = _user_role()
        path = self.dict_list.item(curr_row).data(role)
        if not path:
            return
        self._install_dictionary_in_background(path)

    def _load_config(self) -> None:
        """Populates form controls with current configuration values."""
        # Checkable note-type list + per-type mappings (multi-type mode)
        self._load_type_mappings()
        # Select the first checked type so the field form starts populated
        for row in range(self.note_types_list.count()):
            item = self.note_types_list.item(row)
            checked = item.checkState() == Qt.CheckState.Checked
            if checked:
                self.note_types_list.setCurrentRow(row)
                break

        # Load dictionary ladder
        saved_dicts = self.config.get("dictionaries", [])
        if not saved_dicts and self.config.get("dictionary_folder"):
            # Auto-detect from legacy single folder path
            saved_dicts = find_dictionary_folders(self.config["dictionary_folder"])
        # Recovery: if config was clobbered to [] by the v1.0.20 race but DB still
        # has indexed dictionaries, restore them so user doesn't need to re-import.
        if not saved_dicts:
            try:
                from .provider import LocalSQLiteProvider
                import os as _os
                _addon_dir = _os.path.dirname(_os.path.abspath(__file__))
                _cache_dir = _os.path.join(_addon_dir, "user_files", "cache")
                _db_path = _os.path.join(_cache_dir, "dictionaries.db")
                if _os.path.isfile(_db_path):
                    import sqlite3
                    _conn = sqlite3.connect(_db_path)
                    try:
                        _rows = _conn.execute("SELECT path FROM dictionaries").fetchall()
                        _db_paths = [r[0] for r in _rows if r[0] and _os.path.exists(r[0])]
                        if _db_paths:
                            saved_dicts = _db_paths
                            print(f"CompreDef: recovered {len(_db_paths)} dictionaries from DB after config was empty (v1.0.20 bug)")
                    finally:
                        _conn.close()
            except Exception:
                pass

        # Restore disabled set (paths normalized the same way as _add_dict_path)
        self.disabled_dicts = {
            os.path.realpath(os.path.expanduser(str(p)))
            for p in self.config.get("disabled_dictionaries", [])
            if p
        }

        for d_path in saved_dicts:
            self._add_dict_path(d_path)
        # Sync Yomitan/Local visibility after ladder is populated (must be
        # after _add_dict_path so ordered_dicts is not empty when we save).
        try:
            self._on_source_changed(save=False)
        except Exception:
            pass
        # NOTE: tab_generate_check's state is restored in _init_ui (at widget
        # creation) — NOT here. The dictionary loop above persists the dialog
        # state on every add (crash safety), so the checkbox must already
        # carry its saved value by the time _add_dict_path saves.

    def _collect_type_config(self) -> Dict[str, Any]:
        """
        Builds the note-type portion of the config: the multi-type
        'targets' dict plus a legacy mirror (note_type + flat fields) of
        the FIRST target so old configs and hand-edited config.json
        files keep working.
        """
        self._stash_current_mapping()  # dropdowns -> active type
        targets: Dict[str, Dict[str, str]] = {}
        for type_name, mapping in self.type_mappings.items():
            word = mapping.get("word_field", "")
            def_f = mapping.get("definition_field", "")
            # Only complete mappings can generate; incomplete ones are
            # kept in the UI but not saved as targets.
            if word and def_f:
                targets[type_name] = {
                    "word_field": word,
                    "reading_field": mapping.get("reading_field", ""),
                    "definition_field": def_f,
                }
        first_name = next(iter(targets), "")
        first = targets.get(first_name, {})
        return {
            "targets": targets,
            # Legacy mirror: first configured target in flat form.
            "note_type": first_name,
            "word_field": first.get("word_field", ""),
            "reading_field": first.get("reading_field", ""),
            "definition_field": first.get("definition_field", ""),
        }

    def _save_and_accept(self) -> None:
        """Saves settings to Anki config and closes dialog."""
        role = _user_role()
        ordered_dicts = [
            self.dict_list.item(i).data(role)
            for i in range(self.dict_list.count())
        ]

        updated_config = {
            **self._collect_type_config(),
            "dictionaries": ordered_dicts,
            # Disabled paths: kept in `dictionaries` for order preservation,
            # listed here so generation skips them.
            "disabled_dictionaries": sorted(self.disabled_dicts),
            # Tab-to-Generate (auto-fill on word-field unfocus)
            "tab_generate": self.tab_generate_check.isChecked(),
            "plain_text_definitions": self.plain_text_check.isChecked(),
            "dictionary_source": self.source_combo.currentData() or "local",
            "yomitan_url": self.yomitan_url_edit.text().strip() or "http://127.0.0.1:19633",
            # Backwards compatibility
            "dictionary_folder": ordered_dicts[0] if ordered_dicts else "",
            "mode": "Ladder",
        }

        mw.addonManager.writeConfig(self.addon_name, updated_config)
        # Reset provider singleton so next get_provider() respects new source
        try:
            from .core import reset_provider_cache  # type: ignore
            reset_provider_cache()
        except Exception:
            try:
                from core import reset_provider_cache  # type: ignore
                reset_provider_cache()
            except Exception:
                pass
        self.accept()

    def _save_config_now(self) -> None:
        """
        Persists the current dialog state to the config WITHOUT closing.

        Called before any background install starts: if Anki crashes or the
        user closes the dialog mid-install, their dictionary list changes
        are already durable (a crash once silently reverted a completed
        'remove all + re-import' to the previous dictionary list).
        """
        try:
            role = _user_role()
            ordered_dicts = [
                self.dict_list.item(i).data(role)
                for i in range(self.dict_list.count())
            ]
            # Never clobber a populated ladder with an empty one during
            # the initial Yomitan/Local toggle race (v1.0.20 bug).
            if not ordered_dicts and isinstance(self.config.get("dictionaries"), list) and self.config["dictionaries"]:
                # If the UI list is empty but config still has dictionaries, keep them
                # (happens only during the early _on_source_changed save before _load_config).
                ordered_dicts = list(self.config["dictionaries"])
            mw.addonManager.writeConfig(self.addon_name, {
                **self._collect_type_config(),
                "dictionaries": ordered_dicts,
                "disabled_dictionaries": sorted(self.disabled_dicts),
                "tab_generate": self.tab_generate_check.isChecked(),
                "plain_text_definitions": self.plain_text_check.isChecked(),
                "dictionary_source": self.source_combo.currentData() or "local",
                "yomitan_url": self.yomitan_url_edit.text().strip() or "http://127.0.0.1:19633",
                "dictionary_folder": ordered_dicts[0] if ordered_dicts else "",
                "mode": "Ladder",
            })
            self.config = mw.addonManager.getConfig(self.addon_name) or self.config
        except Exception:
            import traceback
            print(f"CompreDef: config pre-save failed:\n{traceback.format_exc()}")


def show_config_dialog() -> None:
    """Displays the configuration dialog."""
    dialog = ConfigDialog(parent=mw.app.activeWindow() if mw and mw.app else None)
    dialog.exec()


def show_knowledge_dialog() -> None:
    """Displays a read-only view of the learner-knowledge snapshot."""
    dialog = QDialog(parent=mw.app.activeWindow() if mw and mw.app else None)
    dialog.setWindowTitle("CompreDef Learner Knowledge")
    dialog.resize(520, 480)
    layout = QVBoxLayout()
    dialog.setLayout(layout)
    hint = QLabel(
        "Kanji and words from the FIRST FIELD of your MATURE notes\n"
        "(cards with interval \u2265 21 days), across all note types.\n"
        "Definitions, examples, and other fields are never counted.\n"
        "Restart Anki to rebuild the snapshot."
    )
    hint.setWordWrap(True)
    layout.addWidget(hint)
    view = QTextEdit()
    view.setReadOnly(True)
    try:
        view.setPlainText(knowledge_summary_text())
    except Exception:
        import traceback
        view.setPlainText(
            "CompreDef: could not build knowledge summary:\n"
            + traceback.format_exc()
        )
    layout.addWidget(view)
    if hasattr(QDialogButtonBox, "StandardButton"):
        close_flag = QDialogButtonBox.StandardButton.Close
    else:
        close_flag = QDialogButtonBox.Close
    button_box = QDialogButtonBox(close_flag)
    button_box.rejected.connect(dialog.reject)
    layout.addWidget(button_box)
    dialog.exec()
