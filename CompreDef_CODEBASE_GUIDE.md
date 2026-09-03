# CompreDef — Codebase & Architecture Guide

> A pedagogical guide to understanding what CompreDef does, how the current code works, why some parts are expensive, and what the important architectural ideas mean.
>
> This document describes the **current implementation conceptually**. It is intended for a developer who has been changing the project without yet having a complete mental model of it.

---

## 1. What CompreDef is, in one sentence

**CompreDef is a learner-aware definition selector for Anki.**

It does not primarily try to be a dictionary. Its job is to take the definitions available for a Japanese word and choose the one that is most understandable for the particular learner.

The high-level problem is:

```text
Anki card

Expression: 独立
Definition: [empty]

        ↓

What dictionary definition can THIS learner understand?

        ↓

Look at the learner's known Japanese
        ↓

Look through the configured dictionaries
        ↓

Score candidate definitions
        ↓

Choose the best candidate
        ↓

Write it into the Anki note
```

That is the core product idea.

Everything else exists to support this pipeline.

---

# 2. The whole system at a glance

The current implementation can be understood as these major pieces:

```text
                         ANKI
                           │
                ┌──────────┴──────────┐
                │                     │
          learner knowledge       user interaction
                │                     │
                ▼                     ▼
          db_utils.py        editor_browser.py / gui.py
                │                     │
                │                     │
                └──────────┬──────────┘
                           │
                           ▼
                     generator.py
                    (selection logic)
                           │
                           ▼
                      parser.py
                (current dictionary backend)
                           │
                  ┌────────┴────────┐
                  │                 │
             Yomitan data      SQLite index
                  │                 │
                  └────────┬────────┘
                           │
                           ▼
                    selected definition
                           │
                           ▼
                         ANKI
```

The roles are roughly:

- **`db_utils.py`**: builds the learner-knowledge picture.
- **`generator.py`**: decides which definition wins.
- **`parser.py`**: currently owns most of the dictionary machinery: reading Yomitan dictionaries, indexing, lookup, and rendering.
- **`editor_browser.py`**: connects generation to actual Anki editor/browser actions.
- **`gui.py`**: configuration and dictionary management UI.
- **`__init__.py` / addon lifecycle code**: hooks CompreDef into Anki.

The most important distinction is:

> **Dictionary retrieval and definition selection are different jobs.**

The current code combines too much of the first job inside `parser.py`, while the interesting CompreDef-specific logic mostly lives in `generator.py`.

---

# 3. What happens when a user generates a definition?

Take a simple example:

```text
Expression: 抱える
Reading:    かかえる
Definition: [empty]
```

The runtime flow is approximately:

```text
1. User clicks Generate / triggers Tab-to-Generate
             ↓
2. Anki integration gets the word + reading
             ↓
3. generator.py is called
             ↓
4. learner knowledge is obtained
             ↓
5. configured dictionaries are queried
             ↓
6. candidate definitions are scored
             ↓
7. best candidate is returned
             ↓
8. Anki integration writes it to the note
```

The selection logic does **not** need to know how the dictionary was stored internally. In a future architecture, this is the boundary where a different dictionary provider can be plugged in.

---

# 4. `db_utils.py`: What does the learner know?

CompreDef needs some approximation of the learner's Japanese knowledge.

The current implementation primarily builds a set of **known kanji** from sufficiently mature Anki material.

Conceptually:

```text
Anki collection
      ↓
find sufficiently mature cards/notes
      ↓
extract Japanese text
      ↓
extract kanji
      ↓
known_kanji = {日, 本, 学, 生, ...}
```

There is also a known-vocabulary cache in the code, but the current definition-selection algorithm relies primarily on the known-kanji set.

## Why is this useful?

Suppose the learner knows:

```text
日 本 学 生 自 分 人
```

A definition containing mostly those kanji is treated as easier than one containing many unfamiliar kanji.

This is only a **proxy for comprehension**, not a full model of Japanese ability.

For example, a learner may know all the individual kanji in:

```text
達成
```

without actually knowing the vocabulary `達成`.

That is one of the biggest conceptual limitations of the current scoring system and a likely area for future improvement.

---

# 5. Learner knowledge is a snapshot, not real-time state

The known-kanji/known-vocabulary data should be thought of as a **learner proficiency snapshot**.

A reasonable mental model is:

```text
Anki starts
    ↓
build learner knowledge once
    ↓
keep it in memory
    ↓
reuse it for many definition generations
```

This is different from dictionary indexing.

### Dictionary knowledge

```text
Dictionary installed/re-indexed
    ↓
expensive one-time processing
    ↓
local searchable index
```

### Learner knowledge

```text
Anki session / explicit refresh
    ↓
scan learner data
    ↓
known-kanji / known-vocabulary snapshot
    ↓
reuse it
```

The reason to avoid recalculating it after every note modification is conceptual as well as practical: a learner's Japanese level does not meaningfully change every few seconds. It changes over days, weeks, and months.

A future implementation could build this snapshot in the background at Anki startup and provide a manual refresh operation when desired.

---

# 6. `generator.py`: the brain of CompreDef

`generator.py` is where the project-specific selection logic lives.

Its job can be simplified to:

```text
INPUT
    word
    reading
    dictionaries
    learner knowledge

        ↓

retrieve candidate definitions

        ↓

score candidates

        ↓

select winner

        ↓

return result
```

## Current scoring idea

For a candidate definition, CompreDef extracts its base text and looks at the kanji it contains.

For example:

```text
彼は重要な問題を抱えている。
```

The relevant kanji might be approximately:

```text
彼 重要 問題 抱
```

If the learner knows 4 of 5 relevant kanji:

```text
score = 4 / 5 = 0.80
```

The current code treats a candidate with no kanji as fully comprehensible for this specific metric, because there are no unknown kanji to penalize.

This produces a simple, deterministic score.

---

# 7. The dictionary ladder

This is one of the central ideas of CompreDef.

The user chooses dictionaries in priority order:

```text
1. Dictionary A
2. Dictionary B
3. Dictionary C
```

CompreDef roughly does:

```text
Dictionary A
    ↓
get candidates
    ↓
any perfect candidate?
    ├── yes → stop
    └── no
          ↓
Dictionary B
    ↓
get candidates
    ↓
any perfect candidate?
    ├── yes → stop
    └── no
          ↓
Dictionary C
```

If no candidate reaches 100% comprehensibility, the highest-scoring candidate encountered is used as the fallback.

So there are two principles:

1. **Dictionary order matters.**
2. **A sufficiently understandable definition can terminate the search early.**

This is not the same as asking for the objectively best dictionary definition. It is asking:

> “What is the most useful definition for this learner, while respecting the user's dictionary preferences?”

---

# 8. `parser.py`: why it is the complicated part

`parser.py` is currently responsible for many different concepts that ideally would be separate.

Conceptually it does all of this:

```text
Yomitan dictionary source
        ↓
open folder / ZIP
        ↓
read term-bank JSON
        ↓
interpret dictionary records
        ↓
render structured content to HTML
        ↓
store processed entries in SQLite
        ↓
provide fast lookup later
```

This is why the module is much more complex than `generator.py`.

There are really two different workflows hidden inside it.

## A. Dictionary installation / indexing

This is the expensive path:

```text
large dictionary
      ↓
read many records
      ↓
parse them
      ↓
render/process definitions
      ↓
insert into SQLite
      ↓
build database indexes
      ↓
done
```

The goal is to pay this cost once.

## B. Definition lookup

After indexing:

```text
word
 ↓
SQLite lookup
 ↓
candidate rows
```

This should be very fast.

The important architectural achievement of the current project is that **dictionary parsing is not supposed to happen on every definition lookup**.

---

# 9. What is JSON? Why is processing it expensive?

JSON is fundamentally **text**.

A dictionary file can look conceptually like:

```json
[
  ["人", "ひと", "noun", "...definition..."],
  ["人", "じん", "suffix", "...definition..."],
  ["人", "にん", "counter", "...definition..."]
]
```

There is nothing magical about the format. The problem is its size.

A Yomitan dictionary is not necessarily one small JSON document. It can contain very large term-bank files with enormous numbers of records.

When Python parses a large JSON file, it is doing more than looking at characters. It turns text like:

```text
["人", "ひと", "..."]
```

into Python objects such as lists and strings that the program can manipulate.

Then CompreDef processes those records, potentially renders structured content, and stores the result.

So a large dictionary can represent substantial CPU, memory, disk I/O, and database work during installation.

---

# 10. What is indexing?

This is worth understanding because “indexing” is one of the most important ideas in the project.

**Indexing means creating extra data structures that make future lookups fast.**

It does **not** simply mean “sorting the JSON.”

Imagine a million dictionary entries.

Without an index, a naive search looks like:

```text
entry 1?  no
entry 2?  no
entry 3?  no
...
entry 847391?  yes
```

That is roughly an **O(n)** search: in the worst case, you inspect essentially everything.

An index is extra information designed to avoid this.

---

# 11. Hash tables and indexes

A **hash table** is one possible way to implement a lookup structure.

Conceptually:

```text
hash("人") → bucket/location
hash("猫") → bucket/location
hash("犬") → bucket/location
```

Then:

```text
find("人")
    ↓
calculate hash("人")
    ↓
jump to relevant bucket
    ↓
find 人
```

Hash tables can provide approximately **O(1) average lookup** for exact-key lookups.

But “index” is the broader concept.

```text
Index
├── hash-based index
├── B-tree index
├── other tree/bitmap/spatial structures
└── etc.
```

An **index is the purpose**; a hash table or B-tree is one possible implementation.

---

# 12. Why SQLite indexes are useful

SQLite commonly uses **B-tree structures** for ordinary indexes.

A B-tree keeps keys organized so the database can quickly navigate to the relevant region rather than scanning every row.

Very simplified:

```text
                 [M]
               /     \
           [G-H]      [T-Z]
           /   \      /   \
         ...   人   ...   ...
```

To find `人`, the database follows a small number of branches instead of reading every row.

That is roughly **O(log n)** rather than **O(n)**.

The important practical idea is:

> The database creates extra lookup structures so repeated searches do not require repeated full scans.

---

# 13. The three layers: data, database, index

These are easy to confuse.

### Data

The actual dictionary entries:

```text
人 → definition A
人 → definition B
猫 → definition C
```

### Database

A structured place to store those entries, such as SQLite tables:

```text
entries
--------------------------------
term | reading | definition | ...
人   | ひと    | ...        |
人   | じん    | ...        |
猫   | ねこ    | ...        |
```

### Index

Extra structures attached to the database to make searches faster:

```text
index(term)

犬 → relevant row(s)
人 → relevant row(s)
猫 → relevant row(s)
```

So when we say:

> “CompreDef indexes the dictionary”

what we really mean is closer to:

> “CompreDef reads the dictionary, loads its entries into a searchable local database, and lets SQLite maintain indexes that make later lookups fast.”

---

# 14. Why the current system has an expensive installation step

Suppose the dictionary is 1.3 GB.

During indexing, CompreDef essentially has to process a huge amount of source data:

```text
1.3 GB dictionary
     ↓
read records
     ↓
parse JSON
     ↓
interpret structured content
     ↓
render/process data
     ↓
insert rows into SQLite
     ↓
build indexes
```

That is expensive because the computer has to **look at a large amount of data once** to build the structures that future lookups can use.

Once that work is done:

```text
lookup("人")
     ↓
SQLite index
     ↓
relevant rows
```

is very fast.

This is the fundamental tradeoff:

```text
BUILD INDEX
    expensive, roughly proportional to dataset size

LOOKUP
    cheap, because the index avoids scanning everything
```

---

# 15. A simple real-world analogy for indexing

Imagine a one-million-page book.

### Without an index

You want information about “quantum mechanics”.

You start at page 1 and search forward.

### With an index

At the back of the book:

```text
Quantum mechanics → page 847
```

You can jump directly to the relevant location.

The book itself was not necessarily rearranged.

You created **extra lookup information**.

A database index is the same idea, implemented with a data structure rather than a printed alphabetical list.

---

# 16. Why JSON being sorted is a separate issue

It is possible to make search faster with a sorted dataset as well.

For example:

```text
apple
banana
cat
dog
elephant
...
```

If sorted, a program can use **binary search**:

```text
look at middle
    ↓
wrong half? discard it
    ↓
look at middle of remaining half
    ↓
repeat
```

For a million sorted entries, you need on the order of a few dozen comparisons rather than a million.

But Yomitan dictionaries are more complicated than a simple sorted list. There can be multiple entries for the same term, readings, definitions, metadata, sequences, etc.

SQLite is useful because it can represent the dataset and provide appropriate lookup structures without requiring CompreDef to reinvent all of that itself.

---

# 17. Why rendering is a separate difficulty

Finding an entry and displaying it correctly are different problems.

Yomitan dictionary content is not necessarily just plain HTML like:

```html
<p>A person...</p>
```

Dictionary entries can contain structured content such as:

```text
text
ruby
lists
links
images
styles
nested content
examples
```

The current CompreDef implementation has its own code for interpreting and rendering this information.

Conceptually:

```text
Yomitan structured content
        ↓
CompreDef renderer
        ↓
HTML for Anki
```

This is exactly where many of the historical bugs can come from, because CompreDef is recreating part of Yomitan's functionality itself.

---

# 18. Why the current `parser.py` architecture matters

The current `parser.py` mixes several independent responsibilities:

```text
source access
JSON parsing
Yomitan interpretation
HTML rendering
SQLite schema/storage
index building
lookup
```

This works, but it means the rest of the application can become coupled to implementation details of the current dictionary backend.

A cleaner conceptual architecture is:

```text
                 DictionaryProvider
                        │
             ┌──────────┴──────────┐
             │                     │
     Current local backend   Future Yomitan backend
             │                     │
             └──────────┬──────────┘
                        ▼
                DefinitionCandidate
                        │
                        ▼
                 CompreDef scorer
```

This is one of the most important architectural seams in the project.

---

# 19. `editor_browser.py`: turning the engine into an Anki feature

This module is the bridge from the core logic to actual Anki usage.

It handles things such as:

- editor actions
- Generate Definition button behavior
- Tab-to-Generate
- browser/bulk generation
- note updates
- UI refreshes

Conceptually:

```text
User action
    ↓
editor_browser.py
    ↓
generation service / generator.py
    ↓
chosen definition
    ↓
Anki note update
```

The important architectural principle is that this module should **orchestrate** the process, not contain the definition-selection algorithm itself.

---

# 20. `gui.py`: configuration and dictionary management

The GUI is mostly the control panel.

It deals with things such as:

```text
note type
word field
reading field
definition field
dictionary order
indexing / re-indexing
other settings
```

A useful mental model is:

```text
GUI = “How should CompreDef be configured?”

Generator = “Which definition should I choose?”
```

They are different concerns.

---

# 21. What happens when a dictionary is installed?

The user adds a Yomitan dictionary.

The expensive path is roughly:

```text
Yomitan dictionary
     ↓
read source files
     ↓
parse term-bank data
     ↓
interpret/render definitions
     ↓
write to SQLite
     ↓
create lookup indexes
     ↓
mark installation/indexing complete
```

The result is local searchable data.

The important payoff is:

```text
installation time = expensive
lookup time       = cheap
```

That is why the current project moved toward install-time indexing instead of re-parsing huge dictionaries whenever a user generates a definition.

---

# 22. What happens when a definition is generated?

After indexing, the runtime path is much shorter:

```text
Anki note
   ↓
word + reading
   ↓
learner knowledge snapshot
   ↓
dictionary lookup
   ↓
candidate definitions
   ↓
score candidates
   ↓
select winner
   ↓
write to Anki
```

The important point is that **dictionary indexing should not happen here**.

If every definition generation re-read a 1.3 GB dictionary, the whole point of the indexing architecture would be lost.

---

# 23. The product's current limitations

Understanding what CompreDef does not do is just as important as understanding what it does.

It currently does not fundamentally:

```text
❌ understand Japanese semantics
❌ understand grammar deeply
❌ know whether the learner truly knows a vocabulary item
❌ generate definitions with an LLM
❌ translate text
```

Its core is:

```text
learner knowledge proxy
+
dictionary candidate retrieval
+
scoring/ranking
+
Anki automation
```

That simplicity is a strength, but also defines the current limitations.

---

# 24. The big conceptual weakness: kanji ≠ vocabulary

The current score is primarily based on known kanji.

Consider:

```text
彼は目標を達成する。
```

A learner may know:

```text
彼 目 標 達
```

but not actually know the vocabulary:

```text
達成
```

The current model can therefore overestimate comprehension.

A future evolution could combine several signals:

```text
                     comprehension
                          │
             ┌────────────┴────────────┐
             │                         │
        known kanji             known vocabulary
             │                         │
             └────────────┬────────────┘
                          ↓
                     final score
```

Potentially other signals could be added later, such as definition length, dictionary priority, reading match, or penalties for poor/reference-only entries.

But this is future functionality, not what the current implementation fundamentally does.

---

# 25. Why Yomitan is interesting for the future

The current project is reimplementing substantial pieces of Yomitan's dictionary machinery:

```text
Yomitan dictionary files
        ↓
CompreDef parser
        ↓
CompreDef renderer
        ↓
CompreDef SQLite cache
        ↓
CompreDef lookup
```

The ideal future architecture may instead be:

```text
Yomitan
  ↓
its own dictionary/search machinery
  ↓
candidate definitions
  ↓
CompreDef scoring
  ↓
selected candidate
  ↓
Yomitan rendering
  ↓
Anki
```

In other words:

> **Yomitan handles dictionary semantics and rendering. CompreDef handles learner-aware ranking. Anki handles the learning environment and storage.**

That is a much cleaner division of responsibility.

---

# 26. Why the Yomitan API could be a major simplification

Yomitan's local API exposes term-entry data and Anki-field rendering capabilities.

The especially interesting endpoints are:

```text
POST /termEntries
POST /ankiFields
```

Conceptually, CompreDef could ask Yomitan:

```text
“Give me the candidate entries for this word.”
```

Then CompreDef could do:

```text
candidate 1 → score 0.35
candidate 2 → score 0.72
candidate 3 → score 0.91
...
candidate 17 → score 1.00
```

and select candidate 17.

Then the goal would be to have Yomitan render that exact candidate using its own mature rendering machinery.

If this works reliably, CompreDef would no longer need to duplicate as much of:

```text
Yomitan JSON parsing
Yomitan structured-content interpretation
HTML rendering
local dictionary copies
custom caching/indexing
```

That is why the Yomitan integration experiment is potentially much more than a minor optimization.

---

# 27. Sharing Yomitan's dictionaries instead of duplicating them

The attractive end state is:

```text
User installs dictionary in Yomitan once
                  ↓
        Yomitan owns the data
                  ↓
          CompreDef asks Yomitan
                  ↓
       no second dictionary copy
```

The challenge is that Yomitan runs in the browser extension environment while CompreDef runs inside Anki/Python.

A local API can act as the bridge:

```text
Anki / CompreDef
       │
       │ localhost
       ▼
   Yomitan API
       │
       ▼
Yomitan's own data/search/rendering
```

This is attractive because CompreDef does not need to know where Yomitan stores its internal data.

---

# 28. Why the exact candidate/rendering question matters

It is not enough for Yomitan to say:

> “Here are the first five definitions.”

Suppose the best learner-friendly definition is number 17.

CompreDef needs enough candidates to see it:

```text
Yomitan candidates
1
2
3
...
17  ← best for this learner
...
```

Then CompreDef needs a reliable way to obtain the **same candidate rendered correctly**.

The ideal flow is:

```text
/termEntries
     ↓
structured candidate objects
     ↓
CompreDef scoring
     ↓
choose candidate #17
     ↓
Yomitan renders candidate #17
     ↓
HTML written to Anki
```

This is the key technical question to validate before replacing the current dictionary system.

---

# 29. Why “just export the first 5 definitions” may not be enough

A fixed candidate limit can create a hidden failure mode:

```text
Yomitan returns first 5
        ↓
useful candidate is #8
        ↓
CompreDef never sees it
        ↓
bad definition selected
```

The better architecture is to obtain a sufficiently large candidate pool, or ideally all relevant candidates, and let CompreDef perform the final learner-specific ranking.

This preserves the distinction:

```text
Yomitan = dictionary search / candidate generation
CompreDef = learner-specific selection
```

---

# 30. Why a clean provider interface matters

A future-friendly CompreDef should think about dictionaries through an interface such as:

```python
class DictionaryProvider:
    def lookup(term, reading=None):
        ...
```

The important idea is not the exact class name. It is that the rest of CompreDef only needs:

```text
“Give me candidate definitions for this term.”
```

The implementation behind that request could be:

```text
Current local dictionary provider
             or
Yomitan API provider
```

The scoring engine should not care.

This makes the future migration dramatically easier.

---

# 31. The architecture to keep in your head

The clean conceptual model is:

```text
                 USER / ANKI
                      │
                      ▼
             Application orchestration
                      │
                      ▼
             CompreDef selection engine
                      │
                      ▼
             DictionaryProvider
                      │
           ┌──────────┴──────────┐
           │                     │
      local backend         future Yomitan API
           │                     │
           └──────────┬──────────┘
                      ▼
             definition candidates
                      │
                      ▼
              learner-aware score
                      │
                      ▼
                 best result
                      │
                      ▼
                    ANKI
```

The **interesting proprietary/project-specific part** is the middle:

> Given a learner's knowledge and a set of valid dictionary candidates, which candidate should be shown?

The dictionary itself should ideally be somebody else's solved problem.

---

# 32. One complete example

Suppose the user has learned:

```text
日 本 人 学 生 大 自 分 力
```

and has a card for:

```text
独立
```

Yomitan/dictionary search provides candidates:

```text
Candidate A:
他からの支配を受けず、自分の力で...

Candidate B:
他の人に頼らず、自分自身で...

Candidate C:
国家などが他の国から支配を受けないこと...
```

CompreDef evaluates them against its learner model.

Conceptually:

```text
A → 0.75
B → 0.90
C → 0.30
```

Therefore:

```text
B wins
```

The project is not saying B is universally the best definition.

It is saying:

> B is the best candidate for **this learner**, under the configured selection rules.

---

# 33. What to remember when reading the source

When opening a file, ask:

### `generator.py`

> “How does CompreDef decide which candidate wins?”

### `db_utils.py`

> “How does CompreDef estimate what the learner knows?”

### `parser.py`

> “How does the current implementation obtain, store, and render dictionary candidates?”

### `editor_browser.py`

> “How does a user action in Anki invoke the engine and save the result?”

### `gui.py`

> “How does the user configure the system and manage dictionaries?”

### lifecycle / addon initialization

> “How does CompreDef get attached to Anki without doing expensive work at startup?”

That mental model is much more useful than memorizing individual functions.

---

# 34. The most important distinction in the whole project

There are three different kinds of work:

```text
1. LEARNER MODEL
   What does this learner know?

2. DICTIONARY ENGINE
   What definitions exist for this word?

3. SELECTION ENGINE
   Which of those definitions is best for this learner?
```

The current project has:

```text
CompreDef learner model
+
CompreDef dictionary engine
+
CompreDef selection engine
```

The long-term architecture we'd like to approach is:

```text
CompreDef learner model
+
Yomitan dictionary engine
+
CompreDef selection engine
```

That removes a huge amount of duplicated functionality while making the unique part of CompreDef clearer.

---

# 35. Practical development roadmap

A sensible future sequence is:

```text
CURRENT
  ↓
clean architecture
  ↓
introduce DictionaryProvider boundary
  ↓
experiment with Yomitan API
  ↓
prove candidate retrieval + exact rendering
  ↓
make Yomitan provider primary
  ↓
keep local backend as fallback if useful
  ↓
only then consider deleting old dictionary machinery
```

Do not delete the existing parser/indexer before proving the alternative.

---

# 36. Final mental model

If everything else is forgotten, remember this:

```text
                   COMPREDEF

    “Give me a good dictionary definition
       that THIS learner can understand.”

                    │
                    ▼
        ┌─────────────────────────┐
        │ What does learner know? │
        └────────────┬────────────┘
                     │
                     ▼
        ┌─────────────────────────┐
        │ What candidates exist?  │
        └────────────┬────────────┘
                     │
                     ▼
        ┌─────────────────────────┐
        │ Score the candidates    │
        └────────────┬────────────┘
                     │
                     ▼
        ┌─────────────────────────┐
        │ Pick the best candidate │
        └────────────┬────────────┘
                     │
                     ▼
        ┌─────────────────────────┐
        │ Put result into Anki    │
        └─────────────────────────┘
```

The current project is already implementing this idea successfully.

The main architectural question for the next phase is simply:

> **Can Yomitan provide the “what candidates exist?” and “render this candidate correctly” parts so CompreDef can focus almost entirely on the learner-aware selection problem?**

If yes, that could make CompreDef substantially simpler, more reliable, and more faithful to Yomitan than maintaining a second dictionary engine.
