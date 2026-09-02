"""
generator.py - Definition generation logic for CompreDef.

Implements Mode A (Dictionary Ladder) and Mode B (Kanji Score Matrix).
"""

from typing import List
from .db_utils import get_known_kanji_set
from .parser import DictionaryLoader
from aqt import mw


def generate_definition(target_word: str, mode: str, dictionary_folder: str) -> str:
    """
    Generates a definition based on the selected mode.
    """
    loader = DictionaryLoader(dictionary_folder)
    definitions = loader.lookup(target_word)
    
    if not definitions:
        return f"[{mode}] No definition found for: {target_word}"

    if mode == "Mode B":
        return _generate_mode_b(definitions)
    else:
        # Mode A: Placeholder for ladder logic + LLM
        return f"[{mode}] (Stub) Best definition: {definitions[0]}"


def _generate_mode_b(definitions: List[str]) -> str:
    """
    Implements Mode B: Local Kanji Score Matrix.
    """
    known_kanji = get_known_kanji_set()
    best_def = definitions[0]
    best_score = -1.0

    for definition in definitions:
        # Score definition
        score = _calculate_kanji_score(definition, known_kanji)
        if score > best_score:
            best_score = score
            best_def = definition
            
    return f"[Mode B] {best_def} (Score: {best_score:.2f})"


def _calculate_kanji_score(text: str, known_kanji: set) -> float:
    """
    Calculates kanji comprehension score: (Known Kanji / Total Kanji).
    """
    kanji_in_text = [char for char in text if '\u4e00' <= char <= '\u9fff']
    
    if not kanji_in_text:
        return 1.0 # No unknown kanji in text
        
    known_count = sum(1 for char in kanji_in_text if char in known_kanji)
    return known_count / len(kanji_in_text)
