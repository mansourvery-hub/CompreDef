# CompreDef

Anki add-on for automatically generating Japanese vocabulary definitions strictly tailored to your known vocabulary and kanji levels.

## Core Concept: The Dictionary Ladder

CompreDef eliminates the circular lookup trap of Japanese monolingual dictionaries by searching your dictionaries in an **ordered ladder** from simplest to most advanced:

1. **Children's / Elementary** (e.g. 小学館例解学習国語)
2. **Intermediate / High School** (e.g. 三省堂国語辞典)
3. **Advanced / Comprehensive** (e.g. 大辞泉, 大辞林)

### How it Works
- **Early Exit**: CompreDef checks dictionaries in the order you specify. If a dictionary provides a definition containing only kanji you already know (cards with interval > 0 in Anki), **the search stops immediately** and writes that definition to your note. This gives beginners simple definitions and saves massive CPU time.
- **Maximal Definition Fallback**: If no definition has 100% known kanji, CompreDef selects the definition with the highest kanji comprehension score (the least complicated definition available).
- **Zero LLM Dependency**: Operates completely offline, locally, and deterministically.

> For an in-depth mathematical specification and flowcharts, see the [Algorithm Specification Wiki (WIKI.md)](WIKI.md).

---

## Features

- **Dictionary Ladder GUI**: Add, remove, and reorder dictionaries using Move Up/Down buttons or native Drag-and-Drop.
- **Folder Auto-Scanner**: Point CompreDef at a folder containing multiple unzipped Yomitan dictionaries to auto-detect and add all of them.
- **ZIP Archive Support**: Add Yomitan dictionaries directly as `.zip` files for zero-disk footprint and instant loading.
- **Auto-Matching Fields**: Automatically detects and maps your Target Word (`Expression`, `Word`) and `Definition` fields.
- **Card Editor Button**: One-click definition generation directly inside Anki's card editor toolbar.
- **Bulk Generation**: Generate definitions for hundreds of selected cards at once from the Anki Browser (via `Edit -> Generate CompreDef Definitions...` or `Ctrl+Shift+D`).
- **Independent Disk Caching**: Each dictionary is parsed once and cached in `user_files/cache/dictionaries.db`, enabling instant (0.08ms) B-tree lookups and instant reordering without re-parsing.
- **100% Faithful Yomitan HTML**: Renders rich Yomitan structured-content with `<ruby>`, `data-sc-*` attributes, inline CSS, and `用例` blocks directly into Anki notes.

---

## Installation

1. Download or build `CompreDef.ankiaddon` (see **Testing & Local Installation** below).
2. In Anki: **Tools → Add-ons → Install from file...** → select it → restart Anki.
3. Configure your Note Type and Dictionaries under **Tools → Add-ons → CompreDef → Config**.

---

## Configuration

1. **Target Note Type**: Choose your Japanese note type (e.g., `Japanese`, `Mining`).
2. **Target Word Field**: Field containing the Japanese word to define (e.g., `Expression`).
3. **Definition Field**: Field to populate with the chosen definition.
4. **Dictionary Ladder**:
   - Click **Add Zip Archive...** to select a Yomitan `.zip` file.
   - Click **Add Folder...** to select an unzipped dictionary folder.
   - Click **Scan Folder...** to select a parent folder containing multiple dictionaries (both `.zip` files and subfolders).
   - Use **Move Up ↑** and **Move Down ↓** (or drag and drop) to position simpler dictionaries at the top and advanced dictionaries at the bottom.

---

## Testing & Local Installation

Run the regression suite (no Anki/PyQt needed — the Anki API is stubbed automatically; real-dictionary smoke tests self-skip if the dictionaries are absent):

```bash
python3 tests/test_regression.py
```

Build the installable package to test the current code in Anki:

```bash
./scripts/build.sh        # → dist/CompreDef.ankiaddon
```

Then install it: **Anki → Tools → Add-ons → Install from file...** → select `dist/CompreDef.ankiaddon` → restart Anki.

Prefer a live checkout while developing? Symlink the repo instead of installing the package:

```bash
ln -sfn /path/to/CompreDef ~/.local/share/Anki2/addons21/CompreDef
```

For test + commit + push in one go:
```bash
./scripts/ci.sh
```
For a full release (tests, package, tag, GitHub Release with the `.ankiaddon`):
```bash
./scripts/release.sh [vX.Y.Z]
```
 
The regression suite verifies (among others) that definitions stay **rich Yomitan HTML** (never plain text), furigana readings never pollute kanji scoring, the dictionary ladder exits early on the simplest comprehensible dictionary, cross-reference titles lose to real definitions, `.zip` archives produce byte-identical output to their unzipped folders, and Tab-to-Generate never overwrites an existing definition.

---

## Architecture & Code Structure

```
CompreDef/
├── __init__.py         # Add-on entry point & hook registration
├── gui.py              # Configuration dialog with Ladder ordering
├── generator.py        # Ladder early-exit & kanji matrix scoring engine
├── parser.py           # SingleDictionary loader, Yomitan parser, ZIP support, SQLite caching
├── db_utils.py         # Native Anki database queries (interval >= 21 kanji scan)
├── editor_browser.py   # Editor toolbar button and browser bulk menu hooks
├── tests/
│   └── test_regression.py  # Fundamental regression suite (run before committing)
├── icons/              # UI toolbar icons (compredef.svg)
├── config.json         # Default configuration settings
├── WIKI.md             # In-depth algorithm & architecture wiki
├── ARCHITECTURE.md     # Architectural goals and technical constraints
└── AGENTS.md           # Coding rules for development agents
```

---

## Development & Safety Rules
 
- **CI/CD**: Use `./scripts/ci.sh` for testing and pushing, and `./scripts/release.sh` for tagged releases.
- **Native DB Access Only**: Never open `collection.anki2` with raw sqlite3. CompreDef strictly uses `mw.col.db` to prevent database locks.
- **Non-Blocking Concurrency**: All dictionary parsing and scoring operations execute in background threads using `mw.taskman.run_in_background()`.
- **PyQt Compatibility**: Imports use `aqt.qt` for multi-version Qt compatibility.
- **SQLite Caching**: Dictionary lookups use indexed B-tree tables in `user_files/cache/dictionaries.db` for ~0.08ms performance with 0MB RAM footprint.

---

## License

GNU General Public License v3 or later.
