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
- `gui.py`: PyQt configuration dialog allowing users to map note types, target word/definition fields, and order their dictionary ladder with drag-and-drop or Move Up/Down buttons.
- `generator.py`: Core definition generation engine implementing ladder traversal, early exit, candidate filtering, and kanji comprehension scoring.
- `parser.py`: Independent dictionary loader (`SingleDictionary`) with dedicated per-dictionary pickle disk caches and Yomitan structured-content text extraction.
- `db_utils.py`: Safe, read-only Anki collection scanner (`mw.col.db`) using compiled regular expressions for fast known-kanji extraction.
- `editor_browser.py`: `aqt.gui_hooks` integration injecting the card editor button and browser bulk edit menu options.

For a detailed mathematical and algorithmic breakdown, see [WIKI.md](WIKI.md).
