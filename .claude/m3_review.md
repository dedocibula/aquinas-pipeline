# M3 — Glossary Review & Lock

**Status:** build-locked
**Reads:** database.md, decisions.md, sources.md
**Estimate:** 1 day (engineering); theologian review runs in parallel, duration open
**Prerequisite:** M2 complete and accepted

---

## User story
*As the engineer, I need a reliable two-script pipeline that exports the M2 glossary
to a Google Sheet for theologian review and imports approvals back to the DB — so that
a non-technical reviewer can approve or correct Slovak terms without touching code, and
every approval is precisely reflected in the translation pipeline.*

## Objective
Build `export_sheet.py` and `import_approvals.py`. Both are manual-trigger scripts.
M3 is **not a blocking gate** — M4 may begin translating as soon as some terms are
approved. The Sheet is a living review queue; approvals flow in continuously and M4
picks them up via the stale-segment query.

---

## Schema (no migration required)
M2 already wrote everything M3 needs:
- `glossary_term.category` (term/name/formula/prose) — drives Sheet ordering
- `glossary_sense.status` + `glossary_sense.version` — write-back targets
- `sense_rendering(sk, model)` — the M2 DeepSeek proposal the reviewer reads
- `sense_rendering(sk, human)` — the confirmed term write-back creates
- `term_usage.sense_version_used` — stale query reads this

Add `gspread` and `google-auth` to `pyproject.toml`. No other new dependencies.

---

## Steps

### Step 1 — Google credentials
Create a service account in Google Cloud Console with Sheets API enabled.
Store the JSON key at `.secrets/gsheets_service_account.json` (gitignored).
Add `GSHEETS_SPREADSHEET_ID` to `.env.example`.
Share the target Sheet with the service account email.

### Step 2 — export_sheet.py
Exports the M2 dedup roll-up to Google Sheets. Idempotent — safe to re-run;
existing rows are updated in place, new rows appended.

**Query (the dedup roll-up):**
```sql
SELECT
    gt.term_id,
    gs.sense_id,
    gt.latin_lemma,
    gt.category,
    gs.context_label,
    sr_sk.content       AS proposed_slovak,
    sr_cs.content       AS czech_anchor,
    sr_en.content       AS english_cue,
    tu_agg.method       AS resolution_method,
    tu_agg.freq         AS frequency,
    tu_agg.sample       AS sample_locator,
    gs.status,
    gs.version,
    -- group_id: same proposed_slovak + same category → same group
    dense_rank() OVER (
        PARTITION BY gt.category
        ORDER BY sr_sk.content
    )                   AS group_id
FROM glossary_term gt
JOIN glossary_sense gs ON gs.term_id = gt.term_id
LEFT JOIN sense_rendering sr_sk ON sr_sk.sense_id = gs.sense_id AND sr_sk.lang = 'sk'
LEFT JOIN sense_rendering sr_cs ON sr_cs.sense_id = gs.sense_id AND sr_cs.lang = 'cs'
LEFT JOIN sense_rendering sr_en ON sr_en.sense_id = gs.sense_id AND sr_en.lang = 'en'
LEFT JOIN (
    SELECT sense_id,
           mode() WITHIN GROUP (ORDER BY resolution_method) AS method,
           count(*) AS freq,
           min(s.locator_path::text)                        AS sample
    FROM term_usage tu
    JOIN segment s USING (segment_id)
    GROUP BY sense_id
) tu_agg ON tu_agg.sense_id = gs.sense_id
ORDER BY
    CASE gt.category
        WHEN 'term'    THEN 1
        WHEN 'name'    THEN 2
        WHEN 'formula' THEN 3
        WHEN 'prose'   THEN 4
        ELSE 5
    END,
    CASE gs.status WHEN 'flagged' THEN 1 ELSE 2 END,  -- flagged first
    tu_agg.freq DESC NULLS LAST,
    sr_sk.content,        -- near-duplicates cluster here (same Slovak = adjacent rows)
    gt.latin_lemma;
```

**Sheet columns (in order):**

| col | header | notes |
|---|---|---|
| A | `approved` | checkbox; FALSE on export; reviewer ticks to approve |
| B | `latin_lemma` | read-only for reviewer |
| C | `category` | term / name / formula / prose |
| D | `context_label` | NULL shown as blank |
| E | `proposed_slovak` | **editable** — reviewer corrects here if needed |
| F | `czech_anchor` | read-only reference |
| G | `english_cue` | read-only reference |
| H | `resolution_method` | read-only |
| I | `frequency` | read-only; how many segments use this |
| J | `sample_locator` | read-only; one example coordinate |
| K | `sense_id` | hidden column; used by import script as the join key |
| L | `group_id` | hidden column; near-duplicate grouping indicator |
| M | `db_version` | hidden column; the DB version at export time |

**Near-duplicate visual grouping:** rows with the same `group_id` within a category
have the same `proposed_slovak`. The sort order naturally clusters them (sr_sk.content
sort). The reviewer sees `divina / divino / divinus` as three consecutive rows with
identical proposed Slovak and can tick all three in one pass. No special Sheet
formatting is required — the clustering does the work.

**Do not export** `krystal_single` terms with status='approved' to the main tab.
Put them in a separate "Auto-resolved" tab for audit if needed. The reviewer works
only the terms that need attention.

### Step 3 — import_approvals.py
Reads ticked rows from the Sheet and writes approvals back to the DB.
Idempotent — safe to re-run; already-confirmed rows are skipped.

**Logic per approved row:**

```python
def process_approval(row):
    sense_id      = row['sense_id']       # hidden column K
    new_slovak    = row['proposed_slovak'] # possibly edited by reviewer
    sheet_version = row['db_version']      # hidden column M

    # 1. Conflict check — did the DB change while the Sheet was open?
    current = db.get_sense(sense_id)
    if current.version != sheet_version:
        log.warning(f"Version conflict on sense {sense_id}: "
                    f"sheet has {sheet_version}, DB has {current.version}. Skipping.")
        return 'CONFLICT'

    # 2. Write the confirmed term as a NEW row (preserve model proposal for diff)
    db.upsert_sense_rendering(
        sense_id=sense_id, lang='sk', source='human', content=new_slovak
    )

    # 3. Bump version ONLY if Slovak content changed
    model_proposal = db.get_sense_rendering(sense_id, lang='sk', source='model')
    if new_slovak != model_proposal.content:
        db.increment_sense_version(sense_id)   # triggers M4 stale query
        log.info(f"sense {sense_id}: content changed → version bumped")
    else:
        log.info(f"sense {sense_id}: content unchanged → version held")

    # 4. Always update status
    db.update_sense_status(sense_id, status='approved')
    return 'OK'
```

**Conflict handling:** if `sheet_version != current DB version`, the sense was
modified after the Sheet was exported (e.g. a prior import run already approved it).
Log the conflict, skip the row, do not overwrite. Print a summary of conflicts at
the end so the engineer can investigate.

**Run output:**
```
Import complete.
  Approved:   N terms
  Skipped:    N (already confirmed)
  Conflicts:  N (see conflicts.log)
  Version bumped (re-run triggered): N terms
  Version held (no content change):  N terms
```

---

## The re-run trigger
`import_approvals.py` does not execute re-translations — it only bumps versions.
The re-run is triggered when M4 runs (or re-runs). M4's stale query finds segments
whose `sense_version_used` is behind the current version and adds them to the
translation queue. This separation is intentional: approvals and re-translations
are decoupled. The reviewer can batch-approve 50 terms, then run M4 once to
re-translate all affected segments in one pass.

**The stale query (used by M4):**
```sql
SELECT DISTINCT tu.segment_id
FROM term_usage tu
JOIN glossary_sense gs ON tu.sense_id = gs.sense_id
WHERE tu.sense_version_used < gs.version;
```

---

## Technologies
Python 3.12 + uv · psycopg2-binary · gspread · google-auth

New in `pyproject.toml`:
```toml
gspread = ">=6.0"
google-auth = ">=2.0"
```

---

## Deliverables
1. `src/review/export_sheet.py` — runnable, outputs row count exported
2. `src/review/import_approvals.py` — runnable, outputs summary + conflict log
3. `.env.example` updated with `GSHEETS_SPREADSHEET_ID`
4. `.secrets/` added to `.gitignore`
5. `reports/m3_import_summary.txt` — output of first real import run

## Acceptance criteria
- `export_sheet.py` exports all non-krystal_single terms; Sheet opens with correct
  columns and checkbox in col A; near-duplicate rows are adjacent
- `import_approvals.py` on a Sheet with 10 manually ticked rows: correct rows
  get `sense_rendering(sk, human)`, correct version bumps, correct skips
- Re-running `import_approvals.py` on already-confirmed rows is a no-op
- A row where proposed_slovak was edited AND approved: version bumps
- A row where proposed_slovak was NOT edited AND approved: version does NOT bump
- Conflict detection fires correctly when DB version != sheet version
- `Auto-resolved` tab present and populated with krystal_single terms