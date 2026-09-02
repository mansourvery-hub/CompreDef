# CompreDef Wiki - Algorithm & Architecture Specification

## Overview

CompreDef is designed around Stephen Krashen’s **$i+1$ Comprehensible Input Hypothesis**: language acquisition occurs most effectively when learners are exposed to messages that are slightly beyond their current level, but still almost entirely comprehensible.

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
    FilterRefs --> ScoreDefs[Score each Definition in Dictionary<br>Score = Known Kanji / Total Kanji]
    
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

3. **Candidate Filtering**:
   - Short cross-reference headwords (e.g., `"会社更生法"`, `"参照"`) that do not contain sentence punctuation (`。`, `、`) are filtered out so that real explanatory sentences are always selected.

4. **Kanji Comprehension Scoring**:
   - For every candidate definition string $D$, let $K(D)$ be the multiset of kanji characters appearing in $D$.
   - The comprehension score $S(D)$ is calculated as:
     $$S(D) = \begin{cases} 1.0 & \text{if } |K(D)| = 0 \\ \frac{\sum_{c \in K(D)} [c \in \mathcal{K}_{\text{known}}]}{|K(D)|} & \text{if } |K(D)| > 0 \end{cases}$$
   - Pure hiragana/katakana definitions have $S(D) = 1.0$ (fully readable).

5. **Early Exit (Short-Circuit Evaluation)**:
   - If a definition yields $S(D) = 1.0$ (100% of the kanji are known):
     - **The loop immediately halts and returns that definition.**
     - Dictionaries further down the ladder are **never loaded or queried**.
     - **Benefits**:
       - *Pedagogical*: The learner receives the simplest definition from the easiest dictionary that can explain the concept.
       - *Computational*: Avoids unpickling, parsing, or scoring subsequent massive dictionaries, reducing execution time to **0.05 seconds**.

6. **The Maximal Fallback**:
   - If no dictionary in the entire ladder yields a 100% match, the algorithm returns the candidate definition with the highest comprehension score $S(D)$ found across all evaluated dictionaries.
   - This ensures the learner always receives the **least complicated definition available**.

---

## 2. Independent Per-Dictionary Disk Caching

Parsing unpacked Yomitan term banks (which often span 50–70 JSON files per dictionary) is CPU-intensive. To eliminate latency, CompreDef implements an independent disk cache per dictionary:

- **Cache Location**: `<addon_folder>/user_files/cache/dict_<hash>.pkl`
- **Cache Invalidation Signature**:
  A hash of all `term_bank_*.json` file names, modification timestamps (`st_mtime_ns`), and file sizes (`st_size`).
- **Benefits**:
  - Reordering dictionaries in the Ladder GUI takes **zero re-parsing time** because each dictionary's binary representation is cached independently.
  - An elementary dictionary with 5 term banks loads from disk in **0.05 seconds**.
  - Subsequent lookups are served directly from in-memory objects.

---

## 3. Database Safety Mandate

In strict accordance with Anki development standards:
- **No Direct SQLite Connections**: External sqlite3 connections to `collection.anki2` can cause database corruption and SQLite locks. CompreDef exclusively accesses the database through Anki's native Python wrapper: `mw.col.db.all()`.
- **Non-Blocking Background Threads**: All dictionary searches, scoring calculations, and database scans execute asynchronously using `mw.taskman.run_in_background()` with a completion callback to the main thread.
