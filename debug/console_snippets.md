# Live-collection console snippets

Paste into Anki's **Help → Debug Console** (or Ctrl+Shift+`;`, then
Ctrl+Enter to run). Read-only except U4. Replace `1619602654` only if
your add-on folder id differs.

## U1 — Triage "0 known kanji"

```python
import importlib, json, os
folder = mw.addonManager.addonsFolder("1619602654")
print("version:", json.load(open(os.path.join(folder, "manifest.json")))["human_version"])
m = importlib.import_module("1619602654.anki")
print("status:", m.knowledge_status())
print("mature:", len(mw.col.find_cards("prop:ivl>=21")))
```

Interpretation (see also `status["last_error"]`):

- `mature: 0` → the empty set is correct; knowledge needs
  `ivl >= 21` cards.
- `mature_notes_scanned: 0` with mature cards present → the query
  itself failed (check `last_error`); on old versions this meant the
  query hit a renamed table.
- `words_kept: 0` with scanned notes present → every mature note has
  an empty first field (unusual — inspect a few notes manually).
- `mature_notes_scanned: 0` with mature cards present AND `ready: true`
  → the snapshot was built before the collection opened (pre-v1.0.12
  bug); update the add-on.

## U2 — Does the set match my level?

```python
import importlib
m = importlib.import_module("1619602654.anki")
known = m.get_known_kanji_set()
vocab = m.get_known_vocabulary_set()
print("kanji:", len(known), "words:", len(vocab))
print("sample:", "".join(sorted(known)[:120]))
for k in ["漢", "語", "龍"]:
    print(k, "known?" , k in known)
```

Put kanji you have definitely studied (expect True) and ones you have
not (expect False) in the probe list.

## U3 — Where did this kanji come from?

Finds mature notes whose **first field** contains a kanji:

```python
K, SEP = "龍", "\x1f"
hits = 0
for (flds,) in mw.col.db.all(
        "SELECT flds FROM notes WHERE id IN "
        "(SELECT nid FROM cards WHERE ivl >= 21)"):
    first = flds.split(SEP, 1)[0]
    if K in first:
        print("first field:", first[:60])
        hits += 1
        if hits >= 10:
            break
print("hits:", hits)
```

If a kanji is "known" but this prints no hits, the snapshot is stale —
restart Anki to rebuild it.

## U4 — Force a clean rebuild

```python
import importlib
m = importlib.import_module("1619602654.anki")
m.reset_caches()
print("rebuilt, known kanji:", len(m.get_known_kanji_set()))
```

The first read after a reset may block briefly while the snapshot
rebuilds; that is expected (manual operation, not the hot path).
