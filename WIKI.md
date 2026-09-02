# CompreDef Wiki - Algorithm & Architecture Specification

## Overview

CompreDef is designed around Stephen Krashen's **$i+1$ Comprehensible Input Hypothesis**: language acquisition occurs most effectively when learners are exposed to messages that are slightly beyond their current level, but still almost entirely comprehensible.

Traditional Japanese-Japanese (国語) dictionaries (like 大辞林, 大辞泉, or 広辞苑) frequently define target words using obscure literary vocabulary or unlearned kanji. For a beginner or intermediate learner, looking up a word in such a dictionary creates an infinite lookup loop. Conversely, children's dictionaries (like 例解学習国語) provide simpler explanations using elementary kanji and grammar, but lack coverage of advanced terms.

CompreDef solves this deterministically through the **Dictionary Ladder with Early Exit & Kanji Matrix Scoring**.

---

## 1. The Algorithm

```mermaid
flowchart TD
    Start([User Requests Definition for Target Word]) --> ScanDB[Scan Anki DB for Known Kanji<br>Cards with interval > 0]
    ScanDB --> LoopDicts[Inspect Next Dictionary in User Ladder Order]

    LoopDicts --> LookupWord{Does Dictionary<br>contain Target Word?}
    LookupWord -- No --> HasMoreDicts{More Dictionaries<br>in Ladder?}

    LookupWord -- Yes --> FilterRefs[Filter out cross-reference titles]
    FilterRefs --> ScoreDefs[Score each Definition in Dictionary<br>Score = Known Base Kanji / Total Base Kanji]

    ScoreDefs --> Check100{Any Definition<br>has Score = 1.0?<br>100% Known Kanji}

    Check100 -- Yes --> EarlyExit([EARLY EXIT: Return Definition Immediately!<br>Skip all subsequent dictionaries])

    Check100 -- No --> TrackBest[Update Maximal Definition if<br>Score > Best Score so far]
    TrackBest --> HasMoreDicts

    HasMoreDicts -- Yes --> LoopDicts
    HasMoreDicts -- No --> ReturnMaximal([Return Maximal Definition<br>Least complicated candidate found])
```

### Step-by-Step Execution

1. **Known Kanji Extraction (The Kanji Matrix)**:
   - The add-on queries Anki's native database:
     ```sql
     SELECT DISTINCT notes.id, notes.flds
     FROM notes
     JOIN cards ON notes.id = cards.nid
     WHERE cards.ivl > 0
     ```
   - Only cards with `interval > 0` are analyzed (ensuring only reviewed/retained material is counted as "known").
   - A compiled C-level regular expression `[\u4e00-\u9fff]` extracts all kanji from the field blobs in **0.18 seconds** across 60,000+ card rows.
   - The result is a set $\mathcal{K}_{\text{known}}$ of all kanji the learner currently knows.

2. **The Dictionary Ladder Traversal**:
   - The user arranges their installed dictionaries in order from simplest to most advanced:
     1. **Children's / Elementary**: e.g., 小学館例解学習国語
     2. **Standard / High School**: e.g., 三省堂国語辞典
     3. **Comprehensive Monolingual**: e.g., 大辞泉, 大辞林
   - The generator iterates through this ladder **one dictionary at a time**.

3. **HTML Processing for Scoring**:
   - Before scoring, definitions are processed to extract **base text** for comprehension scoring:
     - Strip all `<rt>` (furigana) and `<rp>` tags.
     - Remove remaining HTML tags.
     - Unescape HTML entities.
   - This ensures only base kanji are counted for the Kanji Matrix Scoring, while the full rich HTML with furigana is preserved for Anki display.

4. **Candidate Filtering**:
   - Short cross-reference headwords (e.g., `"会社更生法"`, `"参照"`) that do not contain sentence punctuation (`。`, `、`) are filtered out so that real explanatory sentences are always selected.

5. **Kanji Comprehension Scoring**:
   - For every candidate definition HTML string $D$, let $K(D)$ be the multiset of kanji characters appearing in $D$ (base text only, no furigana).
   - The comprehension score $S(D)$ is calculated as:
     $$S(D) = \begin{cases} 1.0 & \text{if } |K(D)| = 0 \\ \frac{\sum_{c \in K(D)} [c \in \mathcal{K}_{\text{known}}]}{|K(D)|} & \text{if } |K(D)| > 0 \end{cases}$$
   - Pure hiragana/katakana definitions have $S(D) = 1.0$ (fully readable).

6. **Early Exit (Short-Circuit Evaluation)**:
   - If a definition yields $S(D) = 1.0$ (100% of the kanji are known):
     - **The loop immediately halts and returns that definition.**
     - Dictionaries further down the ladder are **never loaded or queried**.
     - **Benefits**:
       - *Pedagogical*: The learner receives the simplest definition from the easiest dictionary that can explain the concept.
       - *Computational*: Avoids unpickling, parsing, or scoring subsequent massive dictionaries, reducing execution time to **0.05 seconds**.

7. **The Maximal Fallback**:
   - If no dictionary in the entire ladder yields a 100% match, the algorithm returns the candidate definition with the highest comprehension score $S(D)$ found across all evaluated dictionaries.
   - This ensures the learner always receives the **least complicated definition available**.

---

## 2. SQLite-Cached Dictionary Indexing

CompreDef now uses **SQLite caching** for dictionary lookups instead of pickle files, providing faster B-tree lookups with zero RAM footprint.

### Dictionary Sources

CompreDef supports both:
- **Unzipped Directories**: Contains `term_bank_*.json` files and `index.json` metadata.
- **ZIP Archives**: Yomitan `.zip` dictionaries loaded directly from compressed archives (zero-disk footprint, instant access).

### SQLite Database Structure

The cache database (`user_files/cache/dictionaries.db`) contains:
- `dictionaries` table: Stores dictionary path, title, signature, and entry count.
- `entries` table: Maps `(dict_path, term)` to rich HTML definitions.

### Cache Invalidation

- **Signature**: Computed from dictionary source (file modification times and sizes for folders, mtime + size for ZIPs).
- **Update Trigger**: When dictionary files change, the signature updates, triggering re-indexing.
- **Performance**: First lookup (~0.5s) indexes the dictionary; subsequent lookups are **instant (0.08ms)** via indexed B-tree queries.

### Yomitan HTML Rendering

CompreDef renders 100% faithful Yomitan structured-content:
- **Ruby Furigana**: `<ruby>` tags with `<rt>` readings preserved for Anki display.
- **Data Attributes**: `data-sc-*` attributes for accessibility and tooling.
- **Inline CSS**: Yomitan's `style` field converted to inline CSS (e.g., `fontSize: 14em` becomes `font-size: 14em`).
- **Special Tags**: `<data-sc-name="用例">` for example sentences.
- **Rich Elements**: `<span class="gloss-sc-span">`, `<div class="gloss-sc-div">`, table cells with `colSpan`/`rowSpan`.
- **Output**: Full HTML with all styling and semantics for direct insertion into Anki note fields.

---

## 3. Database Safety Mandate

In strict accordance with Anki development standards:
- **No Direct SQLite Connections**: External sqlite3 connections to `collection.anki2` can cause database corruption and SQLite locks. CompreDef exclusively accesses the database through Anki's native Python wrapper: `mw.col.db.all()`.
- **Non-Blocking Background Threads**: All dictionary searches, scoring calculations, and database scans execute asynchronously using `mw.taskman.run_in_background()` with a completion callback to the main thread.
