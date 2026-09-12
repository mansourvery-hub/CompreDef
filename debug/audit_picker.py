#!/usr/bin/env python3
"""
debug/audit_picker.py — Offline dictionary-picker audit (Ring 2/3 tooling).

Compares every dictionary's definition for each card of the user's
real 10-card deck, under 3 learner profiles (mine / beginner / native)
and both dictionary sources (local dictionaries / Yomitan bridge), then
writes a human-readable HTML report for grading.

NOT part of the regression suite and NEVER run by CI, build.sh or
release.sh: capture needs the LOCAL machine (AnkiConnect, the local
collection, the indexed dictionary cache, optionally the Yomitan
bridge). The AUDIT half (fixture -> HTML) is pure offline recompute.

Usage:
    python3 debug/audit_picker.py --capture   # live capture -> tests/fixtures/picker_audit.json
    python3 debug/audit_picker.py --html      # fixture -> debug/reports/picker_audit_<ts>.html
    python3 debug/audit_picker.py --capture --html   # both (the normal run)
    python3 debug/audit_picker.py --import-grades picker_grades.json  # record chat verdicts

Grading: the user judges each ★ pick in chat; grades are recorded
with --import-grades (JSON: {source: {note_id: {profile: {hash,
verdict}}}}), and the strict Ring 1 test then checks every grade: a
"wrong" verdict FAILS the suite until the picker is fixed.

Why two halves: the strict Ring 1 test (test_picker_audit_strict)
reads ONLY the frozen fixture — hermetic, millisecond-fast, green on
CI with no Anki/DB/network. Re-running --capture refreshes the frozen
expectations after dictionaries or knowledge legitimately change
(new dict installed, intervals grew); the diff then shows exactly
which picks moved and why.

Read-only by design (except the fixture/report it writes):
- deck words come from AnkiConnect (tiny: 11 notes), never from a
  second collection opener;
- knowledge rows come from an SQLite ONLINE BACKUP of collection.anki2
  into a temp copy (brief shared locks only, WAL-safe) with every
  query running against the COPY. Rationale: AGENTS.md Q-D1 forbids
  external sqlite3 on the live collection (locks/corruption) and a
  6070-card AnkiConnect pull (~1 GB of rendered Q/A) would freeze the
  running Anki for minutes. The add-on runtime itself is untouched and
  still uses mw.col.db exclusively.
- the dictionary cache is only SELECTed (the add-on does the same on
  every generation); the Yomitan bridge is only queried, never driven.
"""

import argparse
import datetime
import hashlib
import html as html_mod
import json
import os
import sqlite3
import sys
import tempfile
import types
import urllib.request

DEBUG_ROOT = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(DEBUG_ROOT)
sys.path.insert(0, REPO_ROOT)

# anki.py does `from aqt import mw` at module top; the audit only needs
# its pure math/regexes (never mw), so a minimal stub keeps this
# Anki-free — the same trick tests/test_units.py uses.
sys.modules.setdefault("aqt", types.SimpleNamespace(mw=None))  # type: ignore[arg-type]

import anki as anki_mod  # noqa: E402
import picker as picker_mod  # noqa: E402
import scoring as scoring_mod  # noqa: E402
import utils as utils_mod  # noqa: E402
from models import DictionaryEntry  # noqa: E402

ANKI_CONNECT_URL = "http://127.0.0.1:8765"
TARGET_DECK = "My Life Decks::Japanese::anki-japanese-template"
FIXTURE_PATH = os.path.join(REPO_ROOT, "tests", "fixtures", "picker_audit.json")
REPORTS_DIR = os.path.join(DEBUG_ROOT, "reports")
HOME = os.path.expanduser("~")
INSTALLED_META = os.path.join(
    HOME, ".local/share/Anki2/addons21/1619602654/meta.json")
PROFILE_CACHE = os.path.join(
    HOME, ".local/share/Anki2/addons21/1619602654/user_files/cache")
COLLECTION = os.path.join(HOME, ".local/share/Anki2/User 1/collection.anki2")

PROFILES = ("mine", "beginner", "native")
SOURCES = ("local", "yomitan")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _def_hash(definition: str) -> str:
    """Short stable id for a captured definition (suite matching)."""
    return hashlib.sha1(definition.encode("utf-8")).hexdigest()[:12]


def _ac(action: str, **params):  # type: ignore[no-untyped-def]
    """One AnkiConnect call; raises loudly with the bridge error."""
    payload = json.dumps(
        {"action": action, "version": 6, "params": params}).encode("utf-8")
    req = urllib.request.Request(
        ANKI_CONNECT_URL, data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if result.get("error"):
        raise RuntimeError(f"AnkiConnect {action} failed: {result['error']}")
    return result["result"]


def _installed_config() -> dict:
    """The user's REAL add-on config (dictionaries, scope, targets)."""
    with open(INSTALLED_META, encoding="utf-8") as f:
        meta = json.load(f)
    cfg = meta.get("config")
    if not isinstance(cfg, dict):
        raise RuntimeError(f"no user config in {INSTALLED_META}")
    return cfg


def _native_points(candidates: list, exclude: tuple = ()) -> tuple:
    """A Japanese native knows every kanji/compound in the candidates."""
    kanji: dict = {}
    vocab: dict = {}
    for title, definition in candidates:
        base = scoring_mod.scoring_base_text(definition, exclude)
        for ch in set(c for c in base if "\u4e00" <= c <= "\u9fff"):
            kanji[ch] = 1.0
        for word in scoring_mod.extract_kanji_words(base):
            vocab[word] = 1.0
    return kanji, vocab


def _profile_points(profile: str, mine: dict, candidates: list,
                    exclude: tuple = ()) -> tuple:
    """(kanji_points, vocab_points) for one audit profile."""
    if profile == "mine":
        return mine["kanji"], mine["vocab"]
    if profile == "beginner":
        # Total beginner: knows no words and no kanji — every score is
        # 0.0, so the ranking is pure tie-break (most kanji first).
        return {}, {}
    if profile == "native":
        return _native_points(candidates, exclude)
    raise ValueError(f"unknown profile {profile!r}")


def _ranked(candidates: list, kanji: dict, vocab: dict,
            exclude: tuple = ()) -> list:
    """picker.rank_definitions as JSON-able rows (best first)."""
    out = []
    for (title, definition), res in picker_mod.rank_definitions(
            candidates, kanji, vocab, exclude=exclude):
        out.append({
            "dict": title,
            "hash": _def_hash(definition),
            "density": round(res.density_total, 4),
            "kanji_score": round(res.kanji_score, 3),
            "vocab_score": round(res.vocab_score, 3),
            "total": round(res.total_score, 3),
            "kanji_count": res.kanji_count,
            "is_reference": scoring_mod.is_reference_title(definition),
            "excerpt": utils_mod.extract_base_text(definition)[:160],
            "definition": definition,
        })
    return out


# ---------------------------------------------------------------------------
# Capture (live, local machine only)
# ---------------------------------------------------------------------------

def _capture_words(fallback_file: str = "") -> list:
    """The deck's notes via AnkiConnect: [{note_id, word, reading}].

    Falls back to --words-file (a JSON list with the same shape) when
    Anki is closed — the word list changes only when the user edits
    the deck, so a verified transcript is a faithful stand-in and the
    fixture records exactly which words were audited.
    """
    if fallback_file:
        with open(fallback_file, encoding="utf-8") as f:
            raw_notes = json.load(f)
        print(f"words: {len(raw_notes)} (from {fallback_file}, Anki closed)")
    else:
        nids = _ac("findNotes", query=f'"deck:{TARGET_DECK}"')
        raw_notes = []
        for note in _ac("notesInfo", notes=nids):
            fields = note["fields"]
            raw_notes.append({
                "note_id": note["noteId"],
                "expression_raw": fields.get("Expression", {}).get("value", ""),
                # Production resolves the reading through the per-type
                # targets; this note type maps reading_field="reading".
                "reading": fields.get("reading", {}).get("value", "") or "",
            })
    words = []
    for note in raw_notes:
        raw = note.get("expression_raw", "")
        words.append({
            "note_id": note["note_id"],
            "word": utils_mod.extract_clean_word(
                utils_mod.extract_base_text(raw) if "<" in raw else raw),
            "reading": note.get("reading", "") or "",
            "expression_raw": raw[:120],
        })
    words.sort(key=lambda w: w["note_id"])
    if not words:
        raise RuntimeError(f"no notes found in deck {TARGET_DECK!r}")
    return words


def _capture_mine_points(scope_decks: list) -> dict:
    """Interval-weighted knowledge, replicating anki.py on a backup copy.

    Same admission rule (mature notes, ivl >= 365, Scope decks,
    first field only), same math (ivl/365 capped at 1.0, vocab =
    multi-kanji compounds only). Counts are printed so the run can be
    eyeballed against the Learner Knowledge dialog.
    """
    src = sqlite3.connect(f"file:{COLLECTION}?mode=ro", uri=True, timeout=10)
    tmp = tempfile.NamedTemporaryFile(
        suffix=".anki2", prefix="compredef_audit_", delete=False)
    tmp.close()
    try:
        dst = sqlite3.connect(tmp.name, timeout=30)
        try:
            # Online backup: brief shared locks, WAL-safe; every query
            # below runs against the COPY, never the live collection.
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    try:
        db = sqlite3.connect(f"file:{tmp.name}?mode=ro", uri=True)
        try:
            decks = {r[1].replace("\x1f", "::"): r[0]
                     for r in db.execute("SELECT id, name FROM decks")}
            dids = {did for name, did in decks.items()
                    if any(name == s or name.startswith(s + "::")
                           for s in scope_decks)}
            if not dids:
                raise RuntimeError(
                    f"scope decks not found: {scope_decks!r}")
            did_list = ",".join(str(int(d)) for d in sorted(dids))
            rows = db.execute(
                "SELECT notes.flds, MAX(cards.ivl) FROM notes "
                "JOIN cards ON cards.nid = notes.id "
                f"WHERE cards.ivl >= {anki_mod._MATURE_IVL_DAYS} "
                f"AND cards.did IN ({did_list}) "
                "GROUP BY notes.id").fetchall()
        finally:
            db.close()
    finally:
        os.unlink(tmp.name)
    kanji: dict = {}
    vocab: dict = {}
    for blob, ivl in rows:
        text = anki_mod._first_field_text(blob)
        if not text:
            continue
        pts = anki_mod._maturity_points(ivl)
        for ch in set(anki_mod._KANJI_RE.findall(text)):
            if pts > kanji.get(ch, 0.0):
                kanji[ch] = round(pts, 4)
        if anki_mod._KANJI_WORD_RE.match(text):
            if pts > vocab.get(text, 0.0):
                vocab[text] = round(pts, 4)
    print(f"knowledge: {len(rows)} mature notes -> "
          f"{len(kanji)} kanji / {len(vocab)} vocab points")
    return {"kanji": kanji, "vocab": vocab,
            "mature_notes": len(rows)}


def _capture_local(words: list, dictionary_paths: list) -> dict:
    """Every dictionary's raw entries per word (unfiltered)."""
    from provider import LocalSQLiteProvider
    provider = LocalSQLiteProvider(PROFILE_CACHE)
    out: dict = {}
    for item in words:
        per_word = []
        for path in dictionary_paths:
            try:
                entries = provider.lookup_by_path(
                    path, item["word"], item["reading"])
            except Exception as e:  # noqa: BLE001 — one bad dict
                print(f"  local lookup failed for {path}: {e}")  # never kills the run
                continue
            for entry in entries:
                per_word.append({
                    "dict": entry.dictionary_title,
                    "path": entry.dictionary_path,
                    "definition": entry.definition,
                })
        out[str(item["note_id"])] = per_word
        print(f"local {item['word']!r}: {len(per_word)} candidates")
    return out


def _capture_yomitan(words: list) -> dict:
    """Live Yomitan bridge entries per word ([] when unreachable)."""
    from yomitan import fetch_yomitan_definitions, clear_yomitan_cache
    clear_yomitan_cache()
    out: dict = {}
    for item in words:
        try:
            entries = fetch_yomitan_definitions(
                item["word"], item["reading"])
        except Exception as e:  # noqa: BLE001 — bridge down is a
            print(f"  yomitan lookup failed: {e}")  # valid capture, not a crash
            entries = []
        out[str(item["note_id"])] = [
            {"dict": e.dictionary_title, "definition": e.definition}
            for e in entries
        ]
        print(f"yomitan {item['word']!r}: {len(entries)} candidates")
    return out


def cmd_capture(words_file: str = "") -> dict:
    """Runs the full live capture and writes the frozen fixture."""
    cfg = _installed_config()
    dictionary_paths = utils_mod.resolve_dictionary_paths(
        cfg.get("dictionaries"), cfg.get("dictionary_folder", ""),
        cfg.get("disabled_dictionaries"))
    if not dictionary_paths:
        raise RuntimeError("no dictionaries in installed config")
    scope = cfg.get("scope_decks") or []
    print(f"dictionaries ({len(dictionary_paths)}):")
    for path in dictionary_paths:
        print(f"  - {path}")
    print(f"scope: {scope}")
    words = _capture_words(words_file)
    print(f"words: {len(words)}")
    mine = _capture_mine_points(scope)
    local = _capture_local(words, dictionary_paths)
    yomitan = _capture_yomitan(words)
    try:
        from provider import LocalSQLiteProvider
        renderer_version = LocalSQLiteProvider.RENDERER_VERSION
    except Exception:  # noqa: BLE001 — meta only; never blocks capture
        renderer_version = "unknown"
    fixture = {
        "meta": {
            "captured_at": datetime.datetime.now().isoformat(
                timespec="seconds"),
            "deck": TARGET_DECK,
            "dictionaries": dictionary_paths,
            "scope": scope,
            "renderer_version": renderer_version,
            "strategy": picker_mod.get_active_strategy().name,
        },
        "words": words,
        "mine": mine,
        "local": local,
        "yomitan": yomitan,
        "grades": {},
        "expected": _compute_expected(words, mine, local, yomitan),
    }
    os.makedirs(os.path.dirname(FIXTURE_PATH), exist_ok=True)
    with open(FIXTURE_PATH, "w", encoding="utf-8") as f:
        json.dump(fixture, f, ensure_ascii=False, indent=1)
    print(f"fixture written: {FIXTURE_PATH}")
    return fixture


def _filtered_candidates(items: list) -> list:
    """Production picker input: reference titles dropped (unless lone)."""
    entries = [DictionaryEntry(word="", reading="", definition=i["definition"],
                               dictionary_title=i["dict"],
                               dictionary_path=i.get("path", ""))
               for i in items]
    valid = picker_mod.filter_valid_entries(entries)
    return [(e.dictionary_title, e.definition) for e in valid]


def _compute_expected(words: list, mine: dict, local: dict, yomitan: dict) -> dict:
    """Frozen full rankings per source x word x profile (strict order)."""
    expected: dict = {"local": {}, "yomitan": {}}
    raws = {"local": local, "yomitan": yomitan}
    for source in SOURCES:
        for item in words:
            nid = str(item["note_id"])
            cands = _filtered_candidates(raws[source].get(nid, []))
            exclude = (item["word"],) if item.get("word") else ()
            per_profile = {}
            for profile in PROFILES:
                kp, vp = _profile_points(profile, mine, cands, exclude)
                per_profile[profile] = [
                    {"dict": title, "hash": _def_hash(defn),
                     "density": round(res.density_total, 4),
                     "total": round(res.total_score, 3),
                     "kanji_count": res.kanji_count}
                    for (title, defn), res in picker_mod.rank_definitions(
                        cands, kp, vp, exclude=exclude)
                ]
            expected[source][nid] = per_profile
    return expected


# ---------------------------------------------------------------------------
# Audit (offline): fixture -> HTML report
# ---------------------------------------------------------------------------

def _load_fixture() -> dict:
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


def cmd_html(fixture: dict) -> str:
    """Recomputes every ranking from the fixture and writes the report."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(REPORTS_DIR, f"picker_audit_{stamp}.html")
    parts = [_html_head(fixture)]
    for n, item in enumerate(fixture["words"]):
        parts.append(_html_word(fixture, item, open_first=(n == 0)))
    parts.append(_html_foot())
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    print(f"report written: {path}")
    return path


def _short_dict(title: str) -> str:
    """Compact dictionary label for the winners matrix."""
    import re
    return re.sub(r"[　\s]+第.*版\s*$", "", title).strip() or title


def _html_head(fixture: dict) -> str:
    meta = fixture["meta"]
    mine = fixture["mine"]
    dictionary_items = "".join(
        f"<li>{html_mod.escape(p)}</li>" for p in meta["dictionaries"])
    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<title>CompreDef picker audit — {meta["captured_at"]}</title>
<style>
body {{ font-family: sans-serif; max-width: 70em; margin: 2em auto; padding: 0 1em; }}
table {{ border-collapse: collapse; width: 100%; margin: .5em 0 1.5em; }}
th, td {{ border: 1px solid #ccc; padding: .3em .5em; text-align: left; font-size: .9em; }}
th {{ background: #f0f0f0; }}
tr.winner td {{ background: #e6f4ea; font-weight: bold; }}
tr.ref td {{ color: #888; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.excerpt {{ color: #333; }}
details.def {{ margin: .2em 0; }}
.badge {{ display: inline-block; padding: .1em .5em; border-radius: .5em; background: #eee; font-size: .8em; }}
.badge.ok {{ background: #e6f4ea; }}
.badge.drift {{ background: #fce8e6; }}
h2.word {{ border-bottom: 2px solid #333; padding-bottom: .2em; }}
td.win {{ font-weight: bold; }}
</style></head><body>
<h1>CompreDef picker audit</h1>
<p>Captured {html_mod.escape(meta["captured_at"])} — deck
{html_mod.escape(meta["deck"])} — strategy
<strong>{html_mod.escape(meta.get("strategy", "?"))}</strong>
(comprehension = known-kanji density + known-compound density).</p>
<p>Your knowledge: {len(mine["kanji"])} kanji / {len(mine["vocab"])} vocab points
from {mine.get("mature_notes", "?")} mature notes.
Beginner knows nothing (most-kanji wins); native knows everything in
the candidates.</p>
<h2>Winners at a glance</h2>
<p>★ = picked definition (highest density). Open a word below for the
full ranking. Grey rows in rankings = cross-reference titles the picker
drops (shown, never picked unless alone). 「no entry」= that dictionary
has nothing for the word.</p>
{_html_matrix(fixture)}
<h2>Dictionaries ({len(meta["dictionaries"])})</h2><ol>{dictionary_items}</ol>"""


def _html_matrix(fixture: dict) -> str:
    """One-glance winners matrix: rows = words, columns = profiles."""
    mine = fixture["mine"]
    rows = ["<table><tr><th>word</th>"] + [
        f"<th colspan='2'>{s}</th>" for s in SOURCES] + ["</tr><tr><th></th>"]
    for _ in SOURCES:
        rows += ["<th>you</th><th>beginner / native</th>"]
    rows += ["</tr>"]
    for item in fixture["words"]:
        nid = str(item["note_id"])
        cells = [f"<tr><td><strong>{html_mod.escape(item['word'] or '(empty)')}"
                 f"</strong><br><span class='excerpt'>"
                 f"{html_mod.escape(item['reading'] or '—')}</span></td>"]
        for source in SOURCES:
            raw = fixture[source].get(nid, [])
            cands = _filtered_candidates(raw)
            exclude = (item["word"],) if item.get("word") else ()
            for profiles in (("mine",), ("beginner", "native")):
                parts = []
                for profile in profiles:
                    kp, vp = _profile_points(profile, mine, cands, exclude)
                    ranked = _ranked(cands, kp, vp, exclude)
                    if ranked:
                        parts.append(
                            f"{html_mod.escape(_short_dict(ranked[0]['dict']))} "
                            f"{ranked[0]['density']:.2f}")
                    else:
                        parts.append("—")
                # Beginner and native usually agree; collapse when equal.
                if len(set(parts)) == 1:
                    cells.append(f"<td class='win'>{parts[0]}</td>")
                else:
                    cells.append(f"<td>{' / '.join(parts)}</td>")
        rows.append("".join(cells) + "</tr>")
    return "".join(rows) + "</table>"


def _html_foot() -> str:
    return "</body></html>"


def _html_word(fixture: dict, item: dict, open_first: bool) -> str:
    nid = str(item["note_id"])
    mine = fixture["mine"]
    chunks = [
        f'<h2 class="word">{html_mod.escape(item["word"])}'
        f' <span class="badge">{html_mod.escape(item["reading"] or "—")}</span></h2>'
    ]
    for source in SOURCES:
        raw = fixture[source].get(nid, [])
        cands = _filtered_candidates(raw)
        exclude = (item["word"],) if item.get("word") else ()
        chunks.append(f"<h3>{source} — {len(cands)} candidates "
                      f"({len(raw)} raw)</h3>")
        if not cands:
            chunks.append("<p><em>no entry in any dictionary.</em></p>")
            continue
        for profile in PROFILES:
            kp, vp = _profile_points(profile, mine, cands, exclude)
            ranked = _ranked(cands, kp, vp, exclude)
            frozen = fixture["expected"][source][nid][profile]
            live_order = [r["hash"] for r in ranked]
            frozen_order = [r["hash"] for r in frozen]
            if live_order == frozen_order:
                badge = '<span class="badge ok">MATCH</span>'
            else:
                badge = '<span class="badge drift">DRIFT vs frozen</span>'
            chunks.append(
                f'<details {"open" if open_first and profile == "mine" and source == "local" else ""}>'
                f"<summary><strong>{profile}</strong> "
                f'winner: <span class="badge">{html_mod.escape(_short_dict(ranked[0]["dict"]))}</span> '
                f'score {ranked[0]["density"]:.3f} '
                f'{badge}</summary>'
                f'<table><tr><th>#</th><th>dictionary</th>'
                f"<th>score</th><th>n</th>"
                f"<th>definition</th></tr>")
            for i, row in enumerate(ranked):
                cls = "winner" if i == 0 else ("ref" if row["is_reference"] else "")
                chunks.append(
                    f'<tr class="{cls}"><td class="num">{i + 1}'
                    f'{" ★" if i == 0 else ""}</td>'
                    f"<td>{html_mod.escape(row['dict'])}</td>"
                    f'<td class="num">{row["density"]:.3f}</td>'
                    f'<td class="num">{row["kanji_count"]}</td>'
                    f'<td><span class="excerpt">{html_mod.escape(row["excerpt"])}</span>'
                    f'<details class="def"><summary>full</summary>'
                    f"{row['definition']}</details></td></tr>")
            chunks.append("</table></details>")
    return "\n".join(chunks)


def cmd_import_grades(path: str) -> None:
    """Merges an exported grades JSON into the fixture (suite checks it)."""
    with open(path, encoding="utf-8") as f:
        grades = json.load(f)
    if not isinstance(grades, dict):
        raise RuntimeError(f"grades file is not an object: {path}")
    fixture = _load_fixture()
    merged = fixture.get("grades", {})
    count = 0
    for source, words in grades.items():
        if source not in SOURCES or not isinstance(words, dict):
            raise RuntimeError(f"bad grades source {source!r} in {path}")
        for nid, profiles in words.items():
            if not isinstance(profiles, dict):
                raise RuntimeError(f"bad grades entry {source}/{nid}")
            for profile, grade in profiles.items():
                if (profile not in PROFILES or not isinstance(grade, dict)
                        or grade.get("verdict") not in ("correct", "wrong")
                        or not grade.get("hash")):
                    raise RuntimeError(
                        f"bad grade {source}/{nid}/{profile} in {path}")
                count += 1
        merged[source] = {**merged.get(source, {}), **words}
    fixture["grades"] = merged
    with open(FIXTURE_PATH, "w", encoding="utf-8") as f:
        json.dump(fixture, f, ensure_ascii=False, indent=1)
    print(f"merged {count} grades into {FIXTURE_PATH}")


def cmd_refreeze() -> None:
    """Recomputes frozen expectations from the stored candidates.

    No network, no databases: reads the fixture's captured words,
    candidates and knowledge, re-ranks with the CURRENT picker code,
    and overwrites `expected` (+ strategy + timestamp). For scoring
    changes like headword exclusion or boilerplate stripping, where
    the captured definitions are still valid but their order moves.
    """
    fixture = _load_fixture()
    fixture["expected"] = _compute_expected(
        fixture["words"], fixture["mine"],
        fixture["local"], fixture["yomitan"])
    fixture["meta"]["strategy"] = picker_mod.get_active_strategy().name
    fixture["meta"]["refrozen_at"] = datetime.datetime.now().isoformat(
        timespec="seconds")
    with open(FIXTURE_PATH, "w", encoding="utf-8") as f:
        json.dump(fixture, f, ensure_ascii=False, indent=1)
    print(f"refrozen expectations in {FIXTURE_PATH}")


def main(argv: list = None) -> int:  # type: ignore[assignment]
    """CLI entry point (--capture and/or --html, --import-grades)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", action="store_true",
                        help="live capture -> frozen fixture")
    parser.add_argument("--html", action="store_true",
                        help="fixture -> human HTML report")
    parser.add_argument("--refreeze", action="store_true",
                        help="recompute frozen expectations with current code")
    parser.add_argument("--words-file", default="",
                        help="JSON word list (used when Anki is closed)")
    parser.add_argument("--import-grades", default="", metavar="FILE",
                        help="merge grades JSON into the fixture")
    args = parser.parse_args(argv)
    if args.import_grades:
        try:
            cmd_import_grades(args.import_grades)
        except Exception as e:  # noqa: BLE001 — debug tool fails loud
            print(f"audit_picker FAILED: {e}")
            return 1
        return 0
    if args.refreeze:
        try:
            cmd_refreeze()
        except Exception as e:  # noqa: BLE001 — debug tool fails loud
            print(f"audit_picker FAILED: {e}")
            return 1
        return 0
    if not args.capture and not args.html:
        parser.print_help()
        return 2
    try:
        fixture = (cmd_capture(args.words_file) if args.capture
                   else _load_fixture())
        if args.html:
            cmd_html(fixture)
    except Exception as e:  # noqa: BLE001 — debug tool fails loud, never silent
        print(f"audit_picker FAILED: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
