from typing import Dict, List, Optional, Set, Tuple

# Dual-context sibling imports (relative inside Anki's package load,
# absolute in the top-level test harness — see core.py for why).
if __package__:
    from .provider import DictionaryProvider
    from .picker import collect_dictionary_candidates, filter_valid_entries, pick_best
    from .utils import extract_clean_word, extract_base_text
    from .models import DictionaryEntry, ScoringResult
else:
    from provider import DictionaryProvider
    from picker import collect_dictionary_candidates, filter_valid_entries, pick_best
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

    Thin delegate to picker.filter_valid_entries (kept here so existing
    callers and tests keep working; the picker module owns the logic).
    """
    return filter_valid_entries(entries)


def _pick_best(entries: List[DictionaryEntry],
               kanji_points: Dict[str, float],
               vocab_points: Dict[str, float]) -> Optional[Tuple[ScoringResult, str]]:
    """Active-strategy argmax picker over candidate definitions.

    Thin delegate to picker.pick_best (kept here so existing callers
    and tests keep working; the picker module owns the ranking).
    """
    return pick_best(entries, kanji_points, vocab_points)


def _collect_local_candidates(provider, dictionary_paths: List[str],
                               word: str, reading: str = "") -> list:
    """Walks the local dictionaries with the given provider — never raises.

    Thin delegate to picker.collect_dictionary_candidates (kept here so
    existing callers keep working; the picker module owns gathering).
    """
    return collect_dictionary_candidates(provider, dictionary_paths, word, reading)


class DefinitionGenerator:
    """
    Orchestrates the definition generation process.

    All candidates from all dictionaries are scored with
    interval-weighted density points; the single best definition
    wins (ties break toward more kanji). Selection is pure argmax —
    the user reads the definition they can best understand,
    regardless of which dictionary it came from.
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
        dictionary_paths: List[str],
        reading: str = "",
        plain_text: Optional[bool] = None,
    ) -> Optional[str]:
        """
        Core generation algorithm:
        1. Gather candidates from the Yomitan source (if selected) or
           walk the local dictionaries collecting EVERY entry.
        2. Drop reference titles / child-entry lists.
        3. Score all candidates: comprehension density (ivl/365, cap 1).
        4. Return the highest score; ties break to most kanji count.

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

        # If user selected Yomitan as primary source, query Yomitan
        # directly (single fetch, then score).
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
            # v1.2.15 fail-safe (mirrors the local->Yomitan one below):
            # Yomitan selected but unreachable (browser closed) or with
            # no entry for this word => default to the LOCAL dictionaries
            # instead of returning nothing. Only fires when local
            # dictionaries are actually configured.
            if dictionary_paths:
                local_provider = None
                try:
                    # Lazy import: core imports this module at load, so a
                    # top-level import would be circular.
                    if __package__:
                        from .core import get_local_provider
                    else:
                        from core import get_local_provider  # type: ignore
                    local_provider = get_local_provider()
                except Exception:
                    local_provider = None
                if local_provider is not None:
                    print(f"CompreDef: Yomitan gave nothing for '{word}' — "
                          f"falling back to local dictionaries.")
                    local_candidates = _collect_local_candidates(
                        local_provider, dictionary_paths, word, reading)
                    picked_local = _pick_best(
                        local_candidates, self.kanji_points,
                        self.vocab_points)
                    if picked_local is not None:
                        return _finalize(picked_local[1])
            return None

        # Local dictionaries: collect ALL candidates, then argmax.
        all_candidates = _collect_local_candidates(
            self.provider, dictionary_paths, word, reading)

        picked = _pick_best(all_candidates, self.kanji_points,
                            self.vocab_points)
        if picked is not None:
            return _finalize(picked[1])

        # ------------------------------------------------------------------
        # Fail-safe: if the local dictionaries produced nothing, try Yomitan API.
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
