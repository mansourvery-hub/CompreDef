"""
db_utils.py - Anki database interaction utilities for CompreDef.

Provides safe, read-only access to Anki's database for analyzing known
vocabulary and kanji, following the "Kanji Grid" approach by using the
native `mw.col.db` wrapper instead of direct SQL access (per AGENTS.md
rule 2: never open collection.anki2 with an external sqlite3 connection).

Performance notes:
- `SELECT DISTINCT` on the flds text blob forces SQLite to hash/compare
  every huge field blob, which pegs the CPU on large collections. We
  instead select note ids (cheap integers) and dedupe in Python.
- Kanji extraction uses a compiled regex instead of a per-character
  Python loop (orders of magnitude faster on megabytes of text).
- Results are memoized; call `reset_caches()` after bulk note edits.
"""

import re
from aqt import mw
from typing import Set, Tuple

# Matches a single kanji character in the CJK Unified Ideographs block
# (U+4E00-U+9FFF). Compiled once at import time for speed.
_KANJI_RE = re.compile(r'[\u4e00-\u9fff]')

# Notes store fields joined by the unit-separator byte \x1f
_FIELD_SEP = '\x1f'

# Module-level caches. Scanning the whole collection is expensive, so the
# results are memoized. `reset_caches()` must be called if the collection
# changes (e.g. after bulk note edits) so scores stay accurate.
_known_kanji_cache: Set[str] = set()
_known_vocab_cache: Set[str] = set()
_caches_loaded: bool = False


def _fetch_learned_note_fields() -> list:
    """
    Fetches the field blob of every note that has at least one 'Mature' card
    with interval >= 21 days (indicating long-term retention).

    In Anki statistics, cards with an interval of 21 days or longer are
    classified as Mature. Filtering for ivl >= 21 ensures that only kanji
    and vocabulary the user has genuinely retained long-term are counted
    as known in the matrix.

    Selects note ids + flds and relies on the JOIN to provide only notes
    with mature cards.
    """
    try:
        if not mw or not mw.col:
            return []

        # Kanji Grid-style query: join notes with their cards and keep only
        # material on mature cards (interval >= 21 days for long-term retention).
        query = """
        SELECT notes.id, notes.flds
        FROM notes
        JOIN cards ON notes.id = cards.nid
        WHERE cards.ivl >= 21
        """
        return mw.col.db.all(query) or []
    except Exception as e:
        print(f"CompreDef: DB error while fetching learned notes: {e}")
        return []


def _build_caches() -> None:
    """
    Single-pass scan building both the known-kanji set and the known-vocab
    set from the learned notes. Runs once, then both sets are cached.
    """
    global _known_kanji_cache, _known_vocab_cache, _caches_loaded

    if _caches_loaded:
        return

    known_kanji: Set[str] = set()
    known_words: Set[str] = set()

    for row in _fetch_learned_note_fields():
        if not row or len(row) < 2:
            continue

        _, field_blob = row
        if not field_blob:
            continue

        # Kanji extraction via compiled regex (fast C-level scan)
        known_kanji.update(_KANJI_RE.findall(field_blob))

        # First field is conventionally the expression/word field
        first_field = field_blob.split(_FIELD_SEP, 1)[0].strip()
        if first_field:
            known_words.add(first_field)

    _known_kanji_cache = known_kanji
    _known_vocab_cache = known_words
    _caches_loaded = True


def get_known_kanji_set() -> Set[str]:
    """
    Scans the Anki database for all kanji present on mature cards (interval >= 21).

    Returns:
        A set of unique kanji characters considered 'known' through long-term retention.
    """
    _build_caches()
    return _known_kanji_cache


def get_known_vocabulary_set() -> Set[str]:
    """
    Scans the Anki database for all vocabulary (words) present on mature cards
    with an interval >= 21 days.

    The first field of each note is treated as the word/expression field
    (the standard convention for Japanese note types).

    Returns:
        A set of unique vocabulary words considered 'known'.
    """
    _build_caches()
    return _known_vocab_cache


def reset_caches() -> None:
    """
    Clears the memoized known-kanji / known-vocab sets.

    Should be invoked after operations that modify notes (e.g. bulk
    definition generation) so subsequent scoring reflects the new data.
    """
    global _known_kanji_cache, _known_vocab_cache, _caches_loaded

    _known_kanji_cache = set()
    _known_vocab_cache = set()
    _caches_loaded = False
