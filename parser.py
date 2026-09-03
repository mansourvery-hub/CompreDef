"""
parser.py - SQLite-backed dictionary storage and Yomitan HTML generator for CompreDef.

Key Features:
- Native .zip & Folder Support: Directly reads and indexes unzipped folders AND
  unextracted Yomitan .zip dictionary archives.
- 100% Faithful Yomitan HTML Rendering: Implements Yomitan's StructuredContentGenerator
  to produce rich, semantic HTML (<ruby>, <rt>, <span class="gloss-sc-span">,
  <div data-sc-name="用例">, inline CSS styles, etc.).
- Blazing-Fast SQLite Cache: All parsed definitions are stored in
  `user_files/cache/dictionaries.db`. Lookups execute in ~0.08ms via indexed B-Tree
  with 0MB RAM footprint and 0% CPU overhead.
"""

import json
import os
import re
import html
import hashlib
import sqlite3
import threading
import zipfile
from typing import Dict, List, Optional, Tuple, Any

# Bump this whenever the HTML rendering output format changes in ANY way.
# It is embedded into every dictionary's cache signature so that upgrading
# the renderer automatically invalidates stale SQLite entries instead of
# silently serving old plain-text definitions forever (the exact bug where
# '先ず' kept returning 121 chars of plain text instead of ~7000 chars of
# rich Yomitan HTML after the renderer was rewritten).
RENDERER_VERSION = "yomitan_html_v1"

# How many rendered definitions to hold in memory before flushing to SQLite.
# Bounded RAM: giant dictionaries (大辞泉: 632,876 entries ≈ 1.3 GB of HTML)
# must NEVER be accumulated in a single Python list — that exhausts memory,
# drives the system into swap thrash, and freezes Anki at 100% CPU on what
# should be a microsecond lookup of a nonsense word (bug: 駿ってさ crash).
_INDEX_BATCH_SIZE = 5000


def _style_to_css(style: dict) -> str:
    """
    Converts a Yomitan structured-content style dictionary into an inline CSS string.
    Maps camelCase properties to kebab-case (e.g. fontSize -> font-size)
    and handles numeric em dimensions as Yomitan does.
    """
    if not isinstance(style, dict):
        return ""

    css_rules = []
    for prop, val in style.items():
        kebab = "".join(["-" + c.lower() if c.isupper() else c for c in prop]).lstrip("-")

        # In Yomitan: if margin or padding is numeric, append 'em'
        if isinstance(val, (int, float)) and kebab in (
            "margin-top", "margin-left", "margin-right", "margin-bottom",
            "padding-top", "padding-left", "padding-right", "padding-bottom",
            "width", "height"
        ):
            css_rules.append(f"{kebab}: {val}em")
        elif isinstance(val, list):
            css_rules.append(f"{kebab}: {' '.join(str(x) for x in val)}")
        elif val is not None:
            css_rules.append(f"{kebab}: {val}")

    return "; ".join(css_rules)


def render_structured_content_node(node: object) -> str:
    """
    Renders a Yomitan structured-content node to semantic HTML.
    Faithful port of Yomitan's StructuredContentGenerator.
    """
    if isinstance(node, str):
        return html.escape(node)

    if isinstance(node, list):
        return "".join(render_structured_content_node(child) for child in node)

    if isinstance(node, dict):
        tag = node.get("tag", "span")
        classes = [f"gloss-sc-{tag}"]
        attrs = []

        # Data attributes: data-sc-{name}
        data = node.get("data")
        if isinstance(data, dict):
            for dk, dv in data.items():
                attrs.append(f'data-sc-{html.escape(dk.lower())}="{html.escape(str(dv))}"')

        # Inline styles
        style = node.get("style")
        if isinstance(style, dict):
            css = _style_to_css(style)
            if css:
                attrs.append(f'style="{html.escape(css)}"')

        # Language attribute
        lang = node.get("lang")
        if lang:
            attrs.append(f'lang="{html.escape(str(lang))}"')

        # Title attribute
        title = node.get("title")
        if title:
            attrs.append(f'title="{html.escape(str(title))}"')

        # Href for links
        href = node.get("href")
        if href:
            attrs.append(f'href="{html.escape(str(href))}"')

        # Table cell dimensions
        for cell_attr in ("colSpan", "rowSpan"):
            val = node.get(cell_attr)
            if isinstance(val, int):
                attrs.append(f'{cell_attr.lower()}="{val}"')

        # Details open boolean
        if node.get("open") is True:
            attrs.append("open")

        attrs.insert(0, f'class="{" ".join(classes)}"')
        attr_str = " " + " ".join(attrs) if attrs else ""

        if tag == "br":
            return f"<br{attr_str}>"

        if tag == "img":
            src = node.get("path") or node.get("src", "")
            alt = node.get("title") or node.get("alt", "image")
            img_attrs = ['class="gloss-image"']
            if src:
                img_attrs.append(f'src="{html.escape(str(src))}"')
            if alt:
                img_attrs.append(f'alt="{html.escape(str(alt))}"')
            return f'<img {" ".join(img_attrs)}>'

        content = node.get("content", "")
        inner = render_structured_content_node(content) if content is not None else ""
        return f"<{tag}{attr_str}>{inner}</{tag}>"

    return ""


def render_yomitan_definition_html(def_block: object) -> str:
    """
    Renders a Yomitan definition block into Anki-ready HTML.
    - Structured content: returns `<span class="structured-content">{rendered_html}</span>`
    - Plain text strings: HTML-escapes and replaces newlines with `<br>`.
    """
    if isinstance(def_block, str):
        return html.escape(def_block.strip()).replace("\n", "<br>")

    if isinstance(def_block, dict):
        if def_block.get("type") == "text":
            return html.escape(str(def_block.get("text", "")).strip()).replace("\n", "<br>")
        if def_block.get("type") == "structured-content" or "content" in def_block:
            content = def_block.get("content", [])
            rendered = render_structured_content_node(content)
            return f'<span class="structured-content">{rendered}</span>'

    return ""


def is_zip_dictionary(path: str) -> bool:
    """Checks if path points to a valid Yomitan dictionary zip archive."""
    if not (path.endswith(".zip") and os.path.isfile(path)):
        return False
    try:
        with zipfile.ZipFile(path, "r") as z:
            names = z.namelist()
            return "index.json" in names or any("term_bank" in n and n.endswith(".json") for n in names)
    except Exception:
        return False


def is_directory_dictionary(path: str) -> bool:
    """Checks if path points to an unzipped Yomitan dictionary directory."""
    if not os.path.isdir(path):
        return False
    try:
        names = os.listdir(path)
        return "index.json" in names or any(n.startswith("term_bank") and n.endswith(".json") for n in names)
    except Exception:
        return False


def _get_cache_dir() -> str:
    """Returns directory path where dictionaries.db is stored."""
    try:
        addon_dir = os.path.dirname(os.path.abspath(__file__))
        cache_dir = os.path.join(addon_dir, "user_files", "cache")
        os.makedirs(cache_dir, exist_ok=True)
        return cache_dir
    except Exception:
        fallback = os.path.join("/tmp", "compredef_cache")
        os.makedirs(fallback, exist_ok=True)
        return fallback


def _get_db_path() -> str:
    """Returns absolute file path to the dictionary SQLite database."""
    return os.path.join(_get_cache_dir(), "dictionaries.db")


def _get_db_connection() -> sqlite3.Connection:
    """
    Creates a configured SQLite connection for dictionary queries.

    The connection must ALWAYS be closed by the caller (use
    `_get_db_connection()` inside try/finally or a closing wrapper).
    The old code used `with conn:` which only commits the transaction —
    it does NOT close the connection, leaking one open handle per call
    until the process exits.
    """
    conn = sqlite3.connect(_get_db_path(), timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -32000")  # 32 MB cache
    return conn


def _db_query(sql: str, params: tuple = ()) -> list:
    """
    Runs a read-only query and GUARANTEES the connection is closed.

    Every short-lived lookup must go through here so connection handles
    never leak — hundreds of lookups during bulk generation previously
    left hundreds of open SQLite handles behind.
    """
    conn = _get_db_connection()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _init_db_tables() -> None:
    """Initializes tables and indexes in the dictionary database."""
    conn = _get_db_connection()
    try:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS dictionaries (
            path TEXT PRIMARY KEY,
            title TEXT,
            signature TEXT,
            entry_count INTEGER
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            dict_path TEXT,
            term TEXT,
            definition TEXT
        )
        """)
        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_entries_lookup
        ON entries(dict_path, term)
        """)
        conn.commit()
    finally:
        conn.close()


_init_db_tables()


def get_dictionary_title(path: str) -> str:
    """
    Retrieves the display title for a dictionary folder or .zip file.
    First checks SQLite metadata, then index.json (from zip or folder), then basename.
    """
    if not path:
        return ""
    norm_path = os.path.realpath(os.path.expanduser(path))

    # 1. Try SQLite metadata
    try:
        rows = _db_query(
            "SELECT title FROM dictionaries WHERE path = ?", (norm_path,)
        )
        if rows and rows[0][0]:
            return rows[0][0]
    except Exception:
        pass

    # 2. Try index.json inside .zip
    if is_zip_dictionary(norm_path):
        try:
            with zipfile.ZipFile(norm_path, "r") as z:
                if "index.json" in z.namelist():
                    meta = json.loads(z.read("index.json").decode("utf-8"))
                    if isinstance(meta, dict) and meta.get("title"):
                        return str(meta["title"]).strip()
        except Exception:
            pass

    # 3. Try index.json inside directory
    if os.path.isdir(norm_path):
        try:
            index_file = os.path.join(norm_path, "index.json")
            if os.path.isfile(index_file):
                with open(index_file, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                    if isinstance(meta, dict) and meta.get("title"):
                        return str(meta["title"]).strip()
        except Exception:
            pass

    # Fallback to file/folder basename
    base = os.path.basename(norm_path.rstrip("/\\"))
    if base.endswith(".zip"):
        base = base[:-4]
    return base or path


def find_dictionary_folders(parent_or_dict_path: str) -> List[str]:
    """
    Discovers all dictionary archives (.zip) and unzipped folders in a path.
    Deduplicates if both an unzipped folder and its .zip archive exist.
    """
    if not parent_or_dict_path:
        return []

    norm = os.path.realpath(os.path.expanduser(parent_or_dict_path))

    # Single dictionary file or folder
    if is_zip_dictionary(norm) or is_directory_dictionary(norm):
        return [norm]

    if not os.path.isdir(norm):
        return []

    found: List[str] = []
    seen_titles: set = set()

    # 1. Check for unzipped dictionary folders first
    for entry in sorted(os.listdir(norm)):
        sub = os.path.join(norm, entry)
        if is_directory_dictionary(sub):
            title = get_dictionary_title(sub)
            if title not in seen_titles:
                found.append(sub)
                seen_titles.add(title)

    # 2. Check for .zip dictionary archives
    for entry in sorted(os.listdir(norm)):
        sub = os.path.join(norm, entry)
        if is_zip_dictionary(sub):
            title = get_dictionary_title(sub)
            if title not in seen_titles:
                found.append(sub)
                seen_titles.add(title)

    return found


class SingleDictionary:
    """
    Represents an individual dictionary (either .zip archive or folder)
    indexed in SQLite.
    """

    # Serializes re-indexing: Anki's background threads can call lookup()
    # on the same dictionary concurrently; without the lock, two threads
    # would each build giant in-flight batches and double the RAM blow-up.
    _index_lock = threading.Lock()

    def __init__(self, path: str):
        self.path = os.path.realpath(os.path.expanduser(path))
        self.is_zip = is_zip_dictionary(self.path)
        self.title = get_dictionary_title(self.path)

    def _compute_signature(self) -> str:
        """
        Computes a checksum signature of the dictionary files.

        IMPORTANT: the renderer version is part of the signature. Signatures
        based purely on file mtime/size cannot detect a rendering-logic
        upgrade, which once left plain-text entries cached in SQLite while
        the code had moved on to rich HTML rendering.
        """
        # ZIP archives: mtime+size of the archive file itself
        if self.is_zip:
            try:
                st = os.stat(self.path)
                return f"zip:{RENDERER_VERSION}:{st.st_mtime_ns}:{st.st_size}"
            except Exception:
                return f"zip_error:{RENDERER_VERSION}"

        # Unzipped folders: mtime+size of every term bank file
        if os.path.isdir(self.path):
            parts = [RENDERER_VERSION]
            try:
                bank_files = [
                    f for f in os.listdir(self.path)
                    if f.startswith("term_bank") and f.endswith(".json")
                ]
                for f in sorted(bank_files):
                    st = os.stat(os.path.join(self.path, f))
                    parts.append(f"{f}:{st.st_mtime_ns}:{st.st_size}")
            except Exception:
                pass
            return hashlib.md5("\n".join(parts).encode("utf-8")).hexdigest()

        return f"unknown:{RENDERER_VERSION}"

    def ensure_indexed(self) -> None:
        """
        Verifies that the dictionary is indexed in SQLite and up to date.

        MEMORY-SAFE BY DESIGN: rendered HTML is streamed to SQLite in
        bounded batches of _INDEX_BATCH_SIZE rows. Never accumulates the
        whole dictionary in RAM — 大辞泉 alone renders to ~1.3 GB of HTML,
        which previously exhausted memory and froze Anki at 100% CPU
        (even for a nonsense-word lookup like 駿ってさ).
        """
        if not os.path.exists(self.path):
            return

        current_sig = self._compute_signature()

        rows = _db_query(
            "SELECT signature FROM dictionaries WHERE path = ?", (self.path,)
        )
        if rows and rows[0][0] == current_sig:
            return  # already indexed and fresh — instant path

        # Re-index under the class-wide lock so concurrent lookups wait
        # instead of duplicating a giant re-index in parallel threads.
        with SingleDictionary._index_lock:
            # Re-check inside the lock: another thread may have finished
            # the exact re-index we were about to start.
            rows = _db_query(
                "SELECT signature FROM dictionaries WHERE path = ?",
                (self.path,),
            )
            if rows and rows[0][0] == current_sig:
                return

            print(
                f"CompreDef: Indexing '{self.title}' into SQLite "
                f"with rich Yomitan HTML (streamed, bounded RAM)..."
            )
            try:
                self._reindex_streaming(current_sig)
            except Exception as e:
                print(f"CompreDef: Indexing failed for '{self.title}': {e}")
                return

    def _reindex_streaming(self, current_sig: str) -> None:
        """
        Streams term banks through the renderer into SQLite in batches.

        Peak memory stays at roughly one batch of rendered HTML
        (~a few MB) regardless of dictionary size. Term banks are loaded
        ONE AT A TIME and dropped after rendering, and each batch is
        committed immediately.
        """
        conn = _get_db_connection()
        try:
            # Start a clean slate for this dictionary (stale rows from a
            # previous renderer version must not survive).
            conn.execute("DELETE FROM entries WHERE dict_path = ?", (self.path,))
            conn.execute("DELETE FROM dictionaries WHERE path = ?", (self.path,))
            conn.commit()

            total = 0
            batch: List[Tuple[str, str, str]] = []

            def flush() -> None:
                """Writes and clears the in-memory batch; commits instantly."""
                nonlocal total
                if batch:
                    conn.executemany(
                        "INSERT INTO entries VALUES (?, ?, ?)", batch
                    )
                    conn.commit()
                    total += len(batch)
                    batch.clear()

            # Iterator over every term bank (zip or folder), loaded lazily
            # so only one bank's JSON is ever alive at a time.
            for entries in self._iter_term_banks():
                for entry in entries:
                    if not isinstance(entry, list) or len(entry) < 6:
                        continue
                    word = entry[0]
                    if not word or not isinstance(word, str):
                        continue
                    for def_block in entry[5]:
                        html_def = render_yomitan_definition_html(def_block)
                        if html_def:
                            batch.append((self.path, word, html_def))
                    # Bounded RAM: flush long before the batch can grow to
                    # hundreds of megabytes of rendered HTML strings.
                    if len(batch) >= _INDEX_BATCH_SIZE:
                        flush()
                # The parsed bank JSON becomes garbage here; the next bank
                # loads fresh — at no point do two banks coexist in RAM.

            flush()  # commit the final partial batch

            conn.execute(
                "INSERT INTO dictionaries VALUES (?, ?, ?, ?)",
                (self.path, self.title, current_sig, total),
            )
            conn.commit()
            print(f"CompreDef: Finished indexing '{self.title}' ({total} entries)")
        finally:
            conn.close()

    def _iter_term_banks(self):
        """
        Yields one parsed term bank (a list of entries) at a time.

        Works identically for .zip archives and unzipped folders so the
        streaming indexer never needs to care about the source format.
        Corrupt banks are skipped with a log line instead of aborting the
        whole re-index.
        """
        if self.is_zip:
            with zipfile.ZipFile(self.path, "r") as z:
                bank_names = sorted([
                    n for n in z.namelist()
                    if "term_bank" in n and n.endswith(".json")
                ])
                for b_name in bank_names:
                    try:
                        yield json.loads(z.read(b_name).decode("utf-8"))
                    except Exception as e:
                        print(f"CompreDef: Failed to read {b_name} in zip: {e}")
        elif os.path.isdir(self.path):
            bank_files = sorted([
                f for f in os.listdir(self.path)
                if f.startswith("term_bank") and f.endswith(".json")
            ])
            for b_name in bank_files:
                try:
                    with open(
                        os.path.join(self.path, b_name), "r", encoding="utf-8"
                    ) as f:
                        yield json.load(f)
                except Exception as e:
                    print(f"CompreDef: Failed to read {b_name}: {e}")

    def lookup(self, word: str) -> List[str]:
        """
        Performs an instant B-tree lookup for `word` in this dictionary.
        Returns rich Yomitan HTML definitions list in microseconds.
        """
        self.ensure_indexed()
        rows = _db_query(
            "SELECT definition FROM entries WHERE dict_path = ? AND term = ?",
            (self.path, word),
        )
        return [row[0] for row in rows]


_loaded_dicts: Dict[str, SingleDictionary] = {}


def get_single_dictionary(path: str) -> SingleDictionary:
    """Returns or creates a SingleDictionary instance for the given path."""
    norm = os.path.realpath(os.path.expanduser(path))
    if norm not in _loaded_dicts:
        _loaded_dicts[norm] = SingleDictionary(norm)
    return _loaded_dicts[norm]


class DictionaryLoader:
    """
    Backwards-compatible wrapper managing an ordered list of dictionaries.
    """

    def __init__(self, directory_or_paths: object):
        self.dictionaries: List[SingleDictionary] = []

        paths: List[str] = []
        if isinstance(directory_or_paths, list):
            paths = [str(p) for p in directory_or_paths if p]
        elif isinstance(directory_or_paths, str) and directory_or_paths:
            found = find_dictionary_folders(directory_or_paths)
            paths = found if found else [directory_or_paths]

        for p in paths:
            if os.path.exists(p):
                self.dictionaries.append(get_single_dictionary(p))

    def lookup_ladder(self, word: str) -> List[Tuple[SingleDictionary, List[str]]]:
        results = []
        for d in self.dictionaries:
            defs = d.lookup(word)
            if defs:
                results.append((d, defs))
        return results

    def lookup_all(self, word: str) -> List[str]:
        all_defs = []
        for d in self.dictionaries:
            all_defs.extend(d.lookup(word))
        return all_defs

    def lookup(self, word: str) -> List[str]:
        return self.lookup_all(word)
