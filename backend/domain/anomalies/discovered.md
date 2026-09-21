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

**What is wrong**
A crew-type composition record may refer to an employee type absent from the employee-type reference list. The association can therefore describe a workforce category that the business cannot identify.

**Why it matters**
Crew composition, staffing requirements and workforce reporting can be understated or assigned to the wrong category. Invalid employee types may also disappear from downstream joins.

**How to detect**
For every crew-type employee association, confirm that its employee type exists in the employee-type reference list. Flag non-blank associations with no matching employee type.

**Do NOT flag**
Do not flag a valid employee type that is inactive or no longer used, provided it remains in the reference list. Do not treat an employee type as missing merely because no individual employee currently uses it.

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

**What is wrong**
A crew-to-equipment assignment may name an equipment item that is absent from the equipment register. The assignment would look valid while referring to no usable equipment record.

**Why it matters**
Equipment availability, crew capability and resource planning can be overstated or attributed to the wrong crew. Maintenance and allocation work may also be missed.

**How to detect**
For every non-blank equipment assignment, confirm that the named equipment exists in the equipment register. Report assignments with no matching equipment.

**Do NOT flag**
Do not flag historical assignments if the equipment register is intentionally limited to currently managed equipment, or if the business confirms that the assignment uses a separate equipment source.

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

**What is wrong**
A progress update may identify a well that is absent from the well register. The progress record then cannot be reliably attributed to a real well.

**Why it matters**
Well-level construction and completion reporting can include orphan progress, omit genuine well progress, or show incorrect cumulative performance to PDO and Al Tasnim.

**How to detect**
For every progress record with a well identifier, confirm that the identifier exists in the authoritative well register. Report unmatched identifiers, after treating repeated weekly history as separate progress records rather than duplicate wells.

**Do NOT flag**
Do not flag a progress record while the well register is being loaded for the same reporting cycle, or where the business confirms that the progress source contains a separate well population.

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

**What is wrong**
A schedule snapshot may name a well that cannot be found in the well register. Planned work from that snapshot becomes unassignable at well level.

**Why it matters**
Schedule dates, planned progress and delivery forecasts may be attributed to no well, distorting readiness and delay reporting.

**How to detect**
For each schedule record with a well identifier, confirm that the identifier exists in the authoritative well register. Report unmatched identifiers and retain multiple dated snapshots as history.

**Do NOT flag**
Do not flag records for a separately managed planning population until the business confirms that the schedule source is required to cover the PDO well register.

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

**What is wrong**
An engineering task-plan record may contain a project identifier with no matching project record. The task remains present but cannot be placed within the project structure.

**Why it matters**
Project-level planning, progress rollups and responsibility reporting can silently exclude those tasks or assign them to the wrong project.

**How to detect**
For every non-blank project identifier in the engineering task plan, confirm a match in the authoritative project register. Report unmatched identifiers and evaluate distinct task identities rather than counting history rows as separate tasks.

**Do NOT flag**
Do not flag imported or archived task plans if their project identifiers intentionally refer to a retired system, or until the business confirms which project register is authoritative.

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

**What is wrong**
A revenue record may refer to a task code absent from the engineering task plan. Revenue is then not traceable to a planned task.

**Why it matters**
Planned and actual purpose values, PMS reporting and financial attribution can be omitted or attached to the wrong work.

**How to detect**
Confirm each non-blank revenue task reference matches a task in the authoritative engineering task plan, using the business-defined task identity. Report unmatched revenue records.

**Do NOT flag**
Do not flag revenue for approved standalone adjustments, legacy tasks retained after plan closure, or another revenue source until the business confirms that every revenue record must originate from the engineering task plan.

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

**What is wrong**
An engineering record may contain a well identifier that is absent from the well-priority register used to identify engineering wells. The design record cannot then be tied to a recognised well.

**Why it matters**
Design status, document readiness and engineering delays may be omitted from well reporting or attributed to the wrong well, affecting delivery decisions.

**How to detect**
For every non-blank well identifier on an engineering record, confirm a match in the authoritative engineering-well register. Report unmatched identifiers while preserving the record history.

**Do NOT flag**
Do not flag records for intentionally archived, prospective or non-well engineering documents, or until the business confirms that the engineering-well register is the required authority for this source.

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

**What is wrong**
A task carries a project identifier for which no project record exists. The project reference cannot be resolved because the project register contains no matching record.

**Why it matters**
Project-level progress, ownership, scheduling and reporting cannot reliably place the task in the correct project. Tasks may disappear from project dashboards or be attributed to the wrong work package.

**How to detect**
For each non-blank task project identifier, require a matching project in the project register. Report identifiers with no match.

**Do NOT flag**
Do not flag blank project identifiers here; they are a separate completeness issue. Exclude explicitly retained historical or test tasks only if the business has identified them as outside the active project register.

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

**What is wrong**
Step classification is absent from 71% of revenue records. Those records cannot be assigned to a defined revenue step.

**Why it matters**
Revenue and PMS reporting by step cannot reconcile to the full revenue population. Management may compare incomplete categories or mistake unclassified revenue for zero activity.

**How to detect**
For revenue records that represent an applicable revenue step, require a non-blank step classification and a matching defined step. Report the missing classifications separately from invalid classifications.

**Do NOT flag**
Do not flag records for which the business confirms that step classification is genuinely not applicable, such as summary or adjustment records. That exception must be identified by an agreed record type; do not infer it from the blank value alone.

---
