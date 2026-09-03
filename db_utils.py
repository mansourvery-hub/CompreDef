"""
db_utils.py - Deprecated compatibility shim for CompreDef.

The learner-knowledge logic (known kanji / known vocabulary) used to live
here and scanned the ENTIRE notes.flds blob, which incorrectly counted
kanji from Definition/Example fields as "known". That implementation has
been retired: the single source of truth is now ``anki.py``, which
extracts knowledge ONLY from the configured Expression (word) field and
holds it as a per-session snapshot.

This module is kept so any lingering ``import db_utils`` keeps working;
every name is re-exported from ``anki``. Do not add new logic here.
"""

from anki import (
    get_known_kanji_set,
    get_known_vocabulary_set,
    reset_caches,
    init_caches_async,
)

__all__ = [
    "get_known_kanji_set",
    "get_known_vocabulary_set",
    "reset_caches",
    "init_caches_async",
]
