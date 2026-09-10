from dataclasses import dataclass

# NOTE: there is intentionally NO RENDERER_VERSION here. The single
# source of truth lives on provider.LocalSQLiteProvider (used by
# _compute_signature); parser.py re-exports it by reference. A second
# literal copy here once existed and could silently desync — removed
# (ruff-driven cleanup).

@dataclass(frozen=True)
class DictionaryEntry:
    """Represents a single definition entry from a dictionary."""
    word: str
    reading: str
    definition: str
    dictionary_title: str
    dictionary_path: str

@dataclass(frozen=True)
class ScoringResult:
    """The result of scoring a definition.

    v1.2 scoring: kanji_score + vocab_score are interval-weighted
    mastery points (ivl/365 capped at 1.0); total_score is their sum.
    The engine ranks by total_score, tie-broken by kanji_count (most
    kanji wins). `score`/`is_perfect` remain as the legacy normalized
    view for compatibility.
    """
    definition: str
    score: float
    is_perfect: bool
    kanji_score: float = 0.0
    vocab_score: float = 0.0
    total_score: float = 0.0
    kanji_count: int = 0
