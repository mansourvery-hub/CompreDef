"""
generator.py - Compatibility layer for legacy tests and imports.
"""
from core import get_generator
from utils import extract_clean_word

def generate_definition(target_word, mode="", dictionary_folder="", dictionaries=None, reading="", disabled_dictionaries=None):
    # Map legacy args to new DefinitionGenerator.generate()
    from utils import resolve_ladder_paths
    ladder = resolve_ladder_paths(dictionaries, dictionary_folder, disabled_dictionaries)
    return get_generator().generate(target_word, ladder, reading)

# Re-export for tests
def _calculate_kanji_score(text, known):
    from scoring import calculate_kanji_score
    return calculate_kanji_score(text, known).score

def _is_reference_title(text):
    from scoring import is_reference_title
    return is_reference_title(text)

def get_known_kanji_set():
    from anki import get_known_kanji_set
    return get_known_kanji_set()
