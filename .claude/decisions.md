# Decision Log

Read this before "improving" anything. Every non-obvious choice is recorded here
with its rationale. If you think something looks wrong, check here first.

---

## Article and question titles stored as segments, not a separate table

**Decision:** `question_title` and `article_title` are `segment` rows with their own
`locator_path` (`I.q3`, `I.q3.a1`) and `element_type` ('question_title', 'article_title').
Their text lives in `segment_text` alongside body segments.

**Why:** titles are translatable units — the Slovak translation needs them to produce a
complete, readable document (HTML preview, Word export). Storing them in a separate table
would require a parallel schema for what is structurally identical content. The renderer
reads all segment types uniformly via a single `ORDER BY locator_path` query.

**Rendering:** `locator_path` ltree ordering naturally places `I.q3` before `I.q3.a1`
before `I.q3.a1.arg1`, giving correct document order without any special-casing.

**Source of titles:** Latin HTML (`<H2>/<H3>`) if available; Dominican English (`<h1>/<h2>`)
is the reliable fallback since it always has clean heading markup. The renderer uses the
first non-null language in sk→la→en order.

---

## No CHECK on `segment.element_type`

**Decision:** `element_type` is a free-text label with no CHECK constraint. The comment documents the Summa values ('arg','sed_contra','respondeo','reply') but does not enforce them.

**Why:** a CHECK would bake Summa structure into the schema. Contra Gentiles chapters have no `sed_contra`, no `reply` — different element types entirely. The value is owned by the parser that writes it, exactly like `locator_path`. Validation belongs in the parser (fail-loud), not in the DB constraint.

---

## Opaque `locator_path` string, not typed columns

**Decision:** `segment.locator_path` is plain TEXT ('I.q3.a1.arg2'), not three
columns (pars, quaestio, articulus).

**Why:** typed columns would bake the Summa's structure into the schema. A future
Contra Gentiles parser writes 'lib1.cap13'; a free text writes paragraph indices.
Nothing downstream needs to parse the string — they just need it to be unique and
stable. `LIKE 'I.q3.%'` handles range queries. Generality cost: zero.

---

## Languages and sources as rows, not columns

**Decision:** `segment_text(segment_id, lang, content, source_id)` not
`segment(latin_text, czech_text, english_text, slovak_draft)`.

**Why:** typed language columns bake a fixed language set into the schema.
Adding French→Slovak would require `ALTER TABLE`. Languages are an open-ended set;
model them as rows. Same logic for `sense_rendering`.

---

## CHECK enums for closed sets, FK tables for open sets

**Decision:** `element_type`, `status`, `confidence`, `resolution_method` use
CHECK constraints. `lang`, `source_id` use FK tables.

**Why:** normalize what grows (languages, sources, senses); don't over-normalize
what's fixed (there are exactly four element types; they will never change).
A lookup table for element_type adds a join for zero benefit.

---

## Single global `authority_rank` on `source`

**Decision:** one integer, one global ordering. Not per-job (e.g. one rank for
disambiguation, another for translation reference).

**Why:** there is no empirical evidence yet that English out-disambiguates Bahounek
for sense selection. Building two rank columns would be speculative complexity.
If after the first full translation the data shows English is the better
disambiguator, add a second column then. Defer the decision to when you can
make it empirically rather than by intuition.

---

## Krystal wins; Bahounek fills gaps only

**Decision:** Krystal glossary beats all other sources for any term it covers.
Bahounek is consulted only for terms Krystal has no entry for.

**Why:** we have Krystal's glossary (~150 entries) and style rules but not their
running translation text. Within Krystal's vocabulary, Bahounek's choices are
irrelevant — Krystal is the scholarly authority and the pipeline's target voice.
Bahounek predates Krystal's conventions and systematically disagrees on some
name forms (Diviš vs. Dionýsios). Krystal wins on every conflict; there is
no arbitration.

---

## Coexisting Slovak rows (model + human)

**Decision:** `(sk, model)` and `(sk, human)` rows coexist in `segment_text`.
The model's draft is never overwritten when a human edits.

**Why:** preserving both rows lets us diff drafts vs finals and measure how
much humans actually changed. This is free (storage is cheap) and provides
useful quality signal. "Give me the final" = filter on source_id=human;
the draft is still there for audit.

---

## `version` + `sense_version_used` as the invalidation engine

**Decision:** `glossary_sense.version` increments on any change to a sense's
approved rendering or status. `term_usage.sense_version_used` records the version
live when that segment was translated. Stale segments = WHERE sense_version_used < version.

**Why:** this bounds re-run cost to the affected segments — never the whole corpus.
When a reviewer changes a term, `WHERE sense_id=X AND sense_version_used < new_version`
returns exactly the segments that need re-translation. For a term appearing 47 times,
that's 47 segments re-translated, not 2,669 articles. The version number is not
for historical archaeology; it is the cost-control mechanism.

Do not conflate this with a full audit history (every past value, who changed what).
That's a separate `sense_history` table, deferred. The version counter is infrastructure;
the history table is documentation.

---

## `reply_to` retained despite Krystal dropping the formula

**Decision:** `segment.reply_to` preserves the objection↔reply linkage even though
Krystal's output style drops "ad primum dicendum" and just numbers objections/replies.

**Why:** the M4 reviewer agent's structural-fidelity check requires knowing which
reply answers which objection. If the linkage is discarded at ingest time, it cannot
be reconstructed. The Krystal output style is applied by the formatter; the internal
data model must retain the structure regardless of output conventions.

---

## `style_profile.yaml` outside the DB

**Decision:** Krystal's heading templates, citation rules, name forms, and
orthography live in a YAML file version-controlled with code.

**Why:** these drive prompt behavior (translator + reviewer agents), not lookups.
They are config, not data. Storing them in the DB would mean loading them into
application memory on every prompt build anyway — the YAML is simpler and more
transparent. A different publisher's house style = a different YAML file.

---

## No LangGraph in M0–M2

**Decision:** M0–M2 use plain Python. LangGraph is not introduced until at least M4.

**Why:** M0–M2 are linear batch pipelines (parse → resolve → report). There are no
cycles, no agent handoffs, no conditional loop-backs. LangGraph's value (cyclic state
+ durable checkpointing) applies only at the translation loop. Introducing it earlier
adds ceremony, obscures debuggable Python, and couples the project to a framework
before the loop structure is even designed. Even in M4, Prefect+plain-Python is
a viable alternative to LangGraph; the decision is deferred.

---

## No vectors in M0–M2 (vector discovery deferred)

**Decision:** no embeddings or vector search in M0–M2. Deferred in favour of a
post-hoc consistency report after the first full translation.

**Why:** upfront vector discovery (predicting which terms need locking before translating)
is speculative. Post-hoc consistency checking (finding terms that actually drifted
after translating) is evidence-driven, cheaper, simpler, and more accurate for v1.
After the first full translation, group by latin_lemma over finished Slovak and
surface terms rendered inconsistently — those are the glossary-expansion candidates.
Vectors re-enter in v2 if the evidence warrants it.

---

## Summa-specific build; generality preserved only at the seams

**Decision:** build everything for the Summa. Parsers hardcode the scholastic structure.
Only two generality seams are preserved: opaque locator string; `source.authority_rank` table.

**Why:** premature generalization would mean designing for Contra Gentiles' structure,
French→Slovak pairs, and essay-shaped texts we haven't examined — paying an abstraction
tax on every milestone. The real seams only become clear after translating one full work
and trying to point the pipeline at a second. Ship the Summa first; generalize from
evidence. The two preserved seams cost nothing now and prevent pouring concrete into
the foundation.
