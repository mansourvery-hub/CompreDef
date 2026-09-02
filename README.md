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
- **Auto-Matching Fields**: Automatically detects and maps your Target Word (`Expression`, `Word`) and `Definition` fields.
- **Card Editor Button**: One-click definition generation directly inside Anki's card editor toolbar.
- **Bulk Generation**: Generate definitions for hundreds of selected cards at once from the Anki Browser (via `Edit -> Generate CompreDef Definitions...` or `Ctrl+Shift+D`).
- **Independent Disk Caching**: Each dictionary is parsed once and cached in `user_files/cache/`, enabling instant (0.05s) warm lookups and instant reordering without re-parsing.

---

## Installation

1. Clone or download this repository into your Anki add-ons directory:
   ```bash
   ln -sfn /path/to/CompreDef ~/.local/share/Anki2/addons21/CompreDef
   ```
2. Restart Anki.
3. Configure your Note Type and Dictionaries under **Tools → Add-ons → CompreDef → Config**.

---

## Configuration

1. **Target Note Type**: Choose your Japanese note type (e.g., `Japanese`, `Mining`).
2. **Target Word Field**: Field containing the Japanese word to define (e.g., `Expression`).
3. **Definition Field**: Field to populate with the chosen definition.
4. **Dictionary Ladder**:
   - Click **Scan Folder...** to select a parent folder (e.g., `/path/to/Dicts/`) containing unzipped dictionaries.
   - Use **Move Up ↑** and **Move Down ↓** (or drag and drop) to position simpler dictionaries at the top and advanced dictionaries at the bottom.

---

## Architecture & Code Structure

```
CompreDef/
├── __init__.py         # Add-on entry point & hook registration
├── gui.py              # Configuration dialog with Ladder ordering
├── generator.py        # Ladder early-exit & kanji matrix scoring engine
├── parser.py           # SingleDictionary loader, Yomitan parser & disk cache
├── db_utils.py         # Native Anki database queries (interval > 0 kanji scan)
├── editor_browser.py   # Editor toolbar button and browser bulk menu hooks
├── icons/              # UI toolbar icons (compredef.svg)
├── config.json         # Default configuration settings
├── WIKI.md             # In-depth algorithm & architecture wiki
├── ARCHITECTURE.md     # Architectural goals and technical constraints
└── AGENTS.md           # Coding rules for development agents
```

---

## Development & Safety Rules

- **Native DB Access Only**: Never open `collection.anki2` with raw sqlite3. CompreDef strictly uses `mw.col.db` to prevent database locks.
- **Non-Blocking Concurrency**: All dictionary parsing and scoring operations execute in background threads using `mw.taskman.run_in_background()`.
- **PyQt Compatibility**: Imports use `aqt.qt` for multi-version Qt compatibility.

---

## License

GNU General Public License v3 or later.
