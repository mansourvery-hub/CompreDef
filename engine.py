import os
from typing import List, Optional, Set

# Dual-context sibling imports (relative inside Anki's package load,
# absolute in the top-level test harness — see core.py for why).
if __package__:
    from .provider import DictionaryProvider
    from .scoring import calculate_kanji_score, is_reference_title
    from .utils import extract_clean_word, extract_base_text
    from .models import DictionaryEntry
else:
    from provider import DictionaryProvider
    from scoring import calculate_kanji_score, is_reference_title
    from utils import extract_clean_word, extract_base_text
    from models import DictionaryEntry


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

class DefinitionGenerator:
    """
    Orchestrates the definition generation process.
    Separates the 'Ladder' algorithm from the 'Provider' (data access).
    """
    def __init__(self, provider: DictionaryProvider, known_kanji: Set[str]):
        self.provider = provider
        self.known_kanji = known_kanji

    def generate(
        self,
        target_word: str,
        ladder_paths: List[str],
        reading: str = "",
        plain_text: Optional[bool] = None,
    ) -> Optional[str]:
        """
        Core Dictionary Ladder algorithm:
        1. Walk dictionaries in order.
        2. Early Exit: if a definition is 100% known, return it immediately.
        3. Fallback: return the highest scoring definition across all dictionaries.

        When plain_text is True (or config plain_text_definitions is enabled)
        the returned definition is plain text (no HTML) — converted via
        extract_base_text if the stored entry was HTML, otherwise returned as-is.
        This avoids HTML post-processing cost when plain entries were already
        stored, and falls back to cheap stripping for legacy HTML caches.
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

        best_definition: Optional[str] = None
        best_score: float = -1.0

        for path in ladder_paths:
            if hasattr(self.provider, 'lookup_by_path'):
                entries = self.provider.lookup_by_path(path, word, reading)
            else:
                entries = self.provider.lookup(word, reading)

            if not entries:
                continue

            non_ref = [e for e in entries if not is_reference_title(e.definition)]
            valid = non_ref if non_ref else (entries if len(entries) == 1 else [])
            
            if not valid:
                continue

            for entry in valid:
                res = calculate_kanji_score(entry.definition, self.known_kanji)
                
                if res.is_perfect:
                    return _finalize(res.definition)
                
                if res.score > best_score:
                    best_score = res.score
                    best_definition = res.definition

        if best_definition is not None and plain_text:
            return _to_plain_text(best_definition)
        return best_definition
