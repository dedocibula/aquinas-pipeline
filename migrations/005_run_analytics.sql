-- M5: versioned run analytics — translation_run + run_segment
--
-- Purpose: make structural/incremental pipeline changes measurable across runs
-- (regression vs improvement) instead of manual JSONL forensics. PromptLogger
-- JSONL remains the deep-dive artifact (full prompts/drafts); these tables hold
-- only the queryable dimensions.
--
-- translation_run: one row per flow invocation (translate_corpus / rerun_stale
-- / pilot). Captures the exact code+prompt+glossary state so two runs can be
-- compared apples-to-apples by run_compare.py.
--
-- run_segment: one row per segment processed in a run. failure_classes is a
-- jsonb array of per-iteration failure records written by loop.py at failure
-- time (no post-hoc log parsing), e.g.
--   [{"iter": 1, "class": "precheck_terminology", "term": "rozum"},
--    {"iter": 2, "class": "reviewer_revision"}]
-- Known classes: precheck_terminology, precheck_structure, reviewer_revision,
-- preamble, latin_output, translator_error, reviewer_error. Not CHECK-enforced:
-- classes are produced by one writer (loop.py) and new classes must not require
-- a migration.

CREATE TABLE translation_run (
    run_id            serial PRIMARY KEY,
    flow_name         text NOT NULL,                  -- 'translate_corpus' | 'rerun_stale' | 'pilot'
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,                    -- NULL while running / after crash
    git_sha           text,                           -- code state (short sha)
    prompt_hash       text,                           -- sha256 of translator_system.txt + reviewer_system.txt
    glossary_snapshot jsonb,                          -- {"approved_senses": N, "max_version": N}
    translator_model  text,
    reviewer_model    text,
    temperature       numeric(3, 2),
    filters           jsonb,                          -- {"pars": [...], "max_question": N} or NULL
    max_workers       int,
    total_segments    int,
    total_translated  int,
    total_needs_human int,
    total_cost_usd    numeric(10, 4),
    jsonl_path        text                            -- PromptLogger artifact for deep dives
);

CREATE TABLE run_segment (
    run_id           int NOT NULL REFERENCES translation_run (run_id) ON DELETE CASCADE,
    segment_id       int NOT NULL REFERENCES segment (segment_id),
    final_status     text NOT NULL
        CHECK (final_status IN ('translated', 'needs_human')),
    iterations_used  int NOT NULL,
    chosen_iteration int,                             -- NULL when no draft survived
    cost_usd         numeric(10, 6),
    failure_classes  jsonb,                           -- array of {"iter", "class", ...detail}; NULL = clean pass
    last_feedback    text,                            -- most recent reviewer/precheck feedback
    PRIMARY KEY (run_id, segment_id)
);

-- run_compare.py joins run_segment to itself across two runs by segment_id.
CREATE INDEX idx_run_segment_segment ON run_segment (segment_id);

-- Triage queries filter failures within a run.
CREATE INDEX idx_run_segment_failures ON run_segment (run_id)
    WHERE final_status = 'needs_human';
