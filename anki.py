import re
from aqt import mw
from typing import Set

_KANJI_RE = re.compile(r'[\u4e00-\u9fff]')
_FIELD_SEP = '\x1f'

_known_kanji_cache: Set[str] = set()
_known_vocab_cache: Set[str] = set()
_caches_loaded: bool = False

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
    global _known_kanji_cache, _known_vocab_cache, _caches_loaded
    if _caches_loaded:
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
    _caches_loaded = True

def get_known_kanji_set() -> Set[str]:
    _build_caches()
    return _known_kanji_cache

def get_known_vocabulary_set() -> Set[str]:
    _build_caches()
    return _known_vocab_cache

def reset_caches() -> None:
    global _known_kanji_cache, _known_vocab_cache, _caches_loaded
    _known_kanji_cache = set()
    _known_vocab_cache = set()
    _caches_loaded = False
