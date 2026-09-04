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
        role = _user_role()
        # Block signals to prevent itemChanged from triggering a loop during refresh
        self.dict_list.blockSignals(True)
        for i in range(self.dict_list.count()):
            item = self.dict_list.item(i)
            path = item.data(role)
            title = get_provider().get_title(path)
            # Index status: dictionaries must be installed (indexed) before
            # generation works; unindexed ones are skipped by lookups.
            try:
                provider = get_provider()
                if provider.is_installed(path):
                    count = provider.get_entry_count(path)
                    status = f"✓ ({count:,} entries)"
                else:
                    status = "⚠ not indexed — re-add to install"
            except Exception:
                status = "⚠ error"
            item.setText(f"[{i + 1}] {title} {status}")
            item.setCheckState(Qt.CheckState.Checked if path not in self.disabled_dicts else Qt.CheckState.Unchecked)
            item.setToolTip(
                path + ("\n(disabled — skipped during generation)" if path in self.disabled_dicts else "")
            )
        self.dict_list.blockSignals(False)

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

        # Restore disabled set (paths normalized the same way as _add_dict_path)
        self.disabled_dicts = {
            os.path.realpath(os.path.expanduser(str(p)))
            for p in self.config.get("disabled_dictionaries", [])
            if p
        }

        for d_path in saved_dicts:
            self._add_dict_path(d_path)
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
            # Backwards compatibility
            "dictionary_folder": ordered_dicts[0] if ordered_dicts else "",
            "mode": "Ladder",
        }

        mw.addonManager.writeConfig(self.addon_name, updated_config)
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
            mw.addonManager.writeConfig(self.addon_name, {
                **self._collect_type_config(),
                "dictionaries": ordered_dicts,
                "disabled_dictionaries": sorted(self.disabled_dicts),
                "tab_generate": self.tab_generate_check.isChecked(),
                "plain_text_definitions": self.plain_text_check.isChecked(),
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
