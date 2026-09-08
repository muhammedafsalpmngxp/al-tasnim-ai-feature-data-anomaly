# Complete Anomaly Taxonomy

The maximum set of data-quality and anomaly classes this database can express, organised by
dimension. **14 dimensions, ~165 classes.** Measured values come from `others/discovery/`; classes
marked *(no instance yet)* are checked anyway, because absence today is not absence next week.

Columns: **Gen** = emitted by the invariant generator from `column_semantics.yaml`;
**Hand** = hand-written from `BUSINESS_RULES.md`; **Stat** = statistical;
**LLM** = semantic discovery. **Well** = the finding attaches to a `well_id`.

---

## 1. COMPLETENESS — data absent

| id | Class | Measured | Src | Well |
|---|---|---|---|---|
| CMP-01 | Column 100% NULL (dead) | 80 of 936 | Gen | |
| CMP-02 | Column > 90% NULL | 46 in `engineering_acc_dump` alone | Gen | |
| CMP-03 | **FK-backed column 100% NULL** | `status_id`, `flowline_const_status_id`, `station_id` | Gen | ✓ |
| CMP-04 | Populated but single-valued (frozen) | 10 in `well_master` | Gen | |
| CMP-05 | **Populated but all-zero (no information)** | `etp.manhoursactual` on 128,641 rows | Gen | |
| CMP-06 | Empty table with a defined schema | 5, incl. `well_details` (115 cols) | Gen | |
| CMP-07 | Blank string used alongside NULL | `ssfd_value` 127 blanks | Gen | ✓ |
| CMP-08 | Sentinel text in a typed column | `ramz_id='NO FLAF'` ×25; `UOM='#N/A'` ×182 | Gen | ✓ |
| CMP-09 | **Parent row with no children** | 217 incomplete wells with **zero tasks** | Hand | ✓ |
| CMP-10 | **Child row with no parent** | 184 orphan wells in `task_daily` | Gen | ✓ |
| CMP-11 | NULL FK on the fact grain | 175 `task_daily` rows, `well_id` NULL | Gen | |
| CMP-12 | Audit columns never written | `task_daily.created_at`/`updated_at` 100% NULL | Gen | |
| CMP-13 | **Reference row decayed to unusable** | 13 `crew_type` rows: no code, no name | Gen | |
| CMP-14 | Attribution missing on the fact grain | `crew_type_id` 99.3% NULL; `crew_id` 67% | Gen | ✓ |
| CMP-15 | Contact/identity coverage collapse | `employee_contact` 3.31%; `email` 96.7% NULL | Gen | |

**CMP-09 is the business's own query.** 448 wells are incomplete; **217 have no task activity
at all**, and **45 of those are already past their `ex_rig_on_date`** — worst is well `10204`,
**245 days past** its master date with no pegging, no FLAF, no rig and no tasks.

> **Completeness must always be split by whether the value is *due*.** 1,278 raw "missing"
> schedule dates are **916 PENDING** (correctly awaiting a future milestone) and only
> **362 GAP** (genuinely overdue). Reporting the raw count sends a team chasing records that
> are already correct — see `01d`.

---

## 2. VALIDITY — value outside its legal domain

| id | Class | Measured | Src | Well |
|---|---|---|---|---|
| VAL-01 | Percentage outside [0,1] | `progress > 1` on 2 tasks; `< 0` on 2 | Gen | ✓ |
| VAL-02 | Weightage > 1.0 where a fraction is meant | `job_progress` max 1.1167 | Gen | |
| VAL-03 | **Weightage > 100 where a percent is meant** | `PW1` = 135.34 | Hand | |
| VAL-04 | Negative quantity | `required` min −1,992 | Gen | ✓ |
| VAL-05 | Negative hours / manhours | `data_hours` −2.5; `actual_manhours` −45.07 | Gen | ✓ |
| VAL-06 | Negative duration | drilling span −92 d; task spans −69 d | Gen | ✓ |
| VAL-07 | Coordinate outside the region | 3 zero, 3 easting, 4 northing out of Oman UTM 40N | Gen | ✓ |
| VAL-08 | Enum value not in its reference table | 0 today | Gen | ✓ |
| VAL-09 | Date before a plausible epoch | `week_number` = 1900-01-01 ×209 | Gen | ✓ |
| VAL-10 | **Actual date in the future** | rig_on 2, rig_off 2, `location_po_recvd` 19, `ActionOn` 307 | Gen | ✓ |
| VAL-11 | Date absurdly far future | `endDate` 2071-03-24; span 16,471 d | Gen | ✓ |
| VAL-12 | **Non-numeric in a numeric concept** | `fl_length_m` = `"1 km"` on 517 rows | Gen/LLM | ✓ |
| VAL-13 | Free text where an enum is expected | `fl_dia` 12 spellings of 3 diameters | Gen/LLM | ✓ |
| VAL-14 | Wrong concept entirely in the column | `Coriolis` in `fl_dia` (a flow meter) | LLM | ✓ |
| VAL-15 | Repeated-digit magnitude (entry error) | `planned` = 1,011,111 | Stat | ✓ |
| VAL-16 | Implausible magnitude | `actual_manhours` = 1,162,384 h = 132 years | Stat | ✓ |
| VAL-17 | Malformed composite key | `G01-07-01-54321`, 6 rows | Gen | |
| VAL-18 | **Phantom key value** | `well_id = 0` on `MSSF1180-00000` | Gen | ✓ |

---

## 3. CONSISTENCY — internal contradiction

| id | Class | Measured | Src | Well |
|---|---|---|---|---|
| CON-01 | **Flag contradicts measure** | `completed=0` & `progress>=1` on 2,804 tasks | Hand | ✓ |
| CON-02 | Flag contradicts date | `actual_end` set & `completed=0` on 2,218 | Hand | ✓ |
| CON-03 | Measure contradicts flag | `completed=1` & `progress<1` on 233 | Hand | ✓ |
| CON-04 | Completion date missing on a completed record | 141 tasks | Hand | ✓ |
| CON-05 | **Derived column contradicts its inputs** | `duration` ≠ P6 span on ~37,777 rows (35%) | Gen | ✓ |
| CON-06 | Stored aggregate contradicts recomputation | `prev_week_cum_progress` wrong on 3 of 4 | Hand | ✓ |
| CON-07 | **Lifecycle progress contradicts lifecycle date** | **273 of 605 completed wells show < 100%** | Hand | ✓ |
| CON-08 | ...and the inverse | 2 wells at 100% with no completion date | Hand | ✓ |
| CON-09 | **Rig off but construction < 100%** | **308 wells** | Hand | ✓ |
| CON-10 | Same fact, two tables, two values | well name differs on 260 wells | Gen | ✓ |
| CON-11 | Same-day duplicate rows **disagree** | **3,085 of 22,628 groups** | Gen | ✓ |
| CON-12 | Cross-well contamination | `well_id` ≠ task_code suffix in `job_progress` | Gen | ✓ |
| CON-13 | Two sources for one relationship disagree | `ref.crew.employees` vs bridge (4.9% coverage) | Gen | |
| CON-14 | Redundant duplicated key column | `employee.id` == `employee.employee_id` | Gen | |
| CON-15 | Mixed vocabularies in one column | `parent_wbs_code`: codes **and** names | LLM | ✓ |
| CON-16 | Denormalised list vs normalised table | `ref.crew.employees` = `[8484, 7056, …]` | LLM | |
| CON-17 | Unit inconsistency for one concept | `progress` `decimal(3,2)` vs `decimal(5,2)` | Gen | |
| CON-18 | Type inconsistency for one concept | `well_id`: `int` vs `nvarchar(50)` | Gen | ✓ |

**CON-07 is the largest consistency finding: 273 of 605 wells have `eng_completion_date` set
— the business definition of complete (§8) — yet `overall_progress < 1`.** Any dashboard
reading progress will under-report completion by 45%.

---

## 4. UNIQUENESS — duplication

| id | Class | Measured | Src | Well |
|---|---|---|---|---|
| UNQ-01 | Table has no primary key | 19 of 78 | Gen | |
| UNQ-02 | Declared PK reused across rows | `task_daily.id` ×3 | Gen | |
| UNQ-03 | **Business key duplicated** | `emp_uid` repeated, one ×3 | Gen | |
| UNQ-04 | ...with conflicting attributes | 1 `emp_uid`, 2 names | Gen | |
| UNQ-05 | **Grain key duplicated** | `(well_id, week_number)` 601 groups, **98,771 excess** | Gen | ✓ |
| UNQ-06 | **Daily-log grain duplicated** | `(task_code, well_id, ActionOn)` 22,628 groups, 23,789 excess | Gen | ✓ |
| UNQ-07 | Runaway append duplication | `activity_task_plan` up to **2,718×** | Gen | ✓ |
| UNQ-08 | Master-data key duplicated | `activity_id` ×4; `crew.code` ×1 | Gen | |
| UNQ-09 | Reference rows triplicated | `core.plant_description` every code ×3 | Gen | |
| UNQ-10 | Near-duplicate entity by normalised name | 677 `emp_name` groups, 1,087 extra | Stat | |
| UNQ-11 | **Phantom key from whitespace** | 37 WBS → 35 real, **2 phantom** | Gen | |

**UNQ-06 splits into two remediations, which is why the distinction matters:** of 22,628
same-day duplicate groups, **19,543 are byte-identical** (safe to dedup automatically) and
**3,085 hold different values** (two different truths for one task on one day — needs a human).

> The business asked about well `33776`: it is **clean** — 12 rows, 12 distinct task codes,
> 1.00 rows per task. The worst true duplication factor anywhere is 2.00.

---

## 5. REFERENTIAL INTEGRITY

| id | Class | Measured | Src | Well |
|---|---|---|---|---|
| REF-01 | Orphan on a declared FK | 0 — the 41 FKs hold | Gen | |
| REF-02 | **Orphan on an undeclared reference** | `emp_id` ×26, `project_id` ×8 | Gen | ✓ |
| REF-03 | **Mapping chain breaks at hop 1** | 23,877 rows (22.2%), 621 activity_ids | Hand | ✓ |
| REF-04 | **Whole discipline unmapped** | `HUP`, `LOC`, `OHL`, `CW*`, `NS*`, `RR*`, `RW*`, `RO*` — 100% | Hand | ✓ |
| REF-05 | Mapping row exists but its key is NULL | `activity_code` NULL on 10.29% | Gen | |
| REF-06 | Reference data unused | `well_type` 402 of 414; `cluster` 8 of 10 | Gen | |
| REF-07 | Reference row unusable but referenced | 4 crews → a `crew_type` with no code | Gen | |
| REF-08 | **Business rule names the wrong column** | crew_code → `crew_type_code` (53/57), not `crew.code` (0/57) | Hand | |
| REF-09 | Two identity systems, no crosswalk | `ramz_id` 68% NULL vs `well_name` 791 | Gen | ✓ |
| REF-10 | Master rows with no dependents | 13 of 19 projects have no wells | Gen | |
| REF-11 | Data-error placeholder in reference data | `ref.cluster` = `'Wrong Plant'` | LLM | |
| REF-12 | Code spaces colliding numerically | `plant_code 3525='GB'` vs `cluster 3525='Amal'` | LLM | |

---

## 6. TEMPORAL — date logic

**752 candidate date-order invariants exist across 82 date columns.** `well_master` alone has
36 date columns → 630 pairs. These are generated, not hand-written.

| id | Class | Measured | Src | Well |
|---|---|---|---|---|
| TMP-01 | **`start > end`, same role** | 45 invariants violated, **16,532 rows** | Gen | ✓ |
| TMP-02 | ...worst single case | `etp.actual_start > actual_end` **14,827 rows** | Gen | |
| TMP-03 | ...absurd magnitude | `dsq.earl_start > earl_end` by **−739,700 d** | Gen | |
| TMP-04 | `end` present, `start` absent | 1,062 in `etp`; 39 in `task_daily` | Gen | ✓ |
| TMP-05 | `start` present, `end` absent | 4 committed pairs | Gen | ✓ |
| TMP-05b | `committed_start <= committed_end` | **0 violations — clean** | Gen | ✓ |
| TMP-06 | Cross-role order violation | 651 generated invariants | Gen | ✓ |
| TMP-07 | **Lifecycle order broken (§7)** | `const_complete` after `rig_on` ×101 | Hand | ✓ |
| TMP-08 | | `rig_on` after `loc_finish` ×**266 wells** | Gen | ✓ |
| TMP-09 | | `rig_on` after `eng_finish` ×72 | Gen | ✓ |
| TMP-10 | | `pegged`/`flaf` after `rig_on` ×5 / ×5 | Hand | ✓ |
| TMP-11 | | `rig_on` after `rig_off` ×4 | Hand | ✓ |
| TMP-12 | | hook-up before rig-off ×3 | Hand | ✓ |
| TMP-13 | Planned pair inverted | `ex_rig_on > ex_rig_off` ×21 | Hand | ✓ |
| TMP-14 | **Planner target pair inverted** | 46 rows, worst −23 d | Gen | ✓ |
| TMP-15 | PO date after the milestone it enables | `location_po_recvd` after 7 later dates, 17–48 each | Gen | ✓ |
| TMP-16 | **Sub-cause: off by 1–7 days** | 42 of 51 inverted tasks | Stat | ✓ |
| TMP-17 | **Sub-cause: year component +1** | 2 tasks (305, 327 d) | Stat | ✓ |
| TMP-18 | **Culprit-column diagnosis** | 42 of 51: `actual_start`==`target_start` ⇒ **end is wrong** | Stat | ✓ |
| TMP-19 | Placeholder date as a NULL surrogate | `1900-01-01` on 86,357 cells | Gen | ✓ |
| TMP-20 | Blank-string date-cast trap | `TRY_CAST('' AS date)`→1900-01-01, 127 rows | Gen | ✓ |
| TMP-21 | Zero-length span — **NOT an anomaly** | 38,355 one-day tasks, correctly represented | — | |

> **`committed_start`/`committed_end` are `display_only`.** Their business meaning is not
> confirmed, so they appear in the report and UI as "Commit" (sortable) but are **never a
> baseline** for delay, variance or compliance. Only the two integrity checks above run on
> them, because those hold whatever the column turns out to mean.

**TMP-18 is what makes the report actionable.** Not *"51 inverted dates, investigate"* but
*"42 tasks: fix `actual_end`. 3 tasks: fix `actual_start`. 2 tasks: the year is typed one
year ahead."* Three work lists, three fixes.

---

## 7. MONOTONICITY — a cumulative measure must never decrease

The business asked for this specifically. **It cannot be built on `well.well_progress`.**

| | Measured |
|---|---|
| Rows in `well_progress` | 99,589 |
| After deduplicating `(well_id, week_number)` | ~818 |
| Wells with data | 605 |
| **Wells with exactly ONE week** | **601** |
| Wells with more than one week | **4** (max 2 weeks) |
| Wells parked at `week_number = 1900-01-01` | 209, all with zero progress |
| **Usable week-over-week transitions** | **4** |

There is no time series. Of the 4 transitions that exist, **3 have
`prev_week_cum_progress` greater than the current `overall_progress`** — a cumulative
measure going backwards on 75% of a 4-row sample.

**But the check is buildable on the tables that *do* have a time series:**

| id | Class | Measured | Src | Well |
|---|---|---|---|---|
| MON-01 | **`progress` decreased between days** | **4,697 transitions, 2,279 tasks**, worst drop −1.0 | Hand | ✓ |
| MON-02 | **`completed` reverted 1 → 0** | **2,181 transitions, 2,123 tasks** | Hand | ✓ |
| MON-03 | Stored cumulative < previous actual | 3 of 4 in `well_progress` | Hand | ✓ |
| MON-04 | Snapshot progress regressed | across `etp`'s 81 snapshots | Stat | |
| MON-05 | Weightage drifted between snapshots | `PW1` 18→19→22 in 3 days | Stat | |
| MON-06 | `remaining_duration` increased while progress rose | *(to measure)* | Gen | ✓ |
| MON-07 | Cumulative exceeds its own total | *(no instance yet)* | Gen | ✓ |
| MON-08 | Progress velocity implausible (0→100% in a day) | Stat | Stat | ✓ |

**Report both facts.** MON-01/02 are real, large and actionable. And the report must state
plainly that weekly cumulative-progress monotonicity **cannot be assessed** because
`well_progress` holds one week per well — that is itself a finding worth raising.

---

## 8. BUSINESS RULE COMPLIANCE — §4 deadlines

| id | Class | Deadline | Late | Overdue & missing | Well |
|---|---|---|---|---|---|
| BIZ-01 | **Pegging sheet** | `ex_rig_on − 60 d` | **19** | **117** | ✓ |
| BIZ-02 | **FLAF issued** | `ex_rig_on − 90 d` | **146** | **88** | ✓ |
| BIZ-03 | Location Construction | `ex_rig_on − 1 d` | rig rule only (§4) | — | ✓ |
| BIZ-04 | Flowline Construction | `ex_rig_on − 1 d` | rig rule only (§4) | — | ✓ |
| BIZ-05 | **Hook-up** | `rig_off + 2 d` | **313 of 366** | **95** | ✓ |
| BIZ-06 | Hook-up planned deadline pre-rig-off | `ex_rig_off + 2 d` | — | — | ✓ |
| BIZ-07 | **Task target past the master date** | `target_end > ex_rig_on` | **39,991 tasks, 449 wells**, worst 5,459 d | | ✓ |
| BIZ-08 | Rig off but tasks still open | — | 422 wells, 40,127 open | ✓ |
| BIZ-09 | Well complete but never pegged | — | 27 wells | ✓ |
| BIZ-10 | Well complete but no FLAF | — | 1 well | ✓ |
| BIZ-11 | Delay ownership: Location ⇒ Al Tasnim (penalty) | §6 | classification | ✓ |
| BIZ-12 | Delay ownership: Flowline ⇒ PDO, not a due well | §6 | classification | ✓ |
| BIZ-13 | Drilling attributed to Al Tasnim | §7 — PDO's activity | *(no instance)* | ✓ |
| BIZ-14 | Nationality reported without the NULL group | §2 — 1,491 unrecorded | Hand | |
| BIZ-15 | **Ahead of schedule — NOT a defect** | §5 | 7,141 tasks early | ✓ |

**BIZ-01/02 are the 60/90-day rules the business named.** Both are reported two ways —
*issued late* (the date exists but past deadline) and *missing and past due* (no date, deadline
gone). Wells where the deadline has **not** yet passed are `PENDING`, not findings: 137
pegging and 146 FLAF.

**BIZ-15 has its own report section.** §5 is explicit that early is good news, and it is the
easiest way for an LLM layer to produce a damaging report.

---

## 9. AGGREGATION & ROLLUP — §9 PMS weightage

| id | Class | Measured | Src |
|---|---|---|---|
| AGG-01 | **WBS weightage does not total 100% per project** | **44.0 / 65.99 / 270.69** | Hand |
| AGG-02 | Single WBS weightage exceeds the whole | `PW1` = 135.34 | Hand |
| AGG-03 | **Activities in one WBS not equally weighted** (§9 `1/N`) | 0.01 vs 0.008333 | Hand |
| AGG-04 | `1/weight` is not a whole number | 1/0.010714 = 93.3 | Hand |
| AGG-05 | Float precision noise | `0.9999990000000001` | Gen |
| AGG-06 | **All activities 100% but parent WBS < 100%** | on the pinned snapshot | Hand |
| AGG-07 | Parent 100% but a child < 100% | on the pinned snapshot | Hand |
| AGG-08 | Weighted rollup ≠ stored parent (tol 0.005) | on the pinned snapshot | Hand |
| AGG-09 | Orphan node in the WBS tree | 0 in the pinned snapshot | Hand |
| AGG-10 | **Unmapped work excluded ⇒ denominator wrong** | 23,877 rows outside every WBS | Hand |
| AGG-11 | **Phantom WBS inflates the weight total** | 2 of 37 | Gen |
| AGG-12 | WBS defined but never reached by work | 15 of 37 | Gen |

---

## 10. STATISTICAL OUTLIERS — *(the business asked for these explicitly)*

Median + MAD, never mean + σ. Every large peer group here is contaminated: `FLCV1260` runs
−69 to 65 days, `FLME1040` reaches 247 against an 18-day mean. A σ-based fence would widen
until it caught nothing.

| id | Class | Applied to | Reported as |
|---|---|---|---|
| OUT-01 | Robust z-score by peer group | duration per `activity_id` | score + peer group + n |
| OUT-02 | IQR fence 1.5× / 3× | `data_qty`, `data_hours`, `required`, `planned` | fence + distance |
| OUT-03 | **Extreme magnitude** | 1,162,384 manhours; 16,471-day span | value + median |
| OUT-04 | Norm-vs-actual ratio | `data_qty / (norms × duration)` | ratio + the norm used |
| OUT-05 | Plan-vs-actual ratio | target span vs actual span | 3,836 over-, 754 under-calculated |
| OUT-06 | Duration vs norm-implied duration | per `activity_id` | signed days |
| OUT-07 | Progress velocity | per task per day | %/day + median |
| OUT-08 | Cardinality collapse | 6 projects for 814 wells; 4 PMS values | distinct + expected |
| OUT-09 | Ingestion clustering | 36 `created_at` in a 35-second window | batch-load fingerprint |
| OUT-10 | Null-rate drift vs the previous run | every column | delta vs `dq.metric` |
| OUT-11 | Row-count drift vs the previous run | every table | delta |
| OUT-12 | Distribution shift | numeric columns | KS-style vs baseline |
| OUT-13 | Benford deviation | quantities, manhours | digit-frequency |
| OUT-14 | Rounding/heaping | quantities | mass at round values |

**Every outlier is `severity = review`, never `critical`.** An outlier is a question. And per
§5 an outlier in the *early* direction is labelled "ahead of schedule", not flagged.

---

## 11. FORMAT & SEMANTICS — *(only the LLM layer catches most of these)*

| id | Class | Measured | Src |
|---|---|---|---|
| FMT-01 | **Text column is 100% numeric** | `buffer_status` — a day count, 814 rows | Gen |
| FMT-02 | **Text column is 100% dates** | `progress` — 111 of 111 | Gen |
| FMT-03 | **Unit contradicts the column name** | `fl_length_m` holds km | LLM |
| FMT-04 | Unit embedded in the value | `"1 km"`, `"1.1 Km"` | Gen |
| FMT-05 | Vocabulary drift within a column | `fl_dia` 12 spellings | Stat |
| FMT-06 | UOM vocabulary drift across tables | `M`/`m`, `No`/`Nos`, `Joint`/`Jts` | Gen |
| FMT-07 | **Control characters in a grouping key** | 7 WBS contain `CHAR(10)` | Gen |
| FMT-08 | Values collapsing on normalisation | 37 WBS → 35 | Gen |
| FMT-09 | Spelling error in reference data | `Commisoning`; `Mamul` | LLM |
| FMT-10 | Trailing-space variants | `'Painting '` vs `'Painting Yard'` | Gen |
| FMT-11 | Un-decoded HTML entities | 20,269 rows with `&amp;` | Gen |
| FMT-12 | Mojibake | `m<?>` for `m²`, 60 rows | Gen |
| FMT-13 | Deprecated column type | 8 `text`/`ntext` columns | Gen |
| FMT-14 | Identifier format families | `ramz_id`: 4 shapes + 25 non-numeric | Stat |
| FMT-15 | Placeholder inside a name | `'VINOD KUMAR .'` — trailing `" ."` | LLM |

---

## 12. MASTER DATA & MIGRATION

A single coherent story: **a code migration is half finished across activities *and* crews.**

| id | Class | Measured | Src |
|---|---|---|---|
| MDM-01 | **Two masters disagree on norms** | 120 of 387 (31%) | Hand |
| MDM-02 | **Two code schemes coexist** | activity code: 387 of 387 disagree | Hand |
| MDM-03 | **Crew codes: same migration** | `crew_type_code` vs `Crew_group_code`: **49 of 50 mismatch** | Gen |
| MDM-04 | ...and the new scheme matches `mapping_master.New_Crew_code` | `CRW1308` → `FCG-0402` | LLM |
| MDM-05 | UOM disagrees for the same activity | 68 of 387 | Gen |
| MDM-06 | Norms zero / absurd | 39 NULL, 1 over 100, max 500 | Gen |
| MDM-07 | Reference decay by id range | ids 330–344 progressively emptier | Stat |
| MDM-08 | Old scheme still in the WBS source | `activity_master_csv` on old codes | Hand |

**MDM-03 corrects an earlier statement of mine.** I reported `Crew_group_code` as 100% NULL;
it is populated on 63 of 159 rows, and it holds the **new** crew-code scheme
(`ETS0601` → `FET-0501`, `SRV0401` → `SSS-0401`). Only **1 of 50** pairs agrees. Combined
with `mapping_master.New_Activity_Code`, the same migration is stalled in both dimensions.

---

## 13. RESOURCE PLAUSIBILITY

| id | Class | Measured | Src | Well |
|---|---|---|---|---|
| RES-01 | **Rig on two wells at once** | **481 pairs, 16 rigs**, worst 24-day overlap | Hand | ✓ |
| RES-02 | Crew on overlapping assignments | *(needs validity dates — none exist)* | Hand | |
| RES-03 | Employee in implausibly many crews | 51 crews for one employee | Stat | |
| RES-04 | Crew without supervisor | 1,307 of 7,582 (17%) | Gen | |
| RES-05 | Crew without employees | 1,267 of 7,582 (17%) | Gen | |
| RES-06 | Manhours > 24 h per person-day | 0 — `daily_actual_hours` is 100% NULL | Gen | ✓ |
| RES-07 | Status field never maintained | 2 of 18,476 employees inactive | Stat | |
| RES-08 | Equipment/type unused | 13 of 91 types | Gen | |
| RES-09 | Productivity vs norm | **blocked** — actual qty/hours empty | Stat | ✓ |

**RES-09 is a stated scope limit, not a hidden gap.** `task_daily.daily_actual_hours` and
`daily_actual_quantity` are 100% NULL; `etp.manhoursactual` is all zeros. Productivity
checks fall back to `data_qty`/`data_hours` (59% coverage, contains negatives) or
`job_progress.actual_manhours` (range −45 → 1,162,384). Both need outlier gating first, and
the report says so rather than printing a number nobody can trust.

---

## 14. PIPELINE & FRESHNESS

| id | Class | Measured | Src |
|---|---|---|---|
| PIP-01 | **Snapshot table read without pinning** | 81 snapshots, 2 on one day | Hand |
| PIP-02 | Snapshot stale vs today | latest `etp` = 2026-07-29 | Gen |
| PIP-03 | Table not refreshed since last run | vs `dq.metric` | Gen |
| PIP-04 | Audit timestamps absent ⇒ no change detection | `activity_task_plan` | Gen |
| PIP-05 | Batch-load fingerprint | revenue: 36 timestamps in 35 s | Stat |
| PIP-06 | Row count moved by more than a threshold | vs baseline | Stat |
| PIP-07 | New column appeared / disappeared | schema diff vs baseline | Gen |
| PIP-08 | Existing DQ engine coverage gap | `DataQualityCheckLog`: 2 checks, 1 of 78 tables | Hand |

---

## 15. The well-centric view — the report's primary axis

Most classes above carry a `well_id`. That makes the well the natural unit, which is what
the business asked for. Rolling every well-linked class into a per-well score:

| Anomalies per well | Wells |
|---|---|
| **0 — clean** | **332 (41%)** |
| 1 | 118 |
| 2–3 | 153 |
| 4–9 | 167 |
| **10 or more** | **44** |

Worst offenders:

| well_id | ramz_id | Unmapped | Progress/flag | Date inv. | Tasks | **Total** |
|---|---|---|---|---|---|---|
| 33151 | `SON000TQ5173` | 118 | 8 | 0 | 131 | **126** |
| 30750 | `SON000NM5198` | 74 | 11 | 0 | 167 | **85** |
| 29437 | `SON000TQ4192` | 69 | 12 | 0 | 83 | **81** |
| 32257 | *(no ramz_id)* | 42 | 8 | 0 | 42 | **50** |
| 33141 | `SON000TQ4200` | 48 | 0 | 0 | 48 | **48** |
| 36078 | `SON000NM5682` | 4 | 22 | 1 | 80 | **27** |

Well `33151` has 118 of its 131 tasks unmapped to any WBS — so its WBS progress is
computed over 10% of its actual work. That is a single, specific, fixable statement, and it
is only visible once findings are grouped by well.

**Excel sheet 5 "Well Scorecard"** is therefore the workbook's centre: 814 rows, one per
well, with a column per anomaly family, the lifecycle stage, the key dates, and a total —
sortable, filterable, and each cell drilling through to its evidence rows.

---

## 16. Totals

| Source | Checks |
|---|---|
| Generated invariants (F1–F7) | ~929 |
| Hand-written business rules (§4, §5, §7, §9) | ~50 |
| Domain checks from discovery | ~74 |
| Statistical (OUT-01…14 × peer groups) | ~40 |
| LLM semantic discovery | unbounded |
| **Total deterministic** | **~1,093** |
| **Anomaly classes in this taxonomy** | **~165 across 14 dimensions** |

Every one passes the six VERIFY gates before it prints, and every finding carries its
`class` (VIOLATION / DEFECT / GAP / PENDING / DESIGN / RISK / REVIEW), its `grain`, its
`baseline`, and its `well_id` where one applies.
