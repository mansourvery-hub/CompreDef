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
# Pipe separators join child-entry term lists ("(子) 会社員 | 会社組合 | …")
# and never occur in Japanese prose definitions.
_LIST_SEP_RE = re.compile(r'[|｜]')
# Sentence-ending punctuation always means real prose, never a reference.
_SENTENCE_END_RE = re.compile(r'[。？！]')

def is_reference_title(html_or_text: str) -> bool:
    """
    Detects entries that are cross-references, not readable definitions:
    - short titles without punctuation ("会社更生法"), and
    - long pipe-separated child-entry lists ("(子) 会社員 | 会社組合 | …"):
      every kanji in them can be known, so without this they score ~1.0
      and beat the genuine definition (the 会社 incident).
    Any sentence-ending punctuation (。？！) means real prose and always wins.
    """
    clean_text = extract_base_text(html_or_text)
    if _SENTENCE_END_RE.search(clean_text):
        return False
    if len(clean_text) < 10:
        return not any(p in clean_text for p in ("、", "：", "，"))
    return _LIST_SEP_RE.search(clean_text) is not None

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
