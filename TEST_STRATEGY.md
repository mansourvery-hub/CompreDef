# CompreDef — Test Strategy

Answers: *how do we mechanically prove QUALITY.md stays true?*

```
QUALITY.md (WHAT must hold)
    ↓
TEST_STRATEGY.md (HOW it is verified)
    ↓
tests / linters / smoke scripts / CI
```

## Layers (cheap → expensive)

| Layer | What | When | Cost |
|---|---|---|---|
| Targeted checks | `check()` cases for the touched area | While iterating | seconds |
| `tests/test_regression.py` | Full suite, exit 0 = gate | Before every commit | ~2 s |
| `debug/smoke_dialog.py` | Headless Learner-Knowledge window check (tabs, sorting, Browser search) | After gui.py/anki.py dialog changes | ~30 s, local Anki needed |
| `debug/sanity_knowledge.py` | Standalone snapshot sanity (Anki stubbed) | After `anki.py` DB changes | seconds |
| `debug/console_snippets.md` | Copy-paste Debug Console recipes | Live-collection triage | manual |
| Offscreen real-collection run | Dialog + scoring against the real 57k-note collection | Before releases touching scoring/knowledge | minutes |
| `./scripts/ci.sh` | Suite → commit → push → version bump → GitHub Release → AnkiWeb upload verify → local install | Every finished session | minutes |

## Requirement → enforcement map

| QUALITY rule | Enforced by |
|---|---|
| Q-K1 first-field-only | `test_kanji_extraction_correctness` (+ S2/S3 twins in `sanity_knowledge.py`) |
| Q-K2 no type gating | `test_kanji_extraction_correctness` (multi-layout notes) |
| Q-K3 mastered ≥ 365d | `test_kanji_extraction_correctness` (182.5/100d notes seen-not-mastered), summary/totals tests |
| Q-K4 scope-bounded, fail-closed | `test_scope_deck_filtering`, `test_multi_note_type_targeting` §6, empty-scope cases |
| Q-K5 session snapshot | `test_snapshot_waits_for_open_collection`, `test_sync_reset_is_thread_safe`, `test_dialog_payload_cached_per_generation` |
| Q-D1 native wrapper only | Code review + `test_db_connections_are_closed`; `debug/README.md` S1 AST scan |
| Q-D2 schema-proof SQL | `test_knowledge_survives_new_schema` (rejects legacy `models` table), S1 AST scan |
| Q-D3 visible failure | `test_indexing_failure_reported`, `_warn_db_error` paths |
| Q-D4 graceful degradation | Empty-scope / missing-field / malformed-row cases across suite |
| Q-G1 never overwrite | Tab decision matrix (`test_tab_generate_decisions`), bulk skip paths |
| Q-G2 ladder + argmax | `test_ladder_early_exit_order`, `test_v12_scoring_algorithm`, `test_disabled_dictionaries_skipped` |
| Q-G3 kana never scores | `test_scoring_ignores_furigana`, `test_parse_furigana_field_formats`, `test_reference_title_filtering`, `test_extract_clean_word_formats`, `test_reading_disambiguates_homographs` |
| Q-G4 non-blocking UI | `test_sync_reset_is_thread_safe` (taskman main-thread rule), dialog `run_in_background` paths |
| Q-G5 rendering fidelity | `test_structured_content_html_fidelity`, `test_renderer_version_invalidates_cache`, `test_data_sc_attribute_names`, `test_zip_folder_parity`, Yomitan split/pick tests, real-dictionary smoke |
| Q-C1 Qt agnostic | `test_qt_enum_compat` (scoped enums), `aqt.qt`-only imports |
| Q-C2 package identity | `scripts/build.sh` `[3/4]` manifest verification |
| Q-C3 renderer version bump | `test_renderer_version_invalidates_cache` |
| Q-C4 dual-context imports | `test_package_relative_imports` |
| Q-P1 suite green gate | `build.sh`/`ci.sh` refuse to proceed on any FAIL |
| Q-P2 history/comments/hints | `test_no_undefined_names_in_shipped_modules`, review |

## Regression discipline (the debugging branch)

```
bug discovered → diagnose → fix → add regression test → verify → commit
```

Every fixed production incident earns a permanent test named after
the incident (see `tests/test_regression.py` header map). A fix
without its regression test is incomplete.

## Fixture policy

- Default: tiny synthetic fixtures (3-entry Yomitan dicts, stub
  collection) — hermetic, millisecond-fast, no network.
- Real dictionaries (`/home/mohamed/Desktop/Dicts`, Yomitan bridge)
  are used ONLY by self-skipping smoke tests; `yomitan_fallback`
  stays disabled under test so live services can never leak in.
- Heavy scans (GROUP BY over 100k real rows) are banned from the
  suite — synthetic equivalents cover the same branches.
