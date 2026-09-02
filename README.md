# CompreDef

Anki add-on for automatically generating Japanese vocabulary definitions tailored to your known vocabulary and kanji levels.

## Features

### Mode A: Dictionary Ladder with LLM Fallback
1. **The Ladder**: Loads JSON dictionaries in order (Children's → Standard → Advanced)
2. **Parsing**: Parses definitions into discrete words using MeCab/Sudachi
3. **Filtering**: Checks your Anki database (cards with interval > 0) to see if you know every word
4. **Selection**: Picks the first dictionary definition with 100% known-word match
5. **LLM Fallback**: If all local dictionaries fail, uses an LLM to rewrite the definition using only known concepts

### Mode B: Local-Only Kanji Score Matrix
- Operates entirely offline without LLMs
- Generates a "Kanji Matrix" by scanning your Anki database for kanji on cards with interval > 0
- Scores definitions based on known kanji ratio
- Selects the definition with the highest kanji comprehension score

### UI & Integration
- **Configuration Dialog**: Intuitive UI to select Note Type, map Word/Definition fields, and choose generation mode
- **Editor Button**: Toolbar button to generate definitions for current note
- **Bulk Operations**: Generate definitions for multiple selected notes via Browser menu

## Installation

1. Download or clone this repository
2. Symlink the folder to your Anki add-ons directory:
   ```bash
   ln -sfn /path/to/CompreDef ~/.local/share/Anki2/addons21/CompreDef
   ```
3. Restart Anki
4. Go to **Tools → Add-ons → CompreDef → Config** to configure

## Configuration

### Target Note Type
Select the Anki note type you want to target (e.g., "Japanese", "Cloze")

### Field Mappings
- **Target Word Field**: The field containing the Japanese word (e.g., "Expression", "Word")
- **Definition Field**: The field to populate with generated definitions

Auto-matching is enabled: if fields are named clearly (e.g., "definition", "word", "expression"), they will be automatically selected.

### Generation Mode
- **Mode A**: Dictionary Ladder + LLM Fallback
- **Mode B**: Local Kanji Score Matrix

### Dictionary Folder
Path to your local JSON dictionary directory (optional for Mode B, required for Mode A)

## Requirements

- Anki 2.1+
- Python 3.10+
- Optional: MeCab or Sudachi for parsing

## Architecture

### Project Structure
```
CompreDef/
├── __init__.py         # Add-on entry point
├── gui.py              # Configuration dialog
├── editor_browser.py   # Editor button and bulk operations
├── icons/              # UI icons (SVG)
├── config.json         # Default configuration
├── AGENTS.md           # Agent coding rules
├── ARCHITECTURE.md     # Project architecture
└── README.md           # This file
```

### Key Modules

- **gui.py**: PyQt configuration dialog with cascading dropdowns and auto-matching
- **editor_browser.py**: Hook system integration for editor toolbar and browser menus
- **config.json**: Anki-managed configuration storage

## Development

### Agent Coding Rules (AGENTS.md)
- Never use external `sqlite3` on `collection.anki2` - use `mw.col.db`
- All database queries via Anki's native wrapper
- Use `aqt.gui_hooks` for UI integration (never overwrite core classes)
- Execute intensive tasks in background via `mw.taskman.run_in_background()`
- Import Qt components from `aqt.qt` only
- Type hints and docstrings required

### Git Workflow
As per AGENTS.md, all changes are automatically pushed to GitHub after each edit:
```bash
git push origin master
```

## License

This project follows the licensing terms of the Anki ecosystem.

## Credits

Inspired by the "Kanji Grid" add-on (Anki Web ID: 1610304449) for database scanning logic.
