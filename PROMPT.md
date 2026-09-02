Hello OpenCode. We are building a sophisticated Anki add-on called CompreDef. 

I have created two context files for you:
1. `ARCHITECTURE.md` - explains our dual-mode generation logic (Mode A: Dictionary Ladder + LLM, Mode B: Local Kanji Score Matrix based on the Kanji Grid add-on logic).
2. `AGENTS.md` - establishes strict Anki-specific coding rules, emphasizing clean code, modularity, explicit comments, and non-blocking background threads.

Please read both files completely and acknowledge their rules before writing any code.

Your first task is to set up the project boilerplate using best software engineering practices:
1. Create `__init__.py`, `config.json`, and `gui.py`. 
2. In `gui.py`, build a clean, commented PyQt configuration dialog (using `aqt.qt`) that allows the user to:
   - Input their target Note Type.
   - Map their Target Word and Definition fields.
   - Select between 'Mode A' and 'Mode B'.
   - Input a file path to their dictionary folder.
3. Link this dialog to Anki's addon manager inside `__init__.py` using `mw.addonManager.setConfigAction()`.

Do not implement the generation logic, MeCab parsing, or the Kanji Matrix database queries yet. I just want to see the configuration GUI working cleanly inside Anki with proper type hints and comments. 

Let me know when this is done, and please execute `git add` and `git commit -m "Initialize project boilerplate and config GUI"` to save this milestone.