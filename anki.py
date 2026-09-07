import re
import threading
from aqt import mw
from typing import Dict, List, Optional, Set, Tuple

# Dual-context sibling imports (relative inside Anki's package load,
# absolute in the top-level test harness — see core.py for why).
if __package__:
    from .scope import (SCOPE_CONFIG_KEY, get_scope_decks, scope_dids,
                        implied_note_types)
else:
    from scope import (SCOPE_CONFIG_KEY, get_scope_decks, scope_dids,
                       implied_note_types)

_KANJI_RE = re.compile(r'[\u4e00-\u9fff]')
# "Word" knowledge = multi-kanji compounds ONLY. Pure-kana words are
# inflection-hostile (やめる vs やめて) and single kanji are already
# covered by the kanji score, so both are excluded from vocab points.
_KANJI_WORD_RE = re.compile(r'^[\u4e00-\u9fff]{2,}$')
_FIELD_SEP = '\x1f'

# Interval-weighted knowledge (v1.2 scoring algorithm):
# each kanji/vocab scores ivl/365 capped at 1.0 — a year-old interval
# is full mastery, anything less counts proportionally.
_FULL_MASTERY_IVL_DAYS = 365.0

# v1.2.3: a note/card counts as MATURE only once its interval reaches a
# FULL YEAR (user decision; the legacy 21-day threshold is deprecated).
# Single source of truth for the SQL query and every user-facing label.
_MATURE_IVL_DAYS = 365

_known_kanji_cache: Set[str] = set()
_known_vocab_cache: Set[str] = set()
_kanji_points_cache: Dict[str, float] = {}
_vocab_points_cache: Dict[str, float] = {}
# Scope note-type first-field names, resolved once per snapshot (they
# change only when the user edits note types or the Scope — never per
# dialog open). None = not yet resolved.
_scope_first_fields_cache: Optional[List[str]] = None
_caches_ready = threading.Event()
_build_lock = threading.Lock()
_db_warned = False
# Last-build diagnostics (read via knowledge_status() in the Debug Console)
_last_rows_scanned = 0
_last_words_kept = 0
_last_error = None
_last_scope_label = ""

def _warn_db_error(msg: str) -> None:
    """
    Surfaces database failures visibly instead of failing silently.
    A silent empty knowledge set (0 known kanji) is indistinguishable
    from a genuine beginner collection — the v1.0.5 'JOIN models' bug
    proved this must be loud. Warns once per session to avoid spam.
    """
    global _db_warned, _last_error
    _last_error = msg
    print(f"CompreDef: {msg}")
    if _db_warned:
        return
    _db_warned = True
    try:
        from aqt.utils import tooltip
        tooltip(f"CompreDef: {msg}")
    except Exception:
        pass  # headless/test environments have no tooltip; print suffices

def _get_scope_deck_names() -> List[str]:
    """
    Reads the user's Scope deck selection from add-on config.

    Returns [] when unconfigured — which the fetch below treats as
    fail-closed (empty knowledge), never as whole-collection.
    """
    try:
        if not mw or not hasattr(mw, "addonManager"):
            return []
        try:
            name = mw.addonManager.addonFromModule(__name__)
        except Exception:
            name = None
        if not name:
            name = "1619602654"
        cfg = mw.addonManager.getConfig(name)
        return get_scope_decks(cfg if isinstance(cfg, dict) else {})
    except Exception:
        return []


def _maturity_points(ivl: float) -> float:
    """Interval-weighted mastery: ivl/365, capped at 1.0.

    A one-year interval earns the full point (fluently known);
    younger intervals count proportionally. Matches the user's v1.2
    scoring spec exactly.
    """
    try:
        days = float(ivl)
    except (TypeError, ValueError):
        return 0.0
    if days <= 0:
        return 0.0
    if days >= _FULL_MASTERY_IVL_DAYS:
        return 1.0
    return days / _FULL_MASTERY_IVL_DAYS


def _first_field_text(flds_blob: str) -> str:
    """Extracts the clean first-field text from a notes.flds blob."""
    if not flds_blob or not isinstance(flds_blob, str):
        return ""
    return flds_blob.split(_FIELD_SEP, 1)[0].strip()


def _fetch_learned_note_rows(mature_only: bool = True) -> List[Tuple[str, float]]:
    """
    Returns [(first_field_text, max_card_interval_days)] for every note
    inside the Scope.

    mature_only=True (default — the knowledge snapshot's admission rule):
    only notes owning a card with interval >= 365 days (a full year;
    the legacy 21-day threshold is deprecated). These are the MATURE
    notes: their kanji/vocab earn mastery points.

    mature_only=False: every in-scope note with a strictly positive
    interval — used by the knowledge dialog to also show the SEEN
    kanji/vocab (ivl > 0, any age) alongside the mastered ones.

    Only cards in the user's selected Scope decks (subdecks included)
    count — a French deck must never inflate Japanese kanji knowledge.
    An empty scope (or one whose decks are all missing/renamed) is
    fail-closed and yields no rows at all.

    Only the first field is used — conventionally the word / expression /
    front across note types. Every other field is ignored, so definitions
    (including CompreDef's own generated ones), examples, readings, and
    notes can never pollute the known set.

    Schema-proof by design: the query touches ONLY the 'notes' and
    'cards' tables (stable across Anki versions). It must NEVER reference
    the legacy 'models' table by name — renamed to 'notetypes' in Anki
    23.10+, so 'JOIN models' fails with 'no such table' on modern Anki
    and silently yields an empty knowledge set. Deck ids are interpolated
    as plain integers (they come from Anki's own deck map, so there is no
    injection surface) to avoid db.all() placeholder-style drift across
    Anki versions.
    """
    global _last_scope_label
    try:
        if not mw or not mw.col:
            return []

        scope = _get_scope_deck_names()
        if not scope:
            # Fail-closed by design: no scope selected means no knowledge
            # (the GUI shows a "pick your decks" warning for this state).
            _last_scope_label = "empty selection (fail-closed)"
            global _last_rows_scanned
            _last_rows_scanned = 0
            return []

        dids = scope_dids(mw.col, scope)
        _last_scope_label = (
            f"{len(scope)} deck(s): {', '.join(scope)}"
            if dids
            else f"no matching decks for: {', '.join(scope)} (renamed?)"
        )
        if not dids:
            _last_rows_scanned = 0
            return []

        # ivl >= 1 = "seen" (strictly positive interval); >= 365 = mature.
        ivl_floor = _MATURE_IVL_DAYS if mature_only else 1
        did_list = ",".join(str(int(d)) for d in sorted(dids))
        rows = mw.col.db.all(
            "SELECT notes.flds, MAX(cards.ivl) FROM notes "
            "JOIN cards ON cards.nid = notes.id "
            f"WHERE cards.ivl >= {ivl_floor} "
            f"AND cards.did IN ({did_list}) "
            "GROUP BY notes.id"
        ) or []
        if mature_only:
            _last_rows_scanned = len(rows)

        out: List[Tuple[str, float]] = []
        for row in rows:
            blob = row[0] if isinstance(row, (list, tuple)) else row
            if not blob or not isinstance(blob, str):
                continue
            word_text = _first_field_text(blob)
            if word_text:
                try:
                    ivl = float(row[1]) if isinstance(row, (list, tuple)) and len(row) > 1 else 0.0
                except (TypeError, ValueError):
                    ivl = 0.0
                out.append((word_text, ivl))
        return out
    except Exception as e:
        _warn_db_error(f"DB error while fetching learned notes: {e}")
        return []

def _build_caches() -> None:
    """Internal worker to build the session knowledge snapshot.

    v1.2 algorithm: knowledge is INTERVAL-WEIGHTED, not binary.
    - Kanji points: for each kanji in a first field, max(ivl)/365
      capped at 1.0.
    - Vocab points: same, but ONLY for multi-kanji compounds
      (single kanji covered by kanji score; kana-only words excluded
      to dodge inflection mismatches like やめる/やめて).
    The engine combines both dictionaries when scoring definitions.
    """
    global _known_kanji_cache, _known_vocab_cache
    global _kanji_points_cache, _vocab_points_cache, _last_words_kept
    with _build_lock:
        if _caches_ready.is_set():
            return
        if not mw or not mw.col:
            # Collection not open yet: add-ons load BEFORE the profile,
            # so at startup mw.col is None. Building now would snapshot
            # an EMPTY collection and mark it ready for the whole
            # session — the v1.0.10/11 '0 known kanji' regression.
            # Leave the snapshot unbuilt; the profile_did_open hook
            # (or a first getter call once the collection exists)
            # builds the real one.
            return

        kanji_points: Dict[str, float] = {}
        vocab_points: Dict[str, float] = {}

        # _fetch_learned_note_rows already returns ONLY first-field
        # text + intervals — kanji from definitions (including
        # CompreDef's own generated ones), examples, readings, and
        # notes can never reach the known set.
        for word_text, ivl in _fetch_learned_note_rows():
            if not word_text or not isinstance(word_text, str):
                continue
            pts = _maturity_points(ivl)
            for kanji in set(_KANJI_RE.findall(word_text)):
                if pts > kanji_points.get(kanji, 0.0):
                    kanji_points[kanji] = pts
            # Vocab: multi-kanji compounds only (v1.2 spec).
            if _KANJI_WORD_RE.match(word_text):
                if pts > vocab_points.get(word_text, 0.0):
                    vocab_points[word_text] = pts

        _kanji_points_cache = kanji_points
        _vocab_points_cache = vocab_points
        # Legacy binary views derived from the points (>0 ⇒ known).
        _known_kanji_cache = set(kanji_points.keys())
        _known_vocab_cache = set(vocab_points.keys())
        _last_words_kept = len(vocab_points)
        # v1.2.5: scope first-field names are part of the snapshot —
        # resolved here ONCE instead of on every dialog open (the
        # 'cache aggressively, rebuild rarely' mandate).
        global _scope_first_fields_cache
        _scope_first_fields_cache = _resolve_scope_first_fields()
        _caches_ready.set()
        print(f"CompreDef: learner snapshot built: "
              f"{len(kanji_points)} mastered kanji / "
              f"{len(vocab_points)} mastered vocab (kanji-only compounds) "
              f"from {_last_rows_scanned} mature notes "
              f"(mastery = interval / {_FULL_MASTERY_IVL_DAYS:.0f}, "
              f"mature = ivl >= {_MATURE_IVL_DAYS}, scope "
              f"[{_last_scope_label}], first field only)")

def init_caches_async() -> None:
    """
    Triggers the asynchronous snapshot build — but ONLY once a
    collection is actually open. At add-on load time mw.col is None
    (the profile has not opened yet); scheduling a build then would
    snapshot an EMPTY collection and mark it ready for the whole
    session. __init__.py re-invokes this from the profile_did_open
    hook, so the real build starts the moment the profile opens.
    """
    if not mw or not hasattr(mw, "taskman"):
        return
    if not mw.col:
        return  # profile not open yet; profile_did_open re-triggers
    mw.taskman.run_in_background(_build_caches)

def get_known_kanji_set() -> Set[str]:
    """Binary view of kanji knowledge (any interval > 0)."""
    if not _caches_ready.is_set():
        _build_caches()
    return _known_kanji_cache

def get_known_vocabulary_set() -> Set[str]:
    """Binary view of vocab knowledge (kanji-only compounds)."""
    if not _caches_ready.is_set():
        _build_caches()
    return _known_vocab_cache

def get_kanji_points() -> Dict[str, float]:
    """Interval-weighted kanji mastery: {kanji: ivl/365 capped at 1.0}."""
    if not _caches_ready.is_set():
        _build_caches()
    return _kanji_points_cache

def get_vocab_points() -> Dict[str, float]:
    """Interval-weighted vocab mastery: {compound: ivl/365 capped at 1.0}.

    Only multi-kanji compounds — kana-only and single-kanji words are
    deliberately excluded (v1.2 spec: avoid inflection mismatches).
    """
    if not _caches_ready.is_set():
        _build_caches()
    return _vocab_points_cache

def reset_caches() -> None:
    """Manual refresh of the knowledge snapshot (main-thread safe only).

    Schedules the rebuild via mw.taskman — which Anki's Taskman
    permits ONLY from the main thread (it prints a 'bug: not called
    from main thread' traceback otherwise; the v1.2.1 knowledge
    dialog triggered exactly that from its background task).
    Background threads must call sync_reset_caches() instead.
    """
    global _last_rows_scanned, _last_words_kept, _last_error, _last_scope_label
    global _scope_first_fields_cache
    _last_rows_scanned = 0
    _last_words_kept = 0
    _last_error = None
    _last_scope_label = ""
    _scope_first_fields_cache = None
    _caches_ready.clear()
    init_caches_async()


def sync_reset_caches() -> None:
    """Thread-safe manual refresh: rebuilds SYNCHRONOUSLY on the caller's
    thread, never touching mw.taskman.

    For background tasks (mw.taskman.run_in_background workers, the
    knowledge dialog's data gatherer): reset_caches() would call
    taskman.run_in_background from a NON-main thread, which Anki's
    Taskman flags as a bug. _build_caches is lock-guarded and only
    touches Anki's DB wrapper, so it is safe to run on any thread.
    """
    global _last_rows_scanned, _last_words_kept, _last_error, _last_scope_label
    global _scope_first_fields_cache
    _last_rows_scanned = 0
    _last_words_kept = 0
    _last_error = None
    _last_scope_label = ""
    _scope_first_fields_cache = None
    _caches_ready.clear()
    # Lazy-build contract: getters see not-ready → _build_caches() runs
    # inline on THIS thread. A closed collection stays not-ready (the
    # col-gate), matching test_snapshot_waits_for_open_collection.
    _build_caches()


def knowledge_totals() -> dict:
    """
    Totals for the X/total readouts in the knowledge dialog (pure
    SQLite counts via Anki's DB wrapper — NEVER an external
    connection; see AGENTS.md).

    Returns {'kanji', 'vocab', 'scope_notes', 'mature_notes',
    'total_notes'}:
    - kanji / vocab: every distinct kanji / multi-kanji compound in the
      FIRST field of ANY note in the Scope decks (the "how much
      Japanese lives in your scoped decks" denominator).
    - scope_notes: notes in the Scope decks — the PRIMARY denominator
      for Mature notes (v1.2.3: the old collection-wide 57185 made
      "10705/57185" meaningless; the user only cares about scope).
    - mature_notes: in-scope notes owning a card with ivl >= 365.
    - total_notes: every note in the collection (secondary info only).
    All counts are 0 when the DB is unavailable — the GUI then shows
    the plain numbers without denominators instead of crashing.
    """
    out = {"kanji": 0, "vocab": 0, "scope_notes": 0,
           "mature_notes": 0, "total_notes": 0}
    try:
        if not mw or not mw.col:
            return out
        # 1) Every distinct kanji / multi-kanji compound appearing in the
        #    FIRST field of ANY in-scope note — the "how much Japanese is
        #    in your decks" denominator for the seen X/total readouts.
        scope = _get_scope_deck_names()
        if scope:
            dids = scope_dids(mw.col, scope)
            if dids:
                did_list = ",".join(str(int(d)) for d in sorted(dids))
                rows = mw.col.db.all(
                    "SELECT DISTINCT notes.flds FROM notes "
                    "JOIN cards ON cards.nid = notes.id "
                    f"WHERE cards.did IN ({did_list})"
                ) or []
                kanji_seen: Set[str] = set()
                vocab_seen: Set[str] = set()
                for row in rows:
                    blob = row[0] if isinstance(row, (list, tuple)) else row
                    text = _first_field_text(blob if isinstance(blob, str) else "")
                    if not text:
                        continue
                    kanji_seen.update(_KANJI_RE.findall(text))
                    if _KANJI_WORD_RE.match(text):
                        vocab_seen.add(text)
                out["kanji"] = len(kanji_seen)
                out["vocab"] = len(vocab_seen)
                # 2) Notes in scope — the Mature-notes denominator the
                #    user asked for (NOT the whole collection).
                out["scope_notes"] = (
                    mw.col.db.scalar(
                        "SELECT COUNT(DISTINCT notes.id) FROM notes "
                        "JOIN cards ON cards.nid = notes.id "
                        f"WHERE cards.did IN ({did_list})"
                    ) or 0
                )
                # 3) Mature notes in scope: notes owning at least one card
                #    with ivl >= 365 (the snapshot's admission rule).
                out["mature_notes"] = (
                    mw.col.db.scalar(
                        "SELECT COUNT(DISTINCT notes.id) FROM notes "
                        "JOIN cards ON cards.nid = notes.id "
                        f"WHERE cards.ivl >= {_MATURE_IVL_DAYS} "
                        f"AND cards.did IN ({did_list})"
                    ) or 0
                )
        # 4) Total notes in the WHOLE collection (secondary readout).
        out["total_notes"] = mw.col.db.scalar(
            "SELECT COUNT() FROM notes") or 0
    except Exception:
        # Totals are a read-only nicety: never let them break the dialog.
        pass
    return out


def knowledge_status() -> dict:
    """
    One-shot diagnostics for the Debug Console: what the snapshot was
    built from and what it holds. Example:
        import importlib
        m = importlib.import_module("1619602654.anki")
        print(m.knowledge_status())
    """
    scope_suffix = f" [{_last_scope_label}]" if _last_scope_label else ""
    return {
        "ready": _caches_ready.is_set(),
        # v1.2.3 terminology: the snapshot admits only MATURE notes
        # (ivl >= 365), so every kanji/vocab in it has mastery 1.0 —
        # these are the MASTERED sets. 'Seen' (any ivl > 0) is a
        # separate, larger universe (get_seen_*_points).
        "mastered_kanji": len(_kanji_points_cache),
        "mastered_words": len(_vocab_points_cache),
        # Back-compat aliases for older console snippets.
        "known_kanji": len(_known_kanji_cache),
        "known_words": len(_known_vocab_cache),
        "mature_notes_scanned": _last_rows_scanned,
        "words_kept": _last_words_kept,
        "scope": f"mature notes (ivl >= {_MATURE_IVL_DAYS}, mastery "
        f"= interval / {_FULL_MASTERY_IVL_DAYS:.0f}) in scope decks, "
        "first field" + scope_suffix,
        "last_error": _last_error,
    }


def _seen_points() -> Tuple[Dict[str, float], Dict[str, float]]:
    """
    Builds SEEN kanji/vocab points from every in-scope note with a
    strictly positive interval (ivl > 0 — any age, not just mature).

    The user's v1.2.3 definitions:
    - SEEN kanji/vocab: appears on a note whose max interval is > 0.
    - MASTERED kanji/vocab: mastery weight is exactly 1.0 (interval
      >= one year). The main snapshot (mature notes only) IS the
      mastered set; this helper exists so the dialog can show both.
    Never touches taskman — safe on background threads (the dialog
    calls it inside its worker). Returns ({kanji: pts}, {vocab: pts}).
    """
    kanji_pts: Dict[str, float] = {}
    vocab_pts: Dict[str, float] = {}
    try:
        for word_text, ivl in _fetch_learned_note_rows(mature_only=False):
            if not word_text or not isinstance(word_text, str):
                continue
            pts = _maturity_points(ivl)
            for kanji in set(_KANJI_RE.findall(word_text)):
                if pts > kanji_pts.get(kanji, 0.0):
                    kanji_pts[kanji] = pts
            if _KANJI_WORD_RE.match(word_text):
                if pts > vocab_pts.get(word_text, 0.0):
                    vocab_pts[word_text] = pts
    except Exception:
        pass
    return kanji_pts, vocab_pts


def knowledge_summary_text(max_kanji: int = 2000,
                           max_words: int = 100) -> str:
    """
    Human-readable snapshot summary for the knowledge dialog and the
    Debug Console. Pure stdlib logic, covered by the regression suite.

    v1.2.3 terminology (user's definitions — 'known' was too loose):
    - MASTERED kanji/vocab: mastery weight 1.0 (note interval >= 1 year).
    - SEEN kanji/vocab: on any in-scope note with interval > 0.
    - 'Vocab' means kanji-only compounds (multi-kanji words); kana-only
      words and single kanji are excluded by design.
    Readouts are X/total where totals stay inside the Scope decks;
    mature notes are X / notes-in-scope (NOT the whole collection).
    """
    mastered = get_kanji_points()   # snapshot = mature notes ⇒ mastery 1.0
    mastered_vocab = get_vocab_points()
    seen_kanji_pts, seen_vocab_pts = _seen_points()
    status = knowledge_status()
    totals = knowledge_totals()
    kanji_list = "".join(sorted(mastered))
    if len(kanji_list) > max_kanji:
        kanji_list = (kanji_list[:max_kanji] +
                      f"… (+{len(mastered) - max_kanji} more)")
    words = sorted(mastered_vocab)
    words_shown = ", ".join(words[:max_words])
    if len(words) > max_words:
        words_shown += f", … (+{len(words) - max_words} more)"
    lines = [
        f"Mastered kanji: {len(mastered)} / {totals['kanji']} "
        "(mastery 1.0, interval >= 1 year)",
        f"Seen kanji: {len(seen_kanji_pts)} / {totals['kanji']} "
        "(any interval > 0)",
        f"Mastered vocab: {len(mastered_vocab)} / {totals['vocab']} "
        "(kanji-only compounds)",
        f"Seen vocab: {len(seen_vocab_pts)} / {totals['vocab']}",
        f"Source: {status['scope']}",
        f"Mature notes scanned: {status['mature_notes_scanned']}"
        f" / {totals['scope_notes']} notes in scope "
        f"(collection: {totals['total_notes']})",
    ]
    if status["last_error"]:
        lines.append(f"Last error: {status['last_error']}")
    lines += ["", "Mastered kanji (all):", kanji_list or "(none)", "",
              "Mastered vocab (all):", words_shown or "(none)"]
    return "\n".join(lines)


# Provenance search strings (v1.2.4): opening Anki's Browser from the
# knowledge dialog. Syntax follows the OFFICIAL Anki manual (docs.ankiweb.
# net/searching.html), verified against 26.08.1:
# - "field:value"  → field matches value EXACTLY ("front:dog" does not
#   match "a dog").
# - "field:*value*" → field CONTAINS value.
# - "re:^value$"    → regex whole-field exact match, any field.
# Quoting a term ("...") keeps special characters literal.

def _resolve_scope_first_fields() -> List[str]:
    """
    Uncached worker: the distinct FIRST-FIELD names of every note type
    that owns cards in the user's Scope decks.

    Why first fields only: the knowledge snapshot counts kanji/vocab
    ONLY from first fields, so provenance searches must target the same
    fields — searching definitions/examples would list notes that never
    contributed to the mastery points.

    Uses implied_note_types() (mid-based, schema-proof) then
    col.models to resolve each type's first field. Returns [] when
    nothing is resolvable — callers fall back to a regex search.
    """
    try:
        if not mw or not mw.col:
            return []
        scope = _get_scope_deck_names()
        if not scope:
            return []
        type_names = implied_note_types(mw.col, scope)
        models = getattr(mw.col, "models", None)
        if models is None:
            return []
        names: List[str] = []
        seen: Set[str] = set()
        for tn in type_names:
            model = None
            try:
                model = models.by_name(tn)
            except Exception:
                model = None
            if not model:
                continue
            flds = model.get("flds") if isinstance(model, dict) else None
            if not flds:
                continue
            try:
                first = flds[0].get("name", "")
            except (IndexError, AttributeError):
                continue
            if first and first not in seen:
                seen.add(first)
                names.append(str(first))
        return names
    except Exception:
        return []


def first_field_names_for_scope() -> List[str]:
    """
    CACHED first-field names for the Scope's note types (v1.2.5).

    The names are resolved once per snapshot inside _build_caches —
    they only change when the user edits note types or the Scope —
    and every subsequent call (each dialog open, each row click)
    reuses that list. Falls back to a lazy resolution only when the
    snapshot is not ready yet.
    """
    global _scope_first_fields_cache
    if _caches_ready.is_set() and _scope_first_fields_cache is not None:
        return list(_scope_first_fields_cache)
    # Snapshot not built yet: resolve now (and remember for next time
    # until a real build replaces it).
    _scope_first_fields_cache = _resolve_scope_first_fields()
    return list(_scope_first_fields_cache)


# Safety cap on the OR-list length: pathological collections can have
# dozens of note types in scope; the search bar stays readable and the
# query fast with at most this many field terms.
_MAX_PROVENANCE_FIELDS = 8


def _deck_search_terms() -> List[str]:
    """
    The Scope's deck names as quoted 'deck:"Name"' search terms.

    Per the manual, 'deck:french' matches the French deck AND its
    subdecks — exactly the Scope's expansion rule — so one deck term
    per selected deck reproduces the snapshot's universe. Names are
    double-quoted: deck names contain spaces ("deck:french words")
    and '::' separators that must stay literal.
    Never raises; [] when the scope is unreadable (the caller then
    searches without a deck restriction — same as before v1.2.5).
    """
    try:
        names = _get_scope_deck_names()
        return [f'"deck:{n}"' for n in names if n] or []
    except Exception:
        return []


def build_provenance_search(kind: str, term: str,
                            first_fields: Optional[List[str]] = None,
                            scope_decks: Optional[List[str]] = None) -> str:
    """
    Builds the Browser search string for a kanji/vocab row's provenance.

    kind='vocab': first-field EXACT match — mirrors how vocab points
    are admitted (the whole first field must BE the compound), so
    "Expression:学校" lists exactly the notes that gave 学校 its mastery.

    kind='kanji': first-field CONTAINS match — mirrors how kanji points
    are admitted (the first field contains the kanji anywhere), so
    "Expression:*学*" lists every contributing note.

    v1.2.5 SCOPE LIMIT: every field term is AND-ed with the Scope's
    deck terms ('deck:"My Life Decks"') — provenance must show only
    notes inside the Scope decks (the snapshot's universe), never
    collection-wide matches from unrelated decks. scope_decks may be
    passed explicitly (tests); otherwise the user's Scope config is
    read.

    Quoting rules per the manual: terms with special characters (the
    '*' wrapper) are double-quoted; plain exact matches are quoted too
    so colons/colons-in-field-names stay literal.

    Fallbacks when first_fields is empty/unresolvable: 're:^term$'
    (vocab, whole-field exact on any field) or the bare quoted term
    (kanji, substring — same as Anki's basic search). Both keep the
    deck restriction.
    """
    if not term:
        return ""
    # Dedupe while keeping order (first occurrence wins) — callers may
    # pass raw lists; the builder itself must be idempotent on dupes.
    clean: List[str] = []
    for f in (first_fields or []):
        name = str(f).strip()
        if name and name not in clean:
            clean.append(name)
    deck_terms: List[str] = []
    if scope_decks is None:
        deck_terms = _deck_search_terms()
    else:
        deck_terms = [f'"deck:{n}"' for n in scope_decks if str(n).strip()]
    # One shared deck restriction: deck:"A" or deck:"B" (a note can sit
    # in any ONE scoped deck), wrapped so it ANDs with the field match.
    deck_part = ""
    if deck_terms:
        deck_part = "(" + " or ".join(deck_terms) + ")"
    if not clean:
        field_part = (f'"re:^{term}$"' if kind != "kanji" else f'"{term}"')
    else:
        terms = []
        if kind == "kanji":
            # Contains: field:*term* (quoted — '*' is special unquoted).
            for f in clean[:_MAX_PROVENANCE_FIELDS]:
                terms.append(f'"{f}:*{term}*"')
        else:  # vocab: exact field match, no wildcards.
            for f in clean[:_MAX_PROVENANCE_FIELDS]:
                terms.append(f'"{f}:{term}"')
        field_part = " or ".join(terms)
    if deck_part:
        # AND binds the field matches to the deck restriction; the
        # field group is parenthesized so its inner ORs cannot leak
        # across the AND.
        return f"({field_part}) and {deck_part}"
    return field_part
