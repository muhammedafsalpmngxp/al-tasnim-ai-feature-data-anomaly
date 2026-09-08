# Generated Invariants — How to Get Complete Coverage

The business asked: *"it will find anomalies like start date > end date — this is just a
sample, similarly a lot of anomalies are there. It should be production ready and accurate."*

That is a **coverage** question, and it is the right one. Hand-writing one check per column
pair does not scale and fails by omission. The answer is to declare column **roles** once
and **generate** every invariant from the role combinations.

Prototyped and run against the live database by `others/discovery/19_generated_invariants.py`.
It found 5 significant issues that my hand-written catalogue had missed.

---

## 1. The scale of the problem

Date columns in the in-scope tables (excluding the two giant tables and the empty ones):

| Table | Date columns | Ordered pairs | Rows |
|---|---|---|---|
| `well.well_master` | **36** | **630** | 814 |
| `well.task_daily` | 12 | 66 | 107,484 |
| `core.engineering_task_plan` | 9 | 36 | 128,643 |
| `dsq.drilling_sequence` | 4 | 6 | 96,952 |
| `project.project_director` | 4 | 6 | 2 |
| `dbo.flaf_po_peg_scr_moc_document_data` | 3 | 3 | 1,009 |
| `dbo.job_progress_plan_snapshot` | 3 | 3 | 1,138 |
| `dbo.Organization_Structure` | 2 | 1 | 5 |
| `dbo.schedule_json_data` | 2 | 1 | 1,048 |
| **Total** | **82** | **752** | |

**752 candidate date-order invariants from 82 columns.** `well_master` alone accounts for
630. Nobody hand-writes 752 checks correctly, and any subset chosen by hand is a guess about
where the problems are.

---

## 2. The generated families

| # | Family | Invariants | Source |
|---|---|---|---|
| F1 | date order: same-role `start <= end` | **101** | generated |
| F2 | date order: cross-role (target vs actual vs plan) | **651** | generated |
| F3 | actual date not in the future | 4 | generated |
| F4 | percentage / weightage within `[0,1]` or `[0,100]` | 17 | generated |
| F5 | quantity / measure `>= 0` | 31 | generated |
| F6 | `*_id` orphan check where no FK is declared | **125** | generated |
| F7 | text column whose content contradicts its type or name | 936 columns scanned | generated |
| F8 | business-rule lifecycle order (§7) | ~12 | **hand-written** |
| F9 | WBS weightage rollup (§9) | ~14 | **hand-written** |
| F10 | milestone deadlines (§4) | ~10 | **hand-written** |

**929 generated invariants + ~36 hand-written business rules.**

The division of labour is the whole point:

- **Generated families give coverage.** Every column pair is tested, so nothing is missed by
  omission. Adding a column to the database automatically adds its invariants.
- **Hand-written families give meaning.** "Pegging is due 60 days before `ex_rig_on_date`"
  cannot be inferred from a column name. It has to be written, reviewed, and pinned to §4.

Generated checks find *impossible* data. Hand-written checks find *non-compliant* data. A
report needs both, and they are labelled differently so a reader knows which they are seeing.

---

## 3. Prototype result — F1 run in full

101 same-role `start <= end` invariants generated and executed:

> **45 invariants violated · 16,532 violating rows**

### Five findings my hand-written catalogue had missed

| Finding | Rows | Worst | Why I missed it |
|---|---|---|---|
| **`core.engineering_task_plan`: `actual_start > actual_end`** | **14,827** | −77 d | I only checked `task_daily`'s actual dates |
| ...plus `actual_end` with no `actual_start` in the same table | 1,062 | — | never tested |
| **`dsq.drilling_sequence`: `earl_start_date > earl_end_date`** | 2 | **−739,700 d (−2,026 years)** | never looked at this table |
| **`well.task_daily`: `startDate > endDate`** (the P6 pair) | 405 | −175 d | I checked actual and target, not P6 |
| **`well_master`: `rig_on_date > loc_finish_date`** | **266 wells** | −1,240 d | not in my lifecycle list |
| `well_master`: `rig_on_date > const_complete_date` | 175 wells | −230 d | I had this one |
| `well_master`: `rig_on_date > eng_finish_date` | 72 wells | −278 d | not in my list |
| `well_master`: `location_po_recvd_date` after 7 later milestones | 17–48 each | −1,298 d | not in my list |
| `well_master`: `f_l_po_recd_date` after 7 later milestones | 1–43 each | −335 d | not in my list |

The 14,827-row finding in `core.engineering_task_plan` is larger than most of my
hand-written findings, and I would not have written that check — I had classified that table
as "the WBS plan" and only examined its weightage and hierarchy.

The −739,700-day value in `dsq.drilling_sequence` is a date around the year 0. One
generated invariant on a table I had never opened.

### A useful negative result

`well.task_daily`: `committed_start > committed_end` → **0 violations**, 4 rows with an end
and no start. The third planning layer is **clean**. Worth stating in the report: a
generated check that passes is evidence, not silence.

### An honest limitation the prototype exposed

Rows like `material_po_date > eng_finish_date` show 0 violations and high
`end_without_start` counts, because `material_po_date` and `eng_start_date` are 100% NULL
columns that my naive name-based role inference paired up anyway.

**This is exactly why the role register must be human-reviewed, not inferred.** In
production, roles come from `config/column_semantics.yaml`, and a column marked
`all_null: true` or `role: unused` generates no invariants. The prototype infers roles from
names only so it can run unattended; that is not the production design.

---

## 4. Root-cause sub-classification — beyond "this row is wrong"

Finding 51 inverted tasks is table-stakes. Telling the business *which date is wrong* is
what makes the report actionable.

Measured at the latest row per task, for `actual_start > actual_end`:

| Root cause | Tasks | Gap range |
|---|---|---|
| A. off by 1–7 days | **42** | 1–7 d |
| B. off by 8–60 days | 5 | 10–28 d |
| D. year component out by 1+ | 2 | 305–327 d |
| E. other | 2 | 64–69 d |

And the diagnostic that identifies the culprit column — compare each actual against its
planner target:

| | Tasks |
|---|---|
| `actual_start` **equals** `target_start` (start is trustworthy) | 42 |
| `actual_end` **equals** `target_end` (end is trustworthy) | 3 |
| **⇒ the END date is the wrong one** | **42 of 51** |
| ⇒ the START date is the wrong one | 3 of 51 |

So the report does not say *"51 tasks have inverted dates, please investigate."* It says:

> **42 tasks have a bad `actual_end` — the start matches the planner's target exactly, so
> correct the end date. 3 tasks have a bad `actual_start`. 2 have a year typed one year
> ahead.**

Three different remediation actions, each with its own row list.

### The sample rows match the business's own screenshot

| task_code | target_start | target_end | actual_start | actual_end | Inverted by |
|---|---|---|---|---|---|
| `LOC1010-34397` | 2024-12-11 | 2024-12-13 | **2025-11-04** | 2024-12-12 | **327 d** |
| `LOC1000-34398` | 2024-12-05 | 2024-12-05 | **2025-10-06** | 2024-12-05 | **305 d** |
| `FLCV1260-35500` | 2026-01-30 | 2026-01-30 | 2026-04-09 | 2026-01-30 | 69 d |
| `FLC1380-34694` | 2025-07-25 | 2025-07-25 | 2025-07-25 | **2025-05-22** | 64 d |
| `FLME1350-37223-T01` | 2026-03-04 | 2026-03-07 | 2026-03-22 | 2026-02-22 | 28 d |

The first two rows are the ones in the screenshot. Both are `LOC*` (Location Construction),
both have an `actual_start` in the following year, and both have `actual_end` exactly equal
to `target_end` — so on these two the **start** is the corrupted value, a year typed ahead.

`LOC1010-34397` also carries `progress = 0.0` with `completed = True`, so one row trips
three separate checks. The incident-correlation step groups them into one row to fix, not
three findings to triage.

---

## 5. What this changes in the design

### 5.1 Checks come from three sources, not one

```
config/column_semantics.yaml   (roles, reviewed by the business)
            │
            ├──▶ generator ──▶ ~929 invariant checks   (coverage)
            │
docs/BUSINESS_RULES.md
            │
            └──▶ hand-written ──▶ ~36 rule checks      (meaning)
                                        │
                              LLM discovery pass        (the unknown)
                                        │
                                   VERIFY (6 gates)
                                        │
                                     report
```

The LLM pass stays as the third source, for semantic anomalies neither of the first two can
express — a column named `progress` holding dates, `Coriolis` in a diameter field.

### 5.2 Every finding carries a sub-cause, not just a rule id

`ACT-006` becomes `ACT-006/A` (off by days), `/B`, `/C` (year typed +1), `/D`, each with its
own row list and its own remediation sentence. The Excel register groups by sub-cause so a
data-entry clerk gets a work list, not a diagnosis puzzle.

### 5.3 Generated checks that pass are reported too

45 of 101 invariants violated means **56 passed**. Those 56 go into the Check Catalogue sheet
marked `PASS` with their row counts. A reader must be able to see what was tested and found
clean — otherwise every run invites "did you check X?".

### 5.4 Volume control

929 generated invariants across 20 M rows cannot all run at full scan on every run:

| Control | Rule |
|---|---|
| Column pruning | a column with `role: unused` or 100% NULL generates nothing |
| Table pruning | above `DQ_LARGE_TABLE_ROW_LIMIT`, invariants run on a `TABLESAMPLE` and the finding is marked `sampled` |
| Batching | one `SELECT` per table computes every invariant for that table in a single scan (the profiler in `04_profile.py` already does this) |
| Zero-suppression | invariants with 0 violations are recorded as `PASS` metrics, not as findings |

`well_master`'s 630 pairs cost **one** table scan of 814 rows, not 630 scans. Batching per
table is what makes the generated approach cheaper than the hand-written one, not more
expensive.

---

## 6. Revised catalogue size

| Source | Checks | Purpose |
|---|---|---|
| Generated invariants (F1–F7) | **~929** | coverage — nothing missed by omission |
| Hand-written business rules (F8–F10) | ~36 | meaning — §4, §7, §9 compliance |
| Domain checks from discovery (RES, FMT, MDM, DUP, PLC, REF, WBS, ACT) | ~74 | the specific issues already found |
| LLM discovery | unbounded | semantic anomalies no rule expresses |
| **Total deterministic** | **~1,039** | every one verified before printing |

Up from the ~110 in the earlier plan. The increase is almost entirely generated, so it costs
configuration, not code.
