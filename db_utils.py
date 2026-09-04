"""
db_utils.py - Deprecated compatibility shim for CompreDef.

The learner-knowledge logic (known kanji / known vocabulary) used to live
here. That implementation has been retired: the single source of truth is
now ``anki.py``, which extracts knowledge from the FIRST field of every
mature note across all note types (never definitions, examples, or other
fields) and holds it as a per-session snapshot.

This module is kept so any lingering ``import db_utils`` keeps working;
every name is re-exported from ``anki``. Do not add new logic here.
"""

# Dual-context sibling imports (relative inside Anki's package load,
# absolute in the top-level test harness — see core.py for why).
if __package__:
    from .anki import (
        get_known_kanji_set,
        get_known_vocabulary_set,
        reset_caches,
        init_caches_async,
    )
else:
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
