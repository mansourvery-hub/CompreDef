"""
gui.py - Configuration GUI for the CompreDef Anki add-on.

Provides a PyQt dialog allowing users to:
- Select the Target Note Type and map Target Word & Definition fields
  with intelligent automatic field matching.
- Configure and order the Dictionary Ladder (drag-and-drop or Move Up/Down buttons)
  from simplest (e.g. Children's) to advanced (e.g. monolingual comprehensive).
"""

import os
from typing import Optional, Dict, Any, List
from aqt import mw
from aqt.qt import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QComboBox,
    QPushButton,
    QFileDialog,
    QDialogButtonBox,
    QWidget,
    QListWidget,
    QListWidgetItem,
    QAbstractItemView,
    QLabel,
    QGroupBox,
    Qt,
)

from .parser import (
    get_dictionary_title,
    find_dictionary_folders,
    is_zip_dictionary,
)


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


# Keyword lists for auto-matching target word and definition fields
_TARGET_WORD_KEYWORDS = [
    "word", "expression", "kanji", "reading", "furigana",
    "hiragana", "romaji", "katakana", "jp", "japanese", "ja"
]

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
        self.resize(550, 520)

        self.addon_name = _get_addon_name()
        self.config: Dict[str, Any] = mw.addonManager.getConfig(self.addon_name) or {}

        self._init_ui()
        self._load_config()

    def _init_ui(self) -> None:
        """Sets up the form controls and layout structure."""
        main_layout = QVBoxLayout()
        self.setLayout(main_layout)

        # -------------------------------------------------------------
        # Note Type & Field Mappings Group
        # -------------------------------------------------------------
        mapping_group = QGroupBox("Note Type & Field Mappings")
        mapping_layout = QFormLayout()
        mapping_group.setLayout(mapping_layout)

        self.note_type_combo = QComboBox()
        self.note_type_combo.currentTextChanged.connect(self._on_note_type_changed)
        mapping_layout.addRow("Target Note Type:", self.note_type_combo)

        self.word_field_combo = QComboBox()
        mapping_layout.addRow("Target Word Field:", self.word_field_combo)

        self.definition_field_combo = QComboBox()
        mapping_layout.addRow("Definition Field:", self.definition_field_combo)

        main_layout.addWidget(mapping_group)

        # -------------------------------------------------------------
        # Dictionary Ladder Group
        # -------------------------------------------------------------
        ladder_group = QGroupBox("Dictionary Ladder (Order of Complexity)")
        ladder_layout = QVBoxLayout()
        ladder_group.setLayout(ladder_layout)

        desc_label = QLabel(
            "Dictionaries are searched in order from top to bottom.\n"
            "• Top = Simplest (Children's / Elementary dictionaries)\n"
            "• Bottom = Advanced (Standard / Monolingual comprehensive)\n"
            "If a fully comprehensible definition (100% known kanji) is found, "
            "search stops immediately (early exit), saving CPU time."
        )
        desc_label.setStyleSheet("color: gray; font-size: 11px;")
        desc_label.setWordWrap(True)
        ladder_layout.addWidget(desc_label)

        # List Widget with drag-and-drop reordering
        list_and_buttons_layout = QHBoxLayout()

        self.dict_list = QListWidget()
        self.dict_list.setDragDropMode(_internal_move_mode())
        self.dict_list.model().rowsMoved.connect(lambda *_: self._refresh_item_labels())
        list_and_buttons_layout.addWidget(self.dict_list)

        # Buttons on the side for reordering and management
        buttons_vbox = QVBoxLayout()

        self.add_zip_btn = QPushButton("Add Zip Archive...")
        self.add_zip_btn.setToolTip("Select a Yomitan dictionary .zip file")
        self.add_zip_btn.clicked.connect(self._on_add_zip)
        buttons_vbox.addWidget(self.add_zip_btn)

        self.add_dict_btn = QPushButton("Add Folder...")
        self.add_dict_btn.setToolTip("Select a single unzipped dictionary folder")
        self.add_dict_btn.clicked.connect(self._on_add_dictionary)
        buttons_vbox.addWidget(self.add_dict_btn)

        self.add_folder_btn = QPushButton("Scan Folder...")
        self.add_folder_btn.setToolTip("Scan a parent folder to automatically find and add all dictionary archives (.zip) and subfolders")
        self.add_folder_btn.clicked.connect(self._on_scan_folder)
        buttons_vbox.addWidget(self.add_folder_btn)

        buttons_vbox.addSpacing(10)

        self.move_up_btn = QPushButton("Move Up ↑")
        self.move_up_btn.setToolTip("Move selected dictionary earlier in ladder (simpler / checked earlier)")
        self.move_up_btn.clicked.connect(self._on_move_up)
        buttons_vbox.addWidget(self.move_up_btn)

        self.move_down_btn = QPushButton("Move Down ↓")
        self.move_down_btn.setToolTip("Move selected dictionary later in ladder (more advanced)")
        self.move_down_btn.clicked.connect(self._on_move_down)
        buttons_vbox.addWidget(self.move_down_btn)

        buttons_vbox.addSpacing(10)

        self.remove_btn = QPushButton("Remove")
        self.remove_btn.setToolTip("Remove selected dictionary from ladder")
        self.remove_btn.clicked.connect(self._on_remove_dictionary)
        buttons_vbox.addWidget(self.remove_btn)

        buttons_vbox.addStretch()
        list_and_buttons_layout.addLayout(buttons_vbox)

        ladder_layout.addLayout(list_and_buttons_layout)
        main_layout.addWidget(ladder_group)

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
        """Dynamically populates and auto-matches field dropdowns."""
        fields = self._get_field_names(note_type_name)

        prev_word_field = self.word_field_combo.currentText()
        prev_def_field = self.definition_field_combo.currentText()

        self.word_field_combo.clear()
        self.word_field_combo.addItems(fields)

        self.definition_field_combo.clear()
        self.definition_field_combo.addItems(fields)

        auto_word_field = None
        auto_def_field = None

        if not prev_word_field:
            auto_word_field = _find_best_field_match(fields, _TARGET_WORD_KEYWORDS, fallback=None)

        if not prev_def_field:
            remaining_fields = [f for f in fields if f != auto_word_field]
            auto_def_field = _find_best_field_match(remaining_fields, _DEFINITION_KEYWORDS, fallback=None)

        if prev_word_field in fields:
            self.word_field_combo.setCurrentText(prev_word_field)
        elif auto_word_field:
            self.word_field_combo.setCurrentText(auto_word_field)

        if prev_def_field in fields:
            self.definition_field_combo.setCurrentText(prev_def_field)
        elif auto_def_field:
            self.definition_field_combo.setCurrentText(auto_def_field)

    def _refresh_item_labels(self) -> None:
        """Updates numeric step prefixes (e.g. '[1] ...') on each item in the list."""
        role = _user_role()
        for i in range(self.dict_list.count()):
            item = self.dict_list.item(i)
            path = item.data(role)
            title = get_dictionary_title(path)
            item.setText(f"[{i + 1}] {title}")
            item.setToolTip(path)

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
        return True

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
        """Removes the selected dictionary from the ladder."""
        curr_row = self.dict_list.currentRow()
        if curr_row >= 0:
            self.dict_list.takeItem(curr_row)
            self._refresh_item_labels()

    def _load_config(self) -> None:
        """Populates form controls with current configuration values."""
        if mw and mw.col:
            all_note_types = mw.col.models.all_names()
            self.note_type_combo.blockSignals(True)
            self.note_type_combo.clear()
            self.note_type_combo.addItems(all_note_types)
            self.note_type_combo.blockSignals(False)

        saved_note_type = self.config.get("note_type", "")
        if saved_note_type and self.note_type_combo.findText(saved_note_type) != -1:
            self.note_type_combo.setCurrentText(saved_note_type)

        current_note_type = self.note_type_combo.currentText()
        self._on_note_type_changed(current_note_type)

        saved_word_field = self.config.get("word_field", "")
        if saved_word_field and self.word_field_combo.findText(saved_word_field) != -1:
            self.word_field_combo.setCurrentText(saved_word_field)

        saved_def_field = self.config.get("definition_field", "")
        if saved_def_field and self.definition_field_combo.findText(saved_def_field) != -1:
            self.definition_field_combo.setCurrentText(saved_def_field)

        # Load dictionary ladder
        saved_dicts = self.config.get("dictionaries", [])
        if not saved_dicts and self.config.get("dictionary_folder"):
            # Auto-detect from legacy single folder path
            saved_dicts = find_dictionary_folders(self.config["dictionary_folder"])

        for d_path in saved_dicts:
            self._add_dict_path(d_path)

    def _save_and_accept(self) -> None:
        """Saves settings to Anki config and closes dialog."""
        role = _user_role()
        ordered_dicts = [
            self.dict_list.item(i).data(role)
            for i in range(self.dict_list.count())
        ]

        updated_config = {
            "note_type": self.note_type_combo.currentText().strip(),
            "word_field": self.word_field_combo.currentText().strip(),
            "definition_field": self.definition_field_combo.currentText().strip(),
            "dictionaries": ordered_dicts,
            # Backwards compatibility
            "dictionary_folder": ordered_dicts[0] if ordered_dicts else "",
            "mode": "Ladder",
        }

        mw.addonManager.writeConfig(self.addon_name, updated_config)
        self.accept()


def show_config_dialog() -> None:
    """Displays the configuration dialog."""
    dialog = ConfigDialog(parent=mw.app.activeWindow() if mw and mw.app else None)
    dialog.exec()
