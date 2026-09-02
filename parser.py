"""
parser.py - JSON Dictionary loading and definition parsing.

Handles loading local dictionary JSON files and parsing them for definition lookups.
"""

import json
import os
from typing import Dict, List, Optional


class DictionaryLoader:
    """
    Loads and manages JSON dictionary files from a specified directory.
    """
    def __init__(self, directory: str):
        self.directory = directory
        self.dictionaries: List[Dict[str, List[str]]] = []
        self._load_dictionaries()

    def _load_dictionaries(self) -> None:
        """Loads all JSON files from the directory and stores them."""
        if not os.path.exists(self.directory):
            return

        # Sort files to ensure laddering order (Children's -> Standard -> Advanced)
        # Assumes filenames indicate order, e.g., '01_easy.json', '02_med.json'
        files = sorted([f for f in os.listdir(self.directory) if f.endswith('.json')])
        
        for filename in files:
            path = os.path.join(self.directory, filename)
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    self.dictionaries.append(json.load(f))
            except (json.JSONDecodeError, IOError):
                continue

    def lookup(self, word: str) -> List[str]:
        """Looks up a word in all loaded dictionaries."""
        definitions = []
        for d in self.dictionaries:
            if word in d:
                definitions.extend(d[word])
        return definitions
