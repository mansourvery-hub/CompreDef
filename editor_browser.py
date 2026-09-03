"""
editor_browser.py - Editor toolbar button and Browser bulk generation for CompreDef.

Injects UI elements into Anki using `aqt.gui_hooks`:
- Card Editor toolbar button to generate a definition for the current note.
- Browser Edit menu & context menu items to bulk generate definitions for
  selected notes.

All generation is EXPLICIT (button / menu). Automatic generation on field
unfocus/Tab was deliberately removed: silently starting a background job
because the user left a field was the historical source of freezes, and
generated definitions were lost to the note-reload race. See ARCHITECTURE.md.

Anki 26.x compatibility (verified against installed 26.08.1 source):
- Two editor generations coexist: the Svelte `NewEditor` (Add window /
  Edit Current) which has NO `.note` attribute (only `.nid`), and the
  legacy `Editor` (Browser / legacy mode) which carries `.note`.
- `run_in_background` executes the task then calls on_done on the MAIN
  thread, so note edits in on_done are safe.
"""

import json
import os
import traceback
from typing import Any, Dict, List, Optional

from aqt import mw, gui_hooks
from aqt.browser import Browser
from aqt.qt import QMenu, QKeySequence
from aqt.utils import tooltip

from .generator import generate_definition
from .parser import parse_furigana_field, extract_clean_word
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


def _get_note_type_name(note) -> str:
    """Returns a note's notetype name via the non-deprecated API."""
    try:
        nt = note.note_type()  # anki 2.1.50+; 'note.model()' is deprecated
        if nt:
            return str(nt.get("name", ""))
    except Exception:
        pass
    return ""


def _resolve_editor_note(editor) -> Optional[Any]:
    """
    Returns the current Note object from either editor generation.

    NewEditor (Svelte) exposes only `.nid` (no `.note`), so we fetch the
    note from the collection. Legacy Editor carries `.note` directly.
    """
    note = getattr(editor, "note", None)
    if note is not None:
        return note
    nid = getattr(editor, "nid", None)
    if nid is not None and mw and mw.col:
        try:
            return mw.col.get_note(nid)
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Single-note generation (editor toolbar button)
# ---------------------------------------------------------------------------

_generation_in_flight = set()  # note ids currently being generated


def on_editor_generate_definition(editor) -> None:
    """
    Action callback triggered when user clicks the CompreDef editor toolbar button.

    Extracts the target word from the configured field and updates the
    definition field asynchronously. The heavy dictionary work is a set of
    SQLite SELECTs against pre-built indexes (see parser.py), so this is
    a lightweight background query — it never parses dictionary files.
    """
    note = _resolve_editor_note(editor)
    if note is None:
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

    # Validate note type match (both sides trimmed; empty config = all types)
    note_model_name = _get_note_type_name(note)
    if target_note_type and target_note_type.strip() and \
            note_model_name != target_note_type.strip():
        tooltip(
            f"Note type '{note_model_name}' does not match configured target '{target_note_type}'.",
            parent=editor.parentWindow,
        )
        return

    # Check field presence in note
    if word_field not in note:
        tooltip(f"Target word field '{word_field}' not found on current note.", parent=editor.parentWindow)
        return

    if def_field not in note:
        tooltip(f"Definition field '{def_field}' not found on current note.", parent=editor.parentWindow)
        return

    # Anki note fields frequently carry HTML wrappers (<div>, <span>) and
    # furigana markup (先[ま]ず / <ruby>先<rt>ま</rt></ruby>ず). The raw
    # string almost never equals the dictionary term, which made lookups
    # return nothing — clean it before the SQLite lookup.
    word_text = extract_clean_word(note[word_field])
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

    # Single-flight guard: never stack duplicate generations for the same
    # note (double-fired hooks previously burned 2x CPU/RAM).
    nid_key = getattr(note, "id", None) or id(note)
    if nid_key in _generation_in_flight:
        return
    _generation_in_flight.add(nid_key)

    tooltip("CompreDef: Generating definition...", parent=editor.parentWindow)

    # Resolve the word's reading (dedicated field or embedded furigana)
    # so homographs like 先ず(まず) vs 先ず(せんず) pick the right entry.
    reading_text = _extract_reading_text(note, word_field, reading_field)

    def task() -> Optional[str]:
        return generate_definition(
            word_text,
            dictionary_folder=dictionary_folder,
            dictionaries=dictionaries,
            reading=reading_text,
            disabled_dictionaries=disabled_dictionaries,
        )

    def on_done(future) -> None:
        # Always release the in-flight lock, even on failure
        _generation_in_flight.discard(nid_key)
        try:
            definition_result = future.result()
            if not definition_result:
                tooltip(
                    f"CompreDef: No definition found for '{word_text}'.",
                    parent=editor.parentWindow,
                )
                return

            _apply_definition_to_editor(editor, note, def_field, definition_result)
            tooltip(f"Generated definition for '{word_text}'!", parent=editor.parentWindow)
        except Exception:
            # Loud, diagnosable failure — never silently swallow (the bulk
            # path's old bare `except: continue` hid real bugs for months).
            print(f"CompreDef: generation failed for word '{word_text}' "
                  f"(note {nid_key}):\n{traceback.format_exc()}")
            tooltip(
                f"CompreDef: generation failed for '{word_text}' — "
                f"see Anki's debug console (Ctrl+Shift+;) for details.",
                parent=editor.parentWindow,
            )

    mw.taskman.run_in_background(task, on_done)


def _apply_definition_to_editor(editor, note, def_field: str, definition_html: str) -> None:
    """
    Persists the generated definition into the note and refreshes the editor.

    Ordering contract (fixes the 'definition disappears' bug):
    1. Write the field on the Note object.
    2. Persist to the collection FIRST (update_note) for existing notes.
    3. THEN refresh the editor UI. A reload can never discard the change
       because it is already durably stored.
    """
    note[def_field] = definition_html

    # Persist existing notes immediately; new (unsaved) notes in the Add
    # window are written by Anki itself when the user confirms the add —
    # updating them here would fail since they have no collection row yet.
    if getattr(note, "id", 0) and mw and mw.col:
        try:
            mw.col.update_note(note)
        except Exception:
            print(f"CompreDef: update_note failed for note {note.id}:\n{traceback.format_exc()}")

    # Refresh the visible editor. run_in_background's on_done runs on the
    # main thread, so touching the webview here is thread-safe.
    try:
        names = list(note.keys())
        values = [note[name] for name in names]
        editor.web.eval(
            f"setFields({json.dumps(names)}, {json.dumps(values)});"
        )
    except Exception:
        # Legacy editor fallback (refresh keeping user focus)
        try:
            if hasattr(editor, "loadNoteKeepingFocus"):
                editor.loadNoteKeepingFocus()
            elif hasattr(editor, "loadNote"):
                editor.loadNote()
        except Exception:
            print(f"CompreDef: editor refresh failed:\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Editor toolbar button
# ---------------------------------------------------------------------------

def add_editor_button(buttons: List[str], editor) -> None:
    """
    Hook callback to append the CompreDef button to the card editor toolbar.

    `editor_did_init_buttons` fires for BOTH editor generations and
    `addButton` exists on both, so one registration covers everything.
    """
    icon_path = os.path.join(os.path.dirname(__file__), "icons", "compredef.svg")

    btn = editor.addButton(
        icon=icon_path if os.path.exists(icon_path) else None,
        cmd="compredef_generate_definition",
        func=lambda ed: on_editor_generate_definition(ed),
        tip="Generate CompreDef Definition",
        label="CD",
        id="compredef_editor_btn",
    )
    buttons.append(btn)


# ---------------------------------------------------------------------------
# Browser bulk generation (explicit menu action)
# ---------------------------------------------------------------------------

def on_bulk_generate_definitions(browser: Browser) -> None:
    """
    Action callback triggered from Browser Edit menu or Context menu.

    Processes all selected notes in a background thread, updating definition
    fields. Failures are logged with note id, word and traceback, reported
    to the user in the summary, and never abort the remaining notes.
    """
    # selected_notes() is the modern name; selectedNotes() the legacy one
    if hasattr(browser, "selected_notes"):
        nids = list(browser.selected_notes())
    else:
        nids = list(browser.selectedNotes())
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

    tooltip(
        f"CompreDef: Generating definitions for {len(nids)} note(s)...",
        parent=browser,
    )

    def task() -> tuple:
        """
        Background task across all selected note IDs.

        Returns (updated_count, skipped_count, failures) where failures is a
        list of (note_id, word, error_message) for the user-facing summary.
        """
        updated_count = 0
        skipped_count = 0
        failures: List[tuple] = []

        for nid in nids:
            word_text = ""
            try:
                note = mw.col.get_note(nid)

                # Check note type match
                if target_note_type and target_note_type.strip():
                    if _get_note_type_name(note) != target_note_type.strip():
                        continue

                if word_field not in note or def_field not in note:
                    continue

                # Strip HTML wrappers and furigana markup so the dictionary
                # term matches (same fix as the editor button path).
                word_text = extract_clean_word(note[word_field])
                if not word_text:
                    continue

                # Per-note reading resolution (dedicated field or embedded
                # furigana) so homographs resolve correctly in bulk too.
                reading_text = _extract_reading_text(
                    note, word_field, reading_field
                )

                # Generate definition using the Dictionary Ladder (pure
                # SQLite lookups — no indexing ever happens here).
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

                # Persist BEFORE any UI refresh, so the definition survives.
                note[def_field] = definition_result
                mw.col.update_note(note)
                updated_count += 1
            except Exception:
                # Log loudly and keep going: one bad note must not kill the
                # batch, but the failure must be visible and diagnosable.
                err = traceback.format_exc()
                print(f"CompreDef: bulk generation FAILED for note {nid} "
                      f"(word '{word_text}'):\n{err}")
                failures.append((nid, word_text, err.splitlines()[-1] if err else "unknown error"))
                continue

        # Note fields were modified, so the memoized known-kanji/vocab
        # sets are stale; force a rescan on next use.
        reset_caches()

        return updated_count, skipped_count, failures

    def on_done(future) -> None:
        try:
            updated_count, skipped_count, failures = future.result()
            # Refresh browser view to reflect updated note fields
            if hasattr(browser, "search"):
                browser.search()

            parts = [f"generated: {updated_count}"]
            if skipped_count:
                parts.append(f"no definition found: {skipped_count}")
            if failures:
                parts.append(f"FAILED: {len(failures)} (see console for details)")
                for nid, word, err in failures[:5]:  # console gets full tracebacks
                    print(f"CompreDef: note {nid} word '{word}': {err}")
            tooltip(
                f"CompreDef: {', '.join(parts)}",
                parent=browser,
            )
        except Exception:
            print(f"CompreDef: bulk generation crashed:\n{traceback.format_exc()}")
            tooltip(f"CompreDef: bulk generation crashed — see console.", parent=browser)

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
    """
    Registers editor toolbar and browser menu hooks with Anki.

    Deliberately does NOT register any automatic generation on field
    unfocus or Tab (editor_did_unfocus_field / JS key listeners): explicit
    button/menu actions only, per the stability-first architecture.
    """
    gui_hooks.editor_did_init_buttons.append(add_editor_button)
    gui_hooks.browser_menus_did_init.append(setup_browser_menu)
    gui_hooks.browser_will_show_context_menu.append(setup_browser_context_menu)
