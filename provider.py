import abc
import os
import json
import sqlite3
import hashlib
import zipfile
import threading
import re
from typing import List, Optional, Callable, Any, Dict

# Dual-context sibling imports (relative inside Anki's package load,
# absolute in the top-level test harness — see core.py for why).
if __package__:
    from .models import DictionaryEntry
    from .renderer import render_yomitan_definition_html
else:
    from models import DictionaryEntry
    from renderer import render_yomitan_definition_html

class IndexingError(Exception):
    """Raised when dictionary installation/indexing fails."""

class DictionaryProvider(abc.ABC):
    """Interface for dictionary lookups and management."""
    
    @abc.abstractmethod
    def lookup(self, word: str, reading: str = "") -> List[DictionaryEntry]:
        """Find definitions for a word, optionally filtered by reading."""
        pass

    @abc.abstractmethod
    def get_title(self, path: str) -> str:
        """Get the display title for a dictionary path."""
        pass

    @abc.abstractmethod
    def is_installed(self, path: str) -> bool:
        """Check if the dictionary at path is indexed/installed."""
        pass

    @abc.abstractmethod
    def get_entry_count(self, path: str) -> int:
        """Get number of indexed entries."""
        pass

    @abc.abstractmethod
    def install(self, path: str, progress_cb: Optional[Callable[[int, int], None]] = None, cancel_check: Optional[Callable[[], bool]] = None) -> int:
        """Parse and index a dictionary."""
        pass

    @abc.abstractmethod
    def uninstall(self, path: str) -> None:
        """Remove dictionary index."""
        pass

    @abc.abstractmethod
    def is_index_current(self, path: str) -> bool:
        """Check if index matches source files."""
        pass

class LocalSQLiteProvider(DictionaryProvider):
    """Implementation of DictionaryProvider using local SQLite indexing."""
    
    RENDERER_VERSION = "yomitan_html_v2_reading"
    _INDEX_BATCH_SIZE = 5000
    _install_lock = threading.Lock()

    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        self.db_path = os.path.join(cache_dir, "dictionaries.db")
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA cache_size = -32000")
        return conn

    def _init_db(self) -> None:
        conn = self._get_conn()
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
                definition TEXT,
                reading TEXT
            )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_lookup ON entries(dict_path, term)")
            cols = {row[1] for row in conn.execute("PRAGMA table_info(entries)")}
            if "reading" not in cols:
                conn.execute("ALTER TABLE entries ADD COLUMN reading TEXT DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_reading ON entries(dict_path, term, reading)")
            conn.commit()
        finally:
            conn.close()

    def _db_query(self, sql: str, params: tuple = ()) -> list:
        conn = self._get_conn()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def get_title(self, path: str) -> str:
        norm_path = os.path.realpath(os.path.expanduser(path))
        rows = self._db_query("SELECT title FROM dictionaries WHERE path = ?", (norm_path,))
        if rows and rows[0][0]:
            return rows[0][0]
        
        if self._is_zip(norm_path):
            try:
                with zipfile.ZipFile(norm_path, "r") as z:
                    if "index.json" in z.namelist():
                        meta = json.loads(z.read("index.json").decode("utf-8"))
                        if isinstance(meta, dict) and meta.get("title"):
                            return str(meta["title"]).strip()
            except Exception: pass
        elif os.path.isdir(norm_path):
            try:
                idx = os.path.join(norm_path, "index.json")
                if os.path.isfile(idx):
                    with open(idx, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                        if isinstance(meta, dict) and meta.get("title"):
                            return str(meta["title"]).strip()
            except Exception: pass
        
        base = os.path.basename(norm_path.rstrip("/\\"))
        if base.endswith(".zip"): base = base[:-4]
        return base or path

    def is_installed(self, path: str) -> bool:
        norm = os.path.realpath(os.path.expanduser(path))
        rows = self._db_query("SELECT entry_count FROM dictionaries WHERE path = ?", (norm,))
        return bool(rows and rows[0][0] is not None)

    def get_entry_count(self, path: str) -> int:
        norm = os.path.realpath(os.path.expanduser(path))
        rows = self._db_query("SELECT entry_count FROM dictionaries WHERE path = ?", (norm,))
        return int(rows[0][0]) if rows and rows[0][0] is not None else 0

    def is_index_current(self, path: str) -> bool:
        norm = os.path.realpath(os.path.expanduser(path))
        rows = self._db_query("SELECT signature FROM dictionaries WHERE path = ?", (norm,))
        return bool(rows and rows[0][0] == self._compute_signature(norm))

    def _compute_signature(self, path: str) -> str:
        if self._is_zip(path):
            try:
                st = os.stat(path)
                return f"zip:{self.RENDERER_VERSION}:{st.st_mtime_ns}:{st.st_size}"
            except Exception: return f"zip_error:{self.RENDERER_VERSION}"
        if os.path.isdir(path):
            parts = [self.RENDERER_VERSION]
            try:
                bank_files = sorted([f for f in os.listdir(path) if f.startswith("term_bank") and f.endswith(".json")])
                for f in bank_files:
                    st = os.stat(os.path.join(path, f))
                    parts.append(f"{f}:{st.st_mtime_ns}:{st.st_size}")
            except Exception: pass
            # FIX: Using md5 on the string is fine, but we must ensure the string includes
            # the RENDERER_VERSION. The original code did this via `parts = [self.RENDERER_VERSION]`.
            # The test fails because it expects the signature to change.
            return hashlib.md5("\n".join(parts).encode("utf-8")).hexdigest()
        return f"unknown:{self.RENDERER_VERSION}"

    def uninstall(self, path: str) -> None:
        norm = os.path.realpath(os.path.expanduser(path))
        conn = self._get_conn()
        try:
            conn.execute("DELETE FROM entries WHERE dict_path = ?", (norm,))
            conn.execute("DELETE FROM dictionaries WHERE path = ?", (norm,))
            conn.commit()
        finally:
            conn.close()

    def lookup(self, word: str, reading: str = "") -> List[DictionaryEntry]:
        # This method is usually called via a wrapper that provides the path.
        # For the interface to work, we need to know WHICH dictionary to query.
        # However, in the current logic, generator.py iterates paths.
        # To keep the interface clean, lookup should probably take (path, word, reading).
        # But if the provider is 'single', it knows its path.
        # Let's refine the interface: the provider represents the WHOLE system.
        # So lookup(path, word, reading) is more accurate.
        raise NotImplementedError("Use lookup_by_path instead")

    def lookup_by_path(self, path: str, word: str, reading: str = "") -> List[DictionaryEntry]:
        norm = os.path.realpath(os.path.expanduser(path))
        title = self.get_title(norm)
        
        if reading:
            norm_r = self._normalize_reading(reading)
            rows = self._db_query("SELECT definition FROM entries WHERE dict_path = ? AND term = ? AND reading = ?", (norm, word, norm_r))
            if rows: return [DictionaryEntry(word, reading, r[0], title, norm) for r in rows]
            rows = self._db_query("SELECT definition FROM entries WHERE dict_path = ? AND term = ? AND reading = ''", (norm, word))
            if rows: return [DictionaryEntry(word, reading, r[0], title, norm) for r in rows]
            rows = self._db_query("SELECT definition FROM entries WHERE dict_path = ? AND term = ?", (norm, word))
            return [DictionaryEntry(word, reading, r[0], title, norm) for r in rows]
        
        rows = self._db_query("SELECT definition FROM entries WHERE dict_path = ? AND term = ?", (norm, word))
        return [DictionaryEntry(word, reading, r[0], title, norm) for r in rows]

    def install(self, path: str, progress_cb: Optional[Callable[[int, int], None]] = None, cancel_check: Optional[Callable[[], bool]] = None) -> int:
        norm = os.path.realpath(os.path.expanduser(path))
        if not os.path.exists(norm): raise IndexingError(f"Dict not found: {norm}")
        
        with self._install_lock:
            sig = self._compute_signature(norm)
            rows = self._db_query("SELECT signature FROM dictionaries WHERE path = ?", (norm,))
            if rows and rows[0][0] == sig and self.is_installed(norm):
                return self.get_entry_count(norm)

            conn = self._get_conn()
            try:
                conn.execute("DELETE FROM entries WHERE dict_path = ?", (norm,))
                conn.execute("DELETE FROM dictionaries WHERE path = ?", (norm,))
                conn.commit()

                total = 0
                batch = []
                title = self.get_title(norm)

                def flush():
                    nonlocal total
                    if batch:
                        conn.executemany("INSERT INTO entries (dict_path, term, definition, reading) VALUES (?, ?, ?, ?)", batch)
                        conn.commit()
                        total += len(batch)
                        batch.clear()

                grand_total = [0]
                for entries in self._iter_term_banks(norm):
                    grand_total[0] += len(entries)
                
                if grand_total[0] == 0: raise IndexingError("No entries found")

                for entries in self._iter_term_banks(norm):
                    for entry in entries:
                        if cancel_check and cancel_check():
                            conn.execute("DELETE FROM entries WHERE dict_path = ?", (norm,))
                            conn.commit()
                            raise IndexingError("Cancelled")
                        if not isinstance(entry, list) or len(entry) < 6: continue
                        word = entry[0]
                        if not word or not isinstance(word, str): continue
                        reading = entry[1] if isinstance(entry[1], str) else ""
                        for def_block in entry[5]:
                            html_def = render_yomitan_definition_html(def_block)
                            if html_def:
                                batch.append((norm, word, html_def, self._normalize_reading(reading)))
                        if len(batch) >= self._INDEX_BATCH_SIZE:
                            flush()
                            if progress_cb: progress_cb(total, grand_total[0])
                
                flush()
                if progress_cb: progress_cb(total, grand_total[0])
                conn.execute("INSERT INTO dictionaries VALUES (?, ?, ?, ?)", (norm, title, sig, total))
                conn.commit()
                return total
            finally:
                conn.close()

    def _iter_term_banks(self, path: str):
        if self._is_zip(path):
            with zipfile.ZipFile(path, "r") as z:
                banks = sorted([n for n in z.namelist() if "term_bank" in n and n.endswith(".json")])
                for b in banks: yield json.loads(z.read(b).decode("utf-8"))
        elif os.path.isdir(path):
            banks = sorted([f for f in os.listdir(path) if f.startswith("term_bank") and f.endswith(".json")])
            for b in banks:
                with open(os.path.join(path, b), "r", encoding="utf-8") as f:
                    yield json.load(f)
        else: raise Exception("Not a dict")

    def _is_zip(self, path: str) -> bool:
        if not (path.endswith(".zip") and os.path.isfile(path)): return False
        try:
            with zipfile.ZipFile(path, "r") as z:
                names = z.namelist()
                return "index.json" in names or any("term_bank" in n and n.endswith(".json") for n in names)
        except Exception: return False

    def _normalize_reading(self, reading: str) -> str:
        if not reading: return ""
        out = []
        for ch in reading:
            code = ord(ch)
            if 0x30A1 <= code <= 0x30F6 or 0x30FD <= code <= 0x30FC:
                out.append(chr(code - 0x60))
            else:
                out.append(ch)
        return re.sub(r"[\s\-・.。_ー()()「」【】]", "", "".join(out))
