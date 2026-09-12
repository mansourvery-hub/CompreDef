import re
from html.parser import HTMLParser
from typing import Dict, Iterable, Set

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

    Deliberately NOT a morphological analyzer (MeCab is an explicit
    non-goal: heavy native dependency inside Anki, version drift).
    Consequences, accepted: runs can glue adjacent compounds when no
    kana/punctuation separates them (rare in prose; synonym lists are
    stripped before this ever runs), and okurigana splits inflections
    (偏る → 偏, correctly kept OUT of vocab — inflection-robust by
    construction). Deterministic on every machine, which a MeCab
    version never is.
    """
    if not text:
        return set()
    return {
        m.group(0)
        for m in re.finditer(r'[\u4e00-\u9fff]+', text)
        if len(m.group(0)) >= 2
    }


class _BoilerplateStripper(HTMLParser):
    """Drops dictionary boilerplate elements for SCORING ONLY.

    Display keeps the full rich HTML (synonym lists are useful to
    read); scoring must not reward or punish them:
    - thesaurus sections: <div data-sc-href="$c-ruigo">…</div>
      (the 類語 synonym chains — dozens of compounds that say nothing
      about how readable the explanation is);
    - part-of-speech tags: any element with data-sc-hinshi
      (〘名〙 and friends — grammatical labels, not prose).
    Depth-counted so nested markup can never leak residue; anything
    without these markers passes through byte-identical. Plain-text
    POS labels (三省堂's ｟名・ダナ｠) carry no tags and are accepted
    noise (1–2 kanji, bounded effect — documented, not stripped,
    because tag-less stripping would eat real prose).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        # Stack depth (len of _stack) to pop back to before emitting
        # again; None = not dropping. The stack itself is ALWAYS
        # maintained so unbalanced markup can never corrupt tracking.
        self._drop_until: int | None = None
        self._stack: list = []
        self._out: list = []

    def _is_boilerplate(self, attrs: list) -> bool:
        attr_map = dict(attrs)
        return (attr_map.get("data-sc-href") == "$c-ruigo"
                or "data-sc-hinshi" in attr_map)

    def handle_starttag(self, tag: str, attrs: list) -> None:
        self._stack.append((tag, attrs))
        if self._drop_until is not None:
            return
        if self._is_boilerplate(attrs):
            if "data-sc-hinshi" in dict(attrs):
                # Part-of-speech tag: drop just this element.
                self._drop_until = len(self._stack)
            else:
                # Thesaurus marker ($c-ruigo) sits on an inner label
                # span — the section is the nearest enclosing div.
                # No enclosing div: drop just the marked element.
                self._drop_until = len(self._stack)
                for depth, (open_tag, _) in enumerate(self._stack):
                    if open_tag == "div":
                        self._drop_until = depth
            return
        self._out.append(self.get_starttag_text())

    def handle_endtag(self, tag: str) -> None:
        if self._stack:
            self._stack.pop()
        if self._drop_until is not None:
            if len(self._stack) <= self._drop_until:
                self._drop_until = None
            return
        self._out.append(f"</{tag}>")

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        if self._drop_until is None and not self._is_boilerplate(attrs):
            self._out.append(self.get_starttag_text())

    def handle_data(self, data: str) -> None:
        if self._drop_until is None:
            self._out.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._drop_until is None:
            self._out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._drop_until is None:
            self._out.append(f"&#{name};")

    def result(self) -> str:
        return "".join(self._out)


def strip_scoring_boilerplate(html_text: str) -> str:
    """Removes boilerplate sections from a definition's HTML (scoring input).

    Never raises: on any parse surprise the input is returned unchanged
    (scoring must degrade to un-stripped, never to empty).
    """
    if not html_text or "<" not in html_text:
        return html_text
    try:
        stripper = _BoilerplateStripper()
        stripper.feed(html_text)
        stripper.close()
        return stripper.result()
    except Exception:
        return html_text


def remove_excluded_terms(text: str, exclude: Iterable[str] = ()) -> str:
    """Removes headword self-mentions from scoring text.

    A definition that repeats the defined word (不公平 inside 不公平's
    own entry, header included) must not earn points for it — the
    learner looked the word up precisely because it is unknown.
    Only multi-character terms are removed: excluding a single kanji
    would wipe every occurrence of a common character. Substring
    removal can split a longer compound (documented, rare — true word
    boundaries need a morphological analyzer, an explicit non-goal).
    """
    if not text or not exclude:
        return text
    for term in exclude:
        if term and isinstance(term, str) and len(term) >= 2:
            text = text.replace(term, "")
    return text


def scoring_base_text(html_or_text: str,
                       exclude: Iterable[str] = ()) -> str:
    """The exact text scoring runs on: boilerplate stripped, base text
    extracted, headword self-mentions removed. Shared by kanji AND
    vocab extraction so both always agree on what counts."""
    return remove_excluded_terms(
        extract_base_text(strip_scoring_boilerplate(html_or_text)),
        exclude)


def score_definition(
    html_or_text: str,
    kanji_points: Dict[str, float],
    vocab_points: Dict[str, float],
    exclude: Iterable[str] = (),
) -> ScoringResult:
    """
    v1.2 definition scorer: interval-weighted kanji + vocab points.

    For a definition's clean base text:
    - kanji score  = sum(point(k) for each kanji occurrence), where
      point(k) = max_interval(k)/365 capped at 1.0 (0 when unknown).
    - vocab score  = sum(point(w) for each DISTINCT multi-kanji
      compound), same weighting. Compounds unknown to the learner
      contribute 0.
    - total = kanji_score + vocab_score (raw sums; kept for
      compatibility and diagnostics).

    v1.3 densities (what the picker actually ranks by — raw sums grow
    with length, so a 20-paragraph 5%-known definition always beat a
    succinct 90%-known one):
    - kanji_density = kanji_score / kanji_count (fraction known, 0..1),
    - vocab_density = vocab_score / distinct-compound count
      (0.0 when the definition holds no compounds),
    - density_total = kanji_density + vocab_density.

    Scoring input (see scoring_base_text): boilerplate sections
    (thesaurus lists, POS tags) are stripped, then every `exclude`
    surface form (the defined headword — looking a word up proves it
    unknown) is removed, and only then are kanji/compounds counted.

    Kept for compatibility: `score` (normalized 0..1 over the kanji
    count) and `is_perfect` so legacy call sites keep working.
    """
    clean_text = scoring_base_text(html_or_text, exclude)
    kanji_in_text = _KANJI_RE.findall(clean_text)

    kanji_score = 0.0
    for ch in kanji_in_text:
        kanji_score += float(kanji_points.get(ch, 0.0))

    compounds = extract_kanji_words(clean_text)
    vocab_score = 0.0
    for word in compounds:
        vocab_score += float(vocab_points.get(word, 0.0))

    kanji_count = len(kanji_in_text)
    total = kanji_score + vocab_score
    kanji_density = kanji_score / kanji_count if kanji_count else 0.0
    vocab_density = vocab_score / len(compounds) if compounds else 0.0

    if kanji_count == 0:
        # Kana-only definitions carry no kanji signal: neutral score.
        return ScoringResult(definition=html_or_text, score=1.0,
                             is_perfect=True, kanji_score=0.0,
                             vocab_score=0.0, total_score=0.0,
                             kanji_count=0, kanji_density=0.0,
                             vocab_density=0.0, density_total=0.0)

    return ScoringResult(
        definition=html_or_text,
        score=kanji_score / kanji_count,  # normalized legacy view
        is_perfect=(kanji_score == kanji_count),  # every kanji fully mature
        kanji_score=kanji_score,
        vocab_score=vocab_score,
        total_score=total,
        kanji_count=kanji_count,
        kanji_density=kanji_density,
        vocab_density=vocab_density,
        density_total=kanji_density + vocab_density,
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
                             kanji_count=0, kanji_density=0.0,
                             vocab_density=0.0, density_total=0.0)

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
        kanji_density=score,
        vocab_density=0.0,
        density_total=score,
    )
