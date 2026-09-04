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

def _get_root_addon_name() -> str:
    """Retrieves the root add-on package name for config access."""
    if not mw or not hasattr(mw, "addonManager"):
        return ""
    # We are in 'compredef.anki', we want 'compredef'
    return __name__.split('.')[0]

def _warn_db_error(msg: str) -> None:
    """
    Surfaces database failures visibly instead of failing silently.
    A silent empty knowledge set (0 known kanji) is indistinguishable
    from a genuine beginner collection — the v1.0.5 'JOIN models' bug
    proved this must be loud. Warns once per session to avoid spam.
    """
    global _db_warned
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
    Fetches the Expression field of every mature note of the configured
    note type. Returns a list of (word_text,) already extracted.

    Schema-proof by design: the query touches ONLY the 'notes' and
    'cards' tables (stable across Anki versions) and resolves each note's
    model via the public mw.col.models API. It must NEVER reference the
    legacy 'models' table by name — that table was renamed to 'notetypes'
    in Anki 23.10+, so 'JOIN models' fails with 'no such table' on modern
    Anki and silently yields an empty knowledge set.
    """
    try:
        if not mw or not mw.col:
            return []

        # 1. Configured note type and word field
        root_name = _get_root_addon_name()
        config = mw.addonManager.getConfig(root_name) or {}
        note_type_name = config.get("note_type")
        word_field_name = config.get("word_field")

        if not note_type_name or not word_field_name:
            return []

        # 2. Mature notes: (mid, flds). The mid identifies the note's
        # model without any assumption about Anki's internal table names.
        rows = mw.col.db.all(
            "SELECT mid, flds FROM notes "
            "WHERE id IN (SELECT nid FROM cards WHERE ivl >= 21)"
        ) or []

        # 3. Resolve mid -> Expression-field index once per model, keeping
        # only notes whose model IS the configured note type.
        index_by_mid: dict = {}
        out = []
        for row in rows:
            mid, flds_blob = row[0], row[1]
            if mid not in index_by_mid:
                index_by_mid[mid] = _expression_index(mid, note_type_name,
                                                      word_field_name)
            field_index = index_by_mid[mid]
            if field_index is None:
                continue
            if not flds_blob or not isinstance(flds_blob, str):
                continue
            fields = flds_blob.split(_FIELD_SEP)
            if field_index >= len(fields):
                continue
            word_text = fields[field_index].strip()
            if word_text:
                out.append(word_text)
        return out
    except Exception as e:
        _warn_db_error(f"DB error while fetching learned notes: {e}")
        return []

def _expression_index(mid: int, note_type_name: str,
                      word_field_name: str) -> Optional[int]:
    """
    Returns the index of the configured word field for the model 'mid',
    or None if that model is not the configured note type or lacks the
    field. Model lookup goes through mw.col.models (public API), never
    raw SQL against Anki's internal tables.
    """
    try:
        model = mw.col.models.get(mid)
    except Exception:
        return None
    if not model or model.get("name") != note_type_name:
        return None
    for i, f in enumerate(model.get("flds", [])):
        name = f.get("name") if isinstance(f, dict) else f
        if name == word_field_name:
            return i
    return None

def _build_caches() -> None:
    """Internal worker to build the session knowledge snapshot."""
    global _known_kanji_cache, _known_vocab_cache
    with _build_lock:
        if _caches_ready.is_set():
            return

        known_kanji: Set[str] = set()
        known_words: Set[str] = set()

        # _fetch_learned_note_fields already returns ONLY the configured
        # Expression field text — kanji from Definition/Example fields can
        # never reach the known set (and generated definitions written to
        # the Definition field can never pollute it).
        for word_text in _fetch_learned_note_fields():
            if not word_text or not isinstance(word_text, str):
                continue
            known_kanji.update(_KANJI_RE.findall(word_text))
            known_words.add(word_text)

        _known_kanji_cache = known_kanji
        _known_vocab_cache = known_words
        _caches_ready.set()

def init_caches_async() -> None:
    """Triggers asynchronous build of learner knowledge snapshot during startup."""
    if not mw or not hasattr(mw, "taskman"):
        return
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
    _caches_ready.clear()
    init_caches_async()

