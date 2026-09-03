from dataclasses import dataclass
from typing import List, Optional

# The version of the HTML rendering output. 
# Bump this whenever rendering changes to invalidate stale SQLite caches.
RENDERER_VERSION = "yomitan_html_v2_reading"

@dataclass(frozen=True)
class DictionaryEntry:
    """Represents a single definition entry from a dictionary."""
    word: str
    reading: str
    definition: str
    dictionary_title: str
    dictionary_path: str

@dataclass(frozen=True)
class ScoringResult:
    """The result of scoring a definition."""
    definition: str
    score: float
    is_perfect: bool
