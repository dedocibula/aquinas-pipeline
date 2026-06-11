# Data-Driven Formula Constraints — remove hardcoded `check_structure`

## Context

`src/translate/prechecks.py::check_structure` hardcodes three lemmas (`respondeo`, `sed_contra`, `praeterea`) and element_type-specific presence/absence logic. The pilot showed sed_contra formula misses precisely because formulas are *checked* but not *constrained* — the translator prompt never receives them when no `term_usage` row exists. The fix: make structural formulas ordinary data-driven glossary constraints, and generalize so **every** glossary term has (immutable slug `latin_lemma`, Latin surface form, proposed Slovak) — surface used for phrase matching (multiword/formula) or review display (singleword).

User decisions (confirmed): data-driven approach; editable `latin_text` Sheet column for all rows; formula precheck = presence-only (existing regex branch, unchanged).

Corpus facts (verified by DB query): openers are regular — `Respondeo dicendum quod` (2,655/2,655), `Sed contra` (2,635/2,655), `Praeterea,` (6,056), `Ad {ordinal} sic proceditur` (~2,655, ordinals to 17 incl. spelling variants), `Ad {ordinal} (ergo) dicendum quod` (~8,300). ~50 surface variants cover ~99%; the tail (`Et per hoc patet responsio…`) gets no constraint. Also: 70 of 73 `category='formula'` DB terms are DeepSeek-mislabeled connectives (`sicut`, `unde`…) that would cause verbatim-regex failure storms if approved — must be recategorized.

**No DDL anywhere.** Latin surface lives in `sense_rendering(lang='la')` (lang is free text; unique on `(sense_id, lang, source_id)`).

## Design rules

- **Storage:** one `la` rendering per sense = canonical Latin surface. Seed/backfill writes under a corpus/seed source; reviewer edits write under the human source. Selection is authority-ranked (human beats seed) — `max() FILTER` in `_load_glossary` is replaced for `la` (and kept consistent with how `get_locked_terms` ranks `sk`).
- **Matching by category** (`src/ingest/resolution.py`):
  - singleword term/name: unchanged — CLTK lemmatization joins on `latin_lemma`; surface is display-only.
  - multiword (`is_multiword=true`): phrase match uses `la_surface`, fallback `latin_lemma` (backward compat).
  - formula: phrase match on `la_surface`, **anchored at start** of whitespace-normalized segment text (`re.match`). Formula terms always go through the phrase-match path (`is_multiword=true`) even when the surface is a single word.
- **Dual-role words (praeterea pattern):** a word can be a formula at segment start and an ordinary connective term mid-sentence — as two glossary entries with distinct slugs: formula `praeterea_opener` (surface `Praeterea`, anchored, → "Ďalej") + term `praeterea` (lemma-matched). Phrase-match masking already prevents the opener occurrence from double-counting as the term.
- **Version bumps:** existing rule (bump when SK changes) stays. An edited `la` surface bumps version **only for multiword/formula** terms (pattern change → must re-resolve + rerun_stale). Singleword surface edits: no bump.
- **Precheck:** `check_terminology_lemma`'s formula branch (word-boundary regex on normalized text, anywhere in draft) stays exactly as-is. `check_structure` and all its plumbing deleted. No positional/negative gate — R1 reviewer owns structural nuance.

## Implementation steps

### 1. `src/common/glossary_repo.py` — surface-aware loading
`_load_glossary`: add `gt.category` and an authority-ranked `la_surface` per sense (LEFT JOIN `sense_rendering sr_la ON … lang='la'`, pick human-source row over seed via `source.authority_rank`; a `DISTINCT ON`/lateral subquery, not `max() FILTER`). Carry `category` and `la_surface` onto the term dict (first non-null across senses).

### 2. `src/ingest/resolution.py` — match-pattern helper
Add `_match_pattern(term) -> (pattern_str, anchored)`: pattern = `term.get("la_surface") or term["latin_lemma"]`; `anchored = term.get("category") == "formula"`. `phrase_match` + `mask_spans` use it; anchored → single `re.match` at position 0, others keep `finditer` substring behavior. Leftmost-longest sort unchanged (determinism). `resolve_segment`/`_write_term_usage` untouched — formulas ride the multiword path (`is_multiword=true`, single sense → `_resolve_single` → `krystal_single`).

### 3. New `src/ingest/seed_formula_terms.py` — seed + backfill + hygiene
CLI: `uv run python -m ingest.seed_formula_terms` (style of `reset_gap_proposals.py`). One transaction, idempotent re-runs. Phases:
1. **Formula scan:** load body segments (`_load_segments`); try opener regexes in order: `^Respondeo dicendum quod` → existing `respondeo`; `^Sed contra` → existing `sed_contra`; `^Praeterea\b` → existing `praeterea`; `^Ad ([a-z]+) sic proceditur` → slug `ad_{ord}_sic_proceditur`; `^Ad ([a-z]+) ergo dicendum (?:quod|est)` → `ad_{ord}_ergo_dicendum`; `^Ad ([a-z]+) dicendum quod` → `ad_{ord}_dicendum`. Ordinals harvested empirically (spelling variants = separate terms). Seed only freq ≥ floor (`--min-freq`, default 5); print unmatched openers (first ~6 words + count) for eyeballing.
2. **Writes:** new `glossary_term(latin_lemma=slug, is_multiword=true, category='formula')` (SELECT-then-INSERT, pattern from `gap_terms` — do NOT reuse `_ensure_glossary_term`, it hardcodes `is_multiword=false`); sense `status='proposed'`; renderings: `la` surface (seed source) + `sk` template (model source), both `ON CONFLICT (sense_id, lang, source_id) DO UPDATE`; never touch `sk` of already-approved senses. Slovak templates via `LATIN_ORDINAL_TO_SK` dict (`nonum → deviatej` → `k deviatej sa postupuje takto`), **no trailing punctuation** (would become a literal-period regex requirement).
3. **Migrate existing formulas:** `respondeo`, `sed_contra` → set `is_multiword=true, category='formula'`; insert `la` surfaces (`Respondeo dicendum quod`, `Sed contra`) on their approved senses. No status/version change → no retranslation storm. **Praeterea is dual-role:** seed a NEW formula term `praeterea_opener` (surface `Praeterea`, anchored, proposed sk `Ďalej`, status `proposed`); the existing approved `praeterea` entry is recategorized to `term` (stays lemma-matched mid-sentence as a connective, keeps its 'Ďalej' rendering and version — no retranslation).
4. **Backfill all other terms (user requirement):** for every sense lacking an `la` rendering, insert surface = `latin_lemma` (seed source). Uniform Sheet display; multiword Krystal phrases get surface = the phrase.
5. **Hygiene:** `UPDATE glossary_term SET category='prose' WHERE category='formula' AND` term has no opener-seeded surface (i.e., not in the seeded/migrated slug set) — catches the ~70 mislabeled connectives. Print affected slugs. (Approved ones like `sicut` move from regex branch to morphological branch — strictly safer; they leave the Review tab since `_WHERE_MAIN` filters term/formula — flag in output.)
6. Summary print: seeded N, migrated 3, backfilled K, recategorized M, X unmatched variants.

### 4. `src/translate/prechecks.py` — deletion
Delete `check_structure`, `_formula_cache`, `_clear_formula_cache`, `_FORMULA_SQL`, `_FORMULA_LEMMAS`, `_load_formulas` (~115 lines) + orphaned imports + docstring mention. `CheckResult`, `check_terminology_lemma`, `check_terminology`, `_normalise` stay.

### 5. `src/translate/loop.py`
- Remove `check_structure` import + call (lines ~338-356); collapse precheck failure branch to terminology-only → always `_build_terminology_microedit`. Remove `precheck_structure` failure class.
- `get_locked_terms`: add authority-ranked `la` scalar subquery as `latin_surface` (don't disturb the `DISTINCT ON` sk selection).
- Constraint building (~line 286): for `category='formula'`, display `latin_surface or latin_lemma` as the Latin side so translator/R1 see `Ad nonum sic proceditur → k deviatej sa postupuje takto`, not the slug. `translator.py` unchanged.
- `_build_surface_constraints` (~line 200): skip condition becomes `if " " in lemma or c.get("category") == "formula"` (slugs have no spaces; must not be CLTK-substituted).

### 6. Sheet: editable `latin_text` column — `src/review/sheets.py`, `export_sheet.py`, `import_approvals.py`
- `sheets.py HEADER`: insert `latin_text` after `latin_lemma` (position D); shift `context_label`→E, `proposed_slovak`→F, … `db_version`→O. Update `SENSE_ID_COL`, `rows_to_sheet_values`, validation/formatting ranges.
- `export_sheet.py`: `_EXPORT_SQL` adds authority-ranked `la` rendering as `latin_text` column; keep `latin_occurrence` = sample segment text as before.
- `import_approvals.py COLS`: re-index; read `latin_text`; if changed vs DB `la` value → `write` human-source `la` rendering (`sense_rendering(lang='la', source=human)`, reuse the `write_human_rendering` pattern generalized for lang, or a sibling helper `write_human_surface`) and **bump version only when the term is multiword/formula**.
- **Migration procedure (operator step, must precede deploy of new header):** run `import_approvals` to drain pending reviewer edits → deploy → `export_sheet` re-export (header mismatch wipes rows; safe once drained).

### 7. Tests
- `tests/translate/test_prechecks.py`: delete FakeConn/FakeCursor, `_STANDARD_FORMULA_ROWS`, cache fixture, 9 `check_structure` tests. Add formula-branch cases: `Odpovedám: treba povedať, že` exact-punctuation match; `k deviatej sa postupuje takto` with draft ending in period; near-miss word-boundary failure.
- `tests/translate/test_loop.py`: remove `_PATCH_STRUCTURE` + ~15 usages; convert/drop structure-failure tests (`precheck_structure` assertions).
- `tests/ingest/test_resolver.py`: `la_surface` + formula anchoring (matches at start, not mid-text; slug never matches; non-formula multiword unchanged; mask consistency).
- New `tests/ingest/test_seed_formula_terms.py`: opener regexes (incl. spelling variants, `Et per hoc patet` non-match), slug construction, ordinal→SK template completeness, freq-floor.
- Sheet tests (`tests/review/…`): header/index shifts, `latin_text` import semantics (bump only multiword/formula).

## Verification (end-to-end)

```bash
uv run pytest tests/
uv run python -m ingest.seed_formula_terms          # inspect seeded/migrated/backfilled/recategorized/unmatched report
# DB spot-check: formula terms + la/sk renderings
uv run python -m review.import_approvals            # drain pending edits BEFORE header change deploy
uv run python -m review.export_sheet                # new header; formula rows visible with latin_text
# fake-approve 2-3 formula senses (UPDATE glossary_sense SET status='approved' …) for pilot
uv run python -m ingest.resolver                    # anchored phrase-match; verify usage counts ≈ corpus (respondeo ≈2655, sed_contra ≈2635, praeterea ≈6056 segment-initial)
PILOT_WORKERS=5 uv run python -m translate.pilot    # or translate.run --pars I --max-questions 1
# check prompt logs: 'Ad nonum sic proceditur → k deviatej sa postupuje takto' constraint lines; precheck behavior
```

## Flags
- No DDL → no human-review stop for schema.
- Reviewer action: approve ~50 seeded formula rows in the Sheet.
- Operator eyeball: recategorization slug list + unmatched-opener report from the seed script.
- Commit in logical chunks (matching, seed script, precheck/loop removal, sheet) with diffs shown before each commit per project rules.

## Follow-ups (out of scope here — record in docs/session_state.md during implementation)
1. **Glossary rebuild from scratch:** unify `src/ingest/gap_terms.py`, `resolution.py`, `resolver.py`, `sense_mining.py` into one coherent sources→glossary stage, so the full pipeline is runnable end-to-end: download sources → ingest/store → extract glossary (correct category, context label, proposed Slovak derived from Czech+English sources, lowercase infinitive lemma form) → resolve → translate. Replaces today's incremental post-fixing of model-assigned categories/renderings.
2. **Krystal de-authorization:** doubts about Krystal source quality — merge Krystal terms into the review flow (proposed, reviewed alongside Bahounek/English evidence) instead of auto-approving them as law. NOTE: this reverses Principle 2 in CLAUDE.md and entries in `.claude/decisions.md` — a deliberate decision change to be made explicitly, after the formula-constraints work lands.
