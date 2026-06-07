# Session State

## Current Milestone
M4 — **IN PROGRESS** — quality bugs fixed; Slovak model downloaded; debug pilot re-run; full Q1–Q6 pilot pending.

## M4 Deliverables (status)
- `migrations/004_translation_status.sql` — **applied**; `translation_status` + `reviewer_notes` columns live
- `src/translate/translator.py` — DeepSeek V3 caller; `build_system_prompt` / `build_user_turn` public
- `src/translate/reviewer.py` — DeepSeek R1 caller; `max_tokens=8000`; `_parse_verdict` updated (XML + bottom-up)
- `src/translate/prechecks.py` — `check_structure` + `check_terminology_lemma` (MorphoDiTa Slovak); both wired
- `src/translate/loop.py` — `translate_segment()`; CLTK surface-form constraints; `PromptLogger` support
- `src/translate/prompt_logger.py` — `notes` field added to `log_iteration`; wired in `loop.py`
- `src/translate/pilot.py` — debug mode: first 10 segments of I.q1; Q1–Q6 full pilot is next step
- `src/common/lemmatize.py` — `lemmatize_slovak` added; Slovak MorphoDiTa model downloaded
- `src/acquire/download_models.py` — **rewritten**: downloads both Czech + Slovak ZIP archives via DSpace 7 bitstream UUIDs
- `prompts/translator_system.txt` — **replaced**: FORMATTING section; LEGIBILITY positive instruction; GRAMMAR (passive infinitive WRONG/RIGHT examples)
- `prompts/reviewer_system.txt` — **replaced**: semantics + legibility only; removed Axes 1 & 2; `<verdict>` XML tags
- `src/server/app.py` + templates — Flask preview server at `localhost:5000`
- `reports/m4_pilot.txt` — written; last debug run: 10 segments, 9 translated, 1 needs_human
- `reports/debug_1780840764.jsonl` — latest debug JSONL (current run)
- `reports/debug_1780757395.jsonl` — previous debug JSONL (for comparison)

## Bugs Fixed (from .claude/m4_quality.md)
- **Bug 1**: `check_terminology_lemma` added to `prechecks.py` + wired in `loop.py` — enforces terms with Slovak lemmatizer
- **Bug 2**: `APPROVED_WITH_NOTES` with empty notes raises `RuntimeError` in `_parse_verdict_text`
- **Bug 3**: `_parse_verdict` replaced with XML-tag extraction + bottom-up line scan
- **Bug 4**: Both prompt files replaced — translator gets LEGIBILITY + passive infinitive guidance; reviewer scoped to semantics only
- **Bug 5**: Sentinel variables renamed (`precheck_passing_draft`, `fallback_draft`)
- **Logger fix**: `notes` field added to `log_iteration` so `APPROVED_WITH_NOTES` content is visible in JSONL

## Latest Debug Pilot Results (debug_1780840764.jsonl)
| Metric | Old run (_048) | New run (_764) |
|---|---|---|
| Translated | 5/10 | 9/10 |
| needs_human | 5/10 (bugs) | 1/10 (legitimate) |
| Avg iterations | 1.0 | 1.2 |
| False positives | 4–5 | 0 |

Remaining `needs_human`: seg 199 (I.q1.a10.reply1) — Krystal terms `toto niečo` (hoc aliquid) and `intencionálny obraz` (species) cannot be forced into natural prose by V3 across 3 iterations. The precheck is working correctly; the glossary entries need human review.

## Slovak MorphoDiTa Model
Downloaded: `models/slovak-morfflex-pdt-170914/` (extracted from ZIP via DSpace 7 API).
Smoke-tested: `lemmatize_slovak('vierou') = ['viera']` ✓

## Known Gaps
- `glossary_term.category` is NULL for 116 Krystal-seeded terms
- All 58 formula entries (`sed_contra`, `respondeo`, `praeterea`) are `proposed` — none `approved` — structure pre-check silently skips these
- Multiword formula terms (`toto niečo`, `intencionálny obraz`) need glossary human review before full pilot
- `style_profile.yaml` replaced with `prompts/translator_system.txt`

## Next Actions
1. **Human review**: `hoc aliquid → toto niečo` — consider revising or exempting from prose pre-check
2. **Human review**: `species → intencionálny obraz` — add translator prompt WRONG/RIGHT example if keeping
3. **Full pilot**: in `pilot.py`, switch from `fetch_debug_segments` to `fetch_pilot_segments` (Q1–Q6, ~150 segments)
4. **Monitor**: abort thresholds at `needs_human > 20%`, `avg_iterations > 2.5`
5. **Gate 1**: review full pilot output at `http://localhost:5000` before M5
