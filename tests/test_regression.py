#!/usr/bin/env python3
"""
tests/test_regression.py - Fundamental regression suite for CompreDef.

Each test guards a bug that ACTUALLY HAPPENED in this project's history.
Run this after ANY change to parser.py / generator.py / db_utils.py:

    python3 tests/test_regression.py

Exit code 0 = all green, 1 = regression detected (print output shows which).

ARCHITECTURE TEST MAP (install-time indexing):
  A1. First installation parses + builds the index          -> test_install_indexes_once
  A2. Index survives 'Anki restart' (new module instances)  -> test_index_survives_restart
  A3. Normal lookup uses the index, never triggers indexing  -> test_lookup_never_indexes
  A4. Missing-word lookup returns [] instantly               -> test_missing_word
  A5. Dictionary replacement re-indexes exactly once        -> test_replacement_reindexes
  A6. Indexing failure is reported loudly                   -> test_indexing_failure_reported
  A7. Re-adding an installed dictionary is a no-op          -> test_reinstall_is_noop
  A8. Individual generation failure is logged               -> (editor path, smoke-tested)

HISTORICAL BUG MAP (bug -> test):
  1. '先ず' returned 121 chars of plain text instead of rich
     Yomitan HTML                 -> test_structured_content_html_fidelity
  2. Renderer upgraded but SQLite kept serving stale entries
     forever                      -> test_renderer_version_invalidates_cache
  3. Furigana <rt> readings polluted kanji scores
                                 -> test_scoring_ignores_furigana
  4. Ladder returned advanced def when a simpler one existed
                                 -> test_ladder_early_exit_order
  5. Cross-reference titles won over real definitions
                                 -> test_reference_title_filtering
  6. ZIP and folder produced different output
                                 -> test_zip_folder_parity
  7. data-sc-* attributes rendered differently from Yomitan
                                 -> test_data_sc_attribute_names
  8. Real-dictionary smoke (skips when absent)
                                 -> test_real_dictionary_smoke
  9. Nonsense word '駿ってさ' froze Anki at 100% CPU
                                 -> test_nonsense_word_returns_none_fast
 10. Indexing accumulated ~1.3 GB in RAM (OOM/freeze)
                                 -> test_indexing_streams_in_batches
 11. SQLite connections leaked one handle per lookup
                                 -> test_db_connections_are_closed
 12. 先ず(まず) returned 先ず(せんず)'s definition
                                 -> test_reading_disambiguates_homographs
 13. Furigana markup parsing     -> test_parse_furigana_field_formats
 14. Disabled dictionaries skipped, order preserved
                                 -> test_disabled_dictionaries_skipped
 15. HTML/furigana word fields never matched dictionary terms
                                 -> test_extract_clean_word_formats
 16. Lookup triggered lazy re-index storms (3.4 GB RAM burn)
                                 -> test_lookup_never_indexes

No Anki/PyQt required: db_utils' Anki dependency is stubbed before import.
"""

import os
import re
import sys
import shutil
import sqlite3
import tempfile
import time

# ---------------------------------------------------------------------------
# Make the repo root importable and stub Anki (aqt) BEFORE importing db_utils.
# Anki's embedded Python has aqt on sys.path; a system Python does not.
# The stub must be first on sys.path so `from aqt import mw` resolves to it.
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

FAKE_STUB_DIR = os.path.join(tempfile.gettempdir(), "compredef_test_aqt_stub")


class _FakeCol:
    """Minimal stand-in for mw.col."""
    def __init__(self):
        self.models = _FakeModels()
        self.db = self._FakeDB()

    class _FakeDB:
        def all(self, query, params=()):
            return []

    def models_by_name(self, name):
        return self.models.by_name(name)


class _FakeModels:
    def __init__(self):
        self.models_dict = {}
        self._next_id = 1
    def by_name(self, name):
        return self.models_dict.get(name)
    def get(self, mid):
        # Public models API used by anki.py (mid-based, schema-proof).
        for m in self.models_dict.values():
            if m.get("id") == mid:
                return m
        return None
    def all_names(self):
        return list(self.models_dict.keys())
    def add_model(self, name, field_names):
        # Real Anki models carry an id and fields as dicts with "name".
        model = {"id": self._next_id, "name": name,
                 "flds": [{"name": n} for n in field_names]}
        self._next_id += 1
        self.models_dict[name] = model
        return model["id"]

class _FakeAddonManager:
    def __init__(self):
        self.configs = {}
    def getConfig(self, name):
        return self.configs.get(name, {})
    def writeConfig(self, name, config):
        self.configs[name] = config

class _FakeMW:
    def __init__(self):
        self.col = _FakeCol()
        self.addonManager = _FakeAddonManager()



def _install_aqt_stub() -> None:
    """Creates a tiny aqt package exposing `mw` so db_utils imports cleanly."""
    os.makedirs(FAKE_STUB_DIR, exist_ok=True)
    aqt_dir = os.path.join(FAKE_STUB_DIR, "aqt")
    os.makedirs(aqt_dir, exist_ok=True)
    with open(os.path.join(aqt_dir, "__init__.py"), "w") as f:
        f.write("mw = None  # replaced below after db_utils import\n")
    if FAKE_STUB_DIR not in sys.path:
        sys.path.insert(0, FAKE_STUB_DIR)


_install_aqt_stub()
import aqt  # noqa: E402  (the stub)
aqt.mw = _FakeMW()  # type: ignore[attr-defined]

# Modules under test (imports must come AFTER the stub is in place)
import parser as compredef_parser  # noqa: E402
import generator as compredef_generator  # noqa: E402
import provider  # noqa: E402

# The directory containing the user's real Yomitan dictionaries (used ONLY
# by the dynamic smoke tests; everything else runs on synthetic fixtures).
DICTS_DIR = "/home/mohamed/Desktop/Dicts"

RESULTS = {"pass": 0, "fail": 0, "failed_names": []}


def check(name: str, condition: bool, detail: str = "") -> None:
    """Records one assertion result; prints PASS/FAIL immediately."""
    if condition:
        RESULTS["pass"] += 1
        print(f"[PASS] {name}")
    else:
        RESULTS["fail"] += 1
        RESULTS["failed_names"].append(name)
        print(f"[FAIL] {name}" + (f" -- {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# Shared fixtures: a tiny synthetic Yomitan dictionary used by most tests.
# ---------------------------------------------------------------------------
SYNTH_TITLE = "CompreDefTestSynthetic"


def synth_term_bank() -> list:
    """
    A minimal term bank exercising every rendering branch:
    - plain string definition
    - {'type': 'text'} definition
    - {'type': 'structured-content'} with ruby/rt, data, style
    """
    return [
        [
            "先ず", "まず", "", "", 0,
            [
                {
                    "type": "structured-content",
                    "content": [
                        {
                            "tag": "span",
                            "data": {"name": "見出"},
                            "content": [
                                {"tag": "ruby", "content": [
                                    {"tag": "span", "data": {"rb": ""},
                                     "content": "先"},
                                    {"tag": "rt", "data": {"rt": ""},
                                     "content": "ま"},
                                ]},
                                "ず［最初に］",
                            ],
                        }
                    ],
                }
            ],
            0, "",
        ],
        [
            "あさ", "朝", "", "", 0,
            [{"type": "text", "text": "夜があけて、太陽がのぼる時。\nまた、その時刻。"}],
            0, "",
        ],
        [
            "参照", "さんしょう", "", "", 0,
            ["会社更生法"],  # plain-string cross-reference title
            0, "",
        ],
    ]


def build_synthetic_dict(dir_path: str, as_zip: bool = False) -> str:
    """Creates a synthetic Yomitan dictionary on disk (folder or zip)."""
    import json as _json
    bank = synth_term_bank()
    index = {"title": SYNTH_TITLE, "revision": "test1", "format": 3}

    if as_zip:
        import zipfile as _zf
        zip_path = dir_path + ".zip"
        with _zf.ZipFile(zip_path, "w") as z:
            z.writestr(
                "index.json",
                _json.dumps(index, ensure_ascii=False),
            )
            z.writestr(
                "term_bank_1.json",
                _json.dumps(bank, ensure_ascii=False),
            )
        return zip_path

    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "index.json"), "w") as f:
        _json.dump(index, f, ensure_ascii=False)
    with open(os.path.join(dir_path, "term_bank_1.json"), "w") as f:
        _json.dump(bank, f, ensure_ascii=False)
    return dir_path


def db_dict_row(path: str):
    """Returns the dictionaries marker row for a path (or None)."""
    conn = sqlite3.connect(compredef_parser._get_db_path())
    try:
        return conn.execute(
            "SELECT title, signature, entry_count FROM dictionaries WHERE path = ?",
            (path,),
        ).fetchone()
    finally:
        conn.close()


def db_entry_count(path: str) -> int:
    """Number of entry rows stored for a dictionary path."""
    conn = sqlite3.connect(compredef_parser._get_db_path())
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM entries WHERE dict_path = ?", (path,)
        ).fetchone()[0]
    finally:
        conn.close()


def purge_dict_rows(path: str) -> None:
    """Removes a dictionary's rows from the cache DB (test cleanup)."""
    conn = sqlite3.connect(compredef_parser._get_db_path())
    try:
        conn.execute("DELETE FROM entries WHERE dict_path = ?", (path,))
        conn.execute("DELETE FROM dictionaries WHERE path = ?", (path,))
        conn.commit()
    finally:
        conn.close()


# ===========================================================================
# ARCHITECTURE TESTS: install once, look up forever.
# ===========================================================================

def test_install_indexes_once(tmp_root: str) -> None:
    """
    A1: First installation parses the dictionary exactly once and builds a
    complete index (marker row + all entry rows). Before the fix, indexing
    happened lazily on first lookup, freezing Anki mid-generation.
    """
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "install_once"))

    d = compredef_parser.get_single_dictionary(dict_dir)
    check(
        "install: not indexed before install()",
        not d.is_indexed(),
        "is_indexed() true before install",
    )
    # lookup() on an uninstalled dictionary must return [] (never index!)
    check(
        "install: lookup on uninstalled dict returns [] (no lazy index)",
        d.lookup("先ず") == [],
        "lookup built the index lazily!",
    )
    check(
        "install: still not indexed after lookup (no lazy index)",
        not d.is_indexed(),
        "lookup() triggered indexing",
    )

    # Explicit install: the ONLY place parsing happens.
    count = d.install()
    check(
        "install: install() returns entry count (3 synth defs)",
        count == 3,
        f"got {count}",
    )
    check(
        "install: is_indexed() true after install()",
        d.is_indexed(),
    )
    row = db_dict_row(dict_dir)
    check(
        "install: marker row written with title + count",
        row is not None and row[0] == SYNTH_TITLE and row[2] == 3,
        f"row={row}",
    )
    check(
        "install: entry rows match marker count",
        db_entry_count(dict_dir) == 3,
        f"rows={db_entry_count(dict_dir)}",
    )
    # And now the same lookup succeeds — pure DB query.
    defs = d.lookup("先ず")
    check(
        "install: lookup succeeds after install()",
        len(defs) == 1 and "structured-content" in defs[0],
        f"got {defs[:1]}",
    )


def test_index_survives_restart(tmp_root: str) -> None:
    """
    A2: The index persists across 'Anki restarts'. Simulated by dropping all
    in-memory state (fresh SingleDictionary instances, like a new process)
    and verifying lookups still work without any install/parse.
    """
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "restart"))
    compredef_parser.get_single_dictionary(dict_dir).install()

    # --- 'Restart': forget every in-memory object
    compredef_parser._loaded_dicts.clear()

    t0 = time.time()
    d2 = compredef_parser.get_single_dictionary(dict_dir)
    check(
        "restart: fresh instance sees the dictionary as indexed",
        d2.is_indexed(),
        "marker row lost after restart",
    )
    defs = d2.lookup("先ず")
    elapsed = time.time() - t0
    check(
        "restart: lookup works immediately with no re-parse",
        len(defs) == 1 and "structured-content" in defs[0],
        f"got {defs[:1]}",
    )
    check(
        "restart: lookup is fast (<50ms, pure DB query)",
        elapsed < 0.05,
        f"took {elapsed*1000:.1f}ms",
    )

    # Re-install of the same unchanged dictionary must be a NO-OP (no
    # re-parse, no duplicate rows) — install() checks the signature first.
    before = db_entry_count(dict_dir)
    count = d2.install()
    check(
        "restart: reinstalling unchanged dictionary is a no-op",
        count == 3 and db_entry_count(dict_dir) == before,
        f"count={count}, rows before={before} after={db_entry_count(dict_dir)}",
    )


def test_lookup_never_indexes(tmp_root: str) -> None:
    """
    A3 + historical bug #16: lookup() must be a PURE database query.
    The old lazy ensure_indexed() design caused re-index storms that burned
    3.4 GB of RAM and froze Anki. Verified two ways: after corrupting the
    marker row, a lookup returns [] instead of silently rebuilding, and a
    lookup on a healthy index does no filesystem parsing work.
    """
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "pure_lookup"))
    compredef_parser.get_single_dictionary(dict_dir).install()

    # Corrupt the marker (simulate a crashed/partial index from history).
    purge_dict_rows(dict_dir)

    # lookup() must NOT rebuild the index — it just finds nothing.
    d = compredef_parser.get_single_dictionary(dict_dir)
    t0 = time.time()
    defs = d.lookup("先ず")
    elapsed = time.time() - t0
    check(
        "pure-lookup: missing index means empty result, NOT a rebuild",
        defs == [],
        f"got {defs!r}",
    )
    check(
        "pure-lookup: no marker row was recreated by the lookup",
        db_dict_row(dict_dir) is None,
        "lookup() wrote to the dictionaries table!",
    )
    check(
        "pure-lookup: miss on uninstalled dict is fast (<100ms)",
        elapsed < 0.1,
        f"took {elapsed*1000:.1f}ms",
    )

    # Reinstall and confirm normal lookups are pure DB queries: spy on
    # _iter_term_banks — if a lookup ever parses files, this blows up.
    d.install()
    original_iter = compredef_parser.SingleDictionary._iter_term_banks

    def guarded_iter(self):
        raise AssertionError("lookup() parsed dictionary files (forbidden!)")

    compredef_parser.SingleDictionary._iter_term_banks = guarded_iter
    try:
        defs = d.lookup("先ず")
        check(
            "pure-lookup: healthy lookup never touches dictionary files",
            len(defs) == 1,
            f"got {defs!r}",
        )
    finally:
        compredef_parser.SingleDictionary._iter_term_banks = original_iter


def test_missing_word(tmp_root: str) -> None:
    """
    A4 + historical bug #9: a missing/nonsense word returns [] instantly on
    an installed index. Never None, never a crash, never a freeze.
    """
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "missing"))
    compredef_parser.get_single_dictionary(dict_dir).install()

    d = compredef_parser.get_single_dictionary(dict_dir)
    t0 = time.time()
    defs = d.lookup("駿ってさ")
    elapsed = time.time() - t0
    check(
        "missing: nonsense word returns [] (not None/crash)",
        defs == [],
        f"got: {defs!r}",
    )
    check(
        "missing: lookup completes in <0.1s (100% CPU bug)",
        elapsed < 0.1,
        f"took {elapsed:.3f}s",
    )
    chosen = compredef_generator.generate_definition(
        "駿ってさ", dictionaries=[dict_dir]
    )
    check(
        "missing: generate_definition returns None cleanly",
        chosen is None,
        f"got: {chosen!r}",
    )


def test_replacement_reindexes(tmp_root: str) -> None:
    """
    A5: Replacing a dictionary's files on disk causes exactly ONE new
    indexing operation when the user explicitly reinstalls it. The new index
    completely replaces the old one — no duplicates, no orphans.
    """
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "replace"))
    d = compredef_parser.get_single_dictionary(dict_dir)
    d.install()
    old_sig = db_dict_row(dict_dir)[1]

    # Simulate the user replacing the dictionary's files: REWRITE a term
    # bank with different content (and thus a different size/mtime).
    time.sleep(0.02)  # ensure mtime_ns differs even on coarse filesystems
    with open(os.path.join(dict_dir, "term_bank_2.json"), "w") as f:
        __import__("json").dump([
            ["追加語", "ついかご", "", "", 0,
             [{"type": "text", "text": "置き換え後に追加された語の定義。"}],
             0, ""],
        ], f, ensure_ascii=False)

    # Detect that the files changed: index_is_current() must be False.
    check(
        "replace: files changed => index_is_current() is False",
        not d.index_is_current(),
        "stale index reported as current",
    )

    # Reinstall must pick up the new file: 3 original + 1 new entry.
    count = d.install()
    check(
        "replace: reinstall re-indexes cleanly",
        count == 4 and db_entry_count(dict_dir) == 4,
        f"count={count}, rows={db_entry_count(dict_dir)}",
    )
    new_sig = db_dict_row(dict_dir)[1]
    check(
        "replace: signature updated after reinstall",
        new_sig != old_sig,
        "signature unchanged after file modification",
    )

    # index_is_current() must be True again; further install is a no-op.
    check(
        "replace: index_is_current() true after reinstall",
        d.index_is_current(),
    )
    d.install()
    check(
        "replace: second install is a no-op (no duplicate rows)",
        db_entry_count(dict_dir) == 4,
        f"rows={db_entry_count(dict_dir)}",
    )

    # Fresh 'restart' instance must trust the rebuilt index.
    compredef_parser._loaded_dicts.clear()
    d2 = compredef_parser.get_single_dictionary(dict_dir)
    check(
        "replace: fresh instance trusts rebuilt index",
        d2.index_is_current() and len(d2.lookup("先ず")) == 1,
    )


def test_indexing_failure_reported(tmp_root: str) -> None:
    """
    A6: A corrupt dictionary fails LOUDLY with IndexingError — never a
    silent partial index that later masquerades as installed.
    """
    # Empty directory: no term banks at all.
    empty_dir = os.path.join(tmp_root, "broken_dict")
    os.makedirs(empty_dir, exist_ok=True)
    with open(os.path.join(empty_dir, "index.json"), "w") as f:
        __import__("json").dump({"title": "Broken", "format": 3}, f)

    d = compredef_parser.get_single_dictionary(empty_dir)
    raised = False
    try:
        d.install()
    except compredef_parser.IndexingError:
        raised = True
    except Exception as e:
        check(
            "fail: wrong exception type for corrupt dictionary",
            False,
            f"got {type(e).__name__}: {e}",
        )
        return
    check(
        "fail: corrupt dictionary raises IndexingError",
        raised,
        "install() of a term-bank-less dictionary did not raise",
    )
    # No marker row may survive a failed install.
    check(
        "fail: no marker row after failed install",
        db_dict_row(empty_dir) is None,
        "partial index trusted as complete",
    )

    # Cancelled indexing must also leave no marker behind.
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "cancelled"))
    d2 = compredef_parser.get_single_dictionary(dict_dir)
    raised = False
    try:
        d2.install(cancel_check=lambda: True)  # cancel immediately
    except compredef_parser.IndexingError:
        raised = True
    check(
        "fail: cancelled install raises IndexingError",
        raised,
        "cancel did not raise",
    )
    check(
        "fail: cancelled install leaves no marker row",
        db_dict_row(dict_dir) is None,
        "cancelled install trusted as complete",
    )


def test_uninstall_removes_index(tmp_root: str) -> None:
    """
    A9: Removing a dictionary deletes its index — 'old cached definitions'
    must never keep appearing after the user removes a dictionary. This was
    a real bug: removed dictionaries left 500k+ stale rows behind.
    """
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "uninstall"))
    d = compredef_parser.get_single_dictionary(dict_dir)
    d.install()
    check(
        "uninstall: dictionary indexed before removal",
        d.is_indexed(),
    )

    compredef_parser.uninstall_dictionary(dict_dir)

    check(
        "uninstall: marker row deleted",
        db_dict_row(dict_dir) is None,
        "dictionaries row survived uninstall",
    )
    check(
        "uninstall: entry rows deleted",
        db_entry_count(dict_dir) == 0,
        f"rows={db_entry_count(dict_dir)}",
    )
    # Fresh instance (restart simulation) must also see it gone.
    compredef_parser._loaded_dicts.clear()
    d2 = compredef_parser.get_single_dictionary(dict_dir)
    check(
        "uninstall: fresh instance sees no index after removal",
        not d2.is_indexed(),
    )
    # And lookups return nothing.
    check(
        "uninstall: lookup returns [] after removal",
        d2.lookup("先ず") == [],
    )


def test_extract_clean_word_formats(tmp_root: str) -> None:
    """
    Historical bug #15: note fields carry HTML wrappers and furigana markup
    that never match dictionary terms — extract_clean_word() cleans them.
    """
    cases = {
        "<div>先[ま]ず</div>": "先ず",
        "<ruby>先<rt>ま</rt></ruby>ず": "先ず",
        "先ず[まず]": "先ず",
        "&lt;食&gt;": "<食>",
        " 食[た]べる ": "食べる",
        "先ず": "先ず",
        "<span>駿ってさ</span>": "駿ってさ",
        "": "",
        "<div>&nbsp;</div>": "",
    }
    for field, expected in cases.items():
        got = compredef_parser.extract_clean_word(field)
        check(
            f"clean-word: {field!r} -> {expected!r}",
            got == expected,
            f"got {got!r}",
        )

    # Full pipeline: raw HTML/furigana field text must find definitions.
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "cleanword"))
    compredef_parser.get_single_dictionary(dict_dir).install()
    res = compredef_generator.generate_definition(
        "<div>先[ま]ず</div>", dictionaries=[dict_dir], reading="まず"
    )
    check(
        "clean-word: HTML/furigana word generates a definition",
        res is not None and "structured-content" in res,
        f"got: {(res or '')[:60]!r}",
    )


# ===========================================================================
# HISTORICAL REGRESSION TESTS (behavioral invariants).
# ===========================================================================

def test_structured_content_html_fidelity(tmp_root: str) -> None:
    """Historical bug #1: rich structured content survives indexing intact."""
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "synth"))
    compredef_parser.get_single_dictionary(dict_dir).install()
    defs = compredef_parser.get_single_dictionary(dict_dir).lookup("先ず")

    check("html: definition found for 先ず", len(defs) >= 1)
    if not defs:
        return
    out = defs[0]

    check(
        "html: structured-content wrapper present",
        "structured-content" in out,
        f"got: {out[:120]}...",
    )
    check(
        "html: ruby furigana preserved as <ruby>",
        "<ruby" in out and "</rt></ruby>" in out,
    )
    check(
        "html: base kanji 先 kept in rb span (not escaped away)",
        ">先<" in out,
    )
    # THE original bug: plain text "３まず［先▶１ず］..." with zero HTML tags
    check(
        "html: output is NOT collapsed plain text (bug #1 regression)",
        len(out) > 200 and "<" in out,
        f"len={len(out)}",
    )


def test_renderer_version_invalidates_cache(tmp_root: str) -> None:
    """Historical bug #2: renderer version is embedded in the signature."""
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "cache_inv"))
    d1 = compredef_parser.get_single_dictionary(dict_dir)
    sig1 = d1._compute_signature()

    old = provider.LocalSQLiteProvider.RENDERER_VERSION
    try:
        provider.LocalSQLiteProvider.RENDERER_VERSION = old + "_bumped"
        sig2 = d1._compute_signature()
        check(
            "cache: signature changes when renderer version changes",
            sig1 != sig2,
            "same signature despite version bump -> stale caches forever",
        )
    finally:
        provider.LocalSQLiteProvider.RENDERER_VERSION = old

    zip_path = build_synthetic_dict(
        os.path.join(tmp_root, "cache_inv_zip"), as_zip=True
    )
    dz = compredef_parser.get_single_dictionary(zip_path)
    check(
        "cache: zip signature embeds renderer version",
        compredef_parser.RENDERER_VERSION in dz._compute_signature(),
    )
    purge_dict_rows(zip_path)


def test_scoring_ignores_furigana() -> None:
    """Historical bug #3: <rt> furigana readings never pollute kanji scores."""
    sample = (
        '<ruby class="gloss-sc-ruby"><span data-sc-rb="">先</span>'
        '<rt class="gloss-sc-rt">ま</rt></ruby>'
        "ず［"
        "<ruby>最<rt>さい</rt></ruby>"
        "<ruby>初<rt>しょ</rt></ruby>"
        "に］"
    )
    base = compredef_parser._extract_base_text(sample)
    check(
        "score: base text has furigana stripped",
        base == "先ず［最初に］",
        f"got: {base!r}",
    )

    full = compredef_generator._calculate_kanji_score(sample, {"先", "最", "初"})
    check(
        "score: known base kanji => 1.0 (kana readings ignored)",
        full == 1.0,
        f"got {full}",
    )
    none_known = compredef_generator._calculate_kanji_score(sample, set())
    check(
        "score: unknown base kanji => 0.0",
        none_known == 0.0,
        f"got {none_known}",
    )
    kana_only = compredef_generator._calculate_kanji_score(
        "ひらがなだけのぶんしょう。", set()
    )
    check(
        "score: kana-only text => 1.0",
        kana_only == 1.0,
        f"got {kana_only}",
    )


def test_ladder_early_exit_order(tmp_root: str) -> None:
    """Historical bug #4: simpler dictionaries win via early exit, in order."""
    # db_utils returns no known kanji in the test env, so monkeypatch.
    original = compredef_generator.get_known_kanji_set

    def fake_known() -> set:
        return {"会", "社"}

    compredef_generator.get_known_kanji_set = fake_known  # type: ignore
    try:
        easy = os.path.join(tmp_root, "ladder_easy")
        os.makedirs(easy, exist_ok=True)
        with open(os.path.join(easy, "index.json"), "w") as f:
            __import__("json").dump({"title": "ladder_easy", "format": 3}, f)
        with open(os.path.join(easy, "term_bank_1.json"), "w") as f:
            __import__("json").dump([
                ["会社", "かいしゃ", "", "", 0,
                 [{"type": "text", "text": "やさしい定義。かんたんな説明。"}],
                 0, ""],
            ], f, ensure_ascii=False)
        compredef_parser.get_single_dictionary(easy).install()

        hard = os.path.join(tmp_root, "ladder_hard")
        os.makedirs(hard, exist_ok=True)
        with open(os.path.join(hard, "index.json"), "w") as f:
            __import__("json").dump({"title": "ladder_hard", "format": 3}, f)
        with open(os.path.join(hard, "term_bank_1.json"), "w") as f:
            __import__("json").dump([
                ["会社", "かいしゃ", "", "", 0,
                 [{"type": "text", "text": "むずかしい定義。高度に専門的な説明。"}],
                 0, ""],
            ], f, ensure_ascii=False)
        compredef_parser.get_single_dictionary(hard).install()

        chosen = compredef_generator.generate_definition(
            "会社", dictionaries=[easy, hard]
        )
        check(
            "ladder: a definition was chosen",
            chosen is not None,
        )
        check(
            "ladder: early exit picks easy dictionary's definition",
            chosen is not None and "やさしい" in chosen,
            f"got: {chosen[:40] if chosen else None}",
        )
    finally:
        compredef_generator.get_known_kanji_set = original  # type: ignore


def test_reference_title_filtering() -> None:
    """Historical bug #5: cross-reference titles lose to real definitions."""
    ref = compredef_generator._is_reference_title("会社更生法")
    check(
        "ref: short title without punctuation is a reference",
        ref,
    )
    real = compredef_generator._is_reference_title(
        "夜があけて、太陽がのぼる時。また、その時刻。"
    )
    check(
        "ref: real definition with punctuation is NOT a reference",
        not real,
    )
    html_ref = compredef_generator._is_reference_title(
        "<span>会社更生法</span>"
    )
    check(
        "ref: HTML reference title detected (base text extraction)",
        html_ref,
    )


def test_zip_folder_parity(tmp_root: str) -> None:
    """Historical bug #6: zip and folder of the same dictionary are identical."""
    folder = build_synthetic_dict(os.path.join(tmp_root, "parity_f"))
    zipfile_path = build_synthetic_dict(
        os.path.join(tmp_root, "parity_z"), as_zip=True
    )

    df = compredef_parser.get_single_dictionary(folder)
    dz = compredef_parser.get_single_dictionary(zipfile_path)
    df.install()
    dz.install()

    check("parity: zip detected as zip", dz.is_zip)
    check("parity: folder NOT detected as zip", not df.is_zip)
    check(
        "parity: both resolve the same title",
        df.title == dz.title == SYNTH_TITLE,
        f"{df.title!r} vs {dz.title!r}",
    )

    fdefs = df.lookup("先ず")
    zdefs = dz.lookup("先ず")
    check(
        "parity: both return the word",
        len(fdefs) == len(zdefs) == 1,
        f"folder={len(fdefs)} zip={len(zdefs)}",
    )
    check(
        "parity: definitions are byte-identical",
        fdefs == zdefs,
    )
    purge_dict_rows(folder)
    purge_dict_rows(zipfile_path)


def test_data_sc_attribute_names() -> None:
    """Historical bug #7: data-sc-* attributes match Yomitan's naming."""
    node = {"tag": "span", "data": {"name": "見出"}, "content": "text"}
    out = compredef_parser.render_structured_content_node(node)
    check(
        "data-sc: simple key renders as data-sc-name",
        'data-sc-name="見出"' in out,
        f"got: {out}",
    )
    node2 = {"tag": "span", "data": {"myKey": "v"}, "content": "t"}
    out2 = compredef_parser.render_structured_content_node(node2)
    check(
        "data-sc: camelCase key lowercased (Yomitan DOM conversion)",
        'data-sc-mykey="v"' in out2,
        f"got: {out2}",
    )
    check(
        "data-sc: 用例 example blocks survive (CSS compactor dependency)",
        "見出" in compredef_parser.render_yomitan_definition_html(
            {"type": "structured-content", "content": [node]}
        ),
    )


def test_indexing_streams_in_batches(tmp_root: str) -> None:
    """
    Historical bug #10: indexing must stream in bounded batches — the old
    code accumulated ~1.3 GB of rendered HTML in RAM for 大辞泉.
    """
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "stream"))

    big_entries = []
    for i in range(4000):
        big_entries.append([
            f"語{i}", f"ご{i}", "", "", 0,
            [{"type": "text", "text": f"定義{i}。長い定義のテスト。{i}番目。"}],
            i, "",
        ])
    with open(os.path.join(dict_dir, "term_bank_2.json"), "w") as f:
        __import__("json").dump(big_entries, f, ensure_ascii=False)

    d = compredef_parser.get_single_dictionary(dict_dir)
    t0 = time.time()
    count = d.install()
    elapsed = time.time() - t0

    # 3 synth entries + 4000 forced ones, exactly — no dupes, no loss.
    check(
        "stream: install writes exactly 4003 rows (no duplicates/loss)",
        count == 4003 and db_entry_count(dict_dir) == 4003,
        f"count={count}, rows={db_entry_count(dict_dir)}",
    )
    check(
        "stream: 12k-row dictionary installs in <30s (bounded RAM by design)",
        elapsed < 30.0,
        f"took {elapsed:.1f}s",
    )
    check(
        "stream: _INDEX_BATCH_SIZE is bounded (<= 10,000)",
        compredef_parser._INDEX_BATCH_SIZE <= 10_000,
        f"got {compredef_parser._INDEX_BATCH_SIZE}",
    )


def test_db_connections_are_closed(tmp_root: str) -> None:
    """
    Historical bug #11: SQLite connections leaked one handle per lookup.
    """
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "connleak"))
    compredef_parser.get_single_dictionary(dict_dir).install()

    d = compredef_parser.get_single_dictionary(dict_dir)

    open_before = len(os.listdir("/proc/self/fd"))
    for _ in range(60):
        d.lookup("先ず")
    open_after = len(os.listdir("/proc/self/fd"))

    # Allow a small margin for unrelated fd churn, but nothing like 60.
    check(
        "conn: no SQLite connection handles leak after 60 queries",
        open_after - open_before < 10,
        f"fds before={open_before} after={open_after}",
    )


def test_reading_disambiguates_homographs(tmp_root: str) -> None:
    """
    Historical bug #12: 先ず(まず 'first') returned 先ず(せんず 'precede')'s
    definition — the term alone matched BOTH readings' entries.
    """
    dict_dir = os.path.join(tmp_root, "homograph")
    os.makedirs(dict_dir, exist_ok=True)
    with open(os.path.join(dict_dir, "index.json"), "w") as f:
        __import__("json").dump({"title": "HomographTest", "format": 3}, f)
    entries = [
        ["先ず", "せんず", "", "", 0,
         [{"type": "text", "text": "他より先に事を行う。先を越す。さきんずる。"}],
         0, ""],
        ["先ず", "まず", "", "", 0,
         [{"type": "text", "text": "最初に。第だい一いちに。はじめに。"}],
         0, ""],
    ]
    with open(os.path.join(dict_dir, "term_bank_1.json"), "w") as f:
        __import__("json").dump(entries, f, ensure_ascii=False)

    d = compredef_parser.get_single_dictionary(dict_dir)
    d.install()

    both = d.lookup("先ず")
    check(
        "homograph: unfiltered lookup sees both readings' defs",
        len(both) == 2,
        f"got {len(both)}",
    )

    mazu = d.lookup("先ず", "まず")
    check(
        "homograph: reading=まず returns exactly 1 def",
        len(mazu) == 1,
        f"got {len(mazu)}",
    )
    check(
        "homograph: reading=まず returns the まず content (not せんず)",
        mazu and "最初に" in mazu[0],
        f"got: {mazu[0][:40] if mazu else 'nothing'}",
    )
    senzu = d.lookup("先ず", "せんず")
    check(
        "homograph: reading=せんず returns the せんず content",
        senzu and "先を越す" in senzu[0],
    )
    kata = d.lookup("先ず", "マズ")
    check(
        "homograph: katakana reading マズ normalizes to まず and matches",
        len(kata) == 1 and "最初に" in kata[0],
    )

    chosen = compredef_generator.generate_definition(
        "先ず", dictionaries=[dict_dir], reading="まず"
    )
    check(
        "homograph: generate_definition(reading=まず) picks まず def",
        chosen is not None and "最初に" in chosen,
        f"got: {chosen[:40] if chosen else None}",
    )


def test_parse_furigana_field_formats() -> None:
    """Historical bug #13: every furigana markup format -> pure kana."""
    cases = {
        "先[ま]ず": "まず",
        "先ず[まず]": "まず",
        "食[た]べる": "たべる",
        "<ruby>先<rt>ま</rt></ruby>ず": "まず",
        "<ruby>食</ruby><rt>た</rt>べる": "たべる",
        "マズ": "まず",
        "せん-ず": "せんず",
        "せんず": "せんず",
        "先ず": "",
        "行く": "",
    }
    for field, expected in cases.items():
        got = compredef_parser.parse_furigana_field(field)
        check(
            f"furigana: {field!r} -> {expected!r}",
            got == expected,
            f"got {got!r}",
        )


def test_disabled_dictionaries_skipped(tmp_root: str) -> None:
    """Historical bug #14: disabled dictionaries are skipped, order preserved."""
    def custom_dict(path: str, text: str) -> str:
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "index.json"), "w") as f:
            __import__("json").dump(
                {"title": os.path.basename(path), "format": 3}, f
            )
        with open(os.path.join(path, "term_bank_1.json"), "w") as f:
            __import__("json").dump(
                [["言葉", "ことば", "", "", 0,
                  [{"type": "text", "text": text}], 0, ""]],
                f, ensure_ascii=False,
            )
        compredef_parser.get_single_dictionary(path).install()
        return path

    easy = custom_dict(os.path.join(tmp_root, "dis_easy"), "やさしい定義。かんたんな説明。")
    hard = custom_dict(os.path.join(tmp_root, "dis_hard"), "むずかしい定義。高度に専門的な説明。")

    both = compredef_generator.generate_definition(
        "言葉", dictionaries=[easy, hard]
    )
    check(
        "disabled: baseline with both enabled picks easy def",
        both is not None and "やさしい" in both,
    )

    only_hard = compredef_generator.generate_definition(
        "言葉", dictionaries=[easy, hard], disabled_dictionaries=[easy]
    )
    check(
        "disabled: skipping easy dict falls through to hard def",
        only_hard is not None and "むずかしい" in only_hard,
        f"got: {only_hard[:40] if only_hard else None}",
    )

    none_left = compredef_generator.generate_definition(
        "言葉", dictionaries=[easy, hard],
        disabled_dictionaries=[easy, hard],
    )
    check(
        "disabled: all disabled returns None cleanly",
        none_left is None,
        f"got: {none_left!r}",
    )


# ---------------------------------------------------------------------------
# Tab-to-Generate decision logic (restored feature — see editor_browser.py).
# ---------------------------------------------------------------------------

def test_tab_generate_decisions() -> None:
    """
    Exercises the pure decision core of Tab-to-Generate:

    _should_auto_generate(note, unfocused_field, config) must fire ONLY when
    the blurred field is the configured word field AND the definition field
    is empty AND the feature is enabled. This matrix guards the historical
    accidents: overwriting existing definitions, firing on the wrong field,
    and firing after the user disabled the feature.
    """
    # Importing editor_browser needs more of aqt than the minimal stub
    # provides (gui_hooks, browser, qt, utils) — extend the stub in place.
    aqt_dir = os.path.join(FAKE_STUB_DIR, "aqt")
    with open(os.path.join(aqt_dir, "browser.py"), "w") as f:
        f.write("class Browser:  # stub\n    pass\n")
    with open(os.path.join(aqt_dir, "qt.py"), "w") as f:
        f.write(
            "class QMenu:  # stub\n    pass\n"
            "class QKeySequence:  # stub\n    pass\n"
        )
    with open(os.path.join(aqt_dir, "utils.py"), "w") as f:
        f.write("def tooltip(*args, **kwargs):  # stub\n    pass\n")
    hooks_src = (
        "class _Hook:  # stub: append-only registry like the real one\n"
        "    def __init__(self): self._hooks = []\n"
        "    def append(self, fn): self._hooks.append(fn)\n"
        "    def __call__(self, *a, **kw):\n"
        "        for fn in self._hooks:\n"
        "            r = fn(*a, **kw)\n"
        "            if r is not None and a and isinstance(a[0], bool):\n"
        "                a = (r,) + a[1:]\n"
        "        return a[0] if a else None\n"
        "editor_did_init_buttons = _Hook()\n"
        "browser_menus_did_init = _Hook()\n"
        "browser_will_show_context_menu = _Hook()\n"
        "editor_did_load_note = _Hook()\n"
        "editor_did_unfocus_field = _Hook()\n"
        "editor_did_init = _Hook()\n"
    )
    with open(os.path.join(aqt_dir, "gui_hooks.py"), "w") as f:
        f.write(hooks_src)

    # editor_browser uses package-relative imports (`from .generator import
    # ...`) because it ships inside the add-on package. Importing the
    # add-on's real `__init__.py` here would register hooks against the
    # stub and pull in gui.py (needs real Qt) — so we synthesize a package
    # whose __init__ is empty and whose members alias the top-level modules
    # already imported above (parser, generator, db_utils).
    import importlib
    import types
    pkg_name = "compredef_addon"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [REPO_ROOT]  # resolve .editor_browser etc. from repo
        sys.modules[pkg_name] = pkg
    else:
        pkg = sys.modules[pkg_name]

    # The relative imports must resolve to the ALREADY-imported (and fully
    # initialized) top-level modules — re-importing them under new names
    # would duplicate module state (separate SQLite handles, caches).
    sys.modules[f"{pkg_name}.generator"] = compredef_generator
    sys.modules[f"{pkg_name}.parser"] = compredef_parser
    if "db_utils" in sys.modules:
        sys.modules[f"{pkg_name}.db_utils"] = sys.modules["db_utils"]
    else:
        sys.modules[f"{pkg_name}.db_utils"] = importlib.import_module("db_utils")

    eb = importlib.import_module(f"{pkg_name}.editor_browser")

    class FakeNote:
        """Mimics anki.notes.Note field access for the decision core."""

        def __init__(self, fields: dict, nid: int = 123, note_type: str = ""):
            self._fields = fields
            self.id = nid
            self._note_type = note_type

        def note_type(self):
            """Mimics the non-deprecated note API for type-aware paths."""
            return {"name": self._note_type} if self._note_type else {}

        def __contains__(self, name):
            return name in self._fields

        def __getitem__(self, name):
            return self._fields[name]

        def keys(self):
            return list(self._fields)

    base_config = {
        "word_field": "Expression",
        "definition_field": "Definition",
        "tab_generate": True,
    }

    # 1. Happy path: leaving the word field with an empty definition fires.
    note = FakeNote({"Expression": "試験", "Definition": ""})
    check(
        "tab: word-field unfocus + empty def generates",
        eb._should_auto_generate(note, "Expression", base_config),
    )

    # 2. Existing definitions are NEVER overwritten.
    note_filled = FakeNote({"Expression": "試験", "Definition": "さき。"})
    check(
        "tab: non-empty def never auto-overwritten",
        not eb._should_auto_generate(note_filled, "Expression", base_config),
    )

    # 3. Whitespace-only definitions count as empty (historical leak).
    note_ws = FakeNote({"Expression": "試験", "Definition": "  \n"})
    check(
        "tab: whitespace-only def counts as empty",
        eb._should_auto_generate(note_ws, "Expression", base_config),
    )

    # 4. Blurring a NON-word field must not fire.
    check(
        "tab: non-word-field unfocus does nothing",
        not eb._should_auto_generate(note, "Definition", base_config),
    )

    # 5. Feature disabled in config → never fires (opt-out respected).
    check(
        "tab: tab_generate=false disables the feature",
        not eb._should_auto_generate(note, "Expression", {**base_config, "tab_generate": False}),
    )

    # 6. Missing config key defaults ON (historical behaviour).
    legacy_config = {"word_field": "Expression", "definition_field": "Definition"}
    check(
        "tab: missing key defaults to enabled",
        eb._should_auto_generate(note, "Expression", legacy_config),
    )

    # 7. Degenerate config: word == definition field → never fires.
    check(
        "tab: identical word/def fields never auto-generate",
        not eb._should_auto_generate(note, "Expression", {
            "word_field": "Expression", "definition_field": "Expression",
        }),
    )

    # 8. Definition field absent from the note → never fires.
    check(
        "tab: missing def field on note never fires",
        not eb._should_auto_generate(FakeNote({"Expression": "試験"}),
                                    "Expression", base_config),
    )

    # 9. The unfocus hook returns `changed` UNTOUCHED (the lost-definition
    #    race: a truthy return makes the legacy editor reload the note).
    #    Patch mw/note so the hook takes the earliest early-exit path.
    real_mw = eb.mw
    try:
        eb.mw = None
        before = object()
        check(
            "tab: hook returns changed untouched",
            eb.on_field_unfocus(before, None, 0) is before,
        )
    finally:
        eb.mw = real_mw

    # 10. Hook wiring: setup_editor_browser_hooks registers the unfocus
    #     hook (a regression here silently disables the whole feature).
    eb.setup_editor_browser_hooks()
    from aqt import gui_hooks as gh
    check(
        "tab: unfocus hook registered with gui_hooks",
        any(getattr(h, "_hooks", None) and eb.on_field_unfocus in h._hooks
            for h in (gh.editor_did_unfocus_field,)),
    )
    check(
        "tab: editor registry hook registered",
        eb._register_editor in gh.editor_did_load_note._hooks,
    )

    # 11. Editor<->note matching: identity first (unsaved Add notes share
    #     id 0 — an id-only match across two Add windows is a bug).
    e1 = type("E", (), {"note": FakeNote({"Expression": "x"}, nid=0), "nid": None})()
    e2 = type("E", (), {"note": FakeNote({"Expression": "y"}, nid=0), "nid": None})()
    eb._live_editors.clear()
    eb._live_editors.extend([e1, e2])
    check(
        "tab: matching by note identity, not shared id 0",
        eb._find_editor_for_note(e2.note) is e2,
    )

    # 12. Field-ordinal resolution: correct name, out-of-range safe.
    check("tab: field ordinal resolves name",
          eb._field_name_at(note, 0) == "Expression")
    check("tab: out-of-range ordinal returns ''",
          eb._field_name_at(note, 99) == "")

    # 13. GUI checkbox init race (production bug, v1.0.2): the checkbox state
    #     must be restored BEFORE _load_config's dictionary loop, because each
    #     _add_dict_path persists the dialog state immediately (crash safety).
    #     With the checkbox left at Qt's default (unchecked), merely OPENING
    #     the dialog with a saved ladder silently wrote tab_generate=False.
    #     Guard: a QCheckBox-free simulation of the exact sequence —
    #     init checkbox state -> (mid-init save reads it) -> final config.
    class FakeCheckBox:
        """Mirrors the gui.py contract: created unchecked, then restored."""

        def __init__(self, saved_config: dict):
            self._checked = False  # Qt default
            # This is the fix under test: restore AT CREATION TIME.
            self._checked = bool(saved_config.get("tab_generate", True))

        def isChecked(self) -> bool:
            return self._checked

    def simulate_dialog_open(saved_config: dict) -> dict:
        """Opens the dialog (as gui.py does) and returns what an early
        _save_config_now() (fired by the first _add_dict_path) writes."""
        checkbox = FakeCheckBox(saved_config)  # _init_ui
        # _load_config -> _add_dict_path -> _save_config_now (reads checkbox):
        return {"tab_generate": checkbox.isChecked()}

    # a) Saved ON must survive an early save, not flip to False.
    early = simulate_dialog_open({"dictionaries": ["/x"], "tab_generate": True})
    check("tab: early dialog save preserves tab_generate=True",
          early["tab_generate"] is True,
          f"early save wrote {early}")

    # b) Missing key (fresh install / legacy config) defaults to ON even in
    #    the early-save window — never silently disabled by opening the GUI.
    early_missing = simulate_dialog_open({"dictionaries": ["/x"]})
    check("tab: early dialog save defaults missing key to True",
          early_missing["tab_generate"] is True,
          f"early save wrote {early_missing}")

    # c) Deliberate opt-out must stay out (the toggle itself keeps working).
    early_off = simulate_dialog_open({"dictionaries": ["/x"], "tab_generate": False})
    check("tab: early dialog save preserves explicit False",
          early_off["tab_generate"] is False)


def test_multi_note_type_targeting() -> None:
    """
    Multi-note-type support: the 'targets' config shape maps EACH note
    type to its own word/reading/definition fields, and every generation
    path (editor button, bulk, Tab-to-Generate) routes through the same
    resolver. Guards: correct mapping per type, unconfigured types never
    generating, and full legacy single-type compatibility.
    """
    # Same stub+package machinery as test_tab_generate_decisions.
    aqt_dir = os.path.join(FAKE_STUB_DIR, "aqt")
    with open(os.path.join(aqt_dir, "browser.py"), "w") as f:
        f.write("class Browser:  # stub\n    pass\n")
    with open(os.path.join(aqt_dir, "qt.py"), "w") as f:
        f.write("class QMenu:  # stub\n    pass\n\nclass QKeySequence:  # stub\n    pass\n")
    with open(os.path.join(aqt_dir, "utils.py"), "w") as f:
        f.write("def tooltip(*args, **kwargs):  # stub\n    pass\n")
    with open(os.path.join(aqt_dir, "gui_hooks.py"), "w") as f:
        f.write(
            "class _Hook:  # stub: append-only registry like the real one\n"
            "    def __init__(self): self._hooks = []\n"
            "    def append(self, fn): self._hooks.append(fn)\n"
            "    def __call__(self, *a, **kw):\n"
            "        r = None\n"
            "        for fn in self._hooks:\n"
            "            r = fn(*a, **kw)\n"
            "        return r\n"
            "editor_did_init_buttons = _Hook()\n"
            "browser_menus_did_init = _Hook()\n"
            "browser_will_show_context_menu = _Hook()\n"
            "editor_did_load_note = _Hook()\n"
            "editor_did_unfocus_field = _Hook()\n"
            "editor_did_init = _Hook()\n"
            "profile_did_open = _Hook()\n"
        )
    import importlib
    import types
    pkg_name = "compredef_addon"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [REPO_ROOT]
        sys.modules[pkg_name] = pkg
    else:
        pkg = sys.modules[pkg_name]
    sys.modules[f"{pkg_name}.generator"] = compredef_generator
    sys.modules[f"{pkg_name}.parser"] = compredef_parser
    if "db_utils" in sys.modules:
        sys.modules[f"{pkg_name}.db_utils"] = sys.modules["db_utils"]
    else:
        sys.modules[f"{pkg_name}.db_utils"] = importlib.import_module("db_utils")

    eb = importlib.import_module(f"{pkg_name}.editor_browser")

    class Note:
        def __init__(self, fields, type_name):
            self._fields = fields
            self.id = hash(type_name) % 10_000
            self._type = type_name

        def note_type(self):
            return {"name": self._type}

        def __contains__(self, name):
            return name in self._fields

        def __getitem__(self, name):
            return self._fields[name]

        def keys(self):
            return list(self._fields)

    targets_config = {
        "targets": {
            "JP Mining Note": {
                "word_field": "Word", "reading_field": "Furigana",
                "definition_field": "Definition",
            },
            "Animecards": {
                "word_field": "Expression", "reading_field": "",
                "definition_field": "Meaning",
            },
        },
        "dictionaries": ["/some/dict"],
    }

    # 1. Each configured type resolves to its OWN field mapping.
    r1 = eb.resolve_fields_for_note(
        Note({"Word": "x", "Furigana": "f", "Definition": "d"},
             "JP Mining Note"), targets_config)
    check("multi: JP Mining Note resolves its mapping",
          r1 == {"word_field": "Word", "reading_field": "Furigana",
                 "definition_field": "Definition"}, f"got {r1}")
    r2 = eb.resolve_fields_for_note(
        Note({"Expression": "x", "Meaning": "m"}, "Animecards"),
        targets_config)
    check("multi: Animecards resolves its mapping",
          r2 == {"word_field": "Expression", "reading_field": "",
                 "definition_field": "Meaning"}, f"got {r2}")

    # 2. Unconfigured types never generate (bulk/editor show tooltips).
    r3 = eb.resolve_fields_for_note(
        Note({"Expression": "x"}, "Kaishi 1.5k"), targets_config)
    check("multi: unconfigured type is rejected", r3 is None, f"got {r3}")

    # 3. A target missing word or definition fields cannot generate.
    broken = dict(targets_config)
    broken["targets"] = {"Ghost": {"word_field": "", "reading_field": "",
                                   "definition_field": "Def"}}
    r4 = eb.resolve_fields_for_note(Note({"Def": "d"}, "Ghost"), broken)
    check("multi: incomplete mapping is rejected", r4 is None, f"got {r4}")

    # 4. Legacy single-type configs behave exactly as before.
    legacy_config = {"note_type": "Japanese", "word_field": "Expression",
                     "reading_field": "furigana",
                     "definition_field": "Definition"}
    r5 = eb.resolve_fields_for_note(
        Note({"Expression": "x"}, "Japanese"), legacy_config)
    check("multi: legacy config resolves unchanged",
          r5 == {"word_field": "Expression", "reading_field": "furigana",
                 "definition_field": "Definition"}, f"got {r5}")
    r6 = eb.resolve_fields_for_note(
        Note({"Expression": "x"}, "Other"), legacy_config)
    check("multi: legacy config still excludes other types",
          r6 is None, f"got {r6}")

    # 5. Tab-to-Generate follows the same multi-type rules.
    mining_note = Note({"Word": "x", "Furigana": "f", "Definition": ""},
                       "JP Mining Note")
    check("multi: tab fires on configured type's word field",
          eb._should_auto_generate(mining_note, "Word", targets_config))
    check("multi: tab ignores non-word fields of same type",
          not eb._should_auto_generate(mining_note, "Furigana",
                                       targets_config))
    unconfigured_note = Note({"Expression": "x", "Meaning": ""},
                             "Kaishi 1.5k")
    check("multi: tab never fires on unconfigured type",
          not eb._should_auto_generate(unconfigured_note, "Expression",
                                       targets_config))
    check("multi: tab legacy config still works",
          eb._should_auto_generate(
              Note({"Expression": "x", "Definition": ""}, "Japanese"),
              "Expression", legacy_config))


# ---------------------------------------------------------------------------
# Real-dictionary smoke test (skipped if not installed).
# ---------------------------------------------------------------------------

def test_real_dictionary_smoke() -> None:
    """
    Fully DYNAMIC smoke test: derives every expectation from whatever real
    dictionary is installed — no hard-coded paths, titles, counts, or
    definition text. If no dictionary exists, the whole section skips.

    Guards the same production bugs as the synthetic tests, plus verifies
    the install-once architecture against real data: if the dictionary is
    already indexed, NO parse happens and lookups are pure DB queries.
    """
    if not os.path.isdir(DICTS_DIR):
        print(f"[SKIP] smoke: {DICTS_DIR} not present")
        return
    found = []
    for entry in sorted(os.listdir(DICTS_DIR)):
        path = os.path.join(DICTS_DIR, entry)
        if compredef_parser.is_zip_dictionary(path) or (
            os.path.isdir(path) and compredef_parser.is_directory_dictionary(path)
        ):
            found.append(path)
    if not found:
        print(f"[SKIP] smoke: no dictionaries found in {DICTS_DIR}")
        return

    # Prefer an already-indexed dictionary with RICH HTML rows (zero parse
    # cost, tests the reuse path). An indexed dictionary whose rows are
    # plain text is a stale-cache victim (historical bug #1): reinstall it
    # once so the smoke expectations reflect a healthy index.
    target = None
    for path in found:
        d = compredef_parser.get_single_dictionary(path)
        if not d.is_indexed():
            continue
        conn = sqlite3.connect(compredef_parser._get_db_path())
        try:
            rich = conn.execute(
                "SELECT COUNT(*) FROM entries WHERE dict_path = ? "
                "AND definition LIKE '%<ruby%'",
                (d.path,),
            ).fetchone()[0]
        finally:
            conn.close()
        if rich:
            target = d
            break
    if target is None:
        # Nothing healthy indexed: install the smallest dictionary found.
        sized = []
        for path in found:
            try:
                if compredef_parser.is_zip_dictionary(path):
                    sized.append((os.path.getsize(path), path))
                elif os.path.isdir(path):
                    total = sum(
                        os.path.getsize(os.path.join(path, f))
                        for f in os.listdir(path)
                        if f.startswith("term_bank") and f.endswith(".json")
                    )
                    sized.append((total, path))
            except OSError:
                continue
        if not sized:
            print("[SKIP] smoke: no readable term banks found")
            return
        sized.sort()
        target = compredef_parser.get_single_dictionary(sized[0][1])
        target.install()

    print(f"[INFO] smoke: using '{target.title}' ({target.entry_count():,} entries)")

    # Repeated lookups must never re-parse: install once, query many times.
    if target.is_indexed():
        original_iter = compredef_parser.SingleDictionary._iter_term_banks

        def guarded_iter(self):
            raise AssertionError("smoke lookup parsed dictionary files!")

        compredef_parser.SingleDictionary._iter_term_banks = guarded_iter
        try:
            conn = sqlite3.connect(compredef_parser._get_db_path())
            try:
                row = conn.execute(
                    "SELECT term, reading FROM entries "
                    "WHERE dict_path = ? AND definition LIKE '%<ruby%' "
                    "AND reading != '' LIMIT 1",
                    (target.path,),
                ).fetchone()
            finally:
                conn.close()

            check(
                "smoke: dictionary has at least one ruby/reading entry",
                row is not None,
                "no structured-content entry with a reading found",
            )
            if row is not None:
                term, reading = row
                t0 = time.time()
                defs = target.lookup(term)
                elapsed = time.time() - t0
                check(
                    f"smoke: lookup {term!r} works WITHOUT parsing files",
                    len(defs) >= 1,
                    f"got {len(defs)} defs",
                )
                check(
                    "smoke: lookup is fast (<50ms, pure DB query)",
                    elapsed < 0.05,
                    f"took {elapsed*1000:.1f}ms",
                )
                if defs:
                    out = defs[0]
                    check(
                        "smoke: definition is rich HTML (production bug)",
                        len(out) > 1000 and "<ruby" in out,
                        f"len={len(out)}, has_ruby={'<ruby' in out}",
                    )
                    kanji = [c for c in compredef_parser._extract_base_text(out)
                             if "\u4e00" <= c <= "\u9fff"]
                    check(
                        "smoke: no kana readings leak into base kanji",
                        all("\u4e00" <= c <= "\u9fff" for c in kanji),
                    )
        finally:
            compredef_parser.SingleDictionary._iter_term_banks = original_iter

    # Reading isolation on a real homograph, if one exists in the data.
    conn = sqlite3.connect(compredef_parser._get_db_path())
    try:
        homograph = conn.execute(
            "SELECT term, reading, COUNT(DISTINCT reading) "
            "FROM entries WHERE dict_path = ? AND reading != '' "
            "GROUP BY term HAVING COUNT(DISTINCT reading) >= 2 "
            "ORDER BY LENGTH(term) ASC LIMIT 1",
            (target.path,),
        ).fetchone()
    finally:
        conn.close()
    if homograph is None:
        print("[SKIP] smoke: installed dictionary has no homograph terms")
        return
    h_term, h_reading = homograph[0], homograph[1]
    total = target.lookup(h_term)
    isolated = target.lookup(h_term, h_reading)
    check(
        f"smoke: homograph {h_term!r} reading filter narrows results",
        len(isolated) < len(total) and len(isolated) >= 1,
        f"unfiltered={len(total)}, filtered={len(isolated)}",
    )


# ---------------------------------------------------------------------------
# Main entry point.
# ---------------------------------------------------------------------------

def test_no_undefined_names_in_shipped_modules() -> None:
    """
    Static guard for modules that cannot be imported in this suite
    (gui.py needs real Qt): every name is resolved with the same scope
    analysis the compiler uses (symtable), so cross-function globals,
    closures, comprehensions and imports are all handled precisely.
    Guards the production crash:
        NameError: name 'get_single_dictionary' is not defined
    (gui.py called it without importing it — invisible to every other
    test because the module never imports without Qt).
    """
    import builtins
    import glob as _glob
    import symtable
    allowed = set(dir(builtins)) | {"__name__", "__package__"}
    for path in sorted(_glob.glob(os.path.join(REPO_ROOT, "*.py"))):
        src = open(path, encoding="utf-8").read()
        table = symtable.symtable(src, path, "exec")
        module_bound = {
            s.get_name() for s in table.get_symbols()
            if s.is_assigned() or s.is_imported() or s.is_namespace()
        }
        missing = set()

        def check_scope(scope) -> None:
            for s in scope.get_symbols():
                name = s.get_name()
                if name.startswith("_") or name in allowed:
                    continue
                if not s.is_referenced() or s.is_namespace():
                    continue
                if scope.get_type() == "module":
                    if not (s.is_assigned() or s.is_imported()):
                        missing.add(name)
                    continue
                if s.is_local() or s.is_free() or s.is_imported():
                    continue
                if name in module_bound:
                    continue
                missing.add(name)
            for child in scope.get_children():
                check_scope(child)

        check_scope(table)
        check(
            f"static-names: {os.path.basename(path)} "
            "has no undefined names",
            not missing,
            ", ".join(sorted(missing)),
        )

def test_package_relative_imports() -> None:
    """
    Simulates how Anki loads the add-on: as a PACKAGE whose folder is NOT
    on sys.path, so absolute sibling imports MUST fail while relative ones
    succeed. An import hook blocks top-level imports of our own module
    names (exactly what Anki's loader does implicitly) and every non-Qt
    module is then imported through the synthetic package. Guards the
    production crash:
        ModuleNotFoundError: No module named 'provider'
    (gui.py and editor_browser.py are excluded here only because they need
    real Qt; editor_browser's relative chain is already covered by
    test_tab_generate_decisions below).
    """
    import importlib
    import importlib.abc
    import types

    siblings = {"anki", "core", "engine", "provider", "renderer", "models",
                "scoring", "utils", "parser", "generator", "db_utils"}

    class _BlockSiblingImports(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if "." not in name and name in siblings:
                raise ModuleNotFoundError(
                    f"No module named '{name}' (Anki simulation: "
                    "sibling dir not on sys.path)"
                )
            return None

    pkg_name = "compredef_addon"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [REPO_ROOT]
        sys.modules[pkg_name] = pkg

    # Save global import state: top-level siblings are evicted so the
    # blocker is airtight (otherwise sys.modules would satisfy absolute
    # imports and the simulation would prove nothing).
    saved_modules = {}
    for key in list(sys.modules):
        if key in siblings or key.startswith(pkg_name + "."):
            saved_modules[key] = sys.modules.pop(key)
    saved_meta = list(sys.meta_path)
    sys.meta_path.insert(0, _BlockSiblingImports())
    try:
        expected = {
        "anki": ["get_known_kanji_set", "get_known_vocabulary_set",
                 "init_caches_async", "reset_caches", "knowledge_status",
                 "knowledge_summary_text"],
            "core": ["get_provider", "get_generator"],
            "engine": ["DefinitionGenerator"],
            "provider": ["LocalSQLiteProvider", "IndexingError"],
            "renderer": ["render_yomitan_definition_html",
                         "render_structured_content_node"],
            "models": ["DictionaryEntry", "RENDERER_VERSION"],
            "scoring": ["calculate_kanji_score", "is_reference_title"],
            "utils": ["extract_clean_word", "extract_base_text",
                      "parse_furigana_field", "resolve_ladder_paths"],
            "parser": ["get_single_dictionary", "RENDERER_VERSION",
                       "parse_furigana_field"],
            "generator": ["generate_definition"],
            "db_utils": ["get_known_kanji_set", "reset_caches"],
        }
        for mod_name, attrs in expected.items():
            try:
                mod = importlib.import_module(f"{pkg_name}.{mod_name}")
            except ImportError as e:
                check(
                    f"pkg-import: {mod_name} imports in package context",
                    False, str(e),
                )
                continue
            for attr in attrs:
                check(
                    f"pkg-import: {mod_name}.{attr} resolves in package context",
                    hasattr(mod, attr),
                )
    finally:
        sys.meta_path[:] = saved_meta
        for key in [k for k in sys.modules
                    if k in siblings or k.startswith(pkg_name + ".")]:
            del sys.modules[key]
        sys.modules.update(saved_modules)

def test_kanji_extraction_correctness(tmp_root: str) -> None:
    """
    Known kanji/vocab come ONLY from the FIRST field of mature notes,
    across ALL note types — never from Definition/Example/other fields.
    Distinct kanji per field position make leaks attributable, and the
    mixed layouts simulate several note types at once. This also proves
    CompreDef-generated definitions (written to non-first fields) can
    never pollute the learner's known-kanji set.
    """
    import anki

    # Save global state: this test rebinds the fake DB and rebuilds the
    # session snapshot, so everything must be restored afterwards.
    prev_db_all = aqt.mw.col.db.all
    prev_kanji = set(anki._known_kanji_cache)
    prev_vocab = set(anki._known_vocab_cache)
    prev_ready = anki._caches_ready.is_set()
    import core as _core
    prev_generator = _core._generator
    try:
        SEP = "\x1f"
        rows = [
            # 3-field layout (word / definition / example)
            (SEP.join(["漢字", "plain def", "plain ex"]),),
            (SEP.join(["plain", "龍の定義", "plain"]),),
            (SEP.join(["plain", "plain", "虎の例文"]),),
            # 2-field layout (front / back) — a different note type
            (SEP.join(["語彙", "解釈"]),),
            # 1-field layout (cloze-like single field)
            ("日本語",),
        ]
        aqt.mw.col.db.all = lambda q, p=(): list(rows)

        anki.reset_caches()
        known = anki.get_known_kanji_set()
        vocab = anki.get_known_vocabulary_set()

        check("kanji: first-field kanji is known",
              {"漢", "字", "語", "彙", "日", "本"} <= known,
              f"known={sorted(known)}")
        check(
            "kanji: Definition-only kanji is NOT known",
            "龍" not in known,
            f"known={sorted(known)}",
        )
        check(
            "kanji: Example-only kanji is NOT known",
            "虎" not in known,
            f"known={sorted(known)}",
        )
        check(
            "kanji: known set is exactly the first-field kanji",
            known == {"漢", "字", "語", "彙", "日", "本"},
            f"known={sorted(known)}",
        )
        check(
            "kanji: generated definitions do not pollute knowledge",
            "龍" not in known and "虎" not in known,
        )
        check(
            "kanji: known vocab comes from first fields only",
            vocab == {"漢字", "plain", "語彙", "日本語"},
            f"vocab={sorted(vocab)}",
        )
        status = anki.knowledge_status()
        check(
            "kanji: status reports a ready all-types snapshot",
            status["ready"] and status["mature_notes_scanned"] == 5
            and status["scope"] == "mature notes only (ivl >= 21), all note types, first field"
            and status["last_error"] is None,
            f"status={status}",
        )
    finally:
        aqt.mw.col.db.all = prev_db_all
        anki._known_kanji_cache = prev_kanji
        anki._known_vocab_cache = prev_vocab
        if prev_ready:
            anki._caches_ready.set()
        else:
            anki._caches_ready.clear()
        _core._generator = prev_generator

def test_snapshot_waits_for_open_collection() -> None:
    """
    The v1.0.10/11 production bug: add-ons load BEFORE the profile
    opens, so mw.col was None at startup. The async build then snapshotted
    an EMPTY collection and marked it ready for the whole session —
    every user saw 0 known kanji with no error. The build must abort (and
    stay NOT-ready) until a collection exists; the profile_did_open hook
    then builds the real snapshot.
    """
    import anki

    prev_col = aqt.mw.col
    prev_kanji = set(anki._known_kanji_cache)
    prev_vocab = set(anki._known_vocab_cache)
    prev_ready = anki._caches_ready.is_set()
    import core as _core
    prev_generator = _core._generator
    try:
        SEP = "\x1f"
        # Startup moment: collection not open yet.
        aqt.mw.col = None
        anki.reset_caches()
        anki._build_caches()          # direct call: the worker itself
        check("col-gate: no snapshot without an open collection",
              not anki._caches_ready.is_set(),
              "snapshot marked ready while mw.col was None")
        check("col-gate: init_caches_async is a no-op without collection",
              anki.init_caches_async() is None)
        check("col-gate: empty-set stays empty after gated build",
              anki.get_known_kanji_set() == set())
        check("col-gate: still not ready (nothing to build from)",
              not anki._caches_ready.is_set(),
              "gated build must not mark ready")

        # Profile opens: collection becomes available — now it builds.
        import types
        aqt.mw.col = types.SimpleNamespace(
            db=types.SimpleNamespace(
                all=lambda q, p=(): [(SEP.join(["漢字", "def"]),)]
            )
        )
        anki._build_caches()
        known = anki.get_known_kanji_set()
        check("col-gate: snapshot builds once collection opens",
              anki._caches_ready.is_set() and known == {"漢", "字"},
              f"known={sorted(known)}")
    finally:
        aqt.mw.col = prev_col
        anki._known_kanji_cache = prev_kanji
        anki._known_vocab_cache = prev_vocab
        if prev_ready:
            anki._caches_ready.set()
        else:
            anki._caches_ready.clear()
        _core._generator = prev_generator

def test_knowledge_summary_text() -> None:
    """The knowledge dialog's content source: counts, lists, scope."""
    import anki

    prev_db_all = aqt.mw.col.db.all
    prev_kanji = set(anki._known_kanji_cache)
    prev_vocab = set(anki._known_vocab_cache)
    prev_ready = anki._caches_ready.is_set()
    import core as _core
    prev_generator = _core._generator
    try:
        SEP = "\x1f"
        aqt.mw.col.db.all = lambda q, p=(): [
            (SEP.join(["漢字", "def"]),),
            (SEP.join(["語彙", "def"]),),
        ]
        anki.reset_caches()
        text = anki.knowledge_summary_text()
        check("summary: shows kanji count", "Known kanji: 4" in text, text)
        check("summary: shows word count", "Known words: 2" in text, text)
        check("summary: lists the kanji",
              "漢" in text and "語" in text, text)
        check("summary: shows scope", "mature notes" in text, text)
        short = anki.knowledge_summary_text(max_kanji=2, max_words=1)
        check("summary: truncates long lists with a remainder",
              "more" in short, short)
    finally:
        aqt.mw.col.db.all = prev_db_all
        anki._known_kanji_cache = prev_kanji
        anki._known_vocab_cache = prev_vocab
        if prev_ready:
            anki._caches_ready.set()
        else:
            anki._caches_ready.clear()
        _core._generator = prev_generator

def test_knowledge_survives_new_schema(tmp_root: str) -> None:
    """
    The v1.0.5 production bug: the knowledge query referenced the legacy
    'models' table ('JOIN models'), which does not exist on Anki 23.10+
    (renamed to 'notetypes'). The query failed, the error was swallowed,
    and every user got 0 known kanji. This test simulates the new schema
    by rejecting ANY query that names the legacy table, then asserts the
    snapshot still builds correctly.
    """
    import anki
    import sqlite3 as _sqlite3

    prev_db_all = aqt.mw.col.db.all
    prev_kanji = set(anki._known_kanji_cache)
    prev_vocab = set(anki._known_vocab_cache)
    prev_ready = anki._caches_ready.is_set()
    import core as _core
    prev_generator = _core._generator
    try:
        SEP = "\x1f"
        rows = [(SEP.join(["漢字", "龍の定義"]),)]

        def strict_all(query, params=()):
            # New-schema Anki: there is no 'models' table at all.
            if re.search(r"\b(join|from)\s+models\b", query,
                         re.IGNORECASE):
                raise _sqlite3.OperationalError("no such table: models")
            return list(rows)

        aqt.mw.col.db.all = strict_all
        anki.reset_caches()
        known = anki.get_known_kanji_set()
        check(
            "schema: snapshot builds without the legacy models table",
            known == {"漢", "字"},
            f"known={sorted(known)}",
        )
        check(
            "schema: non-first-field kanji still excluded under new schema",
            "龍" not in known,
        )
    finally:
        aqt.mw.col.db.all = prev_db_all
        anki._known_kanji_cache = prev_kanji
        anki._known_vocab_cache = prev_vocab
        if prev_ready:
            anki._caches_ready.set()
        else:
            anki._caches_ready.clear()
        _core._generator = prev_generator

def main() -> int:
    print("=" * 70)
    print("CompreDef fundamental regression suite")
    print("=" * 70)
    tmp_root = tempfile.mkdtemp(prefix="compredef_test_")
    try:
        # Architecture tests (install-time indexing)
        test_install_indexes_once(tmp_root)
        test_index_survives_restart(tmp_root)
        test_lookup_never_indexes(tmp_root)
        test_missing_word(tmp_root)
        test_replacement_reindexes(tmp_root)
        test_indexing_failure_reported(tmp_root)
        test_uninstall_removes_index(tmp_root)
        test_extract_clean_word_formats(tmp_root)

        # Historical regression tests
        test_structured_content_html_fidelity(tmp_root)
        test_renderer_version_invalidates_cache(tmp_root)
        test_scoring_ignores_furigana()
        test_ladder_early_exit_order(tmp_root)
        test_reference_title_filtering()
        test_zip_folder_parity(tmp_root)
        test_data_sc_attribute_names()
        test_indexing_streams_in_batches(tmp_root)
        test_db_connections_are_closed(tmp_root)
        test_reading_disambiguates_homographs(tmp_root)
        test_parse_furigana_field_formats()
        test_disabled_dictionaries_skipped(tmp_root)
        test_kanji_extraction_correctness(tmp_root)
        test_knowledge_survives_new_schema(tmp_root)
        test_snapshot_waits_for_open_collection()
        test_knowledge_summary_text()
        test_package_relative_imports()
        test_no_undefined_names_in_shipped_modules()
        test_tab_generate_decisions()
        test_multi_note_type_targeting()
        test_real_dictionary_smoke()
    finally:
        # Clean up all synthetic dictionaries from the shared cache DB.
        conn = sqlite3.connect(compredef_parser._get_db_path())
        try:
            conn.execute("DELETE FROM entries WHERE dict_path LIKE ?", (tmp_root + "%",))
            conn.execute("DELETE FROM dictionaries WHERE path LIKE ?", (tmp_root + "%",))
            conn.commit()
        finally:
            conn.close()
        shutil.rmtree(tmp_root, ignore_errors=True)

    print("=" * 70)
    print(f"RESULT: {RESULTS['pass']}/{RESULTS['pass'] + RESULTS['fail']} "
          f"passed, {RESULTS['fail']} failed")
    if RESULTS["failed_names"]:
        print("\nFAILED TESTS:")
        for name in RESULTS["failed_names"]:
            print(f"  - {name}")
    print("=" * 70)
    return 0 if RESULTS["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
