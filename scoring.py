import re
from typing import Dict, Set

# Dual-context sibling imports (relative inside Anki's package load,
# absolute in the top-level test harness — see core.py for why).
if __package__:
    from .utils import extract_base_text
    from .models import ScoringResult
else:
    from utils import extract_base_text
    from models import ScoringResult

_KANJI_RE = re.compile(r'[\u4e00-\u9fff]')
# Vocab knowledge = multi-kanji compounds only (v1.2 spec: kana-only
# words are inflection-hostile, single kanji already covered by kanji
# points).
_KANJI_WORD_RE = re.compile(r'^[\u4e00-\u9fff]{2,}$')
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


def extract_kanji_words(text: str) -> Set[str]:
    """Extracts the multi-kanji compounds used for vocab scoring.

    Runs on cleaned base text: contiguous kanji runs of length >= 2
    (会社, 不公平). Single kanji are kanji-score territory; runs split
    at any non-kanji character, mirroring how compounds appear inside
    Japanese definitions.
    """
    if not text:
        return set()
    return {
        m.group(0)
        for m in re.finditer(r'[\u4e00-\u9fff]+', text)
        if len(m.group(0)) >= 2
    }


def score_definition(
    html_or_text: str,
    kanji_points: Dict[str, float],
    vocab_points: Dict[str, float],
) -> ScoringResult:
    """
    v1.2 definition scorer: interval-weighted kanji + vocab points.

    For a definition's clean base text:
    - kanji score  = sum(point(k) for each kanji occurrence), where
      point(k) = max_interval(k)/365 capped at 1.0 (0 when unknown).
    - vocab score  = sum(point(w) for each multi-kanji compound
      occurrence), same weighting. Compounds unknown to the learner
      contribute 0.
    - total = kanji_score + vocab_score; the engine picks the highest
      total; ties break toward the definition with the MOST kanji
    (longest, most kanji-dense prose — the user's stated preference).

    Kept for compatibility: `score` (normalized 0..1 over the kanji
    count) and `is_perfect` so legacy call sites keep working; the
    engine now ranks by (total, kanji_count) instead.
    """
    clean_text = extract_base_text(html_or_text)
    kanji_in_text = _KANJI_RE.findall(clean_text)

    kanji_score = 0.0
    for ch in kanji_in_text:
        kanji_score += float(kanji_points.get(ch, 0.0))

    vocab_score = 0.0
    for word in extract_kanji_words(clean_text):
        vocab_score += float(vocab_points.get(word, 0.0))

    kanji_count = len(kanji_in_text)
    total = kanji_score + vocab_score

    if kanji_count == 0:
        # Kana-only definitions carry no kanji signal: neutral score.
        return ScoringResult(definition=html_or_text, score=1.0,
                             is_perfect=True, kanji_score=0.0,
                             vocab_score=0.0, total_score=0.0,
                             kanji_count=0)

    return ScoringResult(
        definition=html_or_text,
        score=kanji_score / kanji_count,  # normalized legacy view
        is_perfect=(kanji_score == kanji_count),  # every kanji fully mature
        kanji_score=kanji_score,
        vocab_score=vocab_score,
        total_score=total,
        kanji_count=kanji_count,
    )


def calculate_kanji_score(html_or_text: str, known_kanji: Set[str]) -> ScoringResult:
    """
    Legacy binary scorer (kept for compatibility + tests): known base
    kanji / total base kanji. New code uses score_definition().
    """
    clean_text = extract_base_text(html_or_text)
    kanji_in_text = _KANJI_RE.findall(clean_text)

    if not kanji_in_text:
        return ScoringResult(definition=html_or_text, score=1.0, is_perfect=True,
                             kanji_score=0.0, vocab_score=0.0, total_score=0.0,
                             kanji_count=0)

    known_count = sum(1 for char in kanji_in_text if char in known_kanji)
    score = known_count / len(kanji_in_text)
    return ScoringResult(
        definition=html_or_text,
        score=score,
        is_perfect=(score >= 1.0),
        kanji_score=float(known_count),
        vocab_score=0.0,
        total_score=float(known_count),
        kanji_count=len(kanji_in_text),
    )
