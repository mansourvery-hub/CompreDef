from typing import Any, Dict, Optional

def get_config() -> Dict[str, Any]:
    """Centralized access to add-on configuration."""
    try:
        from aqt import mw
        if mw and hasattr(mw, "addonManager"):
            try:
                name = mw.addonManager.addonFromModule(__name__)
            except Exception:
                name = None
            if not name:
                name = "1619602654"
            cfg = mw.addonManager.getConfig(name)
            if isinstance(cfg, dict):
                return cfg
    except Exception:
        pass
    return {}

def get_setting(key: str, default: Any = None) -> Any:
    """Helper to get a specific setting."""
    cfg = get_config()
    return cfg.get(key, default)


def get_dictionary_source() -> str:
    """Reads the user's chosen dictionary source from config."""
    src = str(get_setting("dictionary_source") or "local").strip().lower()
    if src in ("yomitan", "yomitan_api", "api"):
        return "yomitan"
    return "local"


def is_plain_text_mode() -> bool:
    """Reads the GUI toggle 'plain_text_definitions' from add-on config."""
    return bool(get_setting("plain_text_definitions", False))


def get_yomitan_url() -> str:
    """Reads Yomitan API URL from config, defaulting to localhost:19633."""
    url = str(get_setting("yomitan_url") or "").strip()
    return url.rstrip("/") if url else "http://127.0.0.1:19633"

