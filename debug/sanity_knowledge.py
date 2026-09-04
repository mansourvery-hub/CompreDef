#!/usr/bin/env python3
"""
debug/sanity_knowledge.py — On-demand sanity checks for CompreDef's
learner-knowledge snapshot (known kanji / known vocabulary).

NOT part of the regression suite (tests/test_regression.py) and NOT run
by CI, build.sh, or release.sh. Run it manually when something looks
wrong — e.g. 0 known kanji, suspicious definition choices, or after any
change to anki.py:

    python3 debug/sanity_knowledge.py

Exit code 0 = all sane, 1 = something needs attention (names printed).
Anki/Qt are stubbed, so this runs on plain system Python.

For diagnosis against a LIVE collection, see console_snippets.md
(copy-paste recipes for Anki's Help → Debug Console).
"""

import os
import re
import sys
import tempfile

DEBUG_ROOT = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(DEBUG_ROOT)
sys.path.insert(0, REPO_ROOT)

STUB_DIR = os.path.join(tempfile.gettempdir(), "compredef_debug_aqt_stub")


class FakeModels:
    """Model registry keyed by name, with mid-based lookup like Anki."""

    def __init__(self):
        self.by_name_dict = {}
        self._next_id = 101

    def add(self, name, field_names):
        model = {"id": self._next_id, "name": name,
                 "flds": [{"name": n} for n in field_names]}
        self._next_id += 1
        self.by_name_dict[name] = model
        return model["id"]

    def by_name(self, name):
        return self.by_name_dict.get(name)

    def get(self, mid):
        for m in self.by_name_dict.values():
            if m.get("id") == mid:
                return m
        return None


class FakeDB:
    """Stand-in for mw.col.db with optional legacy-schema simulation."""

    def __init__(self):
        self.rows = []
        self.calls = 0
        self.fail_with = None
        self.reject_legacy_models_table = False

    def all(self, query, params=()):
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        if self.reject_legacy_models_table and re.search(
                r"\b(join|from)\s+models\b", query, re.IGNORECASE):
            import sqlite3
            raise sqlite3.OperationalError("no such table: models")
        return list(self.rows)


class FakeAddonManager:
    def __init__(self):
        self.configs = {}

    def getConfig(self, name):
        return self.configs.get(name, {})


class FakeTaskman:
    """Runs background tasks synchronously for deterministic checks."""

    def run_in_background(self, fn, on_done=None):
        fn()
        if on_done is not None:
            class _F:
                def result(self):
                    return None
            on_done(_F())


class FakeMW:
    def __init__(self):
        import types
        self.col = types.SimpleNamespace(models=FakeModels(), db=FakeDB())
        self.addonManager = FakeAddonManager()
        self.taskman = FakeTaskman()
        self._tooltips = []


def _install_stub() -> FakeMW:
    """Builds a minimal aqt package (mw + utils.tooltip recorder)."""
    aqt_dir = os.path.join(STUB_DIR, "aqt")
    os.makedirs(aqt_dir, exist_ok=True)
    with open(os.path.join(aqt_dir, "__init__.py"), "w") as f:
        f.write("mw = None\n")
    with open(os.path.join(aqt_dir, "utils.py"), "w") as f:
        f.write("def tooltip(msg, parent=None):\n"
                "    import aqt\n"
                "    aqt.mw._tooltips.append(str(msg))\n"
                "    print(f'[tooltip] {msg}')\n")
    if STUB_DIR not in sys.path:
        sys.path.insert(0, STUB_DIR)
    import aqt
    aqt.mw = FakeMW()
    return aqt.mw


MW = _install_stub()

import anki as anki_mod  # noqa: E402  (real module under check)

RESULTS = {"pass": 0, "fail": 0, "failed": []}


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        RESULTS["pass"] += 1
        print(f"[PASS] {name}")
    else:
        RESULTS["fail"] += 1
        RESULTS["failed"].append(name)
        print(f"[FAIL] {name}" + (f" -- {detail}" if detail else ""))


def fresh_state() -> None:
    """Resets the session snapshot and the fake Anki world.

    Knowledge no longer reads add-on config, so no config setup is
    needed — the first field of every mature note counts, any type.
    """
    anki_mod._known_kanji_cache = set()
    anki_mod._known_vocab_cache = set()
    anki_mod._caches_ready.clear()
    anki_mod._db_warned = False
    anki_mod._last_rows_scanned = 0
    anki_mod._last_words_kept = 0
    anki_mod._last_error = None
    MW.col.models = FakeModels()
    MW.col.db = FakeDB()
    MW.addonManager.configs.clear()
    MW._tooltips.clear()


SEP = "\x1f"


def s1_no_legacy_models_table_sql() -> None:
    """No shipped module may embed SQL naming the legacy 'models' table.

    AST-aware: only non-docstring string literals are scanned (that is
    where real SQL lives). This excludes 'from models import ...' (the
    Python module, not the table) and prose documenting the incident.
    """
    import ast
    hits = []
    for fname in sorted(os.listdir(REPO_ROOT)):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(REPO_ROOT, fname)
        tree = ast.parse(open(path, encoding="utf-8").read())
        docstrings = set()

        def mark_docstrings(node) -> None:
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr):
                val = body[0].value
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    docstrings.add(id(val))

        mark_docstrings(tree)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                mark_docstrings(node)
        for node in ast.walk(tree):
            text, lineno = None, getattr(node, "lineno", "?")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstrings:
                    continue
                text = node.value
            elif isinstance(node, ast.JoinedStr):
                text = "".join(
                    v.value for v in node.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str))
            if text and re.search(r"\b(join|from)\s+models\b", text,
                                  re.IGNORECASE):
                hits.append(f"{fname}:{lineno}")
    check("S1: no legacy 'models'-table SQL in shipped modules", not hits,
          "; ".join(hits))


def s2_expression_only_single_model() -> None:
    """Kanji/vocab come only from first fields (one layout)."""
    fresh_state()
    MW.col.db.rows = [
        (SEP.join(["漢字", "plain", "plain"]),),
        (SEP.join(["plain", "龍の定義", "plain"]),),
        (SEP.join(["plain", "plain", "虎の例文"]),),
    ]
    known = anki_mod.get_known_kanji_set()
    vocab = anki_mod.get_known_vocabulary_set()
    check("S2: first-field kanji known", known == {"漢", "字"},
          f"got {sorted(known)}")
    check("S2: Definition/Example kanji excluded",
          "龍" not in known and "虎" not in known)
    check("S2: vocab is first-field text only",
          vocab == {"漢字", "plain"}, f"got {sorted(vocab)}")


def s3_other_models_ignored() -> None:
    """All note types count: mixed 3-/2-/1-field layouts, first field of
    each contributes, non-first kanji never do."""
    fresh_state()
    MW.col.db.rows = [
        (SEP.join(["漢字", "plain", "plain"]),),
        (SEP.join(["語彙", "解釈"]),),
        ("日本語",),
        (SEP.join(["plain", "龍", "虎"]),),
    ]
    known = anki_mod.get_known_kanji_set()
    vocab = anki_mod.get_known_vocabulary_set()
    check("S3: every type's first field counts",
          known == {"漢", "字", "語", "彙", "日", "本"},
          f"got {sorted(known)}")
    check("S3: non-first kanji excluded across layouts",
          "龍" not in known and "虎" not in known)
    check("S3: vocab spans layouts",
          vocab == {"漢字", "plain", "語彙", "日本語"},
          f"got {sorted(vocab)}")


def s4_misconfiguration_degrades_quietly() -> None:
    """Empty first fields and malformed rows are skipped without crashing.
    Note: knowledge no longer depends on add-on config at all, so even an
    empty/missing config must yield a working snapshot."""
    fresh_state()
    MW.col.db.rows = [
        (SEP.join(["", "漢字"]),),   # empty first field: skipped
        ("   ",),                     # whitespace-only: skipped
        (SEP.join(["漢字", "x"]),),
        "not-a-tuple-but-a-string",   # malformed row shape
        (None,),                      # null blob
    ]
    try:
        known = anki_mod.get_known_kanji_set()
        ok = known == {"漢", "字"}
    except Exception as e:  # noqa: BLE001
        ok, known = False, f"raised {e!r}"
    check("S4: bad rows skipped, good rows kept", ok,
          f"got {sorted(known) if isinstance(known, set) else known}")


def s5_db_failure_is_visible_once() -> None:
    """A DB failure surfaces via print+tooltip exactly once per session."""
    fresh_state()
    MW.col.db.fail_with = Exception("boom")
    known = anki_mod.get_known_kanji_set()
    check("S5: failure yields empty set", known == set())
    check("S5: exactly one visible warning", len(MW._tooltips) == 1,
          f"tooltips={MW._tooltips}")
    anki_mod.reset_caches()  # rebuild attempt must not spam again
    anki_mod.get_known_kanji_set()
    check("S5: warning not repeated on rebuild", len(MW._tooltips) == 1,
          f"tooltips={MW._tooltips}")


def s6_snapshot_built_once() -> None:
    """Repeated reads reuse the snapshot; reset triggers one rebuild."""
    fresh_state()
    MW.col.db.rows = [("漢字",)]
    for _ in range(5):
        anki_mod.get_known_kanji_set()
        anki_mod.get_known_vocabulary_set()
    check("S6: 10 reads cause exactly 1 collection scan",
          MW.col.db.calls == 1, f"calls={MW.col.db.calls}")
    anki_mod.reset_caches()
    anki_mod.get_known_kanji_set()
    check("S6: manual reset triggers exactly 1 rebuild",
          MW.col.db.calls == 2, f"calls={MW.col.db.calls}")


def main() -> int:
    print("=" * 70)
    print("CompreDef learner-knowledge sanity checks (on-demand, not CI)")
    print("=" * 70)
    s1_no_legacy_models_table_sql()
    s2_expression_only_single_model()
    s3_other_models_ignored()
    s4_misconfiguration_degrades_quietly()
    s5_db_failure_is_visible_once()
    s6_snapshot_built_once()
    print("=" * 70)
    total = RESULTS["pass"] + RESULTS["fail"]
    print(f"RESULT: {RESULTS['pass']}/{total} sane, "
          f"{RESULTS['fail']} need attention")
    if RESULTS["failed"]:
        print("\nNEEDS ATTENTION:")
        for name in RESULTS["failed"]:
            print(f"  - {name}")
        print("\nSee debug/README.md and debug/console_snippets.md.")
    print("=" * 70)
    return 0 if RESULTS["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
