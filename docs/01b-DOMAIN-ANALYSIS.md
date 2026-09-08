# Domain Analysis — Wells · Activities · Employees

Second discovery pass. Where `01-DISCOVERY-FINDINGS.md` catalogued *structure*, this
document explains *what the data means* and what its formats actually are — the
understanding the check engine has to encode.

Measured 2026-09-07 by `others/discovery/12_domain_wells.py`, `13_domain_activities.py`,
`14_domain_employees.py`, `15_verify_gaps.py`. Run with the conda env:

```bash
C:/Users/USER/miniconda3/envs/mycuda/python.exe others/discovery/12_domain_wells.py
```

---

## 1. WELLS

### 1.1 Lifecycle stage — where the 814 wells actually are

Derived from the §7 order (each stage implies all prior dates are set):

| Stage | Wells |
|---|---|
| 7. **Completed** — hook-up done | **366** |
| 6. Drilled, awaiting hook-up | 97 |
| 5. Drilling (rig on) | 9 |
| 4. Pegged + FLAF, in construction | 95 |
| 3. FLAF only | 30 |
| 2. Pegged only | 28 |
| 1. **Nothing started** | **189** |

189 wells (23%) have neither a pegging sheet nor a FLAF, yet all 814 carry an
`ex_rig_on_date`. Those are the wells the deadline checks fire hardest on.

### 1.2 Well identity is genuinely broken

Two independent naming systems, and the primary one is mostly empty:

| `well_master.ramz_id` | Wells |
|---|---|
| **NULL** | **554 (68%)** |
| `SON000NM####` | 169 |
| `SONRAKDS####` | 35 |
| `SON<other>` (e.g. `SON000TQ4192`) | 31 |
| **`NO FLAF`** — no digits, a status message | **25** |

So only 235 wells have a real `ramz_id`. Meanwhile `well_progress.well_name` covers 791
distinct names in a completely different format family:

| Format | Distinct names | Example |
|---|---|---|
| `underscore_separated` | 542 | `23_MMHE_WF_PHASE2_PREDG1_OP_3` |
| `hyphen-separated` | 196 | `AL BURJ-279` |
| both `_` and `-` | 52 | `22_MM-GSR-MSC-APP-WI-1` |
| contains spaces | 1 | `BRNW EXT 6` |

`well_name` is at least hygienic — 0 untrimmed, 0 double-spaced, 791 distinct before and
after normalising. **No well is entirely nameless** (0 wells lack both), but there is no
crosswalk, and 260 wells disagree between the two.

`well_id` runs 4,645 → 38,008 for 814 wells — **2.44% density**. It is an external system's
key, not a local sequence, so gaps are expected and must not be flagged.

### 1.3 Rig scheduling is physically impossible

**481 overlapping rig assignments across 16 rigs.** A rig cannot be on two wells at once.

| rig_id | well A | A on → off | well B | B on → off | Overlap |
|---|---|---|---|---|---|
| 6 | 33096 | 2025-12-04 → 2026-02-01 | 33097 | 2025-12-19 → 2026-01-11 | **24 d** |
| 6 | 33096 | 2025-12-04 → 2026-02-01 | 33099 | 2026-01-10 → 2026-02-02 | 23 d |
| 8 | 37624 | 2026-01-21 → 2026-02-08 | 37625 | 2026-01-05 → 2026-02-20 | 19 d |
| 11 | 36343 | 2026-01-23 → 2026-02-03 | 36345 | 2026-01-23 → 2026-02-03 | 12 d |

Well 33097's entire rig period sits inside well 33096's. This is a new anomaly class not in
the original four, and it is checkable with certainty.

### 1.4 Column formats — what the varchar columns really contain

| Column | Declared | Non-NULL | Parses as date | Parses as number | Reality |
|---|---|---|---|---|---|
| `buffer_status` | `varchar(20)` | 814 | 0 | **814** | **100% numeric** — a day count (range ~30–67), 177 distinct. Misnamed and mistyped. |
| `progress` | `varchar(20)` | 111 | **111** | 0 | **100% dates.** A column called `progress` holds only dates. |
| `fl_length_m` | `nvarchar(255)` | 564 | 1 | 46 | 517 values are text like `"1 km"` — **km in a metres column** |
| `ssfd_value` | `varchar(10)` | 307 | 127 | 0 | the 127 "dates" are the **empty strings** — `TRY_CAST('' AS date)` returns `1900-01-01` |
| `ipm` | `varchar(20)` | 698 | 0 | 0 | rig codes: `SWER149`, `SWERIG81` — 19 distinct. Not a flag. |

> **Parsing trap for the engine:** in SQL Server `TRY_CAST('' AS date)` succeeds and returns
> `1900-01-01`. Any type-inference check must test for blank *before* testing castability,
> or 127 empty strings get counted as valid dates.

### 1.5 `fl_dia` — one concept, twelve spellings

| Value | Rows |
|---|---|
| `4"` | 260 |
| `6"` | 250 |
| (NULL) | 250 |
| `4 inch` | 21 |
| `6 inch` | 12 |
| `6` | 5 |
| `10"` | 5 |
| **`Coriolis`** | **4** |
| `6" inch` | 2 |
| `4" inch` | 2 |
| `6 inch CS-PE` | 1 |
| `6 inches` | 1 |
| `N/A` | 1 |

`4"`, `4 inch` and `4" inch` are one diameter written three ways. `Coriolis` is a flow
meter, not a diameter — wrong concept entirely. `6 inch CS-PE` packs diameter and material
into one field.

By contrast `lift_type` is clean — `PCP` 294, `WI` 99, `PI` 55, `ESP` 53, `OP` 23, `BP` 16,
`WSW` 6, `DWI` 5 — and `tie_in_port_avail` is `NO` 441 / `YES` 159 / NULL 214.

### 1.6 Reference data is 97% dead

| Reference table | Rows | Actually used | Unused |
|---|---|---|---|
| `well.well_type` | 414 | **12** | **402 (97%)** |
| `ref.cluster` | 10 | **2** | 8 |
| `well.well_location` | 790 | 780 | 10 |
| `ref.employee_type` | 1,089 | 901 | 188 |
| `ref.equipment_type` | 91 | 78 | 13 |

All 814 wells sit in just two clusters — **Marmul 432** and **Nimr 377** — plus **5 wells
with a NULL `cluster_code`** despite the FK. And `ref.cluster` contains a row literally
named **`Wrong Plant`** (code 3532): a data-error placeholder living in a reference table.

---

## 2. ACTIVITIES

### 2.1 `task_code` format

| Segments | Task rows | Distinct codes | Example |
|---|---|---|---|
| 2 | 99,658 | 34,380 | `COC1000-13616` |
| 3 | 7,820 | 1,063 | `COEI1260-900-1` |
| 4 | **6** | 1 | `G01-07-01-54321` |

**In `well.task_daily` the suffix matches the `well_id` column on 107,309 of 107,309 rows
— 100%.** So `task_daily` is internally consistent, and the cross-well contamination found
in §5.5 of the structural findings is confined to
`dbo.activity_taskplan_job_progress`. That is an important distinction: the two tables need
different checks, not the same one.

The 6 four-segment rows (`G01-07-01-54321`) yield `activity_id = 'G01'`, which maps to
nothing — a malformed code.

### 2.2 The mapping chain fails by *discipline*, not at random

This is the most important activity finding. The 22.2% unmapped figure hides the shape of
the problem: **whole activity prefixes are 100% unmapped, while the flowline prefixes are
almost fully mapped.**

| Prefix | Meaning | Task rows | Unmapped | % | Wells |
|---|---|---|---|---|---|
| `FLME` | Flowline mechanical | 37,380 | 775 | **2.1%** | 497 |
| `FLCV` | Flowline civil | 27,747 | 1,239 | **4.5%** | 513 |
| `FLEI` | Flowline E&I | 10,437 | 252 | **2.4%** | 386 |
| `FLC` | Flowline (other) | 5,353 | 5,353 | **100%** | 67 |
| **`HUP`** | **Hook-up** | **3,031** | **3,031** | **100%** | **63** |
| `MSPF` | Material shifting | 2,444 | 2,444 | **100%** | 2 |
| **`LOC`** | **Location construction** | **1,625** | **1,625** | **100%** | **52** |
| `MSSF` | Material shifting | 1,617 | 1,617 | **100%** | 2 |
| `MSHY` | Material shifting | 1,419 | 1,419 | **100%** | 2 |
| `CWME`,`CWCV`,`CWEI`,`CWCO` | Conversion | 1,155 | 1,155 | **100%** | 16 |
| `NSME`,`NSCV`,`NSEI`,`NSCO` | New slot | 591 | 591 | **100%** | 6 |
| `RRME`,`RRCV`,`RREI`,`RRCO` | Re-entry | 381 | 381 | **100%** | 13 |
| `RWME`,`RWCV`,`RWEI`,`RWCE` | Re-work | 297 | 297 | **100%** | 5 |
| `ROEI`,`ROME`,`ROCV` | Reopen | 141 | 141 | **100%** | 7 |
| `OHL` | Overhead line | 113 | 113 | **100%** | 20 |

**`activity_master_mapping` only covers the FL* flowline family.** Hook-up (HUP),
Location Construction (LOC), overhead line (OHL), and every conversion / re-entry /
re-work / reopen / yard prefix is entirely absent from it.

The consequence for the business rules is direct: **Location Construction and Hook-up are
two of the five milestones in §4, and neither can be rolled up to a WBS at all.** This is
not a scattered data-entry problem — it is a missing master-data domain.

`activity_master_mapping.project_type` confirms the gap by design:

| project_type | Activities | Unmapped to WBS |
|---|---|---|
| Flowline | 145 | 13 |
| SNLP | 133 | 10 |
| Conversion without Flowline | 76 | 12 |
| Location | **25** | 4 |

Only 25 Location activities are registered, against 38 distinct `LOC*` activity_ids in use.

### 2.3 WBS vocabulary — phantom WBS from whitespace

37 distinct `activity_group_description` values are stored. **Seven of them contain a
literal line-feed character:**

| WBS (escaped) | Length | Activities |
|---|---|---|
| `Flowline<LF>Boxup & Hydrotest` | 26 | 10 |
| `LV / HV <LF>Cable Trench` | 21 | 4 |
| `RDX / WDX <LF>Excavation & prepad` | 30 | **9** |
| `RDX / WDX<LF>Excavation & prepad` | 29 | **2** |
| `RDX / WDX <LF>Fabrication & Lowering` | 33 | **7** |
| `RDX / WDX<LF>Fabrication & Lowering` | 32 | **3** |
| `RDX / WDX <LF>Postpad & Backfilling` | 32 | 2 |

Two pairs differ **only by one space** before the line-feed. Normalising whitespace:

```
WBS as stored:            37
WBS after normalising:    35
PHANTOM WBS:               2
```

`RDX / WDX Excavation & prepad` really holds 11 activities but is split 9 + 2 across two
"different" WBS. `RDX / WDX Fabrication & Lowering` is split 7 + 3.

This is exactly the phantom-WBS arithmetic the business rules warn about, and it corrupts
§9's weightage denominator: a WBS split in two gets counted twice in the weight total, and
its activities each get `1/9` and `1/2` instead of a common `1/11`.

Further near-duplicates that are probably the same WBS:

| Variant A | Variant B |
|---|---|
| `Painting ` (trailing space, YARD) | `Painting Yard` (YARD) |
| `PE Pulling ` (Mechanical) | `PE Pulling` (YARD) |
| `Hydrotest` (YARD) | `Hydrotest Yard` (YARD) |
| `Structural Fabrication` (YARD) | `Structure Fabrication Shop` (YARD) |

And **`Commisoning Assistance` is misspelled** (should be "Commissioning") in all three
disciplines it appears under — Electrical, Instrument, Mechanical.

**Only 22 of the 37 stored WBS are reachable from real task work.** 15 WBS groups are
defined but no task ever resolves to them.

### 2.4 Crew mapping — the business rule points at the wrong column

§3 says crew comes from `activity_master_csv.crew_code`. Testing every candidate target:

| Join target | Codes matched (of 57) |
|---|---|
| `ref.crew.code` | **0** |
| `ref.crew.composition` | 50 |
| **`ref.crew_type.crew_type_code`** | **53** |
| `ref.crew_type.Crew_group_code` | 0 |

`crew_code` values look like `BPR0504`, `CBP0704`, `CCL0803`. `ref.crew.code` looks like
`3522-CCL1103-967401` — a composite of cluster + crew type + supervisor. So
**`activity_master_csv.crew_code` resolves to `ref.crew_type.crew_type_code`, not
`ref.crew`** — and 4 of 57 codes resolve to nothing at all.

`ref.crew_type.Crew_group_code` is 100% NULL, so the "crew group" concept is unpopulated.

### 2.5 Duration and norm data

Peer groups for statistical norm checks are healthy in size but contaminated:

| activity_id | n | min | max | avg | Negative |
|---|---|---|---|---|---|
| `FLME1150` | 3,605 | −1 | 179 | 26.7 | 10 |
| `FLCV1260` | 2,498 | **−69** | 65 | 12.5 | 5 |
| `FLME1180` | 2,279 | −1 | 193 | 19.6 | 5 |
| `FLME1040` | 1,653 | −1 | **247** | 18.4 | 6 |
| `FLME1090` | 1,367 | −1 | 244 | 23.3 | **17** |
| `FLME1046` | 1,461 | −1 | 133 | 24.1 | **13** |

Every large peer group contains negative durations, and maxima run 10× the mean. **This is
why the statistical layer must use median + MAD, not mean + standard deviation** — a
247-day value against an 18-day mean would widen a σ-based fence until it catches nothing.

**Planned vs actual — the user's rule 2, quantified.** Over 83,037 rows where all four
dates exist:

| Measure | Rows |
|---|---|
| **Zero-length plan** (`target_start = target_end`) | **36,368 (43.8%)** |
| **`duration` column disagrees with target dates by > 1 day** | **32,960 (39.7%)** |
| Plan over 3× the actual — **over-calculated** | 3,836 |
| Actual over 3× the plan — **under-calculated** | 754 |

43.8% of tasks are planned as zero-length, and the stored `duration` contradicts the stored
target dates on 39.7% of rows. Those two facts alone make any norm comparison unreliable
until one source is declared authoritative.

---

## 3. EMPLOYEES, CREWS, EQUIPMENT

### 3.1 Nationality is clean and matches the rule

`ref.employee.nationality_type` holds exactly two strings, correct case, no variants:

| Value | Employees | Companies | Employee types |
|---|---|---|---|
| `Expat` | 14,183 | 18 | 818 |
| `National` | 2,802 | 7 | 286 |
| **NULL — unrecorded** | **1,491** | 13 | 85 |

No `'Omani'`, `'Local'` or numeric code exists — §2 holds. The 1,491 unrecorded (8.1%) are
a real third group and must always be reported separately. **0 employees have conflicting
nationality across duplicate `emp_uid`** — that part is consistent.

### 3.2 Employee identity problems

`ref.employee` has 13 columns; note it carries **both `id` and `employee_id`**. Verified:
18,476 distinct each, 0 NULL, **0 disagreements** — a fully redundant duplicated key
column. Harmless today, a divergence risk forever.

| Issue | Count |
|---|---|
| `email` NULL | **17,858 of 18,476 (96.7%)** |
| `atnm_sc` NULL | 1,347 |
| `emp_status = 0` (inactive) | **2** |
| **Duplicate normalised `emp_name` groups** | **677** (1,087 extra rows, worst 12) |
| **Duplicate `emp_uid`** — the business identifier | 10+ groups, one appears 3× |
| `emp_uid` with 2 different names | at least 2 |
| `employee_contact` coverage | **612 of 18,476 = 3.31%** |

Only **2 of 18,476 employees are marked inactive** — for a contractor with years of
history, that means the status field is never maintained, so headcount cannot be derived.

Duplicate names cluster on missing surnames — `VINOD KUMAR .` ×12, `ANIL .` ×11,
`MANOJ KUMAR .` ×11, all with a trailing `" ."` placeholder. Each has a distinct `emp_uid`,
so these are likely genuinely different people with truncated names — a **name-quality**
issue, not necessarily duplication. But `emp_uid` duplicates *are* true identity collisions:
`emp_uid = 128034205` appears 3 times, and `110338646` carries two different names.

Referential health is otherwise good: 0 orphan `company_id`, 0 orphan `employee_type_id`,
0 orphan employees in `bridge.crew_employee`. But 188 of 1,089 employee types and 1 company
have no employees.

### 3.3 Crews — two competing sources, both incomplete

`ref.crew` (7,582 rows) stores `employees` and `equipments` as **delimited strings**, not
rows:

```
crew_id 84  code 3522-CCL1103-967401  employees [8484, 7056, 8483, 7380, 5952, 7100, 7576]
crew_id 181 code 3522-SSS-0401-116697 employees [7258, 8688]  equipments []
```

`bridge.crew_employee` is the normalised alternative. They disagree:

| | Value |
|---|---|
| `ref.crew` rows | 7,582 |
| crews present in `bridge.crew_employee` | **6,242** → 1,340 crews have no bridge rows |
| distinct employees in the bridge | **906** of 18,476 (**4.9%**) |
| orphan `crew_id` in the bridge | 19 |
| `ref.crew` with no supervisor | **1,307 (17%)** |
| `ref.crew` with no employees | **1,267 (17%)** |
| duplicate `ref.crew.code` | 1 (`NIM_CCL1103_01`) |

And **employees 8728 and 8722 are each assigned to 51 crews**; 10395 to 48, 10411 to 44.
Either crews are historical records with no validity dates, or assignment is uncontrolled.
There is no date column on `bridge.crew_employee` to distinguish the two — so
concurrent-assignment cannot be proven, only flagged.

### 3.4 Manhours and quantities — mostly absent or absurd

This determines whether norm-based productivity checks are possible at all.

| Source | Rows | Populated | Min | Max |
|---|---|---|---|---|
| `task_daily.daily_actual_hours` | 107,484 | **0** | — | — |
| `task_daily.data_hours` | 107,484 | 63,419 | **−2.5** | 11.7 |
| `etp.manhours` (planned) | 128,643 | 128,643 | 0 | 33,823.2 |
| **`etp.manhoursactual`** | 128,643 | 128,641 | **0** | **0** |
| `job_progress.actual_manhours` | 84,790 | 84,790 | **−45.07** | **1,162,384.37** |

- `daily_actual_hours` is **100% NULL**.
- `manhoursactual` is populated on 128,641 rows and **every value is zero** — a column that
  exists, is written, and carries no information.
- `job_progress.actual_manhours` reaches **1,162,384 hours on a single task** — 132 years
  of continuous work — and goes negative at −45.07.

Quantities are similar:

| Source | Populated | Min | Max |
|---|---|---|---|
| `task_daily.daily_actual_quantity` | **0** | — | — |
| `task_daily.data_qty` | 63,445 | 0 | 9,500 |
| `task_daily.required` | 107,484 | **−1,992** | 133,832.4 |
| `task_daily.planned` | 107,483 | 0 | **1,011,111** |
| `etp.qty` / `etp.qtyactual` | 128,643 | 0 | 96 / **8** |

`required` goes negative. `planned` peaks at `1011111` — a repeated-digit value that reads
as a keyboard entry error. `etp.qtyactual` caps at 8 while `qty` reaches 96.

**Conclusion: norm-based productivity checks (`actual_qty / (norms × duration)`) cannot be
built on `task_daily` — the actual quantity and hour columns are empty.** They must use
`data_qty`/`data_hours` (59% coverage, contains negatives) or
`job_progress.actual_manhours` (100% coverage, wildly out of range). Both need outlier
gating first. This is a scope constraint to state in the report, not a defect to hide.

### 3.5 `well.task_daily` usable-column map

| Column | Populated | % |
|---|---|---|
| `progress`, `completed`, `startDate`, `endDate`, `duration`, `remaining_duration`, `required`, `ready` | 107,484 | 100.0 |
| `planned`, `target_start`, `target_end` | 107,483 | 100.0 |
| `uom_id` | 107,166 | 99.7 |
| `actual_start` | 103,100 | 95.9 |
| `task_assignee` | 90,493 | 84.2 |
| `actual_end` | 83,076 | 77.3 |
| `committed_start` / `committed_end` | 56,812 / 56,571 | 52.9 / 52.6 |
| **`created_at`, `updated_at`, `daily_completed`, `planned_crew`** | **0** | **0.0** |
| `crew_id` NULL | 72,147 | 67% missing |
| `emp_id` NULL | 74,563 | 69% missing |
| `crew_type_id` NULL | 106,690 | **99.3% missing** |
| `daily_employee_ids` NULL/blank | 107,484 | **100% missing** |

**No audit trail** (`created_at`/`updated_at` 100% NULL) means change-detection between runs
must be done by content hashing, not timestamps. **No crew/employee attribution** on 67–100%
of rows means crew-level productivity reporting is not possible from this table.

---

## 4. New anomaly classes this pass added

Beyond the four the business named:

| # | Class | Evidence |
|---|---|---|
| 1 | **Rig double-booking** | 481 overlapping pairs, 16 rigs |
| 2 | **Phantom WBS from whitespace** | 37 stored → 35 real |
| 3 | **Whole disciplines unmapped** | HUP, LOC, OHL, CW*, NS*, RR*, RW*, RO* all 100% |
| 4 | **Zero-length plans** | 36,368 rows (43.8%) |
| 5 | **`duration` contradicts target dates** | 32,960 rows (39.7%) |
| 6 | **Columns that exist but carry no information** | `manhoursactual` all-zero on 128,641 rows |
| 7 | **Absurd magnitude outliers** | 1,162,384 manhours; `planned` = 1,011,111 |
| 8 | **Negative measures** | hours −2.5, manhours −45.07, `required` −1,992 |
| 9 | **Vocabulary drift within a column** | `fl_dia` 12 spellings of 3 diameters |
| 10 | **Wrong concept in a column** | `Coriolis` in `fl_dia`; dates in `progress`; day-count in `buffer_status` |
| 11 | **Business rule points at the wrong column** | crew_code → `crew_type`, not `crew` |
| 12 | **Duplicate business identifier** | `emp_uid` repeated, 2 names under one uid |
| 13 | **Status field never maintained** | 2 of 18,476 employees inactive |
| 14 | **Reference data 97% unused** | `well_type` 402 of 414 dead |
| 15 | **Data-error placeholder as reference data** | `ref.cluster` = `Wrong Plant` |
| 16 | **Redundant duplicated key column** | `employee.id` == `employee.employee_id` |
| 17 | **Denormalised list in a string column** | `ref.crew.employees` = `[8484, 7056, …]` |
| 18 | **Two sources for one relationship, disagreeing** | `ref.crew.employees` vs `bridge.crew_employee` (4.9% coverage) |
| 19 | **Blank-string date-cast trap** | `TRY_CAST('' AS date)` → `1900-01-01`, 127 rows |
| 20 | **Malformed composite key** | `G01-07-01-54321`, 6 rows |

Together with the structural findings, the check catalogue now stands at **~110 checks**.
