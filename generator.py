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
"""

from typing import List, Optional, Union
from .db_utils import get_known_kanji_set
from .parser import (
    get_single_dictionary,
    find_dictionary_folders,
    SingleDictionary,
)


def _is_reference_title(text: str) -> bool:
    """
    Detects entries that are just cross-reference titles (e.g. "会社更生法")
    rather than actual definitions.

    Heuristic: short text (under 10 chars) with no sentence punctuation
    (。、) is almost always a "see also" headword, not a definition.
    """
    if len(text) >= 10:
        return False
    return not any(p in text for p in ("。", "、", "：", "，"))


def _calculate_kanji_score(text: str, known_kanji: set) -> float:
    """
    Calculates kanji comprehension score: known kanji / total kanji.

    A definition with no kanji at all is treated as fully comprehensible (1.0).
    """
    kanji_in_text = [char for char in text if '\u4e00' <= char <= '\u9fff']

    if not kanji_in_text:
        return 1.0

    known_count = sum(1 for char in kanji_in_text if char in known_kanji)
    return known_count / len(kanji_in_text)


def generate_definition(
    target_word: str,
    mode: str = "",
    dictionary_folder: str = "",
    dictionaries: Optional[List[str]] = None,
) -> str:
    """
    Generates a definition for `target_word` following the Dictionary Ladder algorithm.

    Args:
        target_word: The Japanese word/expression to look up.
        mode: Kept for backwards compatibility (ignored, LLM removed).
        dictionary_folder: Fallback single folder path.
        dictionaries: Ordered list of dictionary directory paths from user config.

    Returns:
        The chosen definition string.
    """
    # Resolve ordered list of dictionary paths
    ladder_paths: List[str] = []

    if dictionaries and isinstance(dictionaries, list):
        ladder_paths = [str(p).strip() for p in dictionaries if p and str(p).strip()]

    # Fallback to dictionary_folder if no explicit list was provided
    if not ladder_paths and dictionary_folder:
        ladder_paths = find_dictionary_folders(dictionary_folder)

    if not ladder_paths:
        return f"No dictionary configured for lookup of: {target_word}"

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

            # EARLY EXIT: If 100% of kanji in this definition are known,
            # select it immediately and stop searching further dictionaries.
            # This delivers the simplest level-appropriate definition and saves CPU.
            if score >= 1.0:
                print(
                    f"CompreDef: Early exit at '{dict_obj.title}' "
                    f"(100% kanji match) for '{target_word}'"
                )
                return definition

            # Keep track of the maximal definition (highest score / least unknown kanji)
            if score > best_score:
                best_score = score
                best_definition = definition

    # If no definition had 100% known kanji, return the maximal (least complicated) definition
    if best_definition:
        return best_definition

    return f"No definition found for: {target_word}"
