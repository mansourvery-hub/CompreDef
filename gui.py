"""
gui.py - Configuration GUI for the CompreDef Anki add-on.

Provides a PyQt dialog allowing users to configure note types, field mappings,
generation modes (Mode A vs Mode B), and local dictionary directory paths.
"""

from typing import Optional, Dict, Any, List
from aqt import mw
from aqt.qt import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QLineEdit,
    QComboBox,
    QPushButton,
    QFileDialog,
    QDialogButtonBox,
    QWidget,
)


# Keyword lists for auto-matching target word and definition fields
_TARGET_WORD_KEYWORDS = [
    "word", "expression", "kanji", "reading", "furigana",
    "hiragana", "romaji", "katakana", "jp", "japanese", "ja"
]

_DEFINITION_KEYWORDS = [
    "definition", "meaning", "glossary", "translation",
    "translation_", "translation_", "explanation", "sense", "desc"
]


def _get_addon_name() -> str:
    """
    Safely retrieves the root Anki add-on name for config persistence.

    Returns the correct root addon name instead of the submodule name
    to ensure config.json is saved under the correct key.
    """
    if hasattr(mw, 'addonManager'):
        root_name = mw.addonManager.addonFromModule(__name__)
        if root_name:
            return root_name
    # Fallback to first part of module name
    return __name__.split('.')[0]


def _find_best_field_match(fields: List[str], keywords: List[str], fallback: str = None) -> Optional[str]:
        """
        Finds the best matching field name based on keyword similarity.

        Performs case-insensitive matching against provided keywords, with
        priority given to exact matches followed by substring matches.
        """
        if not fields or not keywords:
            return fallback

        # Normalize fields for comparison
        fields_lower = [f.lower().replace("_", " ").replace("-", " ") for f in fields]

        for keyword in keywords:
            # Check for exact match
            for f_lower in fields_lower:
                if f_lower == keyword.lower():
                    return f
            # Check for substring match (case-insensitive)
            for f_lower in fields_lower:
                if keyword.lower() in f_lower:
                    return f

        return fallback


class ConfigDialog(QDialog):
    """
    Dialog for configuring CompreDef add-on options.

    Reads current settings from Anki's addonManager config, presents a form
    to the user with cascading dropdowns for Note Types and Fields, and updates
    the configuration upon saving.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("CompreDef Configuration")
        self.resize(450, 250)

        # Retrieve existing user configuration or fall back to empty defaults
        addon_name = _get_addon_name()
        self.config: Dict[str, Any] = mw.addonManager.getConfig(addon_name) or {}

        # Initialize UI layout and controls
        self._init_ui()
        self._load_config()

    def _init_ui(self) -> None:
        """Sets up the form controls and layout structure for the configuration dialog."""
        main_layout = QVBoxLayout()
        self.setLayout(main_layout)

        form_layout = QFormLayout()

        # Cascading dropdown for Note Type selection
        self.note_type_combo = QComboBox()
        # Connect signal to update available field dropdowns whenever Note Type changes
        self.note_type_combo.currentTextChanged.connect(self._on_note_type_changed)
        form_layout.addRow("Target Note Type:", self.note_type_combo)

        # Dropdown for Target Word field selection
        self.word_field_combo = QComboBox()
        form_layout.addRow("Target Word Field:", self.word_field_combo)

        # Dropdown for Definition field selection
        self.definition_field_combo = QComboBox()
        form_layout.addRow("Definition Field:", self.definition_field_combo)

        # Dropdown selection for Generation Mode
        self.mode_combo = QComboBox()
        self.mode_combo.addItems([
            "Mode A: Dictionary Ladder + LLM",
            "Mode B: Local Kanji Score Matrix",
        ])
        form_layout.addRow("Generation Mode:", self.mode_combo)

        # File path selector for local dictionary folder
        dict_folder_layout = QHBoxLayout()
        self.dict_folder_input = QLineEdit()
        self.dict_folder_input.setPlaceholderText("/path/to/dictionaries")
        browse_btn = QPushButton("Browse...")
        # Connect browse button to standard QFileDialog directory picker
        browse_btn.clicked.connect(self._on_browse_dict_folder)
        dict_folder_layout.addWidget(self.dict_folder_input)
        dict_folder_layout.addWidget(browse_btn)

        form_layout.addRow("Dictionary Folder:", dict_folder_layout)

        main_layout.addLayout(form_layout)

        # Standard OK/Cancel dialog buttons (PyQt5/PyQt6 cross-version enum compatibility)
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
        """
        Retrieves the field names associated with a specified note type.

        Uses Anki's native `mw.col.models` API to safely fetch field definitions.
        """
        if not mw or not mw.col or not note_type_name:
            return []
        
        # Access the collection model dictionary safely via Anki native wrapper
        model = mw.col.models.by_name(note_type_name)
        if not model or "flds" not in model:
            return []
        
        return [field["name"] for field in model["flds"]]

    def _on_note_type_changed(self, note_type_name: str) -> None:
        """
        Slot triggered when the selected Note Type changes.

        Dynamically updates the items in both Word Field and Definition Field dropdowns,
        auto-selects fields based on keywords if applicable, and preserves valid previous selections.
        """
        fields = self._get_field_names(note_type_name)

        # Save current selections to preserve them if available in the new field list
        prev_word_field = self.word_field_combo.currentText()
        prev_def_field = self.definition_field_combo.currentText()

        self.word_field_combo.clear()
        self.word_field_combo.addItems(fields)

        self.definition_field_combo.clear()
        self.definition_field_combo.addItems(fields)

        # Try to auto-match fields based on keywords if no previous selection exists
        auto_word_field = None
        auto_def_field = None

        if not prev_word_field:
            auto_word_field = _find_best_field_match(fields, _TARGET_WORD_KEYWORDS, fallback=None)
        if not prev_def_field:
            auto_def_field = _find_best_field_match(fields, _DEFINITION_KEYWORDS, fallback=None)

        # Restore previous selection if valid, otherwise use auto-match, otherwise keep as is
        if prev_word_field in fields:
            self.word_field_combo.setCurrentText(prev_word_field)
        elif auto_word_field:
            self.word_field_combo.setCurrentText(auto_word_field)

        if prev_def_field in fields:
            self.definition_field_combo.setCurrentText(prev_def_field)
        elif auto_def_field:
            self.definition_field_combo.setCurrentText(auto_def_field)

    def _load_config(self) -> None:
        """Populates form controls with available Anki models/fields and current config values."""
        # Populate Note Type dropdown from active collection models
        if mw and mw.col:
            all_note_types = mw.col.models.all_names()
            self.note_type_combo.blockSignals(True)
            self.note_type_combo.clear()
            self.note_type_combo.addItems(all_note_types)
            self.note_type_combo.blockSignals(False)

        saved_note_type = self.config.get("note_type", "")
        if saved_note_type and self.note_type_combo.findText(saved_note_type) != -1:
            self.note_type_combo.setCurrentText(saved_note_type)

        # Manually trigger field update for current note type
        current_note_type = self.note_type_combo.currentText()
        self._on_note_type_changed(current_note_type)

        saved_word_field = self.config.get("word_field", "")
        if saved_word_field and self.word_field_combo.findText(saved_word_field) != -1:
            self.word_field_combo.setCurrentText(saved_word_field)

        saved_def_field = self.config.get("definition_field", "")
        if saved_def_field and self.definition_field_combo.findText(saved_def_field) != -1:
            self.definition_field_combo.setCurrentText(saved_def_field)

        # Map saved mode string to combobox index
        current_mode = self.config.get("mode", "Mode A")
        if "Mode B" in current_mode:
            self.mode_combo.setCurrentIndex(1)
        else:
            self.mode_combo.setCurrentIndex(0)

        self.dict_folder_input.setText(self.config.get("dictionary_folder", ""))

    def _on_browse_dict_folder(self) -> None:
        """Opens directory browser dialog to select dictionary directory."""
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Dictionary Directory",
            self.dict_folder_input.text()
        )
        if folder:
            self.dict_folder_input.setText(folder)

    def _save_and_accept(self) -> None:
        """Validates user inputs, persists updated settings to Anki config, and closes dialog."""
        # Determine mode string based on combobox index
        mode_str = "Mode B" if self.mode_combo.currentIndex() == 1 else "Mode A"

        updated_config = {
            "mode": mode_str,
            "note_type": self.note_type_combo.currentText().strip(),
            "word_field": self.word_field_combo.currentText().strip(),
            "definition_field": self.definition_field_combo.currentText().strip(),
            "dictionary_folder": self.dict_folder_input.text().strip(),
        }

        # Persist updated configuration via Anki's addonManager API using root addon name
        mw.addonManager.writeConfig(addon_name, updated_config)
        self.accept()


def show_config_dialog() -> None:
    """
    Instantiates and displays the configuration dialog.

    This function is passed as a callback to `mw.addonManager.setConfigAction`.
    """
    dialog = ConfigDialog(parent=mw.app.activeWindow() if mw and mw.app else None)
    dialog.exec()

