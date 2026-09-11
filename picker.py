"""picker.py — the dictionary picker: WHICH definition wins, and why.

Self-contained by design: everything about deciding one definition
out of many lives here (ladder gathering, filtering, scoring
strategy, ranking, picking) and depends ONLY on scoring/models/utils
plus a duck-typed provider — never on engine, providers, Anki, or Qt.
To try a new picking method a week from now, add a PickerStrategy
subclass (and override gathering only if the method needs different
candidates) and point get_active_strategy (or the `picker_strategy`
add-on config key) at it; no other module changes. engine.py keeps
Anki/config wiring (source selection, fallbacks, plain-text mode) and
delegates every decision here.
"""

import abc
from typing import Dict, List, Optional, Tuple

# Dual-context sibling imports (relative inside Anki's package load,
# absolute in the top-level test harness — see core.py for why).
if __package__:
    from .models import DictionaryEntry, ScoringResult
    from .scoring import is_reference_title, score_definition
else:
    from models import DictionaryEntry, ScoringResult
    from scoring import is_reference_title, score_definition


def filter_valid_entries(entries: List[DictionaryEntry]) -> List[DictionaryEntry]:
    """Reference-title filter shared by every picking strategy.

    Cross-reference titles and pipe-separated child-entry lists are not
    readable definitions (the 会社 incident) — drop them unless they are
    the ONLY candidate, in which case a lone entry is still allowed.
    """
    non_ref = [e for e in entries if not is_reference_title(e.definition)]
    return non_ref if non_ref else (entries if len(entries) == 1 else [])


def collect_ladder_candidates(provider, ladder_paths: List[str],
                              word: str, reading: str = "") -> List[DictionaryEntry]:
    """Walks the ladder with the given provider — never raises.

    Shared by the main local path and the Yomitan->local fail-safe so
    Tab and button can never diverge. One corrupt dictionary must not
    kill the whole ladder (or the fallback), so each path is isolated.
    A None provider (headless tests) simply yields no candidates.
    The provider is duck-typed (needs lookup_by_path, like
    LocalSQLiteProvider) so this module never imports providers, Anki,
    or Qt — a future strategy can override gathering without touching
    engine.py. Reference titles are filtered per path, so every
    returned candidate is pickable.
    """
    all_candidates: List[DictionaryEntry] = []
    if provider is None:
        return all_candidates
    for path in ladder_paths or []:
        try:
            if hasattr(provider, 'lookup_by_path'):
                entries = provider.lookup_by_path(path, word, reading)
            else:
                entries = provider.lookup(word, reading)
        except Exception:
            continue
        if entries:
            all_candidates.extend(filter_valid_entries(entries))
    return all_candidates


class PickerStrategy(abc.ABC):
    """One way to order candidate definitions, best first.

    rank_key() must be a TOTAL order over (result, title, definition)
    built from input PROPERTIES only — never input positions — so
    shuffling the ladder can never change the winner (Q-G2
    order-independence). lower key == more comprehensible.
    """

    name: str = "base"

    @abc.abstractmethod
    def rank_key(self, result: ScoringResult, title: str,
                 definition: str) -> tuple:
        """Sort key for one scored candidate (lower wins)."""
        raise NotImplementedError


class DensityPicker(PickerStrategy):
    """Default (v1.3): length-NORMALIZED comprehension density.

    comprehension(D) = kanji_density(D) + vocab_density(D), where
      kanji_density = sum(point(k)) / (# kanji occurrences), 0..1,
      vocab_density = sum(point(w)) / (# distinct compounds), 0..1,
      point(x)      = min(1, max_interval(x) / 365), 0 when unknown.
    Kana-only definitions hold no kanji signal: density 0 (neutral,
    never wins on merit). Ties break toward more kanji (richer prose),
    then (title, text) for a strict total order.

    Why density, not raw sums: raw sums grow with length, so a
    20-paragraph 5%-known definition always beat a succinct 90%-known
    one. Density measures the fraction the learner can actually read.
    """

    name = "density"

    def rank_key(self, result: ScoringResult, title: str,
                 definition: str) -> tuple:
        return (-result.density_total, -result.kanji_count,
                title, definition)


class LegacySumPicker(PickerStrategy):
    """v1.2 behavior: raw interval-weighted sums (no normalization).

    Kept so the density change stays COMPARABLE — set
    `"picker_strategy": "legacy_sum"` in config.json to A/B the old
    ranking (longest known-kanji mass wins). Not the default: it
    systematically favors length over readability.
    """

    name = "legacy_sum"

    def rank_key(self, result: ScoringResult, title: str,
                 definition: str) -> tuple:
        return (-result.total_score, -result.kanji_count,
                title, definition)


_STRATEGIES: Dict[str, PickerStrategy] = {
    DensityPicker.name: DensityPicker(),
    LegacySumPicker.name: LegacySumPicker(),
}


def get_active_strategy() -> PickerStrategy:
    """The strategy driving picks right now (default: density).

    Reads the hand-editable `picker_strategy` add-on config key so a
    new method can be trialed without code changes; unknown values and
    headless/test environments (no mw) fall back to density LOUDLY via
    print (a silent fallback to a different ranking would corrupt
    grading comparisons).
    """
    try:
        from aqt import mw  # type: ignore
        if mw is not None and hasattr(mw, "addonManager"):
            try:
                name = mw.addonManager.addonFromModule(__name__)
            except Exception:
                name = None
            cfg = mw.addonManager.getConfig(name or "1619602654")
            if isinstance(cfg, dict):
                key = str(cfg.get("picker_strategy") or "density")
                if key in _STRATEGIES:
                    return _STRATEGIES[key]
                print(f"CompreDef: unknown picker_strategy {key!r} — "
                      f"using 'density'.")
    except Exception:
        pass
    return _STRATEGIES["density"]


def rank_definitions(
    candidates: List[Tuple[str, str]],
    kanji_points: Dict[str, float],
    vocab_points: Dict[str, float],
    strategy: Optional[PickerStrategy] = None,
) -> List[Tuple[Tuple[str, str], ScoringResult]]:
    """Deterministic TOTAL order over candidates, best first.

    Scores each (dictionary_title, definition) with score_definition
    and sorts by the strategy key (default: the active strategy).
    Returns [((title, definition), ScoringResult), ...].
    """
    active = strategy or get_active_strategy()
    scored = [((title, definition),
               score_definition(definition, kanji_points, vocab_points))
              for title, definition in candidates]
    scored.sort(key=lambda item: active.rank_key(
        item[1], item[0][0], item[0][1]))
    return scored


def pick_best(entries: List[DictionaryEntry],
              kanji_points: Dict[str, float],
              vocab_points: Dict[str, float],
              strategy: Optional[PickerStrategy] = None,
              ) -> Optional[Tuple[ScoringResult, str]]:
    """The single winning definition, or None when there is no entry.

    Single-pass argmax using the same strategy key as rank_definitions,
    so rank()[0] always agrees with this pick.
    """
    active = strategy or get_active_strategy()
    best: Optional[Tuple[tuple, ScoringResult, str]] = None
    for entry in entries:
        res = score_definition(entry.definition, kanji_points, vocab_points)
        key = active.rank_key(res, entry.dictionary_title, entry.definition)
        if best is None or key < best[0]:
            best = (key, res, entry.definition)
    if best is None:
        return None
    return (best[1], best[2])
