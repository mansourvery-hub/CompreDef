# CompreDef - Agent Coding Rules & Best Practices

## 1. Environment & Entry Point
- This is an Anki add-on running within Anki's embedded Python environment. 
- The main entry point must be `__init__.py`.

## 2. Database Interaction & The "Kanji Grid" Approach
- **NEVER** attempt to read the `collection.anki2` file using an external `sqlite3` connection. This causes database locks and corruption.
- Exclusively use Anki's native database wrapper: `mw.col.db.execute()` or `mw.col.db.all()`.
- When building the known-vocabulary list or the "Kanji Matrix" (for Mode B), reference the database logic used in the open-source "Kanji Grid" add-on (Anki Web ID: 1610304449) to safely parse fields and `interval` stats natively.

## 3. General Programming Practices & Clean Code
- **Commenting:** Write explicit inline comments explaining the *why* of the logic, especially for database queries, regex matching, and API calls. Include docstrings (`"""..."""`) for all functions and classes.
- **Modularity:** Do not stuff all logic into `__init__.py`. Separate concerns (e.g., `gui.py` for UI, `db_utils.py` for Anki database queries, `parser.py` for MeCab/Dictionary logic).
- **Type Hinting:** Use standard Python type hints (e.g., `def filter_words(words: list[str]) -> bool:`) to ensure code readability and prevent type errors.
- **Error Handling:** Wrap API calls and file reading operations in robust `try...except` blocks. Never let an uncaught exception crash Anki. 

## 4. Hook System & Concurrency
- **Hooks:** Do not overwrite Anki's core UI classes. Use `aqt.gui_hooks` (e.g., `editor_did_init`, `note_will_be_added`) to intercept card creation and inject UI seamlessly.
- **Non-Blocking UI:** Any intensive tasks (parsing massive JSON dictionaries, generating the Kanji Matrix, or making LLM API calls) MUST be executed in a background thread using Anki's `mw.taskman.run_in_background()` with a callback to update the UI on completion.

## 5. PyQt Compatibility
- Anki versions differ in their Qt backend. When importing UI components, always import from `aqt.qt` (e.g., `from aqt.qt import QDialog, QVBoxLayout, QPushButton`) rather than hardcoding `PyQt5` or `PyQt6`.

## 6. Regression Test Mandate
- **BEFORE committing and pushing**, run the fundamental regression suite and ensure it is fully green:
  `python3 tests/test_regression.py`
- Alternatively, run the CI script to test, commit, and push in one go:
  `./scripts/ci.sh`
- The suite guards the project's historical bugs (plain-text definitions replacing rich Yomitan HTML, furigana polluting kanji scores, ladder ordering, reference-title filtering, ZIP/folder parity, stale SQLite caches). Exit code 0 = safe to commit; any FAIL = fix the regression first.
- It runs on both system Python and Anki's bundled Python (no Anki/PyQt required — `aqt` is stubbed automatically). Real-dictionary smoke tests self-skip when the dictionaries are absent.
- If you intentionally change rendering behavior, bump `RENDERER_VERSION` in `parser.py` so users' SQLite caches invalidate cleanly, and update the affected test expectations in the same commit.

## 7. Git Remote & Repository Sync Mandate
- **ALWAYS** push all committed changes to the official GitHub remote (`https://github.com/mansourvery-hub/CompreDef`) as the final step of your work session:
  `git push origin master` (or `git push origin main`).
- **AnkiWeb**: Official AnkiWeb Add-on Page: [https://ankiweb.net/shared/info/1619602654](https://ankiweb.net/shared/info/1619602654). Releases are automatically uploaded via GitHub Actions.