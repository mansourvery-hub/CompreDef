import re
import threading
from aqt import mw
from typing import Set, Optional

_KANJI_RE = re.compile(r'[\u4e00-\u9fff]')
_FIELD_SEP = '\x1f'

_known_kanji_cache: Set[str] = set()
_known_vocab_cache: Set[str] = set()
_caches_ready = threading.Event()
_build_lock = threading.Lock()
_db_warned = False
# Last-build diagnostics (read via knowledge_status() in the Debug Console)
_last_rows_scanned = 0
_last_words_kept = 0
_last_error: Optional[str] = None

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

def _fetch_learned_note_fields() -> list:
    """
    Returns the first-field text of every mature note, across ALL note
    types. Learner proficiency is collection-wide: a user with vocabulary
    spread over many decks and note types must not have their knowledge
    gated on a single configured type.

    Only the first field is used — conventionally the word / expression /
    front across note types. Every other field is ignored, so definitions
    (including CompreDef's own generated ones), examples, readings, and
    notes can never pollute the known set.

    Schema-proof by design: the query touches ONLY the 'notes' and
    'cards' tables (stable across Anki versions). It must NEVER reference
    the legacy 'models' table by name — renamed to 'notetypes' in Anki
    23.10+, so 'JOIN models' fails with 'no such table' on modern Anki
    and silently yields an empty knowledge set.
    """
    try:
        if not mw or not mw.col:
            return []

        rows = mw.col.db.all(
            "SELECT flds FROM notes "
            "WHERE id IN (SELECT nid FROM cards WHERE ivl >= 21)"
        ) or []
        global _last_rows_scanned
        _last_rows_scanned = len(rows)

        out = []
        for row in rows:
            blob = row[0] if isinstance(row, (list, tuple)) else row
            if not blob or not isinstance(blob, str):
                continue
            word_text = blob.split(_FIELD_SEP, 1)[0].strip()
            if word_text:
                out.append(word_text)
        return out
    except Exception as e:
        _warn_db_error(f"DB error while fetching learned notes: {e}")
        return []

def _build_caches() -> None:
    """Internal worker to build the session knowledge snapshot."""
    global _known_kanji_cache, _known_vocab_cache, _last_words_kept
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

        known_kanji: Set[str] = set()
        known_words: Set[str] = set()

        # _fetch_learned_note_fields already returns ONLY first-field
        # text — kanji from definitions (including CompreDef's own
        # generated ones), examples, readings, and notes can never reach
        # the known set.
        for word_text in _fetch_learned_note_fields():
            if not word_text or not isinstance(word_text, str):
                continue
            known_kanji.update(_KANJI_RE.findall(word_text))
            known_words.add(word_text)

        _known_kanji_cache = known_kanji
        _known_vocab_cache = known_words
        _last_words_kept = len(known_words)
        _caches_ready.set()
        print(f"CompreDef: learner snapshot built: "
              f"{len(known_kanji)} kanji / {len(known_words)} words "
              f"from {_last_rows_scanned} mature notes "
              f"(all note types, first field only)")

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
    if not _caches_ready.is_set():
        _build_caches()
    return _known_kanji_cache

def get_known_vocabulary_set() -> Set[str]:
    if not _caches_ready.is_set():
        _build_caches()
    return _known_vocab_cache

def reset_caches() -> None:
    """Manual refresh of the knowledge snapshot."""
    global _last_rows_scanned, _last_words_kept, _last_error
    _last_rows_scanned = 0
    _last_words_kept = 0
    _last_error = None
    _caches_ready.clear()
    init_caches_async()


def knowledge_status() -> dict:
    """
    One-shot diagnostics for the Debug Console: what the snapshot was
    built from and what it holds. Example:
        import importlib
        m = importlib.import_module("1619602654.anki")
        print(m.knowledge_status())
    """
    return {
        "ready": _caches_ready.is_set(),
        "known_kanji": len(_known_kanji_cache),
        "known_words": len(_known_vocab_cache),
        "mature_notes_scanned": _last_rows_scanned,
        "words_kept": _last_words_kept,
        "scope": "all note types, first field only",
        "last_error": _last_error,
    }


def knowledge_summary_text(max_kanji: int = 2000,
                           max_words: int = 100) -> str:
    """
    Human-readable snapshot summary for the knowledge dialog and the
    Debug Console. Pure stdlib logic, covered by the regression suite.
    """
    known = get_known_kanji_set()
    vocab = get_known_vocabulary_set()
    status = knowledge_status()
    kanji_list = "".join(sorted(known))
    if len(kanji_list) > max_kanji:
        kanji_list = (kanji_list[:max_kanji] +
                      f"… (+{len(known) - max_kanji} more)")
    words = sorted(vocab)
    words_shown = ", ".join(words[:max_words])
    if len(words) > max_words:
        words_shown += f", … (+{len(words) - max_words} more)"
    lines = [
        f"Known kanji: {len(known)}",
        f"Known words: {len(vocab)}",
        f"Scope: {status['scope']}",
        f"Mature notes scanned: {status['mature_notes_scanned']}",
    ]
    if status["last_error"]:
        lines.append(f"Last error: {status['last_error']}")
    lines += ["", "Kanji:", kanji_list or "(none)", "",
              "Words (sample):", words_shown or "(none)"]
    return "\n".join(lines)

