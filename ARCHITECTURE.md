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
- `scope.py`: Deck-based Scope — the single deck selection driving
  BOTH generation eligibility and knowledge. Deck names (subdecks
  included); a note is in scope when ANY of its cards sits in a scoped
  deck; empty scope is fail-closed.
- `gui.py`, `editor_browser.py`: Anki-specific UI and hook logic. The
  config GUI offers a compact Scope row + deck picker dialog; per-type
  field mappings ("targets") are kept only for types implied by the
  scoped decks. Generation paths resolve fields per note via
  `resolve_fields_for_note` plus the Scope gate. Editor toolbar button,
  Tab-to-Generate (legacy-editor unfocus hook), Browser bulk actions,
  and the Learner Knowledge dialog (non-modal, payload-cached) live
  here.
- `core.py`: Application wiring and singleton management.
- `engine.py`: Implements the Dictionary Ladder algorithm (early exit
  on first fully comprehensible definition, else argmax total score
  with most-kanji tie-break; order-independent).
- `scoring.py`: Interval-weighted kanji/vocab scoring (`ivl/365`
  capped at 1.0) and reference-title filtering.
- `provider.py`: Defines the `DictionaryProvider` interface and the
  current SQLite-backed implementation.
- `renderer.py`: Renders Yomitan structured content to HTML (ruby,
  `data-sc-*`, per-dictionary scoped CSS).
- `utils.py`: Shared text cleaning, dictionary discovery utilities,
  and the Qt-free config-merge helper (`merge_type_targets`).
- `anki.py`: Safe Anki database interaction for the learner-knowledge
  snapshot — first field of every in-scope note; mastered means a
  card interval ≥ 365 days, seen means any positive interval.
  Session-cached with generation counter; on-demand diagnostics and
  the snapshot spec live in `debug/`.
- `yomitan.py`, `yomitan_installer.py`: Optional Yomitan-API
  dictionary source — localhost bridge install/repair, keepalive
  against service-worker suspension, anti-zombie shutdown.
- `models.py`: Shared data structures (e.g., `DictionaryEntry`).

## Install-Time Indexing
Indexing happens exactly ONCE per dictionary during installation via the GUI. This builds a persistent SQLite index in `user_files/cache/dictionaries.db`. Lookups are pure SQL queries, ensuring the UI never freezes during generation.

## Future Extensibility
The `DictionaryProvider` interface allows replacing the `LocalSQLiteProvider` with a `YomitanApiProvider` (or any other source) without modifying the scoring or generation logic in `engine.py`.
