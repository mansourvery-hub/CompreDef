"""
parser.py - JSON Dictionary loading and definition parsing for CompreDef.

Loads Yomitan/Yomichan-format term bank JSON files (term_bank_*.json).
Supports:
- Individual dictionary representation (SingleDictionary) with per-dictionary
  disk caching in user_files/cache/.
- On-demand lazy loading: dictionaries early in the ladder (e.g. Children's)
  are loaded first; if an early dictionary produces a fully comprehensible
  definition, later dictionaries don't even need to be touched.
- Title extraction from index.json for beautiful UI display.
- Discovery helper to detect dictionary subfolders inside a parent folder.
- Plain-text and nested structured-content extraction.
"""

import json
import os
import pickle
import re
import hashlib
from typing import Dict, List, Optional, Tuple


def _extract_text_from_structured_content(node: object) -> str:
    """
    Recursively walks a Yomitan structured-content node and collects
    all human-readable text.

    Structured content is either:
    - a plain string,
    - a list of nodes, or
    - a dict like {"tag": ..., "content": ...} possibly with "rt" (ruby text).
    """
    if isinstance(node, str):
        return node

    if isinstance(node, list):
        parts = [_extract_text_from_structured_content(child) for child in node]
        return "".join(p for p in parts if p)

    if isinstance(node, dict):
        parts = []
        content = node.get("content")
        if content is not None:
            parts.append(_extract_text_from_structured_content(content))
        # Ruby text (furigana) also carries characters
        rt = node.get("rt")
        if rt is not None:
            parts.append(_extract_text_from_structured_content(rt))
        return "".join(p for p in parts if p)

    return ""


def _definition_to_text(def_block: object) -> str:
    """Converts a single Yomitan definition block into plain text."""
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
    text = re.sub(r"svg[^\s\"']*", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def get_dictionary_title(path: str) -> str:
    """
    Retrieves the display title for a dictionary folder.

    First inspects index.json if present (standard Yomitan metadata),
    falling back to the folder's base name.
    """
    if not path:
        return ""
    try:
        norm_path = os.path.realpath(os.path.expanduser(path))
        index_file = os.path.join(norm_path, "index.json")
        if os.path.isfile(index_file):
            with open(index_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
                if isinstance(meta, dict) and meta.get("title"):
                    return str(meta["title"]).strip()
        return os.path.basename(norm_path)
    except Exception:
        return os.path.basename(path.rstrip("/\\")) or path


def find_dictionary_folders(parent_or_dict_path: str) -> List[str]:
    """
    Scans a directory path and returns all valid dictionary folder paths.

    If `parent_or_dict_path` directly contains term_bank_*.json files,
    it returns `[parent_or_dict_path]`.
    Otherwise, it checks immediate subdirectories and returns all that
    contain term_bank_*.json files or index.json.
    """
    if not parent_or_dict_path or not os.path.isdir(parent_or_dict_path):
        return []

    norm = os.path.realpath(os.path.expanduser(parent_or_dict_path))

    # Check if the folder itself is a dictionary
    has_banks = any(
        f.startswith("term_bank") and f.endswith(".json")
        for f in os.listdir(norm)
    )
    if has_banks:
        return [norm]

    # Check immediate subdirectories
    results = []
    for entry in sorted(os.listdir(norm)):
        sub = os.path.join(norm, entry)
        if not os.path.isdir(sub):
            continue
        try:
            sub_files = os.listdir(sub)
            if any(f.startswith("term_bank") and f.endswith(".json") for f in sub_files) or "index.json" in sub_files:
                results.append(sub)
        except Exception:
            continue

    return results


def _get_cache_dir() -> str:
    """
    Locates and creates the persistent cache directory inside the add-on's user_files.
    Falls back to /tmp if unavailable.
    """
    try:
        addon_dir = os.path.dirname(os.path.abspath(__file__))
        cache_dir = os.path.join(addon_dir, "user_files", "cache")
        os.makedirs(cache_dir, exist_ok=True)
        return cache_dir
    except Exception:
        fallback = os.path.join("/tmp", "compredef_cache")
        os.makedirs(fallback, exist_ok=True)
        return fallback


class SingleDictionary:
    """
    Represents a single Yomitan dictionary folder.

    Maintains its own independent cache file so reordering dictionaries in
    the ladder requires ZERO re-parsing.
    """

    def __init__(self, path: str):
        self.path = os.path.realpath(os.path.expanduser(path))
        self.title = get_dictionary_title(self.path)
        self.data: Dict[str, List[str]] = {}
        self.is_loaded: bool = False

    def _compute_signature(self, bank_files: List[str]) -> str:
        """Computes signature of term bank files to detect dictionary updates."""
        parts = []
        for f in sorted(bank_files):
            full_p = os.path.join(self.path, f)
            try:
                st = os.stat(full_p)
                parts.append(f"{f}:{st.st_mtime_ns}:{st.st_size}")
            except Exception:
                parts.append(f)
        return hashlib.md5("\n".join(parts).encode("utf-8")).hexdigest()

    def _cache_file_path(self) -> str:
        """Returns the pickle cache file path specific to this dictionary."""
        digest = hashlib.md5(self.path.encode("utf-8")).hexdigest()[:16]
        return os.path.join(_get_cache_dir(), f"dict_{digest}.pkl")

    def load(self) -> None:
        """
        Loads the dictionary into memory.

        Attempts to load from its dedicated pickle cache. If the cache is
        missing or outdated, parses the JSON term banks and saves the cache.
        """
        if self.is_loaded:
            return

        if not os.path.isdir(self.path):
            print(f"CompreDef: Dictionary path not found: {self.path}")
            self.is_loaded = True
            return

        bank_files = [
            f for f in os.listdir(self.path)
            if f.startswith("term_bank") and f.endswith(".json")
        ]
        if not bank_files:
            print(f"CompreDef: No term banks in: {self.path}")
            self.is_loaded = True
            return

        current_sig = self._compute_signature(bank_files)
        cache_path = self._cache_file_path()

        # Try fast load from disk cache
        if os.path.isfile(cache_path):
            try:
                with open(cache_path, "rb") as f:
                    cached_sig, cached_data = pickle.load(f)
                if cached_sig == current_sig:
                    self.data = cached_data
                    self.is_loaded = True
                    print(f"CompreDef: Loaded '{self.title}' from cache ({len(self.data)} words)")
                    return
            except Exception as e:
                print(f"CompreDef: Cache read failed for '{self.title}' ({e}), re-parsing...")

        # Parse term banks
        print(f"CompreDef: Parsing '{self.title}' ({len(bank_files)} banks)...")
        parsed_data: Dict[str, List[str]] = {}

        for b_name in sorted(bank_files):
            b_path = os.path.join(self.path, b_name)
            try:
                with open(b_path, "r", encoding="utf-8") as f:
                    entries = json.load(f)
            except Exception as e:
                print(f"CompreDef: Failed to load {b_name}: {e}")
                continue

            for entry in entries:
                if not isinstance(entry, list) or len(entry) < 6:
                    continue

                word = entry[0]
                if not word or not isinstance(word, str):
                    continue

                for def_block in entry[5]:
                    text = _clean_definition_text(_definition_to_text(def_block))
                    if text:
                        parsed_data.setdefault(word, []).append(text)

        self.data = parsed_data
        self.is_loaded = True
        print(f"CompreDef: Finished parsing '{self.title}' ({len(self.data)} words)")

        # Persist to disk cache
        try:
            with open(cache_path, "wb") as f:
                pickle.dump((current_sig, self.data), f)
        except Exception as e:
            print(f"CompreDef: Failed to save cache for '{self.title}': {e}")

    def lookup(self, word: str) -> List[str]:
        """Looks up all definitions for `word`, loading data on demand."""
        if not self.is_loaded:
            self.load()
        return self.data.get(word, [])


# In-memory dictionary cache to prevent duplicate instances
_loaded_dicts: Dict[str, SingleDictionary] = {}


def get_single_dictionary(path: str) -> SingleDictionary:
    """Returns or creates a cached SingleDictionary instance for the given path."""
    norm = os.path.realpath(os.path.expanduser(path))
    if norm not in _loaded_dicts:
        _loaded_dicts[norm] = SingleDictionary(norm)
    return _loaded_dicts[norm]


class DictionaryLoader:
    """
    Backwards-compatible loader managing an ordered list of dictionaries.
    """

    def __init__(self, directory_or_paths: object):
        self.dictionaries: List[SingleDictionary] = []

        paths: List[str] = []
        if isinstance(directory_or_paths, list):
            paths = [str(p) for p in directory_or_paths if p]
        elif isinstance(directory_or_paths, str) and directory_or_paths:
            # Discover subfolders or use the single folder
            found = find_dictionary_folders(directory_or_paths)
            paths = found if found else [directory_or_paths]

        for p in paths:
            if os.path.isdir(p):
                self.dictionaries.append(get_single_dictionary(p))

    def lookup_ladder(self, word: str) -> List[Tuple[SingleDictionary, List[str]]]:
        """
        Returns a list of (dictionary, definitions) for each dictionary in
        ladder order.
        """
        results = []
        for d in self.dictionaries:
            defs = d.lookup(word)
            if defs:
                results.append((d, defs))
        return results

    def lookup_all(self, word: str) -> List[str]:
        """Concatenates all definitions across all dictionaries."""
        all_defs = []
        for d in self.dictionaries:
            all_defs.extend(d.lookup(word))
        return all_defs

    def lookup(self, word: str) -> List[str]:
        return self.lookup_all(word)
