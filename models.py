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

    v1.3 scoring (default, see picker.py): length-normalized DENSITIES.
    kanji_density = kanji_score / kanji_count (fraction of the
    definition's kanji the learner knows, 0..1); vocab_density =
    vocab_score / distinct-compound count (0 when there are none);
    density_total = their sum. The picker ranks by density_total, so a
    succinct 90%-known definition beats a 20-paragraph 5%-known one —
    raw sums always favored length. Kana-only definitions carry no
    kanji signal: density_total is 0.0 (neutral, never wins on merit).
    """
    definition: str
    score: float
    is_perfect: bool
    kanji_score: float = 0.0
    vocab_score: float = 0.0
    total_score: float = 0.0
    kanji_count: int = 0
    kanji_density: float = 0.0
    vocab_density: float = 0.0
    density_total: float = 0.0
