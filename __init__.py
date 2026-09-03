"""
CompreDef - Anki Add-on Entry Point

Automatically generates definitions for Japanese vocabulary cards strictly tailored
to the user's known vocabulary and kanji.
"""

from aqt import mw
from aqt.qt import QMenu
from .gui import show_config_dialog
from .editor_browser import setup_editor_browser_hooks
from . import anki

def _add_tools_menu_entry() -> None:


def _add_tools_menu_entry() -> None:
    """
    Adds a direct 'CompreDef Configuration...' entry under Anki's Tools menu.

    Previously the only path was Tools -> Add-ons -> (double-click CompreDef),
    which was buried; the user explicitly asked for one-click access.
    Uses a lambda import guard so the dialog opens fresh config every time.
    """
    if not mw or not hasattr(mw, "form"):
        return
    try:
        tools_menu: QMenu = mw.form.menuTools
        action = tools_menu.addAction("CompreDef Configuration...")
        action.setShortcut("Ctrl+Shift+C")
        action.triggered.connect(lambda _: show_config_dialog())
        # Separate visually from Anki's own entries
        tools_menu.insertSeparator(action)
    except Exception as e:
        # Menu injection must never break Anki startup
        print(f"CompreDef: Failed to add Tools menu entry: {e}")


# Register the configuration dialog callback with Anki's Addon Manager
# This allows users to configure the add-on directly from Tools → Add-ons → Config
if mw:
    # Trigger asynchronous build of learner knowledge snapshot on startup
    anki.init_caches_async()
    mw.addonManager.setConfigAction(__name__, show_config_dialog)
    setup_editor_browser_hooks()
    _add_tools_menu_entry()

