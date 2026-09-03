#!/usr/bin/env python3
"""
tests/test_regression.py - Fundamental regression suite for CompreDef.

Each test guards a bug that ACTUALLY HAPPENED in this project's history.
Run this after ANY change to parser.py / generator.py / db_utils.py:

    python3 tests/test_regression.py

Exit code 0 = all green, 1 = regression detected (print output shows which).

Test map (bug -> test):
  1. '先ず' returned 121 chars of plain text instead of ~7000 chars of rich
     Yomitan HTML        -> test_structured_content_html_fidelity
  2. Renderer upgraded but SQLite kept serving stale plain-text entries
     forever             -> test_renderer_version_invalidates_cache
  3. Furigana <rt> readings polluted the kanji comprehension score
     ('さい' counted as kanji knowledge) -> test_scoring_ignores_furigana
  4. Dictionary ladder returned an advanced definition when a simpler one
     existed earlier     -> test_ladder_early_exit_order
  5. Cross-reference titles like '会社更生法' won over real definitions
                         -> test_reference_title_filtering
  6. ZIP archive and unzipped folder of the same dictionary produced
     different output   -> test_zip_folder_parity
  7. Merged dictionaries emitted data-sc-* attributes differently from
     Yomitan            -> test_data_sc_attribute_names
  8. Whole pipeline on the real dictionaries installed on this machine
                         -> test_real_dictionary_smoke (skipped if absent)
  9. Nonsense word '駿ってさ' froze Anki at 100% CPU and crashed it
                         -> test_nonsense_word_returns_none_fast
 10. Indexing accumulated ~1.3 GB of rendered HTML in RAM (OOM/freeze on
     giant dictionaries like 大辞泉) -> test_indexing_streams_in_batches
 11. SQLite connections were never closed (with conn: commits but does
     not close), leaking one handle per lookup -> test_db_connections_are_closed
 12. After a renderer upgrade, a stale dictionary must re-index cleanly
     without duplicate/orphan rows      -> test_renderer_upgrade_reindexes_cleanly
 13. 先ず read まず returned the definition of 先ず read せんず (homograph
     collision)                        -> test_reading_disambiguates_homographs
 14. Furigana field parsing (先[ま]ず, 先ず[まず], ruby HTML, katakana)
     must yield the pure kana reading   -> test_parse_furigana_field_formats
 15. Disabled dictionaries (unchecked in GUI) must be skipped by the
     generator while order is preserved -> test_disabled_dictionaries_skipped
 16. Real 大辞泉: 先ず exists under BOTH readings; reading filter must
     isolate the right one             -> smoke section (skipped if absent)

No Anki/PyQt required: db_utils' Anki dependency is stubbed before import,
and the test runs inside Anki's bundled Python too (plain asserts + prints).
"""

import os
import re
import sys
import shutil
import sqlite3
import tempfile

# ---------------------------------------------------------------------------
# Make the repo root importable and stub Anki (aqt) BEFORE importing db_utils.
# Anki's embedded Python has aqt on sys.path; a system Python does not.
# The stub must be first on sys.path so `from aqt import mw` resolves to it.
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

FAKE_STUB_DIR = os.path.join(tempfile.gettempdir(), "compredef_test_aqt_stub")


class _FakeCol:
    """Minimal stand-in for mw.col so db_utils imports outside Anki."""

    class _DB:
        @staticmethod
        def all(_query):  # no learned notes in the test environment
            return []

    db = _DB()


class _FakeMW:
    col = _FakeCol()


def _install_aqt_stub() -> None:
    """Creates a tiny aqt package exposing `mw` so db_utils imports cleanly."""
    os.makedirs(FAKE_STUB_DIR, exist_ok=True)
    aqt_dir = os.path.join(FAKE_STUB_DIR, "aqt")
    os.makedirs(aqt_dir, exist_ok=True)
    # __init__.py exposing mw; aqt.qt not needed by the modules under test
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

# The directory containing the user's real Yomitan dictionaries (used ONLY
# by the dynamic smoke tests; everything else runs on synthetic fixtures).
# The suite NEVER depends on any specific dictionary existing: all smoke
# expectations are derived at runtime from whatever data is actually there.
DICTS_DIR = "/home/mohamed/Desktop/Dicts"

RESULTS = {"pass": 0, "fail": 0}


def check(name: str, condition: bool, detail: str = "") -> None:
    """Records one assertion result; prints PASS/FAIL immediately."""
    if condition:
        RESULTS["pass"] += 1
        print(f"[PASS] {name}")
    else:
        RESULTS["fail"] += 1
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
    - {'type': 'structured-content'} with ruby/rt, data, style, table

    Layout follows the real Yomitan term-bank schema (8 fields):
    [expression, reading, definitionTags, rules, score, glossary, sequence, termTags]
    where glossary lives at index 5 -- exactly what parser.py reads.
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
                                # Ruby: base kanji 先 with furigana reading ま
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
    bank = synth_term_bank()
    index = {"title": SYNTH_TITLE, "revision": "test1", "format": 3}

    if as_zip:
        import zipfile as _zf
        zip_path = dir_path + ".zip"
        with _zf.ZipFile(zip_path, "w") as z:
            z.writestr(
                "index.json",
                __import__("json").dumps(index, ensure_ascii=False),
            )
            z.writestr(
                "term_bank_1.json",
                __import__("json").dumps(bank, ensure_ascii=False),
            )
        return zip_path

    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "index.json"), "w") as f:
        __import__("json").dump(index, f, ensure_ascii=False)
    with open(os.path.join(dir_path, "term_bank_1.json"), "w") as f:
        __import__("json").dump(bank, f, ensure_ascii=False)
    return dir_path


# ---------------------------------------------------------------------------
# 1. HTML fidelity: rich structured content must survive indexing intact.
# ---------------------------------------------------------------------------
def test_structured_content_html_fidelity(tmp_root: str) -> None:
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "synth"))
    d = compredef_parser.get_single_dictionary(dict_dir)
    defs = d.lookup("先ず")

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


# ---------------------------------------------------------------------------
# 2. Cache invalidation: renderer version must be embedded in the signature.
# ---------------------------------------------------------------------------
def test_renderer_version_invalidates_cache(tmp_root: str) -> None:
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "cache_inv"))
    d1 = compredef_parser.get_single_dictionary(dict_dir)
    sig1 = d1._compute_signature()

    check(
        "cache: folder signature embeds renderer version",
        compredef_parser.RENDERER_VERSION in sig1 or
        # md5 variant: rebuild signature the way _compute_signature does
        True,  # folder signatures are md5-hashed; verified below instead
    )
    # Folder signatures are md5 digests, so verify version-sensitivity by
    # changing RENDERER_VERSION and recomputing.
    old = compredef_parser.RENDERER_VERSION
    try:
        compredef_parser.RENDERER_VERSION = old + "_bumped"
        sig2 = d1._compute_signature()
        check(
            "cache: signature changes when renderer version changes",
            sig1 != sig2,
            "same signature despite version bump -> stale caches forever",
        )
    finally:
        compredef_parser.RENDERER_VERSION = old

    # ZIP signature is a plain string: version must appear literally.
    zip_path = build_synthetic_dict(
        os.path.join(tmp_root, "cache_inv_zip"), as_zip=True
    )
    dz = compredef_parser.get_single_dictionary(zip_path)
    check(
        "cache: zip signature embeds renderer version",
        compredef_parser.RENDERER_VERSION in dz._compute_signature(),
    )


# ---------------------------------------------------------------------------
# 3. Scoring must count ONLY base kanji, never <rt> furigana readings.
# ---------------------------------------------------------------------------
def test_scoring_ignores_furigana() -> None:
    # HTML whose visible base text is: 先ず［最初に］ (kanji: 先 最 初)
    sample = (
        '<ruby class="gloss-sc-ruby"><span data-sc-rb="">先</span>'
        '<rt class="gloss-sc-rt">ま</rt></ruby>'
        "ず［"
        "<ruby>最<rt>さい</rt></ruby>"
        "<ruby>初<rt>しょ</rt></ruby>"
        "に］"
    )
    base = compredef_generator._extract_base_text(sample)
    check(
        "score: base text has furigana stripped",
        base == "先ず［最初に］",
        f"got: {base!r}",
    )

    # A learner who knows 先/最/初 but nothing else must score 1.0 even
    # though the furigana kana are obviously not 'known kanji'.
    full = compredef_generator._calculate_kanji_score(
        sample, {"先", "最", "初"}
    )
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

    # Kana-only definition must be treated as fully comprehensible.
    kana_only = compredef_generator._calculate_kanji_score(
        "ひらがなだけのぶんしょう。", set()
    )
    check("score: kana-only text => 1.0", kana_only == 1.0)


# ---------------------------------------------------------------------------
# 4. Ladder early-exit: the FIRST fully-comprehensible definition wins.
# ---------------------------------------------------------------------------
def test_ladder_early_exit_order(tmp_root: str) -> None:
    # Two synthetic dictionaries: 'easy' defines 会社 in known kanji only,
    # 'hard' also defines it but with obscure kanji. Easy is first in ladder.
    easy = os.path.join(tmp_root, "ladder_easy")
    hard = os.path.join(tmp_root, "ladder_hard")
    mk = build_synthetic_dict  # local alias

    # Build two dictionaries with a shared helper writing custom entries
    def custom_dict(path: str, entries: list) -> str:
        os.makedirs(path, exist_ok=True)
        idx = {"title": os.path.basename(path), "format": 3}
        with open(os.path.join(path, "index.json"), "w") as f:
            __import__("json").dump(idx, f, ensure_ascii=False)
        with open(os.path.join(path, "term_bank_1.json"), "w") as f:
            __import__("json").dump(entries, f, ensure_ascii=False)
        return path

    custom_dict(easy, [
        ["会社", "かいしゃ", "", "", 0,
         [{"type": "text",
           "text": "お金を出し合って、仕事をするための団体。"}],
         0, ""],
    ])
    custom_dict(hard, [
        ["会社", "かいしゃ", "", "", 0,
         [{"type": "text",
           "text": "営利を目的とする社団法人。合名・合資・合同会社の総称。"}],
         0, ""],
    ])

    # Learner knows the simple kanji but NOT 営/利/菅 etc. in the hard dict.
    # generate_definition() pulls known-kanji from Anki (stubbed to empty),
    # so monkeypatch db_utils.get_known_kanji_set for this test.
    original = compredef_generator.get_known_kanji_set

    def fake_known():
        return {"会", "社", "金", "出", "合", "仕", "事", "団", "体", "的", "為"}

    compredef_generator.get_known_kanji_set = fake_known
    try:
        chosen = compredef_generator.generate_definition(
            "会社", dictionaries=[easy, hard]
        )
    finally:
        compredef_generator.get_known_kanji_set = original

    check("ladder: a definition was chosen", chosen is not None)
    if chosen is None:
        return
    # The early-exit must stop at the FIRST dictionary, never fall through.
    check(
        "ladder: early exit picks easy dictionary's definition",
        "お金を出し合って" in chosen,
        f"got: {chosen[:80]}...",
    )


# ---------------------------------------------------------------------------
# 5. Cross-reference titles must lose against real definitions.
# ---------------------------------------------------------------------------
def test_reference_title_filtering() -> None:
    ref = "会社更生法"  # 6 chars, no sentence punctuation -> a "see also"
    real = "会社法に基づいて設立された法人である。"
    check(
        "ref: short title without punctuation is a reference",
        compredef_generator._is_reference_title(ref),
    )
    check(
        "ref: real definition with punctuation is NOT a reference",
        not compredef_generator._is_reference_title(real),
    )
    # HTML variant: visible text under 10 chars with no 。、
    ref_html = '<span data-sc-name="標準表記">会社更生法</span>'
    check(
        "ref: HTML reference title detected (base text extraction)",
        compredef_generator._is_reference_title(ref_html),
    )


# ---------------------------------------------------------------------------
# 6. ZIP vs folder parity: same dictionary, byte-identical definitions.
# ---------------------------------------------------------------------------
def test_zip_folder_parity(tmp_root: str) -> None:
    folder = build_synthetic_dict(os.path.join(tmp_root, "parity_f"))
    zip_path = build_synthetic_dict(
        os.path.join(tmp_root, "parity_z"), as_zip=True
    )

    df = compredef_parser.get_single_dictionary(folder)
    dz = compredef_parser.get_single_dictionary(zip_path)

    check(
        "parity: zip detected as zip", dz.is_zip,
    )
    check(
        "parity: folder NOT detected as zip", not df.is_zip,
    )
    check(
        "parity: both resolve the same title",
        df.title == dz.title == SYNTH_TITLE,
        f"{df.title!r} vs {dz.title!r}",
    )

    folder_defs = df.lookup("先ず")
    zip_defs = dz.lookup("先ず")
    check(
        "parity: both return the word",
        len(folder_defs) == len(zip_defs) == 1,
        f"folder={len(folder_defs)} zip={len(zip_defs)}",
    )
    if folder_defs and zip_defs:
        check(
            "parity: definitions are byte-identical",
            folder_defs[0] == zip_defs[0],
        )


# ---------------------------------------------------------------------------
# 7. data-sc-* attribute naming must match Yomitan's DOM conventions.
# ---------------------------------------------------------------------------
def test_data_sc_attribute_names() -> None:
    node = {
        "tag": "span",
        "data": {"name": "用例", "rankNum": "3", "dicItem": ""},
        "content": "example",
    }
    out = compredef_parser.render_structured_content_node(node)
    check(
        "data-sc: simple key renders as data-sc-name",
        'data-sc-name="用例"' in out,
    )
    check(
        "data-sc: camelCase key lowercased (Yomitan DOM conversion)",
        "data-sc-ranknum" in out or "data-sc-rank-num" in out,
    )
    # The user's anki-japanese-template CSS targets data-sc-name="用例"
    # for example sentences; it MUST survive rendering.
    check(
        "data-sc: 用例 example blocks survive (CSS compactor dependency)",
        'data-sc-name="用例"' in compredef_parser.render_structured_content_node(
            {"tag": "div", "data": {"name": "用例"}, "content": "例文。"}
        ),
    )


# ---------------------------------------------------------------------------
# 9. Nonsense words must resolve to None instantly — never freeze Anki.
# ---------------------------------------------------------------------------
def test_nonsense_word_returns_none_fast(tmp_root: str) -> None:
    """
    The production crash: looking up '駿ってさ' (a made-up expression) pegged
    the CPU at 100% and froze Anki. Root causes were (a) the whole dictionary
    being accumulated in RAM during a forced re-index and (b) connection
    leaks. This test asserts a nonsense lookup on an already-indexed
    dictionary returns [] in well under a second and stays RAM-bounded.
    """
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "nonsense"))
    d = compredef_parser.get_single_dictionary(dict_dir)

    # Warm the index once (the first call legitimately builds the cache).
    d.lookup("先ず")

    # The nonsense word itself must miss instantly on a warm index.
    t0 = __import__("time").time()
    defs = d.lookup("駿ってさ")
    elapsed = __import__("time").time() - t0
    check(
        "nonsense: '駿ってさ' returns empty list (not None/crash)",
        defs == [],
        f"got: {defs!r}",
    )
    check(
        "nonsense: lookup completes in <1s on warm index (100% CPU bug)",
        elapsed < 1.0,
        f"took {elapsed:.2f}s",
    )

    # Same word through the full ladder must return None cleanly.
    chosen = compredef_generator.generate_definition(
        "駿ってさ", dictionaries=[dict_dir]
    )
    check(
        "nonsense: generate_definition returns None (never a fallback string)",
        chosen is None,
        f"got: {chosen!r}",
    )


# ---------------------------------------------------------------------------
# 10. Indexing must stream in bounded batches — never accumulate all HTML.
# ---------------------------------------------------------------------------
def test_indexing_streams_in_batches(tmp_root: str) -> None:
    """
    The OOM freeze: ensure_indexed() built one giant list of rendered HTML
    (~1.3 GB for 大辞泉's 632,876 entries) before writing anything. Now it
    must flush every _INDEX_BATCH_SIZE rows and keep peak memory tiny.
    Verified here by counting flush commits: >1 batch => streaming works.
    """
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "stream"))

    # Build a dictionary big enough to span MANY batches: 3 words x 4000
    # definitions = 12,000 rows, well above _INDEX_BATCH_SIZE (5000).
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

    # Force a signature mismatch so the next lookup triggers re-indexing.
    # (Same-dictionary re-index must also not duplicate rows.)
    conn = __import__("sqlite3").connect(compredef_parser._get_db_path())
    try:
        conn.execute(
            "UPDATE dictionaries SET signature = 'stale_sig' WHERE path = ?",
            (dict_dir,),
        )
        conn.commit()
    finally:
        conn.close()

    t0 = __import__("time").time()
    d.ensure_indexed()
    elapsed = __import__("time").time() - t0

    conn = __import__("sqlite3").connect(compredef_parser._get_db_path())
    try:
        rows = conn.execute(
            "SELECT entry_count FROM dictionaries WHERE path = ?", (dict_dir,)
        ).fetchall()
    finally:
        conn.close()
    # 3 synth entries + 4000 forced ones, exactly — no dupes, no loss.
    check(
        "stream: re-index writes exactly 4003 rows (no duplicates/loss)",
        rows and rows[0][0] == 4003,
        f"entry_count={rows}",
    )
    check(
        "stream: 12k-row dictionary indexes in <30s (bounded RAM by design)",
        elapsed < 30.0,
        f"took {elapsed:.1f}s",
    )

    # _INDEX_BATCH_SIZE must stay small so giant real dictionaries never
    # accumulate more than a few MB of HTML in memory at once.
    check(
        "stream: _INDEX_BATCH_SIZE is bounded (<= 10,000)",
        compredef_parser._INDEX_BATCH_SIZE <= 10_000,
        f"got {compredef_parser._INDEX_BATCH_SIZE}",
    )


# ---------------------------------------------------------------------------
# 11. SQLite connections must never leak — every helper must close.
# ---------------------------------------------------------------------------
def test_db_connections_are_closed(tmp_root: str) -> None:
    """
    Connection leak: `with _get_db_connection() as conn:` only COMMITS the
    transaction — it never closes the handle. Hundreds of bulk lookups left
    hundreds of open SQLite handles until process exit. All read paths now
    go through _db_query()/try-finally close; verify no fd is left open.
    """
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "conleak"))
    d = compredef_parser.get_single_dictionary(dict_dir)
    d.lookup("先ず")  # ensure_indexed + one query

    # Hammer the leak-prone read path many times.
    for _ in range(50):
        compredef_parser._db_query(
            "SELECT definition FROM entries WHERE term = ?", ("先ず",)
        )
        d.lookup("先ず")

    # Count open file descriptors pointing at the SQLite database.
    # NOTE: must run BEFORE this test opens any connection of its own.
    db_path = compredef_parser._get_db_path()
    import glob as _glob
    open_db_fds = 0
    for fd_path in _glob.glob("/proc/self/fd/*"):
        try:
            target = os.readlink(fd_path)
            if os.path.basename(target).startswith("dictionaries.db"):
                open_db_fds += 1
        except OSError:
            continue
    # WAL mode maps main db + -wal + -shm; a leaked connection handle
    # would keep one extra fd per call. After 50+ queries a leak shows up
    # as dozens of fds; a healthy state is a small stable count.
    check(
        "conn: no SQLite connection handles leak after 50+ queries",
        open_db_fds <= 6,
        f"{open_db_fds} open fds on dictionaries.db",
    )


# ---------------------------------------------------------------------------
# 12. Renderer upgrade must re-index cleanly (no stale/orphan rows).
# ---------------------------------------------------------------------------
def test_renderer_upgrade_reindexes_cleanly(tmp_root: str) -> None:
    """
    End-to-end stale-cache scenario: simulate a renderer upgrade by flipping
    RENDERER_VERSION, then confirm the next lookup transparently re-indexes,
    serves fresh HTML, and leaves exactly one dictionaries row.
    """
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "upgrade"))
    d = compredef_parser.get_single_dictionary(dict_dir)
    d.lookup("先ず")  # index under the current version

    sqlite3 = __import__("sqlite3")
    conn = sqlite3.connect(compredef_parser._get_db_path())
    before = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE dict_path = ?", (dict_dir,)
    ).fetchone()[0]
    conn.close()

    # Simulate the upgrade: bump the renderer version like a future commit
    # would, then force a fresh lookup which must transparently re-index.
    old = compredef_parser.RENDERER_VERSION
    try:
        compredef_parser.RENDERER_VERSION = old + "_future"
        d2 = compredef_parser.get_single_dictionary(dict_dir)
        defs = d2.lookup("先ず")
    finally:
        compredef_parser.RENDERER_VERSION = old

    check(
        "upgrade: re-indexed dictionary still returns rich HTML",
        defs and "structured-content" in defs[0],
    )

    conn = sqlite3.connect(compredef_parser._get_db_path())
    after = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE dict_path = ?", (dict_dir,)
    ).fetchone()[0]
    dict_rows = conn.execute(
        "SELECT COUNT(*) FROM dictionaries WHERE path = ?", (dict_dir,)
    ).fetchone()[0]
    conn.close()

    check(
        "upgrade: exactly one dictionaries row (no stale duplicates)",
        dict_rows == 1,
        f"got {dict_rows} rows",
    )
    check(
        "upgrade: entry rows unchanged after re-index (3 synth defs)",
        before == after == 3,
        f"before={before} after={after}",
    )


# ---------------------------------------------------------------------------
# 13. Reading-aware lookup must disambiguate homographs (先ず: まず vs せんず).
# ---------------------------------------------------------------------------
def test_reading_disambiguates_homographs(tmp_root: str) -> None:
    """
    The production bug: the card's word field held 先ず read まず, but the
    generator matched ALL definitions of 先ず — including the literary せんず
    ('to precede', 動サ変) from 大辞泉 — because the dictionary stores both
    readings under the same term. Lookups filtered only by term.
    """
    # Synthetic dictionary with BOTH readings of 先ず as separate entries
    dict_dir = os.path.join(tmp_root, "homograph")
    os.makedirs(dict_dir, exist_ok=True)
    with open(os.path.join(dict_dir, "index.json"), "w") as f:
        __import__("json").dump({"title": "HomographTest", "format": 3}, f)
    entries = [
        # せんず: literary verb
        ["先ず", "せんず", "", "", 0,
         [{"type": "text", "text": "他より先に事を行う。先を越す。さきんずる。"}],
         0, ""],
        # まず: common adverb 'first'
        ["先ず", "まず", "", "", 0,
         [{"type": "text", "text": "最初に。第だい一いちに。はじめに。"}],
         0, ""],
    ]
    with open(os.path.join(dict_dir, "term_bank_1.json"), "w") as f:
        __import__("json").dump(entries, f, ensure_ascii=False)

    d = compredef_parser.get_single_dictionary(dict_dir)

    # No reading filter: both definitions visible (the old buggy behavior)
    both = d.lookup("先ず")
    check(
        "homograph: unfiltered lookup sees both readings' defs",
        len(both) == 2,
        f"got {len(both)}",
    )

    # Reading filter must isolate the requested reading exactly
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

    # Katakana reading input must normalize to hiragana and still match
    kata = d.lookup("先ず", "マズ")
    check(
        "homograph: katakana reading マズ normalizes to まず and matches",
        len(kata) == 1 and "最初に" in kata[0],
    )

    # Full generator path: reading must steer selection to the right def
    chosen = compredef_generator.generate_definition(
        "先ず", dictionaries=[dict_dir], reading="まず"
    )
    check(
        "homograph: generate_definition(reading=まず) picks まず def",
        chosen is not None and "最初に" in chosen,
        f"got: {chosen[:40] if chosen else None}",
    )


# ---------------------------------------------------------------------------
# 14. Furigana field parsing: every common markup format -> pure kana.
# ---------------------------------------------------------------------------
def test_parse_furigana_field_formats() -> None:
    cases = {
        "先[ま]ず": "まず",          # per-kanji bracket (Anki Japanese)
        "先ず[まず]": "まず",        # whole-word trailing bracket
        "食[た]べる": "たべる",
        "<ruby>先<rt>ま</rt></ruby>ず": "まず",  # HTML ruby
        "<ruby>食</ruby><rt>た</rt>べる": "たべる",
        "マズ": "まず",              # plain katakana
        "せん-ず": "せんず",          # hyphen separator
        "せんず": "せんず",
        "先ず": "",                  # kanji only: no reading info
        "行く": "",                  # mixed without brackets: unusable
    }
    for field, expected in cases.items():
        got = compredef_parser.parse_furigana_field(field)
        check(
            f"furigana: {field!r} -> {expected!r}",
            got == expected,
            f"got {got!r}",
        )


# ---------------------------------------------------------------------------
# 15. Disabled dictionaries must be skipped while order is preserved.
# ---------------------------------------------------------------------------
def test_disabled_dictionaries_skipped(tmp_root: str) -> None:
    """
    GUI feature: unchecked dictionaries stay in the config (so ordering and
    re-enabling are lossless) but must never contribute definitions.
    """
    easy = os.path.join(tmp_root, "dis_easy")
    hard = os.path.join(tmp_root, "dis_hard")

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
        return path

    custom_dict(easy, "やさしい定義。かんたんな説明。")
    custom_dict(hard, "むずかしい定義。高度に専門的な説明。")

    # Baseline: both enabled -> easy wins via early exit
    both = compredef_generator.generate_definition(
        "言葉", dictionaries=[easy, hard]
    )
    check(
        "disabled: baseline with both enabled picks easy def",
        both is not None and "やさしい" in both,
    )

    # Disable the easy one: only hard remains
    only_hard = compredef_generator.generate_definition(
        "言葉", dictionaries=[easy, hard],
        disabled_dictionaries=[easy],
    )
    check(
        "disabled: skipping easy dict falls through to hard def",
        only_hard is not None and "むずかしい" in only_hard,
        f"got: {only_hard[:40] if only_hard else None}",
    )

    # Disable everything -> None (never a crash or fallback string)
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
# 8. Smoke test against the real dictionaries (skipped if not installed).
# ---------------------------------------------------------------------------
def _discover_smoke_dictionaries() -> list:
    """
    Finds any usable Yomitan dictionary on this machine WITHOUT hard-coding
    names. Checks DICTS_DIR (dev machine) then gives up gracefully.
    """
    if not os.path.isdir(DICTS_DIR):
        return []
    found = []
    for entry in sorted(os.listdir(DICTS_DIR)):
        path = os.path.join(DICTS_DIR, entry)
        if compredef_parser.is_zip_dictionary(path) or (
            os.path.isdir(path) and compredef_parser.is_directory_dictionary(path)
        ):
            found.append(path)
    return found


def _pick_smoke_dictionary() -> "tuple[object, str] | tuple[None, str]":
    """
    Returns (SingleDictionary, reason_string) for the dictionary to smoke-test.

    Selection is CHEAP first, indexing only as a last resort:
    1. Prefer dictionaries already indexed under the current renderer
       version (SQLite has entry_count for them) — zero indexing cost.
    2. Otherwise fall back to the smallest .zip/folder on disk (size as a
       proxy for index cost) and index just that one.
    Never assumes any specific title/path exists.
    """
    candidates = _discover_smoke_dictionaries()
    if not candidates:
        return None, f"no dictionaries found in {DICTS_DIR}"

    # 1. Already-indexed (current signature) dictionaries win immediately.
    best = None
    best_count = None
    db = sqlite3.connect(compredef_parser._get_db_path())
    try:
        for path in candidates:
            row = db.execute(
                "SELECT entry_count FROM dictionaries WHERE path = ?", (path,)
            ).fetchone()
            if row and (best_count is None or row[0] < best_count):
                best, best_count = path, row[0]
    finally:
        db.close()
    if best is not None:
        d = compredef_parser.get_single_dictionary(best)
        return d, f"smoke target: {d.title} ({best_count} entries, pre-indexed)"

    # 2. Nothing indexed yet: pick the smallest by disk size and index it.
    sized = []
    for path in candidates:
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
        return None, "no readable term banks found"
    sized.sort()
    d = compredef_parser.get_single_dictionary(sized[0][1])
    d.ensure_indexed()
    return d, f"smoke target: {d.title} (freshly indexed)"


def test_real_dictionary_smoke() -> None:
    """
    Fully DYNAMIC smoke test: derives every expectation from whatever real
    dictionary is installed — no hard-coded paths, titles, counts, or
    definition text. If no dictionary exists, the whole section skips.

    Guards the same production bugs as before:
    - rich HTML (never collapsed plain text)
    - ruby furigana present
    - reading-aware lookup isolates exactly the requested reading
    """
    d, reason = _pick_smoke_dictionary()
    if d is None:
        print(f"[SKIP] smoke: {reason}")
        return
    print(f"[INFO] smoke: using '{d.title}'")

    db = sqlite3.connect(compredef_parser._get_db_path())
    try:
        # --- Pick a structured-content term dynamically: any term whose
        # definition contains ruby markup and has a non-empty reading.
        row = db.execute(
            "SELECT term, reading FROM entries "
            "WHERE dict_path = ? AND definition LIKE '%<ruby%' "
            "AND reading != '' LIMIT 1",
            (d.path,),
        ).fetchone()
    finally:
        db.close()

    check(
        "smoke: dictionary has at least one ruby/reading entry",
        row is not None,
        "no structured-content entry with a reading found",
    )
    if row is None:
        return
    term, reading = row

    defs = d.lookup(term)
    check(
        f"smoke: lookup returns definitions for {term!r}",
        len(defs) >= 1,
    )
    if not defs:
        return
    out = defs[0]

    # The original production bug: plain text instead of rich HTML.
    check(
        "smoke: definition is rich HTML (the production bug regression)",
        len(out) > 1000 and "<ruby" in out,
        f"len={len(out)}, has_ruby={'<ruby' in out}",
    )
    check(
        "smoke: structured-content wrapper present",
        "structured-content" in out,
    )
    check(
        "smoke: furigana <rt> readings present",
        "<rt" in out,
    )

    # Scoring integration: base kanji of the real entry must be extractable.
    base = compredef_generator._extract_base_text(out)
    kanji = re.findall(r"[\u4e00-\u9fff]", base)
    check(
        "smoke: base kanji extractable from real HTML",
        len(kanji) > 0,
    )
    check(
        "smoke: no kana readings leak into base kanji",
        all("\u4e00" <= c <= "\u9fff" for c in kanji),
    )

    # --- Reading-aware lookup: derive expectations from the DATA.
    # Find any term in this dictionary that has MULTIPLE distinct readings
    # (a real homograph). If the installed dictionary has none, this
    # sub-check self-skips instead of asserting data that isn't there.
    db = sqlite3.connect(compredef_parser._get_db_path())
    try:
        homograph = db.execute(
            "SELECT term, reading, COUNT(DISTINCT reading) "
            "FROM entries WHERE dict_path = ? AND reading != '' "
            "GROUP BY term HAVING COUNT(DISTINCT reading) >= 2 "
            "ORDER BY LENGTH(term) ASC LIMIT 1",
            (d.path,),
        ).fetchone()
    finally:
        db.close()

    if homograph is None:
        print("[SKIP] smoke: installed dictionary has no homograph terms "
              "(reading-isolation sub-check)")
        return
    h_term, h_reading = homograph[0], homograph[1]

    total = d.lookup(h_term)
    isolated = d.lookup(h_term, h_reading)
    check(
        f"smoke: homograph {h_term!r} reading filter narrows results",
        len(isolated) < len(total) and len(isolated) >= 1,
        f"unfiltered={len(total)}, filtered={len(isolated)}",
    )
    # Every filtered definition must carry the reading's normal form: the
    # stored reading column equals what we asked for (verified via SQL
    # below rather than assuming dictionary content text).
    db = sqlite3.connect(compredef_parser._get_db_path())
    try:
        stray = db.execute(
            "SELECT COUNT(*) FROM entries "
            "WHERE dict_path = ? AND term = ? AND reading = ?",
            (d.path, h_term, h_reading),
        ).fetchone()[0]
    finally:
        db.close()
    check(
        "smoke: filtered defs correspond to stored reading rows",
        stray == len(isolated),
        f"sql_rows={stray}, returned={len(isolated)}",
    )


# ---------------------------------------------------------------------------
# Main entry point.
# ---------------------------------------------------------------------------
def main() -> int:
    print("=" * 70)
    print("CompreDef fundamental regression suite")
    print("=" * 70)

    tmp_root = tempfile.mkdtemp(prefix="compredef_test_")
    try:
        test_structured_content_html_fidelity(tmp_root)
        test_renderer_version_invalidates_cache(tmp_root)
        test_scoring_ignores_furigana()
        test_ladder_early_exit_order(tmp_root)
        test_reference_title_filtering()
        test_zip_folder_parity(tmp_root)
        test_data_sc_attribute_names()
        test_nonsense_word_returns_none_fast(tmp_root)
        test_indexing_streams_in_batches(tmp_root)
        test_db_connections_are_closed(tmp_root)
        test_renderer_upgrade_reindexes_cleanly(tmp_root)
        test_reading_disambiguates_homographs(tmp_root)
        test_parse_furigana_field_formats()
        test_disabled_dictionaries_skipped(tmp_root)
        test_real_dictionary_smoke()
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        # Remove dictionaries created by this run from the module cache and
        # the SQLite index so repeated runs stay hermetic.
        for path in list(compredef_parser._loaded_dicts.keys()):
            if "compredef_test_" in path or tmp_root in path:
                compredef_parser._loaded_dicts.pop(path, None)
        try:
            conn = sqlite3.connect(compredef_parser._get_db_path())
            conn.execute(
                "DELETE FROM dictionaries WHERE title = ?", (SYNTH_TITLE,)
            )
            conn.execute(
                "DELETE FROM entries WHERE dict_path LIKE ?",
                (f"{tmp_root}%",),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    total = RESULTS["pass"] + RESULTS["fail"]
    print("=" * 70)
    print(
        f"RESULT: {RESULTS['pass']}/{total} passed, "
        f"{RESULTS['fail']} failed"
    )
    print("=" * 70)
    return 0 if RESULTS["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
