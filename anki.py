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

def _get_root_addon_name() -> str:
    """Retrieves the root add-on package name for config access."""
    if not mw or not hasattr(mw, "addonManager"):
        return ""
    # We are in 'compredef.anki', we want 'compredef'
    return __name__.split('.')[0]

def _fetch_learned_note_fields() -> list:
    """
    Fetches the 'flds' blob for all mature notes of the configured note type.
    Returns a list of (flds, field_index).
    """
    try:
        if not mw or not mw.col:
            return []

        # 1. Get configured note type and word field
        root_name = _get_root_addon_name()
        config = mw.addonManager.getConfig(root_name) or {}
        note_type_name = config.get("note_type")
        word_field_name = config.get("word_field")

        if not note_type_name or not word_field_name:
            return []

        # 2. Find the index of the word_field in the model
        model = mw.col.models.by_name(note_type_name)
        if not model:
            return []
        
        # model["flds"] is a list of dicts like {"name": "Expression", ...}
        field_index = None
        for i, f in enumerate(model["flds"]):
            if f["name"] == word_field_name:
                field_index = i
                break
        
        if field_index is None:
            return []

        # 3. Query for flds of mature notes of this specific type
        query = """
        SELECT n.flds
        FROM notes n
        JOIN models m ON n.mid = m.id
        WHERE m.name = ? 
          AND n.id IN (SELECT nid FROM cards WHERE ivl >= 21)
        """
        rows = mw.col.db.all(query, (note_type_name,)) or []
        
        # Return the flds and the target index for extraction
        return [(row[0], field_index) for row in rows]
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

        for flds_blob, field_index in _fetch_learned_note_fields():
            if not flds_blob or not isinstance(flds_blob, str): 
                continue
            
            # Split the flds blob into individual fields
            fields = flds_blob.split(_FIELD_SEP)
            if field_index >= len(fields):
                continue
            
            # CORRECTNESS FIX: Only extract knowledge from the Expression field
            word_text = fields[field_index].strip()
            if not word_text:
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

