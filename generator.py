"""
generator.py - Compatibility layer for legacy tests and imports.
"""
# Dual-context sibling imports (relative inside Anki's package load,
# absolute in the top-level test harness — see core.py for why).
if __package__:
    from .core import get_generator
    from .utils import extract_clean_word
else:
    from core import get_generator
    from utils import extract_clean_word

def generate_definition(target_word, mode="", dictionary_folder="", dictionaries=None, reading="", disabled_dictionaries=None):
    # Map legacy args to new DefinitionGenerator.generate()
    if __package__:
        from .utils import resolve_ladder_paths
    else:
        from utils import resolve_ladder_paths
    ladder = resolve_ladder_paths(dictionaries, dictionary_folder, disabled_dictionaries)
    return get_generator().generate(target_word, ladder, reading)

# Re-export for tests
def _calculate_kanji_score(text, known):
    if __package__:
        from .scoring import calculate_kanji_score
    else:
        from scoring import calculate_kanji_score
    return calculate_kanji_score(text, known).score

def _is_reference_title(text):
    if __package__:
        from .scoring import is_reference_title
    else:
        from scoring import is_reference_title
    return is_reference_title(text)

def get_known_kanji_set():
    if __package__:
        from .anki import get_known_kanji_set
    else:
        from anki import get_known_kanji_set
    return get_known_kanji_set()
