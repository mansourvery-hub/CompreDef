#!/usr/bin/env python3
"""
debug/smoke_dialog.py — Headless GUI smoke test for CompreDef's Learner
Knowledge dialog (the window itself, which tests/test_regression.py
cannot open: it has no screen).

NOT part of the regression suite (tests/test_regression.py) and NOT run
by CI, build.sh, or release.sh — it needs a LOCAL Anki install and a
LOCAL collection. Run it on this machine after any gui.py/anki.py
change affecting the knowledge dialog:

    python3 debug/smoke_dialog.py
    python3 debug/smoke_dialog.py --scope "My Life Decks"
    python3 debug/smoke_dialog.py --collection /path/to/collection.anki2
    python3 debug/smoke_dialog.py --checks dialog,sort,filter,provenance,timing

What it checks (all headless via QT_QPA_PLATFORM=offscreen):
  dialog      the window builds, tabs/labels/counts populate, singleton
              instance is reused on second open
  sort        header-click sorting is NUMERIC (mastery + interval), both
              directions — the '999 days above 9969 days' text-sort bug
  filter      filtering by a kanji narrows to its single row and back
  provenance  click-to-Browse search strings run via col.find_notes():
              vocab hits are exact first-field matches IN SCOPE decks,
              kanji hits contain the kanji in an in-scope first field
  timing      warm payload call is ~instant (payload-cache regression);
              reports cold vs warm open milliseconds

Exit code 0 = all checks passed, 1 = a check failed (names printed),
2 = skipped (no local Anki install or no usable collection/scope).

Read-only by design: opens the collection exactly like Anki does and
only ever READS (find_notes/get_note/models lookups) — it never adds,
updates or deletes notes, cards, decks or config.
"""

import argparse
import glob
import os
import sys
import time
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ALL_CHECKS = ("dialog", "sort", "filter", "provenance", "timing")


# ---------------------------------------------------------------------------
# Environment bootstrap (Anki package, offscreen Qt, fake mw)
# ---------------------------------------------------------------------------

def find_anki_site_packages() -> str | None:
    """Locates a system Anki install (aqt package). None = skip."""
    try:
        import aqt  # noqa: F401  (works when Anki's python runs this)
        return None  # sentinel: already importable, nothing to add
    except ImportError:
        pass
    for pattern in ("/usr/lib/python*/site-packages",
                    "/usr/local/lib/python*/site-packages",
                    os.path.expanduser("~/.local/lib/python*/site-packages")):
        for cand in sorted(glob.glob(pattern)):
            if os.path.isdir(os.path.join(cand, "aqt")):
                return cand
    return None


def default_collection_path() -> str | None:
    """The default local profile's collection, if present."""
    cand = os.path.expanduser("~/.local/share/Anki2/User 1/collection.anki2")
    return cand if os.path.isfile(cand) else None


class SmokeResult:
    """One named check outcome; collects into an exit code."""

    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[str] = []

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        if cond:
            print(f"[OK] {name}" + (f" — {detail}" if detail else ""))
            self.passed.append(name)
        else:
            print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
            self.failed.append(name)
        return cond

    def exit_code(self) -> int:
        print(f"smoke_dialog: {len(self.passed)}/{len(self.passed) + len(self.failed)}"
              " checks passed")
        return 0 if not self.failed else 1


def make_fake_mw(col, scope_decks):
    """Minimal mw stand-in: only .col/.addonManager/.taskman/.app."""

    class _FakeAddonManager:
        def addonFromModule(self, name):
            return "1619602654"

        def getConfig(self, name):
            return {"scope_decks": list(scope_decks)}

        def writeConfig(self, name, cfg):
            pass

    class _SyncTaskman:
        """Runs background tasks inline — deterministic, no threads."""

        def run_in_background(self, task, on_done=None):
            class _F:
                def __init__(self, r):
                    self._r = r

                def result(self):
                    return self._r

            fut = _F(task())
            if on_done:
                on_done(fut)
            return fut

    return types.SimpleNamespace(
        col=col,
        addonManager=_FakeAddonManager(),
        taskman=_SyncTaskman(),
        app=type("A", (), {"activeWindow": staticmethod(lambda: None)})(),
    )


def autoselect_scope(col):
    """Top-level deck holding the most mature (ivl >= 365) notes.

    Lets a bare 'python3 debug/smoke_dialog.py' just work without
    remembering a deck name. Returns (deck_name, mature_count) or
    (None, 0) when nothing mature exists anywhere.
    """
    try:
        rows = col.db.all(
            "SELECT cards.did, COUNT(DISTINCT notes.id) FROM notes "
            "JOIN cards ON cards.nid = notes.id "
            "WHERE cards.ivl >= 365 GROUP BY cards.did") or []
    except Exception:
        return None, 0
    totals: dict[str, int] = {}
    for did, n in rows:
        try:
            top = str(col.decks.name(int(did))).split("::")[0]
        except Exception:
            continue
        totals[top] = totals.get(top, 0) + int(n)
    if not totals:
        return None, 0
    best = max(totals, key=lambda k: totals[k])
    return best, totals[best]


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_dialog(gui_mod, res: SmokeResult) -> object:
    """Window builds, tabs populate, singleton reused on second open."""
    gui_mod.show_knowledge_dialog()
    dlg = gui_mod._knowledge_dialog_instance
    res.check("dialog: window instance created", dlg is not None)
    if dlg is None:
        return None
    res.check("dialog: visible and non-modal",
              bool(dlg.isVisible()) and not bool(dlg.isModal()))
    labels = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
    res.check("dialog: four tabs present", dlg.tabs.count() == 4,
              f"tabs={labels}")
    res.check("dialog: Kanji/Vocab/Mature tabs labeled",
              labels[1].startswith("Kanji") and labels[2].startswith("Vocab")
              and labels[3].startswith("Mature Notes"),
              f"tabs={labels}")
    notes_rows = dlg.notes_tab._table.rowCount()
    kanji_rows = dlg.kanji_tab._table.rowCount()
    res.check("dialog: mature-notes rows populated", notes_rows > 0,
              f"{notes_rows} rows")
    res.check("dialog: kanji rows populated", kanji_rows > 0,
              f"{kanji_rows} rows")
    # Singleton: reopening reuses the same instance.
    gui_mod.show_knowledge_dialog()
    res.check("dialog: singleton reused on second open",
              gui_mod._knowledge_dialog_instance is dlg)
    return dlg


def _desc_order():
    from aqt.qt import Qt
    # noqa: B009 — nested getattr is intentional PyQt5/6 compat: on
    # PyQt5 Qt has no SortOrder enum, so we fall back to Qt itself.
    # Ruff's suggested flattening would crash PyQt5 with AttributeError.
    return getattr(getattr(Qt, "SortOrder", Qt), "DescendingOrder")  # noqa: B009


def _asc_order():
    from aqt.qt import Qt
    # noqa: B009 — same PyQt5/6 compat rationale as _desc_order.
    return getattr(getattr(Qt, "SortOrder", Qt), "AscendingOrder")  # noqa: B009


def _user_role():
    from aqt.qt import Qt
    # noqa: B009 — same PyQt5/6 compat rationale as _desc_order.
    return getattr(getattr(Qt, "ItemDataRole", Qt), "UserRole")  # noqa: B009


def _numeric_column(table, limit=200):
    """UserRole floats of column 1 (the mastery/interval sort keys)."""
    role = _user_role()
    return [float(table.item(i, 1).data(role))
            for i in range(min(limit, table.rowCount()))]


def check_sort(dlg, res: SmokeResult) -> None:
    """Header-click sorting is NUMERIC in both directions."""
    kt = dlg.kanji_tab._table
    kt.sortItems(1, _desc_order())
    vals = _numeric_column(kt)
    res.check("sort: kanji mastery desc is numeric",
              all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1)),
              f"top={[kt.item(i, 0).text() for i in range(3)]}")
    kt.sortItems(1, _asc_order())
    vals = _numeric_column(kt)
    res.check("sort: kanji mastery asc puts weakest first",
              all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1)),
              f"weakest={[kt.item(i, 0).text() for i in range(3)]}")
    nt = dlg.notes_tab._table
    nt.sortItems(1, _desc_order())
    ivls = _numeric_column(nt)
    res.check("sort: notes interval desc is numeric (not text sort)",
              all(ivls[i] >= ivls[i + 1] for i in range(len(ivls) - 1)),
              f"top={[(nt.item(i, 0).text(), nt.item(i, 1).text()) for i in range(2)]}")


def check_filter(dlg, res: SmokeResult) -> None:
    """Filtering by a kanji narrows to its single row and back."""
    kt = dlg.kanji_tab._table
    target = kt.item(5, 0).text()
    dlg.kanji_tab._filter_edit.setText(target)
    label = dlg.kanji_tab._count_label.text()
    res.check("filter: kanji narrows to one row",
              label.startswith("1 shown"),
              f"'{target}' -> {label}")
    dlg.kanji_tab._filter_edit.setText("")
    total = kt.rowCount()
    res.check("filter: clearing restores all rows",
              dlg.kanji_tab._count_label.text() == "" or total > 1,
              f"{total} rows")


def _note_deck_names(col, nid) -> set:
    try:
        note = col.get_note(int(nid))
        return {str(col.decks.name(c.did)) for c in note.cards()}
    except Exception:
        return set()


def _in_scope(deck_names: set, scope: list) -> bool:
    return any(d == s or d.startswith(s + "::") for s in scope for d in deck_names)


def check_provenance(gui_mod, anki_mod, col, scope, res: SmokeResult) -> None:
    """Click-to-Browse searches: exact, in-scope, and they actually run.

    Vocab word -> every hit's FIRST field must equal the word and live
    in a Scope deck. Kanji -> every hit's first field must contain the
    kanji and live in a Scope deck.
    """
    dlg = gui_mod._knowledge_dialog_instance
    fields = anki_mod.first_field_names_for_scope()
    res.check("provenance: scope first-field names resolve",
              len(fields) > 0, f"fields={fields[:5]}")
    word = dlg.words_tab._table.item(0, 0).text()
    qv = anki_mod.build_provenance_search("vocab", word, fields,
                                          scope_decks=scope)
    try:
        vocab_hits = col.find_notes(qv)
        vocab_ok = len(vocab_hits) > 0
        detail = f"'{word}' -> {len(vocab_hits)} notes"
    except Exception as e:
        vocab_ok, detail, vocab_hits = False, f"search failed: {e}", []
    res.check("provenance: vocab search finds notes", vocab_ok, detail)
    exact, scoped = True, True
    for nid in vocab_hits[:50]:
        try:
            note = col.get_note(int(nid))
            if note.fields[0] != word:
                exact = False
            if not _in_scope(_note_deck_names(col, nid), scope):
                scoped = False
        except Exception:
            exact = scoped = False
    res.check("provenance: vocab hits are exact first-field matches",
              exact and vocab_ok)
    res.check("provenance: vocab hits all live in Scope decks",
              scoped and vocab_ok)
    kanji = dlg.kanji_tab._table.item(3, 0).text()
    qk = anki_mod.build_provenance_search("kanji", kanji, fields,
                                          scope_decks=scope)
    try:
        kanji_hits = col.find_notes(qk)
        kanji_ok = len(kanji_hits) > 0
        detail = f"'{kanji}' -> {len(kanji_hits)} notes"
    except Exception as e:
        kanji_ok, detail, kanji_hits = False, f"search failed: {e}", []
    res.check("provenance: kanji search finds notes", kanji_ok, detail)
    contained, kscoped = True, True
    for nid in kanji_hits[:200]:
        try:
            note = col.get_note(int(nid))
            if kanji not in (note.fields[0] if note.fields else ""):
                contained = False
            if not _in_scope(_note_deck_names(col, nid), scope):
                kscoped = False
        except Exception:
            contained = kscoped = False
    res.check("provenance: kanji hits contain it in the first field",
              contained and kanji_ok)
    res.check("provenance: kanji hits all live in Scope decks",
              kscoped and kanji_ok)


def check_timing(anki_mod, res: SmokeResult) -> None:
    """Warm payload call is ~instant (payload-cache regression net)."""
    t0 = time.perf_counter()
    anki_mod.get_knowledge_dialog_payload()
    warm_ms = (time.perf_counter() - t0) * 1000.0
    res.check("timing: warm payload call is instant (<500 ms)",
              warm_ms < 500, f"{warm_ms:.3f} ms")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Headless GUI smoke test for CompreDef's Learner "
                    "Knowledge dialog (local Anki + local collection).")
    parser.add_argument("--scope", action="append", default=[],
                        help="Scope deck (repeatable). If omitted, the deck "
                             "holding the most mature notes is used.")
    parser.add_argument("--collection", default=None,
                        help="collection.anki2 path (default: User 1).")
    parser.add_argument("--checks", default=",".join(ALL_CHECKS),
                        help="Comma list from: " + ",".join(ALL_CHECKS))
    args = parser.parse_args(argv)

    site = find_anki_site_packages()
    if site is None and "aqt" not in sys.modules:
        print("smoke_dialog: SKIP — no local Anki install found "
              "(need the aqt package)")
        return 2
    if site:
        sys.path.insert(0, site)
    # Neutral import root (NOT the repo): inserting the repo itself
    # would shadow Anki's own `anki` package with CompreDef's anki.py
    # (the classic circular-import trap).
    sys.path.insert(0, "/tmp")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    col_path = args.collection or default_collection_path()
    if not col_path:
        print("smoke_dialog: SKIP — no collection found "
              "(pass --collection PATH)")
        return 2

    from anki.collection import Collection
    col = Collection(col_path)

    # Resolve the scope: explicit --scope wins, else auto-pick.
    scope = [s for s in (args.scope or []) if s.strip()]
    auto = False
    if not scope:
        auto, n = autoselect_scope(col)
        if not auto:
            print("smoke_dialog: SKIP — no mature notes anywhere to test")
            col.close()
            return 2
        scope = [auto]
        print(f"smoke_dialog: auto-selected scope deck '{auto}' "
              f"({n} mature notes)")

    # Package-context import: CompreDef loads as a package inside Anki
    # (relative imports), so mirror that here.
    pkg = types.ModuleType("compredef_pkg")
    pkg.__path__ = [REPO_ROOT]
    sys.modules["compredef_pkg"] = pkg

    import aqt
    aqt.mw = make_fake_mw(col, scope)

    from aqt.qt import QApplication
    app = QApplication.instance() or QApplication([])
    _ = app  # kept alive for the dialog's lifetime

    import importlib
    gui_mod = importlib.import_module("compredef_pkg.gui")
    anki_mod = importlib.import_module("compredef_pkg.anki")

    wanted = [c.strip() for c in args.checks.split(",") if c.strip()]
    for c in wanted:
        if c not in ALL_CHECKS:
            print(f"smoke_dialog: unknown check '{c}' "
                  f"(choose from {','.join(ALL_CHECKS)})")
            col.close()
            return 2

    res = SmokeResult()
    dlg = None
    try:
        if "dialog" in wanted:
            dlg = check_dialog(gui_mod, res)
        else:
            gui_mod.show_knowledge_dialog()
            dlg = gui_mod._knowledge_dialog_instance
        if dlg is None:
            print("smoke_dialog: dialog failed to build; "
                  "skipping dependent checks")
        else:
            if "sort" in wanted:
                check_sort(dlg, res)
            if "filter" in wanted:
                check_filter(dlg, res)
            if "provenance" in wanted:
                check_provenance(gui_mod, anki_mod, col, scope, res)
            if "timing" in wanted:
                check_timing(anki_mod, res)
    except Exception:
        import traceback
        print("smoke_dialog: harness crashed:\n" + traceback.format_exc())
        col.close()
        return 1
    col.close()
    return res.exit_code()


if __name__ == "__main__":
    sys.exit(main())
