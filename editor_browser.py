"""
editor_browser.py - Editor toolbar button and Browser bulk definition options for CompreDef.

Injects UI elements into Anki using `aqt.gui_hooks`:
- Card Editor Toolbar button to generate definition for current note.
- Browser Edit menu & context menu items to bulk generate definitions for selected notes.
Runs all processing in background threads via `mw.taskman.run_in_background()`
to adhere to non-blocking UI rules.
"""

import os
from typing import List, Dict, Any
from aqt import mw, gui_hooks
from aqt.editor import Editor
from aqt.browser import Browser
from aqt.qt import QMenu, QKeySequence
from aqt.utils import tooltip


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


def _get_addon_config() -> Dict[str, Any]:
    """
    Retrieves current add-on configuration dictionary using correct root addon name.
    """
    if not mw or not mw.addonManager:
        return {}
    addon_name = _get_addon_name()
    return mw.addonManager.getConfig(addon_name) or {}


def _generate_stub_definition(target_word: str, mode: str) -> str:
    """
    Placeholder definition generator until MeCab and Kanji Matrix modules are linked.

    Formats a descriptive stub based on the selected mode and target word.
    """
    return f"[{mode}] Definition for: {target_word}"


def on_editor_generate_definition(editor: Editor) -> None:
    """
    Action callback triggered when user clicks the CompreDef editor toolbar button.

    Extracts target word from the configured field and updates the definition field
    asynchronously using `mw.taskman.run_in_background()`.
    """
    if not editor.note:
        tooltip("No note selected in editor.", parent=editor.parentWindow)
        return

    config = _get_addon_config()
    target_note_type = config.get("note_type", "")
    word_field = config.get("word_field", "")
    def_field = config.get("definition_field", "")
    mode = config.get("mode", "Mode A")

    # Validate note type match
    note_model_name = editor.note.model()["name"]
    if target_note_type and note_model_name != target_note_type:
        tooltip(
            f"Note type '{note_model_name}' does not match configured target '{target_note_type}'.",
            parent=editor.parentWindow,
        )
        return

    # Check field presence in note
    if word_field not in editor.note:
        tooltip(f"Target word field '{word_field}' not found on current note.", parent=editor.parentWindow)
        return

    if def_field not in editor.note:
        tooltip(f"Definition field '{def_field}' not found on current note.", parent=editor.parentWindow)
        return

    word_text = editor.note[word_field].strip()
    if not word_text:
        tooltip(f"Field '{word_field}' is empty.", parent=editor.parentWindow)
        return

    # Execute generation task in background thread to prevent UI freezing
    def task() -> str:
        # Long-running dictionary / LLM query will execute here
        return _generate_stub_definition(word_text, mode)

    def on_done(future) -> None:
        try:
            definition_result = future.result()
            editor.note[def_field] = definition_result
            
            # Reload note in editor preserving focus so user immediately sees updated field
            if hasattr(editor, "load_note_keeping_focus"):
                editor.load_note_keeping_focus()
            else:
                editor.loadNote()

            tooltip(f"Generated definition for '{word_text}'!", parent=editor.parentWindow)
        except Exception as exc:
            tooltip(f"Error generating definition: {exc}", parent=editor.parentWindow)

    mw.taskman.run_in_background(task, on_done)


def add_editor_button(buttons: List[str], editor: Editor) -> None:
    """
    Hook callback to append CompreDef button to Anki's card editor toolbar.

    Target hook: `gui_hooks.editor_did_init_buttons`.
    """
    icon_path = os.path.join(os.path.dirname(__file__), "icons", "compredef.svg")

    # Add button via native Anki Editor helper
    btn = editor.addButton(
        icon=icon_path if os.path.exists(icon_path) else None,
        cmd="compredef_generate_definition",
        func=lambda ed=editor: on_editor_generate_definition(ed),
        tip="Generate CompreDef Definition",
        label="CD",
        id="compredef_editor_btn",
    )
    buttons.append(btn)


def on_bulk_generate_definitions(browser: Browser) -> None:
    """
    Action callback triggered from Browser Edit menu or Context menu.

    Processes all selected notes in a background thread, updating definition fields.
    """
    nids = browser.selectedNotes()
    if not nids:
        tooltip("No notes selected.", parent=browser)
        return

    config = _get_addon_config()
    target_note_type = config.get("note_type", "")
    word_field = config.get("word_field", "")
    def_field = config.get("definition_field", "")
    mode = config.get("mode", "Mode A")

    def task() -> int:
        """Background task running across all selected note IDs."""
        updated_count = 0
        for nid in nids:
            try:
                note = mw.col.get_note(nid)
                # Check note type match
                if target_note_type and note.model()["name"] != target_note_type:
                    continue

                if word_field not in note or def_field not in note:
                    continue

                word_text = note[word_field].strip()
                if not word_text:
                    continue

                # Generate definition for note
                definition_result = _generate_stub_definition(word_text, mode)
                note[def_field] = definition_result
                mw.col.update_note(note)
                updated_count += 1
            except Exception:
                # Avoid crashing the loop on an individual note failure
                continue

        return updated_count

    def on_done(future) -> None:
        try:
            count = future.result()
            # Refresh browser view to reflect updated note fields
            browser.search()
            tooltip(f"CompreDef: Successfully generated definitions for {count} note(s).", parent=browser)
        except Exception as exc:
            tooltip(f"Error during bulk definition generation: {exc}", parent=browser)

    mw.taskman.run_in_background(task, on_done)


def setup_browser_menu(browser: Browser) -> None:
    """
    Hook callback to append bulk edit option under Browser's Edit menu.

    Target hook: `gui_hooks.browser_menus_did_init`.
    """
    menu: QMenu = browser.form.menuEdit
    menu.addSeparator()

    action = menu.addAction("Generate CompreDef Definitions...")
    action.setShortcut(QKeySequence("Ctrl+Shift+D"))
    action.triggered.connect(lambda _, b=browser: on_bulk_generate_definitions(b))


def setup_browser_context_menu(browser: Browser, menu: QMenu) -> None:
    """
    Hook callback to append bulk edit option to Browser's right-click context menu.

    Target hook: `gui_hooks.browser_will_show_context_menu`.
    """
    action = menu.addAction("Generate CompreDef Definitions")
    action.triggered.connect(lambda _, b=browser: on_bulk_generate_definitions(b))


def setup_editor_browser_hooks() -> None:
    """Registers editor toolbar and browser menu hooks with Anki."""
    gui_hooks.editor_did_init_buttons.append(add_editor_button)
    gui_hooks.browser_menus_did_init.append(setup_browser_menu)
    gui_hooks.browser_will_show_context_menu.append(setup_browser_context_menu)
