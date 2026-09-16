import os
from typing import List, Optional

# Sibling imports must resolve in BOTH contexts:
# - inside Anki the add-on loads as package "1619602654" whose folder is
#   NOT on sys.path (absolute sibling imports crash with
#   ModuleNotFoundError: No module named 'provider');
# - in the regression suite modules are top-level with the repo root on
#   sys.path (relative imports fail there).
# __package__ is truthy only in the packaged (Anki) context.
if __package__:
    from .provider import LocalSQLiteProvider
    from .anki import (get_known_kanji_set, get_kanji_points,
                       get_vocab_points, sync_reset_caches)
    from .engine import DefinitionGenerator
    from .config import get_setting, get_dictionary_source, is_plain_text_mode, get_yomitan_url
else:
    from provider import LocalSQLiteProvider
    from anki import (get_known_kanji_set, get_kanji_points,
                      get_vocab_points, sync_reset_caches)
    from engine import DefinitionGenerator
    from config import get_setting, get_dictionary_source, is_plain_text_mode, get_yomitan_url

# Singletons for the application lifecycle
_provider = None
_generator = None
_provider_source = None  # tracks which source the singleton was built for
# Dedicated local-dictionary provider, independent of the configured
# source. Used ONLY by the Yomitan->local fail-safe (engine.py):
# when Yomitan is selected but unreachable/empty, generation falls back
# to local dictionaries instead of returning nothing.
_local_provider = None


def get_provider():
    global _provider, _provider_source
    src = get_dictionary_source()
    # Rebuild singleton if source changed (user toggled in GUI)
    if _provider is not None and _provider_source != src:
        _provider = None
        # Also reset generator so it picks up new provider
        global _generator
        _generator = None
    if _provider is None:
        _provider_source = src
        if src == "yomitan":
            # Lazy import to avoid circular deps and keep tests headless
            try:
                if __package__:
                    from .yomitan import YomitanApiProvider
                else:
                    from yomitan import YomitanApiProvider
                _provider = YomitanApiProvider(base_url=get_yomitan_url())
            except Exception:
                # Fallback to local if Yomitan provider fails to import
                addon_dir = os.path.dirname(os.path.abspath(__file__))
                cache_dir = os.path.join(addon_dir, "user_files", "cache")
                os.makedirs(cache_dir, exist_ok=True)
                _provider = LocalSQLiteProvider(cache_dir)
                _provider_source = "local"
        else:
            addon_dir = os.path.dirname(os.path.abspath(__file__))
            cache_dir = os.path.join(addon_dir, "user_files", "cache")
            os.makedirs(cache_dir, exist_ok=True)
            _provider = LocalSQLiteProvider(cache_dir)
    return _provider


def get_local_provider():
    """LocalSQLiteProvider regardless of configured source (for fail-safe)."""
    global _local_provider
    if _local_provider is None:
        addon_dir = os.path.dirname(os.path.abspath(__file__))
        cache_dir = os.path.join(addon_dir, "user_files", "cache")
        os.makedirs(cache_dir, exist_ok=True)
        _local_provider = LocalSQLiteProvider(cache_dir)
    return _local_provider


def reset_provider_cache():
    """Called when user changes dictionaries in the GUI."""
    global _provider, _generator, _local_provider, _provider_source
    _provider = None
    _generator = None
    _local_provider = None
    _provider_source = None


def reset_generator() -> None:
    """Forces the next get_generator() to re-snapshot learner knowledge.

    The generator caches the knowledge snapshot at creation time;
    reset_generator() clears the singleton and invalidates knowledge
    caches so the next get_generator() builds a fresh snapshot.
    """
    global _generator
    _generator = None
    sync_reset_caches()


def get_generator():
    """Returns the singleton DefinitionGenerator."""
    global _generator
    if _generator is None:
        _generator = DefinitionGenerator(
            provider=get_provider(),
            known_kanji=get_known_kanji_set(),
            kanji_points=get_kanji_points(),
            vocab_points=get_vocab_points(),
        )
    return _generator


def generate_definition_for_editor(
    word: str,
    reading: str = "",
    dictionary_paths: Optional[List[str]] = None,
    note_type: str = "",
) -> Optional[str]:
    """Entry point for editor/browser UI: returns HTML or plain text."""
    if dictionary_paths is None:
        if __package__:
            from .utils import resolve_dictionary_paths
        else:
            from utils import resolve_dictionary_paths
        dictionaries = get_setting("dictionaries", [])
        dictionary_folder = get_setting("dictionary_folder", "")
        disabled_dictionaries = get_setting("disabled_dictionaries", [])
        dictionary_paths = resolve_dictionary_paths(
            dictionaries, dictionary_folder, disabled_dictionaries
        )
    gen = get_generator()
    return gen.generate(word, dictionary_paths=dictionary_paths, reading=reading)


def generate_definition_for_browser(
    word: str,
    reading: str = "",
    dictionary_paths: Optional[List[str]] = None,
    note_type: str = "",
) -> Optional[str]:
    """Entry point for browser bulk action: returns HTML or plain text."""
    return generate_definition_for_editor(
        word, reading=reading, dictionary_paths=dictionary_paths, note_type=note_type
    )


def trigger_knowledge_rebuild():
    """Background task to rebuild the learner knowledge snapshot."""
    if __package__:
        from .anki import reset_caches
    else:
        from anki import reset_caches
    reset_caches()