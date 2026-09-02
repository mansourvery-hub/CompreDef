"""
generator.py - Definition generation logic for CompreDef.

Implements the Dictionary Ladder with Kanji Matrix Scoring:
- Walks dictionaries in the exact order configured by the user in the GUI
  (e.g., Children's -> Standard -> Advanced).
- Early Exit: If any definition in the current dictionary has 100% known
  kanji, it is immediately selected and returned, terminating the loop.
  This saves massive CPU time and ensures beginners receive simpler definitions.
- Maximal Fallback: If no dictionary has a 100% known definition, the algorithm
  returns the maximal definition (the definition with the highest kanji
  comprehension score / least complicated) across all installed dictionaries.
- HTML-Aware: Evaluates kanji scoring strictly on base kanji characters
  (stripping <rt> furigana readings so kana readings never pollute kanji counts),
  while returning the rich, fully styled Yomitan HTML for insertion into Anki.
"""

import re
import html
from typing import List, Optional, Union, Set

try:
    from .db_utils import get_known_kanji_set
    from .parser import (
        get_single_dictionary,
        find_dictionary_folders,
        SingleDictionary,
    )
except ImportError:
    from db_utils import get_known_kanji_set
    from parser import (
        get_single_dictionary,
        find_dictionary_folders,
        SingleDictionary,
    )

# Matches kanji in CJK Unified Ideographs block
_KANJI_RE = re.compile(r'[\u4e00-\u9fff]')

# Regular expressions to strip ruby readings and tags for accurate kanji scoring
_RT_RE = re.compile(r'<rt\b[^>]*>.*?</rt>', flags=re.DOTALL | re.IGNORECASE)
_RP_RE = re.compile(r'<rp\b[^>]*>.*?</rp>', flags=re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r'<[^>]+>')


def _extract_base_text(html_or_text: str) -> str:
    """
    Extracts visible base text from HTML, stripping ruby furigana (<rt> tags).
    """
    if not html_or_text:
        return ""
    # Strip furigana reading tags
    no_rt = _RT_RE.sub("", html_or_text)
    no_rp = _RP_RE.sub("", no_rt)
    # Strip remaining HTML tags
    plain = _TAG_RE.sub("", no_rp)
    return html.unescape(plain).strip()


def _is_reference_title(html_or_text: str) -> bool:
    """
    Detects entries that are just cross-reference titles (e.g. "会社更生法")
    rather than actual definitions.

    Heuristic: visible text under 10 chars with no sentence punctuation
    (。、) is almost always a "see also" headword, not a definition.
    """
    clean_text = _extract_base_text(html_or_text)
    if len(clean_text) >= 10:
        return False
    return not any(p in clean_text for p in ("。", "、", "：", "，"))


def _calculate_kanji_score(html_or_text: str, known_kanji: Set[str]) -> float:
    """
    Calculates kanji comprehension score: known base kanji / total base kanji.

    Accurately ignores <rt> furigana readings so only the base kanji the user
    is expected to know are counted.

    A definition with no kanji at all (kana only) returns 1.0 (fully comprehensible).
    """
    clean_text = _extract_base_text(html_or_text)
    kanji_in_text = _KANJI_RE.findall(clean_text)

    if not kanji_in_text:
        return 1.0

    known_count = sum(1 for char in kanji_in_text if char in known_kanji)
    return known_count / len(kanji_in_text)


def generate_definition(
    target_word: str,
    mode: str = "",
    dictionary_folder: str = "",
    dictionaries: Optional[List[str]] = None,
) -> Optional[str]:
    """
    Generates a definition for `target_word` following the Dictionary Ladder algorithm.

    Args:
        target_word: The Japanese word/expression to look up.
        mode: Kept for backwards compatibility (ignored, LLM removed).
        dictionary_folder: Fallback single folder or zip path.
        dictionaries: Ordered list of dictionary directory/zip paths from user config.

    Returns:
        The chosen rich Yomitan HTML definition string, or None if not found.
    """
    ladder_paths: List[str] = []

    if dictionaries and isinstance(dictionaries, list):
        ladder_paths = [str(p).strip() for p in dictionaries if p and str(p).strip()]

    # Fallback to dictionary_folder if no explicit list was provided
    if not ladder_paths and dictionary_folder:
        ladder_paths = find_dictionary_folders(dictionary_folder)

    if not ladder_paths:
        return None

    known_kanji = get_known_kanji_set()

    best_definition: Optional[str] = None
    best_score: float = -1.0

    # Step through each dictionary rung in configured order
    for dict_path in ladder_paths:
        dict_obj: SingleDictionary = get_single_dictionary(dict_path)
        raw_defs: List[str] = dict_obj.lookup(target_word)

        # Filter out reference titles
        valid_defs = [d for d in raw_defs if not _is_reference_title(d)]
        if not valid_defs:
            continue

        for definition in valid_defs:
            score = _calculate_kanji_score(definition, known_kanji)

            # EARLY EXIT: If 100% of kanji in this definition are known (on mature cards),
            # select it immediately and stop searching further dictionaries.
            # This delivers the simplest level-appropriate definition and saves CPU.
            if score >= 1.0:
                print(
                    f"CompreDef: Early exit at '{dict_obj.title}' "
                    f"(100% mature kanji match) for '{target_word}'"
                )
                return definition

            # Keep track of the maximal definition (highest score / least unknown kanji)
            if score > best_score:
                best_score = score
                best_definition = definition

    # If no definition had 100% known kanji, return the maximal (least complicated) definition
    if best_definition:
        return best_definition

    # No definition found in any installed dictionary
    return None
