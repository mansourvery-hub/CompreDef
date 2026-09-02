"""
parser.py - JSON Dictionary loading and definition parsing for CompreDef.

Loads Yomitan/Yomichan-format term bank JSON files (term_bank_*.json)
from a dictionary folder, including nested subdirectories (each unzipped
dictionary lives in its own subfolder).

Text extraction supports both plain-text definitions and structured-content
(HTML-like nested dicts) so that any dictionary format yields readable text.

The ladder (Children's -> Standard -> Advanced) is determined by the
alphabetical order of the dictionary folder names, then by term bank number,
mirroring the file ordering rules from ARCHITECTURE.md.
"""

import json
import os
import pickle
import re
import hashlib
from typing import Dict, List, Optional


def _extract_text_from_structured_content(node: object) -> str:
    """
    Recursively walks a Yomitan structured-content node and collects
    all human-readable text.

    Structured content is either:
    - a plain string,
    - a list of nodes, or
    - a dict like {"tag": ..., "content": ...} possibly with "rt" (ruby text).

    Returns the concatenated text with noise (image paths, etc.) removed.
    """
    # Base case: plain text node
    if isinstance(node, str):
        return node

    # List of child nodes: process each and join
    if isinstance(node, list):
        parts = [_extract_text_from_structured_content(child) for child in node]
        return "".join(p for p in parts if p)

    # Dict node: recurse into its "content", append ruby text from "rt"
    if isinstance(node, dict):
        parts = []
        content = node.get("content")
        if content is not None:
            parts.append(_extract_text_from_structured_content(content))
        # Ruby text (furigana) also carries useful characters for kanji scoring
        rt = node.get("rt")
        if rt is not None:
            parts.append(_extract_text_from_structured_content(rt))
        return "".join(p for p in parts if p)

    # Anything else (numbers, booleans, None) is not displayable text
    return ""


def _definition_to_text(def_block: object) -> str:
    """
    Converts a single Yomitan definition block into plain text.

    Yomitan definition blocks come in two flavors:
    - {"type": "text", "text": "..."}                     -> plain text
    - {"type": "structured-content", "content": [...]}     -> nested content
    - plain JSON string                                   -> already text
    """
    if isinstance(def_block, str):
        return def_block.strip()

    if isinstance(def_block, dict):
        if def_block.get("type") == "text":
            return str(def_block.get("text", "")).strip()
        if "content" in def_block:
            return _extract_text_from_structured_content(
                def_block["content"]
            ).strip()

    return ""


def _clean_definition_text(text: str) -> str:
    """
    Normalizes extracted definition text:
    collapses excessive whitespace and removes leftover image markers,
    while preserving Japanese punctuation and newlines between senses.
    """
    if not text:
        return ""
    # Remove residual SVG/image references that sometimes leak through
    text = re.sub(r"svg[^\s\"']*", "", text)
    # Collapse multiple spaces (but keep intentional newlines)
    text = re.sub(r"[ \t]+", " ", text)
    # Collapse 3+ newlines down to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class DictionaryLoader:
    """
    Loads and manages JSON dictionary files (Yomitan term banks) from a
    directory tree.

    Each immediate subdirectory is treated as one dictionary (one rung on
    the ladder). Definitions are kept per-dictionary so Mode A can walk the
    ladder in order while Mode B can score all candidates at once.
    """

    def __init__(self, directory: str):
        self.directory = directory
        # Ordered list of per-dictionary maps: {word: [definitions...]}
        self.dictionaries: List[Dict[str, List[str]]] = []
        # Flat index for quick lookups: {word: [definitions...]}
        self.data: Dict[str, List[str]] = {}

        # Parsing hundreds of JSON term banks takes minutes on first load,
        # so we persist the parsed structure to a pickle cache in the
        # add-on's user files folder. Subsequent loads take milliseconds.
        cache_path = self._cache_path_for(directory)
        if cache_path and os.path.exists(cache_path):
            try:
                with open(cache_path, "rb") as f:
                    self.dictionaries, self.data = pickle.load(f)
                print(f"CompreDef: Loaded {len(self.dictionaries)} dictionaries from cache")
                return
            except Exception as e:
                # Corrupt cache must never break loading; fall through to a
                # full re-parse.
                print(f"CompreDef: Cache read failed ({e}), re-parsing...")

        self._load_dictionaries()
        if cache_path and self.dictionaries:
            self._save_cache(cache_path)

    def _cache_path_for(self, directory: str) -> Optional[str]:
        """
        Computes a stable cache file path for a dictionary folder.

        The path is derived from a hash of the folder path so multiple
        folders can coexist without collisions. Caches live in the
        system temp directory (deleted on reboot, always rebuildable).
        """
        if not directory:
            return None
        digest = hashlib.md5(directory.encode("utf-8")).hexdigest()[:16]
        return os.path.join("/tmp", f"compredef_cache_{digest}.pkl")

    def _save_cache(self, cache_path: str) -> None:
        """Persists the parsed dictionary structure to the pickle cache."""
        try:
            with open(cache_path, "wb") as f:
                pickle.dump((self.dictionaries, self.data), f)
            print(f"CompreDef: Saved dictionary cache to {cache_path}")
        except Exception as e:
            # Cache writing is best-effort; failing to cache must never
            # break dictionary loading.
            print(f"CompreDef: Cache write failed: {e}")

    def _find_term_banks(self) -> Dict[str, List[str]]:
        """
        Discovers term bank files, grouped per dictionary subfolder.

        Returns a mapping of {dictionary_name: [absolute bank paths in
        natural ladder order]} so the ladder order is deterministic.
        """
        banks_by_dict: Dict[str, List[str]] = {}

        if not os.path.isdir(self.directory):
            print(f"CompreDef Error: Dictionary directory not found: {self.directory}")
            return banks_by_dict

        # Case 1: term banks directly inside the configured folder
        top_level = [
            f for f in os.listdir(self.directory)
            if f.startswith("term_bank") and f.endswith(".json")
        ]
        if top_level:
            banks_by_dict[self.directory] = sorted(
                os.path.join(self.directory, f) for f in top_level
            )

        # Case 2: each subfolder is one unzipped dictionary
        for entry in sorted(os.listdir(self.directory)):
            sub_path = os.path.join(self.directory, entry)
            if not os.path.isdir(sub_path):
                continue
            banks = [
                f for f in os.listdir(sub_path)
                if f.startswith("term_bank") and f.endswith(".json")
            ]
            if banks:
                banks_by_dict[entry] = sorted(
                    os.path.join(sub_path, f) for f in banks
                )

        return banks_by_dict

    def _load_dictionaries(self) -> None:
        """Loads all discovered term banks into memory, preserving ladder order."""
        banks_by_dict = self._find_term_banks()

        if not banks_by_dict:
            print(
                "CompreDef Warning: No term_bank_*.json files found in "
                f"{self.directory} (checked subfolders too)."
            )
            return

        # Sort dictionary names so the ladder (easy -> advanced) is stable;
        # users can prefix folders with numbers to control difficulty order.
        total_words = 0
        for dict_name in sorted(banks_by_dict.keys()):
            dict_data: Dict[str, List[str]] = {}
            for bank_path in banks_by_dict[dict_name]:
                try:
                    with open(bank_path, "r", encoding="utf-8") as f:
                        entries = json.load(f)
                except (json.JSONDecodeError, IOError) as e:
                    print(f"CompreDef Error loading {os.path.basename(bank_path)}: {e}")
                    continue

                for entry in entries:
                    # Yomitan format: [term, reading, ..., definitions(at index 5), ...]
                    if not isinstance(entry, list) or len(entry) < 6:
                        continue

                    word = entry[0]
                    if not word or not isinstance(word, str):
                        continue

                    for def_block in entry[5]:
                        text = _clean_definition_text(
                            _definition_to_text(def_block)
                        )
                        if not text:
                            continue
                        dict_data.setdefault(word, []).append(text)

            if dict_data:
                self.dictionaries.append(dict_data)
                total_words += len(dict_data)

        # Build flat lookup index across all dictionaries
        for dict_data in self.dictionaries:
            for word, defs in dict_data.items():
                self.data.setdefault(word, []).extend(defs)

        print(f"CompreDef: Loaded {len(self.dictionaries)} dictionaries, {total_words} unique words")

    def lookup_all(self, word: str) -> List[str]:
        """
        Returns every definition for a word across all dictionaries,
        concatenated (used by Mode B for scoring all candidates).
        """
        return self.data.get(word, [])

    def lookup(self, word: str) -> List[str]:
        """Backwards-compatible alias for `lookup_all`."""
        return self.lookup_all(word)

    def lookup_ladder(self, word: str) -> List[List[str]]:
        """
        Returns definitions grouped per dictionary in ladder order:
        [[dict1 defs...], [dict2 defs...], ...] (used by Mode A).
        """
        return [d.get(word, []) for d in self.dictionaries if d.get(word)]
