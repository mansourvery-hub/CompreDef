# CompreDef — Implementation Plan

Product status: **shipped and stable (v1.2.11+)**. Every slice below
is COMPLETE, tested, and released. New work enters only via the
Backlog at the bottom — never by silent scope creep mid-session.

## Shipped slices (all COMPLETE)

- **T1 — Install-time dictionary indexing** (folder/ZIP, SQLite
  cache, signature + renderer-version invalidation)
- **T2 — Dictionary Ladder generation** (early exit, maximal
  fallback, argmax scoring, tie-breaks)
- **T3 — Learner knowledge snapshot** (first-field extraction,
  mastered ≥ 365d / seen > 0, Scope-bounded, session-cached)
- **T4 — Deck Scope + quick-fix** (subdeck expansion, ANY-card
  membership, fail-closed empty scope, add-deck dialog)
- **T5 — Multi-type field targets + inference** (per-type mappings,
  auto-inference fallback, legacy single-type compat)
- **T6 — Editor integration** (toolbar button, Tab-to-Generate via
  unfocus hook, Add-window deck resolution, single-flight guard)
- **T7 — Browser bulk generation** (menu/context actions, per-note
  failure isolation, summary tooltips)
- **T8 — Learner Knowledge dialog** (Overview stat cards, Kanji/Vocab/
  Mature-Notes tabs, sorting, filter, Browser provenance search,
  non-modal singleton, payload cache)
- **T9 — Yomitan API source** (bridge installer, keepalive,
  anti-zombie shutdown, nonce pairing, nested timeouts)
- **T10 — Yomitan-fidelity rendering** (structured content, ruby,
  `data-sc-*`, per-dictionary CSS scoping, style sanitization,
  ZIP ≡ folder parity)
- **T11 — Release pipeline** (regression gate, packaging + verify,
  GitHub Release, AnkiWeb upload watch, local auto-install)

## Dependency graph (historical — all edges satisfied)

```text
T1 ─┬─→ T2 ─→ T6 ─→ T7
    │         ↑
    ├─→ T3 ───┤
    ├─→ T4 ───┤
    ├─→ T10 ──┘
    └─→ T5 ───→ T8
T9 ──→ T2 (alternate dictionary source)
T11 covers all (release)
```

## Backlog

Empty. New ideas arrive as user requests; each becomes a T-item
here (goal → slices → status) before any code is written.

## Working agreements (from experience)

- One user request = one workstream; finish, verify, release.
- Docs change with behavior in the same commit (QUALITY.md rule
  touched ⇒ TEST_STRATEGY.md row touched).
- Debug-only tooling (`debug/`) never ships (`build.sh` packages
  root `*.py` + assets only) and never runs in CI.
- Root-level scratch files (`debug_*.py`, `*.backup`, `*_original.py`)
  must never be committed — they get packaged into the `.ankiaddon`.
