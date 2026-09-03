import re
from typing import Set

# Dual-context sibling imports (relative inside Anki's package load,
# absolute in the top-level test harness — see core.py for why).
if __package__:
    from .utils import extract_base_text
    from .models import ScoringResult
else:
    from utils import extract_base_text
    from models import ScoringResult

_KANJI_RE = re.compile(r'[\u4e00-\u9fff]')

def is_reference_title(html_or_text: str) -> bool:
    """
    Detects entries that are just cross-reference titles.
    Heuristic: visible text under 10 chars with no sentence punctuation is a reference.
    """
    clean_text = extract_base_text(html_or_text)
    if len(clean_text) >= 10:
        return False
    return not any(p in clean_text for p in ("。", "、", "：", "，"))

def calculate_kanji_score(html_or_text: str, known_kanji: Set[str]) -> ScoringResult:
    """
    Calculates kanji comprehension score: known base kanji / total base kanji.
    """
    clean_text = extract_base_text(html_or_text)
    kanji_in_text = _KANJI_RE.findall(clean_text)

    if not kanji_in_text:
        return ScoringResult(definition=html_or_text, score=1.0, is_perfect=True)

    known_count = sum(1 for char in kanji_in_text if char in known_kanji)
    score = known_count / len(kanji_in_text)
    return ScoringResult(
        definition=html_or_text,
        score=score,
        is_perfect=(score >= 1.0)
    )
