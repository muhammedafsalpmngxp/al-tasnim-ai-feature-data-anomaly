# Data Quality & Anomaly Sentinel — Production Plan

AI feature: statistical + LLM layer over the deterministic rules engine. Detects data
quality issues and anomalies across `AlTasnimBI`, and generates an Excel workbook and a
Word report. Delivered as a **FastAPI backend + React frontend**.

Read the discovery docs first — every decision here traces to a measured fact in them.
`01c` (baseline/grain correction), `01d` (external-report verification) and `01e`
(generated invariants) contain corrections to earlier figures; they take precedence.

---

## 1. Scope

**Does:** reads the database read-only, normalises known structural traps, runs ~1,093
deterministic checks, applies robust statistics, uses an LLM to explain / correlate /
prioritise, persists findings, serves them through an API and a UI, and exports
`.xlsx` + `.docx`.

**Does not:** write to source tables, or auto-fix anything. Remediation stays human.

**Sequences after** the existing deterministic engine that writes `dbo.DataQualityCheckLog`
(2 checks on 1 table). The Sentinel reads that log to report what the rules layer covered
and does not re-implement it.

### Five behavioural guards

Failure modes that would make the report actively harmful. Each gets a code guard, a prompt
instruction, and a regression test.

1. **Early is good.** `rig_on_date < ex_rig_on_date` means the well accelerated (§5). Never
   a defect, never "variance", never an absolute-value "days of delay".
2. **Never invent a construction completion date.** §11 says none is approved.
   `loc_finish_date` / `const_complete_date` must not be substituted.
3. **Ownership is derived, never guessed.** Location late ⇒ Al Tasnim, penalty risk.
   Flowline late ⇒ PDO, *not* a due well for Al Tasnim (§6).
4. **Report the stored value.** Where data conflicts with a rule, state both separately (§12).
5. **The LLM never produces a number.** Counts, ids and dates are injected from finding
   rows after generation. A digit the model invented fails validation.

---

## 2. Repository layout

```
al-tasnim-ai-feature-data-anomaly/
├── README.md
├── .env                       # not tracked
├── .env.example
├── .gitignore
├── docker-compose.yml
├── docs/
│   ├── 01-DISCOVERY-FINDINGS.md
│   ├── 01b-DOMAIN-ANALYSIS.md
│   ├── 01c-BASELINE-AND-GRAIN-CORRECTION.md
│   ├── 01d-VERIFICATION-OF-EXTERNAL-REPORT.md
│   ├── 01e-GENERATED-INVARIANTS.md
│   ├── 02-FEATURE-PLAN.md
│   ├── 03-ANOMALY-TAXONOMY.md
│   └── BUSINESS_RULES.md          # verbatim; fed to the LLM as ground truth
├── others/discovery/                     # the 19 exploration scripts (kept, they are the evidence)
│
├── backend/
│   ├── environment.yml            # conda env spec
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── migrations/                # dq schema DDL
│   ├── app/
│   │   ├── main.py                # FastAPI app, CORS, lifespan
│   │   ├── config.py              # pydantic-settings, reads .env
│   │   ├── logging.py             # structlog JSON
│   │   ├── api/
│   │   │   ├── deps.py
│   │   │   └── v1/
│   │   │       ├── health.py      GET  /health, /health/db, /health/llm
│   │   │       ├── runs.py        POST /runs · GET /runs · GET /runs/{id} · DELETE
│   │   │       ├── findings.py    GET  /runs/{id}/findings (filter, sort, page)
│   │   │       │                  GET  /findings/{fid} · GET /findings/{fid}/evidence
│   │   │       ├── incidents.py   GET  /runs/{id}/incidents
│   │   │       ├── catalog.py     GET  /checks · PATCH /checks/{id} (enable/severity)
│   │   │       ├── metrics.py     GET  /runs/{id}/scorecard · /trend · /wells
│   │   │       ├── reports.py     POST /runs/{id}/reports · GET .../reports/{fmt}
│   │   │       └── stream.py      GET  /runs/{id}/events  (SSE progress)
│   │   ├── domain/                # pydantic models: Run, Finding, Incident, Check, Scorecard
│   │   ├── db/
│   │   │   ├── source.py          # READ-ONLY pool to AlTasnimBI
│   │   │   ├── store.py           # WRITE pool to dq.*
│   │   │   └── repositories/
│   │   ├── sentinel/              # ---- THE ENGINE ----
│   │   │   ├── orchestrator.py    # run lifecycle, phase sequencing, progress events
│   │   │   ├── scope.py           # DQ_ALLOWED_SCHEMAS / DQ_EXCLUDED_* resolution
│   │   │   ├── normalise/
│   │   │   │   ├── snapshot.py    # pin MAX(Time_Stamp) per project_id
│   │   │   │   ├── dedup.py       # well_progress, activity_task_plan
│   │   │   │   ├── placeholder.py # 1900-01-01, '', sentinels, mojibake, HTML entities
│   │   │   │   └── views.py       # emits the dq.v_* normalised views
│   │   │   ├── checks/
│   │   │   │   ├── base.py        # Check ABC + registry + decorator
│   │   │   │   ├── generator.py   # emits F1-F7 invariants from column_semantics.yaml
│   │   │   │   ├── schema_.py     # SCH-*
│   │   │   │   ├── completeness.py# CMP-*
│   │   │   │   ├── referential.py # REF-*
│   │   │   │   ├── lifecycle.py   # BIZ-1xx
│   │   │   │   ├── norms.py       # BIZ-2xx
│   │   │   │   ├── wbs.py         # WBS-*
│   │   │   │   ├── activity.py    # ACT-*
│   │   │   │   ├── masterdata.py  # MDM-*
│   │   │   │   ├── duplication.py # DUP-*, PLC-*
│   │   │   │   ├── resource.py    # RES-*  (rig overlap, crew, manhours)
│   │   │   │   └── format_.py     # FMT-*  (vocabulary drift, wrong concept, units)
│   │   │   ├── stats/
│   │   │   │   ├── robust.py      # median + MAD, IQR
│   │   │   │   ├── drift.py       # vs dq.metric baseline
│   │   │   │   └── timeseries.py  # snapshot deltas, progress velocity
│   │   │   └── llm/
│   │   │       ├── client.py      # provider abstraction (LLM_PROVIDER)
│   │   │       ├── prompts.py
│   │   │       ├── schemas.py     # structured-output JSON schemas
│   │   │       └── validate.py    # citation + no-invented-numbers enforcement
│   │   ├── reporting/
│   │   │   ├── excel.py           # xlsxwriter, 16 sheets
│   │   │   ├── word.py            # python-docx, 10 sections
│   │   │   └── charts.py          # matplotlib -> PNG
│   │   └── workers/
│   │       └── runner.py          # thread-pool job runner + cancellation
│   └── tests/
│       ├── conftest.py
│       ├── test_checks/           # one test per check, fixture-driven
│       ├── test_guards.py         # the five guards above
│       ├── test_normalise.py
│       └── golden/                # LLM regression set
│
└── frontend/
    ├── package.json
    ├── vite.config.ts
    ├── tsconfig.json
    ├── tailwind.config.ts
    └── src/
        ├── main.tsx
        ├── App.tsx
        ├── api/
        │   ├── client.ts          # typed fetch wrapper
        │   └── types.ts           # generated from OpenAPI
        ├── hooks/                 # TanStack Query hooks
        ├── pages/
        │   ├── Dashboard.tsx      # scorecard, severity split, trend, top incidents
        │   ├── RunNew.tsx         # pick families/tables, LLM on/off, start
        │   ├── RunProgress.tsx    # live phase progress via SSE
        │   ├── Findings.tsx       # the register: filter, sort, paginate, export
        │   ├── FindingDetail.tsx  # evidence rows, LLM explanation, SQL, remediation
        │   ├── Incidents.tsx      # correlated root causes
        │   ├── WellScorecard.tsx  # 814 wells x issue families
        │   ├── CheckCatalog.tsx   # all checks, toggle, severity override
        │   └── Reports.tsx        # download .xlsx / .docx, history
        └── components/
            ├── SeverityBadge.tsx  DataTable.tsx  ScoreGauge.tsx
            ├── EvidenceTable.tsx  TrendChart.tsx  PhaseTimeline.tsx
```

---

## 3. Backend architecture

```
POST /api/v1/runs ──▶ runner (thread pool) ──▶ orchestrator
                                                   │
   ┌───────────────────────────────────────────────┴──────────────────────────┐
   │ Phase 0  NORMALISE   snapshot pinning · dedup · placeholders             │
   │ Phase 1  CHECKS      ~1,093 (929 generated), parallel by family          │
   │ Phase 2  STATS       robust outliers + drift vs dq.metric                │
   │ Phase 3  VERIFY      6 gates — grain · baseline · due-date · role ·      │
   │                      counter-query · materiality.  Downgrade or drop.    │
   │ Phase 4  LLM         explain · adversarial pass · correlate · prioritise │
   │ Phase 5  PERSIST     dq.run / dq.finding / dq.incident / dq.metric       │
   │ Phase 6  REPORTS     .xlsx + .docx to disk, path recorded on the run     │
   └──────────────────────────────────────────────────────────────────────────┘
                                    │
   each phase emits progress ──▶ SSE ──▶ GET /api/v1/runs/{id}/events
```

**Phase 3 VERIFY is not optional and has no bypass.** Three independent tools — including me,
twice — produced correctly-computed numbers with wrong conclusions by profiling without
context (`01c-BASELINE-AND-GRAIN-CORRECTION.md`, `01d-VERIFICATION-OF-EXTERNAL-REPORT.md`).
Every finding is adversarially re-tested before it can print, and carries `verified_by`,
`grain`, `baseline` and `counter_query_count`. A finding that cannot state those is dropped
with the reason recorded on the run.

### Finding classes — typed, not only severity-ranked

A flat severity list is what lets a non-issue sit at the top of a report. Every finding
carries a class as well as a severity:

| Class | Meaning | Appears in report as |
|---|---|---|
| `VIOLATION` | a business rule broken on real records | actionable |
| `DEFECT` | data internally impossible | actionable |
| `GAP` | something genuinely due is absent | actionable |
| `PENDING` | absent but **not yet due** | normal — counted, own section |
| `DESIGN` | how the system works, not a fault | normal — counted, own section |
| `RISK` | not wrong yet, will bite | advisory |
| `REVIEW` | statistical outlier, a question for a human | advisory |

`PENDING` and `DESIGN` still print, in their own sections, so a reader can see they were
examined and cleared — that is what stops the next reviewer re-raising them as critical.
The measured impact: of 1,278 "missing" schedule dates, **916 are `PENDING`** (wells
correctly awaiting a future milestone) and only **362 are `GAP`**.

A full run takes minutes, so it is **always asynchronous**: `POST /runs` returns
`202 Accepted` with a `run_id`; the UI follows SSE and falls back to polling
`GET /runs/{id}`. Phases 1–3 are independently skippable
(`?families=BIZ,WBS`, `?llm=false`) so a targeted re-run is fast.

**Thread pool, not Celery.** No Redis or broker is in play, and the workload is a handful of
long IO-bound runs, not high throughput. `ThreadPoolExecutor` with run state in `dq.run`
gives cancellation and restart-safety without new infrastructure. If concurrent scheduled
runs are needed later, swap `workers/runner.py` for a Celery task — the orchestrator does
not change.

### Two connection pools, deliberately separated

| Pool | Target | Mode |
|---|---|---|
| `db/source.py` | `AlTasnimBI` | **read-only** — `ApplicationIntent=ReadOnly`, statement guard rejects anything not `SELECT`/`WITH`, per-query timeout, `MAXDOP 2` |
| `db/store.py` | `dq` schema | read-write, the only pool that may write |

The source pool physically cannot write. That is the safety property that lets this run
against production.

### Persistence — SQLite, not a `dq` schema

**Measured 2026-09-07: `BIuser` is `db_datareader` only.**

| Permission | Result |
|---|---|
| `CREATE SCHEMA` | **0** |
| `CREATE TABLE` | **0** |
| `INSERT` | **0** |
| `SELECT` | 1 |
| roles | `db_datareader` only |

So a `dq` schema inside `AlTasnimBI` is **not possible** with these credentials, and the
feature must not require new grants on a production database to work.

**Findings persist to a local SQLite file** at `${DQ_OUTPUT_DIR}/sentinel.db`. That is a
better fit than it first sounds: findings are thousands of rows not millions, there is a
single writer (the run), the API is the only reader, it is fully SQL-queryable, needs zero
infrastructure, and the whole result set is one portable file that can be attached to an
email or copied to a laptop.

It also makes the safety property absolute rather than merely enforced: **the only
connection to `AlTasnimBI` is physically read-only, because the account cannot write.**

The table shapes below are unchanged — they are created in SQLite instead of `dq`. All
access goes through `db/store.py` behind a repository interface, so if the business later
provisions a writable database (a separate `AlTasnimDQ`, or a grant for a new account),
swapping the store is a single-file change and the schema migrates as-is.

### Table shapes (SQLite; identical if later moved to SQL Server)

```sql
dq.run       (run_id PK, started_at, finished_at, status, triggered_by,
              db_name, snapshot_pinned_json, scope_json, rows_scanned,
              checks_run, checks_skipped, llm_model, llm_tokens, llm_cost_usd,
              xlsx_path, docx_path, error_text)

dq.finding   (finding_id PK, run_id FK, check_id, family, severity,
              entity_type, entity_id, entity_label,      -- e.g. well 36754 / SONRAKDS5277
              affected_count, evidence_json, sql_text,
              business_rule_ref, owner,                    -- PDO | AlTasnim | Unassigned
              llm_explanation, llm_root_cause, llm_remediation,
              status, first_seen_run_id, resolved_at)      -- lifecycle across runs

dq.incident  (incident_id PK, run_id FK, title, root_cause, severity,
              finding_ids_json, llm_narrative)

dq.metric    (run_id FK, table_name, column_name, metric_name, metric_value)
```

`dq.metric` is what makes run *n+1* better than run *n* — it is the baseline the drift
detector compares against, so the second run can say "null rate on this column jumped from
2% to 60% since 2026-09-07". That catches a broken pipeline the day it breaks.

`first_seen_run_id` + `status` give findings a lifecycle: **new / recurring / resolved**.
The UI's most useful screen is "what is new since last week", which needs this.

---

## 4. Layer 0 — Normalisation

The four structural traps from the findings. Without this, every downstream number is wrong.

| Trap | Normalisation | Also emitted as |
|---|---|---|
| `core.engineering_task_plan` is a 55-snapshot table | pin `MAX(Time_Stamp)` **per `project_id`** — exact timestamp, not date | snapshot staleness |
| `well.well_progress` 98,771 duplicate rows | `ROW_NUMBER() PARTITION BY (well_id, week_number) ORDER BY progress_id DESC` | `DUP-001` critical |
| `dbo.activity_task_plan` 200–2,718× duplicated, no timestamps | business-key hash dedup; prefer `activity_taskplan_job_progress` | `DUP-002` critical |
| `1900-01-01` in 86,357 date cells | `NULLIF(col,'1900-01-01')` before every check | `PLC-001` critical |
| `''` vs NULL | `NULLIF(LTRIM(RTRIM(col)),'')` — **before** any type-cast test | `BLK-001` |
| sentinels `'NO FLAF'`, `'#N/A'`, `'N/A'`, `'TBC'` | map to NULL for analysis | `SNT-001` |
| WBS names with `CHAR(10)` / trailing space | normalise whitespace **for grouping only** | `WBS-013` critical |
| `m<?>` mojibake, `&amp;` entities | decode for display only | `ENC-001` |

Two rules that matter:

- **Every rewrite is itself a finding.** Counted and reported. That is the honest way to say
  "98,771 rows had to be removed before your data could be analysed."
- **Blank-before-cast.** `TRY_CAST('' AS date)` returns `1900-01-01` in SQL Server. Test for
  blank first or 127 `ssfd_value` empty strings count as valid dates.

Dedup keys, placeholder values and sentinel lists live in `config/normalisation.yaml` so a
business user can review them without reading SQL.

---

## 5. Layer 1 — Check catalogue (~1,093 checks, 3 sources)

Checks come from **three** sources, not one. See `01e-GENERATED-INVARIANTS.md` for the
prototype and its results.

```
config/column_semantics.yaml  (roles, reviewed by the business)
        └──▶ generator ──▶ ~929 invariant checks      COVERAGE
docs/BUSINESS_RULES.md
        └──▶ hand-written ──▶ ~36 rule checks         MEANING
discovery findings
        └──▶ domain checks ──▶ ~74 checks             THE KNOWN
LLM discovery pass ──────────▶ unbounded              THE UNKNOWN
```

| Family | Invariants | Source |
|---|---|---|
| F1 date order, same-role `start <= end` | 101 | generated |
| F2 date order, cross-role (target/actual/plan) | 651 | generated |
| F3 actual date not in the future | 4 | generated |
| F4 percentage / weightage in range | 17 | generated |
| F5 quantity / measure `>= 0` | 31 | generated |
| F6 `*_id` orphan where no FK is declared | 125 | generated |
| F7 content contradicts type or name | 936 cols scanned | generated |
| F8–F10 lifecycle · WBS rollup · deadlines | ~36 | hand-written |

**Generated gives coverage; hand-written gives meaning.** 82 date columns yield 752
candidate date-order pairs — `well_master` alone has 36 columns and 630 pairs. No one
hand-writes those correctly, and any hand-picked subset is a guess about where problems are.

Running F1 alone found **45 violated invariants over 16,532 rows**, including five issues
the hand-written catalogue had missed — the largest being **14,827 rows in
`core.engineering_task_plan` with `actual_start > actual_end`**, and a
**−739,700-day** span in `dsq.drilling_sequence`, a table the hand-written pass never opened.

**Cost control:** one `SELECT` per table computes every invariant for that table in a single
scan, so `well_master`'s 630 pairs cost one scan of 814 rows. Batching makes the generated
approach cheaper than the hand-written one. Invariants with 0 violations are recorded as
`PASS` metrics, not findings — but they **do** print in the Check Catalogue sheet, so a
reader can see what was tested and found clean.

**Sub-causes, not just rule ids.** `ACT-006` splits into `/A` off-by-days (42 tasks),
`/B` off-by-weeks (5), `/C` year typed +1 (2), each with its own row list and remediation.
Comparing each actual against its planner target identifies the culprit column: on **42 of
51** inverted tasks `actual_start` equals `target_start` exactly, so the **end** date is the
wrong one. The report says which date to fix, not "please investigate".

### The domain checks found during discovery (~74)

Each check is a class with `id`, `family`, `severity`, `business_rule_ref`, `title`,
`why_it_matters`, and a SQL body returning `(entity_type, entity_id, evidence_json)`.
One registry feeds the API, the UI catalogue, the Excel sheet and the tests.

| Family | Prefix | Checks | Headline measured value |
|---|---|---|---|
| Schema & metadata | `SCH` | 8 | 19 tables without a PK; **0 CHECK constraints**; 8 `text` columns |
| Completeness | `CMP` | 9 | 80 of 936 columns fully NULL; 3 FK columns 100% NULL |
| Referential & identity | `REF` | 10 | 184 orphan wells; **23,877 rows unmapped to a WBS** |
| Lifecycle | `BIZ-1xx` | 12 | **422 wells rig-off with 40,127 open tasks** |
| Norms & deadlines | `BIZ-2xx` | 10 | **−92-day drilling span**; 313 of 366 hook-ups late |
| WBS & progress | `WBS` | 14 | Σ weightage **44.0 / 65.99 / 270.69**; 2 phantom WBS |
| Activity contradictions | `ACT` | 10 | **42,831 rows `completed=0` but `progress>=1`** |
| Master data | `MDM` | 9 | norms disagree on 31%; 100% code-scheme mismatch |
| Duplication & placeholders | `DUP`,`PLC` | 9 | 98,771 excess rows; 2,718× duplication |
| **Resource plausibility** | `RES` | 10 | **481 rig double-bookings**; 1,162,384 manhours on one task |
| **Format & semantics** | `FMT` | 11 | `progress` holds dates; `fl_dia` has 12 spellings |

The two new families come entirely from the domain pass:

**`RES-*` — resource plausibility**

| id | Check | Measured |
|---|---|---|
| RES-001 | **Rig on two wells simultaneously** | 481 pairs, 16 rigs |
| RES-002 | Manhours implausible (> 24 h/day, > 10,000 on a task) | max 1,162,384 |
| RES-003 | Negative manhours / hours / quantity | −45.07, −2.5, −1,992 |
| RES-004 | Column populated but all-zero (no information) | `manhoursactual`, 128,641 rows |
| RES-005 | Repeated-digit magnitude (entry error) | `planned` = 1,011,111 |
| RES-006 | Employee in implausibly many crews | 51 crews for one employee |
| RES-007 | Crew without supervisor / without employees | 1,307 / 1,267 of 7,582 |
| RES-008 | Two sources for one relationship disagree | `ref.crew.employees` vs bridge (4.9%) |
| RES-009 | Status field never maintained | 2 of 18,476 employees inactive |
| RES-010 | Attribution missing on the fact grain | `crew_type_id` 99.3% NULL |

**`FMT-*` — format & semantics**

| id | Check | Measured |
|---|---|---|
| FMT-001 | Text column is 100% numeric | `buffer_status`, 814 rows |
| FMT-002 | Text column is 100% dates | `progress`, 111 rows |
| FMT-003 | Unit embedded in value / contradicts column name | `fl_length_m` = `"1 km"` |
| FMT-004 | Vocabulary drift within a column | `fl_dia` 12 spellings |
| FMT-005 | Wrong concept in column | `Coriolis` in `fl_dia` |
| FMT-006 | Control characters in a grouping key | 7 WBS with `CHAR(10)` |
| FMT-007 | Values collapsing on normalisation (phantom keys) | 37 WBS → 35 |
| FMT-008 | Spelling variants in reference data | `Commisoning Assistance` |
| FMT-009 | Un-decoded HTML entities | 20,269 rows |
| FMT-010 | Mojibake | `m<?>` for `m²`, 60 rows |
| FMT-011 | Malformed composite key | `G01-07-01-54321`, 6 rows |

**`REF-004` gets special treatment.** The mapping chain does not fail at random — it fails
by discipline. `HUP` (hook-up), `LOC` (location construction), `OHL` and every conversion /
re-entry / re-work / reopen prefix are **100% unmapped**, while `FLME`/`FLCV`/`FLEI` are
97%+ mapped. Two of the five §4 milestones therefore cannot be rolled up to a WBS at all.
The finding must report *per discipline*, not as one 22% number — the shape is the insight.

**`REF-009`** records the corrected crew join: `activity_master_csv.crew_code` matches
`ref.crew_type.crew_type_code` (53 of 57), **not** `ref.crew.code` (0 of 57). Business rule
§3 names the wrong target; the report states the rule and the measured reality separately
per guard #4.

---

## 6. Layer 2 — Statistical detection

Deterministic checks answer "is this illegal?". Statistics answer "is this unusual?".

| Method | Applied to | Detects |
|---|---|---|
| **Median + MAD z-score** | drilling span, hook-up lag, duration per `activity_id` | under/over-calculated dates — the user's rule 2 |
| IQR fence (1.5× / 3×) | `data_qty`, `data_hours`, `required`, `planned` | implausible magnitudes |
| Norm-vs-actual ratio | `data_qty / (norms × duration)` per `activity_id` | work booked far off its productivity norm |
| Plan-vs-actual ratio | `target` span vs `actual` span | 3,836 over-calculated, 754 under-calculated |
| Internal consistency | `duration` column vs `target_end − target_start` | 32,960 rows disagree (39.7%) |
| Snapshot delta | `engineering_task_plan` across 55 snapshots | progress going backwards, weightage churn |
| Progress velocity | deduped `well_progress` per well per week | impossible jumps, flat-lines, regressions |
| Null-rate / cardinality drift | every column vs `dq.metric` | silent pipeline breakage |
| Cross-source divergence | norms, UOM, well name, dates in 2+ tables | disagreeing sources of truth |

**MAD, not standard deviation.** Every large peer group is contaminated — `FLCV1260` runs
−69 to 65 days, `FLME1040` reaches 247 against an 18-day mean. A σ-based fence would widen
until it caught nothing. This is measured, not assumed.

Every statistical finding is `severity = review`. An outlier is a question, not a defect.
And per guard #1, an outlier in the *early* direction is labelled "ahead of schedule".

**A stated scope limit:** norm-based productivity checks cannot use `task_daily` —
`daily_actual_hours` and `daily_actual_quantity` are 100% NULL. They fall back to
`data_hours`/`data_qty` (59% coverage, contains negatives) or `job_progress.actual_manhours`
(100% coverage, range −45 to 1,162,384). Both need outlier gating first. The report says so
in the "Scope & Limitations" section rather than quietly producing a number nobody can trust.

---

## 7. Layer 3 — LLM semantic layer

`LLM_PROVIDER=openai`, temperature 0, JSON-schema-constrained structured output.

> **This layer splits into two sub-phases.** 4a is a bounded, audited *agentic* tool loop
> that investigates master-data conflicts and newly-discovered schema (new table/column)
> beyond what any fixed check can say -- full design, tool interface, budgets, and the
> worked norms-conflict example in `docs/04-AGENTIC-LIVE-ANALYSIS.md`. 4b is the narration
> job described below, which reads the now-enriched finding set. Both are additive: Phases
> 0-3 alone still produce a correct report if 4a/4b are disabled or fail.
>
> **Every number anywhere in this layer -- deterministic, statistical, or agentic -- is
> computed live at click-time against the current database.** Nothing here is a cached or
> hardcoded figure; see `docs/04-AGENTIC-LIVE-ANALYSIS.md` §1.

### Six jobs (Phase 4b -- narration)

1. **Explain** — two business sentences per finding, for a project manager.
2. **Root-cause hypothesis** — e.g. *"`completed` and `progress` are written by different
   pipelines; 40% disagreement suggests one is never updated."*
3. **Correlate into incidents** — the highest-value job. Real example from the data:
   `DUP-002` (2,718× duplication) + `PLC-003` (audit columns NULL) + `REF-007` (cross-well
   contamination) are **one** incident — *the `activity_task_plan` sync has no dedup key*.
   Fixing one fixes three.
4. **Prioritise** — a wrong WBS weight distorts a live project's reported progress; a dead
   column in a legacy table does not.
5. **Discover unencoded anomalies** — given the column dictionary + profile, find what the
   catalogue missed. Discovery proved this works: a column named `progress` holding dates,
   `fl_length_m` holding kilometres, and `Coriolis` in a diameter field are semantic errors
   no generic rule catches.
6. **Remediation + owner** — concrete fix, and PDO vs Al Tasnim per §6.

### Token discipline

936 columns and 20 M rows cannot go to a model. What does go:

- column dictionary (name, type, null%, distinct, min/max) — ~40 KB
- one row per finding: id, counts, severity, rule ref
- **≤ 5 example rows per finding**, keys and relevant columns only
- `docs/BUSINESS_RULES.md` verbatim — the ground truth for interpretation

Never: raw table dumps, `nvarchar(max)` payloads, `remarks`, or any of
`employee_contact` / `ref.employee.email` / `supervisor_email` / `task_assignee`.

~60–90 K tokens per enrichment run. Findings are batched by family so one oversized family
cannot blow the budget. Token count and cost are recorded on `dq.run`.

### Guardrails

- **Every sentence cites a finding id.** Uncited claims are dropped at validation, not printed.
- **No LLM-authored numbers.** Counts are injected post-generation; a digit not present in
  the finding row fails validation.
- **Guard prompt** carries §5, §6, §11, §12 verbatim plus a worked example of the
  early-date trap.
- **Deterministic fallback.** LLM down or schema-invalid ⇒ the report still builds from
  Layers 1–2 with template prose and a visible banner.
- **Golden set** — ~20 hand-labelled findings including 3 early-date cases that must *not*
  be flagged. Runs as a gate on every prompt change.

---

## 8. Reports

### Excel — `Data_Anomaly_Report_<YYYYMMDD>.xlsx`

| # | Sheet | Content |
|---|---|---|
| 1 | Executive Summary | run metadata, DQ score per dimension, counts by severity, top 10 incidents |
| 2 | Findings Register | one row per finding, autofilter, frozen header, hyperlinked to detail |
| 3 | Incidents | correlated root causes + member finding ids |
| 4 | New Since Last Run | the diff — what appeared, what resolved |
| 5 | Well Scorecard | 814 wells × issue families, with stage and dates |
| 6 | Date & Lifecycle | BIZ-1xx / BIZ-2xx with the actual dates side by side |
| 7 | WBS & Progress | weightage sums, rollup mismatches, phantom WBS, unmapped impact |
| 8 | Mapping Coverage | **unmapped work per discipline** — the HUP/LOC/OHL gap |
| 9 | Activity Contradictions | ACT-* with the disagreeing values in adjacent columns |
| 10 | Master Data Conflicts | norms/UOM/code disagreement side by side per activity |
| 11 | Resource Plausibility | rig overlaps, manhour outliers, crew gaps |
| 12 | Format & Semantics | wrong-type, wrong-unit, vocabulary drift |
| 13 | Duplicates & Placeholders | excess-row counts, 1900-01-01 tallies |
| 14 | Column Health | all 936 columns — null%, distinct, dead/frozen/mistyped flags |
| 15 | Statistical Outliers | score, peer group, direction, threshold used |
| 16 | **Ahead of Schedule** | positive variances — explicitly *not* defects (§5) |
| 17 | Check Catalogue | all ~1,093 checks: ran / PASSED / skipped, and why — passes print too |
| 18 | Scope & Undefined Rules | §11 gaps, and what could not be assessed (e.g. manhours) |

Conditional formatting by severity; count cells hyperlink to their detail sheet.

### Word — `Data_Anomaly_Report_<YYYYMMDD>.docx`

1. Title + run metadata (DB, snapshot pinned, rows scanned, generated at)
2. Executive summary — one page, DQ score, the five issues that matter most
3. Scope & method — what was audited, what was normalised first, what was excluded and why
4. Findings by severity — critical / high / medium / review, each with evidence table,
   business impact, remediation, owner
5. Incident analysis — correlated root causes
6. Business-rule compliance — one subsection per §4–§9 with pass/fail
7. Data quality scorecard — completeness / validity / consistency / uniqueness / timeliness /
   referential integrity, per table
8. Positive findings — wells ahead of schedule
9. Undefined rules and open decisions
10. Appendix — check catalogue, column health, SQL per finding

Charts: matplotlib → PNG → embedded.

---

## 9. Frontend

React 18 + Vite + TypeScript + Tailwind + TanStack Query + Recharts. Types generated from
the backend's OpenAPI schema, so the API contract cannot drift from the UI.

| Page | Purpose |
|---|---|
| **Dashboard** | DQ score gauge, severity split, trend across runs, top incidents, "new since last run" |
| **New Run** | pick families / tables, LLM on-off, start; shows the estimated scan cost |
| **Run Progress** | live phase timeline via SSE, per-family check counts, cancel |
| **Findings** | the register — filter by family/severity/table/owner/status, sort, paginate, export selection |
| **Finding Detail** | LLM explanation, root cause, remediation, owner, the evidence rows, the SQL, history across runs |
| **Incidents** | correlated root causes, member findings, LLM narrative |
| **Well Scorecard** | 814 wells × issue families, drill into one well's full picture |
| **Check Catalogue** | all ~1,093 checks (929 generated), toggle enable, override severity, last-run result |
| **Reports** | download .xlsx / .docx, run history |

Design constraints worth naming: the Findings register must stay usable at ~1,500 rows
(virtualised table, server-side paging), and every count in the UI must be clickable
through to its evidence rows — a number the user cannot verify is a number they will not
trust.

---

## 10. Configuration

`.env` additions. The existing `EXCLUDED_TABLES` is tuned for the text-to-SQL chat feature
and hides four tables this feature must read, so the Sentinel gets **parallel** variables
rather than editing yours:

```ini
# --- Sentinel scope (independent of the chat feature) ---
DQ_ALLOWED_SCHEMAS=dbo,dsq,project,ref,well,core,bridge,wbs
DQ_EXCLUDED_TABLES=dbo.sysdiagrams,well.well_details,well.wmr_conversion
DQ_LARGE_TABLE_ROW_LIMIT=2000000
DQ_SAMPLE_PERCENT=2

# --- LLM ---
DQ_LLM_MODEL=gpt-4o
DQ_LLM_MAX_TOKENS_PER_BATCH=90000
DQ_LLM_ENABLED=true

# --- Storage / output ---
# BIuser is db_datareader only - findings go to SQLite, never to AlTasnimBI
DQ_STORE=sqlite
DQ_STORE_PATH=./output/sentinel.db
DQ_OUTPUT_DIR=./output

# --- API ---
API_HOST=0.0.0.0
API_PORT=8000
API_CORS_ORIGINS=http://localhost:5173
DQ_MAX_CONCURRENT_RUNS=2
```

`landing` and `test` stay out — `test.new_db` and `test.rig_well` are scratch. `wbs` comes
**in**, because `wbs.WBS_master` (81,846 rows) is the WBS code registry.

### Environment

Conda env `mycuda` (Python 3.11.15) already has: `fastapi`, `uvicorn`, `pydantic`, `openai`,
`pyodbc`, `openpyxl`, `python-docx`, `python-dotenv`, `PyYAML`, `numpy`.

Still to install:

```bash
conda activate mycuda
pip install pandas scipy xlsxwriter matplotlib structlog pydantic-settings sse-starlette alembic pytest pytest-asyncio httpx
```

ODBC Driver 18 for SQL Server is present and required (`Encrypt=yes;TrustServerCertificate=yes`).

---

## 11. Safety

- **Read-only source pool** — `ApplicationIntent=ReadOnly`, statement guard rejecting
  anything not `SELECT`/`WITH`. Writes go only through the separate `dq` pool.
- **Query budget** — per-query timeout, `LOCK_TIMEOUT`, `MAXDOP 2`, mandatory `TABLESAMPLE`
  above `DQ_LARGE_TABLE_ROW_LIMIT`. No check scans 17.9 M rows unbounded.
- **PII** — `dbo.employee_contact`, `ref.employee.email`, `supervisor_email`,
  `task_assignee` excluded from every LLM payload and from report evidence. Employee checks
  report counts only.
- **Secrets** — `.env` untracked; `.gitignore` covers `.env`, `output/`, `others/discovery/out/`,
  `node_modules/`, `__pycache__/`.
- **API** — the UI is internal; add auth before any external exposure. `POST /runs` is
  rate-limited by `DQ_MAX_CONCURRENT_RUNS`.

---

## 12. Build sequence

| Phase | Deliverable | Why here |
|---|---|---|
| **1** | SQLite store + migrations + read-only pool + config + Layer 0 normalisation | nothing is measurable until snapshots are pinned and duplicates removed |
| **2** | Check framework + families `SCH`, `CMP`, `REF` + `GET /findings` | broad, cheap, no business-rule risk — proves the pipeline end to end |
| **3** | FastAPI skeleton + `POST /runs` + SSE + frontend Dashboard & Findings | make it visible early; the UI drives what the API needs |
| **4** | Guard tests **then** `BIZ-1xx` + `BIZ-2xx` + Excel sheets 6, 16 | highest-value rules; guards written before the checks they protect |
| **5** | `WBS` + `ACT` + sheets 7, 8, 9 | hardest logic; depends on Phase 1 snapshot pinning being right |
| **6** | `MDM` + `DUP`/`PLC` + `RES` + `FMT` + sheets 10–14 | independent; can slip without blocking |
| **7** | Layer 2 statistics + `dq.metric` baseline + sheet 15 | needs ≥ 2 runs of history to be useful |
| **8** | Layer 3 LLM + validation + golden set + Incidents page + sheet 3 | enrichment last, so the report is already correct without it |
| **9** | Word report + charts + Reports page | consumes everything above |
| **10** | Finding lifecycle (new/recurring/resolved) + Well Scorecard + Check Catalogue UI | the screens that make it a habit rather than a one-off |
| **11** | Scheduling + drift alerting + Docker Compose | operational |

Phase 3 already gives a working UI over real findings. Phase 4 makes it business-critical.

---

## 13. Decisions needed

**Nothing blocks Phase 1 any more.** Both former blockers are resolved:

1. ~~Can `BIuser` create a `dq` schema?~~ **RESOLVED — no.** Measured `db_datareader` only:
   `CREATE SCHEMA` 0, `CREATE TABLE` 0, `INSERT` 0. Findings go to SQLite at
   `${DQ_STORE_PATH}`. See §7.
2. ~~What are `committed_start`/`committed_end`?~~ **RESOLVED — business decision: meaning
   unconfirmed, `role: display_only`.** Shown as "Commit" and sortable; never a baseline.
   `Baseline.COMMITTED` is registered but disabled.

**Still needed, but not blocking — each proceeds under a stated assumption the report prints:**

3. **Is `dbo.activity_task_plan` live or abandoned?** 17.9 M rows for **12 wells**, no
   timestamps, 200–2,718× duplication. `activity_taskplan_job_progress` covers 694 wells in
   84,790 rows and looks like the real source. If the big table is dead, excluding it
   removes ~89% of total scan cost — the single biggest performance decision.
3. **What should WBS weightage total** — 100, 1.0, or unbounded? Measured: 44.0 / 65.99 /
   270.69, with a root node at 135.34. The plan assumes §9's "share of project" ⇒ 100.
4. **Are the two `RDX / WDX` WBS pairs the same WBS?** They differ by one space before a
   line-feed. If yes, that is a phantom-WBS bug corrupting the weight denominator. If they
   are genuinely different scopes, the names need to say so.

6. **Which norms master wins** — `dbo.mapping_master` or `dbo.activity_master_mapping`?
   They disagree on 31% of shared activities (FLCV1090: 5.0 vs 3.0). Until settled, the
   checks can only report the conflict, not the violation.
6. **Is the `New_Activity_Code` migration going ahead?** `activity_master_csv` — the WBS
   source per §3 — is still on the old scheme, and the two masters disagree on 100% of codes.

**Non-blocking but shapes the report:**

7. **`HUP` and `LOC` are 100% unmapped** — is `activity_master_mapping` supposed to cover
   hook-up and location construction, or do those disciplines map elsewhere? Two of the five
   §4 milestones currently cannot be rolled up at all.
8. **§3 names the wrong crew column.** Should the rule be corrected to
   `ref.crew_type.crew_type_code`? Measured 53 of 57 vs 0 of 57 for `ref.crew.code`.
9. **`well_master.progress` contains only dates.** Repurposed column, or broken import?
   Decides whether it is a critical finding or a rename.
10. **Which well name is authoritative** — `ramz_id` (68% NULL) or `well_progress.well_name`
    (791 names)? The report must identify wells consistently, and 260 disagree.
11. **Report cadence and audience** — weekly to project controls, or on demand? Decides
    whether Phase 11 scheduling is in scope.

Items 1–6 need answers. The rest can proceed under a stated assumption, which the report
will print.
