# Data Quality & Anomaly Sentinel

AI-assisted data quality and anomaly detection for **AlTasnimBI** — the PDO / Al Tasnim
well project database. Runs a statistical and LLM layer on top of the deterministic rules
engine, and generates an Excel workbook and a Word report of every data quality issue it
finds.

FastAPI backend · React frontend · SQL Server source.

---

## Status

**Discovery complete. Phases 0/1 (normalise + checks), Phase 4 (LLM enrichment) and Phase 6
(Excel/Word reports) built and verified live end-to-end. Statistics, VERIFY, the agentic
layer, API and frontend are not yet built.**

Run the whole thing today with `python -m app.cli run`: it connects live, normalises,
executes 51 checks (0 errors), narrates every finding and writes an executive summary with
your configured OpenAI model, then produces a real `.xlsx` and `.docx` in `output/<run_id>/`
-- all in one command. `python -m app.cli report --run latest` rebuilds just the reports
from an existing run without re-scanning the database.

**Verified live against a real OpenAI key on 2026-09-08: 46 of 46 findings narrated, 6
incidents correlated, 0 rejected by the citation validator.** Getting to 0 took three real
runs and two real bugs found and fixed along the way -- a validator regex that swallowed a
sentence-ending period as a decimal point (rejecting valid citations like "...section 4."
and dates like "...(2026-09-08)."), and the validator failing to recognise a model's
faithful rounding of a long float (`1.1166666666666667` restated as `1.1167`) or rewording
of a date (`2026-10-19` as "19 October 2026") as the same real data. Both are now covered by
regression tests (`tests/test_llm_validate.py`) that reproduce the exact live failures.

**Model configuration is one variable.** `OPENAI_MODEL` in `.env` is what every LLM job
(narrate/correlate/summarise) uses by default -- four optional `DQ_LLM_MODEL_*` vars exist
only to run one specific job on a different model, and are blank (meaning "use
`OPENAI_MODEL`") unless you set one. No model name is hardcoded in the Python code itself.
If no key is configured, or the API is unreachable, the report still builds correctly from
the deterministic findings alone with a banner saying enrichment was skipped -- verified
both ways.

The check layer concretely: a registry that fails at *registration time* if a check omits
its grain or baseline declaration (`backend/app/sentinel/checks/base.py`); a generator that
builds ~51 checks from `config/column_semantics.yaml` with zero table names in Python
(date-order, future-date, range, minimum -- `generator.py`); and 13 hand-written checks
encoding specific sections of the business rules that no generic invariant can express
(§4 deadlines with the PENDING/GAP split, §7 lifecycle order, §9 WBS weightage, rig
double-booking -- `business_rules.py`). Verified deterministic across repeated runs and
safe to call repeatedly in one long-lived process; `pytest tests/`, 84 tests, no live DB or
API key required.

One correction worth reading before trusting the discovery numbers: building the checks
surfaced the *same* per-row-vs-per-task grain mistake documented in `01c` recurring in new
code, caught this time by the framework itself -- see `01c` §7. The corrected, current
figures for those two checks are materially smaller than the discovery-time numbers quoted
below.

The database has been fully profiled — 81 objects, 936 columns, ~20.1 M rows — and the
findings are written up. Read these before writing any code:

| Document | What it contains |
|---|---|
| [`docs/01-DISCOVERY-FINDINGS.md`](docs/01-DISCOVERY-FINDINGS.md) | Structure: connection, scale, keys, constraints, duplication, placeholders, the four named anomaly classes |
| [`docs/01b-DOMAIN-ANALYSIS.md`](docs/01b-DOMAIN-ANALYSIS.md) | Meaning: wells, activities, employees — formats, vocabularies, mapping chain, 20 further anomaly classes |
| [`docs/01c-BASELINE-AND-GRAIN-CORRECTION.md`](docs/01c-BASELINE-AND-GRAIN-CORRECTION.md) | **Read this before trusting any figure above.** P6-generated dates vs planner-owned `target` dates, the daily-log grain of `task_daily`, and the corrected numbers |
| [`docs/01d-VERIFICATION-OF-EXTERNAL-REPORT.md`](docs/01d-VERIFICATION-OF-EXTERNAL-REPORT.md) | An external profiling report re-measured claim by claim: 8 of 10 correct, but both "critical" items misdiagnosed and missing-data 3.5x overstated. Why the VERIFY stage exists |
| [`docs/01e-GENERATED-INVARIANTS.md`](docs/01e-GENERATED-INVARIANTS.md) | How coverage is achieved: declare column roles once, generate ~929 invariants. Prototype found 5 issues the hand-written catalogue missed, incl. 14,827 inverted dates |
| [`docs/03-ANOMALY-TAXONOMY.md`](docs/03-ANOMALY-TAXONOMY.md) | **The complete anomaly catalogue** — ~165 classes across 14 dimensions, each with its measured value, source and whether it attaches to a `well_id`. Includes the well-centric scorecard |
| [`docs/04-AGENTIC-LIVE-ANALYSIS.md`](docs/04-AGENTIC-LIVE-ANALYSIS.md) | **Every report is computed live, at click-time** — nothing is cached or hardcoded. Plus the agentic investigation layer: a bounded, audited tool-loop that digs into master-data conflicts (e.g. the norms disagreement) beyond what a fixed check can say |
| [`docs/02-FEATURE-PLAN.md`](docs/02-FEATURE-PLAN.md) | The plan: architecture, ~110 checks in 11 families, API, UI, build sequence, open decisions |

---

## What it detects

Four anomaly classes were specified by the business. All four are confirmed present, with
counts measured on 2026-09-07:

| Class | Evidence (verified figures) |
|---|---|
| Rig-off recorded but upstream work unfinished | **422 wells** with the rig off still carry open tasks; worst is **1,258 days** since rig-off with hook-up never recorded |
| Dates not meeting the norm | drilling spans from **−92 days**; **313 of 366** hook-ups past the rig_off+2 deadline; **46 inverted** planner targets |
| Activities 100% complete but the WBS is not | **2,804 of 35,444 tasks (7.9%)** say `completed=0` while `progress >= 1` |
| WBS % wrong, corrupting project progress | WBS weightage totals **44.0 / 65.99 / 270.69** — none is 100; **22% of work maps to no WBS** |

The single largest finding came from the corrected baseline: **39,991 tasks across 449 of
814 wells have a planner `target_end` after their well's `ex_rig_on_date`** — construction
committed to finish after the rig is contractually due (§4).

Discovery added 20 more classes, including **481 impossible rig double-bookings**, phantom
WBS created by line-feed characters in names, entire disciplines (hook-up, location
construction) missing from the activity mapping, **2,181 tasks whose `completed` flag
reverted from 1 to 0**, and a column named `progress` that contains only dates.

> Figures in this table were revised after the business clarified that `target_*` (planner-owned)
> and not `start/end` (P6-generated) is the authoritative baseline, and after `task_daily`
> was confirmed to be a daily log rather than a task list. See
> [`docs/01c`](docs/01c-BASELINE-AND-GRAIN-CORRECTION.md).

---

## Architecture

```
React + Vite frontend
        │  REST + SSE
FastAPI backend
        │
   ┌────┴──────────────────────────────────────────────┐
   │ Phase 0  normalise   snapshot pin · dedup · nulls │
   │ Phase 1  checks      ~1,093 (929 generated)       │
   │ Phase 2  stats       median+MAD · IQR · drift     │
   │ Phase 3  VERIFY      6 gates · downgrade or drop  │
   │ Phase 4  LLM         explain · correlate · rank   │
   │ Phase 5  persist     dq.run / finding / incident  │
   │ Phase 6  reports     .xlsx + .docx                │
   └───────────────────────────────────────────────────┘
        │                              │
  AlTasnimBI (db_datareader)      SQLite  output/sentinel.db
```

**`BIuser` is `db_datareader` only** — measured, not assumed (`CREATE SCHEMA` 0,
`CREATE TABLE` 0, `INSERT` 0). So the connection to production is physically incapable of
writing, and findings persist to a local SQLite file instead of a `dq` schema. That makes
running against production safe by construction rather than by policy.

The LLM never produces a number — counts, ids and dates are injected from deterministic
finding rows after generation. Layers 1 and 2 alone produce a correct report; the LLM is
enrichment, never a correctness dependency.

**Phase 3 VERIFY has no bypass.** Every finding is adversarially re-tested against six gates
— grain, baseline, due-date, column role, an independently written counter-query, and
materiality — and carries `verified_by`, `grain`, `baseline` and `counter_query_count`. A
finding that cannot state those is dropped, with the reason recorded on the run. Three
separate tools, this one twice, produced correctly-computed numbers with wrong conclusions
before this gate existed ([`docs/01c`](docs/01c-BASELINE-AND-GRAIN-CORRECTION.md),
[`docs/01d`](docs/01d-VERIFICATION-OF-EXTERNAL-REPORT.md)).

---

## Getting started

### Prerequisites

- **Conda env `mycuda`** (Python 3.11.15) — already has `fastapi`, `uvicorn`, `pydantic`,
  `openai`, `pyodbc`, `openpyxl`, `python-docx`, `python-dotenv`, `PyYAML`, `numpy`
- **ODBC Driver 18 for SQL Server** — installed; required with
  `Encrypt=yes;TrustServerCertificate=yes`
- **Node 18+** for the frontend

### Install the remaining backend packages

```bash
conda activate mycuda
pip install pandas scipy xlsxwriter matplotlib structlog pydantic-settings sse-starlette alembic pytest pytest-asyncio httpx
```

### Configure

Copy `.env.example` to `.env` and fill in the credentials. The Sentinel uses its own
scope variables (`DQ_ALLOWED_SCHEMAS`, `DQ_EXCLUDED_TABLES`) rather than the chat feature's
`ALLOWED_SCHEMAS` / `EXCLUDED_TABLES` — the latter hides four tables this feature must read.
See `docs/02-FEATURE-PLAN.md` §10.

### Verify the connection

```bash
conda activate mycuda
python others/discovery/01_inventory.py
```

---

## Re-running discovery

The 20 scripts in `others/discovery/` are the evidence base for every number in the docs. They are
read-only and safe to re-run at any time. Output lands in `others/discovery/out/*.json`.

```bash
conda activate mycuda
cd others/discovery

python 01_inventory.py                          # schemas, objects, row counts, scope flags
python 02_columns_keys.py "well.well_master"    # column dictionary, PKs, FKs, constraints
python 03_colsearch.py "progress" "weight"      # keyword search across all 936 columns
python 04_profile.py "well.well_master"         # null% / distinct / min-max / sentinels
python 05_relations.py                          # grain, duplicates, orphans, WBS chain
python 06_wbs_hier.py                           # WBS hierarchy and weightage rollup
python 07_hier2.py                              # parent linkage, 1900-01-01 placeholders
python 08_rules.py                              # the four named business-rule classes
python 09_engine_and_sweep.py                   # existing DQ engine + dead-column sweep
python 10_text_xref.py                          # encoding, sentinels, cross-table names
python 11_norms_xref.py                         # norms / UOM / code disagreement
python 12_domain_wells.py                       # well lifecycle, formats, rig overlaps
python 13_domain_activities.py                  # task_code formats, mapping chain, WBS
python 14_domain_employees.py                   # employees, crews, WBS name collisions
python 15_verify_gaps.py                        # crew join target, dual ids, manhours
python 16_p6_vs_target.py                       # P6 vs planner-target baseline
python 17_grain_check.py                        # task_daily grain; re-measured contradictions
python 18_verify_report.py                      # re-measures an external report's claims
python 19_generated_invariants.py               # generated-invariant engine prototype
python 20_well_centric.py                       # per-well anomaly scorecard, monotonicity
```

`others/discovery/dbx.py` holds the shared connection, `.env` scope parsing and JSON output helpers.

---

## Business rules

`docs/BUSINESS_RULES.md` is authoritative and is fed to the LLM verbatim as ground truth.
Five guards are enforced in code, in the prompt, and in regression tests:

1. **Early is good.** An actual date earlier than expected means the work accelerated (§5).
   Never a defect, never "variance", never an absolute-value "days of delay".
2. **Never invent a construction completion date** — §11 says none is approved.
3. **Ownership is derived.** Location late ⇒ Al Tasnim; Flowline late ⇒ PDO, not a due well
   for Al Tasnim (§6).
4. **Report the stored value.** Where data conflicts with a rule, state both separately (§12).
5. **The LLM never produces a number.**
6. **Nothing prints unverified.** A finding states its grain and its baseline or it is dropped.

Guard 1 is the single easiest way for an LLM layer to produce a wrong, business-damaging
report, which is why the Excel workbook has a dedicated "Ahead of Schedule" sheet for
positive variances.

---

## Layout

```
├── docs/                        discovery findings, domain analysis, corrections, plan, rules
├── others/discovery/            20 read-only exploration scripts -- the evidence for docs/,
│                                 not part of the running app (kept out of backend/ on purpose)
│
├── backend/                     the product: the check engine, LLM enrichment, reports
│   ├── app/
│   │   ├── config.py             pydantic-settings, reads the repo-root .env
│   │   ├── domain/models.py      Finding, Grain, Baseline, Severity, FindingClass, Run
│   │   ├── db/
│   │   │   ├── source.py         READ-ONLY pool to AlTasnimBI + the statement guard
│   │   │   └── store.py          SQLite findings store (BIuser cannot write -- see below)
│   │   ├── sentinel/
│   │   │   ├── scope.py          DQ_ALLOWED_SCHEMAS / DQ_EXCLUDED_TABLES resolution
│   │   │   ├── schema_snapshot.py   schema-drift detection -- see "Nothing is hardcoded"
│   │   │   ├── orchestrator.py   phase sequencing (normalise -> checks -> llm -> reports)
│   │   │   ├── normalise/        Layer 0: snapshot pinning, dedup, placeholder cleanup
│   │   │   ├── checks/           base.py (registry), generator.py, business_rules.py
│   │   │   └── llm/              client.py, prompts.py, schemas.py, validate.py, enrich.py
│   │   │                         -- narrate / correlate / summarise, one model for all
│   │   │                         four jobs by default (OPENAI_MODEL), each independently
│   │   │                         overridable; every LLM number is checked against the
│   │   │                         finding's own evidence before it can print
│   │   ├── reporting/            excel.py (xlsxwriter, 6 sheets), word.py (python-docx)
│   │   └── cli.py                `python -m app.cli doctor|run|show|sql|schema|report`
│   ├── config/
│   │   ├── normalisation.yaml    dedup keys, placeholder values, sentinel strings
│   │   └── column_semantics.yaml column ROLES (owner, baseline, grain) -- reviewed, not coded
│   └── tests/                    84 tests; `pytest tests/` needs no live DB or API key
│
└── frontend/                     React + Vite dashboard and findings register  (to build)
```

### Nothing is hardcoded — new tables and columns are handled automatically

Every table/column reference in the product code comes from one of two places, never from
a literal in Python:

1. **Live schema introspection** (`sys.objects` / `sys.columns`), filtered by `Scope`
   (`DQ_ALLOWED_SCHEMAS` / `DQ_EXCLUDED_TABLES` / `DQ_EXCLUDED_COLUMNS` in `.env`). Add a
   table to an allowed schema and it is in scope on the very next run — no code change.
2. **The two YAML files** under `backend/config/` — the *reviewable* place business rules
   live, editable without touching Python.

Proven, not just asserted — `python -m app.cli schema` captures a fingerprint of every
in-scope table and column and diffs it against the previous run:

```bash
python -m app.cli schema            # capture + diff vs the last run
python -m app.cli schema --persist  # capture and save as the new baseline
```

A table or column that appears between runs is reported automatically as a `PIP-007`
finding (`DESIGN` class — informational, not a defect) with **zero code changes required**.
This was verified end-to-end by simulating drift through `DQ_EXCLUDED_TABLES` /
`DQ_EXCLUDED_COLUMNS` (safe — no DDL touched production) and confirming both a new table
and a new column are detected. A table with no entry in `column_semantics.yaml` still gets
scanned, using conservative defaults (`Grain.row()`, no baseline) until a business owner
reviews it in.

`others/discovery/dbx.py` is a thin wrapper over `app.config` / `app.db.source` /
`app.sentinel.scope` — there is exactly one implementation of the connection string and the
scope resolution in this repository, shared by the exploration scripts and the product.

---

## Safety

- The source account (`BIuser`) is **`db_datareader`** — it cannot create, insert or update,
  verified by `HAS_PERMS_BY_NAME`. `ApplicationIntent=ReadOnly` and a statement guard that
  rejects anything not `SELECT`/`WITH` are belt-and-braces on top. Findings are written only
  to `output/sentinel.db`.
- Large tables are sampled above `DQ_LARGE_TABLE_ROW_LIMIT`; no check scans 17.9 M rows
  unbounded.
- PII (`employee_contact`, `ref.employee.email`, `supervisor_email`, `task_assignee`) is
  excluded from every LLM payload and from report evidence. Employee checks report counts only.
- `.env` is untracked. Never commit credentials.
