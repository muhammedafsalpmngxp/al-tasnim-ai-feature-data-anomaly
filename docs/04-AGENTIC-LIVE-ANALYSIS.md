# Live, On-Demand Analysis and the Agentic Investigation Layer

Two things this document nails down, because both change what "correct" means for this
feature:

1. **Every number in every report is computed at click-time, against the live database.**
   Nothing in this system is allowed to hardcode today's figures — the 100% activity-code
   mismatch, the 31% norms conflict, the specific per-activity values in
   `docs/01-DISCOVERY-FINDINGS.md` — as a threshold, a constant, or a cached result. Those
   numbers are this month's *evidence*, not the spec. The spec is the SQL that produced
   them, and that SQL runs again, fresh, every time.
2. **An agentic layer sits on top of the deterministic engine** to do what a fixed check
   catalog structurally cannot: investigate *why* two sources disagree, decide which is more
   trustworthy, and follow that reasoning wherever the data leads — bounded, audited, and
   never trusted for a number it didn't derive from a query it actually ran.

Read this alongside `02-FEATURE-PLAN.md` (architecture) and `03-ANOMALY-TAXONOMY.md` (the
check catalogue). This document extends both; it does not replace either.

---

## 1. "Click the button" — what actually happens

There is no pre-computed report sitting on disk waiting to be downloaded. There is no
nightly batch job whose output gets served stale. The product has exactly one path:

```
User clicks "Generate Report"
        │
        ▼
POST /api/v1/runs  ──▶  202 Accepted, run_id
        │
        ▼
Orchestrator.run() — starts NOW, against AlTasnimBI's CURRENT state
        │
   ┌────┴─────────────────────────────────────────────────────────────┐
   │ Phase 0   schema snapshot + normalise    (built, Phase 1)        │
   │ Phase 1   ~1,093 deterministic checks    (next to build)         │
   │ Phase 2   statistics                                             │
   │ Phase 3   VERIFY (6 gates)                                       │
   │ Phase 4a  AGENTIC INVESTIGATION  ← this document                 │
   │ Phase 4b  LLM narration & correlation                            │
   │ Phase 5   persist to output/sentinel.db                          │
   │ Phase 6   .xlsx + .docx, timestamped with THIS run's server_now  │
   └────────────────────────────────────────────────────────────────────┘
        │
        ▼
User's browser follows SSE progress, then downloads the two files
```

This is already how Phase 1 is built — confirm it, because it is the property the rest of
this document depends on:

- `Orchestrator.run()` opens a fresh connection and calls `source.server_info()`, which reads
  `GETDATE()` **from the database, at that moment** — not from a config file, not from a
  cached value. `run.as_of_date` and `run.server_now` are that live read.
- `SchemaSnapshotter.capture()` re-introspects `sys.objects`/`sys.columns` on every run. If a
  column was added an hour ago, this run sees it; last run did not.
- Nothing in `app/sentinel/checks/*` (Phase 2, not yet built) may embed a percentage, a row
  count, or a "today's date" literal. Every threshold that looks fixed (§4's 60/90-day
  windows, the Oman UTM coordinate bounds) is a **business rule**, sourced from
  `docs/BUSINESS_RULES.md`, and is different in kind from a **measured fact** (31% norms
  conflict), which must always be a live `COUNT(*)`.

**What persists between runs is not the numbers — it's the comparison.** `dq.metric` and
`finding.status` (`new`/`recurring`/`resolved`, already built in `FindingsStore`) let one
live run be compared against the previous live run. That is the only sense in which history
matters here: "this went from 31% to 22% since last Tuesday" is itself computed by running
the live query twice and diffing, never by remembering last Tuesday's number as a constant.

### UX consequence: this takes minutes, and the UI must say so

A full scan of ~20M rows plus statistics plus agentic investigation is not instant. The
"Generate Report" button starts an async job (already the design in `02-FEATURE-PLAN.md`
§3), and the UI:

- Shows live phase progress over SSE (`RunProgress.tsx`), not a spinner with no information.
- Offers **two depths**, because agentic investigation adds real wall-clock time (§5):
  - **Quick** — Phases 0–3 + 4b only. Deterministic + statistical + narration. No agent tool
    loop. Minutes, not many.
  - **Deep** — adds Phase 4a. Every CRITICAL/HIGH master-data or cross-source finding gets an
    autonomous investigation. Longer, and the report says which mode produced it.
- Timestamps the report with the run's own `server_now`, not "generated on 2026-09-07" baked
  in anywhere — the date is data pulled from `run.as_of_date`, same as every other fact.

---

## 2. Why agentic, using the exact finding the business raised

The norms conflict is the clearest illustration of the ceiling a static check hits.

**What the deterministic check (`MDM-001`) can say, and only this:**

```sql
SELECT COUNT(*) AS shared, SUM(CASE WHEN ABS(mm.Norms - amm.norms) > 0.0001
                                     THEN 1 ELSE 0 END) AS disagree
FROM dbo.mapping_master mm JOIN dbo.activity_master_mapping amm
  ON amm.activity_id = CAST(mm.Activity_ID AS nvarchar(50))
```

→ *"120 of 387 shared activities disagree on norms (31%)."* True, reproducible, and as far
as a fixed query can go. It cannot say which source is right, whether the disagreement
clusters somewhere meaningful, or what to do about it — because answering that requires
**deciding what to look at next based on what the first query returns**, which is exactly
what a fixed SQL statement cannot do and an agent can.

**What an agentic investigation can do with the same starting point:**

1. Runs the check above, sees 120 of 387 disagree.
2. Asks a follow-up it wasn't told to ask: *do the disagreements cluster by `project_type`?*
   → queries `activity_master_mapping.project_type` grouped by disagreement, finds (for
   example) that mismatches concentrate in `Flowline` while `Location` activities mostly
   agree.
3. Asks: *is there independent evidence of which norm is closer to reality?* → joins to
   `well.task_daily`, computes actual `data_qty / duration` per activity, and compares that
   observed rate against both candidate norms.
4. Concludes — for each of the 120, not as one blanket number — something like: *"87 of 120
   disagreements: the `activity_master_mapping` value is within 10% of observed site
   productivity; the `mapping_master` value is off by 2–4×. For these, recommend adopting
   `activity_master_mapping.norms`. The remaining 33: neither source is close to observed
   productivity — recommend a fresh time study before trusting either."*

That is a materially different, more useful deliverable than "31% disagree," and it was
reached by *exploration*, not by a query someone wrote in advance. It is also exactly the
kind of reasoning that caught my own two mistakes earlier in this project — asking "could
this number be wrong, and how would I check?" is the same move, now automated and scoped to
every master-data conflict the deterministic layer flags.

**This does not replace `MDM-001`.** The 31% figure still comes from the reviewed,
reproducible SQL — that is the fact. The agent's job is everything downstream of the fact:
interpretation, root cause, a defensible recommendation. If the agent is unavailable, wrong,
or times out, `MDM-001`'s number still prints. Layer 1+2 alone still produce a correct report
— unchanged from the original design principle.

---

## 3. Architecture — the bounded tool loop

### 3.1 Tools the agent gets, and no others

Every tool is read-only and goes through **the same** `ReadOnlyGuard`, `Scope`, and
`db_query_timeout`/`MAXDOP` bound as the rest of the system. There is no separate, looser
path for agent-issued SQL — a bug in the guard is a bug for the whole system, not a
special case to re-audit.

| Tool | Purpose | Bound |
|---|---|---|
| `describe_table(table)` | column list, types, null%, distinct count, min/max — lets the agent explore without 936 columns dumped into its context | cached per run |
| `get_column_role(table, column)` | looks up `column_semantics.yaml`: role, baseline, `authoritative`, note | — |
| `run_query(sql)` | executes a validated `SELECT`; the tool itself appends `TOP 200` if the agent didn't | max 200 rows returned, max 15s |
| `lookup_business_rule(topic)` | full-text search over `BUSINESS_RULES.md`, returns the matching section verbatim | — |
| `record_finding(text, cited_calls[])` | the agent's only way to conclude — every number in `text` must appear in one of the results referenced by `cited_calls` | validated before it can print (§3.3) |

**`get_column_role` is what stops the agent reinventing the P6-vs-target mistake.** It only
ever returns what `column_semantics.yaml` says is `authoritative`. An agent that tries to
treat `startDate` (role `machine_plan`) as a baseline gets told, by the tool itself, that it
is not authoritative and why — the guard is structural, not a hope that the prompt was
followed.

### 3.2 Budgets — hard limits in code, not suggestions in a prompt

| Budget | Default | Why |
|---|---|---|
| `DQ_AGENT_MAX_TOOL_CALLS` | 8 per investigation | bounds cost and time per finding |
| `DQ_AGENT_MAX_INVESTIGATIONS_PER_RUN` | 25 | bounds total run cost |
| `DQ_AGENT_MAX_SECONDS_PER_INVESTIGATION` | 45 | no single investigation can stall the run |
| `DQ_AGENT_MAX_ROWS_PER_QUERY` | 200 | enforced by the tool, not by asking nicely |
| `DQ_AGENT_MAX_TOKENS_PER_RUN` | 150,000 | separate ceiling from the narration budget (§ Phase 4b in `02-FEATURE-PLAN.md`) |

Exceeding any budget ends that investigation, marks it `agent_status: incomplete`, and the
underlying deterministic finding still reports normally. **An agent failure must never
suppress a deterministic finding** — that would let the enrichment layer make the report
*less* correct, which is the one thing it may never do.

### 3.3 Trust — every agent claim is checked exactly like LLM narration, plus one more gate

Numbers in `record_finding(text, cited_calls)` are checked programmatically: every digit in
`text` must appear in the row data of one of `cited_calls`' results, or the finding is
rejected and logged as a validation failure (not silently dropped — that's a signal the agent
is unreliable and needs its prompt reviewed). This is the same "no invented numbers" rule
already applied to LLM narration (`02-FEATURE-PLAN.md` §7), extended to cover agent output,
which is less constrained by construction and therefore needs it more, not less.

**Full auditability.** Every tool call the agent makes — the exact SQL, the exact rows
returned, the timestamp — is stored in a new `agent_trace` table (SQLite, FK'd to the
finding it supports). A human can replay the agent's reasoning step by step and re-run any
query it ran. Nothing the agent concludes is trusted on its say-so; it is trusted because
the trail underneath it is inspectable.

```sql
CREATE TABLE agent_trace (
    trace_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL REFERENCES run(run_id),
    finding_id   INTEGER REFERENCES finding(finding_id),
    step_no      INTEGER NOT NULL,
    tool_name    TEXT NOT NULL,
    tool_input   TEXT,      -- the exact SQL / lookup, verbatim
    tool_output  TEXT,      -- the exact rows returned (row-capped), verbatim
    tokens_used  INTEGER,
    elapsed_ms   INTEGER
);
```

### 3.4 Trigger policy — when the agent runs, so cost stays bounded

The agent is not dispatched on every finding — most findings (a dead column, a duplicate
row) need no investigation, only a citation. It is dispatched on:

1. **Every `CRITICAL`/`HIGH` finding in families `MDM-*` and `CON-*`** — master-data
   conflicts and cross-source disagreements are precisely the class where "which source is
   right" is the valuable question and a fixed query cannot answer it.
2. **Every `PIP-007` schema-drift finding** (new table/column, from the drift detector built
   in Phase 1) — by definition, no static check exists for something that didn't exist last
   run, so the agent is the *only* thing able to say anything useful about it this run.
3. **On demand, scoped to one finding** — an "Investigate further" button in the UI
   (`FindingDetail.tsx`) runs a single bounded investigation outside the normal pipeline, so
   a reviewer can go deeper on anything, not only what the trigger policy picked.

Capped overall at `DQ_AGENT_MAX_INVESTIGATIONS_PER_RUN` (default 25) so a schema with an
unusually large number of qualifying findings cannot blow the run's time or cost budget —
the highest-severity ones win the slots; the rest are queued for the on-demand path.

### 3.5 Phase placement

```
Phase 3   VERIFY            (6 gates: grain, baseline, due-date, role, counter-query, materiality)
Phase 4a  INVESTIGATE       dispatch bounded agent sessions per §3.4, write agent_trace + enriched findings
Phase 4b  NARRATE           explain / correlate / prioritise over the now-enriched finding set (unchanged design)
```

4a runs before 4b so the narration layer has the agent's deeper findings available to
correlate and prioritise, not just the flat deterministic list.

---

## 4. Configuration additions

```ini
# --- Agentic investigation layer ---
DQ_AGENT_ENABLED=true
DQ_AGENT_MODEL=gpt-4o
DQ_AGENT_MAX_TOOL_CALLS=8
DQ_AGENT_MAX_INVESTIGATIONS_PER_RUN=25
DQ_AGENT_MAX_SECONDS_PER_INVESTIGATION=45
DQ_AGENT_MAX_ROWS_PER_QUERY=200
DQ_AGENT_MAX_TOKENS_PER_RUN=150000

# --- Report depth (surfaced in the UI as Quick / Deep) ---
DQ_DEFAULT_REPORT_DEPTH=deep   # quick = phases 0-3 + 4b only, no agent tool loop
```

`DQ_AGENT_ENABLED=false` degrades exactly like `DQ_LLM_ENABLED=false` already does — the
report still builds, correctly, from Phases 0–3 alone, with a banner noting investigation
was skipped.

---

## 5. Cost and time — stated honestly, not glossed over

Rough order of magnitude, so the depth toggle in §1 is a real product decision and not a
surprise:

| | Quick (no agent) | Deep (agent, ~15–25 investigations) |
|---|---|---|
| Added wall-clock | — | roughly 3–8 minutes |
| Added LLM cost per run | narration only | narration + up to 150K agent tokens |

These are estimates to plan around, not guarantees — they should be measured against the
real system once Phase 2's check catalogue exists and produces a realistic finding count to
drive the trigger policy in §3.4.

---

## 6. What this changes in the build sequence

Nothing about what comes next. The agent depends on findings existing to investigate, so it
cannot be built before Phase 2 (the ~1,093-check catalogue) exists. Build order is unchanged
from `02-FEATURE-PLAN.md` §12: Phase 2 (checks) is still the next thing to build. This
document specifies what Phase 4a becomes when its turn arrives, and makes explicit — for
every phase already built or still to come — that **live, at click-time, against the current
database** is not an implementation detail but the load-bearing property the whole feature
depends on.
