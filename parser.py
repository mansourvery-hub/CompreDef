"""
parser.py - JSON Dictionary loading and definition parsing.

Handles loading local dictionary JSON files (Yomitan/Yomichan format)
and parsing them for definition lookups.
"""

import json
import os
from typing import Dict, List, Any


class DictionaryLoader:
    """
    Loads and manages JSON dictionary files from a specified directory.
    Supports Yomitan/Yomichan term bank format.
    """
    def __init__(self, directory: str):
        self.directory = directory
        # Store as dict: {word: [definition1, ...]}
        self.data: Dict[str, List[str]] = {}
        self._load_dictionaries()

    def _load_dictionaries(self) -> None:
        """Loads all JSON files from the directory and stores them."""
        if not os.path.exists(self.directory):
            print(f"CompreDef Error: Dictionary directory not found: {self.directory}")
            return

        # Look for term_bank_*.json files
        files = [
            os.path.join(self.directory, f)
            for f in os.listdir(self.directory)
            if f.startswith("term_bank") and f.endswith(".json")
        ]
        
        if not files:
            print(f"CompreDef Warning: No term_bank_*.json files found in {self.directory}.")
            return
        
        for path in files:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    entries = json.load(f)
                    for entry in entries:
                        # Yomitan format: ["word", "reading", ..., definition_data]
                        # definition_data is usually a list of structured content
                        word = entry[0]
                        # Definitions can be complex; for now, extract simple text if possible
                        # or just convert to string.
                        definition = str(entry[5]) 
                        
                        if word not in self.data:
                            self.data[word] = []
                        self.data[word].append(definition)
            except (json.JSONDecodeError, IOError, IndexError) as e:
                print(f"CompreDef Error loading {path}: {e}")
                continue

    def lookup(self, word: str) -> List[str]:
        """Looks up a word in the loaded dictionary data."""
        return self.data.get(word, [])
