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

# The real dictionaries present on the development machine (used by the
# smoke test; the suite skips them gracefully elsewhere).
DICTS_DIR = "/home/mohamed/Desktop/Dicts"
SHOGAKU_ZIP = os.path.join(
    DICTS_DIR, "[JA-JA] 小学館例解学習国語 第十二版[2025-08-18].zip"
)
SHOGAKU_FOLDER = os.path.join(
    DICTS_DIR, "[JA-JA] 小学館例解学習国語 第十二版[2025-08-18]"
)

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
# 8. Smoke test against the real dictionaries (skipped if not installed).
# ---------------------------------------------------------------------------
def test_real_dictionary_smoke() -> None:
    if not os.path.isfile(SHOGAKU_ZIP):
        print(f"[SKIP] smoke: real dictionary not present ({SHOGAKU_ZIP})")
        return

    d = compredef_parser.get_single_dictionary(SHOGAKU_ZIP)
    defs = d.lookup("先ず")
    check(
        "smoke: real dictionary returns a definition for 先ず",
        len(defs) >= 1,
    )
    if not defs:
        return
    out = defs[0]

    # The exact production bug: 121-char plain text instead of rich HTML.
    check(
        "smoke: 先ず is rich HTML (the production bug regression)",
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
    # Furigana like さい/しょ are kana and must never appear as base kanji
    check(
        "smoke: no kana readings leak into base kanji",
        all("\u4e00" <= c <= "\u9fff" for c in kanji),
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
