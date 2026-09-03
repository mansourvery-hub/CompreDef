"""
parser.py - SQLite-backed dictionary storage and Yomitan HTML renderer for CompreDef.

ARCHITECTURE (install-time indexing):

    INSTALL DICTIONARY
           |
       parse once
           |
     build SQLite index
           |
       save index
           |
         DONE

    Then forever after:

    GENERATE DEFINITION
           |
     SQLite lookup (pure DB query, no parsing)
           |
       combine definitions

Key rules (these fix the historical first-use freeze / 100% CPU bugs):
- `lookup()` is a PURE database query. It NEVER parses dictionary files,
  NEVER calls ensure_indexed(), and NEVER triggers re-indexing.
- Indexing happens exactly once per dictionary, when the user installs/
  replaces it via the config GUI (`install_dictionary()`), as a background
  operation with progress reporting.
- Indexes persist in `user_files/cache/dictionaries.db` across Anki and
  machine restarts. A dictionary is re-indexed ONLY when the user explicitly
  (re-)installs/replaces it.
- Indexing is streamed in bounded batches: giant dictionaries (大辞泉:
  632,876 entries ≈ 1.3 GB of HTML) never accumulate in RAM.
"""

import json
import os
import re
import html
import hashlib
import sqlite3
import threading
import zipfile
from typing import Callable, Dict, List, Optional, Tuple, Any

# Regular expressions for stripping ruby and HTML tags when extracting plain
# text (used for kanji scoring and cleaning note fields).
_RT_RE = re.compile(r'<rt\b[^>]*>.*?</rt>', flags=re.DOTALL | re.IGNORECASE)
_RP_RE = re.compile(r'<rp\b[^>]*>.*?</rp>', flags=re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r'<[^>]+>')

# Bump this whenever the HTML rendering output format changes in ANY way.
# It is embedded in each dictionary's index signature so a renderer upgrade
# invalidates stale SQLite entries instead of silently serving old
# plain-text definitions forever (the historical '先ず 121-char bug').
RENDERER_VERSION = "yomitan_html_v2_reading"

# How many rendered definitions to hold in memory before flushing to SQLite.
# Bounded RAM: giant dictionaries must NEVER be accumulated in one list —
# that exhausted memory, drove the system into swap thrash and froze Anki
# at 100% CPU (the historical 駿ってさ freeze).
_INDEX_BATCH_SIZE = 5000


# ---------------------------------------------------------------------------
# Plain-text extraction helpers
# ---------------------------------------------------------------------------

def _extract_base_text(html_or_text: str) -> str:
    """
    Extracts visible base text from HTML, stripping ruby furigana (<rt> tags).
    """
    if not html_or_text:
        return ""
    no_rt = _RT_RE.sub("", html_or_text)
    no_rp = _RP_RE.sub("", no_rt)
    plain = _TAG_RE.sub("", no_rp)
    return html.unescape(plain).strip()


def extract_clean_word(field_text: str) -> str:
    """
    Extracts the clean target word/expression from a note field that may
    contain HTML markup, ruby tags, or bracketed furigana.

    Handles:
    - '<div>先[ま]ず</div>'          -> '先ず'
    - '<ruby>先<rt>ま</rt></ruby>ず'  -> '先ず'
    - '先ず[まず]'                   -> '先ず'
    - '&lt;食&gt;'                   -> '<食>'
    - ' 食[た]べる '                 -> '食べる'
    """
    if not field_text:
        return ""

    text = field_text.strip()
    if not text:
        return ""

    # Remove ruby furigana reading elements FIRST, while they are real tags.
    text = _RT_RE.sub("", text)
    text = _RP_RE.sub("", text)

    # Strip remaining HTML tags BEFORE unescaping entities — otherwise
    # '&lt;食&gt;' would unescape to '<食>' and then be mistaken for a tag.
    text = _TAG_RE.sub("", text).strip()

    # Unescape entities only after tag stripping (&nbsp;, &lt;, etc.)
    text = html.unescape(text).strip()

    # Furigana bracket formats
    if "[" in text or "［" in text:
        s = text.replace("［", "[").replace("］", "]")

        # Whole-word trailing bracket: '先ず[まず]' -> '先ず'
        whole = re.fullmatch(r"([^\[\]]+)\[([^\[\]]+)\]", s)
        if whole:
            text = whole.group(1).strip()
        else:
            # Per-run bracket: '先[ま]ず' -> '先ず'
            text = re.sub(r"\[[^\]]*\]", "", s).strip()

    return text


# ---------------------------------------------------------------------------
# Yomitan HTML rendering (used ONLY at install-time indexing)
# ---------------------------------------------------------------------------

def _style_to_css(style: dict) -> str:
    """
    Converts a Yomitan structured-content style dictionary into an inline CSS
    string: camelCase -> kebab-case, numeric em dimensions handled like
    Yomitan does.
    """
    if not isinstance(style, dict):
        return ""

    css_rules = []
    for prop, val in style.items():
        kebab = "".join(["-" + c.lower() if c.isupper() else c for c in prop]).lstrip("-")

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
    - Structured content: `<span class="structured-content">{rendered}</span>`
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


# ---------------------------------------------------------------------------
# Dictionary format detection
# ---------------------------------------------------------------------------

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
    Creates a configured SQLite connection.

    The connection must ALWAYS be closed by the caller (use inside
    try/finally or via _db_query). `with conn:` only commits — it does
    NOT close, leaking one handle per call.
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

        # MIGRATION v2: older caches lacked the `reading` column; add it so
        # homographs (先ず: まず vs せんず) can be disambiguated.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(entries)")}
        if "reading" not in cols:
            conn.execute("ALTER TABLE entries ADD COLUMN reading TEXT DEFAULT ''")
        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_entries_reading
        ON entries(dict_path, term, reading)
        """)

        conn.commit()
    finally:
        conn.close()


_init_db_tables()


# ---------------------------------------------------------------------------
# Reading normalization (katakana -> hiragana etc.)
# ---------------------------------------------------------------------------

# Katakana -> hiragana offset: katakana block starts at U+30A1 (ァ),
# hiragana at U+3041 (ぁ). Shifting by 0x60 converts マズ to まず.
_KATAKANA_TO_HIRAGANA_OFFSET = 0x60


def normalize_reading(reading: str) -> str:
    """
    Normalizes a reading to plain hiragana for robust comparison.

    Katakana -> hiragana, all whitespace and separator characters
    (・, -, ., _) removed so せん-ず == せんず.
    """
    if not reading:
        return ""
    out = []
    for ch in reading:
        code = ord(ch)
        # Full-width katakana (with/without voiced marks) -> hiragana
        if 0x30A1 <= code <= 0x30F6 or 0x30FD <= code <= 0x30FC:
            out.append(chr(code - _KATAKANA_TO_HIRAGANA_OFFSET))
        else:
            out.append(ch)
    joined = "".join(out)
    return re.sub(r"[\s\-・.。_ー()()「」【】]", "", joined)


def parse_furigana_field(field_text: str) -> str:
    """
    Extracts the pure kana reading from a note field that may contain
    furigana markup.

    Handles:
    - '先[ま]ず'                     -> 'まず'   (per-kanji brackets)
    - '先ず[まず]'                   -> 'まず'   (whole-word bracket)
    - '<ruby>先<rt>ま</rt></ruby>ず' -> 'まず'   (HTML ruby)
    - 'マズ'                          -> 'まず'   (plain katakana)
    - '先ず'                          -> ''        (no reading info)
    """
    if not field_text:
        return ""

    text = field_text.strip()

    # HTML ruby: each <ruby>BASE<rt>READING</rt></ruby> chunk is replaced
    # by its rt reading, yielding pure kana.
    if "<ruby" in text or "<rt" in text:
        def _ruby_sub(match: re.Match) -> str:
            inner = match.group(0)
            rt = re.search(r"<rt\b[^>]*>(.*?)</rt>", inner, flags=re.DOTALL)
            return re.sub(r"<[^>]+>", "", rt.group(1)) if rt else ""

        kana = re.sub(
            r"<ruby\b[^>]*>.*?</ruby>", _ruby_sub, text, flags=re.DOTALL
        )
        kana = re.sub(r"<[^>]+>", "", kana)
        return normalize_reading(kana)

    # Bracket formats
    if "[" in text or "［" in text:
        s = text.replace("［", "[").replace("］", "]")

        # Whole-word form: a single bracket covering the whole string.
        whole = re.fullmatch(r"([^\[\]]+)\[([^\[\]]+)\]", s)
        if whole:
            return normalize_reading(whole.group(2))

        # Per-run form: bracket content replaces the kanji run before it.
        result: list = []
        tokens = re.split(r"(\[[^\]]*\])", s)
        for part in tokens:
            if part.startswith("[") and part.endswith("]"):
                if result:
                    prev = result[-1]
                    trimmed = re.sub(r"[\u4e00-\u9fff]+$", "", prev)
                    result[-1] = trimmed
                result.append(part[1:-1])
            else:
                result.append(part)
        return normalize_reading("".join(result))

    # Plain kana / katakana only
    if re.fullmatch(r"[\u3040-\u30ff\u30fc\s\-・]+", text):
        return normalize_reading(text)

    # Kanji without readings -> nothing usable
    if not re.search(r"[\u3040-\u30ff]", text):
        return ""

    # Mixed text without brackets (e.g. '行く'): no reading info
    if re.search(r"[\u4e00-\u9fff]", text):
        return ""

    return normalize_reading(text)


# ---------------------------------------------------------------------------
# Dictionary metadata
# ---------------------------------------------------------------------------

def get_dictionary_title(path: str) -> str:
    """
    Retrieves the display title for a dictionary folder or .zip file.
    First checks SQLite metadata, then index.json (zip or folder), then basename.
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
    Deduplicates when both an unzipped folder and its .zip archive exist.
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

    # 1. Unzipped dictionary folders first
    for entry in sorted(os.listdir(norm)):
        sub = os.path.join(norm, entry)
        if is_directory_dictionary(sub):
            title = get_dictionary_title(sub)
            if title not in seen_titles:
                found.append(sub)
                seen_titles.add(title)

    # 2. Then .zip archives
    for entry in sorted(os.listdir(norm)):
        sub = os.path.join(norm, entry)
        if is_zip_dictionary(sub):
            title = get_dictionary_title(sub)
            if title not in seen_titles:
                found.append(sub)
                seen_titles.add(title)

    return found


# ---------------------------------------------------------------------------
# INSTALL-TIME INDEXING (the ONLY place dictionaries are ever parsed)
# ---------------------------------------------------------------------------

class IndexingError(Exception):
    """Raised when dictionary installation/indexing fails."""


class SingleDictionary:
    """
    Represents an individual dictionary (zip archive or folder).

    Lifecycle:
    - The user installs a dictionary via the config GUI -> `install()` runs
      ONCE as a background operation (parsing + SQLite writing).
    - After that, `lookup()` is a pure SQLite query. It never parses files.
    """

    _install_lock = threading.Lock()

    def __init__(self, path: str):
        self.path = os.path.realpath(os.path.expanduser(path))
        self.is_zip = is_zip_dictionary(self.path)
        self.title = get_dictionary_title(self.path)

    # -- Index state ------------------------------------------------------

    def is_indexed(self) -> bool:
        """
        Returns True when a COMPLETE index for this dictionary exists in the
        SQLite database (a `dictionaries` marker row is written ONLY after
        all entries have been streamed in — partial/crashed indexes have no
        marker and are therefore never trusted).
        """
        rows = _db_query(
            "SELECT entry_count FROM dictionaries WHERE path = ?", (self.path,)
        )
        return bool(rows and rows[0][0] is not None)

    def entry_count(self) -> int:
        """Number of indexed entries (0 when not indexed)."""
        rows = _db_query(
            "SELECT entry_count FROM dictionaries WHERE path = ?", (self.path,)
        )
        return int(rows[0][0]) if rows and rows[0][0] is not None else 0

    # -- Signature (install-time identity) ---------------------------------

    def _compute_signature(self) -> str:
        """
        Computes an identity signature of the dictionary's source files.

        Used ONLY during installation to decide whether the installed index
        belongs to the current files or to an older version of them. It is
        deliberately NOT consulted by lookup() — normal generation must be
        a pure database query with zero filesystem work.
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

    def index_is_current(self) -> bool:
        """
        True when the stored index was built from the exact source files
        currently on disk (same signature). Checked when the dictionary list
        is edited in the config GUI — never during normal lookups.
        """
        rows = _db_query(
            "SELECT signature FROM dictionaries WHERE path = ?", (self.path,)
        )
        return bool(rows and rows[0][0] == self._compute_signature())

    # -- Installation ------------------------------------------------------

    def install(
        self,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> int:
        """
        Parses the dictionary files and builds its SQLite index.

        This is THE one-time, install-time parse. It is normally invoked by
        `install_dictionary()` (background, with progress). After this
        completes, the dictionary is never parsed again — lookups are pure
        SQL against the built index.

        Args:
            progress_cb: optional callback(done, total) for progress UI.
            cancel_check: optional callable returning True to abort.

        Returns the number of indexed entries.

        Raises:
            IndexingError: on unreadable/corrupt dictionaries or cancellation.
        """
        if not os.path.exists(self.path):
            raise IndexingError(f"Dictionary not found: {self.path}")

        # Serialize installations: Anki's background threads can install
        # multiple dictionaries; without the lock two threads would each
        # build giant in-flight batches and double the RAM blow-up.
        with SingleDictionary._install_lock:
            signature = self._compute_signature()

            # Already installed from exactly these files? Nothing to do —
            # this makes re-opening the config dialog idempotent.
            rows = _db_query(
                "SELECT signature FROM dictionaries WHERE path = ?", (self.path,)
            )
            if rows and rows[0][0] == signature and self.is_indexed():
                return self.entry_count()

            conn = _get_db_connection()
            try:
                # Clean slate for this dictionary: rows from a previous
                # version (or a crashed partial index) must not survive.
                conn.execute("DELETE FROM entries WHERE dict_path = ?", (self.path,))
                conn.execute("DELETE FROM dictionaries WHERE path = ?", (self.path,))
                conn.commit()

                total = 0
                batch: List[Tuple[str, str, str, str]] = []

                def flush() -> None:
                    """Writes and clears the in-memory batch; commits instantly."""
                    nonlocal total
                    if batch:
                        conn.executemany(
                            "INSERT INTO entries (dict_path, term, definition, reading) "
                            "VALUES (?, ?, ?, ?)",
                            batch,
                        )
                        conn.commit()
                        total += len(batch)
                        batch.clear()

                def report_progress() -> None:
                    if progress_cb is not None:
                        try:
                            progress_cb(total, grand_total[0])
                        except Exception:
                            pass  # progress reporting must never kill indexing

                # Total for progress: count entries first (cheap pass over
                # the JSON files' entry counts only).
                grand_total = [0]
                try:
                    for entries in self._iter_term_banks():
                        grand_total[0] += len(entries)
                except Exception as e:
                    raise IndexingError(f"Failed reading dictionary banks: {e}")

                if grand_total[0] == 0:
                    raise IndexingError(
                        f"No readable entries found in '{self.title}' "
                        f"(missing or corrupt term banks?)"
                    )

                # Second pass: render + stream into SQLite in bounded batches.
                for entries in self._iter_term_banks():
                    for entry in entries:
                        if cancel_check is not None and cancel_check():
                            # Roll back partial rows; no marker row is written,
                            # so this dictionary will not be trusted.
                            conn.execute("DELETE FROM entries WHERE dict_path = ?", (self.path,))
                            conn.commit()
                            raise IndexingError("Indexing cancelled by user")
                        if not isinstance(entry, list) or len(entry) < 6:
                            continue
                        word = entry[0]
                        if not word or not isinstance(word, str):
                            continue
                        # entry[1] is the Yomitan reading (e.g. まず for 先ず);
                        # stored so lookups can disambiguate homographs.
                        reading = entry[1] if isinstance(entry[1], str) else ""
                        for def_block in entry[5]:
                            html_def = render_yomitan_definition_html(def_block)
                            if html_def:
                                batch.append((self.path, word, html_def, normalize_reading(reading)))
                        # Bounded RAM: flush long before the batch can grow
                        # to hundreds of megabytes of rendered HTML.
                        if len(batch) >= _INDEX_BATCH_SIZE:
                            flush()
                            report_progress()

                flush()  # commit the final partial batch
                report_progress()

                # Marker row: written ONLY after every entry is committed.
                # Its presence is what makes is_indexed() trust the index.
                conn.execute(
                    "INSERT INTO dictionaries VALUES (?, ?, ?, ?)",
                    (self.path, self.title, signature, total),
                )
                conn.commit()
                print(f"CompreDef: Finished indexing '{self.title}' ({total} entries)")
                return total
            finally:
                conn.close()

    def _iter_term_banks(self):
        """
        Yields one parsed term bank (a list of entries) at a time.

        Works identically for .zip archives and unzipped folders so the
        streaming installer never needs to care about the source format.
        Corrupt banks raise (installation must fail loudly, not silently).
        """
        if self.is_zip:
            with zipfile.ZipFile(self.path, "r") as z:
                bank_names = sorted([
                    n for n in z.namelist()
                    if "term_bank" in n and n.endswith(".json")
                ])
                if not bank_names:
                    raise IndexingError(f"No term banks in {self.path}")
                for b_name in bank_names:
                    try:
                        yield json.loads(z.read(b_name).decode("utf-8"))
                    except Exception as e:
                        raise IndexingError(f"Failed to read {b_name} in zip: {e}")
        elif os.path.isdir(self.path):
            bank_files = sorted([
                f for f in os.listdir(self.path)
                if f.startswith("term_bank") and f.endswith(".json")
            ])
            if not bank_files:
                raise IndexingError(f"No term banks in {self.path}")
            for b_name in bank_files:
                try:
                    with open(
                        os.path.join(self.path, b_name), "r", encoding="utf-8"
                    ) as f:
                        yield json.load(f)
                except Exception as e:
                    raise IndexingError(f"Failed to read {b_name}: {e}")
        else:
            raise IndexingError(f"Not a dictionary: {self.path}")

    # -- Lookup (pure database query) --------------------------------------

    def lookup(self, word: str, reading: str = "") -> List[str]:
        """
        Instant B-tree lookup for `word` against the ALREADY-BUILT index.

        PURE DATABASE QUERY: this never parses dictionary files, never
        triggers indexing, and never touches the source dictionary. If the
        dictionary has not been installed yet, it simply returns [] — the
        user will be told to install it via the config GUI.

        When `reading` is supplied (hiragana OR katakana — normalized
        internally), entries whose stored reading matches win; fallback to
        reading-less entries; then reading-agnostic, so homographs like
        先ず(まず 'first') vs 先ず(せんず 'precede') resolve correctly
        while reading-less dictionaries still return results.
        """
        if reading:
            norm = normalize_reading(reading)
            rows = _db_query(
                "SELECT definition FROM entries "
                "WHERE dict_path = ? AND term = ? AND reading = ?",
                (self.path, word, norm),
            )
            if rows:
                return [row[0] for row in rows]
            rows = _db_query(
                "SELECT definition FROM entries "
                "WHERE dict_path = ? AND term = ? AND reading = ''",
                (self.path, word),
            )
            if rows:
                return [row[0] for row in rows]
            rows = _db_query(
                "SELECT definition FROM entries WHERE dict_path = ? AND term = ?",
                (self.path, word),
            )
            return [row[0] for row in rows]

        rows = _db_query(
            "SELECT definition FROM entries WHERE dict_path = ? AND term = ?",
            (self.path, word),
        )
        return [row[0] for row in rows]


# ---------------------------------------------------------------------------
# Module-level convenience API
# ---------------------------------------------------------------------------

_loaded_dicts: Dict[str, SingleDictionary] = {}


def get_single_dictionary(path: str) -> SingleDictionary:
    """Returns or creates a SingleDictionary instance for the given path."""
    norm = os.path.realpath(os.path.expanduser(path))
    if norm not in _loaded_dicts:
        _loaded_dicts[norm] = SingleDictionary(norm)
    return _loaded_dicts[norm]


def install_dictionary(
    path: str,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> int:
    """
    Installs (indexes) a dictionary. Thin module-level wrapper so callers
    don't need the SingleDictionary class. See SingleDictionary.install().
    """
    return get_single_dictionary(path).install(progress_cb, cancel_check)


def uninstall_dictionary(path: str) -> None:
    """
    Removes a dictionary's index from the SQLite database entirely.

    Called when the user removes a dictionary from the ladder in the config
    GUI. Without this, removed dictionaries left their indexes behind and
    kept contributing stale definitions.
    """
    norm = os.path.realpath(os.path.expanduser(path))
    conn = _get_db_connection()
    try:
        conn.execute("DELETE FROM entries WHERE dict_path = ?", (norm,))
        conn.execute("DELETE FROM dictionaries WHERE path = ?", (norm,))
        conn.commit()
    finally:
        conn.close()
    # Drop the cached instance so a later re-add builds a fresh object.
    _loaded_dicts.pop(norm, None)


def is_dictionary_installed(path: str) -> bool:
    """True when a complete index exists for the dictionary at `path`."""
    return get_single_dictionary(path).is_indexed()
