"""
CompreDef - Anki Add-on Entry Point

Automatically generates definitions for Japanese vocabulary cards strictly tailored
to the user's known vocabulary and kanji.
"""

from aqt import mw
from .gui import show_config_dialog

# Register the configuration dialog callback with Anki's Addon Manager
# This allows users to configure the add-on directly from Tools -> Add-ons -> Config
if mw:
    mw.addonManager.setConfigAction(__name__, show_config_dialog)
