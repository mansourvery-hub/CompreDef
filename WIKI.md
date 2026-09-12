# CompreDef — Dictionary Picker Algorithm

## The problem

A learner looking up a word in a Japanese–Japanese dictionary gets a
definition written with words and kanji they have not learned yet —
which sends them looking up *those* words, forever (the **infinite
lookup loop**).

Children's dictionaries use simple language but don't cover advanced
terms. No single dictionary fits a learner at every level.

## The idea

CompreDef owns several dictionaries. For each word it:

1. Collects **every** definition from **every** dictionary.
2. Scores how much of each definition the learner can already read.
3. Returns the most readable one.

Every definition is ranked with the **same logic**, no matter which
dictionary it came from. Dictionary order never decides anything.

```mermaid
flowchart TD
    Start([Word to define]) --> Gather[Gather all definitions<br>from all dictionaries]
    Gather --> Clean[Clean each definition<br>strip furigana and HTML]
    Clean --> Drop[Drop the non-definitions<br>cross-reference titles]
    Drop --> Score[Score what remains<br>readability density]
    Score --> Win([Return the highest score])
```

---

## Step 1 — What the learner knows

Once per session, CompreDef scans the learner's mature Anki notes
(cards reviewed for a full year or more, `interval ≥ 365`) and records
two things:

- **Kanji points** — one number per kanji.
- **Vocab points** — one number per multi-kanji compound
  (e.g. 公平, 会社). Kana-only words are skipped: kana inflects
  (やめる vs やめて), so matching whole kana words is unreliable.

Only the **first field** of each note counts (the word itself).
Definitions and examples never count — otherwise CompreDef's own
output would mark unknown kanji as known.

Each item earns mastery points from its best card interval:

$$
\mathrm{point}(x) = \min\left(1,\ \frac{\mathrm{interval}(x)}{365}\right)
$$

- Unknown item: $0$.
- One-year interval: the full $1.0$.
- Half a year: $0.5$.

So knowledge is not known/unknown — it is a number between 0 and 1
per kanji and per compound.

---

## Step 2 — Gather the definitions

Every installed dictionary is asked for the word — local dictionaries
through the SQLite index, optionally the Yomitan browser bridge.
A dictionary with no entry simply contributes nothing. A missing word
yields no candidates at all (never an error, never a freeze).

---

## Step 3 — Clean the text

Dictionary entries are rich HTML (ruby furigana, styling). Before
scoring, each definition is reduced to **scoring text** in three
passes. Display keeps the full HTML untouched — only the scoring
copy is cleaned.

- **Drop the boilerplate.** Thesaurus sections (`類語` synonym
  chains) and part-of-speech tags (〘名〙 and friends) are identified
  by their HTML markers and removed. A 20-synonym list says nothing
  about how readable the explanation is — scoring it rewards and
  punishes noise. Only tagged blocks are stripped; plain-text labels
  without tags are accepted noise (bounded, documented).
- **Strip the markup.** Remove `<rt>` / `<rp>` furigana readings,
  all remaining HTML tags, unescape entities (`&lt;` → `<`).
  Furigana must never pollute the score: the reading ま above 先 is a
  hint for display, not a kanji the learner needs to know.
- **Remove the headword.** The defined word itself is deleted before
  counting. The learner looked the word up precisely because it is
  unknown — its self-mentions (headers included) earn nothing. Only
  multi-character terms are removed (excluding a single kanji would
  wipe a common character everywhere).

---

## Step 4 — Drop the non-definitions

Some dictionary entries are not definitions at all. Two shapes occur:

- **Cross-reference titles** — a bare headword pointing elsewhere,
  e.g. an entry for 参照 whose whole content is `会社更生法`.
  No explanation, no sentence, just a title.
- **Child-entry lists** — several headwords joined by pipes, e.g.
  `(子) 会社員 | 会社組合 | …`. A list of words, not prose.

Why drop them: every kanji in a short title can be known, so a title
scores ~1.0 and beats the genuine explanation (this really happened
with 会社). Real prose always ends in 。？！ — anything with
sentence-ending punctuation is never dropped.

Exception: if a title is the *only* candidate, it is kept. A poor
definition beats no definition.

---

## Step 5 — The score, in detail

For one definition $D$, write:

- $K(D)$ — its kanji, counted **with repetition**
  (a kanji appearing 3 times counts 3 times).
- $W(D)$ — its **distinct** multi-kanji compounds.

Compounds are found without a morphological analyzer: maximal runs
of kanji characters, length 2 or more (MeCab is an explicit
non-goal — heavy native dependency, version drift). Runs split at
any kana or punctuation, so okurigana inflections (偏る → 偏) stay
out of vocab by construction, and synonyms glued by ・ split
correctly. Adjacent compounds with no separator at all can glue
(rare in prose; synonym lists are stripped first). Deterministic on
every machine.

### Kanji density

The fraction of the definition's kanji the learner knows:

$$
\mathrm{kanji\_density}(D) = \frac{\sum_{k \in K(D)} \mathrm{point}(k)}{|K(D)|}
$$

A definition with 10 kanji, 9 fully known: $9 / 10 = 0.9$.

### Vocab density

The fraction of the definition's compounds the learner knows:

$$
\mathrm{vocab\_density}(D) = \frac{\sum_{w \in W(D)} \mathrm{point}(w)}{|W(D)|}
$$

A definition using 4 compounds, 3 fully known: $3 / 4 = 0.75$.
A definition with no compounds at all scores $0$ here (nothing to
judge — not a penalty, just no signal).

### The score

$$
\mathrm{score}(D) = \mathrm{kanji\_density}(D) + \mathrm{vocab\_density}(D)
$$

One number between 0 and 2. The definition with the highest score
wins.

Two deliberate properties:

- **Kana = known (first approximation).** Kana length never punishes a
  definition. A kana-only definition scores neutral $0$ — it never
  wins on merit, so a kana gloss can't beat real prose, but a
  kana-heavy explanation of known kanji is not penalized either.
- **Density, not mass.** Raw point sums grow with length, so a
  20-paragraph 5%-known definition always beat a succinct 90%-known
  one. Dividing by length measures the fraction the learner can
  actually read. (The old raw-sum ranking survives as `LegacySumPicker`
  for comparison via the `picker_strategy` config key.)

---

## Step 6 — Ranking

Best first, strict order — given $N$ definitions and one learner,
there is exactly one correct ordering:

1. Highest $\mathrm{score}(D)$.
2. Tie-break: **most kanji** (among equals, prefer richer prose).
3. Tie-break: dictionary title, then definition text.

Keys 2–3 compare the definitions themselves, never their positions in
any list — shuffling the input can never change the winner.

---

## Worked example — making the sausage, step by step

Word **不公平**, 4 definitions, one learner: the repo owner, whose
snapshot holds **1379 kanji and 1310 compounds** (totals only — the
full lists live in the Learner Knowledge dialog, not here).

### Pass 1 — clean each definition

Raw base text first, then what cleaning removes:

- **三省堂国語辞典** (39 chars): no boilerplate. Headword 不公平
  appears twice (［不公平］ header, 不公平さ) → removed. Left:
  `ふこうへい［］｟名・ダナ｠あつかいが平等でないこと。（↔公平）さ。`
  (33 chars)
- **小学館例解学習国語** (57 chars): no boilerplate. Headword
  twice (【不公平】 header, 不公平な判定) → removed. (51 chars)
- **デジタル大辞泉** (67 chars): no boilerplate. Headword twice
  (【不公平】 header, 不公平な扱い) → removed. (61 chars)
- **大辞泉 第二版** (168 chars): the 類語 block goes first — 93
  characters of synonym links (先入観・贔屓目・偏見・偏頗 …),
  including 5 kanji the learner does **not** know (**偏**, **僻**,
  **屓**, **贔**, **頗**) and 19 unknown compounds. Then the
  headword (【不公平】 header) → removed. Left: 72 characters of
  actual explanation.

What stays: 三省堂's plain-text POS tag ｟名・ダナ｠ has no HTML
markers, so its 名 survives as scoring input (known — harmless,
documented noise).

### Pass 2 — kanji view: type each definition, highlight what is known

Same cleaned text as Pass 1, typed a first time with every **known**
kanji highlighted and every **unknown** kanji bold. Kana and
punctuation stay plain — they are never scored.

**三省堂国語辞典** — 5 occurrences, all known → $5/5 = 1.0$:

> ふこうへい［］｟<mark>名</mark>・ダナ｠あつかいが<mark>平</mark><mark>等</mark>でないこと。（↔<mark>公</mark><mark>平</mark>）さ。

**小学館例解学習国語** — 11 occurrences, all known → $11/11 = 1.0$:

> ５ふこうへい【】 <mark>名</mark>・<mark>形</mark><mark>動</mark>だな フコーヘー<mark>公</mark><mark>平</mark>でないこと。えこひいきがあること。<mark>例</mark> な<mark>判</mark><mark>定</mark>。<mark>対</mark> <mark>公</mark><mark>平</mark>。

**デジタル大辞泉** — 12 occurrences, all known → $12/12 = 1.0$:

> ふ‐こうへい【】［<mark>名</mark>・<mark>形</mark><mark>動</mark>］<mark>公</mark><mark>平</mark>でないこと。<mark>片</mark><mark>寄</mark>りがあること。また、そのさま。「な<mark>扱</mark>いを<mark>受</mark>ける」[<mark>派</mark><mark>生</mark>]ふこうへいさ［<mark>名</mark>］

**大辞泉 第二版** — 12 occurrences, all known → $12/12 = 1.0$:

> ふ‐こうへい【】アクセント ふこ↓うへい <mark>名</mark>・<mark>形</mark><mark>動</mark>〙<mark>公</mark><mark>平</mark>でないこと。<mark>片</mark><mark>寄</mark>りがあること。また、そのさま。「―な<mark>扱</mark>いを<mark>受</mark>ける」<mark>派</mark><mark>生</mark> ふこうへいさ<mark>名</mark>〙

Nobody has an unknown kanji left — the cleaning pass deleted every
one, so every kanji lights up. Kanji alone cannot separate these
four; the decision moves to compounds entirely.

### Pass 3 — compound view: type each definition again, highlight the words

Same text typed a second time, now with every **known compound**
highlighted and every **unknown** compound bold. Single kanji stay
plain here — they already had their turn in Pass 2; only
multi-kanji runs score as words.

**三省堂国語辞典** — ✓公平, ✓平等 → $2/2 = 1.0$:

> ふこうへい［］｟名・ダナ｠あつかいが<mark>平等</mark>でないこと。（↔<mark>公平</mark>）さ。

**小学館例解学習国語** — ✓公平 | ✗判定, ✗形動 → $1/3 = 0.333$:

> ５ふこうへい【】 名・**形動**だな フコーヘー<mark>公平</mark>でないこと。えこひいきがあること。例 な**判定**。対 <mark>公平</mark>。

**デジタル大辞泉** — ✓公平 | ✗形動, ✗派生, ✗片寄 → $1/4 = 0.25$:

> ふ‐こうへい【】［名・**形動**］<mark>公平</mark>でないこと。**片寄**りがあること。また、そのさま。「な扱いを受ける」[**派生**]ふこうへいさ［名］

**大辞泉 第二版** — ✓公平 | ✗形動, ✗派生, ✗片寄 → $1/4 = 0.25$:

> ふ‐こうへい【】アクセント ふこ↓うへい 名・**形動**〙<mark>公平</mark>でないこと。**片寄**りがあること。また、そのさま。「―な扱いを受ける」**派生** ふこうへいさ名〙

### Pass 4 — score and rank

| dictionary | kanji | vocab | **score** |
|---|---|---|---|
| 三省堂国語辞典 | $5/5 = 1.0$ | $2/2 = 1.0$ | **2.0** ★ |
| 小学館例解学習国語 | $11/11 = 1.0$ | $1/3 = 0.333$ | 1.333 |
| デジタル大辞泉 | $12/12 = 1.0$ | $1/4 = 0.25$ | 1.25 |
| 大辞泉 第二版 | $12/12 = 1.0$ | $1/4 = 0.25$ | 1.25 |

デジタル大辞泉 and 大辞泉 tie on everything (1.25, 12 kanji) — the
tertiary key decides: デジタル大辞泉 sorts before 大辞泉 第二版.

三省堂 wins because it is the only definition whose every compound
is known. Under the old raw sums 大辞泉 won with 49 points of
known-kanji mass while being the least readable of the four — that
flip is the v1.3 change, and you can reproduce every number above
with `python3 debug/audit_picker.py --html`.

---

## Swapping the method

Everything above lives in the self-contained `picker.py` (it depends
only on `scoring` / `models` / `utils` — never on providers, Anki, or
Qt). A new picking idea subclasses `PickerStrategy` — one method,
`rank_key` — and becomes active via `get_active_strategy()` or the
`picker_strategy` config key. No other module changes.

---

## 2. SQLite-Cached Dictionary Indexing

CompreDef uses **SQLite caching** for dictionary lookups instead of
pickle files, providing faster B-tree lookups with zero RAM footprint.

### Dictionary Sources

CompreDef supports both:

- **Unzipped Directories**: Contains `term_bank_*.json` files and
  `index.json` metadata.
- **ZIP Archives**: Yomitan `.zip` dictionaries loaded directly from
  compressed archives (zero-disk footprint, instant access).

### SQLite Database Structure

The cache database (`user_files/cache/dictionaries.db`) contains:

- `dictionaries` table: Stores dictionary path, title, signature, and
  entry count.
- `entries` table: Maps `(dict_path, term)` to rich HTML definitions.

### Cache Invalidation

- **Signature**: Computed from dictionary source (file modification
  times and sizes for folders, mtime + size for ZIPs), with the
  renderer version embedded (a renderer change invalidates every
  index).
- **Update Trigger**: Dictionaries are indexed ONCE at install time
  via the GUI; generation is pure SQLite lookups and never parses
  files. When dictionary files change, the signature updates,
  triggering re-indexing on explicit reinstall.
- **Performance**: Lookups are instant indexed B-tree queries; a
  lookup never triggers indexing (verified by
  `test_lookup_never_indexes`).

### Yomitan HTML Rendering

CompreDef renders faithful Yomitan structured-content:

- **Ruby Furigana**: `<ruby>` tags with `<rt>` readings preserved for
  Anki display.
- **Data Attributes**: `data-sc-*` attributes for accessibility and
  tooling.
- **Inline CSS**: Yomitan's `style` field converted to inline CSS
  (e.g., `fontSize: 14em` becomes `font-size: 14em`).
- **Special Tags**: `<data-sc-name="用例">` for example sentences.
- **Rich Elements**: `<span class="gloss-sc-span">`,
  `<div class="gloss-sc-div">`, table cells with `colSpan`/`rowSpan`.
- **Output**: Full HTML with all styling and semantics for direct
  insertion into Anki note fields.

---

## 3. Database Safety Mandate

In strict accordance with Anki development standards:

- **No Direct SQLite Connections**: External sqlite3 connections to
  `collection.anki2` can cause database corruption and SQLite locks.
  CompreDef exclusively accesses the database through Anki's native
  Python wrapper: `mw.col.db.all()`.
- **Non-Blocking Background Threads**: All dictionary searches,
  scoring calculations, and database scans execute asynchronously
  using `mw.taskman.run_in_background()` with a completion callback
  to the main thread.

---

## 4. Regression Testing Mandate

The fundamental regression suite lives at `tests/test_regression.py`
(plus isolated Ring 0 units at `tests/test_units.py`) and **must be
run green before every commit**
(`python3 tests/test_units.py && python3 tests/test_regression.py`).
Each test maps to a real historical bug:

| Historical bug | Guarding test |
|---|---|
| Plain-text definitions (121 chars) served instead of rich Yomitan HTML (~7000 chars) | `test_structured_content_html_fidelity` + real-dictionary `先ず` smoke test |
| Renderer upgraded but SQLite cache kept serving stale plain text forever | `test_renderer_version_invalidates_cache` (verifies `RENDERER_VERSION` is embedded in signatures) |
| Furigana `<rt>` readings polluted the kanji comprehension score | `test_scoring_ignores_furigana` |
| An advanced dictionary won despite a simpler readable definition existing | `test_order_independent_argmax` (pins order-independent argmax) |
| Raw-sum scoring favored 20-paragraph 5%-known definitions over succinct 90%-known ones | `test_v12_scoring_algorithm` §7 + `test_picker_audit_strict` (density ordering over the real-deck fixture) |
| Cross-reference titles ("see also") won over real definitions | `test_reference_title_filtering` |
| ZIP archive and unzipped folder produced different output | `test_zip_folder_parity` |
| `data-sc-*` attributes drifted from Yomitan's DOM naming (breaking the user's CSS compactor) | `test_data_sc_attribute_names` |
| Nonsense word `駿ってさ` froze Anki at 100% CPU and crashed it | `test_nonsense_word_returns_none_fast` |
| Indexing accumulated ~1.3 GB of rendered HTML in RAM (OOM freeze on giant dictionaries like 大辞泉) | `test_indexing_streams_in_batches` (verifies bounded `_INDEX_BATCH_SIZE` + streamed commits) |
| SQLite connections never closed (`with conn:` commits but does not close), leaking a handle per lookup | `test_db_connections_are_closed` |
| Renderer upgrade left duplicate/orphan rows behind | `test_renderer_upgrade_reindexes_cleanly` |

The suite stubs `aqt` so it runs on both system Python and Anki's
bundled Python without Anki installed. Smoke tests against the real
installed dictionaries self-skip when those dictionaries are absent.

## 5. On-Demand Debugging

Beyond the per-commit suite, `debug/` holds the learner-knowledge
snapshot spec (SP1–SP7), use cases (U1–U5), copy-paste Debug Console
recipes for the live collection (`console_snippets.md`), a standalone
sanity script (`python3 debug/sanity_knowledge.py`), and the
dictionary-picker audit
(`python3 debug/audit_picker.py --capture --html`, fixture at
`tests/fixtures/picker_audit.json`) — all deliberately **not** run by
CI except through their frozen fixtures.
