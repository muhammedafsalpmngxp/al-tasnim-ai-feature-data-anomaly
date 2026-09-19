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
