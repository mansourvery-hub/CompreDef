"""
gui.py - Configuration GUI for the CompreDef Anki add-on.

Provides a PyQt dialog allowing users to configure note types, field mappings,
generation modes (Mode A vs Mode B), and local dictionary directory paths.
"""

from typing import Optional, Dict, Any
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


class ConfigDialog(QDialog):
    """
    Dialog for configuring CompreDef add-on options.

    Reads current settings from Anki's addonManager config, presents a form
    to the user, and updates the configuration upon saving.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("CompreDef Configuration")
        self.resize(450, 250)

        # Retrieve existing user configuration or fall back to empty defaults
        self.config: Dict[str, Any] = mw.addonManager.getConfig(__name__) or {}

        # Initialize UI layout and controls
        self._init_ui()
        self._load_config()

    def _init_ui(self) -> None:
        """Sets up the form controls and layout structure for the configuration dialog."""
        main_layout = QVBoxLayout()
        self.setLayout(main_layout)

        form_layout = QFormLayout()

        # Input for target Note Type (e.g., "Japanese", "Cloze")
        self.note_type_input = QLineEdit()
        self.note_type_input.setPlaceholderText("e.g. Japanese")
        form_layout.addRow("Target Note Type:", self.note_type_input)

        # Input for Target Word field (e.g., "Expression", "Word")
        self.word_field_input = QLineEdit()
        self.word_field_input.setPlaceholderText("e.g. Expression")
        form_layout.addRow("Target Word Field:", self.word_field_input)

        # Input for Definition field (e.g., "Definition", "Glossary")
        self.definition_field_input = QLineEdit()
        self.definition_field_input.setPlaceholderText("e.g. Definition")
        form_layout.addRow("Definition Field:", self.definition_field_input)

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

    def _load_config(self) -> None:
        """Populates form inputs with current config values."""
        self.note_type_input.setText(self.config.get("note_type", "Japanese"))
        self.word_field_input.setText(self.config.get("word_field", "Expression"))
        self.definition_field_input.setText(self.config.get("definition_field", "Definition"))

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
            "note_type": self.note_type_input.text().strip(),
            "word_field": self.word_field_input.text().strip(),
            "definition_field": self.definition_field_input.text().strip(),
            "dictionary_folder": self.dict_folder_input.text().strip(),
        }

        # Persist updated configuration via Anki's addonManager API
        mw.addonManager.writeConfig(__name__, updated_config)
        self.accept()


def show_config_dialog() -> None:
    """
    Instantiates and displays the configuration dialog.

    This function is passed as a callback to `mw.addonManager.setConfigAction`.
    """
    dialog = ConfigDialog(parent=mw.app.activeWindow() if mw and mw.app else None)
    dialog.exec()
