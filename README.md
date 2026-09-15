# Data Quality & Anomaly Sentinel

Agentic anomaly detection over a Microsoft SQL Server database, producing Word and Excel data-quality reports.

A statistical + LLM layer that sits **on top of** a deterministic rules engine and is sequenced **after** it, never duplicating it. Anomalies are declared in markdown; LangGraph agents compile each declaration into validated, verified SQL; a detection run then executes that SQL and writes the report.

**No table or column name appears anywhere in the Python code.** Point it at a different database, edit three markdown files and a `.env`, and it follows.

---

## How it works

Two graphs, not one. This split is the whole design.

### COMPILE — rare, expensive, LLM-heavy

Runs only when the database *structure* changes or a `domain/*.md` file is edited.

```
rule_loader → grounding → anomaly_sql_author → validator → executor
                   ↑                                          ↓
             catalog_writer ← rule_verifier ← sanity_gate ────┘
```

Turns each declared anomaly into a validated, executed, verified pair of SQL probes and writes them to `.cache/anomaly_catalog.json`.

### RUN — frequent, near-free

```
catalog_loader → probe_runner → scorer → summarizer → report_builder
```

Loads the compiled SQL and executes it. **One LLM call for the whole run** (the executive summary). Everything else is deterministic.

| | Compile | Run |
|---|---|---|
| Trigger | structure change / rule edit | every scheduled or manual run |
| LLM calls (~15 rules) | ~4–40 depending on `sql_mode` | **1** |
| Output | the SQL catalog | findings + `.docx` + `.xlsx` |

Daily runs are therefore essentially free.

### Why the catalog survives a data reload

Two fingerprints, deliberately:

- `schema.fingerprint` — structure **+ row counts** + database identity. Guards `schema.txt` and the two hint files, because all three describe *data*.
- **`catalog.fingerprint`** — the same **minus row counts**. Guards the compiled SQL.

Row counts change every day as data loads. A catalog keyed on the full fingerprint would recompile at full LLM cost daily — exactly what compiling once is meant to avoid. A probe's SQL depends on the *structure*, not on how many rows are in it today.

Each rule also stores a hash of its own markdown, so editing one rule recompiles **that rule only**.

---

## Prerequisites

**1. Microsoft ODBC Driver for SQL Server** — required, and `pip` cannot provide it.

```bash
winget install Microsoft.msodbcsql.18
```

On Debian/Ubuntu follow Microsoft's `msodbcsql18` apt instructions. Verify:

```bash
python -c "import pyodbc; print(pyodbc.drivers())"
```

**2. Python 3.11–3.13** in a conda environment. `pyodbc` is a compiled extension and needs a wheel for your exact Python version.

---

## Setup

```bash
conda activate <your-env>
```

```bash
cd backend && pip install -r requirements.txt
```

```bash
cp backend/.env.example backend/.env
```

Then edit `backend/.env` — at minimum `DB_SERVER`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `OPENAI_API_KEY`, and `ALLOWED_SCHEMAS`.

---

## Running it

Every command runs from `backend/`.

### Check the connection and configuration

```bash
python -m app.cli check
```

Prints every setting that decides what the engine can see, then connects. Almost every failure in this system is a driver, connectivity or `ALLOWED_SCHEMAS` problem — this diagnoses all three without running anything expensive.

### Build the database description

```bash
python -m app.cli introspect
```

Writes three files into `backend/.cache/`:

| File | Contents | Why it exists |
|---|---|---|
| `schema.txt` | tables, columns, PKs, declared FKs, grain warnings | what the agents may reference |
| `value_hints.txt` | real coded values from small lookup tables | so probes filter on codes that exist |
| `numeric_hints.txt` | measured min/max/avg/stdev/null-rate + **scale** per numeric column | so a probe never confuses a 0–1 fraction with a 0–100 percentage |

`numeric_hints.txt` is the slow step — one aggregate pass per table — and is cached afterwards. It has no counterpart in a chatbot, and an anomaly engine cannot work without it: `progress < 100` against a column that actually stores 0.0–1.0 flags every row in the table, and nothing in `INFORMATION_SCHEMA` reveals which it is.

### Inspect what the agents will see

```bash
python -m app.cli show schema
```

Also accepts `values`, `numbers`, `fingerprint`.

### List the rules

```bash
python -m app.cli list
```

Parses `domain/data_anomalies.md` and shows every rule with its severity, method and
`sql_mode`, plus an estimate of what a compile would cost in LLM calls. **Needs no database**,
so a rule file can be written and corrected offline. Exits non-zero if any rule failed to
parse — and names the file and line — because a rule the operator believes is running but
is not is the worst failure a data-quality tool has.

### Compile the rules into SQL probes

```bash
python -m app.cli compile --dry-run
```

Shows exactly what would be compiled and what it would cost in LLM calls, **spending nothing**
and needing no database. Run this first — it is the answer to "what will this cost?".

```bash
python -m app.cli compile
```

The expensive step, and the one that should almost never need repeating: a compiled probe
survives a data reload, so only a rule edit or a real structural change makes one stale.
`--rule DQ-003` compiles one rule, `--force` recompiles regardless of staleness, and
`--retry-failed` re-attempts rules that failed last time (they are *not* retried by default —
a rule that failed for a real reason would otherwise fail again, at full cost, every run).

### Run the detection and produce the reports

```bash
python -m app.cli run
```

The cheap step, and the one meant for a schedule: **one LLM call** for the executive summary,
however many probes ran or however much they found. Writes a `.docx` and an `.xlsx` into
`backend/reports/`. `--rule` restricts it, `--format xlsx` produces one format only.

```bash
python -m app.cli runs
```

The recorded history and the score trend. A single score means little; the direction of travel
is the thing a data owner actually needs.

#### The score

A severity-weighted **mean** of how clean each check came back, over every check that examined
at least one record:

```
share(rule) = anomaly_count / scope_total          (0 when the check is clean)
score       = 100 × (1 − Σ(weight × share) / Σ(weight))
```

Weights: critical 40, high 20, medium 8, low 5. It is computed **deterministically** — no model
decides how bad your data is, or the number could not be compared week to week. The model
writes only the prose.

**Why a mean and not a sum of penalties.** The first version subtracted a penalty per rule from
100. On this database that produced a score of **0.0**, because seven tables are over 90%
duplicated and those alone exceeded a hundred points. A score pinned at zero cannot move: fix
half the problems and it still reads 0, so nobody can tell whether the work helped and the
number stops being looked at. A mean is bounded by construction, so any improvement to any rule
moves it.

Checks that examined **no** records are excluded from both sides of the fraction — they are not
evidence of health, and they must not dilute the real findings either. They are reported
separately as coverage gaps.

The score is a **trend indicator**. Read alone it will understate a small number of severe
problems among many healthy checks, which is exactly why the report never shows it alone — the
findings table beside it carries the severity of individual problems.

### Run the self-checks

```bash
python -m eval.test_rules
```

No database, no LLM. Verifies that no business SQL has leaked into Python, that every rule
parses, that malformed rules are *rejected* rather than silently defaulted, that every probe
satisfies the SUMMARY/DETAIL contract, and that the contract the prompts ASK for is the same
one the code ENFORCES.

### Regression harness

```bash
python -m eval.run_eval --record
```

Records what every compiled probe measures **right now** as the baseline. Run this once after a
deliberate change — new rules, a schema change, a prompt edit.

```bash
python -m eval.run_eval
```

Re-measures and compares. **No LLM calls**, and only the SUMMARY query per probe — which
returns exactly one row by contract — so the whole suite is cheap enough to run on every change
rather than once a quarter.

It catches what `test_rules.py` structurally cannot, because these failures are all invisible in
the SQL text and every one of them produces a plausible-looking number:

| Failure | How it shows up |
|---|---|
| a join stops matching after a rename | `scope_total` collapses to 0 — reported as a clean result |
| a condition inverts | the anomaly share jumps from 0.2% to 99% |
| a column's scale moves 0–1 → 0–100 | a correct threshold now matches everything, or nothing |
| a prompt or model change degrades authoring | the rule needs more rewrites to compile |

`--offline` skips the database entirely; `--rule ID` restricts it.

**Two kinds of failure, separated on purpose**, because they need different action:

| Exit | Meaning | What to do |
|---|---|---|
| `0` | everything measures what it did | — |
| `1` | **regression** — something changed since the baseline | investigate today; a column was probably renamed or a condition inverted |
| `2` | **coverage gap** — a probe examines zero records, and did at baseline too | fix that rule's scope, or retire it |

A flat list would bury one genuine regression under sixty standing gaps. Both keep the exit
code non-zero: a probe that proves nothing must never read as a pass.

**On the tolerance bands.** Data changes daily, so an exact-count assertion would fail every
morning and be switched off inside a week — the classic way a regression suite dies. The
baseline records a *ratio* and checks it inside a deliberately wide band, tuned to catch an
inversion or a broken join rather than slow drift. The one assertion with **no** tolerance is
`scope_total > 0`: a probe that examined nothing has proved nothing, and no amount of
legitimate data movement makes that acceptable.

---

## Running the web UI

Two processes: the API and the Vite dev server. Both must be running.

### 1. Backend API

```bash
cd backend && python serve.py
```

Serves on **http://localhost:8100** (`API_PORT`), deliberately not 8000, so it can run beside
the chatbot. Check it with:

```bash
curl http://localhost:8100/api/health
```

### 2. Frontend

```bash
cd frontend && npm install
```

```bash
npm run dev
```

Opens on **http://localhost:5174**. The Vite dev server proxies `/api` to port 8100, so the
browser talks to a single origin and CORS never enters the picture during development.

If `backend/.env` sets an `API_KEY`, copy `frontend/.env.example` to `frontend/.env` and put
the same value in `VITE_API_KEY`. **Vite reads `.env` only at startup** — restart `npm run dev`
after changing it, or every request comes back 401.

> ⚠ In an `.env`, keep a comment on its OWN line when the value is empty. `API_KEY=   # blank`
> does **not** set an empty key: python-dotenv has nothing to terminate the value on and reads
> the comment text itself as the key, so every request then fails with 401.

### 3. Production

In production there is no Vite and no second server. Build the UI once:

```bash
cd frontend && npm install && npm run build
```

Then start the backend alone:

```bash
cd backend && python serve.py
```

It detects `frontend/dist` and serves the UI and the API together on **http://localhost:8100** —
one process, one origin. Nothing proxies, so CORS never applies and `CORS_ORIGINS` can stay
closed. Node is needed only on the machine that runs `npm run build`, never on the server.

Rebuild the UI whenever the frontend changes; the backend picks up the new `dist` on restart.

Set these in `backend/.env` before exposing it beyond localhost:

| Setting | Production value | Why |
|---|---|---|
| `API_KEY` | a long random string | blank disables authentication entirely, and `/api/compile` spends money |
| `CORS_ORIGINS` | leave unset | same origin now, so no cross-origin access is needed at all |
| `API_WORKERS` | `1` | more is **refused at startup**: compiles are serialised by a lock inside one process, and separate workers would let two compiles write the catalog at once |
| `API_RELOAD` | `false` | a reload mid-compile discards the run |

With an `API_KEY` set, put the same value in `frontend/.env` as `VITE_API_KEY` **before**
building — Vite bakes it into the bundle at build time, so changing it later means rebuilding.

### What the UI does

| Tab | Shows |
|---|---|
| **Dashboard** | the score, records flagged, the executive summary, severity and category charts, and the report downloads |
| **Findings** | every rule that found something, worst first; click one for its SQL |
| **Rules** | all rules with their compiled state — click any for its prose, its grounding note, its threshold justification and the exact SQL that runs |
| **Runs** | the score trend and every past run, with its reports |

**Compile rules** and **Run detection** are in the header and stream live progress over SSE —
both operations can take minutes, and a button that simply goes grey is indistinguishable from
one that has crashed. Only one runs at a time: both load the source database heavily, so a
second request is refused with a clear message rather than quietly queued.

Coverage gaps — checks that examined **no** records — are shown as a warning and never folded
into the clean count. A check that examined nothing has proved nothing, and presenting one as a
pass would be the most misleading thing this system could do.

---

## Configuration

Everything lives in `backend/.env` — see `.env.example`, which documents every knob inline.

### Table scope

`ALLOWED_SCHEMAS`, `EXCLUDED_TABLES`, `EXCLUDED_COLUMNS` are the only control over visibility. Secret-looking columns (`password`, `token`, `secret`, `apikey`, `credential`) are always hidden and need no configuration.

Scope here is deliberately **wider** than a chatbot's over the same database: more tables means more declared foreign keys to check for orphans, and more pairs of sources that are supposed to agree with each other. Cross-source disagreement is a high-value anomaly family and it is invisible when only one of the two sources is in scope.

### Model tiers

| Setting | Used by |
|---|---|
| `OPENAI_MODEL` | SQL Author, Rule Verifier, Findings Summarizer — the nodes whose judgement decides correctness |
| `OPENAI_FAST_MODEL` | Grounding. Blank = falls back to the main model, so leaving it unset is safe |

### Row-volume control

Four separate caps, because they solve four different problems:

| Setting | Bounds |
|---|---|
| `ANOMALY_SAMPLE_ROWS` (25) | rows an LLM is ever shown, per rule — the only cap that protects the context window |
| `ANOMALY_REPORT_ROWS` (2 000) | rows per rule in Word |
| `ANOMALY_EXPORT_MAX_ROWS` (1 000 000) | rows per rule sheet in Excel, streamed so memory stays flat |
| `ANOMALY_FETCH_BATCH` (5 000) | rows per fetch while streaming |

Detail queries are ordered worst-first, so a cap still shows the rows that matter and the report states the true total.

---

## The domain files — your control surface

`backend/domain/` holds everything database-specific. Editing these is how the system is taught; no Python changes.

| File | Contents |
|---|---|
| `business_rules.md` | authoritative business definitions, grain, join quirks |
| `few_shots.md` | worked question → T-SQL patterns |
| `data_anomalies.md` | **anomaly definitions in business prose - no SQL. The agent writes the SQL** |

### Where SQL is allowed to live

- **`domain/*.md`** — all business SQL.
- **`app/db/introspect.py` and `app/db/connection.py`** — catalogue queries only (`INFORMATION_SCHEMA`, `sys.*`). Engine queries, identical on every database, zero business knowledge.
- **Nowhere else.** `eval/test_rules.py` fails the self-checks if a business table name appears in any other Python file.

### Anatomy of a rule

````markdown
## RULE DQ-001 — Rig-off recorded but pre-rig-on construction incomplete

- category: Lifecycle consistency
- severity: high
- entity: well
- method: rule
- sql_mode: pinned
- status: active

**What is wrong** … **Why it matters** … **How to detect** … **Do NOT flag** …

```sql summary
-- exactly one row: rule_id, scope_total, anomaly_count, anomaly_pct, worst_severity_val
```

```sql detail
-- entity_key, entity_label, severity_value, evidence_*, explain_text
-- ORDER BY severity_value DESC
```
````

**The two-query contract is what bounds cost.** SUMMARY always returns one row, so it is always safe to show an LLM and always cheap to run. DETAIL runs only when SUMMARY reports a non-zero count, and the LLM never sees more than `ANOMALY_SAMPLE_ROWS` of it. The contract is checked against the driver's own `cursor.description` after execution — never by parsing SQL.

### `sql_mode`

| Mode | Behaviour | LLM calls per rule |
|---|---|---|
| `pinned` | Author skipped. SQL goes straight to validator → executor → contract → verifier. | 1 |
| `seed` *(default when SQL present)* | The SQL is a starting point; the Author adapts it to the live schema. | 2–3 |
| `authored` *(default when absent)* | Generated from the business description alone. | 3–4 |

Pin a hand-tuned query; switch it to `seed` the day a column is renamed.

### Thresholds

Not hardcoded. `tolerance: auto` (the default) requires the Author to write a **self-calibrating** predicate — `> AVG + 2*STDEV`, `> PERCENTILE_CONT(0.95)`, `> Q3 + 1.5*IQR` — computed inside the query, so it re-calibrates against real data on every run. A number stated in the rule always wins. The Verifier rejects any bare invented constant: every threshold must be either stated in the rule or derived from the data.

---

## Project layout

```
backend/
├── .env.example          every knob, documented inline
├── requirements.txt
├── main.py · serve.py    CLI and HTTP entry points
├── domain/               the control surface (see above)
├── .cache/               generated; safe to delete, rebuilt on demand
├── reports/              generated .docx / .xlsx
├── logs/                 anomaly.log (rotating)
├── app/
│   ├── config.py         every setting
│   ├── llm.py            provider abstraction + per-run token accounting
│   ├── utils.py          SQL/JSON extraction from model output
│   ├── observability.py  file log + colour pipeline trace
│   ├── tracing.py        optional Langfuse (inert unless configured)
│   ├── cli.py
│   ├── db/               connection.py · introspect.py
│   ├── rules/            spec · loader · generic · catalog · contract
│   ├── graph/            state · run_state · build · prompts · context · sqlcheck · nodes/
│   ├── report/           xlsx_report · docx_report · format · spool
│   ├── api/              FastAPI + SSE
└── eval/                 test_rules.py (offline) · run_eval.py · golden.jsonl
frontend/                 React 18 + TypeScript + Vite + ECharts
```

---

## Safety

- **SELECT-only, enforced deterministically.** The Validator rejects anything but a single `SELECT` / `WITH … SELECT`: no second statement, no DDL/DML keyword, no `sp_`/`xp_` procedure, no `OPENROWSET`/`OPENQUERY`/`BULK`, no `sys.sql_logins`. It holds regardless of the login's privileges.
- **Read-only by construction.** Nothing in this system issues a write to the source database.
- **`READ UNCOMMITTED`** (`ANOMALY_READ_UNCOMMITTED`, on by default) so scanning probes never block production writers. Issued on the connection by our own code — it is never part of the SQL the Validator inspects, so it cannot smuggle a second statement past the gate.
- **Bounded concurrency** (`ANOMALY_RUN_CONCURRENCY`, default 3). These are scanning queries; an unbounded pool would cost the source database more than the report is worth.
- **Tracing carries full prompts and responses**, including sampled data rows. Point Langfuse only at a trusted, access-controlled instance.

---

## Build status

| Phase | Scope | State |
|---|---|---|
| 1 | Config, logging, LLM client, DB layer, introspection, CLI | **done** |
| 2 | `rules/` package, `data_anomalies.md`, self-checks | **done** |
| 3 | Structural rule families (`rules/expand.py`) - one prose rule cloned over every matching schema feature, zero extra LLM calls | **done** |
| 4 | Compile graph + catalog | **done** |
| 5 | Run graph + Excel | **done** |
| 6 | Word + summarizer | **done** |
| 7 | FastAPI + SSE | **done** |
| 8 | React UI | **done** |
| 9 | Eval harness (golden baseline, regression net) | **done** |

All nine phases are built. Measured on the live database:

| | |
|---|---|
| 197 generated structural probes compiled | **5 min, 0 LLM calls, 0 failures** |
| a full detection run over all 197 | **1 LLM call** |
| records examined in one run | 58,012,422 |
| schema block per author call, after grounding | ~36,000 chars → 1,000–5,000 |
| offline self-checks | 89, all passing |

The generated structural probes cost nothing to compile because they skip grounding, the author
*and* the reviewer — their SQL is rendered deterministically from the schema's own foreign keys,
grain markers and measured scales, so there is no business judgement in it for a model to add.
Only the hand-declared rules in `data_anomalies.md` spend LLM calls, at roughly 3–4 each.
