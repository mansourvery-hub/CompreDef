"""
generator.py - Definition generation logic for CompreDef.

Implements Mode A (Dictionary Ladder) and Mode B (Kanji Score Matrix)
as described in ARCHITECTURE.md.

Dictionary loading is expensive (100+ JSON files), so the loader is cached
per dictionary folder: bulk generation on hundreds of notes must not
re-parse the dictionaries for every single note.
"""

from typing import Dict, List, Optional
from .db_utils import get_known_kanji_set
from .parser import DictionaryLoader


# Cache of DictionaryLoader instances keyed by folder path.
# This avoids re-parsing hundreds of JSON files for every note
# (critical for bulk generation performance).
_loader_cache: Dict[str, DictionaryLoader] = {}


def _get_loader(dictionary_folder: str) -> DictionaryLoader:
    """
    Returns a cached DictionaryLoader for the given folder, creating it
    on first use. Falls back to an empty loader if the folder is invalid.
    """
    if not dictionary_folder:
        return DictionaryLoader("")

    if dictionary_folder not in _loader_cache:
        _loader_cache[dictionary_folder] = DictionaryLoader(dictionary_folder)

    return _loader_cache[dictionary_folder]


def generate_definition(target_word: str, mode: str, dictionary_folder: str) -> str:
    """
    Generates a definition for a target word using the selected mode.

    Mode A: walks the dictionary ladder (easy -> advanced) and picks the
    first fully comprehensible definition; LLM fallback is not yet wired.
    Mode B: scores all candidate definitions against the user's known
    kanji matrix and picks the highest-scoring one.
    """
    loader = _get_loader(dictionary_folder)

    if mode == "Mode B":
        return _generate_mode_b(target_word, loader)

    return _generate_mode_a(target_word, loader)


def _generate_mode_a(target_word: str, loader: DictionaryLoader) -> str:
    """
    Implements Mode A: The Dictionary Ladder.

    Walks dictionaries in ladder order (Children's -> Standard -> Advanced)
    and returns the first definition whose kanji are all known to the user.
    If nothing is fully comprehensible, falls back to the easiest
    (first ladder rung) definition. LLM fallback comes later.
    """
    ladder = loader.lookup_ladder(target_word)

    if not ladder:
        return f"[Mode A] No definition found for: {target_word}"

    known_kanji = get_known_kanji_set()

    # Ladder rungs are ordered easiest-first, so the first rung whose
    # definitions contain only known kanji wins (per ARCHITECTURE.md).
    for rung_defs in ladder:
        for definition in rung_defs:
            if _all_kanji_known(definition, known_kanji):
                return definition

    # LLM fallback placeholder: no local definition was fully comprehensible.
    # For now, fall back to the simplest (first) definition available.
    return ladder[0][0]


def _generate_mode_b(target_word: str, loader: DictionaryLoader) -> str:
    """
    Implements Mode B: Local Kanji Score Matrix.

    Gathers every definition across all dictionaries and deterministically
    selects the one with the highest kanji comprehension score.
    """
    definitions = loader.lookup_all(target_word)

    if not definitions:
        return f"[Mode B] No definition found for: {target_word}"

    known_kanji = get_known_kanji_set()
    best_def: str = definitions[0]
    best_score: float = -1.0

    for definition in definitions:
        score = _calculate_kanji_score(definition, known_kanji)
        if score > best_score:
            best_score = score
            best_def = definition

    return best_def


def _all_kanji_known(text: str, known_kanji: set) -> bool:
    """
    Returns True if every kanji character in the text is within the
    user's known kanji set (a card with interval > 0 used it).
    """
    for char in text:
        if '\u4e00' <= char <= '\u9fff' and char not in known_kanji:
            return False
    return True


def _calculate_kanji_score(text: str, known_kanji: set) -> float:
    """
    Calculates kanji comprehension score: known kanji / total kanji.

    A definition with no kanji at all is treated as fully comprehensible
    (kana-only text is readable if the user knows kana).
    """
    kanji_in_text = [char for char in text if '\u4e00' <= char <= '\u9fff']

    if not kanji_in_text:
        return 1.0

    known_count = sum(1 for char in kanji_in_text if char in known_kanji)
    return known_count / len(kanji_in_text)
