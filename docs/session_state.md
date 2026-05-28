# Session State

## Current Milestone
M0 — complete (pending live source downloads + final commit)

## Overall Status
All code and tests are done. 132/132 tests pass. Sources have NOT been downloaded
yet — acquire scripts are written and tested but not yet run against live sites.

## Key Decisions This Session

### Latin (Corpus Thomisticum)
- Site serves **HTML not XML** (87 `.html` files via `iopera.html` index)
- Structure encoded in `<P TITLE="...">` attributes (arg/s.c./co./ad N)
- `ad arg.` (reply to combined objection) is a valid reply variant
- File list discovered at runtime — no hardcoded list

### Bahounek (cormierop.cz)
- 4 monolithic HTML files (one per pars); no Supplementum — text ends at III ot. 90
- Coordinate tag format: `{pars} ot. N čl. N {part}` where pars ∈ {I, I-II, II-II, III}
- `k N` reply format exists alongside `ad N` (M1 parser must handle both)

### Freddoso (freddoso.com)
- Articles are **PDFs**, not HTML; TOC pages used for coverage mapping
- Coverage: I (118/119 — q99 missing), I-II (114/114), II-II (189/189), III (78/90 — q79–q90 missing)
- `coverage_gaps.json` will record `missing` list for M1 English ingest fall-back

### Dominican Province (newadvent.org)
- URL scheme: pars-digit + zero-padded question (e.g. Prima Pars Q1 = `1001.htm`)
- Total 614 pages (Prima Pars 119 + I-II 114 + II-II 189 + III 90 + Suppl 99 + App I 2 + App II 1)
- Structural check: `body id="{code}.htm"`, `class="summa"`, `div#springfield2`, `h2[id^="article"]`

### Code reviewer findings (all addressed)
- `uv.lock` un-gitignored (reproducibility)
- `.gitkeep` exception added to `sources/` gitignore rule
- `latin.py`: `recover=False` → `recover=True`; dead `except XMLSyntaxError` removed; encoding fixed
- `bahounek.py`: dead `prefix_re` removed; `import random` moved to module level
- `dominican.py` + `freddoso.py`: relative `DEST` → anchored `Path(__file__).resolve().parents[2]`
- Test imports standardised to `from acquire.X import ...` (consistent with `pythonpath = ["src"]`)
- `packages = ["src/acquire"]` in hatchling config (was `["src"]`, collision-prone)

## Files Created/Modified
- `.gitignore`
- `pyproject.toml` — deps, `pythonpath = ["src", "."]`, `packages = ["src/acquire"]`
- `docker-compose.yml`
- `.env.example`
- `style_profile.yaml` (stub)
- `src/acquire/__init__.py`
- `src/acquire/latin.py` + `tests/test_acquire_latin.py` (34 tests)
- `src/acquire/bahounek.py` + `tests/test_acquire_bahounek.py` (24 tests)
- `src/acquire/dominican.py` + `tests/test_acquire_dominican.py` (32 tests)
- `src/acquire/freddoso.py` + `tests/test_acquire_freddoso.py` (26 tests)
- `verify_sources.py` + `tests/test_verify_sources.py` (16 tests)
- `sources/{latin,czech/bahounek,czech/krystal,english/dominican,english/freddoso}/.gitkeep`

## DB State
- Docker container `aquinas-pipeline-db-1` running
- `vector` and `ltree` extensions loaded

## Exact Next Steps
1. Place Krystal docx at `sources/czech/krystal/Teologicka__Suma_u_zus_-_verze_4.docx`
2. Copy `.env.example` → `.env` and fill in DATABASE_URL (already configured for local Docker)
3. Run acquire scripts (can run in parallel in separate terminals):
   ```
   uv run python src/acquire/latin.py
   uv run python src/acquire/bahounek.py
   uv run python src/acquire/dominican.py
   uv run python src/acquire/freddoso.py
   ```
4. Run `uv run python verify_sources.py` — must print all green
5. Commit: `feat(m0): environment scaffold and source acquisition`
6. Begin M1: schema creation (DDL review required before execution — per CLAUDE.md)
