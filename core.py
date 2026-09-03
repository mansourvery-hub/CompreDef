import os

# Sibling imports must resolve in BOTH contexts:
# - inside Anki the add-on loads as package "1619602654" whose folder is
#   NOT on sys.path (absolute sibling imports crash with
#   ModuleNotFoundError: No module named 'provider');
# - in the regression suite modules are top-level with the repo root on
#   sys.path (relative imports fail there).
# __package__ is truthy only in the packaged (Anki) context.
if __package__:
    from .provider import LocalSQLiteProvider
    from .anki import get_known_kanji_set
    from .engine import DefinitionGenerator
else:
    from provider import LocalSQLiteProvider
    from anki import get_known_kanji_set
    from engine import DefinitionGenerator

# Singletons for the application lifecycle
_provider = None
_generator = None

def get_provider():
    global _provider
    if _provider is None:
        # Replicate the old _get_cache_dir() logic
        addon_dir = os.path.dirname(os.path.abspath(__file__))
        cache_dir = os.path.join(addon_dir, "user_files", "cache")
        os.makedirs(cache_dir, exist_ok=True)
        _provider = LocalSQLiteProvider(cache_dir)
    return _provider

def get_generator():
    global _generator
    if _generator is None:
        _generator = DefinitionGenerator(get_provider(), get_known_kanji_set())
    return _generator
