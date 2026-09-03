import re
import threading
from aqt import mw
from typing import Set

_KANJI_RE = re.compile(r'[\u4e00-\u9fff]')
_FIELD_SEP = '\x1f'

_known_kanji_cache: Set[str] = set()
_known_vocab_cache: Set[str] = set()
_caches_ready = threading.Event()
_build_lock = threading.Lock()

def _fetch_learned_note_fields() -> list:
    try:
        if not mw or not mw.col:
            return []
        query = """
        SELECT notes.flds
        FROM notes
        WHERE notes.id IN (SELECT cards.nid FROM cards WHERE cards.ivl >= 21)
        """
        return mw.col.db.all(query) or []
    except Exception as e:
        print(f"CompreDef: DB error while fetching learned notes: {e}")
        return []

def _build_caches() -> None:
    """Internal worker to build the session knowledge snapshot."""
    global _known_kanji_cache, _known_vocab_cache
    with _build_lock:
        if _caches_ready.is_set():
            return

        known_kanji: Set[str] = set()
        known_words: Set[str] = set()

        for row in _fetch_learned_note_fields():
            if not row: continue
            field_blob = row[0] if isinstance(row, (list, tuple)) else row
            if not field_blob or not isinstance(field_blob, str): continue
            known_kanji.update(_KANJI_RE.findall(field_blob))
            first_field = field_blob.split(_FIELD_SEP, 1)[0].strip()
            if first_field:
                known_words.add(first_field)

        _known_kanji_cache = known_kanji
        _known_vocab_cache = known_words
        _caches_ready.set()

def init_caches_async() -> None:
    """Triggers asynchronous build of learner knowledge snapshot during startup."""
    if not mw or not hasattr(mw, "taskman"):
        return
    mw.taskman.run_in_background(_build_caches)

def get_known_kanji_set() -> Set[str]:
    # If caches aren't ready, attempt to build them synchronously.
    # This ensures the test suite (and any non-async startup) doesn't deadlock.
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
    # Trigger a rebuild asynchronously
    init_caches_async()
