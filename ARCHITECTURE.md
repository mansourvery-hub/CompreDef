# CompreDef - Project Architecture

## Goal
Build an Anki 2.1+ Python add-on named "CompreDef" that automatically generates definitions for Japanese vocabulary cards strictly tailored to the user's known vocabulary and kanji levels.

## Core Philosophy: The Dictionary Ladder
Rather than relying on non-deterministic external LLMs or single-dictionary lookups, CompreDef uses an ordered **Dictionary Ladder** of local JSON dictionaries (Yomitan format) paired with **Kanji Matrix Scoring**:

1. **User-Configured Ladder**:
   The user orders their dictionaries from simplest (e.g. Children's / Elementary) to most advanced (e.g. Standard, Comprehensive Monolingual).

2. **Early Exit (Short-Circuit Evaluation)**:
   The generator evaluates candidate definitions dictionary-by-dictionary in user order. If a dictionary produces a definition where 100% of kanji are known to the user (present on Anki cards with `interval > 0`), the search terminates immediately and writes that definition. This delivers simpler definitions to beginners and avoids unnecessary processing.

3. **Maximal Definition Fallback**:
   If no dictionary yields a 100% known definition, the algorithm returns the maximal definition (the definition with the highest kanji comprehension score / least complicated) across all candidate definitions.

## Install-Time Indexing (THE core performance architecture)

Dictionary parsing and SQLite index building happen exactly ONCE per
dictionary, when the user installs/replaces it in the config GUI:

```
INSTALL DICTIONARY (config GUI: Add / Scan / Reinstall)
       ↓
   parse once (background thread, progress %, cancellable)
       ↓
 build SQLite index (streamed, bounded RAM)
       ↓
 save index persistently (marker row written only after ALL entries)
       ↓
      DONE
```

Then forever afterward:

```
GENERATE DEFINITION (editor button / browser menu — explicit actions only)
       ↓
 SQLite lookup (pure DB query — zero dictionary-file parsing)
       ↓
 combine definitions (ladder walk + kanji scoring)
       ↓
 return result
       ↓
 update note (persisted BEFORE editor refresh)
```

Hard rules enforced by `tests/test_regression.py`:
- `lookup()` NEVER calls indexing, never reads dictionary files, never
  touches the filesystem. It is a pure `SELECT` against `dictionaries.db`
  (~1ms; guarded by a test that makes any parse attempt raise).
- Indexes persist across Anki/machine restarts. A fresh process sees the
  dictionary as installed and queries it directly.
- The `dictionaries` marker row is written ONLY after every entry has been
  committed; partial/crashed installs leave no marker and are never trusted.
- Re-adding an unchanged dictionary is a no-op (signature check); replacing
  files on disk causes exactly ONE re-index when the user reinstalls.
- Indexing failures raise `IndexingError` and are reported — no silent
  partial indexes.
- Generation is triggered by explicit button/menu actions, plus opt-out
  Tab-to-Generate (auto-fill on word-field unfocus, empty definitions
  only). The old freeze/lost-definition failure modes are structurally
  fixed: generation is pure SQLite lookup, the unfocus hook returns
  `changed` untouched (no editor-reload race), and persistence happens
  BEFORE any editor refresh. See `editor_browser.py`.

## Key Modules

- `__init__.py`: Entry point registering config actions and UI hooks. Does no dictionary work at startup.
- `gui.py`: PyQt configuration dialog allowing users to map note types, target word/reading/definition fields, and order their dictionary ladder. Adding a dictionary triggers the one-time background indexing with a progress dialog; a "Reinstall / Update Index" button handles replaced dictionary files. List entries show install status (✓ entries / ⚠ not indexed).
- `generator.py`: Core definition generation engine implementing ladder traversal, early exit, candidate filtering, and kanji comprehension scoring. Extracts base kanji (stripping `<rt>` furigana) while preserving rich HTML with furigana for Anki insertion. Pure SQLite lookups only.
- `parser.py`: Dictionary installer + pure-SQL lookup. `SingleDictionary.install()` is the ONLY place dictionary files are parsed (streamed in bounded batches; a marker row records the source signature). `lookup()` is a pure database query. Supports unzipped folders and Yomitan `.zip` archives. Renders faithful Yomitan HTML with `<ruby>`, `data-sc-*` attributes, inline CSS, and `用例` blocks.
- `db_utils.py`: Safe, read-only Anki collection scanner (`mw.col.db`) using compiled regular expressions for fast known-kanji extraction.
- `editor_browser.py`: `aqt.gui_hooks` integration injecting the card editor toolbar button, browser bulk-edit menu options, and Tab-to-Generate (word-field unfocus auto-fills empty definitions; opt-out via `tab_generate` in config). Generation runs via `mw.taskman.run_in_background`; the note is persisted (`update_note`) BEFORE the editor refresh so definitions never disappear; failures are logged with note id/word/traceback and reported, never swallowed.

## Data Formats

### Yomitan Dictionary Structure
- **Folders**: Directories containing `term_bank_*.json` files and optionally `index.json`.
- **Zips**: `.zip` archives with same structure (term_bank_*.json, index.json) for zero-disk footprint.
- **Entries**: JSON arrays `[word, reading, syllabary, accent, definitions, notes, freq]` where definitions contain Yomitan structured-content.

### Yomitan HTML Output
ComprehDef renders 100% faithful Yomitan structured-content:
- Ruby furigana with `<ruby>`, `<rt>` tags preserved for Anki display.
- Data attributes: `data-sc-*` for accessibility and tooling.
- Inline CSS from Yomitan's `style` field (e.g., `fontSize: 14em` becomes `font-size: 14em`).
- Special tags: `<data-sc-name="用例">` for example sentences.
- Rich elements: `<span class="gloss-sc-span">`, `<div class="gloss-sc-div">`, table cells with `colSpan`/`rowSpan`.

For a detailed mathematical and algorithmic breakdown, see [WIKI.md](WIKI.md).
