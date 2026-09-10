# Data Quality & Anomaly Sentinel

AI-assisted data quality and anomaly detection for **AlTasnimBI** — the PDO / Al Tasnim
well project database. Runs a statistical and LLM layer on top of the deterministic rules
engine, and generates an Excel workbook and a Word report of every data quality issue it
finds.

FastAPI backend · React frontend · SQL Server source.

---

## Status

**Discovery complete. Phases 0/1 (normalise + checks), Phase 4 (LLM enrichment) and Phase 6
(Excel/Word reports) built and verified live end-to-end. A Tier 2 suggestion agent, a
FastAPI backend and a React frontend are also built and verified live. Statistics, VERIFY
and the Tier 3 investigation agent are not yet built.**

Run the whole thing today with `python -m app.cli run`: it connects live, normalises,
executes 55 checks (0 errors), narrates every finding (reusing prose for findings that have not changed since the last run) and writes an executive summary with
your configured OpenAI model, then produces a real `.xlsx` and `.docx` in `output/<run_id>/`
-- all in one command. `python -m app.cli report --run latest` rebuilds just the reports
from an existing run without re-scanning the database. The same pipeline is also reachable
over HTTP -- see [**Running the app**](#running-the-app) below for the backend API and
frontend dashboard, where "Generate Report" does exactly this and gives you the Excel/Word
files to download once it finishes.

**A Tier 2 suggestion agent finds candidate NEW checks beyond the fixed 55.** It explores
the live schema with bounded, read-only tools (`list_tables`/`describe_table`/`run_query`,
same `ReadOnlyGuard` as everything else), verifies each hypothesis by actually re-running
its own proposed SQL, and can only ever *propose* -- nothing it produces becomes a real
check without a human reading the generated `.py.suggested` file, fixing its grain/baseline
TODOs, and renaming it. Verified live across three real sessions, each of which surfaced and
led to a fix for a genuine bug: a trailing-semicolon query-wrapping bug, a proposal that
violated business rule §5 ("early is good", now also blocked server-side by keyword, not
just by prompt instruction), and a proposed check that didn't implement its own hypothesis
(caught in human review, not by the agent). One verified proposal was promoted to a real
check this way -- `BIZ-109` in `business_rules.py`, whose promotion also fixed a real
per-row-vs-per-task grain bug in the agent's own SQL (49 vs the correct 33). See
`python -m app.cli suggest --help`.

**Verified live against a real OpenAI key on 2026-09-08: 46 of 46 findings narrated, 6
incidents correlated, 0 rejected by the citation validator.** Getting to 0 took three real
runs and two real bugs found and fixed along the way -- a validator regex that swallowed a
sentence-ending period as a decimal point (rejecting valid citations like "...section 4."
and dates like "...(2026-09-08)."), and the validator failing to recognise a model's
faithful rounding of a long float (`1.1166666666666667` restated as `1.1167`) or rewording
of a date (`2026-10-19` as "19 October 2026") as the same real data. Both are now covered by
regression tests (`tests/test_llm_validate.py`) that reproduce the exact live failures.

**Model configuration is one variable.** `OPENAI_MODEL` in `.env` is what every LLM job
uses -- narrate, correlate, summarise and the suggestion agent, all four. The optional
`DQ_LLM_MODEL_*` vars exist only to point ONE job somewhere else deliberately, and all
four are blank by default, so out of the box exactly one model runs everything and no job
can quietly use a model you did not ask for. (Two of them used to default to `gpt-5-mini`,
which meant a run configured for `gpt-4o-mini` still made two calls on another model --
visible in the log as a 400 `llm.temperature_unsupported`, since `gpt-5-mini` rejects the
`temperature` parameter. Fixed 2026-09-09; verified live, 52/52 calls on `OPENAI_MODEL`.)
If no key is configured, or the API is unreachable, the report still builds correctly from
the deterministic findings alone with a banner saying enrichment was skipped -- verified
both ways.

The check layer concretely: a registry that fails at *registration time* if a check omits
its grain or baseline declaration (`backend/app/sentinel/checks/base.py`); a generator that
builds 39 checks from `config/column_semantics.yaml` with zero table names in Python
(date-order, future-date, range, minimum -- `generator.py`); and 13 hand-written checks
encoding specific sections of the business rules that no generic invariant can express
(§4 deadlines with the PENDING/GAP split, §7 lifecycle order, §9 WBS weightage, rig
double-booking, logical contradictions -- `business_rules.py`) -- 55 checks total. Verified deterministic across
repeated runs and safe to call repeatedly in one long-lived process; `pytest tests/`, 176
tests, no live DB or API key required.

**Narration is reused across runs, keyed on the data itself.** Narration was ~95% of every
run's tokens and most findings recur unchanged, so a finding's prose is now cached and
reused. The key is a fingerprint of the finding's own measured data (title, count,
evidence, grain, rule ref) **plus the model and a hash of the prompt** — so on live data
the cache is right about change, not just sameness: a partly-fixed anomaly (33 wells → 23)
re-narrates because the prose quotes the number, a fixed one disappears, a new one is
written fresh, and switching `OPENAI_MODEL` or editing `BUSINESS_RULES.md` invalidates
everything rather than mixing two authors' text into one report. Measured live on
consecutive runs: **222,228 tokens / 160s → 10,934 tokens / 44s (95% fewer tokens, 73%
faster)**, and on the run that added three new checks: **52 reused, 3 narrated — exactly
the three new findings**. See `llm/cache.py`; `tests/test_narration_cache.py` covers each
change case.

**The report states how much of the database it actually covered (PIP-010/011/012).** Three
facts the check layer structurally cannot report about itself, all derived from the schema
pass that already runs:

* **PIP-010** — tables that exist but hold no rows *(3: `ref.division`, `ref.location`,
  `ref.project_type`)*. An empty lookup table is a silent failure: anything joining to it
  finds no match and blanks the value rather than erroring.
* **PIP-011** — **this report examined 10 of 74 in-scope tables**; the other 61 hold
  19,939,255 rows and have no declaration, so no check has ever opened them. Stated in the
  report's Scope section as well as the register, because a reader who sees "55 findings"
  would otherwise assume full coverage. Nothing says those tables are dirty — it says
  nobody has looked.
* **PIP-012** — columns that are NULL on every single row *(56 across 13 tables, worst
  `well.well_master` with 20)*. Deliberately a full scan, never a sample: a column can be
  NULL for a million rows and populated later, so a sampled "looks empty" would report
  live fields as dead. Tables above `DQ_LARGE_TABLE_ROW_LIMIT` are skipped rather than
  estimated, and the skip is named in the evidence.

Caught before shipping: PIP-010 first reported **7** empty tables, and 3 were populated
views (742, 36 and 11 rows). `table_row_counts()` covers user tables only (`sys.objects`
type `'U'`) while the schema pass also snapshots views (`'V'`), and the absent count was
defaulted to `0`. Emptiness is now taken only from a **measured** count and each candidate
zero is then confirmed with a real `COUNT(*)` — `sys.partitions` is documented as
approximate, and a medium-severity claim that someone's table is empty should rest on
having counted it. Both failure modes are regression-tested
(`tests/test_schema_coverage.py`).

**Logical contradictions are checked, not just bad values (CON-012/013/014).** Reporting
"51% of `actual_end_date` is 1900-01-01" says the data is wrong; these say what it makes
wrong: an activity at 100% progress with no usable end date *(533 rows)*, work reported
under way with no usable start date *(291)*, and a task at 100% progress whose completion
flag still says not done *(2,812 tasks across 12 wells)*. That last one is one of the four
anomaly classes the business named at the outset and was the only one with no check of its
own — its 2,812 matches the 2,804 measured at discovery.

**Normalisation reports what it rewrites, including at the value level.** Layer 0 nulls
placeholder dates, blank strings and sentinel strings before any check runs, so nothing
mistakes them for real values -- and every one of those rewrites is now counted and
reported as its own finding (`PLC-001` critical, `SNT-001` medium, `BLK-001` low, the
severities declared in `config/normalisation.yaml`). That closes a real hole: those three
rewrites used to happen silently, so `dbo.activity_taskplan_job_progress.actual_end_date`
being **43,232 of 84,790 rows (51%) parked at `1900-01-01`** appeared in the report as
simply "empty", with nothing saying the emptiness was disguised as a real date that an
`IS NOT NULL` completion test would count as finished. Measured live, the added findings
take the run from 7 critical to 9. The measuring predicate is asserted by test to be the
same condition that does the nulling -- if they ever drift, the reported count stops
describing the actual rewrite (`tests/test_normalise_value_cleanups.py`).

One correction worth knowing before trusting the "What it detects" numbers below: building
the checks surfaced the *same* per-row-vs-per-task grain mistake from the original discovery
profiling recurring in new code, caught this time by the framework itself -- the mandatory
grain declaration exists specifically because of that recurrence. The corrected, current
figures for those two checks are materially smaller than the discovery-time numbers quoted
below.

The database was fully profiled during discovery — 81 objects, 936 columns, ~20.1 M rows.
That profiling's own working documents (connection/structure notes, domain analysis, the
baseline/grain correction, the anomaly taxonomy, the feature plan) were discovery-phase
scratch work, not runtime input, and have been removed now that their findings are absorbed
into the working code itself: `config/column_semantics.yaml` (column roles, grain,
baseline), `business_rules.py` (the hand-written checks and their business-rule citations),
and this README. The one document still in the repo, `docs/BUSINESS_RULES.md`, is the one
actually read by the running code -- see [Business rules](#business-rules) below.

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
> was confirmed to be a daily log rather than a task list -- see `Baseline.TARGET` and the
> `task_daily` grain declaration in `config/column_semantics.yaml`, where that correction
> now lives as reviewable config rather than a one-time discovery note.

---

## Architecture

```
React + Vite frontend  (frontend/)
        │  REST, polled  (no websocket/SSE -- a plain GET every ~1.5s while a run is live)
FastAPI backend  (backend/app/api/) -- thin, read-mostly layer over FindingsStore;
        │                             POST /api/runs starts Orchestrator.run() on a thread
Orchestrator (backend/app/sentinel/orchestrator.py) -- same one the CLI's `run` calls
        │
   ┌────┴──────────────────────────────────────────────┐
   │ Phase 0  normalise   snapshot pin · dedup · nulls │
   │                      every rewrite reported, incl.│
   │                      placeholder/blank/sentinel   │
   │ Phase 1  checks      52 (13 hand-written + 39     │
   │                      generated) + a Tier 2 agent  │
   │                      that proposes NEW candidates │
   │ Phase 2  stats       median+MAD · IQR · drift     │  (not yet built)
   │ Phase 3  VERIFY      6 gates · downgrade or drop  │  (not yet built)
   │ Phase 4  LLM         explain · correlate · rank   │
   │ Phase 5  persist     run / finding / incident     │
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

**Phase 3 VERIFY is designed, not yet built** (see the architecture diagram above). The
design: every finding gets adversarially re-tested against six gates — grain, baseline,
due-date, column role, an independently written counter-query, and materiality — and would
carry `verified_by`, `grain`, `baseline` and `counter_query_count`; a finding that can't
state those would be dropped, with the reason recorded on the run. The motivation is
concrete, not hypothetical: earlier profiling attempts against this same database produced
correctly-computed numbers with wrong conclusions (a wrong baseline column, a wrong grain)
that only surfaced on manual re-check — the mandatory `grain`/`baseline` declaration already
enforced at check-registration time (`checks/base.py`) is the first half of closing that
gap; VERIFY is the second half.

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
pip install pandas scipy xlsxwriter matplotlib structlog rich pydantic-settings sse-starlette alembic pytest pytest-asyncio httpx
```

### Configure

Copy `.env.example` to `.env` and fill in the credentials. The Sentinel uses its own
scope variables (`DQ_ALLOWED_SCHEMAS`, `DQ_EXCLUDED_TABLES`) rather than the chat feature's
`ALLOWED_SCHEMAS` / `EXCLUDED_TABLES` — the latter hides four tables this feature must read
(see `backend/app/sentinel/scope.py`).

### Verify the connection

```bash
conda activate mycuda
cd backend
python -m app.cli doctor
```

Prints the resolved config, connects live, and reports the account's actual permissions
(`db_datareader`/`db_owner`/`can_insert`/`can_create_table`) so a misconfigured or
over-privileged connection is caught before any check ever runs.

---

## Running the app

Three ways to run this, from simplest to full-stack. All three call the exact same
`Orchestrator` -- there is one pipeline implementation, not one for the CLI and a different
one for the API.

### 1. CLI only (no servers)

```bash
conda activate mycuda
cd backend
python -m app.cli run                 # full live scan + LLM enrichment + Excel/Word
python -m app.cli run --no-llm --no-reports --tables well.well_master   # fast, scoped, free
python -m app.cli show --run latest   # re-print the findings from the last run
python -m app.cli report --run latest # rebuild just the reports, no re-scan
python -m app.cli suggest --max-calls 18   # run a Tier 2 suggestion-agent session
```

Everything the run does streams to the terminal as it happens, rendered with `rich`
(`app/logging.py`): a clock and colour-coded level per record, values highlighted, and
full-colour tracebacks. Every individual check logs its own line -- `check.pass`,
`check.fail`, and `check.skipped`/`check.error` at **WARNING** so a check that silently
didn't run (a hole in that run's coverage) stands out rather than hiding inside the
aggregate count. Narration logs per finding (`enrich.narrated 12/47 …`), because that
phase is ~4 of the ~6 minutes and would otherwise sit silent.

```bash
DQ_LOG_LEVEL=DEBUG python -m app.cli run   # more detail
DQ_LOG_JSON=true    python -m app.cli run  # one JSON object per line, for shipping to a log store
```

Logs go to **stderr**, the report tables to **stdout**, so
`python -m app.cli run > run.txt` keeps the tables in the file while the live log still
streams to the terminal.

### 2. Backend API only

The API (`backend/app/api/`) is a thin, mostly-read layer over the same `FindingsStore`
`show`/`report` already use, plus one action endpoint: `POST /api/runs` starts a run on a
background thread (a full run takes minutes, so the request returns a `job_id`
immediately and the caller polls `GET /api/jobs/{job_id}` for progress). Nothing in
`app/sentinel`, `app/db`, `app/reporting` or `app/cli.py` was changed to add it.

```bash
conda activate mycuda
cd backend
python -m app.api.main
# or, equivalently, for a production-style launch:
uvicorn app.api.main:app --host 0.0.0.0 --port 8001
```

Interactive API docs (Swagger UI) are then at `http://localhost:8001/docs`. Key endpoints:

| Method & path | What it does |
|---|---|
| `POST /api/runs` | Start a live run (`{tables?, triggered_by?, enrich?, generate_reports?}`) → `{job_id, status}` |
| `GET /api/jobs/{job_id}` | Poll progress: `{status, run_id, phase, phase_state, elapsed_seconds}` |
| `GET /api/runs` | List past runs with severity/class summaries |
| `GET /api/runs/{run_id}` | One run's summary |
| `GET /api/runs/{run_id}/findings` | Findings, filterable by `severity`/`family`/`finding_class` |
| `GET /api/runs/{run_id}/incidents` | Correlated incidents |
| `GET /api/runs/{run_id}/report/excel` `/report/word` | Download the generated files |
| `GET /api/checks` | The hand-written check catalogue |

`DQ_MAX_CONCURRENT_RUNS` (`.env`, default 2) bounds how many runs the API will start at
once; a request beyond that limit gets `409 Conflict` rather than contending for the same
live database connection and SQLite store.

### 3. Backend API + frontend (the full dashboard)

```bash
# terminal 1
conda activate mycuda
cd backend
python -m app.api.main            # http://localhost:8001

# terminal 2
cd frontend
npm install                       # first time only
npm run dev                       # http://localhost:5173
```

Open `http://localhost:5173`. The dashboard lists past runs with their severity
breakdown; **Generate Report** starts a new live run and shows its progress phase-by-phase
(schema → normalise → checks → AI analysis → reports) until the Excel and Word files are
ready to download -- this is the "analyze the data, then produce the report" flow end to
end, verified live in-browser. Click into any run for its full findings register
(searchable/filterable by severity, family and class) and its correlated incidents.

In dev, Vite proxies `/api/*` to `http://127.0.0.1:8001` (see `vite.config.ts`) so there is
no CORS configuration to think about locally -- if you run the backend on a different port,
update that proxy target, or set `API_CORS_ORIGINS` in `.env` and `VITE_API_BASE_URL` when
building the frontend for a real (non-proxied) deployment.

`.claude/launch.json` defines both servers (`backend-api` on port 8001, `frontend` on port
5173) for previewing them in an editor/agent that reads that file, matching the `API_PORT`
default (8001) in `app/config.py` / `.env.example`.

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
├── docs/BUSINESS_RULES.md        the one doc the running code reads -- fed to the LLM
│                                 verbatim as ground truth, and parsed structurally by
│                                 business_rules_index.py for the reports (see below)
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
│   │   │                         -- suggest.py/tools.py: Tier 2 agent, bounded read-only
│   │   │                         tool loop, human-approval gate via .py.suggested files
│   │   ├── reporting/            excel.py (xlsxwriter, 7 sheets) -- the complete working
│   │   │                         data; word.py (python-docx) -- the ~8-page read-once
│   │   │                         document: register table covers every finding, prose
│   │   │                         only for critical/high (NARRATIVE_DEPTH), full text and
│   │   │                         all-severity narration deferred to the workbook
│   │   ├── sentinel/business_rules_index.py   parses BUSINESS_RULES.md's own "## N. Title"
│   │   │                         headers so a report can expand "§4" into "§4 Milestone
│   │   │                         deadlines" and quote the section's real text verbatim
│   │   ├── api/                  FastAPI layer -- main.py, routes.py, jobs.py, schemas.py;
│   │   │                         reads through FindingsStore, POST /api/runs starts the
│   │   │                         same Orchestrator.run() the CLI's `run` calls, on a thread
│   │   └── cli.py                `python -m app.cli doctor|run|show|sql|schema|report|suggest`
│   ├── config/
│   │   ├── normalisation.yaml    dedup keys, placeholder values, sentinel strings
│   │   └── column_semantics.yaml column ROLES (owner, baseline, grain) -- reviewed, not coded
│   └── tests/                    176 tests; `pytest tests/` needs no live DB or API key
│
└── frontend/                     React + Vite + TypeScript + Tailwind dashboard
    └── src/
        ├── lib/api.ts             typed client for backend/app/api
        ├── components/            GenerateReportPanel (the live-progress "generate" flow),
        │                          FindingsTable, IncidentsList, RunsTable, badges
        └── pages/                 Dashboard (run history + generate), RunDetail (findings)
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
