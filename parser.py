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
        
        print(f"CompreDef: Loading {len(files)} dictionary files...")
        
        for path in files:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    entries = json.load(f)
                    
                    for entry in entries:
                        # Yomitan format: ["word", "reading", "reading2", "reading3", value, definitions, value2, extra]
                        # Check if entry is a list with at least 2 items
                        if not isinstance(entry, list) or len(entry) < 2:
                            continue
                        
                        word = entry[0]
                        
                        # If we can't get the word, skip
                        if not word or not isinstance(word, str):
                            continue
                        
                        # Extract definitions from index 5
                        definitions_data = entry[5] if len(entry) > 5 else []
                        
                        # Process each definition block
                        definitions = []
                        for def_block in definitions_data:
                            if isinstance(def_block, list):
                                for item in def_block:
                                    if isinstance(item, dict):
                                        # Try to find content in the structured format
                                        content = item.get('content', [])
                                        if isinstance(content, list):
                                            for content_item in content:
                                                if isinstance(content_item, str):
                                                    definitions.append(content_item)
                                        elif isinstance(content, str):
                                            definitions.append(content)
                        
                        # If we found definitions, store them
                        if definitions:
                            if word not in self.data:
                                self.data[word] = []
                            self.data[word].extend(definitions)
                            
            except (json.JSONDecodeError, IOError, IndexError) as e:
                print(f"CompreDef Error loading {os.path.basename(path)}: {e}")
                continue
        
        print(f"CompreDef: Loaded {len(self.data)} unique words")

    def lookup(self, word: str) -> List[str]:
        """Looks up a word in the loaded dictionary data."""
        return self.data.get(word, [])
