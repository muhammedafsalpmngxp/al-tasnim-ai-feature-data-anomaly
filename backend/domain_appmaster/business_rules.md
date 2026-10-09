# BUSINESS RULES — PDO / AL TASNIM WELL DELIVERY (AppMasterDB + AppMasterEngDB)

Authoritative business interpretation for the two operational databases. Follow it exactly. Do
not infer, modify, or invent a definition. If a check needs a rule that is not stated below, say
the rule is **not yet defined** — do not assume one.

Figures quoted as "measured" were read from the live data on 2026-10-08. They describe the data's
shape so a probe is written against reality; they are not thresholds, and they will drift.

**Two databases are in scope, so every table name has THREE parts — `database.schema.table` —
written exactly as the SCHEMA block lists it.** A two-part name resolves against the connection's
own database and silently reads the wrong table, or fails, for anything in the other one. The PDO
well id is the only key the two databases share (§2).

## 1. Parties, clusters and scope

| Term | Meaning |
|---|---|
| **PDO** | Petroleum Development Oman — the client. Issues the pegging sheet and the FLAF; performs drilling. |
| **Al Tasnim** | The contractor. Performs Location Construction, Flowline Construction, hook-up and commissioning. |
| **Well** | The central entity. Identified everywhere by the **PDO well id**. |
| **Cluster** | The field area a well belongs to: `Nimr`, `Marmul` or `Amal` (exact strings). |

Each well has **two construction projects**: **Location Construction** and **Flowline
Construction**. They are separate — never merge or substitute one for the other.

## 2. Two databases, one well — cross-database rules

Every table is named with THREE parts: `database.schema.table`. Two databases are in scope and a
query may join across them freely, because both live on the same server:

| Database | Holds |
|---|---|
| `AppMasterDB` | the operational data: the well master, daily task records, the project schedule, the SAP drilling sequence, the employee, crew and equipment registers, and the activity mapping |
| `AppMasterEngDB` | the engineering data: the engineering task plan and the engineering well-priority lists per cluster |

**The PDO well id is the ONLY key shared by the two databases.** It is a number held as text,
five digits for every well in the well master (measured range `10204`..`38121`). Where a table
stores it as a number (`bigint`), cast that side to text to compare — never cast the text side
to a number, because a stray non-numeric value then fails the whole query instead of simply not
matching:

```sql
-- correct: the bigint side is converted
ON w.pdo_well_id = CAST(p.[Well ID] AS nvarchar(20))
```

Where the well id is not a column but the suffix of a code (`<activity>-<well id>`), extract it as
the text after the FIRST `-`:

```sql
SUBSTRING(code, CHARINDEX('-', code) + 1, 50)      -- 'ENG1030-35379' -> '35379'
```

A well appearing in one database and not the other is a finding ONLY in the direction stated by
the rule. In particular the SAP drilling sequence covers **every PDO well** (measured: 5,739 in the
latest week), not just Al Tasnim's (919), so a SAP well missing from the well master is normal.

## 3. The well master — `AppMasterDB.dbo.WellMonitoringReportOptimizedVersions`

A **versioned** table: one row per well per report version `WeekNumber` (measured: 41 versions,
918 wells in the latest, no well twice in one version). The CURRENT state of every well is its row
in the LATEST version — restrict to it for every check about a well as it is now:

```sql
WHERE v.WeekNumber = (SELECT MAX(WeekNumber) FROM AppMasterDB.dbo.WellMonitoringReportOptimizedVersions)
```

Never count across versions as though they were separate wells — a well appears in every version.

Several column names contain dots, hyphens or slashes and MUST be written in brackets, exactly as
below and in the SCHEMA block. Use exactly these columns; never a synonym:

| Business term | Column | Kind |
|---|---|---|
| PDO well id | `pdo_well_id` | key |
| Report version | `WeekNumber` | version (date) |
| Expected / master rig-on date | `[latest_exp.rig_on_location_sap_data]` | expected (from SAP) |
| Expected rig-on date, previous week | `[last_week_exp.rig_on_location_sap_data]` | expected |
| Expected / predicted rig-off date | `[exp.rig_off_location_sap_data]` | expected (from SAP) |
| Actual rig-on date | `actual_rig_on_date` | actual |
| Actual rig-off date | `actual_rig_off_date` | actual |
| Pegging sheet date | `actual_pegged_date` | actual |
| FLAF date | `flaf_issue_date` | actual |
| Hook-up completion = **well completion date** | `[actual_eng._completion_date]` | actual |
| Commissioning start | `[actual_comm._start_date]` | actual |
| Commissioning finish | `[actual_comm._finish_date_with_in_2_days_from_actual_engg._completion_date]` | actual |
| Engineering finish | `engineering_actual_finish_date` | actual |
| Cluster | `Cluster` | `Nimr` / `Marmul` / `Amal` |
| Project | `project_id` | GUID — see §8 |
| Source-system error flag | `data_error` | text, set when the source flags the row |

⚠ **The expected rig-on date is only kept for wells the rig has NOT reached yet.** It is copied
from the SAP drilling sequence, and SAP drops a well once it is drilled: measured in the latest
version, 401 wells have no actual rig-on date. A rigged-on or completed well with no expected date
is therefore NORMAL. "Missing expected rig-on date" is only a defect for a well whose
`actual_rig_on_date` is still NULL. Earlier versions keep the last expected date a drilled well had.

⚠ `engineering_actual_start_date` is not populated — no check may rely on it.

### Progress columns are FRACTIONS (0 to 1)

`progress`, `flowline_construction_progress`, `over_all_progress_percentages`,
`cum_progress_for_this_week`, `last_week_cum_progress`, `ohl_progress` and the other progress
columns hold **0.0–1.0** (measured: maximum exactly 1.0000). "Complete" is `>= 1`, never `>= 100`,
despite names like `over_all_progress_percentages`.

### Status columns

`location_preparation_status_in_progress_completed`, `[flow_line_const._status_in_progress_completed]`
and `[flow_line_commi._status_in_progress_completed]` hold `'Not Started'`, `'In Progress'`,
`'Completed'`, or NULL.

## 4. Change between versions, and the raw well-monitoring imports

The earlier versions exist for one purpose: to see what a well looked like BEFORE. A date that was
recorded in one version and is blank in a later one, or progress that falls between two versions,
can only be seen here. Compare a version with the immediately preceding one for the SAME well
(`LAG()` over `WeekNumber`, partitioned by `pdo_well_id`).

`AppMasterDB.dbo.WMR`, `dbo.WMR_STG_*`, `dbo.WMR_SQL_Bulk_Update` and `dbo.WMR_Conversion` are the
RAW imports and staging steps the versions are built from. Their columns are mostly TEXT, even for
dates and progress. They are never the authority on a well — the latest version is — and a check
on them is an import-quality check: a value that cannot be read as the date or number it should
be, or a raw row that never reached the versions.

## 5. Milestone deadlines

Every deadline is derived from the expected rig-on date in §3. For a milestone that **has** an
actual date column, "missed" = the actual date is later than the deadline, or the actual date is
still NULL once the deadline has passed.

| Milestone | Owner | Actual date | Deadline |
|---|---|---|---|
| Pegging sheet issued | PDO | `actual_pegged_date` | expected rig-on − 60 days |
| FLAF issued | PDO | `flaf_issue_date` | expected rig-on − 90 days |
| Location Construction complete | Al Tasnim | *not approved — see §13* | expected rig-on − 1 day |
| Flowline Construction complete | Al Tasnim | *not approved — see §13* | expected rig-on − 1 day |
| Hook-up complete | Al Tasnim | `[actual_eng._completion_date]` | actual rig-off + 2 days |

**Construction missed its deadline** — judged from the rig, because no construction completion
date is approved as the source:

```
missed  ⇔  CAST(GETDATE() AS date) > DATEADD(day, -1, [latest_exp.rig_on_location_sap_data])
           AND actual_rig_on_date IS NULL
```

**Hook-up deadline.** Before the rig is off the planned deadline is expected rig-off + 2 days.
Once `actual_rig_off_date` is populated the actual deadline is `actual_rig_off_date + 2 days`, and
**the actual date takes precedence**.

Because the expected rig-on date disappears once a well is drilled (§3), a deadline check over
the well master can only judge wells still waiting for the rig. Say so; do not substitute another
date to widen the scope.

## 6. Schedule variance — being EARLY is not an anomaly

An expected date and an actual date differing is **normal** and never, on its own, a data error.

| Comparison | Meaning |
|---|---|
| actual **earlier than** expected | ahead of schedule — a GOOD outcome |
| actual **equal to** expected | on schedule |
| actual **later than** expected | behind schedule — delayed |

```
schedule_variance_days = DATEDIFF(day, [latest_exp.rig_on_location_sap_data], actual_rig_on_date)
    negative → ahead   zero → on schedule   positive → behind
```

⚠ NEVER describe an early actual date as a delay or an anomaly, and never take the absolute value
and call it "days of delay".

## 7. Lifecycle order

```
PDO issues pegging sheet  → Al Tasnim: Location Construction ┐
PDO issues FLAF           → Al Tasnim: Flowline Construction ┘
  → both complete before the expected rig-on date
  → PDO rig-on (actual_rig_on_date) → PDO drilling → rig-off (actual_rig_off_date)
  → handover to Al Tasnim → hook-up ([actual_eng._completion_date]) = WELL COMPLETED
  → commissioning start → commissioning finish (within 2 days of hook-up)
```

A well is **completed** when `[actual_eng._completion_date] IS NOT NULL`. Drilling (rig-on to
rig-off) is PDO's activity, not Al Tasnim's.

## 8. Projects — `AppMasterDB.dbo.ProjectIDs`

One row per project (31). `ID` is the project GUID, stored as **lower-case text**; other tables
hold the same GUID as `uniqueidentifier` or in upper case. Compare them as text, trimmed and
case-insensitively — `LTRIM(RTRIM(p.ID))` — because one `ID` carries a trailing space (measured).
`Cluster` gives the cluster, `Type` the project kind: `Location`, `Flowline`, `SNLP`, `Yard`,
`Re-Open`, conversions and others.

A **Yard** project is not a well: its tasks carry a placeholder well id such as `00001`. Never
report a Yard task as "not attached to a real well".

## 9. Daily task records — `AppMasterDB.dbo.task_daily`

**One row per task per working day** (measured: 112,076 rows over 35,801 distinct `task_code`,
about 3 rows per task). `ActionOn` is the day the row records. The CURRENT state of a task is its
row with the latest `ActionOn` — collapse to that row before judging a task:

```sql
ROW_NUMBER() OVER (PARTITION BY task_code ORDER BY ActionOn DESC, id DESC) = 1
```

| Business term | Column |
|---|---|
| Task | `task_code` = `<activity id>-<well id>` e.g. `FLME1150-37664` |
| Well | `well_id` — must equal the suffix of `task_code` (§2) |
| Project | `project_id` (`uniqueidentifier`) → §8 |
| Progress | `progress` — a **FRACTION 0–1** (measured average 0.84; complete = `>= 1`) |
| Completed flag | `completed` (bit) |
| Actual start / finish | `actual_start` / `actual_end` |
| Planned start / finish | `target_start` / `target_end` |
| Committed start / finish | `committed_start` / `committed_end` |
| Remaining duration (days) | `remaining_duration` |
| Hours, quantity | `data_hours`, `data_qty`, `daily_actual_hours`, `daily_actual_quantity` |
| Crew | `crew_code` → `crews.Code` (§11) |
| Unit of measure | `task_uom` |

⚠ `progress` is a fraction even though values above 1 exist (measured maximum 66.7 on one row):
those are the defect, not evidence of a 0–100 scale. Never compare it against 100.

⚠ `crew_code` is blank on most rows (measured 70,742 of 112,076). Blank means "not recorded",
which is a separate finding from "recorded but unknown".

## 10. Task code → activity → WBS

`task_code` encodes the activity. The text before the FIRST `-` is the **activity id**:

```sql
LEFT(task_code, NULLIF(CHARINDEX('-', task_code), 0) - 1)     -- 'FLME1150-37664' -> 'FLME1150'
```

**Is the activity id a real, known activity?** Use `AppMasterDB.dbo.ActivityCodesNorms.Activity_ID`
directly (nvarchar, no cast). Measured: 634 of 945 distinct task activity ids match (67%). The
remainder is a genuine gap — do not report near-100% unmatched; that means the wrong table was
joined.

**What is its WBS?** Two hops:

```
activity id → AppMasterDB.dbo.MappingMaster.Activity_ID   (CAST to nvarchar — it is a `text` column)
            → .New_Activity_Code
            → AppMasterDB.dbo.ActivityMasterCSV.ACTIVITY_CODE
               → .Activity_Group_Description = WBS
               → .CREW_GROUP_CODE            = crew group
```

- ⚠ **USE `New_Activity_Code` HERE, NEVER `Old_Activity_Code`.** In this database the activity
  master is keyed on the NEW scheme (`F-E-OHE-INT-03`): measured, 174 New codes match it and only
  71 Old ones. This is the OPPOSITE of the reporting warehouse — do not carry that habit over.
- ⚠ `MappingMaster.Activity_ID` is a legacy `text` column; comparing it directly fails with "the
  data types text and nvarchar are incompatible". Always `CAST(m.Activity_ID AS nvarchar(50))`.
- ⚠ `MappingMaster` holds the spreadsheet error string `'#N/A'` in place of missing values. Treat
  `'#N/A'` exactly like NULL when matching, and as a defect in its own right.

## 11. People, crews and equipment

| Fact | Where | Join |
|---|---|---|
| Employee | `dbo.Employee` (`id` int, PK) | |
| Employee type | `dbo.EmployeeType` (`id` nvarchar) | `CAST(e.EmployeeType AS nvarchar(50)) = t.id` |
| Nationality | `dbo.Employee.[Nationals/Expats]` | `'National'`, `'Expat'`, or NULL = not recorded |
| Employer group | `dbo.Employee.[ATNM/SC]` | `'ATNM'` (Al Tasnim), `'SC'` (subcontractor), NULL |
| Crew | `dbo.crews` (`ID` nvarchar, `Code`) | |
| Crew membership | `dbo.CrewEmployee` (`Crew` int, `Employee` int) | `CAST(ce.Crew AS nvarchar(50)) = c.ID` |
| Equipment | `dbo.Equipment` (`EquipmentType` int) | `CAST(e.EquipmentType AS nvarchar(50)) = t.ID` on `dbo.EquipmentType` |

- Nationality NULL is a real third group — never count it as Expat.
- `Employee.Status` is written both `'Active'` and `'ACTIVE'`; compare case-insensitively.
- Column names containing `/` must be bracketed: `[Nationals/Expats]`, `[ATNM/SC]`.

## 12. Snapshot tables — judge the LATEST snapshot only

These tables append a full copy of their source on every load: the SAP drilling sequence, the
project schedule (its activity, WBS and milestone rows) and the engineering task plan. Each copy is a snapshot, so the same record appears once per load.
A check over "all rows" counts every record — every task, activity or document — many times.

| Table | Snapshot column | Measured |
|---|---|---|
| `AppMasterDB.dbo.SAP_DRILLING_SEQUENCE_Latest` | `week_number` (date) | 26 weekly loads, ~6,500 rows each |
| `AppMasterDB.dbo.ActivityTaskPlanProject` | `Time_Stamp` (text `YYYY-MM-DD HH:MM:SS -0700`) | ~21 daily loads, 18,219 rows each |
| `AppMasterEngDB.dbo.EngineeringTaskPlan` | `Time_Stamp` (datetime2) | 135 daily loads, ~5,500 rows each |

Restrict to the latest snapshot (`= (SELECT MAX(<snapshot column>) FROM <same table>)`) unless the
rule is explicitly about change between snapshots. The text `Time_Stamp` sorts correctly as text
because every value has the same format.

### SAP drilling sequence

`Well_ID` is the PDO well id (text). `Earl_start_date` / `EarliestEndDate` are SAP's planned
start and end of the rig operation on that well. `Well_ID` also holds non-well values (measured:
`'GAS'`, and `'4645'` on 967 rows of one week) — exclude values that are not a well id before
matching.

### Project schedule — `ActivityTaskPlanProject`

`type`: `A` activity, `W` WBS group, `M` milestone. Numbers are stored as TEXT — use `TRY_CAST`.
`progress` is a fraction 0–1. `actual_start` / `actual_end` hold **`1900-01-01` as a placeholder
for "not recorded"** — treat it as NULL, never as a real date. Its `code` values use a DIFFERENT
scheme from `task_daily.task_code` (measured: zero overlap) — never join the two on code.
`Well_ID` is NULL on every row.

### Engineering task plan — `AppMasterEngDB.dbo.EngineeringTaskPlan`

`code` = `<engineering step>-<well id>`, e.g. `ENG1120-16220`. Steps run `ENG1000` (review of FLAF)
to `ENG1130` (AFC for E&I design); `ENG1120` is "Review & Issuance of AFC for Mechanical design".
`progress` is text `'0'`/`'1'`. `actual_start` is datetime2 but `actual_end`, `committed_start` and
`committed_end` are TEXT in ISO form (`2026-07-28T00:00:00.000+00:00`) — `TRY_CAST(... AS datetime2)`
before comparing. `Cluster_Source` gives the cluster.

## 13. Engineering well priority lists — `AppMasterEngDB.dbo.EngWellPriority_Nimr`, `_Marmul`, `_Amal`

One list per cluster of wells engineering must deliver, each with `[Well ID]`, `[Well Name]`,
`[AFC Target Date]` (the date the AFC design must be issued) and up to three revised targets.
`[Well ID]` is `bigint` in the Nimr and Amal lists and TEXT in Marmul; Marmul's `[AFC Target Date]`
is also TEXT and contains `'TBC'` (to be confirmed) — `TRY_CAST` it and treat `'TBC'` as "no
target yet", not as a defect. Every column name contains spaces and must be bracketed.

## 14. Never interchange these pairs

| | vs | |
|---|---|---|
| Expected / master date | ⟷ | Actual date |
| `[latest_exp.rig_on_location_sap_data]` | ⟷ | `actual_rig_on_date` |
| `[exp.rig_off_location_sap_data]` | ⟷ | `actual_rig_off_date` |
| PDO responsibility | ⟷ | Al Tasnim responsibility |
| Location Construction | ⟷ | Flowline Construction |
| Hook-up deadline | ⟷ | Well completion |
| A fraction (0–1) | ⟷ | A percentage (0–100) |
| The latest snapshot or version | ⟷ | All snapshots or versions |

## 15. Not yet defined

State plainly that these are undefined — never invent them:

* **Construction completion dates.** The well master holds `actual_finish_date` and
  `[const._complete_date_including_f_l_final_hydro_test_1_day_before_rig_on_date]`, which look like
  Location and Flowline construction completion. Neither is approved as the source, so on-time /
  missed is judged by the rig rule in §5.
* Whether "overall progress complete" in the well master is meant to include commissioning.
* Which equipment status codes mean the same thing (`'IOP'`, `'In Operation'`,
  `'Eqp In Operation'` all appear).

## 16. Strict instructions

* These rules are authoritative for every check about PDO, Al Tasnim, wells, construction,
  pegging, FLAF, rig-on, rig-off, hook-up, completion, tasks, activities, crews and engineering.
* If the database holds a value that conflicts with a business rule, **report the database value**
  and explain the business-rule interpretation separately. Never silently change a result.
