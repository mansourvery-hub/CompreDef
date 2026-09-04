# Live-collection console snippets

Paste into Anki's **Help → Debug Console** (or Ctrl+Shift+`;`, then
Ctrl+Enter to run). Read-only except U4. Replace `1619602654` only if
your add-on folder id differs.

## U1 — Triage "0 known kanji"

```python
import importlib
m = importlib.import_module("1619602654.anki")
print("mature cards:", len(mw.col.find_cards("prop:ivl>=21")))
print("config:", mw.addonManager.getConfig("1619602654"))
print("tables:", [r[0] for r in mw.col.db.all(
    "SELECT name FROM sqlite_master WHERE type='table'")])
known = m.get_known_kanji_set()
print("known kanji:", len(known))
```

Interpretation:

- `mature cards: 0` → the empty set is correct; knowledge needs
  `ivl >= 21` cards.
- `config` shows a wrong `note_type`/`word_field` (exact names matter)
  → fix it in Tools → CompreDef Configuration and restart Anki.
- `tables` lacks `models` but the snapshot is still empty with mature
  cards present → the query is hitting a renamed table (the v1.0.5
  incident); current code must not reference it (see S1).

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

Finds mature notes whose **Expression field** contains a kanji:

```python
K, SEP = "龍", "\x1f"
cfg = mw.addonManager.getConfig("1619602654")
model = mw.col.models.by_name(cfg["note_type"])
idx = [f["name"] for f in model["flds"]].index(cfg["word_field"])
hits = 0
for mid, flds in mw.col.db.all(
        "SELECT mid, flds FROM notes WHERE id IN "
        "(SELECT nid FROM cards WHERE ivl >= 21)"):
    model2 = mw.col.models.get(mid)
    if not model2 or model2["name"] != cfg["note_type"]:
        continue
    parts = flds.split(SEP)
    if idx < len(parts) and K in parts[idx]:
        print("note mid:", mid, "| expression:", parts[idx][:60])
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
