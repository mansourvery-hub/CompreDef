# CompreDef - Project Architecture

## Goal
Build an Anki 2.1+ Python add-on named "CompreDef" that automatically generates definitions for Japanese vocabulary cards strictly tailored to the user's known vocabulary and kanji levels.

## Core Philosophy: The Dictionary Ladder
CompreDef uses an ordered **Dictionary Ladder** of local JSON dictionaries paired with **Kanji Matrix Scoring**:

1. **User-Configured Ladder**: Dictionaries are tried top to bottom; the first whose definition passes the comprehension gate wins.
2. **Early Exit**: If a definition has 100% known kanji, search terminates immediately.
3. **Maximal Fallback**: If no 100% match is found, the definition with the highest comprehension score is returned.

## Architecture Structure

The system is organized into a layered architecture to separate data access, scoring, and orchestration:

```text
UI / Anki Integration (gui.py, editor_browser.py)
        ↓
Orchestration (core.py, engine.py)
        ↓
Scoring & Filtering (scoring.py)
        ↓
Dictionary Provider Interface (provider.py -> DictionaryProvider)
        ↓
Implementation (provider.py -> LocalSQLiteProvider)
```

### Key Modules
- `gui.py`, `editor_browser.py`: Anki-specific UI and hook logic.
- `core.py`: Application wiring and singleton management.
- `engine.py`: Implements the Dictionary Ladder algorithm.
- `scoring.py`: Kanji comprehension scoring and reference filtering.
- `provider.py`: Defines the `DictionaryProvider` interface and the current SQLite-backed implementation.
- `renderer.py`: Renders Yomitan structured content to HTML.
- `utils.py`: Shared text cleaning and dictionary discovery utilities.
- `anki.py`: Safe Anki database interaction for known-kanji extraction.
- `models.py`: Shared data structures (e.g., `DictionaryEntry`).

## Install-Time Indexing
Indexing happens exactly ONCE per dictionary during installation via the GUI. This builds a persistent SQLite index in `user_files/cache/dictionaries.db`. Lookups are pure SQL queries, ensuring the UI never freezes during generation.

## Future Extensibility
The `DictionaryProvider` interface allows replacing the `LocalSQLiteProvider` with a `YomitanApiProvider` (or any other source) without modifying the scoring or generation logic in `engine.py`.
