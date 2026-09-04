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
_provider_source = None  # tracks which source the singleton was built for

def _get_dictionary_source() -> str:
    """Reads the user's chosen dictionary source from config."""
    try:
        from aqt import mw  # type: ignore
        if mw and hasattr(mw, "addonManager"):
            try:
                name = mw.addonManager.addonFromModule(__name__)
            except Exception:
                name = None
            if not name:
                name = "1619602654"
            cfg = mw.addonManager.getConfig(name)
            if isinstance(cfg, dict):
                src = str(cfg.get("dictionary_source") or "local").strip().lower()
                if src in ("yomitan", "yomitan_api", "api"):
                    return "yomitan"
    except Exception:
        pass
    return "local"


def _get_yomitan_url() -> str:
    """Reads Yomitan API URL from config, defaulting to localhost:19633."""
    try:
        from aqt import mw  # type: ignore
        if mw and hasattr(mw, "addonManager"):
            try:
                name = mw.addonManager.addonFromModule(__name__)
            except Exception:
                name = None
            if not name:
                name = "1619602654"
            cfg = mw.addonManager.getConfig(name)
            if isinstance(cfg, dict) and cfg.get("yomitan_url"):
                url = str(cfg["yomitan_url"]).strip()
                if url:
                    return url.rstrip("/")
    except Exception:
        pass
    return "http://127.0.0.1:19633"


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


def reset_provider_cache() -> None:
    """Forces next get_provider() to re-read config — called after GUI save."""
    global _provider, _generator, _provider_source
    _provider = None
    _generator = None
    _provider_source = None


def get_generator():
    global _generator
    if _generator is None:
        _generator = DefinitionGenerator(get_provider(), get_known_kanji_set())
    return _generator
