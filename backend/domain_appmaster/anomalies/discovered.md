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
