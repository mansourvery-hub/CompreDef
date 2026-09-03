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
from aqt.qt import QMenu, QKeySequence, QObject, QEvent, Qt, QKeyEvent
from aqt.utils import tooltip
from .generator import generate_definition
from .parser import parse_furigana_field
from .db_utils import reset_caches


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


def _extract_reading_text(note, word_field: str, reading_field: str) -> str:
    """
    Extracts the word's kana reading from the note for homograph resolution.

    Priority: a dedicated reading/furigana field if configured, else the
    word field itself (Expression fields often embed furigana markup like
    '先[ま]ず'). Returns '' when neither carries usable reading info —
    the generator then falls back to reading-agnostic lookup.
    """
    # Dedicated reading field first (explicit user configuration wins)
    if reading_field and reading_field in note:
        parsed = parse_furigana_field(note[reading_field])
        if parsed:
            return parsed

    # Fall back to the word field (may embed 先[ま]ず / ruby markup)
    if word_field and word_field in note:
        parsed = parse_furigana_field(note[word_field])
        if parsed:
            return parsed

    return ""


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
    reading_field = config.get("reading_field", "")
    def_field = config.get("definition_field", "")
    dictionaries = config.get("dictionaries", [])
    disabled_dictionaries = config.get("disabled_dictionaries", [])
    dictionary_folder = config.get("dictionary_folder", "")

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

    # Early validation: without any dictionary configured nothing can be generated
    if not dictionaries and not dictionary_folder:
        tooltip(
            "CompreDef: No dictionaries configured.\nSet them under Tools -> Add-ons -> CompreDef -> Config.",
            parent=editor.parentWindow,
        )
        return

    # The first generation after startup loads/caches dictionaries; show
    # the user what is happening so Anki never looks frozen.
    tooltip("CompreDef: Generating definition...", parent=editor.parentWindow)

    # Resolve the word's reading (dedicated field or embedded furigana)
    # so homographs like 先ず(まず) vs 先ず(せんず) pick the right entry.
    reading_text = _extract_reading_text(editor.note, word_field, reading_field)

    # Execute generation task in background thread to prevent UI freezing
    def task() -> str:
        return generate_definition(
            word_text,
            dictionary_folder=dictionary_folder,
            dictionaries=dictionaries,
            reading=reading_text,
            disabled_dictionaries=disabled_dictionaries,
        )

    def on_done(future) -> None:
        try:
            definition_result = future.result()
            if not definition_result:
                tooltip(
                    f"CompreDef: No definition found for '{word_text}'.",
                    parent=editor.parentWindow,
                )
                return

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



class EditorTabFilter(QObject):
    """
    DEPRECATED: Replaced by JS-based bridge for better reliability in WebEngine.
    """
    def __init__(self, editor: Editor) -> None:
        super().__init__()
        self.editor = editor

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        return super().eventFilter(obj, event)


def add_editor_button(buttons: List[str], editor: Editor) -> None:
    """
    Hook callback to append CompreDef button to Anki's card editor toolbar.
    Also injects JS to handle Tab-to-Generate.
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

    # Inject JS listener for Tab-to-Generate
    if hasattr(editor, "web"):
        # We inject the script to listen for Tab.
        # We use a more robust approach to identify the field and send it.
        js_code = """
        (function() {
            if (window.compredef_tab_listener) return;
            window.compredef_tab_listener = true;

            document.addEventListener('keydown', function(e) {
                if (e.key === 'Tab') {
                    const activeEl = document.activeElement;
                    let fieldName = 'unknown';
                    if (activeEl) {
                        fieldName = activeEl.getAttribute('data-field-name') || 
                                   (activeEl.id ? activeEl.id.replace('field-', '') : 'unknown');
                    }
                    
                    if (typeof pycmd === 'function') {
                        pycmd('compredef_tab_pressed:' + fieldName);
                    }
                }
            }, true);
        })();
        """
        editor.web.eval(js_code)



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
    reading_field = config.get("reading_field", "")
    def_field = config.get("definition_field", "")
    dictionaries = config.get("dictionaries", [])
    disabled_dictionaries = config.get("disabled_dictionaries", [])
    dictionary_folder = config.get("dictionary_folder", "")

    # Early validation: without any dictionary configured nothing can be generated
    if not dictionaries and not dictionary_folder:
        tooltip(
            "CompreDef: No dictionaries configured.\nSet them under Tools -> Add-ons -> CompreDef -> Config.",
            parent=browser,
        )
        return

    # First generation after startup loads/caches dictionaries; show the
    # user what is happening so Anki never looks frozen.
    tooltip(
        f"CompreDef: Generating definitions for {len(nids)} note(s)...",
        parent=browser,
    )

    def task() -> tuple[int, int]:
        """Background task running across all selected note IDs. Returns (updated_count, skipped_count)."""
        updated_count = 0
        skipped_count = 0
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

                # Per-note reading resolution (dedicated field or embedded
                # furigana) so homographs resolve correctly in bulk too.
                reading_text = _extract_reading_text(
                    note, word_field, reading_field
                )

                # Generate definition for note using the Dictionary Ladder
                definition_result = generate_definition(
                    word_text,
                    dictionary_folder=dictionary_folder,
                    dictionaries=dictionaries,
                    reading=reading_text,
                    disabled_dictionaries=disabled_dictionaries,
                )
                if not definition_result:
                    skipped_count += 1
                    continue

                note[def_field] = definition_result
                mw.col.update_note(note)
                updated_count += 1
            except Exception:
                # Avoid crashing the loop on an individual note failure
                continue

        # Note fields were modified, so the memoized known-kanji/vocab
        # sets are stale; force a rescan on next use.
        reset_caches()

        return updated_count, skipped_count

    def on_done(future) -> None:
        try:
            updated_count, skipped_count = future.result()
            # Refresh browser view to reflect updated note fields
            browser.search()
            if skipped_count > 0:
                tooltip(
                    f"CompreDef: Generated definitions for {updated_count} note(s) ({skipped_count} skipped - no definition found).",
                    parent=browser,
                )
            else:
                tooltip(
                    f"CompreDef: Successfully generated definitions for {updated_count} note(s).",
                    parent=browser,
                )
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


def on_field_unfocus(changed: bool, note: Any, current_field_index: int) -> bool:
    """
    Hook callback triggered when a field in the editor loses focus.
    If the Word field was unfocused and the Definition field is empty,
    it triggers automatic definition generation.
    """
    if not note:
        return changed

    config = _get_addon_config()
    word_field = config.get("word_field", "")
    def_field = config.get("definition_field", "")

    # Resolve the name of the field that was just unfocused
    try:
        fields = mw.col.models.field_names(note.model())
        if current_field_index < 0 or current_field_index >= len(fields):
            return changed
        unfocused_field = fields[current_field_index]
    except Exception:
        return changed

    if unfocused_field == word_field:
        # Only generate if definition is empty
        if def_field in note and not note[def_field].strip():
            # Try to find the active editor instance
            editor = None
            # 1. Try to find an active Editor window
            for window in mw.app.topLevelWidgets():
                if hasattr(window, "editor") and window.editor:
                    editor = window.editor
                    break
            
            if not editor:
                return changed
            
            on_editor_generate_definition(editor)
    
    return changed

def setup_editor_browser_hooks() -> None:
    """Registers editor toolbar and browser menu hooks with Anki."""
    gui_hooks.editor_did_init_buttons.append(add_editor_button)
    gui_hooks.browser_menus_did_init.append(setup_browser_menu)
    gui_hooks.browser_will_show_context_menu.append(setup_browser_context_menu)
    
    # Use the native unfocus hook for Tab-to-Generate behavior
    gui_hooks.editor_did_unfocus_field.append(on_field_unfocus)

def _patch_webview_bridge(editor: Editor) -> None:
    """
    Intercepts bridge commands to handle 'compredef_tab_pressed'
    without needing a separate setBridgeCmd method.
    """
    original_on_bridge_cmd = editor.web.onBridgeCmd
    
    def wrapped_on_bridge_cmd(cmd: str) -> Any:
        if cmd.startswith("compredef_tab_pressed"):
            # Extract field name from "compredef_tab_pressed:fieldName"
            field_from_js = "unknown"
            if ":" in cmd:
                field_from_js = cmd.split(":", 1)[1]

            config = _get_addon_config()
            word_field = config.get("word_field", "")
            def_field = config.get("definition_field", "")
            
            # Check 1: Use Anki's reported current field
            current_field = getattr(editor.web, "currentField", None)
            
            # Check 2: Use the field reported by the JS DOM check
            # (Fallback in case currentField is not updated yet)
            is_word_field = (current_field == word_field) or (field_from_js == word_field)
            
            if is_word_field:
                note = getattr(editor, "note", None)
                if note and def_field in note and not note[def_field].strip():
                    on_editor_generate_definition(editor)
                    return "generated"
            return "ignored"
        return original_on_bridge_cmd(cmd)
    
    editor.web.onBridgeCmd = wrapped_on_bridge_cmd

def _handle_tab_generate_bridge(editor: Editor, data: Dict[str, Any]) -> str:
    """
    JS-Bridge handler for Tab-to-Generate.
    Checks if current field is the Word field and Definition is empty.
    """
    config = _get_addon_config()
    word_field = config.get("word_field", "")
    def_field = config.get("definition_field", "")
    
    current_field = data.get("field", "")
    
    # We check if the field reported by JS matches our configured word field
    # and if the definition field is empty.
    if current_field == word_field:
        note = getattr(editor, "note", None)
        if note and def_field in note and not note[def_field].strip():
            on_editor_generate_definition(editor)
            return "generated"
    return "ignored"
