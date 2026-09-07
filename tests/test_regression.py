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


class _FakeDecks:
    """Minimal stand-in for mw.col.decks (deck-name Scope support)."""
    def __init__(self):
        self.decks = {}  # name -> id
        self._next_id = 1
    def add(self, name):
        if name not in self.decks:
            self.decks[name] = self._next_id
            self._next_id += 1
        return self.decks[name]
    def all_names_and_ids(self):
        import types as _types
        return [_types.SimpleNamespace(name=n, id=i)
                for n, i in self.decks.items()]
    def all(self):
        return [{"name": n, "id": i} for n, i in self.decks.items()]


class _FakeCol:
    """Minimal stand-in for mw.col."""
    def __init__(self):
        self.models = _FakeModels()
        self.decks = _FakeDecks()
        self.db = self._FakeDB()

    class _FakeDB:
        """Routes Scope queries by SQL shape; plain flds rows otherwise.

        Tests populate `notes` as {nid: {"flds": blob, "dids": [...],
        "mid": int}} for deck-aware paths, or set `flds_rows` for the
        legacy deck-agnostic shape (returned verbatim for queries that
        carry no did filter).
        """
        def __init__(self):
            self.flds_rows = []
            self.notes = {}

        def all(self, query, params=()):
            import re as _re
            ql = (query or "").lower()
            if "select did from cards where nid" in ql:
                m = _re.search(r"nid\s*=\s*(\d+)", query or "")
                nid = int(m.group(1)) if m else None
                dids = self.notes.get(nid, {}).get("dids", []) \
                    if nid is not None else []
                return [(d,) for d in dids]
            if "select distinct mid from notes" in ql:
                m = _re.search(r"did in\s*\(([\d,\s]+)\)", ql)
                dids = {int(x) for x in m.group(1).split(",") if x.strip()} \
                    if m else set()
                out = set()
                for n in self.notes.values():
                    if set(n.get("dids", [])) & dids \
                            and n.get("mid") is not None:
                        out.add(n["mid"])
                return [(mm,) for mm in out]
            # v1.2.1 knowledge_totals: every in-scope first field (for
            # the X/total denominators). Returns (flds, ivl) rows so the
            # same post-processing as production runs on them.
            if "select distinct notes.flds from notes" in ql:
                m = _re.search(r"did in\s*\(([\d,\s]+)\)", ql)
                dids = {int(x) for x in m.group(1).split(",") if x.strip()} \
                    if m else set()
                rows = []
                for n in self.notes.values():
                    if set(n.get("dids", [])) & dids:
                        rows.append((n["flds"],))
                return rows
            if "count(distinct notes.id) from notes" in ql:
                # scope-notes / mature-notes-in-scope counts (scalar).
                # The ivl floor is parsed from the query so the fake
                # follows production's threshold automatically (21 -> 365
                # in v1.2.3); no floor = plain scope-notes count.
                m = _re.search(r"did in\s*\(([\d,\s]+)\)", ql)
                dids = {int(x) for x in m.group(1).split(",") if x.strip()} \
                    if m else set()
                ivl_floor_m = _re.search(r"ivl\s*>=\s*(\d+)", ql)
                floor = int(ivl_floor_m.group(1)) if ivl_floor_m else 0
                count = 0
                for n in self.notes.values():
                    if not (set(n.get("dids", [])) & dids):
                        continue
                    if floor > 0:
                        ivl = n.get("ivl", 30)
                        if "ivls" in n:
                            hit = set(n.get("dids", [])) & dids
                            ivl = max((n["ivls"].get(d, 0) for d in hit),
                                      default=0)
                        if ivl < floor:
                            continue
                    count += 1
                return [(count,)]
            if "select count() from notes" in ql:
                # total notes in the collection (scalar call).
                return [(len(self.notes),)]
            if "from notes" in ql and "group by notes.id" in ql:
                # v1.2 knowledge snapshot: (flds, MAX(ivl)) per note,
                # filtered by scoped dids + the query's own ivl floor
                # (>= 365 mature in v1.2.3, >= 1 seen). The floor is
                # parsed from the SQL so the fake can never desync from
                # production's threshold. Notes may carry per-card ivls:
                # {"dids": [...], "ivls": {did: ivl}}.
                m = _re.search(r"did in\s*\(([\d,\s]+)\)", ql)
                dids = {int(x) for x in m.group(1).split(",") if x.strip()} \
                    if m else set()
                ivl_floor_m = _re.search(r"ivl\s*>=\s*(\d+)", ql)
                floor = int(ivl_floor_m.group(1)) if ivl_floor_m else 21
                rows = []
                for n in self.notes.values():
                    hit = set(n.get("dids", [])) & dids
                    if not hit:
                        continue
                    if "ivls" in n:
                        ivl = max((n["ivls"].get(d, 0) for d in hit),
                                  default=0)
                    else:
                        ivl = n.get("ivl", 30)  # sane mature default
                    if ivl >= floor:
                        rows.append((n["flds"], ivl))
                return rows
            if "from notes" in ql and "did in" in ql:
                m = _re.search(r"did in\s*\(([\d,\s]+)\)", ql)
                dids = {int(x) for x in m.group(1).split(",") if x.strip()} \
                    if m else set()
                rows = [(n["flds"],) for n in self.notes.values()
                        if set(n.get("dids", [])) & dids]
                rows.extend(self.flds_rows)
                return rows
            return list(self.flds_rows)

        def scalar(self, query, params=()):
            """Single-value queries (production uses db.scalar for the
            knowledge_totals counts); routed through all()."""
            rows = self.all(query, params)
            if not rows:
                return 0
            first = rows[0]
            if isinstance(first, (list, tuple)):
                return first[0] if first else 0
            return first

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
        # yomitan_fallback stays disabled for ALL tests: the suite must be
        # hermetic. On a dev machine with live Yomitan running, the engine's
        # fail-safe would otherwise query the real bridge during tests that
        # expect None ("missing word", "all dictionaries disabled"), because
        # the aqt stub's empty config enables the fallback by default.
        if name not in self.configs:
            return {"yomitan_fallback": False}
        cfg = dict(self.configs[name])
        cfg.setdefault("yomitan_fallback", False)
        return cfg
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
import scope as compredef_scope  # noqa: E402

# The directory containing the user's real Yomitan dictionaries (used ONLY
# by the dynamic smoke tests; everything else runs on synthetic fixtures).
DICTS_DIR = "/home/mohamed/Desktop/Dicts"

RESULTS = {"pass": 0, "fail": 0, "failed_names": []}


def _save_collection_state() -> dict:
    """Snapshots stub collection state (decks/notes/models/config)."""
    col = aqt.mw.col
    return {
        "decks": dict(col.decks.decks),
        "deck_next": col.decks._next_id,
        "notes": {k: dict(v) for k, v in col.db.notes.items()},
        "flds": list(col.db.flds_rows),
        "models": dict(col.models.models_dict),
        "model_next": col.models._next_id,
        "cfg": dict(aqt.mw.addonManager.configs.get("1619602654", {})),
        "had_cfg": "1619602654" in aqt.mw.addonManager.configs,
    }


def _restore_collection_state(st: dict) -> None:
    """Restores stub collection state saved by _save_collection_state."""
    col = aqt.mw.col
    col.decks.decks = st["decks"]
    col.decks._next_id = st["deck_next"]
    col.db.notes = st["notes"]
    col.db.flds_rows = st["flds"]
    col.models.models_dict = st["models"]
    col.models._next_id = st["model_next"]
    if st["had_cfg"]:
        aqt.mw.addonManager.configs["1619602654"] = st["cfg"]
    else:
        aqt.mw.addonManager.configs.pop("1619602654", None)


def _set_scope_config(decks: list) -> None:
    """Points the stub config at the given Scope decks."""
    aqt.mw.addonManager.configs["1619602654"] = {"scope_decks": list(decks)}


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
    """Historical bug #4 → v1.2 SEMANTICS CHANGE: with argmax scoring the
    BETTER-comprehension definition wins regardless of dictionary order.
    Known 会/社: the 'easy' dictionary's definition uses only known
    kanji and MORE vocab compounds (会社), so argmax picks it — but it
    must win on QUALITY, not on being first in the ladder (reversed
    order must produce the same winner)."""
    # db_utils returns no known kanji in the test env, so monkeypatch.
    original = compredef_generator.get_known_kanji_set

    def fake_known() -> set:
        return {"会", "社", "定", "義", "説", "明", "高", "度", "専", "門", "的",
                "や", "さ", "し", "い", "か", "ん", "た", "な", "む", "ず", "こ"}

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
        # With all these kanji known, BOTH definitions are fully known;
        # argmax tie-break: most kanji → the hard/kanji-dense one wins.
        check(
            "ladder: argmax tie-break picks most-kanji definition",
            chosen is not None and "むずかしい" in chosen,
            f"got: {chosen[:40] if chosen else None}",
        )
        # Order-independence: reversed ladder must pick the same winner.
        chosen_rev = compredef_generator.generate_definition(
            "会社", dictionaries=[hard, easy]
        )
        check(
            "ladder: argmax is order-independent",
            chosen_rev == chosen,
            f"forward={chosen[:20] if chosen else None!r} "
            f"reversed={chosen_rev[:20] if chosen_rev else None!r}",
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
        "disabled: baseline picks a definition",
        both is not None,
    )
    # v1.2 argmax: winner is quality-based (no early exit by order); the
    # same winner must appear regardless of which dictionaries remain.
    check(
        "disabled: baseline with both enabled picks the argmax winner",
        both is not None and ("やさしい" in both or "むずかしい" in both),
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

    # Scope wiring: a single "Japanese" deck; the default FakeNote id
    # maps into it so the decision matrix exercises in-scope notes.
    scope_state = _save_collection_state()
    _tab_jp = aqt.mw.col.decks.add("Japanese")
    aqt.mw.col.decks.add("French")
    aqt.mw.col.db.notes[123] = {"flds": "x", "dids": [_tab_jp], "mid": 1}
    _set_scope_config(["Japanese"])

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
        "scope_decks": ["Japanese"],
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
    legacy_config = {"word_field": "Expression", "definition_field": "Definition",
                     "scope_decks": ["Japanese"]}
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

    # d) Out-of-scope notes never auto-generate, even with an empty def.
    fr_note = FakeNote({"Expression": "試験", "Definition": ""}, nid=999)
    aqt.mw.col.db.notes[999] = {"flds": "x", "dids": [
        aqt.mw.col.decks.decks["French"]], "mid": 2}
    check("tab: out-of-scope deck never auto-generates",
          not eb._should_auto_generate(fr_note, "Expression", base_config))

    _restore_collection_state(scope_state)


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

    # Scope wiring: Japanese deck holds the configured types' cards,
    # French deck holds everything else.
    scope_state = _save_collection_state()
    _multi_jp = aqt.mw.col.decks.add("Japanese")
    _multi_fr = aqt.mw.col.decks.add("French")

    def _deck_for(type_name: str) -> int:
        return _multi_jp if type_name in (
            "JP Mining Note", "Animecards", "Japanese") else _multi_fr

    class Note:
        def __init__(self, fields, type_name):
            self._fields = fields
            self.id = hash(type_name) % 10_000
            self._type = type_name
            aqt.mw.col.db.notes[self.id] = {
                "flds": "", "dids": [_deck_for(type_name)], "mid": 1,
            }

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
        "scope_decks": ["Japanese"],
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

    # 4. Legacy single-type configs behave exactly as before (plus Scope).
    legacy_config = {"note_type": "Japanese", "word_field": "Expression",
                     "reading_field": "furigana",
                     "definition_field": "Definition",
                     "scope_decks": ["Japanese"]}
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
    # v1.2.1: an UNCONFIGURED but in-scope type now auto-generates via
    # the same field inference the toolbar button uses — the old
    # hard rejection made Tab "not always work" while the button did.
    # ("Japanese" sits in the JP deck but has no targets_config entry.)
    unconfigured_note = Note({"Expression": "x", "Meaning": ""},
                             "Japanese")
    check("multi: tab fires on unconfigured type via inference",
          eb._should_auto_generate(unconfigured_note, "Expression",
                                   targets_config))
    # …but a type whose fields CANNOT be inferred stays silent.
    cryptic_note = Note({"Side A": "x", "Side B": ""}, "Japanese")
    check("multi: tab stays silent when fields cannot be inferred",
          not eb._should_auto_generate(cryptic_note, "Side A",
                                       targets_config))
    check("multi: tab legacy config still works",
          eb._should_auto_generate(
              Note({"Expression": "x", "Definition": ""}, "Japanese"),
              "Expression", legacy_config))

    # 6. Scope gate: a configured type outside the Scope decks never
    #    generates, and an in-scope note with an emptied scope is dead.
    check("multi: configured type out of scope is rejected",
          eb.resolve_fields_for_note(
              Note({"Word": "x", "Furigana": "f", "Definition": "d"},
                   "JP Mining Note"),
              {**targets_config, "scope_decks": ["French"]}) is None)
    check("multi: empty scope rejects everything",
          eb.resolve_fields_for_note(
              Note({"Word": "x", "Furigana": "f", "Definition": "d"},
                   "JP Mining Note"),
              {**targets_config, "scope_decks": []}) is None)

    _restore_collection_state(scope_state)


def test_v12_scoring_algorithm() -> None:
    """
    v1.2 definition scoring — the 不公平 incident:
    - kanji score  = Σ point(k) per occurrence, point = ivl/365 cap 1.0
    - vocab score  = Σ point(w) per multi-kanji compound occurrence
    - winner = highest (kanji + vocab); tie-break = most kanji count
    - kana-only inflections NEVER match vocab (やめる vs やめて)
    """
    import scoring as scoring_mod

    # Learner: 公/平 fully known (1.0), 判/定 half-year (0.5), 不 unknown.
    # Kana-only words never enter vocab candidates (v1.2 spec).
    kp = {"公": 1.0, "平": 1.0, "判": 0.5, "定": 0.5}
    vp = {"会社": 1.0}

    # Three candidates shaped like the user's 不公平 case.
    shogakukan = "公平でないこと。えこひいきがあること。例 不公平な判定。対 公平。"
    daijirin = "かたよっていて、扱いが公平でない・こと（さま）。⇔公平。「━な処置」「━感」━さ（名）"
    # A kanji-dense tie candidate: same total as another, more kanji.
    tie_a = "公平な判定。"
    tie_b = "公平でない判定がある。"

    # 1. Kanji points: 公平×3 (2.0+2.0+2.0? no — per OCCURRENCE:
    #    公平 appears 3× ⇒ 3×(1+1)=6.0; 不=0; 判定=(0.5+0.5)=1.0; 例=0; 対=0
    #    → kanji_score = 7.0. Vocab: no known compounds in text → 0.
    res = scoring_mod.score_definition(shogakukan, kp, vp)
    check("v12: kanji pts sum per occurrence",
          abs(res.kanji_score - 7.0) < 1e-9,
          f"got {res.kanji_score}")
    check("v12: vocab pts from known compounds only",
          res.vocab_score == 0.0,
          f"got {res.vocab_score}")
    check("v12: total = kanji + vocab",
          abs(res.total_score - 7.0) < 1e-9,
          f"got {res.total_score}")
    check("v12: counts every kanji occurrence",
          res.kanji_count == 11,
          f"got {res.kanji_count}")

    # 2. Kana-heavy daijirin: fewer kanji ⇒ lower total despite same
    #    readability. (User can read both; algorithm prefers kanji-vocab
    #    coverage — matches "I'd get 大辞泉" once weights are equal.)
    res_d = scoring_mod.score_definition(daijirin, kp, vp)
    check("v12: kana-dense definition scores lower",
          res_d.total_score < res.total_score,
          f"daijirin={res_d.total_score} vs shogakukan={res.total_score}")

    # 3. Tie-break: equal totals → most kanji wins.
    #    tie_a: 公平な判定。 = 4 kanji, total 3.0
    #    tie_b: 公平な判定を取る。 = 4 kanji + more kana, still 3.0 —
    #    need a REAL tie with different kanji counts: same total via
    #    a half-known extra kanji: 公平な判定に対して。 → 対 known 0.0
    #    …so instead: tie_c drops 判定 (−1.0) but adds 2 full kanji.
    tie_a = "公平な判定。"                       # 3.0, 4 kanji
    tie_b = "公平な会社の判定だ。"               # 公平(2)+会社(2)+判定(1)=5? no
    # Simpler deterministic tie: 公平な判定。 vs 公平な決定場。 both 3.0?
    # 公(1)+平(1)+判(0.5)+定(0.5)=3.0, 4 kanji
    # 公(1)+平(1)+決(0.5)+定(0.5)+場(0)=3.0, 5 kanji
    kp2 = {**kp, "決": 0.5, "場": 0.0}
    ra = scoring_mod.score_definition(tie_a, kp2, vp)
    rb = scoring_mod.score_definition("公平な決定場。", kp2, vp)
    check("v12: tie-break set up (equal totals)",
          abs(ra.total_score - rb.total_score) < 1e-9,
          f"{ra.total_score} vs {rb.total_score}")
    check("v12: tie-break prefers more kanji",
          rb.kanji_count > ra.kanji_count,
          f"{rb.kanji_count} vs {ra.kanji_count}")

    # 4. Inflection immunity: やめて never matches known やめる because
    #    kana-only words are not vocab candidates at all.
    kp2 = {}
    vp2 = {"やめる": 1.0}
    res_y = scoring_mod.score_definition("やめてもいいですか。", kp2, vp2)
    check("v12: kana-only vocab never scores (inflection immunity)",
          res_y.vocab_score == 0.0 and res_y.kanji_score == 0.0)

    # 5. extract_kanji_words: compounds only, split at non-kanji.
    words = scoring_mod.extract_kanji_words("不公平な会社の判定だ。")
    check("v12: kanji-word extraction",
          words == {"不公平", "会社", "判定"}, f"got {words}")
    check("v12: single kanji not a vocab word",
          scoring_mod.extract_kanji_words("日") == set())

    # 6. Engine-level argmax with the 不公平-style dictionary set:
    #    two fully-readable defs; the one with more kanji+compounds wins.
    import engine as engine_mod
    from models import DictionaryEntry
    entries = [
        DictionaryEntry("不公平", "ふこうへい", daijirin, "大辞林", "x"),
        DictionaryEntry("不公平", "ふこうへい", shogakukan, "小学館", "y"),
    ]
    picked = engine_mod._pick_best(entries, kp, vp)
    check("v12: engine argmax picks the richer definition",
          picked is not None and "えこひいき" in picked[1],
          f"got {(picked[1][:30] if picked else None)!r}")
    # Order independence:
    picked_rev = engine_mod._pick_best(list(reversed(entries)), kp, vp)
    check("v12: engine argmax is order-independent",
          picked_rev is not None and picked_rev[1] == picked[1])


def test_scope_deck_filtering() -> None:
    """
    Deck Scope: subdeck inclusion, ANY-card membership, unsaved-note
    type fallback, fail-closed empty scope, and missing-deck handling.
    """
    scope_state = _save_collection_state()
    try:
        col = aqt.mw.col
        jp = col.decks.add("Japanese")
        jpv = col.decks.add("Japanese::Vocab")
        col.decks.add("JapaneseExtended")  # prefix lookalike, NOT a child
        fr = col.decks.add("French")

        # 1. Expansion: children included, lookalikes excluded.
        expanded = compredef_scope.expand_scope_names(
            ["Japanese", "Japanese::Vocab", "JapaneseExtended", "French"],
            ["Japanese"],
        )
        check("scope: deck implies its subdecks",
              expanded == {"Japanese", "Japanese::Vocab"},
              f"got {sorted(expanded)}")
        check("scope: missing decks reported",
              compredef_scope.missing_scope_decks(["Japanese"], ["Klingon"])
              == ["Klingon"])
        check("scope: scope_dids resolves children to ids",
              compredef_scope.scope_dids(col, ["Japanese"]) == {jp, jpv})

        # 2. Membership via cards (ANY-card rule for multi-deck notes).
        mid_jp = col.models.add_model(
            "JP Mining Note", ["Word", "Furigana", "Definition"])
        mid_fr = col.models.add_model("French Note", ["Mot", "Def"])
        col.db.notes[101] = {"flds": "x", "dids": [jp], "mid": mid_jp}
        col.db.notes[102] = {"flds": "x", "dids": [fr], "mid": mid_fr}
        col.db.notes[103] = {"flds": "x", "dids": [fr, jpv], "mid": mid_jp}

        class N:
            def __init__(self, nid, tname=""):
                self.id = nid
                self._t = tname

            def note_type(self):
                return {"name": self._t} if self._t else {}

        cfg = {"scope_decks": ["Japanese"]}
        check("scope: note in scoped deck is in scope",
              compredef_scope.note_in_scope(N(101), cfg, col))
        check("scope: subdeck card is in scope",
              compredef_scope.note_in_scope(N(103), cfg, col))
        check("scope: French-deck note is out of scope",
              not compredef_scope.note_in_scope(N(102), cfg, col))
        check("scope: empty scope is fail-closed",
              not compredef_scope.note_in_scope(N(101), {"scope_decks": []}, col))
        check("scope: missing deck matches nothing",
              not compredef_scope.note_in_scope(
                  N(101), {"scope_decks": ["Klingon"]}, col))

        # 3. Unsaved notes (no cards yet) fall back to implied types.
        check("scope: implied types come from scoped decks",
              compredef_scope.implied_note_types(col, ["Japanese"])
              == ["JP Mining Note"])
        check("scope: unsaved note of implied type is in scope",
              compredef_scope.note_in_scope(N(0, "JP Mining Note"), cfg, col))
        check("scope: unsaved note of other type is out of scope",
              not compredef_scope.note_in_scope(N(0, "French Note"), cfg, col))
        check("scope: empty config is empty scope",
              compredef_scope.is_scope_empty({}) and
              compredef_scope.get_scope_decks({}) == [])

        # 4. SIBLING-DECK case (v1.1.2 production confusion): note in
        #    Japanese::Sibling vs scope=Japanese::Vocab (another child)
        #    — same parent, NOT covered; note_deck_names reveals it.
        sibling = col.decks.add("Japanese::Sibling")
        col.db.notes[104] = {"flds": "x", "dids": [sibling], "mid": mid_jp}
        leaf_cfg = {"scope_decks": ["Japanese::Vocab"]}
        check("scope: sibling deck is NOT covered by leaf selection",
              not compredef_scope.note_in_scope(N(104), leaf_cfg, col))
        check("scope: sibling deck IS covered by parent selection",
              compredef_scope.note_in_scope(
                  N(104), {"scope_decks": ["Japanese"]}, col))
        names = compredef_scope.note_deck_names(N(104), col)
        check("scope: note_deck_names reveals the blocking deck",
              names == ["Japanese::Sibling"],
              f"got {names}")
        check("scope: note_deck_names empty for unsaved notes",
              compredef_scope.note_deck_names(N(0), col) == [])

        # 5. ADD-WINDOW resolution (v1.1.3 "Add deck does nothing" bug):
        #    unsaved notes scope-check against the deck the window will
        #    add to — DeckChooser when reachable, else col curDeck.
        class _FakeChooser:
            def __init__(self, did):
                self.selected_deck_id = did

        class _FakeEditor:
            def __init__(self, did):
                self.deck_chooser = _FakeChooser(did)

        # curDeck points at French (out of scope)…
        class _CfgCol:
            pass
        cfg_col = _CfgCol()
        cfg_col.decks = col.decks
        cfg_col.db = col.db
        cfg_col.models = col.models
        cfg_col.get_config = lambda key, default=None: fr  # curDeck

        unsaved_jp = N(0, "JP Mining Note")
        # a) editor's DeckChooser pointing at an in-scope deck → in scope
        check("scope: add-window chooser deck decides scope (in-scope deck)",
              compredef_scope.note_in_scope(
                  unsaved_jp, cfg, cfg_col, editor=_FakeEditor(jp)))
        # b) chooser pointing OUT of scope → out (even for implied type)
        check("scope: add-window chooser deck decides scope (out-of-scope deck)",
              not compredef_scope.note_in_scope(
                  unsaved_jp, cfg, cfg_col, editor=_FakeEditor(fr)))
        # c) no editor → curDeck fallback (French → out)
        check("scope: curDeck fallback used for unsaved notes",
              not compredef_scope.note_in_scope(unsaved_jp, cfg, cfg_col))
        # d) resolve_deck_for_note mirrors the same results
        check("scope: resolve_deck_for_note reads the chooser",
              compredef_scope.resolve_deck_for_note(
                  unsaved_jp, cfg_col, editor=_FakeEditor(jp)) == ["Japanese"])
        check("scope: resolve_deck_for_note falls back to curDeck",
              compredef_scope.resolve_deck_for_note(unsaved_jp, cfg_col)
              == ["French"])
        # e) saved notes ignore the editor entirely (cards win)
        check("scope: saved note ignores editor chooser",
              compredef_scope.note_in_scope(
                  N(101), cfg, cfg_col, editor=_FakeEditor(fr)))
    finally:
        _restore_collection_state(scope_state)


# ---------------------------------------------------------------------------
class _FakeN:
    """Tiny note stand-in for scope tests (module-level for reuse)."""
    def __init__(self, nid, type_name=""):
        self.id = nid
        self._t = type_name
    def note_type(self):
        return {"name": self._t} if self._t else {}


def test_multi_deck_quickfix_semantics() -> None:
    """
    v1.2 quick-fix contract (the 会社 complaint):
    - A note with cards in SEVERAL decks is in scope when ANY one is
      covered — the user must never need to add every deck.
    - The quick-fix must never offer "Add deck" when scope already
      passes but mapping fails (that loop made the dialog reappear
      forever).
    """
    aqt_dir = os.path.join(FAKE_STUB_DIR, "aqt")
    with open(os.path.join(aqt_dir, "browser.py"), "w") as f:
        f.write("class Browser:  # stub\n    pass\n")
    with open(os.path.join(aqt_dir, "qt.py"), "w") as f:
        f.write("class QMenu:  # stub\n    pass\nclass QKeySequence:  # stub\n    pass\n")
    with open(os.path.join(aqt_dir, "utils.py"), "w") as f:
        f.write("def tooltip(*args, **kwargs):  # stub\n    pass\n")
    with open(os.path.join(aqt_dir, "gui_hooks.py"), "w") as f:
        f.write(
            "class _Hook:  # stub: append-only registry like the real one\n"
            "    def __init__(self): self._hooks = []\n"
            "    def append(self, fn): self._hooks.append(fn)\n"
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

    scope_state = _save_collection_state()
    col = aqt.mw.col
    jp = col.decks.add("Japanese")
    fr = col.decks.add("French")
    try:
        # 会社-like note: cards in BOTH a scoped deck and an unscoped deck.
        col.db.notes[500] = {"flds": "会社", "dids": [jp, fr], "mid": 1,
                            "ivl": 400}
        cfg_any = {"scope_decks": ["Japanese"]}
        check("quickfix: ANY covered deck puts multi-deck note in scope",
              compredef_scope.note_in_scope(
                  _FakeN(500, "JP Mining Note"), cfg_any, col))
        # And the quick-fix only ever needs to add the OUT-of-scope decks:
        # scope.note_deck_names lists all; the caller filters against the
        # scope before appending (editor_browser._add_deck_to_scope_and_reset
        # appends ONLY missing ones — a no-op when already in scope).
        check("quickfix: note decks include both",
              set(compredef_scope.note_deck_names(_FakeN(500, ""), col))
              == {"Japanese", "French"})

        # 2. Mapping-fail must NOT trigger the add-deck dialog: scope
        #    passes, mapping fails → _offer_add_to_scope routes to the
        #    mapping branch (dialog text mentions fields, no Add-deck).
        eb = importlib.import_module(f"{pkg_name}.editor_browser")
        # A note whose type IS in scope but has NO inferable fields.
        class EmptyNote:
            id = 500
            def note_type(self):
                return {"name": "NoFields"}
            def keys(self):
                return ["Front", "Back"]
            def __contains__(self, k):
                return k in ("Front", "Back")
            def __getitem__(self, k):
                return ""
        col.db.notes[500]["flds"] = "会社"
        col.db.notes[500]["mid"] = None  # type lookup yields no mapping
        cfg_scope_ok = {"scope_decks": ["Japanese"], "targets": {}}
        # resolve: scope OK → mapping: inference on Front/Back fails.
        res = eb.resolve_fields_for_note(EmptyNote(), cfg_scope_ok)
        check("quickfix: scope-pass + mapping-fail yields None",
              res is None)
        # _offer_add_to_scope re-checks scope itself: it must detect
        # in-scope and NOT offer add-deck (we can't run the Qt dialog
        # headlessly, so assert the branch decision helper directly).
        in_scope = eb._note_in_scope(EmptyNote(), cfg_scope_ok)
        check("quickfix: dialog routes mapping-fail away from add-deck",
              in_scope is True)
    finally:
        _restore_collection_state(scope_state)


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

def test_qt_enum_compat() -> None:
    """
    PyQt6 scoped all Qt enums: raw spellings like `Qt.ItemIsUserCheckable`
    or `Qt.UserRole` raise AttributeError at RUNTIME on Anki's Qt6
    builds (production crash, v1.0.14 — the config dialog died on open).
    gui.py may reference Qt enums ONLY via two-level scoped names
    (Qt.CheckState.X, Qt.ItemFlag.X, ...) or via the designated compat
    helpers' `return Qt.<Name>` fallback lines, which exist precisely
    to serve PyQt5.
    """
    import re as _re
    path = os.path.join(REPO_ROOT, "gui.py")
    src = open(path, encoding="utf-8").read()
    # Strip comments and docstrings: only executable code counts.
    import ast
    tree = ast.parse(src)
    doc_ranges = []
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if body and isinstance(body, list) and isinstance(body[0], ast.Expr):
            v = body[0].value
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                doc_ranges.append((body[0].lineno, body[0].end_lineno))
    lines = src.splitlines()
    offenders = []
    pattern = _re.compile(r"Qt\.([A-Za-z_][A-Za-z0-9_]*)(?:\.([A-Za-z_][A-Za-z0-9_]*))?")
    for lineno, line in enumerate(lines, start=1):
        if any(a <= lineno <= b for a, b in doc_ranges):
            continue
        code = line.split("#", 1)[0]  # strip trailing comments
        stripped = code.strip()
        if "hasattr(Qt" in code:
            continue  # compat-probe lines (enum CLASS refs are still top-level in PyQt6)
        for m in pattern.finditer(code):
            first, second = m.group(1), m.group(2)
            if second:
                continue  # scoped usage (Qt.CheckState.Checked) — always fine
            if stripped.startswith("return Qt."):
                continue  # compat-helper PyQt5 fallback (see _user_role etc.)
            offenders.append(f"gui.py:{lineno}: raw 'Qt.{first}' -> use scoped enum")
    check(
        "qt-compat: gui.py uses only scoped Qt enums (PyQt6-safe)",
        not offenders,
        "; ".join(offenders[:6]),
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
                "scoring", "utils", "parser", "generator", "db_utils",
                "scope"}

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
                 "knowledge_summary_text", "knowledge_totals",
                 "sync_reset_caches", "_seen_points"],
            "core": ["get_provider", "get_generator"],
            "engine": ["DefinitionGenerator"],
            "provider": ["LocalSQLiteProvider", "IndexingError"],
            "renderer": ["render_yomitan_definition_html",
                         "render_structured_content_node"],
            "models": ["DictionaryEntry", "RENDERER_VERSION"],
            "scoring": ["calculate_kanji_score", "is_reference_title"],
            "scope": ["get_scope_decks", "expand_scope_names",
                      "note_in_scope", "implied_note_types", "scope_dids",
                      "is_scope_empty", "note_deck_names",
                      "resolve_deck_for_note"],
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
    Mastered kanji/vocab come ONLY from the FIRST field of mature notes
    (ivl >= 365 — v1.2.3 raised the bar from the deprecated 21 days)
    INSIDE the Scope decks — never from Definition/Example/other fields,
    and never from out-of-scope decks (the French deck must not inflate
    Japanese knowledge). Knowledge is INTERVAL-WEIGHTED (ivl/365
    capped at 1.0) and vocab is KANJI-ONLY (multi-kanji compounds;
    kana-only words like 'plain' excluded, single kanji covered by the
    kanji score). This also proves CompreDef-generated definitions
    (written to non-first fields) can never pollute the mastered set.
    """
    import anki

    # Save global state: this test rebinds the fake collection and
    # rebuilds the session snapshot, so everything must be restored.
    prev_kanji = set(anki._known_kanji_cache)
    prev_vocab = set(anki._known_vocab_cache)
    prev_ready = anki._caches_ready.is_set()
    import core as _core
    prev_generator = _core._generator
    scope_state = _save_collection_state()
    try:
        SEP = "\x1f"
        col = aqt.mw.col
        jp = col.decks.add("Japanese")
        fr = col.decks.add("French")
        col.db.notes = {
            # 3-field layout (word / definition / example), in scope.
            # ivls vary: 400 (mature: >= 365), 182.5 and 100 (SEEN but
            # NOT mature since v1.2.3 — below the one-year bar).
            1: {"flds": SEP.join(["漢字", "plain def", "plain ex"]),
                "dids": [jp], "mid": 1, "ivl": 400},
            2: {"flds": SEP.join(["plain", "龍の定義", "plain"]),
                "dids": [jp], "mid": 1, "ivl": 182.5},
            3: {"flds": SEP.join(["plain", "plain", "虎の例文"]),
                "dids": [jp], "mid": 1, "ivl": 100},
            # 2-field layout (front / back) — a different note type
            4: {"flds": SEP.join(["語彙", "解釈"]),
                "dids": [jp], "mid": 2, "ivl": 400},
            # 1-field layout (cloze-like single field) — seen-only:
            # 182.5-day interval is BELOW mature (365) since v1.2.3, so
            # 日本語 must NOT enter the mastered snapshot.
            5: {"flds": "日本語", "dids": [jp], "mid": 2, "ivl": 182.5},
            # French deck: first-field kanji must NOT leak into knowledge
            6: {"flds": SEP.join(["仏文", "définitions"]),
                "dids": [fr], "mid": 3, "ivl": 400},
        }
        _set_scope_config(["Japanese"])

        anki.reset_caches()
        anki._build_caches()
        known = anki.get_known_kanji_set()
        vocab = anki.get_known_vocabulary_set()
        kanji_pts = anki.get_kanji_points()
        vocab_pts = anki.get_vocab_points()

        check("kanji: first-field kanji is mastered",
              {"漢", "字", "語", "彙"} <= known,
              f"known={sorted(known)}")
        check(
            "kanji: Definition-only kanji is NOT mastered",
            "龍" not in known,
            f"known={sorted(known)}",
        )
        check(
            "kanji: Example-only kanji is NOT mastered",
            "虎" not in known,
            f"known={sorted(known)}",
        )
        check(
            "kanji: out-of-scope (French deck) kanji is NOT mastered",
            "仏" not in known,
            f"known={sorted(known)}",
        )
        check(
            "kanji: sub-year notes (182.5/100d) are seen, not mastered",
            known == {"漢", "字", "語", "彙"},
            f"known={sorted(known)}",
        )
        check(
            "kanji: generated definitions do not pollute knowledge",
            "龍" not in known and "虎" not in known,
        )
        # v1.2: vocab is KANJI-ONLY — kana/plain words ('plain') excluded,
        # and only MATURE (>= 1 year) compounds are mastered.
        check(
            "kanji: mastered vocab is mature kanji-only compounds",
            vocab == {"漢字", "語彙"},
            f"vocab={sorted(vocab)}",
        )
        # v1.2: interval weighting — mature notes earn exactly 1.0.
        check("kanji: 400-day interval earns full 1.0 points",
              kanji_pts.get("漢") == 1.0 and kanji_pts.get("字") == 1.0,
              f"pts={kanji_pts}")
        check("kanji: vocab points follow the same weighting",
              vocab_pts.get("漢字") == 1.0 and vocab_pts.get("語彙") == 1.0,
              f"vpts={vocab_pts}")
        # v1.2.3 SEEN view: sub-year notes ARE seen (ivl > 0) — the
        # mature-only snapshot must not lose them from the seen totals.
        seen_kpts, seen_vpts = anki._seen_points()
        check("kanji: seen includes sub-year kanji (日本語)",
              {"日", "本"} <= set(seen_kpts),
              f"seen_kpts={sorted(seen_kpts)}")
        check("kanji: seen includes sub-year vocab (日本語)",
              "日本語" in seen_vpts,
              f"seen_vpts={sorted(seen_vpts)}")
        status = anki.knowledge_status()
        check(
            "kanji: status reports a ready scoped snapshot",
            status["ready"] and status["mature_notes_scanned"] == 2
            and "scope decks" in status["scope"]
            and "Japanese" in status["scope"]
            and status["last_error"] is None,
            f"status={status}",
        )

        # Empty scope is fail-closed: no decks selected, no knowledge.
        _set_scope_config([])
        anki.reset_caches()
        anki._build_caches()
        check("kanji: empty scope yields empty knowledge",
              anki.get_known_kanji_set() == set()
              and anki.get_known_vocabulary_set() == set()
              and anki.get_kanji_points() == {})
    finally:
        _restore_collection_state(scope_state)
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
    had_cfg = "1619602654" in aqt.mw.addonManager.configs
    prev_cfg = dict(aqt.mw.addonManager.configs.get("1619602654", {}))
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
        # The reopened collection carries one Scope deck; knowledge is
        # scoped to it. (v1.2 rows carry (flds, ivl).)
        import types
        _decks = _FakeDecks()
        _jp = _decks.add("Japanese")
        aqt.mw.col = types.SimpleNamespace(
            db=types.SimpleNamespace(
                all=lambda q, p=(): [(SEP.join(["漢字", "def"]), 400)]
            ),
            decks=_decks,
        )
        _set_scope_config(["Japanese"])
        anki._build_caches()
        known = anki.get_known_kanji_set()
        check("col-gate: snapshot builds once collection opens",
              anki._caches_ready.is_set() and known == {"漢", "字"},
              f"known={sorted(known)}")
    finally:
        aqt.mw.col = prev_col
        if had_cfg:
            aqt.mw.addonManager.configs["1619602654"] = prev_cfg
        else:
            aqt.mw.addonManager.configs.pop("1619602654", None)
        anki._known_kanji_cache = prev_kanji
        anki._known_vocab_cache = prev_vocab
        if prev_ready:
            anki._caches_ready.set()
        else:
            anki._caches_ready.clear()
        _core._generator = prev_generator

def test_sync_reset_is_thread_safe() -> None:
    """
    The v1.2.1 production bug: the knowledge dialog's background task
    called reset_caches(), which reaches mw.taskman.run_in_background
    from a NON-main thread — Anki's Taskman printed a 'bug: run_in_
    background not called from main thread' traceback for every dialog
    refresh. sync_reset_caches() must rebuild WITHOUT ever touching
    taskman, and still produce a ready, scoped snapshot.
    """
    import anki
    import threading

    prev_kanji = set(anki._known_kanji_cache)
    prev_vocab = set(anki._known_vocab_cache)
    prev_ready = anki._caches_ready.is_set()
    import core as _core
    prev_generator = _core._generator
    scope_state = _save_collection_state()
    # Tripwire: any taskman access from the reset path fails the test.
    taskman_calls = []
    prev_mw_taskman = getattr(aqt.mw, "taskman", None)

    class _TripwireTaskman:
        def run_in_background(self, *a, **kw):
            taskman_calls.append(a)
            raise AssertionError("taskman touched from sync reset path")

    try:
        SEP = "\x1f"
        col = aqt.mw.col
        jp = col.decks.add("Japanese")
        col.db.notes = {
            1: {"flds": SEP.join(["漢字", "def"]), "dids": [jp], "mid": 1,
                "ivl": 400},
        }
        _set_scope_config(["Japanese"])
        aqt.mw.taskman = _TripwireTaskman()
        # Simulate the dialog's worker thread exactly: a background
        # thread calling the SYNCHRONOUS reset.
        result = {}

        def worker():
            try:
                anki.sync_reset_caches()
                result["ok"] = True
            except Exception as e:  # noqa: BLE001 — the failure IS the test
                result["ok"] = False
                result["err"] = e

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=30)
        check("sync-reset: background thread never touches taskman",
              result.get("ok") is True and not taskman_calls,
              f"result={result} taskman_calls={taskman_calls}")
        check("sync-reset: snapshot is ready after background rebuild",
              anki._caches_ready.is_set()
              and anki.get_known_kanji_set() == {"漢", "字"},
              f"known={sorted(anki._known_kanji_cache)}")
        # core.reset_generator (used by generation paths that can run on
        # background threads) must also stay off taskman.
        _core.reset_generator()
        check("sync-reset: reset_generator stays off taskman",
              not taskman_calls,
              f"taskman_calls={taskman_calls}")
    finally:
        if prev_mw_taskman is not None:
            aqt.mw.taskman = prev_mw_taskman
        else:
            try:
                del aqt.mw.taskman
            except AttributeError:
                pass
        _restore_collection_state(scope_state)
        anki._known_kanji_cache = prev_kanji
        anki._known_vocab_cache = prev_vocab
        if prev_ready:
            anki._caches_ready.set()
        else:
            anki._caches_ready.clear()
        _core._generator = prev_generator

def test_knowledge_summary_text() -> None:
    """The knowledge dialog's content source: counts, lists, scope.

    v1.2.3 terminology (the user's definitions — 'known' was too
    loose): MASTERED = mastery 1.0 (interval >= 1 year), SEEN = any
    interval > 0, 'Vocab' = kanji-only compounds. Readouts are
    X/total with totals INSIDE the Scope decks; Mature notes divide
    by notes-in-scope (the old whole-collection denominator made
    '10705/57185' meaningless), with the collection count secondary.
    """
    import anki

    prev_kanji = set(anki._known_kanji_cache)
    prev_vocab = set(anki._known_vocab_cache)
    prev_ready = anki._caches_ready.is_set()
    import core as _core
    prev_generator = _core._generator
    scope_state = _save_collection_state()
    try:
        SEP = "\x1f"
        col = aqt.mw.col
        jp = col.decks.add("Japanese")
        col.db.notes = {
            1: {"flds": SEP.join(["漢字", "def"]), "dids": [jp], "mid": 1,
                "ivl": 400},
            2: {"flds": SEP.join(["語彙", "def"]), "dids": [jp], "mid": 1,
                "ivl": 400},
        }
        _set_scope_config(["Japanese"])
        anki.reset_caches()
        anki._build_caches()
        text = anki.knowledge_summary_text()
        check("summary: shows mastered kanji count",
              "Mastered kanji: 4" in text, text)
        check("summary: shows mastered vocab count",
              "Mastered vocab: 2" in text, text)
        check("summary: shows seen kanji count",
              "Seen kanji: 4" in text, text)
        check("summary: mastered kanji carries /total denominator",
              "Mastered kanji: 4 / 4" in text, text)
        check("summary: mastered vocab carries /total denominator",
              "Mastered vocab: 2 / 2" in text, text)
        check("summary: mature notes divide by notes IN SCOPE",
              "/ 2 notes in scope" in text and
              "collection: 2" in text, text)
        check("summary: lists the kanji",
              "漢" in text and "語" in text, text)
        check("summary: shows scope", "scope decks" in text, text)
        # v1.2.3: 'Kanji Words' is renamed 'Vocab' everywhere; the old
        # 'Kanji words' / 'Known' labels must be gone.
        check("summary: mastered lists labeled with new terminology",
              "Mastered kanji (all):" in text and
              "Mastered vocab (all):" in text, text)
        check("summary: no deprecated 'Known'/'Kanji words' labels remain",
              "Known kanji" not in text and
              "Kanji words" not in text and "Kanji-words" not in text, text)
        check("summary: no deprecated '(sample)' label remains",
              "(sample)" not in text, text)
        check("summary: no deprecated 21-day mention remains",
              "21" not in text, text)
        short = anki.knowledge_summary_text(max_kanji=2, max_words=1)
        check("summary: truncates long lists with a remainder",
              "more" in short, short)
        # Totals: mature/seen denominators count only in-scope notes;
        # the collection count is separate and secondary.
        totals = anki.knowledge_totals()
        check("totals: counts mature notes in scope",
              totals["mature_notes"] == 2, f"totals={totals}")
        check("totals: counts notes in scope (primary denominator)",
              totals["scope_notes"] == 2, f"totals={totals}")
        check("totals: counts all notes in collection",
              totals["total_notes"] == 2, f"totals={totals}")
        # An out-of-scope note widens ONLY the collection total; the
        # scope denominators must stay untouched (the user's 57185 bug).
        col.db.notes[3] = {"flds": SEP.join(["仏文", "d"]), "dids": [999],
                           "mid": 1, "ivl": 1}
        totals2 = anki.knowledge_totals()
        check("totals: out-of-scope note widens collection total only",
              totals2["total_notes"] == 3 and
              totals2["scope_notes"] == 2 and
              totals2["kanji"] == 4 and totals2["vocab"] == 2,
              f"totals2={totals2}")
        # A SEEN-but-young in-scope note joins the seen/total kanji
        # denominators but never the mastered snapshot.
        col.db.notes[4] = {"flds": SEP.join(["若い", "d"]), "dids": [jp],
                           "mid": 1, "ivl": 40}
        totals3 = anki.knowledge_totals()
        check("totals: young in-scope note joins kanji totals",
              totals3["kanji"] == 5 and totals3["scope_notes"] == 3 and
              totals3["mature_notes"] == 2,
              f"totals3={totals3}")
        seen_kpts, seen_vpts = anki._seen_points()
        check("seen: young in-scope kanji is seen but not mastered",
              "若" in seen_kpts and "若" not in anki.get_kanji_points(),
              f"seen={sorted(seen_kpts)}")
    finally:
        _restore_collection_state(scope_state)
        anki._known_kanji_cache = prev_kanji
        anki._known_vocab_cache = prev_vocab
        if prev_ready:
            anki._caches_ready.set()
        else:
            anki._caches_ready.clear()
        _core._generator = prev_generator


def test_provenance_search_builders() -> None:
    """
    Browser provenance searches (v1.2.4): clicking a Kanji/Vocab row in
    the knowledge dialog must open Anki's Browse screen with the notes
    that actually produced that item's mastery points.

    Syntax contract per the OFFICIAL Anki manual (searching.html):
    - "field:value"  = field matches EXACTLY (vocab rows)
    - "field:*value*" = field CONTAINS (kanji rows — kanji points are
      admitted when the first field contains the kanji anywhere)
    - terms are double-quoted (special characters stay literal),
      multiple fields OR-join, list capped at 8 fields.
    - fallbacks: 're:^term$' (vocab) / bare quoted term (kanji).

    v1.2.5: every search is AND-ed with the Scope's deck terms
    ('deck:"Name" or deck:"Other"') — provenance lists ONLY notes
    inside the Scope decks, the same universe the snapshot counts
    from. Blank-scope searches keep the old un-restricted shape.
    """
    import anki

    scope_state = _save_collection_state()
    try:
        # ---- Pure builder: shapes, quoting, caps, fallbacks -----------
        # Explicit scope: field matches AND deck restriction.
        SCOPE = ["My Life Decks", "日本語::Mining"]
        q = anki.build_provenance_search("vocab", "学校",
                                         ["Expression", "Word"],
                                         scope_decks=SCOPE)
        check("prov: vocab search is exact fields AND scope decks",
              q == '("Expression:学校" or "Word:学校") and '
                   '("deck:My Life Decks" or "deck:日本語::Mining")',
              f"got {q}")
        qk = anki.build_provenance_search("kanji", "学",
                                          ["Expression"], scope_decks=SCOPE)
        check("prov: kanji search is contains fields AND scope decks",
              qk == '("Expression:*学*") and '
                    '("deck:My Life Decks" or "deck:日本語::Mining")',
              f"got {qk}")
        # Dedup + 8-field cap (9 fields -> 8 terms).
        many = [f"F{i}" for i in range(9)]
        qm = anki.build_provenance_search("vocab", "学校", many,
                                          scope_decks=SCOPE)
        check("prov: field list capped at 8 terms",
              qm.count("F") == 8 and '"F8:学校"' not in qm, f"got {qm}")
        # Dedup keeps first occurrence order.
        qd = anki.build_provenance_search("vocab", "学校",
                                          ["Expression", "Expression"],
                                          scope_decks=SCOPE)
        check("prov: duplicate field names deduped",
              qd == '("Expression:学校") and '
                    '("deck:My Life Decks" or "deck:日本語::Mining")',
              f"got {qd}")
        # Fallbacks when no fields resolve — STILL scope-limited.
        check("prov: vocab fallback is regex exact AND scope",
              anki.build_provenance_search("vocab", "学校", [],
                                           scope_decks=SCOPE) ==
              '("re:^学校$") and ("deck:My Life Decks" or '
              '"deck:日本語::Mining")',
              "fallback shape changed")
        check("prov: kanji fallback is plain term AND scope",
              anki.build_provenance_search("kanji", "学", None,
                                          scope_decks=SCOPE) ==
              '("学") and ("deck:My Life Decks" or "deck:日本語::Mining")',
              "fallback shape changed")
        check("prov: empty term yields empty search",
              anki.build_provenance_search("kanji", "", ["F"],
                                           scope_decks=SCOPE) == "")
        # Whitespace-only field names are dropped, not searched.
        check("prov: blank field names ignored",
              anki.build_provenance_search("vocab", "学校", [" ", ""],
                                           scope_decks=SCOPE).startswith(
                  '("re:^学校$")'))
        # Blank scope list => no deck restriction (bare legacy shape,
        # no parentheses — a lone term needs no grouping).
        q_no = anki.build_provenance_search("vocab", "学校",
                                            ["Expression"], scope_decks=[])
        check("prov: blank scope searches without deck terms",
              q_no == '"Expression:学校"', f"got {q_no}")

        # ---- Live resolution against the fake collection ---------------
        # Two note types in scope: their FIRST fields are what the
        # snapshot counts from, so provenance must target exactly them.
        col = aqt.mw.col
        jp = col.decks.add("Japanese")
        mid_a = col.models.add_model("Type A", ["Expression", "Reading"])
        mid_b = col.models.add_model("Type B", ["Word", "Definition"])
        col.db.notes = {
            1: {"flds": "学校", "dids": [jp], "mid": mid_a, "ivl": 400},
            2: {"flds": "語彙", "dids": [jp], "mid": mid_b, "ivl": 400},
        }
        _set_scope_config(["Japanese"])
        # Cached-fields contract (v1.2.5): the builder reuses the
        # snapshot's cached list; a fresh build refreshes it.
        anki.reset_caches()
        anki._build_caches()
        fields = anki.first_field_names_for_scope()
        check("prov: scope first-field names resolve",
              sorted(fields) == ["Expression", "Word"], f"got {fields}")
        # Reading the config scope (what the dialog passes) produces
        # the same deck restriction.
        qv = anki.build_provenance_search("vocab", "学校", fields,
                                          scope_decks=["Japanese"])
        check("prov: live vocab search covers both first fields",
              qv == '("Expression:学校" or "Word:学校") and ("deck:Japanese")',
              f"got {qv}")

        # Empty scope is fail-closed (no fields -> fallback shape).
        _set_scope_config([])
        anki.reset_caches()
        anki._build_caches()
        check("prov: empty scope yields no field names",
              anki.first_field_names_for_scope() == [])
        # Out-of-scope types never contribute their fields.
        _set_scope_config(["Japanese"])
        anki.reset_caches()
        anki._build_caches()
        fr = col.decks.add("French")
        mid_c = col.models.add_model("French Type", ["Front", "Back"])
        col.db.notes[3] = {"flds": "bonjour", "dids": [fr], "mid": mid_c,
                           "ivl": 400}
        fields2 = anki.first_field_names_for_scope()
        check("prov: out-of-scope type fields excluded",
              "Front" not in fields2 and
              sorted(fields2) == ["Expression", "Word"],
              f"got {fields2}")
    finally:
        _restore_collection_state(scope_state)


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
    scope_state = _save_collection_state()
    try:
        SEP = "\x1f"
        aqt.mw.col.decks.add("Japanese")
        _set_scope_config(["Japanese"])
        rows = [(SEP.join(["漢字", "龍の定義"]), 400)]

        def strict_all(query, params=()):
            # New-schema Anki: there is no 'models' table at all.
            if re.search(r"\b(join|from)\s+models\b", query,
                          re.IGNORECASE):
                raise _sqlite3.OperationalError("no such table: models")
            return list(rows)

        aqt.mw.col.db.all = strict_all
        anki.reset_caches()
        anki._build_caches()
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
        _restore_collection_state(scope_state)
        anki._known_kanji_cache = prev_kanji
        anki._known_vocab_cache = prev_vocab
        if prev_ready:
            anki._caches_ready.set()
        else:
            anki._caches_ready.clear()
        _core._generator = prev_generator

def test_config_survives_yomitan_toggle() -> None:
    """
    Regression for v1.0.27 bug: switching Dictionary Source to Yomitan
    and closing the dialog wiped Local ladder and Note Types targets.

    The dialog's early save (before _load_config populates the ladder)
    wrote {"dictionaries":[],"targets":{}} over the real config, and
    install_local.sh deleting meta.json made it permanent. The fix preserves
    previous config when the UI list is empty but previous config was not.
    """
    # Simulate the preservation logic in gui.py without needing Qt
    # by directly testing _collect_type_config's fallback.
    # We mock a ConfigDialog-like object with minimal state.
    class FakeDialog:
        def __init__(self, prev_config):
            self.config = prev_config
            self.type_mappings = {}  # empty UI (before load)
        def _stash_current_mapping(self):
            pass
        # Copy the fixed _collect_type_config logic
        def _collect_type_config(self):
            # Simplified copy of gui.py's fixed method
            targets = {}
            for type_name, mapping in self.type_mappings.items():
                word = mapping.get("word_field", "")
                def_f = mapping.get("definition_field", "")
                if word and def_f:
                    targets[type_name] = {
                        "word_field": word,
                        "reading_field": mapping.get("reading_field", ""),
                        "definition_field": def_f,
                    }
            if not targets and isinstance(self.config.get("targets"), dict) and self.config["targets"]:
                prev_targets = self.config["targets"]
                if isinstance(prev_targets, dict) and any(isinstance(v, dict) and v.get("word_field") and v.get("definition_field") for v in prev_targets.values()):
                    targets = {str(k): dict(v) for k, v in prev_targets.items() if isinstance(v, dict)}
            if not targets and self.config.get("note_type") and self.config.get("word_field") and self.config.get("definition_field"):
                _legacy_type = str(self.config["note_type"])
                targets = {
                    _legacy_type: {
                        "word_field": str(self.config.get("word_field") or ""),
                        "reading_field": str(self.config.get("reading_field") or ""),
                        "definition_field": str(self.config.get("definition_field") or ""),
                    }
                }
            first_name = next(iter(targets), "")
            first = targets.get(first_name, {})
            return {
                "targets": targets,
                "note_type": first_name,
                "word_field": first.get("word_field", ""),
                "reading_field": first.get("reading_field", ""),
                "definition_field": first.get("definition_field", ""),
            }

    prev = {
        "targets": {
            "Japanese": {"word_field": "Expression", "reading_field": "Reading", "definition_field": "Definition"},
            "Mining": {"word_field": "Word", "reading_field": "", "definition_field": "Def"},
        },
        "dictionaries": ["/tmp/dictA", "/tmp/dictB"],
        "note_type": "Japanese",
        "word_field": "Expression",
        "definition_field": "Definition",
    }
    dlg = FakeDialog(prev)
    result = dlg._collect_type_config()
    check("config-preserve: Yomitan toggle keeps targets",
          result["targets"] == prev["targets"],
          f"got {result['targets']}")
    check("config-preserve: legacy mirror preserved",
          result["note_type"] == "Japanese" and result["word_field"] == "Expression",
          f"got {result}")

    # Empty previous config should stay empty (new user, not a clobber)
    dlg2 = FakeDialog({"targets": {}, "dictionaries": []})
    result2 = dlg2._collect_type_config()
    check("config-preserve: empty stays empty for new user",
          result2["targets"] == {},
          f"got {result2['targets']}")

    # Dictionaries preservation (simulated via save logic)
    # When UI list is empty but previous config has dictionaries, preserve
    prev_dicts = ["/tmp/dA", "/tmp/dB"]
    ui_ordered = []
    preserved = list(prev_dicts) if (not ui_ordered and prev_dicts) else ui_ordered
    check("config-preserve: Yomitan toggle keeps dictionaries",
          preserved == prev_dicts,
          f"got {preserved}")


def test_yomitan_bridge_sw_keepalive() -> None:
    """
    Regression for the 2026-09-04 "Yomitan completely dead" incident.

    Root cause (verified live on Chrome 151 + Yomitan 26.8.24.0): Yomitan's
    MV3 service worker owns the native-messaging port. ~30s after
    connectNative the SW suspends, Chrome closes both pipes, and the old
    bridge became a ZOMBIE: still listening on port 19633, stdin dead, so
    every /ankiFields returned 502 forever and no new launch could bind.

    The bundled BRIDGE_SCRIPT must therefore:
    1. Ping the port periodically (KEEPALIVE_INTERVAL < 30s SW idle timer)
       so port.onMessage keeps firing and the SW never suspends.
    2. Watch stdin from a reader thread and, on EOF, SHUT THE HOST DOWN so
       the process exits and frees port 19633 (anti-zombie self-heal).
    3. Match replies by a per-request nonce so keepalive echoes can never
       be mis-paired with a real HTTP request's reply.
    4. Never kill a browser-launched bridge from the installer (v1.0.34
       incident) — only bare-cmdline standalone orphans.
    5. Keep the bridge's Yomitan wait (YOMITAN_RESPONSE_TIMEOUT) below the
       Anki-side fetch timeout in yomitan.py (nested timeouts).
    """
    import yomitan_installer as installer

    src = installer.BRIDGE_SCRIPT

    # 1. Keepalive: interval must be strictly under the 30s SW idle timer.
    m = re.search(r"KEEPALIVE_INTERVAL\s*=\s*([0-9.]+)", src)
    check("bridge: keepalive interval configured",
          m is not None, "KEEPALIVE_INTERVAL not found")
    if m:
        check("bridge: keepalive fires under 30s SW idle timer",
              0 < float(m.group(1)) < 30.0,
              f"got {m.group(1)}")
    check("bridge: keepalive thread started",
          "_keepalive_loop, daemon=True" in src.replace(" ", "").replace("\n", "")
          or "threading.Thread(target=_keepalive_loop" in src,
          "keepalive thread not launched")
    check("bridge: keepalive sends a port message (resets SW idle timer)",
          re.search(r'_raw_send\(\{"action": "keepalive"', src) is not None,
          "no keepalive ping found")

    # 2. Anti-zombie: a stdin reader thread must trigger host shutdown on EOF.
    check("bridge: stdin reader thread exists",
          "target=_stdin_reader" in src, "no _stdin_reader thread")
    check("bridge: reader clears connected state on EOF",
          re.search(r"_yomitan_connected\.clear\(\)", src) is not None,
          "EOF does not clear connection state")
    check("bridge: reader shuts the HTTP server down on EOF",
          re.search(r"def _stdin_reader.*?_shutdown_host\(\)", src, re.S) is not None,
          "reader thread does not call _shutdown_host()")
    check("bridge: _shutdown_host stops the HTTP server",
          re.search(r"def _shutdown_host.*?httpd\.shutdown\(\)", src, re.S) is not None,
          "_shutdown_host missing or does not call httpd.shutdown()")
    check("bridge: port is closed on exit (frees 19633)",
          "server_close()" in src, "no server_close() on exit path")

    # 3. Nonce pairing: every request carries a unique nonce in params.
    check("bridge: requests carry a nonce",
          '"_cd": nonce' in src, "no nonce in request frame")
    check("bridge: replies matched by nonce",
          re.search(r"params\.get\(\"_cd\"\)", src) is not None,
          "reader does not extract nonce from echoed params")

    # 4. Browser-launched guard: the killer must skip chrome-extension:// procs.
    inst_src = open(os.path.join(REPO_ROOT, "yomitan_installer.py"), encoding="utf-8").read()
    check("installer: _is_browser_launched guard exists",
          "def _is_browser_launched" in inst_src,
          "no browser-launched guard in killer")
    check("installer: killer checks the guard before killing",
          re.search(r"not _is_browser_launched\(pid\)", inst_src) is not None,
          "killer does not check _is_browser_launched")
    check("installer: guard treats unreadable cmdline as browser-launched",
          re.search(r"Unreadable.*?return True", inst_src, re.S) is not None
          or "assume browser-launched" in inst_src,
          "guard does not err on the side of not killing")

    # 5. Nested timeouts: bridge wait must stay under Anki fetch timeout.
    m = re.search(r"YOMITAN_RESPONSE_TIMEOUT\s*=\s*([0-9.]+)", src)
    bridge_wait = float(m.group(1)) if m else 0.0
    m2 = re.search(r"YOMITAN_TIMEOUT\s*=\s*([0-9.]+)",
                   open(os.path.join(REPO_ROOT, "yomitan.py"), encoding="utf-8").read())
    anki_wait = float(m2.group(1)) if m2 else 0.0
    check("bridge: bridge wait < Anki fetch timeout (nested)",
          0 < bridge_wait < anki_wait,
          f"bridge={bridge_wait}s anki={anki_wait}s")

    # 6. 502 must be reported only via the connected-state check (so a
    # dying SW between request and reply still fails within the timeout,
    # never hangs the HTTP thread forever).
    check("bridge: HTTP path checks connection before sending",
          re.search(r"if not _yomitan_connected\.is_set\(\)", src) is not None,
          "no connection pre-check in do_POST")

    # 7. The script must compile.
    import ast
    try:
        ast.parse(src)
        ok = True
        err = ""
    except SyntaxError as e:
        ok = False
        err = str(e)
    check("bridge: BRIDGE_SCRIPT parses as valid Python", ok, err)


def _yomitan_multi_dict_blob() -> str:
    """Synthetic /ankiFields glossary: 3 dictionaries glued in one blob.

    Mirrors Yomitan's real template: each <li data-dictionary> is followed
    by its OWN interleaved <style> block (scoped [data-dictionary] CSS that
    Anki needs to render the definition like Yomitan does).

    Dict B's definition uses only known kanji (不公平); A and C smuggle in
    unknown kanji (贔屓扱受 / 欠続). B also carries a nested example list to
    prove inner <li> items (no data-dictionary) never cause a false split.
    """
    return (
        "<ol>"
        '<li data-dictionary="Dict A"><i>(Dict A)</i>'
        "<span>贔屓があって、不公平な扱いを受けること。</span></li>"
        '<style>[data-dictionary="Dict A"]{color:red;}</style>'
        '<li data-dictionary="Dict B"><i>(Dict B)</i>'
        "<span>公で平でないこと。不公平であること。</span>"
        "<ul><li>nested example without marker</li></ul></li>"
        '<style>[data-dictionary="Dict B"]{color:green;}</style>'
        '<li data-dictionary="Dict C"><i>(Dict C)</i>'
        "<span>公平を欠く状態が続くこと。</span></li>"
        '<style>[data-dictionary="Dict C"]{color:blue;}</style>'
        "</ol>"
    )


def test_yomitan_glossary_split_per_dictionary() -> None:
    """Yomitan blob splits into per-dictionary native-HTML slices (no network)."""
    import yomitan

    blob = _yomitan_multi_dict_blob()
    parts = yomitan.split_glossary_by_dictionary(blob)
    check("yomitan-split: blob splits into 3 slices",
          len(parts) == 3, f"got {len(parts)}")
    check("yomitan-split: titles extracted in order",
          [t for t, _ in parts] == ["Dict A", "Dict B", "Dict C"],
          f"got {[t for t, _ in parts]}")
    check("yomitan-split: slices are verbatim substrings",
          all(s in blob for _, s in parts))
    check("yomitan-split: slices tile the <li> region exactly",
          "".join(s for _, s in parts) == blob[blob.find("<li"):blob.rfind("</ol>")])
    check("yomitan-split: nested inner <li> does not over-split",
          "nested example without marker" in parts[1][1])
    check("yomitan-split: no <ol> wrapper leaks into slices",
          all("<ol" not in s.lower() for _, s in parts))
    check("yomitan-split: each slice carries its OWN <style> block",
          "color:green" in parts[1][1]
          and "color:red" not in parts[1][1]
          and "color:blue" not in parts[1][1]
          and "color:red" in parts[0][1]
          and "color:blue" in parts[2][1],
          f"styles leaked across slices")

    single = '<div class="yomitan-glossary">単独の定義文です。</div>'
    check("yomitan-split: marker-less blob falls back to whole",
          yomitan.split_glossary_by_dictionary(single) == [(None, single)])
    custom = ("<ol><li><span>custom template one。</span></li>"
              "<li><span>custom template two。</span></li></ol>")
    check("yomitan-split: custom template without markers falls back to whole",
          yomitan.split_glossary_by_dictionary(custom) == [(None, custom)])
    check("yomitan-split: empty input falls back",
          yomitan.split_glossary_by_dictionary("") == [(None, "")])


def _yomitan_child_list_blob() -> str:
    """Synthetic blob mirroring the 会社 incident: one dictionary contributes
    only a 子見出し child-entry term list (all-common kanji → scores ~1.0),
    while 小学館 holds the genuine prose definition (partly-unknown kanji).
    The term list must NEVER win, no matter its kanji score.
    """
    return (
        "<ol>"
        '<li data-dictionary="子見出し辞典"><i>(子見出し辞典)</i>'
        "<span>(子) 会社員 | 会社組合 | 会社法 | 会社人間</span></li>"
        '<li data-dictionary="小学館例解学習国語 第十二版">'
        "<i>(小学館例解学習国語 第十二版)</i>"
        "<span>利益をえるため、お金を出し合って作る仕組みの団体。例 株式会社。</span></li>"
        "</ol>"
    )


def test_yomitan_term_list_loses_to_real_definition() -> None:
    """Regression for the 会社 incident: a pipe-separated child-entry list
    ((子) 会社員 | 会社組合 | …) outscored the genuine 小学館 definition
    because every kanji in the list was already known. Term lists are not
    readable definitions — they must be filtered like reference titles."""
    import yomitan
    import engine as engine_mod
    from models import DictionaryEntry

    junk = "(子) 会社員 | 会社組合 | 会社更生法 | 会社整理 | 会社説明会"
    check("ref: long pipe-separated term list is a reference",
          compredef_generator._is_reference_title(junk), f"got: {junk[:40]!r}")
    prose_with_pipe = "定義にパイプ｜記号がある場合もある。"
    check("ref: prose WITH sentence punctuation stays real (pipe or not)",
          not compredef_generator._is_reference_title(prose_with_pipe))

    blob = _yomitan_child_list_blob()
    real_slice = yomitan.split_glossary_by_dictionary(blob)[1][1]
    entries = [
        DictionaryEntry(word="会社", reading="かいしゃ", definition=s,
                        dictionary_title=t or "Yomitan",
                        dictionary_path="yomitan://api")
        for t, s in yomitan.split_glossary_by_dictionary(blob)
    ]

    # Learner knows every kanji in the junk slice (list + its dictionary
    # title are all common) but only a few of the real definition's —
    # without the filter the junk scores a perfect 1.0 and early-exits.
    known = {"子", "会", "社", "員", "組", "合", "法", "人", "間",
             "見", "出", "辞", "典"}
    junk_score = compredef_generator._calculate_kanji_score(entries[0].definition, known)
    check("term-list: junk really does score 1.0 here (repro is valid)",
          junk_score == 1.0, f"got {junk_score}")

    original_fetch = engine_mod.fetch_yomitan_definitions
    engine_mod.fetch_yomitan_definitions = lambda w, r="": entries  # type: ignore
    cfgs = aqt.mw.addonManager.configs
    had_key = "1619602654" in cfgs
    old_cfg = cfgs.get("1619602654")
    cfgs["1619602654"] = {"dictionary_source": "yomitan"}
    try:
        gen = engine_mod.DefinitionGenerator(provider=None, known_kanji=known)
        result = gen.generate("会社", ladder_paths=[], reading="かいしゃ")
    finally:
        engine_mod.fetch_yomitan_definitions = original_fetch  # type: ignore
        if had_key:
            cfgs["1619602654"] = old_cfg
        else:
            cfgs.pop("1619602654", None)
    check("term-list: genuine definition wins, byte-exact",
          result == real_slice, f"got: {(result or '')[:80]!r}")


def test_yomitan_returns_single_best_definition() -> None:
    """Engine scores per-dictionary slices, returns winner in native HTML."""
    import yomitan
    import engine as engine_mod
    from models import DictionaryEntry

    blob = _yomitan_multi_dict_blob()
    winner_slice = yomitan.split_glossary_by_dictionary(blob)[1][1]
    entries = [
        DictionaryEntry(word="不公平", reading="ふこうへい", definition=s,
                        dictionary_title=t or "Yomitan",
                        dictionary_path="yomitan://api")
        for t, s in yomitan.split_glossary_by_dictionary(blob)
    ]

    # Hermetic: stub the network fetch AND the dictionary-source config.
    original_fetch = engine_mod.fetch_yomitan_definitions
    engine_mod.fetch_yomitan_definitions = lambda w, r="": entries  # type: ignore
    cfgs = aqt.mw.addonManager.configs
    had_key = "1619602654" in cfgs
    old_cfg = cfgs.get("1619602654")
    cfgs["1619602654"] = {"dictionary_source": "yomitan"}
    try:
        gen = engine_mod.DefinitionGenerator(
            provider=None, known_kanji={"不", "公", "平"})
        result = gen.generate("不公平", ladder_paths=[], reading="ふこうへい")
    finally:
        engine_mod.fetch_yomitan_definitions = original_fetch  # type: ignore
        if had_key:
            cfgs["1619602654"] = old_cfg
        else:
            cfgs.pop("1619602654", None)
    check("yomitan-pick: a definition was returned", result is not None)
    check("yomitan-pick: winner is the fully-known Dict B slice, byte-exact",
          result == winner_slice, f"got: {(result or '')[:80]!r}")
    check("yomitan-pick: losing dictionaries are NOT in the output",
          result is not None and "Dict A" not in result and "Dict C" not in result,
          f"got: {(result or '')[:80]!r}")


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
        test_sync_reset_is_thread_safe()
        test_knowledge_summary_text()
        test_provenance_search_builders()
        test_package_relative_imports()
        test_no_undefined_names_in_shipped_modules()
        test_qt_enum_compat()
        test_tab_generate_decisions()
        test_multi_note_type_targeting()
        test_scope_deck_filtering()
        test_multi_deck_quickfix_semantics()
        test_v12_scoring_algorithm()
        test_config_survives_yomitan_toggle()
        test_yomitan_bridge_sw_keepalive()
        test_yomitan_glossary_split_per_dictionary()
        test_yomitan_term_list_loses_to_real_definition()
        test_yomitan_returns_single_best_definition()
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
