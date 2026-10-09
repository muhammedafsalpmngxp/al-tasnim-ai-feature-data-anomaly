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

## RULE DQ-S01 - Design tracker has unreadable remaining-days values

- category: Coding consistency
- severity: high
- entity: row
- method: rule
- sql_mode: authored
- status: rejected
- reason: withdrawn after trial
- source: discovered
- evidence: 93 of 895 non-blank remaining-days values (10.4%) cannot be read as a number.
- discovered_from: AppMasterDB+AppMasterEngDB
- decided: 2026-10-09

Wrong: Some design-tracker remaining-days values are text that cannot be interpreted as a number.
Matters: Remaining-time and delivery-risk calculations silently exclude affected engineering records.
Detect: Require every non-blank remaining-days value to be numerically readable before calculating days remaining or aggregating delivery risk.
Never flag: blank values; formally documented nonnumeric status values

---
