"""
db_utils.py - Anki database interaction utilities for CompreDef.

Provides safe, read-only access to Anki's database for analyzing known
vocabulary and kanji, following the "Kanji Grid" approach by using the
native `mw.col.db` wrapper instead of direct SQL access (per AGENTS.md
rule 2: never open collection.anki2 with an external sqlite3 connection).
"""

from aqt import mw
from typing import Set

# Module-level caches. Scanning the whole collection is expensive, so the
# results are memoized. `reset_caches()` must be called if the collection
# changes (e.g. after bulk note edits) so scores stay accurate.
_known_kanji_cache: Set[str] = set()
_known_vocab_cache: Set[str] = set()
_kanji_cache_loaded: bool = False
_vocab_cache_loaded: bool = False


def _fetch_learned_note_fields() -> list:
    """
    Fetches the field blob of every note that has at least one card with
    interval > 0 (i.e. the user has actually learned the material).

    Wrapped in try/except so a database hiccup never crashes Anki
    (AGENTS.md rule 3.4: robust error handling around DB access).
    """
    try:
        if not mw or not mw.col:
            return []

        # Kanji Grid-style query: join notes with their cards and keep only
        # material the user has seen through review (interval > 0).
        query = """
        SELECT DISTINCT notes.flds
        FROM notes
        JOIN cards ON notes.id = cards.nid
        WHERE cards.ivl > 0
        """
        return mw.col.db.all(query) or []
    except Exception as e:
        print(f"CompreDef: DB error while fetching learned notes: {e}")
        return []


def get_known_kanji_set() -> Set[str]:
    """
    Scans the Anki database for all kanji present on cards with interval > 0.

    Results are cached after the first call to keep bulk generation fast.

    Returns:
        A set of unique kanji characters considered 'known'.
    """
    global _known_kanji_cache, _kanji_cache_loaded

    if _kanji_cache_loaded:
        return _known_kanji_cache

    known_kanji: Set[str] = set()

    for row in _fetch_learned_note_fields():
        field_blob = row[0] if row else ""
        if not field_blob:
            continue
        # Extract only Japanese kanji characters (Unicode range 4E00-9FFF)
        for char in field_blob:
            if '\u4e00' <= char <= '\u9fff':
                known_kanji.add(char)

    _known_kanji_cache = known_kanji
    _kanji_cache_loaded = True
    return known_kanji


def get_known_vocabulary_set() -> Set[str]:
    """
    Scans the Anki database for all vocabulary (words) present on cards
    with an interval > 0.

    The first field of each note is treated as the word/expression field
    (the standard convention for Japanese note types).

    Returns:
        A set of unique vocabulary words considered 'known'.
    """
    global _known_vocab_cache, _vocab_cache_loaded

    if _vocab_cache_loaded:
        return _known_vocab_cache

    known_words: Set[str] = set()

    for row in _fetch_learned_note_fields():
        field_blob = row[0] if row else ""
        if not field_blob:
            continue
        # Notes store fields joined by the unit-separator byte \x1f;
        # the first field is conventionally the expression/word.
        fields = field_blob.split('\x1f')
        if fields and fields[0].strip():
            known_words.add(fields[0].strip())

    _known_vocab_cache = known_words
    _vocab_cache_loaded = True
    return known_words


def reset_caches() -> None:
    """
    Clears the memoized known-kanji / known-vocab sets.

    Should be invoked after operations that modify notes (e.g. bulk
    definition generation) so subsequent scoring reflects the new data.
    """
    global _known_kanji_cache, _known_vocab_cache
    global _kanji_cache_loaded, _vocab_cache_loaded

    _known_kanji_cache = set()
    _known_vocab_cache = set()
    _kanji_cache_loaded = False
    _vocab_cache_loaded = False
