# CompreDef - Project Architecture

## Goal
Build an Anki 2.1+ Python add-on named "CompreDef" that automatically generates definitions for Japanese vocabulary cards strictly tailored to the user's known vocabulary and kanji.

## Configuration GUI
The add-on must register a configuration action using `mw.addonManager.setConfigAction(__name__, function)`. 
The PyQt UI must allow the user to:
- Select the active Generation Mode (Mode A or Mode B).
- Define the target Anki Note Type.
- Map the "Target Word" and "Definition" fields.
- Specify the file path to their local JSON dictionary directories.

## Generation Mode A: The Dictionary Ladder with LLM Fallback
1. **The Ladder:** The system loads an ordered list of JSON dictionaries based on difficulty (Children's -> Standard -> Advanced).
2. **Parsing:** It parses the definition strings into discrete words using a MeCab or Sudachi wrapper.
3. **Filtering:** It queries the Anki SQLite database (cards with an interval > 0) to check if the user "knows" every parsed word.
4. **Selection:** It selects the first dictionary definition that yields a 100% known-word match.
5. **LLM Fallback:** If all local dictionaries fail, it makes an asynchronous HTTP request to an LLM API. The prompt must contain the simplest available definition and the list of unknown words, instructing the LLM to rewrite it using only known concepts.

## Generation Mode B: Local-Only Kanji Score Matrix (Inspired by "Kanji Grid")
This mode operates entirely offline without LLMs, referencing the database scanning logic found in the popular "Kanji Grid" add-on (ID 1610304449).
1. **Matrix Generation:** It generates a "Kanji Matrix/Cube" in memory by scanning the Anki database for all kanji present on cards with an `interval > 0`. 
2. **Definition Gathering:** For a queried target word, it retrieves all possible definitions across all loaded local JSON dictionaries.
3. **Scoring:** It scores each definition mathematically based on the ratio of known kanji to unknown kanji (penalizing definitions containing kanji missing from the user's matrix).
4. **Deterministic Selection:** It writes the definition with the highest kanji comprehension score to the Anki note.
