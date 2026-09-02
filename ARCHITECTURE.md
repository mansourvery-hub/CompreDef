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

## Key Modules

- `__init__.py`: Entry point registering config actions and UI hooks.
- `gui.py`: PyQt configuration dialog allowing users to map note types, target word/definition fields, order their dictionary ladder (add folders or zip archives), and configure Anki field mappings.
- `generator.py`: Core definition generation engine implementing ladder traversal, early exit, candidate filtering, and kanji comprehension scoring. Extracts base kanji (stripping `<rt>` furigana) while preserving rich HTML with furigana for Anki insertion.
- `parser.py`: Independent dictionary loader with per-dictionary indexing. Supports both unzipped folders and Yomitan `.zip` archives containing `term_bank_*.json` files. Renders 100% faithful Yomitan HTML with `<ruby>`, `data-sc-*` attributes, inline CSS, and `用例` blocks. Uses SQLite (`dictionaries.db`) for fast B-tree lookups (~0.08ms) with signature-based cache invalidation.
- `db_utils.py`: Safe, read-only Anki collection scanner (`mw.col.db`) using compiled regular expressions for fast known-kanji extraction.
- `editor_browser.py`: `aqt.gui_hooks` integration injecting the card editor button and browser bulk edit menu options.

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
