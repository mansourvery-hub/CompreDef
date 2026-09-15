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
    from .anki import (get_known_kanji_set, get_kanji_points,
                       get_vocab_points, sync_reset_caches)
    from .engine import DefinitionGenerator
    from .config import get_config_value
else:
    from provider import LocalSQLiteProvider
    from anki import (get_known_kanji_set, get_kanji_points,
                      get_vocab_points, sync_reset_caches)
    from engine import DefinitionGenerator
    from config import get_config_value

# Singletons for the application lifecycle
_provider = None
_generator = None
_provider_source = None  # tracks which source the singleton was built for
# Dedicated local-dictionary provider, independent of the configured
# source. Used ONLY by the Yomitan->local fail-safe (engine.py):
# when Yomitan is selected but unreachable/empty, generation falls back
# to local dictionaries instead of returning nothing.
_local_provider = None

def _get_dictionary_source() -> str:
    """Reads the user's chosen dictionary source from config."""
    src = str(get_config_value("dictionary_source") or "local").strip().lower()
    if src in ("yomitan", "yomitan_api", "api"):
        return "yomitan"
    return "local"


def _get_yomitan_url() -> str:
    """Reads Yomitan API URL from config, defaulting to localhost:19633."""
    url = str(get_config_value("yomitan_url") or "").strip()
    return url.rstrip("/") if url else "http://127.0.0.1:19633"


def get_provider():
    global _provider, _provider_source
    src = _get_dictionary_source()
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
                _provider = YomitanApiProvider(base_url=_get_yomitan_url())
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
    """LocalSQLiteProvider regardless of the configured source.

    Powers the Yomitan->local fail-safe: the main provider singleton
    follows the user's source choice (and is a YomitanApiProvider when
    Yomitan is selected — whose lookups ignore dictionary paths), so the
    fallback needs its own local handle. Separate singleton so
    source switches never disturb it; cleared by reset_provider_cache().
    Never raises (returns None only if the cache dir itself is broken).
    """
    global _local_provider
    if _local_provider is None:
        try:
            addon_dir = os.path.dirname(os.path.abspath(__file__))
            cache_dir = os.path.join(addon_dir, "user_files", "cache")
            os.makedirs(cache_dir, exist_ok=True)
            _local_provider = LocalSQLiteProvider(cache_dir)
        except Exception:
            return None
    return _local_provider


def reset_provider_cache() -> None:
    """Forces next get_provider() to re-read config — called after GUI save."""
    global _provider, _generator, _provider_source, _local_provider
    _provider = None
    _generator = None
    _provider_source = None
    _local_provider = None


def reset_generator() -> None:
    """Forces the next get_generator() to re-snapshot learner knowledge.

    The generator caches the knowledge at build time; after the Scope
    changes (GUI save, quick-fix add-deck), the old generator would keep
    scoring against the STALE snapshot — the v1.1.4 "add deck does not
    fix it" contributor. Also rebuilds knowledge from Anki's DB when
    possible. Uses the SYNCHRONOUS reset because this can be reached from
    background threads (generation tasks); mw.taskman must never be
    called off the main thread (Anki prints a 'bug:' traceback).
    """
    global _generator
    _generator = None
    try:
        sync_reset_caches()
    except Exception:
        pass


def get_generator():
    """Returns the DefinitionGenerator singleton (knowledge-aware).

    Built with interval-weighted kanji + vocab points (v1.2). The
    snapshot is read ONCE per generator; reset_generator() forces a
    rebuild after knowledge-relevant changes.
    """
    global _generator
    if _generator is None:
        _generator = DefinitionGenerator(
            get_provider(),
            get_known_kanji_set(),
            kanji_points=dict(get_kanji_points()),
            vocab_points=dict(get_vocab_points()),
        )
    return _generator
