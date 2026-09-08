from typing import Dict, List, Optional, Set, Tuple

# Dual-context sibling imports (relative inside Anki's package load,
# absolute in the top-level test harness — see core.py for why).
if __package__:
    from .provider import DictionaryProvider
    from .scoring import is_reference_title, score_definition
    from .utils import extract_clean_word, extract_base_text
    from .models import DictionaryEntry, ScoringResult
else:
    from provider import DictionaryProvider
    from scoring import is_reference_title, score_definition
    from utils import extract_clean_word, extract_base_text
    from models import DictionaryEntry, ScoringResult

# Yomitan fail-safe import — never crash if yomitan.py is missing or aqt stub incomplete
try:
    if __package__:
        from .yomitan import fetch_yomitan_definitions
    else:
        from yomitan import fetch_yomitan_definitions
except Exception:
    fetch_yomitan_definitions = None  # type: ignore


def _get_dictionary_source() -> str:
    """Reads the user's chosen dictionary source from config."""
    try:
        from aqt import mw  # type: ignore
        if mw and hasattr(mw, "addonManager"):
            try:
                name = mw.addonManager.addonFromModule(__name__)
            except Exception:
                name = None
            if not name:
                name = "1619602654"
            cfg = mw.addonManager.getConfig(name)
            if isinstance(cfg, dict):
                src = str(cfg.get("dictionary_source") or "local").strip().lower()
                if src in ("yomitan", "yomitan_api", "api"):
                    return "yomitan"
    except Exception:
        pass
    return "local"


def _is_plain_text_mode() -> bool:
    """Reads the GUI toggle 'plain_text_definitions' from add-on config.

    In headless/test environments (no mw) returns False so existing
    regression tests keep expecting HTML.
    """
    try:
        from aqt import mw  # type: ignore
        if mw is None or not hasattr(mw, "addonManager"):
            return False
        try:
            name = mw.addonManager.addonFromModule(__name__)
        except Exception:
            name = None
        if not name:
            name = "1619602654"
        cfg = mw.addonManager.getConfig(name)
        if isinstance(cfg, dict) and cfg.get("plain_text_definitions"):
            return True
        return False
    except Exception:
        return False


def _to_plain_text(html_or_text: str) -> str:
    """Converts a stored definition (HTML or plain) to plain text.

    Uses extract_base_text which strips <rt>/<rp>/tags and unescapes
    entities. If the stored value is already plain, it is returned (stripped)
    unchanged. This is the cheap O(def_len) path for plain mode — no HTML
    was generated at generation time; existing HTML entries are just stripped.
    """
    if not html_or_text:
        return ""
    # Heuristic: if it looks like HTML, strip it; else just strip.
    if "<" in html_or_text and ">" in html_or_text:
        return extract_base_text(html_or_text)
    return html_or_text.strip()


def _filter_valid_entries(entries: List[DictionaryEntry]) -> List[DictionaryEntry]:
    """Reference-title filter shared by every scoring path.

    Cross-reference titles and pipe-separated child-entry lists are not
    readable definitions (the 会社 incident) — drop them unless they are
    the ONLY candidate, in which case a lone entry is still allowed.
    """
    non_ref = [e for e in entries if not is_reference_title(e.definition)]
    return non_ref if non_ref else (entries if len(entries) == 1 else [])


def _pick_best(entries: List[DictionaryEntry],
               kanji_points: Dict[str, float],
               vocab_points: Dict[str, float]) -> Optional[Tuple[ScoringResult, str]]:
    """v1.2 argmax picker over candidate definitions.

    Scores every candidate with score_definition (interval-weighted
    kanji + vocab points) and returns the winner by:
      1. highest total_score (kanji pts + vocab pts),
      2. tie-break: most kanji occurrences (kanji_count).
    The old early-exit ("first 100% known wins") is gone — with weighted
    scores, a dictionary-order fluke must never beat a strictly better
    definition (the 不公平 case: 小学館 won by order, not quality).
    Returns (best_result, best_definition) or None when empty.
    """
    best: Optional[Tuple[ScoringResult, str]] = None
    for entry in entries:
        res = score_definition(entry.definition, kanji_points, vocab_points)
        if best is None:
            best = (res, entry.definition)
            continue
        cur = best[0]
        # Strictly better total, or equal total with more kanji (tie-break).
        if (res.total_score > cur.total_score
                or (res.total_score == cur.total_score
                    and res.kanji_count > cur.kanji_count)):
            best = (res, entry.definition)
    return best


class DefinitionGenerator:
    """
    Orchestrates the definition generation process.

    v1.2 algorithm: all candidates from all dictionaries are scored with
    interval-weighted kanji + vocab points; the single best definition
    wins (ties break toward more kanji). The ladder ORDER still matters
    as the source enumeration, but selection is pure argmax — the user
    reads the definition they can best understand, regardless of which
    dictionary it came from.
    """

    def __init__(self, provider: DictionaryProvider,
                 known_kanji: Set[str],
                 kanji_points: Optional[Dict[str, float]] = None,
                 vocab_points: Optional[Dict[str, float]] = None):
        self.provider = provider
        self.known_kanji = known_kanji
        # Interval-weighted knowledge (v1.2). When not provided, derive
        # a binary view from known_kanji so legacy callers/tests keep
        # working identically to the old scorer.
        self.kanji_points: Dict[str, float] = (
            kanji_points if kanji_points is not None
            else {k: 1.0 for k in known_kanji}
        )
        self.vocab_points: Dict[str, float] = vocab_points or {}

    def generate(
        self,
        target_word: str,
        ladder_paths: List[str],
        reading: str = "",
        plain_text: Optional[bool] = None,
    ) -> Optional[str]:
        """
        Core generation algorithm (v1.2):
        1. Gather candidates from the Yomitan source (if selected) or
           walk the local ladder collecting EVERY dictionary's entries.
        2. Drop reference titles / child-entry lists.
        3. Score all candidates: kanji pts + vocab pts (ivl/365, cap 1).
        4. Return the highest total; ties break to most kanji count.

        When plain_text is True (or config plain_text_definitions is
        enabled) the returned definition is plain text (no HTML) —
        converted via extract_base_text if the stored entry was HTML,
        otherwise returned as-is.
        """
        word = extract_clean_word(target_word)
        if not word:
            return None

        # Resolve plain-text mode: explicit arg wins, else config.
        if plain_text is None:
            plain_text = _is_plain_text_mode()

        def _finalize(definition: str) -> str:
            if plain_text:
                # No HTML generation here; just strip if needed.
                return _to_plain_text(definition)
            return definition

        # If user selected Yomitan as primary source, bypass local ladder
        # entirely and query Yomitan directly (single fetch, then score).
        if _get_dictionary_source() == "yomitan":
            if fetch_yomitan_definitions is not None:
                try:
                    y_entries = fetch_yomitan_definitions(word, reading)
                except Exception:
                    y_entries = []
                if y_entries:
                    valid = _filter_valid_entries(y_entries)
                    picked = _pick_best(valid, self.kanji_points,
                                         self.vocab_points)
                    if picked is not None:
                        return _finalize(picked[1])
            return None

        # Local ladder: collect ALL candidates, then argmax (v1.2).
        all_candidates: List[DictionaryEntry] = []
        for path in ladder_paths:
            if hasattr(self.provider, 'lookup_by_path'):
                entries = self.provider.lookup_by_path(path, word, reading)
            else:
                entries = self.provider.lookup(word, reading)
            if entries:
                all_candidates.extend(_filter_valid_entries(entries))

        picked = _pick_best(all_candidates, self.kanji_points,
                            self.vocab_points)
        if picked is not None:
            return _finalize(picked[1])

        # ------------------------------------------------------------------
        # Fail-safe: if local ladder produced nothing, try Yomitan API.
        # Minimal, no GUI toggle — if the user has Yomitan running with
        # dictionaries, we borrow them instead of returning None. All local
        # dictionaries still take precedence; this only fires when CompreDef
        # has zero candidates. Users can manually set "yomitan_fallback": false
        # in config.json to disable.
        # ------------------------------------------------------------------
        def _yomitan_enabled() -> bool:
            try:
                from aqt import mw  # type: ignore
                if mw and hasattr(mw, "addonManager"):
                    try:
                        name = mw.addonManager.addonFromModule(__name__)
                    except Exception:
                        name = None
                    if not name:
                        name = "1619602654"
                    cfg = mw.addonManager.getConfig(name)
                    if isinstance(cfg, dict) and cfg.get("yomitan_fallback") is False:
                        return False
            except Exception:
                pass
            return True

        if fetch_yomitan_definitions is not None and _yomitan_enabled():
            try:
                y_entries = fetch_yomitan_definitions(word, reading)
            except Exception:
                y_entries = []
            if y_entries:
                valid_y = _filter_valid_entries(y_entries)
                picked_y = _pick_best(valid_y, self.kanji_points,
                                       self.vocab_points)
                if picked_y is not None:
                    return _finalize(picked_y[1])

        return None
