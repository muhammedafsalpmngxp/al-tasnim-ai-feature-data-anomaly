# DATA ANOMALIES — AppMasterDB + AppMasterEngDB

Every data-quality problem this engine looks for in the two operational databases is described
here, in plain business language. Both databases are checked together, so a rule may compare
what one says against what the other says.

To add a check, copy an existing one and describe the problem in your own words. To switch one
off, change its status to `disabled`. You never need to write SQL, and you never need to know a
table or column name — the AI reads the live databases and the business rules and works it out.

Each entry has four parts:

- **Wrong** — the problem, in one or two sentences.
- **Matters** — the consequence, so the severity can be justified.
- **Detect** — the logic in words. Which business facts to compare, and how.
- **Never flag** — the cases that are legitimate, or already reported elsewhere.

Status: `active` runs. `draft` is listed but never run — use it when the business has not yet
agreed the definition, so the gap stays visible instead of being quietly dropped. `disabled` is
switched off deliberately.

Figures in a "Measured" line were read from the live data on 2026-10-08, to show the scale of the
problem when the rule was written. They are not thresholds.

---

# GROUP W — The well master: milestones and lifecycle

Every check here judges a well on its LATEST version in the well master, unless it says it is
about change between versions (business rules §3, §4).

## RULE DQ-W01 - A well still waiting for the rig has no expected rig-on date

- category: Milestone dates
- severity: critical
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, well master, deadline

Wrong: A well the rig has not yet come on to has no expected rig-on date.
Matters: The expected rig-on date is the master date every construction, pegging and FLAF deadline
  is worked out from. A waiting well without it cannot be judged for lateness at all — it silently
  drops out of every deadline check instead of appearing as a problem.
Detect: Examine every well with no actual rig-on date. Flag the well when its expected rig-on date
  is also absent.
Never flag: A well the rig has already come on to, or a completed well. The expected date is
  removed once a well is drilled, so its absence there is normal (business rules §3).
Measured: 91 of 401 waiting wells.
---

## RULE DQ-W02 - Rig is on the well but no expected rig-off date

- category: Milestone dates
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, well master, deadline

Wrong: The rig has come on to a well and not yet come off, but the well has no expected rig-off
  date.
Matters: The hook-up deadline is planned from the expected rig-off date until the rig actually
  leaves. Without it the hook-up crew cannot be scheduled against anything.
Detect: Examine wells with an actual rig-on date and no actual rig-off date. Flag those with no
  expected rig-off date.
Never flag: Wells the rig has already left, and wells it has not reached yet.
---

## RULE DQ-W03 - Rig left the well before it arrived

- category: Milestone dates
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, well master, lifecycle

Wrong: A well's actual rig-off date is earlier than its actual rig-on date.
Matters: Drilling duration comes out negative, and every lifecycle report that orders the
  milestones places this well wrongly. One of the two dates is a typo.
Detect: Examine wells holding both actual dates. Flag those where rig-off is earlier than rig-on.
  No threshold: a rig cannot leave before it arrives.
Never flag: Wells missing either date — those are separate checks.
---

## RULE DQ-W04 - Rig left the well but was never recorded as arriving

- category: Milestone dates
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, well master, lifecycle

Wrong: A well has an actual rig-off date and no actual rig-on date.
Matters: The rig cannot have left a well it never came on to. Drilling duration and the schedule
  variance on rig-on cannot be worked out for this well.
Detect: Flag wells with an actual rig-off date whose actual rig-on date is absent.
Never flag: Wells with neither date.
---

## RULE DQ-W05 - Hook-up completed before the rig left the well

- category: Lifecycle order
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, hook-up, completion, well master, lifecycle

Wrong: A well's hook-up completion date is earlier than its actual rig-off date.
Matters: Hook-up is Al Tasnim's work after drilling ends and the well is handed over. Completing
  it before the rig leaves is impossible, so either the completion date or the rig-off date is
  wrong — and the well's completion is reported in the wrong period.
Detect: Examine wells holding both dates. Flag those where hook-up completion is earlier than
  rig-off.
Never flag: Wells missing either date.
---

## RULE DQ-W06 - Well completed while drilling is not recorded as finished

- category: Lifecycle order
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, hook-up, completion, well master, lifecycle

Wrong: A well has a hook-up completion date but no actual rig-off date.
Matters: Completion comes after drilling. A completed well with no rig-off date breaks the hook-up
  deadline (rig-off + 2 days), which then cannot be checked at all.
Detect: Flag wells with a hook-up completion date and no actual rig-off date.
Never flag: Wells that are not completed.
---

## RULE DQ-W07 - A milestone that already happened is dated in the future

- category: Milestone dates
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, well master

Wrong: One of a well's ACTUAL milestone dates — rig-on, rig-off, pegging, FLAF, hook-up
  completion, commissioning start or commissioning finish — is later than today.
Matters: An actual date records something that has happened. In the future it is almost always a
  typo in the year, and it makes the well look further along than it is.
Detect: Compare each actual milestone date against today's date on the database's own clock. Flag
  the well once, naming every future actual date it holds.
Never flag: Expected dates — they are meant to be in the future.
---

## RULE DQ-W08 - Expected rig-off is planned before expected rig-on

- category: Milestone dates
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, well master, sap

Wrong: A well's expected rig-off date is earlier than its expected rig-on date.
Matters: The plan has the rig leaving before it arrives, so every deadline derived from either
  date is nonsense.
Detect: Examine wells holding both expected dates. Flag those where expected rig-off is earlier
  than expected rig-on.
Never flag: Wells missing either expected date.
---

## RULE DQ-W09 - Construction deadline passed and the rig has still not arrived

- category: Delivery deadlines
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: deadline, construction, rig, milestone, well master

Wrong: Construction must be complete one day before the expected rig-on date. That day has passed
  and the rig has still not come on to the well.
Matters: This is the business's own definition of missed construction (business rules §5). It is
  the list of wells at risk of a rig delay, and a Location Construction delay can carry a penalty.
Detect: Examine wells with an expected rig-on date and no actual rig-on date. Flag those where
  today is later than the expected rig-on date minus one day. The severity is the number of days
  past the deadline.
Never flag: Wells the rig has come on to — they are not missed under this rule, even if the rig
  came late. Wells with no expected rig-on date — reported by DQ-W01 instead.
---

## RULE DQ-W10 - Well completed while flowline construction is not marked complete

- category: Status consistency
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: completion, construction, status, well master

Wrong: A well has a hook-up completion date, but its flowline construction status is not
  `Completed`.
Matters: Hook-up connects the well to the flowline. A completed well whose flowline is still "In
  Progress" or "Not Started" is either not really complete, or its status was never updated — both
  misreport progress.
Detect: Examine wells with a hook-up completion date. Flag those whose flowline construction
  status is anything other than `Completed`.
Never flag: Wells with no hook-up completion date.
Measured: 5 wells.
---

## RULE DQ-W11 - Overall progress is complete but commissioning is not

- category: Status consistency
- severity: low
- entity: well
- method: rule
- sql_mode: authored
- status: draft
- tags: progress, status, well master

Wrong: A well's overall progress is complete (1 on the 0–1 scale), but its commissioning status is
  not `Completed`.
Matters: If overall progress is meant to include commissioning, these wells overstate progress.
Detect: Flag wells whose overall progress is at least 1 and whose commissioning status is not
  `Completed`.
Never flag: Wells below full overall progress.
**Draft — the business has not said whether overall progress includes commissioning** (business
rules §15). Measured: 217 wells, which is too many to be noise and too many to report until that
is agreed.
---

## RULE DQ-W12 - Well is flagged as an error by the source system

- category: Source-system flags
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: well master, status

Wrong: The well's record carries the source system's own data-error flag.
Matters: The application that builds the well master has already decided this record is wrong.
  Until it is corrected, every figure reported for the well is suspect.
Detect: Flag wells whose data-error marker is set. Show the well's location and cluster so the
  owner can find it.
Never flag: Wells where the marker is empty.
Measured: 26 wells.
---

## RULE DQ-W13 - Well has no classification

- category: Reference integrity
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: well master, classification

Wrong: A well in the well master has no entry in the well classification list (conventional or
  non-standard, gas or oil).
Matters: Non-standard wells are planned and reported differently. An unclassified well silently
  falls out of every breakdown by well type.
Detect: Examine every well in the well master. Flag those with no classification entry, matching
  on the PDO well id.
Never flag: Classification entries for wells that are not in the well master — a different
  question.
Measured: 110 of 918 wells.
---

## RULE DQ-W14 - Well is not linked to a project, or to a project that does not exist

- category: Reference integrity
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: well master, project

Wrong: A well in the well master has no project, or names a project that is not in the project
  register.
Matters: Cost, revenue and delivery are tracked per project. A well with no valid project is
  invisible to project reporting.
Detect: Examine every well. Flag it when its project is absent, or when no project in the register
  matches it — comparing the project ids as text, trimmed and case-insensitively (business rules
  §8). Say which of the two cases applies.
Never flag: Nothing else — every well belongs to a project.
---

## RULE DQ-W15 - A milestone date was removed after being recorded

- category: History integrity
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: history, milestone, well master

Wrong: In the weekly history of the well master, an ACTUAL milestone date (rig-on, rig-off,
  pegging, FLAF, hook-up completion) was present in one version and is blank in the next version
  of the same well.
Matters: An event that has happened cannot un-happen. A disappearing date usually means an
  overwrite from a stale source, and it makes a finished well look unfinished again.
Detect: For each well, compare each version with the immediately preceding version. Flag each
  occasion where an actual milestone date goes from present to blank, naming the date and the
  version. Use the history table, never the current well master.
Never flag: Expected dates, which legitimately change and disappear. A date changing from one
  value to another — that is a correction, a different question.
---

## RULE DQ-W16 - Well progress went backwards between weekly versions

- category: History integrity
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: history, progress, well master

Wrong: In the weekly history, a well's overall progress is lower than it was in the immediately
  preceding version of the same well.
Matters: Physical progress does not go backwards. A drop is a data overwrite or a recalculation
  that nobody explained, and it moves every progress total reported for that week.
Detect: For each well, compare overall progress with the preceding version. Flag decreases, with
  the size of the drop as the severity. Progress is a 0–1 fraction (business rules §3).
Never flag: Versions where either value is blank.
---

# GROUP B — Pegging sheet and FLAF deadlines

## RULE DQ-B01 - Pegging sheet missed its deadline

- category: Delivery deadlines
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: pegging, deadline, milestone, well master

Wrong: The pegging sheet must be issued 60 days before the expected rig-on date. It was issued
  after that, or it has not been issued and the deadline has passed.
Matters: Location Construction cannot start without the pegging sheet. A late pegging sheet is
  PDO's delay, and it is the first link in a chain that ends in a late rig.
Detect: Examine wells with an expected rig-on date. The deadline is expected rig-on minus 60 days.
  Flag the well when the pegging date is later than the deadline, or when there is no pegging date
  and today is past the deadline. Severity is the days late.
Never flag: Wells with no expected rig-on date (DQ-W01). A pegging sheet issued early.
---

## RULE DQ-B02 - FLAF missed its deadline

- category: Delivery deadlines
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: flaf, deadline, milestone, well master

Wrong: The FLAF must be issued 90 days before the expected rig-on date. It was issued after that,
  or it has not been issued and the deadline has passed.
Matters: Flowline Construction cannot start without the FLAF. A late FLAF is PDO's delay, and per
  the business rules a flowline delay that it causes is not Al Tasnim's.
Detect: Examine wells with an expected rig-on date. The deadline is expected rig-on minus 90 days.
  Flag the well when the FLAF date is later than the deadline, or when there is no FLAF and today
  is past the deadline. Severity is the days late.
Never flag: Wells with no expected rig-on date (DQ-W01). A FLAF issued early.
---

## RULE DQ-B03 - Pegging sheet dated after the rig came on

- category: Lifecycle order
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: pegging, milestone, rig, well master, lifecycle

Wrong: A well's pegging date is later than its actual rig-on date.
Matters: The location must be pegged and built before the rig can come on. A pegging date after
  rig-on is a recording error, and it makes the pegging deadline report a miss that may never
  have happened.
Detect: Examine wells with both dates. Flag those where the pegging date is later than the actual
  rig-on date.
Never flag: Wells missing either date.
---

# GROUP T — Daily task records

Every check here judges a task on its CURRENT record — the latest day recorded for that task —
unless it says otherwise (business rules §9).

## RULE DQ-T01 - Task record's well does not match the well in its task code

- category: Reference integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, well

Wrong: A daily task record names one well in its well field and a different well at the end of
  its task code.
Matters: The task is counted against whichever well a report happens to read. Progress, hours and
  cost land on the wrong well.
Detect: Examine every daily record that has a well. Compare it with the text after the first dash
  of the task code. Flag records where they differ.
Never flag: Records with no well recorded (DQ-T02).
Measured: 192 of 112,076 records.
---

## RULE DQ-T02 - Task is not attached to a real well

- category: Reference integrity
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, well, project

Wrong: A task's well is missing, blank, all zeros, or not a well id at all.
Matters: The task's progress cannot be attributed to any well, so it disappears from every
  per-well report.
Detect: Judge each task on its current record. Flag tasks whose well is blank, missing, all zeros
  or not purely numeric.
Never flag: Tasks belonging to a Yard project — a yard is not a well and uses a placeholder well
  id by design (business rules §8).
---

## RULE DQ-T03 - Task names a well the well master does not know

- category: Reference integrity
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, well, well master

Wrong: A task is attached to a well id that has no entry in the well master.
Matters: The work is real but the well it was done on is invisible to the well-level reports, so
  the effort and progress are never reported against anything.
Detect: Take the distinct wells named by tasks. Flag each well with no matching well-master entry,
  and say how many tasks name it. Severity is the number of tasks.
Never flag: Yard projects and tasks with no well (DQ-T02).
Measured: 188 of 800 distinct wells.
---

## RULE DQ-T04 - A task has a daily record dated in the future

- category: Date integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task

Wrong: One or more of a task's daily records is dated later than today.
Matters: A daily record reports work done on a day. One dated in the future is a typo, and the
  task's current state is then taken from it — so every "latest" figure for that task is wrong.
Detect: Judge each task (one task = one task code), not each daily record: the daily table holds
  about three records per task. Scope is every task. Flag a task when at least one of its daily
  records is dated later than today on the database's own clock. Severity is the furthest number
  of days into the future; the explanation names how many of its records are future-dated.
Never flag: Planned, committed or target dates — they are meant to be in the future.
Measured: 1,006 daily records, some in 2031.
---

## RULE DQ-T05 - Task shows full progress but is not marked complete

- category: Status consistency
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, progress, status

Wrong: On its current record, a task's progress is complete (at least 1 on the 0–1 scale) but its
  completed flag is not set.
Matters: Reports that count completed tasks and reports that sum progress disagree about the same
  task.
Detect: Judge each task on its current record. Flag those with progress of at least 1 and the
  completed flag off.
Never flag: Earlier records of the task — only its current state matters here.
Measured: 2,929 of 35,801 tasks.
---

## RULE DQ-T06 - Task marked complete while progress is below full

- category: Status consistency
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, progress, status

Wrong: On its current record, a task is flagged completed but its progress is below 1.
Matters: The task is counted as done while its progress says otherwise, so earned value and
  completion counts disagree.
Detect: Judge each task on its current record. Flag those flagged completed with progress below 1.
  Severity is how far short of full the progress is.
Never flag: Tasks with no progress recorded.
Measured: 234 tasks.
---

## RULE DQ-T07 - Task marked complete with no actual finish date

- category: Status consistency
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, status

Wrong: On its current record, a task is flagged completed but has no actual finish date.
Matters: The task cannot be placed in the period it finished in, so period completion counts and
  durations leave it out.
Detect: Judge each task on its current record. Flag completed tasks with no actual finish date.
Never flag: Tasks that are not complete.
Measured: 143 tasks.
---

## RULE DQ-T08 - A task has progress above full or below zero

- category: Value range
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, progress

Wrong: One or more of a task's daily records holds progress greater than 1 or less than 0. Task
  progress is a 0–1 fraction (business rules §9).
Matters: Out-of-range progress spreads into every weighted rollup that uses it. A value such as
  66.7 is a percentage typed into a fraction field: it outweighs thousands of correct tasks.
Detect: Judge each task (one task = one task code), not each daily record. Scope is every task with
  at least one progress value. Flag a task when any of its records has progress above 1 or below 0.
  Compare against 1 — never 100; the scale is stated in the business rules and must not be
  inferred from the values. Severity is the worst distance outside 0..1.
Never flag: Records with no progress.
Measured: 66.7 maximum, −0.05 minimum.
---

## RULE DQ-T09 - A task has negative time remaining, hours or quantity

- category: Value range
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task

Wrong: One or more of a task's daily records holds a negative remaining duration, negative hours or
  a negative quantity.
Matters: None of these can be negative. They reduce totals that should only grow, and a negative
  remaining duration makes overdue work look ahead of plan.
Detect: Judge each task (one task = one task code), not each daily record. Scope is every task.
  Flag a task when any of its records has a remaining duration, recorded hours, daily actual hours,
  recorded quantity or daily actual quantity below zero. Name every negative field in the
  explanation. Severity is the most negative value.
Never flag: Zero values.
---

## RULE DQ-T10 - Planned quantity is wildly out of proportion

- category: Value range
- severity: medium
- entity: task
- method: statistical
- sql_mode: authored
- status: active
- tags: task, quantity
- tolerance: auto

Wrong: A task's planned quantity is so far above the planned quantities of other tasks with the
  same unit of measure that it cannot be real.
Matters: A single typo such as 989,089,888,899,998 (measured) dominates every total of planned
  quantity it is part of.
Detect: Judge each task on its current record. Compare its planned quantity with other tasks in the
  same unit, using a threshold calibrated from the data itself — for example far beyond the upper
  quartile plus several interquartile ranges. Severity is how many times the threshold is
  exceeded.
Never flag: Units held by too few tasks to calibrate against.
---

## RULE DQ-T11 - Task's activity is not a known activity

- category: Reference integrity
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, activity

Wrong: The activity id at the start of a task code is not in the activity norms register.
Matters: Without a known activity there is no norm to measure productivity against and no WBS to
  roll the task up into.
Detect: Take the distinct activity ids derived from task codes (business rules §10). Flag each one
  not found in the activity norms register, and say how many tasks use it.
Never flag: Nothing else. Use the norms register directly — never the WBS mapping chain, which
  answers a different question and under-counts.
Measured: 311 of 945 activity ids.
---

## RULE DQ-T12 - Task names a crew that is not in the crew register

- category: Reference integrity
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, crew

Wrong: A daily record names a crew code that does not exist in the crew register.
Matters: The work cannot be traced to the people and equipment who did it, so productivity and
  cost per crew are understated.
Detect: Take the distinct crew codes recorded on daily records. Flag each one with no matching
  crew, and say how many records use it.
Never flag: Records with no crew recorded — "not recorded" is a different finding from "recorded
  but unknown" (business rules §9).
Measured: 716 of 2,189 crew codes.
---

## RULE DQ-T13 - The same unit of measure is written in different ways

- category: Coding consistency
- severity: low
- entity: row
- method: rule
- sql_mode: authored
- status: active
- tags: task, quantity

Wrong: Task units of measure that mean the same thing are spelled differently — `no` and `Nos`,
  `Joint` and `JOINT`, `Ls` and `LS` — or contain a broken character in place of a symbol.
Matters: Any total grouped by unit splits one unit into several rows, and quantities in the same
  unit are never added together.
Detect: Group the distinct units by a normalised form (case-insensitive, ignoring spaces and full
  stops). Flag every spelling that shares its normalised form with another spelling, and every unit
  containing the replacement character. Report one row per spelling with how many records use it.
Never flag: Units that differ after normalising.
---

## RULE DQ-T14 - Task belongs to a project that is not in the project register

- category: Reference integrity
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, project

Wrong: A daily record names a project that the project register does not contain.
Matters: The work cannot be assigned to a cluster or a project type, so it falls out of every
  project-level report.
Detect: Take the distinct projects on daily records. Flag each with no match in the register,
  comparing as text, trimmed and case-insensitively (business rules §8). Say how many records use
  it.
Never flag: Records with no project.
Measured: 5 of 31 projects.
---

## RULE DQ-T15 - Progress recorded but the task never started

- category: Lifecycle order
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, progress

Wrong: On its current record, a task has progress above zero but no actual start date.
Matters: Work cannot progress before it starts. Duration and productivity cannot be worked out.
Detect: Judge each task on its current record. Flag those with progress above 0 and no actual
  start date.
Never flag: Tasks with no progress.
---

## RULE DQ-T16 - Task progress went backwards

- category: History integrity
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, progress, history

Wrong: A task's progress on one day is lower than on the previous day recorded for the same task.
Matters: Physical progress does not go backwards. A drop is an overwrite or a data-entry error, and
  it understates earned progress for that period.
Detect: For each task, compare each day's progress with the previous recorded day. Flag decreases,
  with the size of the drop as severity.
Never flag: Days where either value is blank.
---

# GROUP R — Reference and master data

## RULE DQ-R01 - Employee's type is not in the employee type register

- category: Reference integrity
- severity: medium
- entity: employee
- method: rule
- sql_mode: authored
- status: active
- tags: employee

Wrong: An employee names an employee type that the employee type register does not contain.
Matters: Rates, trades and crew composition are taken from the type. An employee with an unknown
  type is costed at nothing and missing from every trade breakdown.
Detect: Flag employees whose type has no match in the register (business rules §11 — the two
  sides have different data types).
Never flag: Employees with no type recorded.
Measured: 2,726 of 21,205 employees.
---

## RULE DQ-R02 - Employee nationality is not recorded

- category: Completeness
- severity: low
- entity: employee
- method: rule
- sql_mode: authored
- status: active
- tags: employee

Wrong: An active employee has no nationality recorded.
Matters: Omanisation is reported to the client. An employee with no nationality is in neither the
  National nor the Expat count, so the ratio is computed over the wrong total.
Detect: Examine active employees (compare status case-insensitively). Flag those with no
  nationality.
Never flag: Inactive employees.
Measured: about 1,500 employees.
---

## RULE DQ-R03 - Crew membership names a crew that does not exist

- category: Reference integrity
- severity: medium
- entity: row
- method: rule
- sql_mode: authored
- status: active
- tags: crew, employee

Wrong: A crew membership record names a crew that is not in the crew register.
Matters: The employee is counted in a crew nobody can find, so crew strength and cost per crew are
  wrong.
Detect: Flag crew membership records whose crew has no match in the crew register (business rules
  §11 — the two sides have different data types).
Never flag: Nothing else.
Measured: 19 records.
---

## RULE DQ-R04 - Activity mapping holds spreadsheet error values

- category: Coding consistency
- severity: medium
- entity: activity
- method: rule
- sql_mode: authored
- status: active
- tags: activity, wbs

Wrong: An activity mapping record holds the spreadsheet error `#N/A` in place of a code, a crew,
  a unit or a norm.
Matters: The mapping was loaded from a spreadsheet whose lookup failed. The activity cannot be
  mapped to its WBS or crew, and `#N/A` reads as a real code to any join that does not know.
Detect: Flag mapping records where any of the new activity code, crew codes, unit or norm is
  exactly `#N/A`. Name every affected field.
Never flag: Genuinely blank fields — a different finding.
---

## RULE DQ-R05 - Activity mapping points at a code missing from the activity master

- category: Reference integrity
- severity: medium
- entity: activity
- method: rule
- sql_mode: authored
- status: active
- tags: activity, wbs

Wrong: An activity mapping record names a new activity code that the activity master does not
  contain.
Matters: Tasks of that activity resolve to no WBS group and no crew group, so they appear as
  "unmapped" in every WBS breakdown.
Detect: Examine mapping records with a new activity code. Flag those whose code has no match in
  the activity master. Use the NEW code (business rules §10).
Never flag: Records whose new code is blank or `#N/A` (DQ-R04).
---

## RULE DQ-R06 - Project id in the register carries stray spaces

- category: Coding consistency
- severity: low
- entity: project
- method: rule
- sql_mode: authored
- status: active
- tags: project

Wrong: A project id in the project register has a leading or trailing space.
Matters: Every exact join to the project silently fails, so the project's tasks and wells look
  unlinked.
Detect: Flag register entries whose id differs from its own trimmed value.
Never flag: Nothing else.
Measured: 1 project.
---

## RULE DQ-R07 - Equipment status uses several vocabularies for one meaning

- category: Coding consistency
- severity: low
- entity: equipment
- method: rule
- sql_mode: authored
- status: draft
- tags: equipment

Wrong: Equipment status is recorded both as short codes and as phrases that appear to mean the same
  thing — `IOP`, `In Operation` and `Eqp In Operation`.
Matters: Counts of equipment by status split one state across several rows.
Detect: Flag equipment whose status is a long-form phrase that has an agreed short-code equivalent.
Never flag: Statuses with no agreed equivalent.
**Draft — the business has not stated which codes are equivalent** (business rules §15). It cannot
be checked until that mapping exists.
---

# GROUP P — Schedules and snapshots

Every check here judges the LATEST load of a snapshot table only (business rules §12).

## RULE DQ-P01 - Plan activity finished before it started

- category: Date integrity
- severity: high
- entity: activity
- method: rule
- sql_mode: authored
- status: active
- tags: snapshot, schedule

Wrong: In the latest load of the project schedule, an activity's actual finish is earlier than its
  actual start.
Matters: Durations come out negative and the activity is placed in the wrong period.
Detect: Use the latest load only. Treat the placeholder date 1900-01-01 as not recorded. Flag
  activities holding both real dates where the finish is earlier than the start.
Never flag: Activities where either date is missing or the placeholder.
Measured: 218 activities in the latest load.
---

## RULE DQ-P02 - Plan activity progress is above full or below zero

- category: Value range
- severity: medium
- entity: activity
- method: rule
- sql_mode: authored
- status: active
- tags: snapshot, schedule, progress

Wrong: In the latest load of the project schedule, an activity's progress is below 0 or above 1.
Matters: Progress is a 0–1 fraction. Out-of-range values distort every weighted rollup of the
  project.
Detect: Use the latest load only. Progress is stored as text: convert it safely, and flag values
  below 0 or above 1.
Never flag: Values that cannot be read as a number — a different finding.
Measured: 17 activities below zero.
---

## RULE DQ-P03 - Plan activity shows progress but has no real start date

- category: Lifecycle order
- severity: medium
- entity: activity
- method: rule
- sql_mode: authored
- status: active
- tags: snapshot, schedule, progress

Wrong: In the latest load of the project schedule, an activity of type activity has progress above
  zero but its actual start is missing or the placeholder 1900-01-01.
Matters: Work that has progressed must have started. The placeholder makes the activity look a
  century old to anything that does not know to ignore it.
Detect: Use the latest load and activity rows only. Flag those with progress above 0 whose actual
  start is missing or 1900-01-01.
Never flag: WBS groups and milestones — their dates are rolled up, not recorded.
---

## RULE DQ-P04 - Engineering step complete with no actual finish date

- category: Status consistency
- severity: medium
- entity: activity
- method: rule
- sql_mode: authored
- status: active
- tags: snapshot, engineering

Wrong: In the latest load of the engineering task plan, an activity's progress is complete but it
  has no actual finish date that can be read as a date.
Matters: The engineering milestone cannot be dated, so AFC delivery against its target date cannot
  be judged.
Detect: Use the latest load and activity rows only. Flag rows whose progress reads as 1 and whose
  actual finish is missing or cannot be converted to a date (it is stored as text).
Never flag: Incomplete activities.
---

## RULE DQ-P05 - Document register export appears truncated at a round limit

- category: Load completeness
- severity: high
- entity: row
- method: rule
- sql_mode: authored
- status: disabled
- tags: snapshot, engineering, load
- row_limit: 20000

Wrong: Every recent load of the engineering document register holds exactly {{row_limit}} rows.
Matters: A register of design documents does not grow to a perfectly round number and then stop.
  The export is almost certainly capped, so every document beyond the cap is missing — and every
  check on engineering documents silently sees only part of the register.
Detect: Count rows per load. Report the latest load as the anomaly when its row count is exactly
  {{row_limit}}; the explanation must state how many of the recent loads hit the same count.
  Scope is the latest load.
Never flag: A load whose count differs from {{row_limit}}.
Measured: all 144 loads hold exactly 20,000 rows.
**Disabled — the engineering document register (EngineeringACCDump) is not in
INCLUDED_TABLES.** Add it there and set this rule back to `active` to check it.
---

# GROUP X — Do the two databases agree?

## RULE DQ-X01 - Engineering priority list names a well the well master does not know

- category: Cross-database agreement
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: cross-database, engineering, well master

Wrong: A well on one of the engineering priority lists (Nimr, Marmul or Amal) has no entry in the
  well master.
Matters: Engineering is designing for a well that operations does not track — or the two hold the
  well under different ids. Either way the design work cannot be linked to construction and
  delivery.
Detect: Take every well id from the three priority lists (engineering database). Flag each one with
  no matching well-master entry (operations database), naming its list and well name. Convert the
  numeric ids to text to compare (business rules §2).
Never flag: Wells in the well master that are not on a priority list — the lists are a subset.
Measured: 188 of 365 listed wells.
---

## RULE DQ-X02 - Engineering plan names a well the well master does not know

- category: Cross-database agreement
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: cross-database, engineering, snapshot, well master

Wrong: The latest engineering task plan holds activities for a well that has no entry in the well
  master.
Matters: Engineering effort is being planned against a well operations does not know, so it is
  never reported against a delivered well.
Detect: Use the latest load of the engineering task plan, activity rows only. Take the well id from
  the code (the text after the first dash). Flag each distinct well with no well-master entry, with
  the number of activities.
Never flag: Codes with no dash, which carry no well id.
---

## RULE DQ-X03 - Expected rig-on date disagrees with the SAP drilling sequence

- category: Cross-database agreement
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: cross-database, sap, milestone, well master, snapshot

Wrong: The well master's expected rig-on date differs from the planned start in the latest SAP
  drilling sequence for the same well.
Matters: The well master copies this date from SAP, and every construction deadline is derived from
  it. A stale copy schedules construction against a rig date that has already moved.
Detect: Examine wells holding an expected rig-on date that appear in the latest SAP week. Take the
  earliest planned start SAP holds for the well in that week. Flag disagreements, with the
  difference in days as severity.
Never flag: Wells absent from SAP, and SAP wells absent from the well master — SAP covers every PDO
  well (business rules §2).
Measured: 23 of 313 wells.
---

## RULE DQ-X04 - AFC target date passed but engineering is not finished

- category: Cross-database agreement
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: cross-database, engineering, deadline, well master

Wrong: A well on an engineering priority list is past its AFC target date, but the well master has
  no engineering finish date for it.
Matters: The AFC design is what construction is built to. A priority well past its target with no
  finished engineering is a construction start at risk.
Detect: Take the wells on the three priority lists with an AFC target date earlier than today (the
  Marmul date is text and may say `TBC` — convert it safely and ignore what does not convert).
  Match each to the well master. Flag those with no engineering finish date. Severity is the days
  past the target.
Never flag: Wells with no AFC target date, and wells not in the well master (DQ-X01).
---

## RULE DQ-X05 - Engineering document register names a well the well master does not know

- category: Cross-database agreement
- severity: low
- entity: well
- method: rule
- sql_mode: authored
- status: disabled
- tags: cross-database, engineering, snapshot, well master

Wrong: In the latest load of the engineering document register, documents are filed against a well
  id that has no entry in the well master.
Matters: Design documents filed to an unknown well cannot be found from the well they belong to.
Detect: Use the latest load only. Consider only well ids that are exactly five digits (business
  rules §12 — most values are not well ids). Flag each distinct such well with no well-master entry,
  with the number of documents.
Never flag: Values that are not five-digit numbers.
**Disabled — the engineering document register (EngineeringACCDump) is not in
INCLUDED_TABLES.** Add it there and set this rule back to `active` to check it.
---

# GROUP G — Checks applied across the whole database

Every check above is about one specific thing: a named milestone, a particular deadline, a known
rollup. The six below are different. Each is written once and applied automatically to every
matching part of the database — one check per relationship, per date pair, per measured number —
so a finding still names the individual thing that is broken rather than collapsing dozens of
unrelated problems into a single number.

`expands_over` names the kind of thing to apply the check to. It is never a table name.

| `expands_over` | Applied to |
|---|---|
| `foreign_key` | every declared relationship between two tables |
| `duplicate_key` | every table expected to hold one record per thing |
| `date_pair` | every start/finish date pair |
| `future_date` | every date recording something that already happened |
| `numeric_range` | every number with a measured range |
| `text_numeric` | every piece of text holding numbers |

---

## RULE DQ-G01 - Record points at something that does not exist
- category: Referential integrity
- severity: high
- entity: row
- method: rule
- expands_over: foreign_key
- status: active

Wrong: A record refers to another record that is not there. The relationship is declared in the
  database, so this should be impossible — where it happens, the constraint is untrusted or was
  added after the bad data.
Matters: Every lookup through this link silently drops the record, so it vanishes from reports
  rather than appearing as an error.
Detect: Keep the records whose link has no matching target, ignoring records where the link is
  not set at all. No threshold: one is a defect.
Never flag: An unset link. "Not linked yet" is a different finding from "linked to something
  that does not exist", and treating them as one hides both.
---

## RULE DQ-G02 - The same thing appears more than once
- category: Grain integrity
- severity: high
- entity: row
- method: rule
- expands_over: duplicate_key
- status: draft

Wrong: Something that should appear once appears on more than one record.
Matters: Any count, sum or average over that data double-counts it. This is the single most
  damaging silent error in this kind of database, because the query runs perfectly and simply
  returns a number that is too big.
Detect: Group by the thing that should be unique and keep the groups holding more than one
  record. No threshold.
Never flag: An unset value, and data where repetition is legitimate — a history or snapshot is
  supposed to hold many records per thing.
**Draft — this check is circular as it stands, and must not be activated until that is fixed.**
It is currently applied wherever repetition was *detected*, so it reports repetition in exactly
the places already known to repeat. On the last measurement it flagged over 99% of one table and
over 90% of two others — which describes those tables' shape, not a defect. To make it work, the
business must name the data that genuinely holds one record per thing. Something is unique
because the business says so, never because a measurement says it is not.

---

## RULE DQ-G03 - A finish date comes before its start date
- category: Date integrity
- severity: high
- entity: row
- method: rule
- expands_over: date_pair
- status: active

Wrong: A finish date falls before the start date it is paired with.
Matters: Every duration worked out from the pair is negative, which quietly corrupts averages
  and totals rather than failing.
Detect: Compare the two directly. No threshold: a negative duration is impossible, not merely
  unusual.
Never flag: Records where either date is missing — a different finding. Never compare a plan
  against an outcome here: that measures the plan slipping, not a broken record.
---

## RULE DQ-G04 - A date for something that already happened is in the future
- category: Date integrity
- severity: medium
- entity: row
- method: rule
- expands_over: future_date
- status: draft

Wrong: A date recording something that has already happened is later than today.
Matters: Usually a typo in the year. It makes completed work look outstanding, or pulls a
  forecast years out.
Detect: Compare against the database's own current date, so the check uses the same clock the
  data was written against.
Never flag: Any date holding a target, schedule, forecast or expectation. Those are supposed to
  be in the future — that is what they are for. Where it is not established which of the two a
  date is, it must be left unchecked and reported as a gap in coverage, never assumed. **Draft —
  waiting on a statement of which dates record an outcome.** The database cannot say: both kinds
  look identical. An earlier version guessed from the name and reported thousands of perfectly
  good schedule dates as defects. The business rules already answer this for the well
  milestones; extend the same statement to the task dates and this check can be built with
  nothing guessed.
---

## RULE DQ-G05 - A number is outside the range it should occupy
- category: Value range
- severity: medium
- entity: row
- method: rule
- expands_over: numeric_range
- status: active

Wrong: A number falls outside the range it is supposed to occupy — most often a percentage above
  a hundred, or a negative quantity.
Matters: An out-of-range progress figure spreads into every total that averages or weights it.
Detect: Compare against the bounds that were **measured** from the data rather than assumed, so
  a fraction is judged against its own scale and a percentage against its own. Never substitute
  a bound of your own — the measured scale is the authority, and assuming the wrong one silently
  flags or clears everything.
Never flag: Numbers with no measured scale. Something unmeasured has no bounds to be outside of.
---

## RULE DQ-G06 - A number stored as text cannot be read as a number
- category: Type integrity
- severity: medium
- entity: row
- method: rule
- expands_over: text_numeric
- status: active

Wrong: Something holding a quantity as text contains a value that cannot be read as a number.
Matters: Every calculation has to convert it safely, and a safe conversion turns the bad value
  into a blank — so the record is silently dropped from the calculation instead of failing it.
  The defect is invisible precisely because the safe conversion hides it.
Detect: Attempt a numeric conversion that yields a blank on failure. A value that is present and
  non-blank but fails to convert is the anomaly. No threshold.
Never flag: Blank values, and data where no value at all reads as a number — that holds codes or
  identifiers, was never numeric, and flagging it would report everything.