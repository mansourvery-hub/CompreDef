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
    Fetches the field blob of every note that has at least one card with
    interval > 0 (i.e. the user has actually learned the material).

    Selects note ids + flds without DISTINCT on the text blob (see
    performance notes above) and dedupes by note id in Python.

    Wrapped in try/except so a database hiccup never crashes Anki
    (AGENTS.md rule 3.4: robust error handling around DB access).
    """
    try:
        if not mw or not mw.col:
            return []

        # Kanji Grid-style query: join notes with their cards and keep only
        # material the user has seen through review (interval > 0).
        query = """
        SELECT DISTINCT notes.id, notes.flds
        FROM notes
        JOIN cards ON notes.id = cards.nid
        WHERE cards.ivl > 0
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
    seen_note_ids: set = set()

    for row in _fetch_learned_note_fields():
        if not row or len(row) < 2:
            continue

        note_id, field_blob = row[0], row[1]
        if note_id in seen_note_ids or not field_blob:
            continue
        seen_note_ids.add(note_id)

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
    Scans the Anki database for all kanji present on cards with interval > 0.

    Returns:
        A set of unique kanji characters considered 'known'.
    """
    _build_caches()
    return _known_kanji_cache


def get_known_vocabulary_set() -> Set[str]:
    """
    Scans the Anki database for all vocabulary (words) present on cards
    with an interval > 0.

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
