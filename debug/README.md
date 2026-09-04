# CompreDef Debugging Section

On-demand diagnostics for the learner-knowledge snapshot (known kanji /
known vocabulary). This directory is **not** part of the regression suite
and is **never** executed by CI, `build.sh`, or `release.sh`.

## When to come here

| Symptom | Start with |
|---|---|
| `known kanji: 0` (or suspiciously low) in the Debug Console | Use case U1 in `console_snippets.md`, then S1–S4 in `sanity_knowledge.py` |
| Definitions seem too hard / too easy for your level | Use case U2, then S2–S3 |
| "Where did this kanji come from?" | Use case U3 |
| A DB-related change was just made to `anki.py` | Full `sanity_knowledge.py` run |
| Anki itself upgraded major versions | S1 + the regression suite |

## Contents

- **`sanity_knowledge.py`** — standalone checks, system Python, Anki
  stubbed. `python3 debug/sanity_knowledge.py`. Exit 0 = sane.
- **`console_snippets.md`** — copy-paste recipes for Anki's
  Help → Debug Console against the **live** collection.
- This file — specifications and use cases.

## Specifications

The learner-knowledge snapshot MUST satisfy:

- **SP1 — First-field-only extraction, all note types.** Learner
  proficiency is collection-wide: for every mature note of ANY type,
  only the FIRST field (conventionally the word / expression / front)
  contributes kanji and vocabulary. Definition, Example, Reading, and
  all other fields contribute nothing. Rationale: generated definitions
  are written to non-first fields; scanning them would let CompreDef's
  own output mark unknown kanji known. History: up to v1.0.9 the
  snapshot was gated on a single configured note type, which silently
  discarded ~99% of one user's collection.
- **SP2 — No note-type gating.** Notes are never filtered by model. The
  generation dialogs keep their own single-type targeting; knowledge
  does not.
- **SP3 — Schema-proof data access.** The collection may only be read
  via `mw.col.db.all()` plus the public `mw.col.models` API. Raw SQL
  must never name Anki's internal tables except `notes` and `cards`
  (stable across versions). In particular, never `JOIN`/`FROM` the
  legacy `models` table — renamed to `notetypes` in Anki 23.10+.
  Incident: v1.0.5 did exactly this and every modern-Anki user silently
  got 0 known kanji.
- **SP4 — Session snapshot.** The snapshot builds once per session
  (async at startup), is reused for all generations, and is never
  rebuilt by note edits or definition writes. Manual `reset_caches()`
  triggers exactly one rebuild.
- **SP5 — Visible failure.** A knowledge-build failure must surface
  (console log + one tooltip per session), never an unexplained empty
  set. An empty set with no warning is treated as a bug, not as
  "beginner collection".
- **SP6 — Graceful degradation.** Missing config, unknown note type or
  field, unknown `mid`, and malformed rows yield an empty set without
  raising.

## Use cases

- **U1 — Triage "0 known kanji".** Distinguish: no mature cards vs.
  config mismatch vs. broken query. (Console recipe; S1+S4 locally.)
- **U2 — Level plausibility.** Counts, samples, and membership probes
  against the learner's real level. (Console recipe; S2 locally.)
- **U3 — Kanji provenance.** Given a kanji, find the mature note(s)
  and field(s) it came from. (Console recipe.)
- **U4 — Force a clean rebuild.** Reset the snapshot and verify the
  counts change as expected. (Console recipe; S6 locally.)
- **U5 — Pre-change confidence.** After touching `anki.py`, run the
  sanity script plus the regression suite before committing.

## Check ↔ spec map

| Sanity check | Specs covered |
|---|---|
| S1 no legacy `models`-table SQL (AST scan of shipped sources) | SP3 |
| S2 first-field-only, single layout | SP1 |
| S3 mixed layouts across types; non-first kanji excluded | SP1, SP2 |
| S4 empty/malformed rows skipped; works with empty config | SP6 |
| S5 DB failure warns visibly, exactly once | SP5 |
| S6 one scan per session; reset rebuilds once | SP4 |

Behavioral twins of S2/S3/S6 also live in `tests/test_regression.py`
(`test_kanji_extraction_correctness`,
`test_knowledge_survives_new_schema`) so CI guards them on every
commit; this directory keeps the on-demand, exploratory, and
live-collection side.

## Adding a check

1. Add an `sN_...()` function using the local `check()` helper.
2. Keep it hermetic: start with `fresh_state()` (resets snapshot,
   fake models/DB/config/tooltips).
3. Reference the spec ID it guards (SP1–SP6) in the docstring.
4. If the behavior must hold on every commit, mirror it as a
   regression test instead of (or in addition to) here.
