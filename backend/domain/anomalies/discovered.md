# DISCOVERED ANOMALIES

Proposals from the Scout that a person has decided on. **This file is maintained by the
application** - Accept and Reject in the UI append to it. Everything here was proposed by the
Scout and kept, or refused, by a human being.

Rules live here rather than in data_anomalies.md so that the two never mix: that file is yours
alone, and deleting this one reverts every machine-proposed rule in a single step.

- `status: probation` runs, and its findings are reported SEPARATELY. It counts towards nothing
  on the dashboard - not the score, not the flagged total, not the check counts - until someone
  promotes it to `active`.
- `status: active` is a fully trusted rule, identical in every way to one written by hand.
- `status: rejected` never runs and never appears in a report. It is kept only so the Scout
  cannot propose the same idea again.

---

## RULE DQ-S01 - Crew assignment names an employee type that does not exist

- category: Reference integrity
- severity: medium
- entity: row
- method: rule
- sql_mode: authored
- status: active
- source: discovered
- evidence: The crew-type employee association holds 1,112 rows, but its employee-type reference has no declared relationship and nothing verifies that the values exist there.
- discovered_from: AlTasnimBI
- decided: 2026-09-19

Wrong: A crew-type composition record names an employee type that is not in the employee-type
  reference list.
Matters: Crew composition and workforce reporting are understated or put in the wrong category,
  and invalid types drop out of downstream joins.
Detect: For every crew-type employee association, require its employee type to exist in the
  reference list. Flag non-blank associations with no match.
Never flag: A valid type that is inactive or no longer used but still in the reference list. A
  type that no individual employee currently uses.
---

## RULE DQ-S02 - Crew equipment assignment names equipment that does not exist

- category: Reference integrity
- severity: high
- entity: row
- method: rule
- sql_mode: authored
- status: rejected
- reason: withdrawn after trial
- source: discovered
- evidence: bridge.crew_equipment.equipment_id is named like a reference; bridge.crew_equipment holds 65 rows and declares no relationship to the equipment register.
- discovered_from: AlTasnimBI
- decided: 2026-09-21

Wrong: A crew-to-equipment assignment names an equipment item that is not in the equipment
  register.
Matters: Equipment availability, crew capability and resource planning are overstated or
  attributed to the wrong crew, and maintenance and allocation work is missed.
Detect: For every non-blank equipment assignment, require the named equipment to exist in the
  equipment register. Report assignments with no match.
Never flag: Historical assignments where the register intentionally holds only currently managed
  equipment, or where the business confirms the assignment uses a separate equipment source.
---

## RULE DQ-S03 - Well progress record is attached to a well that does not exist

- category: Reference integrity
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- source: discovered
- evidence: well.well_progress.well_id is named like a reference; well.well_progress holds 99,924 rows and declares no relationship to the well master.
- discovered_from: AlTasnimBI
- decided: 2026-09-21

Wrong: A progress update identifies a well that is not in the well register.
Matters: Well-level construction and completion reporting includes orphan progress, omits
  genuine well progress, or shows incorrect cumulative performance to PDO and Al Tasnim.
Detect: For every progress record with a well identifier, require a match in the authoritative
  well register. Treat repeated weekly history as separate progress records, not duplicate
  wells.
Never flag: A progress record while the well register is being loaded for the same reporting
  cycle, or where the business confirms the progress source holds a separate well population.
---

## RULE DQ-S04 - Schedule record is attached to an unknown well

- category: Reference integrity
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- source: discovered
- evidence: dbo.schedule_json_data.well_id is named like a reference; dbo.schedule_json_data holds 1,125 rows and declares no relationship to the well register.
- discovered_from: AlTasnimBI
- decided: 2026-09-21

Wrong: A schedule snapshot names a well that cannot be found in the well register.
Matters: Schedule dates, planned progress and delivery forecasts are attributed to no well,
  distorting readiness and delay reporting.
Detect: For each schedule record with a well identifier, require a match in the authoritative
  well register. Retain multiple dated snapshots as history.
Never flag: A separately managed planning population, until the business confirms the schedule
  source is required to cover the PDO well register.
---

## RULE DQ-S05 - Engineering task plan points to a project that cannot be found

- category: Reference integrity
- severity: high
- entity: project
- method: rule
- sql_mode: authored
- status: active
- source: discovered
- evidence: core.engineering_task_plan.project_id is named like a reference; core.engineering_task_plan holds 256,059 rows and declares no relationship to dbo.task_daily_project, which is empty.
- discovered_from: AlTasnimBI
- decided: 2026-09-21

Wrong: An engineering task-plan record carries a project identifier with no matching project
  record.
Matters: Project-level planning, progress rollups and responsibility reporting silently exclude
  those tasks or assign them to the wrong project.
Detect: For every non-blank project identifier in the engineering task plan, require a match in
  the authoritative project register. Judge distinct task identities, not history rows.
Never flag: Imported or archived task plans whose project identifiers intentionally refer to a
  retired system, or anything here until the business confirms which project register is
  authoritative.
---

## RULE DQ-S06 - Revenue record names a task that cannot be found

- category: Reference integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- source: discovered
- evidence: core.revenue.task_code is named like a reference; core.revenue holds 21,566 rows and declares no relationship to core.engineering_task_plan.
- discovered_from: AlTasnimBI
- decided: 2026-09-21

Wrong: A revenue record names a task code that is not in the engineering task plan.
Matters: Planned and actual purpose values, PMS reporting and financial attribution are omitted
  or attached to the wrong work.
Detect: Require each non-blank revenue task reference to match a task in the authoritative
  engineering task plan, using the business-defined task identity. Report unmatched revenue
  records.
Never flag: Approved standalone adjustments, legacy tasks retained after plan closure, or
  another revenue source, until the business confirms every revenue record must originate from
  the engineering task plan.
---

## RULE DQ-S07 - Engineering record names a well that cannot be found

- category: Reference integrity
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- source: discovered
- evidence: dbo.engineering_acc_dump.well_id is named like a reference; dbo.engineering_acc_dump holds 1,555,914 rows and declares no relationship to dbo.engineering_well_priority.
- discovered_from: AlTasnimBI
- decided: 2026-09-21

Wrong: An engineering record carries a well identifier that is absent from the well-priority
  register used to identify engineering wells.
Matters: Design status, document readiness and engineering delays are omitted from well
  reporting or attributed to the wrong well, affecting delivery decisions.
Detect: For every non-blank well identifier on an engineering record, require a match in the
  authoritative engineering-well register. Preserve the record history.
Never flag: Intentionally archived, prospective or non-well engineering documents, or anything
  here until the business confirms the engineering-well register is the required authority for
  this source.
---

## RULE DQ-S08 - Task is attached to a project that cannot be found

- category: Reference integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- source: discovered
- evidence: OBS-053 states that well.task_daily.project_id is a named reference across 110,184 rows, with no declared relationship to dbo.task_daily_project; that table is empty.
- discovered_from: AlTasnimBI
- decided: 2026-09-21

Wrong: A task carries a project identifier for which the project register holds no matching
  record.
Matters: Project-level progress, ownership, scheduling and reporting cannot place the task in
  the correct project, so tasks vanish from project dashboards or land on the wrong work
  package.
Detect: For each non-blank task project identifier, require a matching project in the project
  register. Report identifiers with no match.
Never flag: Blank project identifiers, which are a separate completeness issue. Retained
  historical or test tasks, only where the business has identified them as outside the active
  project register.
---

## RULE DQ-S09 - Revenue record has no step classification

- category: Structural completeness
- severity: medium
- entity: row
- method: rule
- sql_mode: authored
- status: active
- source: discovered
- evidence: OBS-081 states that core.revenue.step_type_id is empty in 71% of 21,566 rows, and that joins or filters through it lose that share of the table.
- discovered_from: AlTasnimBI
- decided: 2026-09-21

Wrong: Step classification is absent from 71% of revenue records, so those records cannot be
  assigned to a defined revenue step.
Matters: Revenue and PMS reporting by step cannot reconcile to the full revenue population, so
  management compares incomplete categories or reads unclassified revenue as zero activity.
Detect: For revenue records that represent an applicable revenue step, require a non-blank step
  classification matching a defined step. Report missing classifications separately from invalid
  ones.
Never flag: Records the business confirms are genuinely not applicable, such as summary or
  adjustment records. That exception must come from an agreed record type, never inferred from
  the blank value alone.
---

## RULE DQ-S10 - Master well is absent from the engineering priority register

- category: Reference integrity
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- source: discovered
- evidence: The engineering priority register contains 802 rows and its well identifier has no declared relationship to the priority source, while the master well register contains 841 rows (OBS-030).
- discovered_from: AlTasnimBI
- decided: 2026-09-25

Wrong: A master well has no corresponding record in the engineering priority register.
Matters: Engineering priority and delivery reporting can exclude wells that exist in the central well population.
Detect: Compare distinct wells in the master well register with distinct wells in the engineering priority register and report master wells without a match.
Never flag: wells intentionally outside engineering-priority scope; confirm that the engineering priority register is intended to cover every master well before flagging

---
