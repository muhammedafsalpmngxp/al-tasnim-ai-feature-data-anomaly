# Verification of an External Profiling Report

An external profiling report was supplied as a target format. Every claim in it was
re-measured against the live database by `others/discovery/18_verify_report.py`.

**Result: the arithmetic is largely right; both items it labels "critical" are
misdiagnosed, and its missing-data totals overstate the actionable problem by 3.5×.**

This document is the scorecard, and it is the strongest argument for the verification stage
in the production design (see §4).

---

## 1. Scorecard

| # | Claim | Numbers | Diagnosis | Verdict |
|---|---|---|---|---|
| 1 | etp: 128,643 rows, 3,359 distinct IDs → "critical PK integrity problem" | ✅ exact | ❌ **wrong** | **misdiagnosed** |
| 2 | Documents table: "all primary/key values empty for 1,009 rows" | ❌ | ❌ **false** | **incorrect** |
| 3 | Crew missing 72,147 (67.1%) / crew-type 106,690 (99.26%) | ✅ exact | ✅ | **correct** |
| 4 | End dates to 2071-03-24, start dates to 2032-06-15 | ✅ exact | ⚠ vague | **correct, under-specified** |
| 5 | 209 wells (25.7%) no project; only 6 distinct projects | ✅ exact | ⚠ framing off | **correct, wrong emphasis** |
| 6 | Pegging 254 / FLAF 234 / rig-on 342 / hook-up 448 missing | ✅ exact | ❌ **3.5× overstated** | **correct, misleading** |
| 7 | 17.9 M and 1.75 M row tables need targeted QC | ✅ exact | ✅ | **correct** |
| 8 | Plant table: 15 rows, 5 distinct codes → 10 duplicates | ✅ exact | ✅ | **correct** |
| 9 | Revenue: 4 distinct PMS 0.00–0.30, IDs unique | ✅ exact | ✅ | **correct** |
| 10 | Activity mapping: 379 rows, 375 distinct activity IDs | ✅ exact | ✅ | **correct** |

8 of 10 correct. The 2 failures are the 2 it ranked highest.

---

## 2. The two misdiagnoses

### 2.1 Claim 1 — snapshot history mistaken for broken keys

The report says:

> *Critical primary-key integrity problem: 128,643 rows but only 3,359 distinct IDs
> (125,284 duplicate ID occurrences). This breaks row-level uniqueness and will corrupt
> joins/aggregations.*

Measured:

| Metric | Value |
|---|---|
| rows | 128,643 |
| distinct `id` | 3,359 |
| duplicate `id` occurrences | 125,284 |
| distinct `Time_Stamp` snapshots | **81** |
| distinct `(project_id, code)` | **3,359** |
| **distinct `(project_id, code, Time_Stamp)`** | **128,643 — equals the row count** |
| duplicate `(project_id, code, Time_Stamp)` groups | **0** |

`core.engineering_task_plan` is a **daily snapshot table**. 3,359 tasks × ~38 snapshots =
128,643 rows. `id` is the *source system's* task identifier, and it is *correctly* stable
across snapshots — the same task keeps its id as it is re-captured each day. The real key
is `(project_id, code, Time_Stamp)` and it is **perfectly unique, zero duplicates**.

There is no primary-key integrity problem. The table is history.

The real risk in this table is different and the report misses it entirely: **you must pin
one `Time_Stamp` per project before aggregating**, or every weightage sum double-counts. And
pinning by *date* is not enough — 2026-07-29 holds two snapshots (08:16:51 and 12:09:22).
That is a genuine critical issue; "the PK is broken" is not.

Note the report's remediation — *"use a composite key or row-hash as a temporary
safeguard"* — half-lands by accident. But acting on the stated diagnosis would send someone
hunting a de-duplication bug that does not exist, and they would likely "fix" it by deleting
80 days of history.

### 2.2 Claim 2 — factually false

The report says:

> *A documents table used for FLAF/pegging/MOC has all primary/key values empty for its
> 1,009 rows — those rows cannot be linked or trusted as unique records.*

`dbo.flaf_po_peg_scr_moc_document_data` measured:

| Column | Result |
|---|---|
| `Record_ID` (int) | exists |
| **`Well_id`** | **0 NULLs, 871 distinct values** |
| `MOCjson` | 1,003 of 1,009 NULL (99.4%) |
| `PEGjson` | 524 NULL |
| `FLAFjson` | 229 NULL |

The keys are fully populated and the table **can** be joined to wells. What is actually
sparse are the *document payload* columns — `MOCjson` at 99.4% NULL, and `MOCversionid`
likewise. The report appears to have read "the version-id columns are mostly empty" as "the
primary key is empty".

The genuine finding here is narrower and worth keeping: **1,009 rows across 871 distinct
wells**, so 138 wells carry multiple document rows, and the MOC document is effectively
never populated.

---

## 3. The missing-data numbers are 3.5× overstated

The report lists 1,278 missing schedule dates and concludes *"these gaps affect any
deadline/compliance reporting."* The raw counts are exact. The conclusion is not, because a
NULL on a well that has not reached that stage yet is **correct data, not a gap**.

Splitting each by whether the deadline has actually passed:

| Field | Reported "missing" | Not yet due — correct NULL | **Genuinely overdue** |
|---|---|---|---|
| `rig_on_date` | 342 | 280 | **62** |
| `pegged_date` | 254 | 137 | **117** |
| `flaf_issue_date` | 234 | 146 | **88** |
| `eng_completion_date` | 448 | 351 (+2 inside the 2-day window) | **95** |
| **Total** | **1,278** | **916** | **362** |

916 of the 1,278 are wells correctly awaiting a future milestone — 311 wells have an
`ex_rig_on_date` still in the future, so a NULL `rig_on_date` is exactly right.

**A team told to chase 1,278 gaps would spend most of its effort on records that are
already correct.** The actionable number is 362. Reporting the raw count without the split
is the difference between a report that gets used and one that gets ignored.

---

## 4. Findings the report missed, in the tables it already looked at

Not criticism of coverage — these matter because they are in the *same* tables, so the same
scan could have caught them.

| Finding | Detail |
|---|---|
| **Snapshot pinning required** | 81 snapshots, 2 on some days; unpinned aggregation double-counts weightage |
| **The 2071 date is 2 malformed codes on a phantom well** | `MSSF1180-00000` and `MSBP1020-00000`, both `well_id = 0`, spans of 45 and 33 years. 494 rows, 27 task codes, **5 wells** — not a vague "needs confirmation" |
| **13 of 19 defined projects have no wells** | The report's "only 6 distinct projects" is the symptom; 0 well projects are orphaned, but 13 master projects are unused |
| **`core.plant_description` rows are triplicated** | Every one of the 5 plant codes appears exactly 3× |
| **`plant_code` and `cluster_code` collide numerically** | `plant_code 3525 = 'GB'` but `cluster_code 3527`; `ref.cluster` says 3525 = Amal, 3527 = GB. Two code spaces sharing a number range |
| **`'Mamul Projects'`** | typo for "Marmul" in reference data |
| **`task_daily` has 3 duplicate `id` values** | the report caught this one; it is real and worth keeping |

---

## 5. What this changes in the design

Both failures share one cause: **a number was computed correctly and then interpreted
without checking the table's grain or the business meaning of the column.** That is the same
error I made twice — P6 vs target baseline, and per-row vs per-task grain
(`01c-BASELINE-AND-GRAIN-CORRECTION.md`).

Three tools independently made it. So it is not carelessness; it is the default outcome of
profiling without context. The production design must make it structurally impossible.

### 5.1 A mandatory VERIFY stage

Findings do not go straight to the report. Each is adversarially re-tested first, and a
finding that fails any gate is downgraded or dropped with the reason recorded.

| Gate | Question | Catches |
|---|---|---|
| **Grain** | Is the declared grain the table's real key? Test the candidate key for uniqueness. | Claim 1 — `(project_id, code, Time_Stamp)` is unique ⇒ not duplication |
| **Baseline** | Is this measured against the authoritative column? | P6 vs target — the 5.5× error |
| **Due-date** | Is a NULL actually *due* yet? | The 1,278 → 362 collapse |
| **Column role** | Is this column the one the rule means, per the ownership register? | Claim 2 — payload column read as a key |
| **Counter-query** | Does an independently written query reproduce the count? | arithmetic and join fan-out |
| **Materiality** | Does it affect ≥ 1 real entity a person can act on? | phantom findings on empty tables |

Every printed finding carries `verified_by`, `grain`, `baseline`, and `counter_query_count`.
A finding that cannot state those does not print.

### 5.2 Findings are typed, not just severity-ranked

The report's flat severity list is what allowed a non-issue to sit at the top. Each finding
also gets a **class**:

| Class | Meaning | Example |
|---|---|---|
| `VIOLATION` | A business rule is broken on real records | 449 wells with targets past `ex_rig_on_date` |
| `DEFECT` | The data is internally impossible | 481 rig double-bookings; 46 inverted targets |
| `GAP` | Something genuinely due is absent | the 362 overdue milestones |
| `PENDING` | Absent but not yet due — **reported as normal** | the 916 correct NULLs |
| `DESIGN` | How the system works, not a fault | 81 snapshots; `id` reused across them |
| `RISK` | Not wrong yet, will bite | no CHECK constraints; 19 tables without a PK |
| `REVIEW` | Statistical outlier — a question | 247-day activity against an 18-day median |

`PENDING` and `DESIGN` still appear in the report — in their own sections, counted, so the
reader can see they were examined and cleared. That is what stops the next reviewer
re-raising them as critical.

### 5.3 The AI's job includes challenging findings

Beyond explaining and correlating, the LLM layer gets an explicit **adversarial pass**: for
each finding, argue why it might *not* be a problem, given the business rules and the
column-ownership register. Findings that survive their own counter-argument are ranked
higher; ones that do not are reclassified.

Had that pass run over the external report, the prompt *"argue why 3,359 distinct IDs in
128,643 rows might be correct"* has an obvious answer — snapshot history — and the finding
would have been reclassified `DESIGN` before it ever reached a page.
