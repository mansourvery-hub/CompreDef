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

# Legacy alias for older modules expecting get_config_value
get_config_value = get_setting

