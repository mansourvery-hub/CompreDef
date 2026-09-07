"""
editor_browser.py - Editor toolbar button, Tab-to-Generate and Browser bulk
generation for CompreDef.

Injects UI elements into Anki using `aqt.gui_hooks`:
- Card Editor toolbar button to generate a definition for the current note.
- Tab-to-Generate: leaving the configured word field (Tab / clicking away)
  auto-fills the definition field when it is empty.
- Browser Edit menu & context menu items to bulk generate definitions for
  selected notes.

Tab-to-Generate stability contract (this feature was removed in a1a92a3
after it froze Anki and lost definitions; it is back ONLY because every
historical failure mode is now structurally fixed):
1. Generation itself is pure SQLite lookups (install-time indexing, see
   parser.py) — the old first-use freeze came from parsing dictionary
   files inside the unfocus path, which can never happen anymore.
2. The hook returns `changed` UNTOUCHED so it never triggers the legacy
   editor's "reload after filter" race (the old lost-definition bug).
   Persistence is done by the generation path itself, which updates the
   note BEFORE refreshing the editor (see _apply_definition_to_editor).
3. Single-flight guard: rapid Tab-Tab-Tab never stacks duplicate jobs.
4. Opt-out via config ("tab_generate": false) for users who prefer
   explicit-only workflows.

Anki 26.x compatibility (verified against installed 26.08.1 source):
- Two editor generations coexist: the Svelte `NewEditor` (Add window /
  Edit Current) which has NO `.note` attribute (only `.nid`) and fires no
  unfocus hook, and the legacy `Editor` (Browser / legacy mode) which
  carries `.note` and fires `editor_did_unfocus_field` on blur. Tab-to-
  Generate uses the unfocus hook, so it is active wherever that hook
  fires (the legacy editor; on this user's Anki the Svelte experiment is
  disabled so ALL editors are legacy). No JS injection, no bridge
  monkeypatching — those were the fragile parts of the old approach.
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

from .core import get_generator
from .utils import parse_furigana_field, extract_clean_word, resolve_ladder_paths

# Dual-context sibling import (see core.py for why both forms are needed).
if __package__:
    from .scope import note_in_scope as _scope_note_in_scope
    from .scope import note_deck_names as _scope_note_deck_names
    from .scope import SCOPE_CONFIG_KEY as _SCOPE_KEY
else:
    from scope import note_in_scope as _scope_note_in_scope
    from scope import note_deck_names as _scope_note_deck_names
    from scope import SCOPE_CONFIG_KEY as _SCOPE_KEY


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


def _note_in_scope(note, config: Dict[str, Any], editor: Any = None) -> bool:
    """
    Scope gate shared by every generation path (editor button, bulk,
    Tab-to-Generate): True only when the note has a card in one of the
    user's Scope decks (subdecks included). An empty scope is
    fail-closed (False). Unsaved Add-window notes are checked against
    the deck the window will add to (editor's DeckChooser / curDeck).
    Never raises — a scope-check failure must not break editing; it
    only skips generation for this note.
    """
    try:
        return bool(_scope_note_in_scope(note, config, editor=editor))
    except Exception:
        print(f"CompreDef: scope check failed:\n{traceback.format_exc()}")
        return False


def _infer_field_mapping(note) -> Optional[Dict[str, str]]:
    """
    Auto-infers word/reading/definition field names from the note's
    actual fields. Used as a fallback when a note is inside the Scope
    but has no explicit entry in `targets` — picking a deck should be
    enough to enable generation (the user said 'no need to add each
    note type'). Reuses the same keyword heuristics as the GUI.
    """
    try:
        field_names = list(note.keys())
    except Exception:
        return None
    if not field_names:
        return None
    # Inline lightweight version of gui's keyword match to avoid import
    # cycles; keeps editor_browser independent from gui.
    _WORD_KW = ["word", "expression", "kanji", "front"]
    _READ_KW = ["furigana", "reading", "kana", "hiragana", "katakana",
                "yomi", "読み"]
    _READ_KW2 = ["expression", "word", "front"]
    _DEF_KW = ["definition", "meaning", "glossary", "translation",
               "explanation", "sense", "desc"]

    def _best(fields: List[str], kws: List[str]) -> Optional[str]:
        lows = [f.lower().replace("_", " ").replace("-", " ") for f in fields]
        for kw in kws:
            for i, fl in enumerate(lows):
                if fl == kw.lower():
                    return fields[i]
            for i, fl in enumerate(lows):
                if kw.lower() in fl:
                    return fields[i]
        return None

    word = _best(field_names, _WORD_KW)
    # Prefer dedicated reading field, else word field itself
    reading = _best(field_names, _READ_KW) or (word or "")
    remaining = [f for f in field_names if f != word]
    definition = _best(remaining, _DEF_KW) or _best(field_names, _DEF_KW)
    if not word or not definition or word == definition:
        return None
    return {"word_field": word, "reading_field": reading or "",
            "definition_field": definition}


def _resolve_mapping_for_inscope_note(note, config: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Mapping resolution for a note that ALREADY passed the Scope gate.

    Returns the field mapping or None when no usable mapping exists
    (explicit + inferred both failed). Kept separate so callers can
    distinguish "outside Scope" (fix by adding the deck) from "no field
    mapping" (fix by configuring fields) — the v1.2 dialog must never
    offer "Add deck" for a mapping problem, which looped forever.
    """
    targets = config.get("targets")
    if isinstance(targets, dict) and targets:
        mapping = targets.get(_get_note_type_name(note))
        if isinstance(mapping, dict):
            resolved = {
                "word_field": str(mapping.get("word_field", "") or ""),
                "reading_field": str(mapping.get("reading_field", "") or ""),
                "definition_field": str(mapping.get("definition_field", "") or ""),
            }
            if resolved["word_field"] and resolved["definition_field"]:
                return resolved
            # Incomplete mapping → try inference.
            return _infer_field_mapping(note)
        # No entry for this type → infer.
        return _infer_field_mapping(note)

    # Legacy single-type config: applies only to that one type.
    legacy_type = str(config.get("note_type", "") or "").strip()
    if legacy_type and _get_note_type_name(note) != legacy_type:
        # Legacy gate failed but Scope already passed — allow inference
        # for any in-scope type when a Scope is configured.
        if isinstance(config.get("scope_decks"), list) and config.get("scope_decks"):
            return _infer_field_mapping(note)
        return None
    resolved = {
        "word_field": str(config.get("word_field", "") or ""),
        "reading_field": str(config.get("reading_field", "") or ""),
        "definition_field": str(config.get("definition_field", "") or ""),
    }
    if resolved["word_field"] and resolved["definition_field"]:
        return resolved
    return _infer_field_mapping(note)


def resolve_fields_for_note(note, config: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    Returns {'word_field', 'reading_field', 'definition_field'} for this
    note, or None when the note must not generate.

    Field mapping still comes from the note's own type (multi-type
    'targets' when configured, legacy single-type otherwise), but
    membership is decided by the Scope: the note must have a card in
    one of the selected Scope decks (ANY card suffices — a note with
    cards in several decks is in scope when at least one is covered;
    the user never needs to add every deck it touches).

    If the note is inside the Scope but has no explicit entry in
    `targets`, an auto-inferred mapping is returned so that selecting a
    deck alone is enough — the "no need to add each note type" promise.
    """
    # Scope gate first: deck membership is the primary filter. The editor
    # instance enables Add-window (unsaved note) deck resolution.
    if not _note_in_scope(note, config, editor=getattr(note, "_cd_editor", None)):
        return None
    return _resolve_mapping_for_inscope_note(note, config)


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


def _add_deck_to_scope_and_reset(deck_names: List[str]) -> bool:
    """Appends decks to the Scope config and rebuilds knowledge.

    Shared by the quick-fix dialog and the Add-window path. Returns
    True on success. Also drops the generator singleton so the next
    generation re-scores against the NEW knowledge (the v1.1.4 "add
    deck does not fix it" contributor: the generator kept the stale
    snapshot). Never raises.
    """
    try:
        addon = _get_addon_name()
        cfg = mw.addonManager.getConfig(addon) or {}
        old_scope = list(cfg.get(_SCOPE_KEY) or [])
        new_scope = list(old_scope)
        for d in deck_names:
            if d and d not in new_scope:
                new_scope.append(d)
        if new_scope == old_scope:
            # Nothing changed — still reset the generator, since the
            # caller expects a behavior change (idempotent safety).
            pass
        cfg[_SCOPE_KEY] = new_scope
        mw.addonManager.writeConfig(addon, cfg)
        print(f"CompreDef: quick-fix added deck(s) {deck_names} to Scope "
              f"(now: {new_scope})")
        # Knowledge must rebuild for the new deck to count, and the
        # generator must drop its stale knowledge snapshot.
        try:
            from .core import reset_generator as _reset_gen
        except Exception:
            try:
                from core import reset_generator as _reset_gen  # type: ignore
            except Exception:
                _reset_gen = None
        if _reset_gen is not None:
            try:
                _reset_gen()
            except Exception:
                pass
        else:
            # Last resort: reset knowledge caches directly.
            try:
                from .anki import reset_caches as _reset
            except Exception:
                try:
                    from anki import reset_caches as _reset  # type: ignore
                except Exception:
                    _reset = None
                if _reset:
                    _reset()
            except Exception:
                pass
        return True
    except Exception:
        print(f"CompreDef: add-to-scope failed:\n{traceback.format_exc()}")
        return False


def _target_deck_for_note(note, editor) -> str:
    """Resolves the deck a NEW (unsaved) note will land in.

    Saved notes: their own card decks. Add-window notes (id 0, no
    cards): the window's selected deck — via its DeckChooser when
    reachable, else the collection's 'curDeck' (the same default
    Anki's own Add window uses). Returns '' when unknowable.
    """
    # Saved note: its cards' decks.
    try:
        decks = _scope_note_deck_names(note)
        if decks:
            return decks[0]
    except Exception:
        pass
    # Unsaved note: the Add window's deck chooser.
    try:
        chooser = getattr(editor, "deck_chooser", None) or \
            getattr(getattr(editor, "parentWindow", None), "deck_chooser", None)
        did = getattr(chooser, "selected_deck_id", None)
        if did and mw and mw.col:
            name = mw.col.decks.name(int(did))
            if name and name != "(none)":
                return name
    except Exception:
        pass
    # Fallback: collection's current deck (Anki's own Add default).
    try:
        if mw and mw.col:
            did = mw.col.get_config("curDeck", default=None)
            if did:
                name = mw.col.decks.name(int(did))
                if name and name != "(none)":
                    return name
    except Exception:
        pass
    return ""


def _offer_add_to_scope(note, editor) -> None:
    """
    Out-of-scope quick fix: explains WHY the note is blocked (which of
    its decks are covered vs not — ANY coverage suffices) and offers a
    one-click "add this deck & retry".

    v1.2 hardening: when the Scope ALREADY covers one of the note's
    decks but generation still failed, the cause is a field-mapping
    problem, not scope — offering "Add deck" there looped forever (the
    user's report). That case now gets a mapping-specific message and
    NEVER the add-deck button.
    """
    # Re-check precisely: is scope the actual blocker, or mapping?
    cfg = _get_addon_config() if mw else {}
    in_scope = _note_in_scope(note, cfg, editor=getattr(note, "_cd_editor", None))
    note_decks = _scope_note_deck_names(note)
    if not note_decks:
        target = _target_deck_for_note(note, editor)
        if target:
            note_decks = [target]
    scope = list(cfg.get(_SCOPE_KEY) or [])
    scope_txt = ", ".join(scope) if scope else "(none)"

    if in_scope:
        # Scope passes but the caller landed here → mapping failure.
        type_name = _get_note_type_name(note)
        fields = list(note.keys()) if note is not None else []
        msg = (
            f"This note's type could not be mapped to word/definition "
            f"fields:\n\nNote type: {type_name}\n"
            f"Fields: {', '.join(fields[:8])}{' …' if len(fields) > 8 else ''}\n\n"
            f"Map it under Tools → CompreDef Configuration → Fields."
        )
        try:
            from aqt.utils import askUserDialog  # type: ignore
            diag = askUserDialog(
                msg, ["Open Configuration…", "Cancel"],
                parent=editor.parentWindow if editor is not None else None,
                title="CompreDef — field mapping needed",
            )
            if diag.run() == "Open Configuration…":
                try:
                    from .gui import show_config_dialog
                except Exception:
                    from gui import show_config_dialog  # type: ignore
                # Fields tab is index 1 (Scope=0, Fields=1).
                show_config_dialog(initial_tab=1)
        except Exception:
            tooltip(msg, parent=editor.parentWindow if editor else None)
        return

    deck_txt = ", ".join(note_decks) if note_decks else "(unknown — no cards yet)"
    msg = (
        f"This note is outside the CompreDef Scope:\n\n"
        f"Note is in: {deck_txt}\n"
        f"Scope covers: {scope_txt}\n\n"
        f"Add this deck to the Scope and generate now?\n"
        f"(A note only needs ONE of its decks in the Scope.)"
    )
    try:
        from aqt.utils import askUserDialog  # type: ignore
        btns = ["Add deck & Generate", "Open Scope…", "Cancel"]
        parent_w = editor.parentWindow if editor is not None else None
        diag = askUserDialog(msg, btns, parent=parent_w,
                              title="CompreDef — outside Scope")
        # ButtonedDialog.run() returns the clicked button's string.
        result = diag.run()
    except Exception:
        # Cross-version fallback: plain tooltip with guidance.
        tooltip(
            f"CompreDef: note deck ({deck_txt}) is outside the Scope ({scope_txt}).\n"
            "Add the deck via Tools → CompreDef Scope.",
            parent=editor.parentWindow if editor else None,
        )
        return

    if result == "Add deck & Generate":
        if not note_decks:
            # No deck resolvable (deeply headless?): open the picker so
            # the user can fix it manually instead of a silent no-op.
            tooltip(
                "CompreDef: could not determine this note's deck.\n"
                "Pick decks manually via Tools → CompreDef Scope.",
                parent=editor.parentWindow if editor else None,
            )
            return
        if _add_deck_to_scope_and_reset(note_decks):
            # Retry generation with the updated config + fresh knowledge.
            on_editor_generate_definition(editor)
    elif result == "Open Scope…":
        try:
            from .gui import show_scope_dialog
        except Exception:
            from gui import show_scope_dialog  # type: ignore
        show_scope_dialog()


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
    # Attach the editor so scope/deck resolution can use the Add
    # window's DeckChooser for unsaved notes (id 0, no cards yet).
    try:
        note._cd_editor = editor  # type: ignore[attr-defined]
    except Exception:
        pass

    config = _get_addon_config()
    dictionaries = config.get("dictionaries", [])
    disabled_dictionaries = config.get("disabled_dictionaries", [])
    dictionary_folder = config.get("dictionary_folder", "")

    # Field mapping comes from the note's own type (multi-type 'targets'
    # when configured, legacy single-type otherwise). The Scope gate
    # decides membership; when it blocks, the quick-fix dialog explains
    # the deck mismatch and can add the deck on the spot. Mapping-only
    # failures (scope passes) get the mapping dialog instead.
    fields = resolve_fields_for_note(note, config)
    if fields is None:
        _offer_add_to_scope(note, editor)
        return
    word_field = fields["word_field"]
    reading_field = fields["reading_field"]
    def_field = fields["definition_field"]

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
    # When Yomitan is the selected source, local dictionaries are not required.
    # When local is selected but Yomitan fallback is enabled (default), we still
    # allow generation — engine will try Yomitan as fail-safe.
    _src = str(config.get("dictionary_source") or "local").strip().lower()
    _is_yomitan_src = _src in ("yomitan", "yomitan_api", "api")
    _yomitan_fallback_enabled = config.get("yomitan_fallback") is not False
    if not _is_yomitan_src and not dictionaries and not dictionary_folder and not _yomitan_fallback_enabled:
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
        return get_generator().generate(
            word_text,
            ladder_paths=resolve_ladder_paths(dictionaries, dictionary_folder, disabled_dictionaries),
            reading=reading_text,
        )

    def on_done(future) -> None:
        # Always release the in-flight lock, even on failure
        _generation_in_flight.discard(nid_key)
        try:
            definition_result = future.result()
            if not definition_result:
                # When Yomitan is the selected source, surface the actual
                # bridge error instead of the generic "No definition found"
                _is_yomitan = str(config.get("dictionary_source") or "local").strip().lower() in ("yomitan","yomitan_api","api")
                if _is_yomitan:
                    try:
                        if __package__:
                            from .yomitan import get_last_yomitan_error
                        else:
                            from yomitan import get_last_yomitan_error
                        err = get_last_yomitan_error()
                        if err:
                            print(f"CompreDef: Yomitan lookup failed for '{word_text}': {err}")
                            tooltip(
                                f"CompreDef: Yomitan not reachable.\n{err}\n"
                                f"Fix: In CompreDef config → Dictionary Source → Yomitan API →\n"
                                f"click 'Install / Repair Bridge', restart browser, and enable\n"
                                f"Yomitan → Settings → Advanced → General → Enable Yomitan API.\n"
                                f"Or switch back to Local dictionaries.",
                                parent=editor.parentWindow,
                            )
                            return
                    except Exception:
                        pass
                tooltip(
                    f"CompreDef: No definition found for '{word_text}'." + (" (Yomitan mode — is Yomitan running?)" if _is_yomitan else ""),
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
# Tab-to-Generate (automatic generation on word-field unfocus)
# ---------------------------------------------------------------------------

# Live editor registry: the unfocus hook gives us (note, field index) but NOT
# the editor instance, and the old implementation guessed it by walking
# topLevelWidgets() — which could grab the WRONG editor when several windows
# are open. Instead we track editors as they load notes via
# editor_did_load_note (fires for both editor generations) and match them to
# blurred notes in the unfocus hook (identity first, then note id).
_live_editors: List[Any] = []


def _register_editor(editor) -> None:
    """
    Hook callback for `editor_did_load_note`: keeps a weak registry of live
    editors so Tab-to-Generate can map a blurred note back to its editor.

    Anki does not fire a symmetric 'editor closed' hook, so the registry is
    pruned lazily: destroyed Qt objects are filtered out on each load
    (via `sip.isdeleted`, imported inside a try/except because the test
    stub has no Qt bindings).
    """
    # Import locally: tests stub aqt, where the Qt/sip bindings may be absent.
    try:
        from aqt.qt import sip  # type: ignore[attr-defined]

        def _gone(obj) -> bool:
            return sip.isdeleted(obj)

        alive = [e for e in _live_editors if not _gone(e)]
        if _live_editors and len(alive) != len(_live_editors):
            _live_editors[:] = alive
    except Exception:
        # No sip available (test stub): keep everything; the registry is
        # only a lookup aid, never a correctness requirement.
        pass
    if editor not in _live_editors:
        _live_editors.append(editor)


def _find_editor_for_note(note) -> Optional[Any]:
    """
    Returns a live editor currently editing `note`, if any.

    Identity match first: the legacy editor keeps the SAME Note object the
    blur hook hands us, so object identity is exact — vital in the Add
    window where unsaved notes all share id 0 and an id-only match could
    pick a different open Add window. The id fallback covers any editor
    generation that resolves notes through the collection.
    """
    # Pass 1: exact object identity (legacy editors).
    for editor in reversed(_live_editors):  # most recently loaded wins
        if getattr(editor, "note", None) is note:
            return editor
    # Pass 2: match by note id (collection-resolved notes, same id).
    target_nid = getattr(note, "id", None)
    if not target_nid:  # 0/None = unsaved note; identity pass already failed
        return None
    for editor in reversed(_live_editors):
        candidate = _resolve_editor_note(editor)
        if candidate is not None and getattr(candidate, "id", None) == target_nid:
            return editor
    return None


def _tab_generate_enabled(config: Dict[str, Any]) -> bool:
    """
    Resolves whether Tab-to-Generate is active for this config.

    Defaults to ON (this was the feature's historical behaviour when it
    worked); explicitly set to False to disable. Kept as a separate pure
    function so the regression suite can exercise the decision matrix.
    """
    return bool(config.get("tab_generate", True))


def _should_auto_generate(note, unfocused_field: str, config: Dict[str, Any],
                          editor: Any = None) -> bool:
    """
    Pure decision function for Tab-to-Generate — returns True when leaving
    `unfocused_field` on `note` should kick off automatic generation.

    Conditions (all must hold):
    - The feature is enabled in config.
    - The unfocused field IS the configured word field.
    - The definition field exists and is empty (never overwrite existing
      content — explicit regeneration stays available via the toolbar
      button).
    """
    if not _tab_generate_enabled(config):
        return False

    # Scope gate first: out-of-scope notes never auto-generate (this
    # covers both the empty-scope state and notes in other decks).
    # editor enables Add-window deck resolution for unsaved notes.
    if not _note_in_scope(note, config, editor=editor):
        return False

    # Multi-type mode: only fire when the note's type is a configured
    # target AND its own word/definition fields are involved.
    targets = config.get("targets")
    if isinstance(targets, dict) and targets:
        mapping = targets.get(_get_note_type_name(note))
        if not isinstance(mapping, dict):
            return False
        word_field = str(mapping.get("word_field", "") or "")
        def_field = str(mapping.get("definition_field", "") or "")
    else:
        # Legacy single-type config
        legacy_type = str(config.get("note_type", "") or "").strip()
        if legacy_type and _get_note_type_name(note) != legacy_type:
            return False
        word_field = config.get("word_field", "")
        def_field = config.get("definition_field", "")

    if not word_field or not def_field or word_field == def_field:
        return False
    if unfocused_field != word_field:
        return False
    if def_field not in note:
        return False

    # Only auto-fill EMPTY definition fields. .strip() on the raw field
    # covers whitespace-only content; HTML emptiness (e.g. a lone <br>) is
    # handled by extract_clean_word downstream during generation itself.
    return not note[def_field].strip()


def on_field_unfocus(changed: bool, note, current_field_index: int) -> bool:
    """
    Hook callback for `editor_did_unfocus_field` — the Tab-to-Generate seam.

    Fired by the legacy editor when a field loses focus (Tab, click-away,
    window switch). If the blurred field is the configured word field and
    the definition field is empty, generation starts in the background.

    CRITICAL: returns `changed` UNTOUCHED. The legacy editor reloads the
    note when any filter returns True, which raced with our background
    write and deleted freshly generated definitions (historical bug).
    Returning the input unchanged leaves the reload decision to Anki;
    our own persistence path (update_note BEFORE editor refresh) is what
    makes the definition survive.
    """
    try:
        if not mw or not note:
            return changed

        config = _get_addon_config()
        if not _should_auto_generate(
            note, _field_name_at(note, current_field_index), config,
            editor=_find_editor_for_note(note),
        ):
            return changed

        editor = _find_editor_for_note(note)
        if editor is None:
            # No live editor for this note (e.g. programmatic blur) —
            # do nothing rather than guess at a window like the old code.
            return changed

        # Reuse the exact same generation path as the toolbar button
        # (validation, single-flight guard, background thread, safe
        # persistence) so Tab and button can never diverge in behaviour.
        # The editor is attached to the note so resolve_fields_for_note
        # can resolve the Add window's deck for unsaved notes.
        try:
            note._cd_editor = editor  # type: ignore[attr-defined]
        except Exception:
            pass
        on_editor_generate_definition(editor)
    except Exception:
        # A hook failure must never break editing; log loudly and move on.
        print(f"CompreDef: Tab-to-Generate unfocus hook failed:\n{traceback.format_exc()}")
    return changed


def _field_name_at(note, index: int) -> str:
    """
    Resolves the field name for a field ordinal via the non-deprecated
    note API. Returns '' for out-of-range indices (hook can fire during
    notetype switches with a stale index).
    """
    try:
        names = list(note.keys())
        if 0 <= index < len(names):
            return names[index]
    except Exception:
        pass
    return ""


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
    dictionaries = config.get("dictionaries", [])
    disabled_dictionaries = config.get("disabled_dictionaries", [])
    dictionary_folder = config.get("dictionary_folder", "")

    # Early validation: without any dictionary configured nothing can be generated
    _src2 = str(config.get("dictionary_source") or "local").strip().lower()
    _is_yomitan_src2 = _src2 in ("yomitan", "yomitan_api", "api")
    _yomitan_fallback_enabled2 = config.get("yomitan_fallback") is not False
    if not _is_yomitan_src2 and not dictionaries and not dictionary_folder and not _yomitan_fallback_enabled2:
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

                # Field mapping per note type (multi-type 'targets' or
                # legacy single-type). Unconfigured types are skipped
                # silently in bulk — the selection may span many types.
                fields = resolve_fields_for_note(note, config)
                if fields is None:
                    skipped_count += 1
                    continue
                word_field = fields["word_field"]
                reading_field = fields["reading_field"]
                def_field = fields["definition_field"]

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
                definition_result = get_generator().generate(
                    word_text,
                    ladder_paths=resolve_ladder_paths(dictionaries, dictionary_folder, disabled_dictionaries),
                    reading=reading_text,
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

        # Learner knowledge is a per-session snapshot: bulk definition
        # writes must NOT invalidate it (no repeated full collection scan).

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

    Tab-to-Generate is registered via `editor_did_unfocus_field` + the
    `editor_did_load_note` registry. There is deliberately NO JS key
    listener and NO webview bridge monkeypatching: the old Tab feature
    relied on both, they never fired on the Svelte editor, and the
    unfocus hook already covers every Tab/click-away path natively.
    """
    gui_hooks.editor_did_init_buttons.append(add_editor_button)
    gui_hooks.browser_menus_did_init.append(setup_browser_menu)
    gui_hooks.browser_will_show_context_menu.append(setup_browser_context_menu)
    # Tab-to-Generate: map blurred notes back to their editor, and react
    # to word-field unfocus (returns `changed` untouched — see on_field_unfocus).
    gui_hooks.editor_did_load_note.append(_register_editor)
    gui_hooks.editor_did_unfocus_field.append(on_field_unfocus)
