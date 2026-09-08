# Discovery Findings — AlTasnimBI

Evidence base for the Data Quality & Anomaly Sentinel. Every number below was measured
against the live database on **2026-09-07** by the scripts in `others/discovery/`.
Re-run any of them to reproduce; raw output is in `others/discovery/out/*.json`.

---

## 1. Connection & environment

| Item | Value |
|---|---|
| Server | `20.98.112.250,1433` — Microsoft SQL Server 2022 (RTM-CU25-GDR) 16.0.4260.1 |
| Database | `AlTasnimBI` |
| Login | `BIuser` (read access confirmed across all 10 schemas) |
| Collation | `SQL_Latin1_General_CP1_CI_AS` — **case-insensitive, non-Unicode default** |
| Server clock | UTC (`GETDATE()` == `GETUTCDATE()` to within 4 ms) |
| Driver required | `ODBC Driver 18 for SQL Server` with `Encrypt=yes;TrustServerCertificate=yes` |

Driver 17 is also installed as a fallback. Driver 18 defaults `Encrypt=yes`, so
`TrustServerCertificate=yes` is **mandatory** — without it the connection fails on the
self-signed certificate. Python is 3.14.5 at
`%LOCALAPPDATA%\Programs\Python\Python314\python.exe`; the discovery venv is `.venv/`.

### Environment variables present

`DB_SERVER`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `ALLOWED_SCHEMAS`,
`EXCLUDED_TABLES`, `EXCLUDED_COLUMNS` (blank), `LLM_PROVIDER=openai`, `OPENAI_API_KEY`.

---

## 2. Scale

| Metric | Count |
|---|---|
| Schemas | 10 — `bridge, core, dbo, dsq, landing, project, ref, test, wbs, well` |
| Tables + views | 81 (78 tables, 3 views) |
| Columns profiled | 936 |
| Tables with a primary key | 59 of 78 |
| Foreign keys declared | 41 |
| **CHECK constraints declared** | **0** |
| Rows, total | ~20.1 M |

Two tables hold 97% of the volume: `dbo.activity_task_plan` (17,930,484) and
`dbo.engineering_acc_dump` (1,751,828). Everything else is ≤ 130 k rows.

### Scope gap — the config hides the evidence we need

`ALLOWED_SCHEMAS` omits `landing`, `test`, `wbs`. `EXCLUDED_TABLES` additionally hides 13
tables. That leaves 67 of 81 objects visible. **But the WBS weightage and progress data
this feature must audit lives almost entirely in excluded objects:**

| Object | Rows | Status | Why we need it |
|---|---|---|---|
| `dbo.activity_task_plan` | 17,930,484 | excluded | `weightage`, `progress_percent`, `parent_wbs_code` |
| `dbo.activity_taskplan_job_progress` | 84,790 | excluded | `weightage`, `progress`, `parent_wbs_code`, per-well |
| `dbo.activity_master_mapping` | 379 | excluded | **required by business rule §3** (hop 1 of task_code → WBS) |
| `wbs.WBS_master` | 81,846 | schema not allowed | WBS code registry |
| `well.wmr_conversion` | 0 | excluded | empty — genuinely skippable |

`EXCLUDED_TABLES` is the right list for a text-to-SQL chat feature (it hides tables that
confuse an LLM writing queries). It is the wrong list for an auditor. **The Sentinel needs
its own scope variables** — see plan §11.

---

## 3. The well spine

`well.well_master` — 814 rows, 103 columns, one row per well, `well_id` unique but
**no primary key declared**. This is the anchor for every business rule.

All seven business-rule date columns from §2 are present and correctly named:
`ex_rig_on_date`, `rig_on_date`, `ex_rig_off_date`, `rig_off_date`, `pegged_date`,
`flaf_issue_date`, `eng_completion_date`. `ex_rig_on_date` and `ex_rig_off_date` are the
only two date columns with **0% NULL** — consistent with `ex_rig_on_date` being the master
planning date.

### Satellite coverage

| Table | Rows | Distinct wells | Orphan wells (not in master) |
|---|---|---|---|
| `well.well_progress` | 99,589 | 814 | 0 |
| `well.task_daily` | 107,484 | 778 | **184** |
| `well.well_classification` | 809 | 809 | 0 |
| `dbo.engineering_well_priority` | 802 | 261 | **122** |

- **220 wells** in master have no row in `well.task_daily` at all.
- **5 wells** have no `well_classification` row.
- `well.task_daily` has **175 rows with `well_id IS NULL`** — unattributable work.

---

## 4. Confirmed anomalies — the four classes the business named

### Class 1 — Rig-off recorded but upstream work unfinished

463 wells have a `rig_off_date` (drilling complete). Among them:

| Missing upstream fact | Wells |
|---|---|
| no `loc_finish_date` | 194 |
| no `const_complete_date` | 187 |
| no `loc_start_date` | 170 |
| no `pegged_date` | 33 |
| no `tie_in_ready_date` | 20 |
| no `flaf_issue_date` | 17 |

And at task level: **422 wells whose rig is off still carry 40,127 open tasks**
(`completed <> 1`) out of 75,258.

Worst case — well `36754` / `SONRAKDS5277`: rig off **2023-03-29**, i.e. **1,258 days ago**,
156 of 277 tasks still open, `eng_completion_date` never recorded.

Also: 366 wells are complete (`eng_completion_date` set) yet **27 have no `pegged_date`**
and 1 has no `flaf_issue_date` — a well cannot finish without having been pegged.

### Class 2 — Dates not meeting the norm

**Lifecycle order violations** (business rule §7):

| Violation | Wells |
|---|---|
| `const_complete_date` after `rig_on_date` | **101** |
| `ex_rig_on_date` after `ex_rig_off_date` | **21** |
| `pegged_date` after `rig_on_date` | 5 |
| `flaf_issue_date` after `rig_on_date` | 5 |
| `rig_on_date` after `rig_off_date` | 4 |
| `eng_completion_date` before `rig_off_date` | 3 |
| `loc_start_date` after `loc_finish_date` | 3 |

**Duration outliers** — drilling span (`rig_on` → `rig_off`), n = 463:
min **−92 days**, max 59, mean 9.14. **4 negative**, **24 zero-day**.
A −92-day drilling span is arithmetically impossible.

**Hook-up lag** (`rig_off` → `eng_completion`), n = 366: min **−21**, max **258**,
mean 12.4. Deadline per §4 is rig_off + 2 days → **313 of 366 missed it**; 3 are negative.

**Milestone lead times** vs `ex_rig_on_date` (§4 norms of −60 d and −90 d):

| | Late | Missing and past due |
|---|---|---|
| Pegging (−60 d) | 19 | 117 |
| FLAF (−90 d) | 146 | 88 |

**Impossible actuals** — dates in the future relative to 2026-09-07:
`rig_on_date` 2, `rig_off_date` 2, `location_po_recvd_date` **19**,
`task_daily.ActionOn` **307**.

### Class 3 — Activity/WBS completion contradictions

`well.task_daily`, 107,484 rows:

| Contradiction | Rows |
|---|---|
| `completed = 0` but `progress >= 1` | **42,831** (39.8%) |
| `actual_end` present but `completed = 0` | **40,812** |
| `completed = 1` but `progress < 1` | 311 |
| `completed = 1` but `actual_end` NULL | 220 |
| `actual_start` after `actual_end` | 209 |
| `target_start` after `target_end` | 46 |
| `actual_end` without `actual_start` | 39 |
| `progress > 1` | 14 |
| `progress < 0` | 2 |

The first two lines are the mechanical cause of "all activities of a WBS are 100% complete
but the WBS is not". A rollup driven by `completed` disagrees with one driven by `progress`
on **40% of all task rows**.

### Class 4 — WBS % wrong, corrupting overall project progress

`core.engineering_task_plan` is the WBS/activity plan: `type` = `W` (WBS), `A` (activity),
`M` (milestone), `T` (new task). The `parent` column resolves 100% cleanly to another
row's `id` within the same snapshot, so the tree **is** traversable.

**WBS weightage does not total 100% on any project.** At the latest snapshot
(2026-07-29 12:09:22), summing `weightage` over distinct type-`W` codes:

| Project | WBS codes | Σ weightage |
|---|---|---|
| `65A1B60E-…D4E0` | 45 | **44.00** |
| `99906E82-…3A28` | 67 | **65.99** |
| `B33321F6-…88B8` | 271 | **270.69** |

Business rule §9 requires WBS PMS weights to be a share of the project. None of these is
100. The root node `PW1` alone carries `weightage = 135.344794` on project B33321F6 —
and its own `progress` is 0.69, so a 135% weight is feeding the headline progress figure.

`PW1` weightage also **drifts across snapshots** on project 65A1B60E: 18 → 19 → 22 over
three days while its WBS count grew 15 → 37 → 45. Weight is tracking node count, not a
normalised share.

**Float precision noise**: weightages stored as `0.9999990000000001`, `1.000001`,
`0.9999990000000003` where 1.0 is meant. `E10468` ranges 0.999999 → **1.4023**.
In `dbo.activity_taskplan_job_progress`, `weightage` max for type `W` is **1.1167** — over 1.0.

**Equal-weight rule violated** (§9: activities in the same WBS share `1/N`): under WBS
"Sleeper Laying" one activity carries 0.01 and another 0.008333; under "Foundation
Installation" activities carry 0.010714 (= 1/93.3, not a whole `1/N`).

---

## 5. Structural traps — these will produce false positives if ignored

### 5.1 `well.well_progress` is 99% duplicate

99,589 rows for 814 wells. **601 duplicate `(well_id, week_number)` groups holding 98,771
excess rows.** Week `2026-02-05` alone has 793 rows for 5 wells. Only ~818 rows are real.

Any statistical check that reads this table without deduplicating first will compute
distributions from a 122× inflated population.

Also here: `week_number` = **1900-01-01 for 209 rows** (placeholder), and
`curr_week_cum_progress` is **99.99% NULL** while `prev_week_cum_progress` is 0% NULL —
the "current" column was never populated.

### 5.2 `dbo.activity_task_plan` is 200–2,700× duplicated

17,930,484 rows covering **only 12 wells**:

| well_id | rows | distinct task_codes | duplication factor |
|---|---|---|---|
| 33760 | 6,200,534 | 20,892 | 297× |
| 31722 | 5,014,994 | 16,115 | 311× |
| 11354 | 2,315,693 | 852 | **2,718×** |
| 35495 | 2,794,740 | 13,539 | 206× |

`created_at` and `updated_at` are **100% NULL**, so there is no timestamp to pick a latest
version by. This looks like an append-on-every-sync table with no dedup key.

### 5.3 `core.engineering_task_plan` is a daily snapshot, not current state

128,643 rows across **55 snapshot days** and only 3 projects — averaging 38.3 rows per
`(project_id, code)` pair. Within a single exact `Time_Stamp` there are **zero** duplicate
`(project_id, code, type)` groups, so each snapshot is internally clean.

**Every check must pin to one `Time_Stamp` per project.** Grouping by
`CAST(Time_Stamp AS date)` is not enough — 2026-07-29 has two snapshots
(08:16:51 and 12:09:22), and averaging across them doubles every weightage sum.

### 5.4 `1900-01-01` is a NULL surrogate

In `dbo.activity_taskplan_job_progress` (84,790 rows):

| Column | rows = 1900-01-01 |
|---|---|
| `actual_end_date` | **43,232** (51%) |
| `actual_start_date` | **39,446** (47%) |
| `target_start_date` / `target_end_date` | 3,679 each |
| `planned_start_date` / `planned_end_date` | 0 |

Any "is it complete?" test reading `actual_end_date IS NOT NULL` will treat half the table
as complete on 1900-01-01. This must be normalised to NULL **before** checks run, and
reported as its own finding.

### 5.5 Cross-well contamination in `activity_taskplan_job_progress`

`task_code` is `<activity_id>-<well_id>`. For rows where the `well_id` **column** says
`33760`, the task_code suffixes include `-32258`, `-33128` and `-33760`:

| task_code | well_id column | progress | weightage | parent_wbs_code | actual_end |
|---|---|---|---|---|---|
| `FLCV1040-32258` | 33760 | 0.0 | 0.010714 | `W33760` | 1900-01-01 |
| `FLCV1040-33128` | 33760 | 1.0 | **0.0** | `Foundation Installation` | 2025-12-05 |
| `FLCV1040-33760` | 33760 | 1.0 | 0.010714 | `Foundation Installation` | 2026-02-17 |

Three different wells' work filed under one `well_id`, with disagreeing weightage.

`parent_wbs_code` also **mixes two vocabularies in one column** — sometimes a code
(`W33760`), sometimes a WBS name (`Commissioning`, `Punch Point`, `Sleeper Laying`).

### 5.6 Business rule §5 — early is good, not an anomaly

An actual date earlier than its expected date means the work accelerated. The engine must
**never** flag `rig_on_date < ex_rig_on_date` or `rig_off_date < ex_rig_off_date` as a
defect, and must never report the absolute variance as "days of delay". This needs to be a
hard guard in code, an explicit instruction in the LLM prompt, and a regression test —
it is the single easiest way for an LLM layer to produce a wrong, business-damaging report.

### 5.7 Business rule §11 — no actual construction completion date exists

There is no approved column for Location or Flowline Construction actual completion.
`loc_finish_date` and `const_complete_date` exist but §4 explicitly forbids using them for
this. The engine must report the rule as undefined rather than substituting a column.

---

## 6. Master-data conflicts

### 6.1 Two competing activity-code systems

`dbo.mapping_master` (642 rows) and `dbo.activity_master_mapping` (379 rows) share 387
`Activity_ID` values. On those:

| Field | Disagreements |
|---|---|
| activity code | **387 / 387 (100%)** |
| `Norms` | **120 / 387 (31%)** |
| `UOM` | 68 / 387 |

The code disagreement is a half-finished migration: `mapping_master.Old_Activity_Code`
(`FL-CV-ML01-01`) matches `activity_master_mapping.activity_code`, while
`mapping_master.New_Activity_Code` (`F-C-SLL-SSO-01`) is a new scheme nothing else uses.
`activity_master_csv` — the WBS source per §3 — is still on the old scheme.

The norms conflict is worse, because "start and end dates not meeting the norm" cannot be
evaluated when the norm itself is ambiguous:

| Activity | `mapping_master.Norms` | `activity_master_mapping.norms` |
|---|---|---|
| FLCV1000 | 0.4 | 0.5 |
| FLCV1040 | 1.0 | 0.625 |
| FLCV1070 | 3.0 | 1.375 |
| FLCV1090 | 5.0 | 3.0 |
| FLCV1140 | 3.0 | 2.5 |

### 6.2 The task_code → WBS chain breaks at hop 1

Per §3, `task_code` → `activity_id` → `activity_master_mapping.activity_code` →
`activity_master_csv.activity_group_description` (= WBS).

Measured over all 107,484 `task_daily` rows:

| Stage | Result |
|---|---|
| rows with no `-` in task_code | 0 |
| **unresolved at hop 1** (activity_id not in mapping) | **23,877 rows (22.2%)** |
| unresolved at hop 2 | 246 rows |
| distinct activity_ids missing from mapping | **621** |
| WBS values successfully resolved | 22 |
| activity_codes in mapping but missing from csv | 0 |

So hop 2 is clean; hop 1 loses 22% of all daily work. Top unmapped activity_ids:
`FLME1038` (757 rows across 238 wells), `FLC1490` (685 rows, 41 wells),
`MSHY1020` (602 rows), `MSSF1200` (573 rows), `HUP1090` (351 rows, 44 wells).

Additionally, `activity_master_mapping` has **4 duplicate `activity_id` values**
(`ENG1120`, `ENG1130`, `MIL2000`, `MIL2010`) and each duplicate pair has
`activity_code IS NULL` on both rows — so those activity_ids can never resolve.
`activity_code` is NULL on 10.29% of the mapping table.

### 6.3 UOM vocabulary drift

`M`/`m`, `No`/`Nos`, `Joint`/`Jts`, `Ea`, `Lot`/`LS`. Plus:

- **`#N/A` as a literal UOM string on 182 `mapping_master` rows.**
- **`m²` stored mojibake as `m�`** in both tables (34 + 26 rows) — the `m²`/`m2`
  distinction is lost.

---

## 7. Column-level data quality

### 7.1 Dead columns

80 of 936 columns (8.5%) are 100% NULL. Concentration:

| Table | Columns | Fully NULL | Dead % |
|---|---|---|---|
| `well.well_master` | 103 | **39** | **37.9%** |
| `well.task_daily` | 44 | 11 | 25.0% |
| `dbo.activity_task_plan` | 41 | 5 | 12.2% |
| `core.engineering_task_plan` | 34 | 4 | 11.8% |
| `dbo.engineering_acc_dump` | 75 | 7 | 9.3% (+46 more over 90% NULL) |

Empty tables: `ref.division`, `ref.location`, `ref.project_type`, `well.well_details`
(115 columns, 0 rows), `well.wmr_conversion` (68 columns, 0 rows).

### 7.2 FK-backed columns that are entirely NULL

`well.well_master.status_id`, `.flowline_const_status_id` and `.station_id` all have
declared foreign keys **and are 100% NULL**. Well status is unknown for every one of the
814 wells, so no status-driven logic can work.

### 7.3 Frozen columns

`well_master.cold_b`, `pole_hole_drill`, `pole_erect`, `conductor_string`, `ohl_ti`,
`overall_ohl` are all constant **0** (605 rows, rest NULL). `electri`, `instrumentat`,
`overall_comm_mi`, `overall_comm_progress` are all constant **1**. These are stuck values,
not measurements — and `overall_comm_progress` sounds like a progress metric.

### 7.4 Wrong type / wrong unit for the column name

| Column | Declared | Actual content |
|---|---|---|
| `well_master.progress` | `varchar(20)` | **dates** — `2026-03-04`, `2026-04-04`, … |
| `well_master.fl_length_m` | `nvarchar(255)` | `"1 km"`, `"0.9 km"`, `"1.1 Km"` — **km in a `_m` column**, unit in the string, mixed case |
| `well_progress.overall_progress` | `decimal(3,2)` | max 9.99 representable — fractions 0–1 |
| `well_progress.location_prep_progress` | `decimal(5,2)` | same 0–1 data, 100× wider type |
| `activity_task_plan.well_id` | `nvarchar(50)` | integers — `int` everywhere else |
| `mapping_master.Activity_ID` | **`text`** | deprecated type, cannot be compared with `=` |

8 columns still use the deprecated `text` type, including six JSON payloads in
`dbo.flaf_po_peg_scr_moc_document_data` (`POLjson`, `POFjson`, `FLAFjson`, `MOCjson`,
`PEGjson`, `SCRjson`).

### 7.5 Sentinels and encoding

- `well_master.ramz_id` = **`'NO FLAF'` on 25 wells** — a status message in an ID column.
- Sentinel strings in `flowline_dl`, `fl_dia`, `fl_length_m` (1 each) and
  `tie_in_port_no` (18).
- `well_master.ssfd_value`: 127 **empty strings** alongside 507 NULLs — two different
  "no value" representations in one column.
- **20,269 rows** of `activity_taskplan_job_progress.task_name` contain un-decoded HTML
  entities (`&amp;`).

### 7.6 Identity conflict — wells have two unrelated names

Comparing `well_master.ramz_id` against the latest `well_progress.well_name` for all 814 wells:

| | Wells |
|---|---|
| names differ | **260** |
| name only in `well_progress` (`ramz_id` NULL) | **554** |
| name only in `well_master` | 0 |

| well_id | `ramz_id` | `well_name` |
|---|---|---|
| 10206 | `SONRAKDS5114` | `RAKID-220` |
| 27129 | `SON000NM4624` | `NM-A_PI1` |
| 34422 | `SON000NM5542` | `AL BURJ-284` |
| 30314 | `SON000NM5525` | `NMR A_6515383_WSW_2` |

Two naming systems, no crosswalk table. Only 260 of 814 wells even have both names to
compare. Any report identifying a well by name will disagree with a report using the other
source.

### 7.7 Coordinates

562 wells have both `northing` and `easting`. Against Oman UTM Zone 40N bounds
(E 200 000–800 000, N 1 800 000–2 800 000): **3 are exactly zero**, 3 eastings and
4 northings fall outside range.

### 7.8 Un-enforced references

No FK on these, and they have orphans:

| Reference | Orphans |
|---|---|
| `task_daily.emp_id` → `ref.employee` | 26 distinct ids |
| `task_daily.project_id` → `project.project_mstr` | 8 distinct ids |
| `task_daily.crew_id` → `ref.crew` | 0 |

### 7.9 Employee nationality — matches the business rule

`ref.employee.nationality_type`: `Expat` 14,183 · `National` 2,802 · **NULL 1,491**.
Exactly the three groups §2 describes. The 1,491 unrecorded must always be reported
separately, never folded into Expat.

---

## 8. What already exists as a rules engine

`dbo.DataQualityCheckLog` — 4 rows, schema
`(check_run_id, run_datetime, check_area, check_description, issue_count, status)`.
Two distinct checks, both on `project.project_mstr`, both `PASS`, run twice on
2026-08-19: a row-count reconciliation and a field-level reconciliation against
`AppMasterDB_UAT.dbo.ProjectIDs`.

There are **no stored procedures or functions** in the database other than the SSMS
diagram helpers and `dbo.fn_GetPeriods`. So the existing deterministic engine runs
outside the database and writes its results here.

This table is the correct sequencing point: the Sentinel runs **after** it, reads it to
know what the deterministic layer already covered, and writes its own findings to new
tables rather than overloading this one. Its current coverage is 2 checks on 1 table out
of 78 — so there is no meaningful duplication risk today.

---

## 9. Discovery scripts

| Script | Purpose |
|---|---|
| `others/discovery/dbx.py` | connection, `.env` scope parsing, JSON output helper |
| `01_inventory.py` | server info, schemas, objects, row counts, scope flags |
| `02_columns_keys.py` | full column dictionary, PKs, FKs, CHECK constraints |
| `03_colsearch.py` | keyword search across all 936 column names |
| `04_profile.py` | per-column null%/distinct/min/max/blank/sentinel/future-date |
| `05_relations.py` | grain, duplicates, orphans, task_code→WBS resolution |
| `06_wbs_hier.py` | WBS hierarchy, weightage rollup, snapshot grain |
| `07_hier2.py` | parent/ancestor linkage, weightage semantics, 1900 placeholders |
| `08_rules.py` | the four named business-rule anomaly classes |
| `09_engine_and_sweep.py` | existing DQ engine artifacts + DB-wide dead-column sweep |
| `10_text_xref.py` | encoding, sentinels, cross-table name/coordinate consistency |
| `11_norms_xref.py` | norms/UOM/code disagreement between the two masters |

Run any of them with:

```bash
.venv/Scripts/python.exe others/discovery/04_profile.py "well.well_master"
```
