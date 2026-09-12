# DATA ANOMALIES

Every anomaly this engine looks for is declared here, **in business language only**. This file
is the control surface: adding, editing or disabling a check is a change to this file, never to
Python — and never a change to SQL.

## How this file works

You describe **what is wrong and why**. The SQL Author agent reads that description together
with the live `schema.txt`, `value_hints.txt`, `numeric_hints.txt` and `business_rules.md`, and
writes the SQL itself. A Verifier agent then judges the result against this same description
before it is allowed into the catalog.

**Do not put SQL in a rule.** SQL written here would be correct only against the schema as it
stood the day it was typed, which is exactly the guarantee that decays. Describing the intent
instead means a renamed column, a changed grain or an entirely different database is handled by
the agent rather than silently producing a wrong or failing probe.

    ## RULE <id> - <title>
    - category:  groups the section in the report
    - severity:  critical | high | medium | low
    - entity:    what one finding is about (well, task, activity, wbs, ...)
    - method:    rule | statistical | rollup
    - sql_mode:  authored        <- always, for a rule written in this file
    - status:    active | draft | disabled
    - <anything else>: extra context the agent is shown

    **What is wrong**   the condition, in business terms
    **Why it matters**  the consequence, so severity is justifiable
    **How to detect**   the logic in words — which business concepts to compare, and how
    **Do NOT flag**     the exclusions, so the check does not cry wolf

Column names belong in `business_rules.md` (§2 holds the column dictionary). Refer to business
terms here — "the master expected rig-on date" — and let the agent resolve the column. Where a
rule depends on a stated business rule, cite the section: **(business_rules §4)**.

## Status values

`active` compiles and runs. `draft` is parsed and listed but never run — use it for a check
whose business definition is **not yet agreed**, so the gap stays visible instead of being
quietly dropped. `disabled` is switched off deliberately.

Several rules below are `draft` because they need a threshold or a decision the business has
not made. That is the honest state. Per business_rules §11, an undefined rule must never be
invented.

## Thresholds

`tolerance: auto` means **the query must derive its own threshold from the data** (`AVG`,
`STDEV`, `PERCENTILE_CONT`) — never a number pulled from the air. A bare invented constant is
rejected by the Verifier.

A value declared in a rule's own metadata - a deadline in days, a placeholder date - is
**stated, not invented**, and the agent may use it as a literal. Only a constant with no
stated source is rejected.

A threshold that is *definitional* may be a literal: the 60-day and 90-day deadlines below are
approved business rules, and an `epsilon` allowing for floating-point noise when two figures
should be exactly equal is a numeric-precision allowance, not a business judgement.

## Already covered deterministically — do NOT write rules for these

The structural layer generates these from the schema itself, with no model involved. Writing a
rule here that repeats one would double-report the same record:

| Condition | Covered by |
|---|---|
| An end date earlier than its paired start date (rig-on/rig-off, start/end, start/finish) | `GEN-DATE` |
| A foreign key pointing at a row that does not exist — invalid cluster, rig, station, well type, status | `GEN-FK` |
| A progress or percentage value outside its measured range | `GEN-RANGE` |
| Repeated rows for a key that should identify one entity | `GEN-DUP` |
| A date recording something that already happened, set in the future | `GEN-FUT` |
| A numeric quantity stored as text that will not parse | `GEN-CAST` |

The rules below are the ones that need **business knowledge** — a deadline, a lifecycle order, a
completion definition, a rollup.

---

## PATTERNS

Shapes the SQL Author must follow. These are structural templates, not a schema reference: the
SCHEMA block is the only authority on what tables and columns exist.

### Pattern A — the contract

Every probe is a pair. SUMMARY returns exactly one row and is always cheap; DETAIL returns the
offending records and runs only when SUMMARY reports a non-zero count.

```sql
-- SUMMARY: aggregate over the SCOPE, never over the anomalies. Filtering down to the
-- anomalies first makes scope_total equal anomaly_count and the percentage meaningless.
WITH scoped AS (
    SELECT <entity key>,
           CASE WHEN <the anomalous condition> THEN 1 ELSE 0 END AS is_anomaly,
           <a numeric measure of how bad it is>                  AS sev
    FROM <table>
    WHERE <what puts a record IN SCOPE — not what makes it anomalous>
)
SELECT '<RULE-ID>'                                                 AS rule_id,
       COUNT(*)                                                    AS scope_total,
       SUM(is_anomaly)                                             AS anomaly_count,
       CAST(100.0 * SUM(is_anomaly) / NULLIF(COUNT(*),0) AS decimal(9,4)) AS anomaly_pct,
       MAX(CASE WHEN is_anomaly = 1 THEN sev END)                  AS worst_severity_val
FROM scoped;
```

DETAIL must return `entity_key`, `entity_label`, `severity_value`, at least one `evidence_*`
column proving the finding, and `explain_text`, ordered `severity_value DESC`. Never add `TOP`
— the executor caps it, and a hand-added limit makes the count and the rows disagree.

### Pattern B — statistical, self-calibrating

Both tails, with a minimum-sample guard. Declaring an outlier from six observations is noise.

```sql
WITH observed AS (
    SELECT <key>, CAST(<measure> AS float) AS measure
    FROM <table> WHERE <measure> IS NOT NULL
),
stats AS (
    SELECT AVG(measure) AS mean_val, STDEV(measure) AS sd_val, COUNT(*) AS n_obs FROM observed
)
SELECT o.*, ABS(o.measure - s.mean_val) AS deviation
FROM observed o CROSS JOIN stats s
WHERE s.n_obs >= 30 AND s.sd_val > 0
  AND ABS(o.measure - s.mean_val) > 2 * s.sd_val;   -- threshold FROM the data
```

### Pattern C — parent/child rollup

```sql
WITH rolled AS (
    SELECT <parent key>,
           COUNT(*)                                         AS children,
           SUM(CASE WHEN <child complete> THEN 1 ELSE 0 END) AS children_done,
           SUM(<weight> * <progress>) / NULLIF(SUM(<weight>), 0) AS computed_progress
    FROM <child table> GROUP BY <parent key>
)
SELECT r.*, p.<stored progress>, ABS(r.computed_progress - p.<stored progress>) AS gap
FROM rolled r JOIN <parent table> p ON p.<key> = r.<parent key>;
```

Read the scale from NUMERIC HINTS before comparing two progress figures. One side may be a 0-1
fraction and the other a 0-100 percentage, and the comparison is meaningless until they match.

---

# GROUP A — Milestone dates on the well

## RULE DQ-A01 - Master expected rig-on date is missing

- category: Milestone dates
- severity: critical
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, planning

**What is wrong**
A well has no expected (master) rig-on date recorded.

**Why it matters**
This is *the* master date the whole schedule is measured against (business_rules §2). Every
construction deadline, the pegging deadline and the FLAF deadline are all derived from it. A
well without it cannot be assessed for lateness at all — it silently disappears from every
deadline check rather than appearing as a problem.

**How to detect**
Examine every well. Flag the well when the expected rig-on date is absent.

**Do NOT flag**
Nothing. There is no legitimate state in which a planned well lacks this date.

---

## RULE DQ-A02 - Expected rig-off date is missing

- category: Milestone dates
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, planning

**What is wrong**
A well has no expected rig-off date recorded.

**Why it matters**
The planned hook-up deadline is derived from it before the rig is actually off
(business_rules §4). Without it, hook-up cannot be judged on time until rig-off happens.

**How to detect**
Examine every well. Flag the well when the expected rig-off date is absent.

**Do NOT flag**
Wells whose actual rig-off is already recorded — from that point the actual date takes
precedence over the expected one for every deadline, so the missing plan no longer blocks
anything.

---

## RULE DQ-A03 - Rig arrived but no expected rig-on date was ever planned

- category: Milestone dates
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, planning

**What is wrong**
An actual rig-on date exists while the expected rig-on date is absent.

**Why it matters**
The event happened but was never planned, so no variance can be computed and the well is
invisible to schedule reporting. It also means every deadline derived from the master date was
never enforced for this well.

**How to detect**
Restrict the scope to wells whose actual rig-on date is recorded. Flag those whose expected
rig-on date is absent.

**Do NOT flag**
Wells with no actual rig-on date — those are covered by DQ-A01.

---

## RULE DQ-A04 - Drilling finished but no expected rig-off date was ever planned

- category: Milestone dates
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, planning

**What is wrong**
An actual rig-off date exists while the expected rig-off date is absent.

**Why it matters**
Rig-off variance cannot be computed, so drilling performance for this well is unmeasurable.

**How to detect**
Restrict the scope to wells whose actual rig-off date is recorded. Flag those whose expected
rig-off date is absent.

**Do NOT flag**
Wells with no actual rig-off date.

---

## RULE DQ-A05 - Rig arrived after drilling was expected to finish

- category: Milestone dates
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, lifecycle

**What is wrong**
The actual rig-on date falls later than the expected rig-off date — the rig arrived after the
date drilling was supposed to be complete.

**Why it matters**
The planned window has been overtaken by events. Any forecast still built on the expected
rig-off date is meaningless for this well, and the lifecycle order in business_rules §7 is
broken.

**How to detect**
Restrict the scope to wells where both the actual rig-on date and the expected rig-off date are
present. Flag those where rig-on falls after the expected rig-off.

**Do NOT flag**
Wells missing either date — those are separate findings above.

---

## RULE DQ-A06 - Drilling finished before the rig was expected to arrive

- category: Milestone dates
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, lifecycle

**What is wrong**
The actual rig-off date falls earlier than the expected rig-on date — drilling is recorded as
complete before the rig was even due on the well.

**Why it matters**
This is very unlikely to be a real sequence and much more likely a mistyped year or a date
written into the wrong field. Left alone it makes the well appear finished far ahead of plan and
distorts every completion statistic it feeds.

**How to detect**
Restrict the scope to wells where both the actual rig-off date and the expected rig-on date are
present. Flag those where rig-off falls before the expected rig-on.

**Do NOT flag**
A well that is genuinely ahead of schedule by a normal margin is **not** an anomaly
(business_rules §5). This rule fires only on the impossible ordering, not on being early.

---

## RULE DQ-A07 - Hook-up recorded as complete before the rig arrived

- category: Milestone dates
- severity: critical
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, lifecycle, completion

**What is wrong**
The well completion (hook-up) date falls earlier than the actual rig-on date.

**Why it matters**
Hook-up is the last step in the lifecycle (business_rules §7) and cannot precede the rig
arriving. A well marked complete before it was drilled corrupts every completion count and
every progress rollup that reads it.

**How to detect**
Restrict the scope to wells where both the completion date and the actual rig-on date are
present. Flag those where completion falls before rig-on.

**Do NOT flag**
Wells without a completion date — an incomplete well is not an anomaly.

---

## RULE DQ-A08 - Hook-up recorded as complete before drilling finished

- category: Milestone dates
- severity: critical
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, lifecycle, completion

**What is wrong**
The well completion (hook-up) date falls earlier than the actual rig-off date.

**Why it matters**
Hook-up is handed to Al Tasnim only after the rig is off and the well has been cleaned
(business_rules §7). Completing before rig-off is impossible and indicates a wrong date.

**How to detect**
Restrict the scope to wells where both the completion date and the actual rig-off date are
present. Flag those where completion falls before rig-off.

**Do NOT flag**
Wells without a completion date, or without an actual rig-off date.

---

## RULE DQ-A09 - Well completed while drilling is not recorded as finished

- category: Milestone dates
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: draft
- tags: milestone, lifecycle, undefined

**What is wrong**
A completion (hook-up) date exists while the actual rig-off date is absent, so the well is
recorded as finished without drilling ever being recorded as finished.

**Why it matters**
Either the rig-off date was never captured, or the completion date is wrong. Both distort the
lifecycle.

**How to detect**
Restrict the scope to wells with a completion date. Flag those whose actual rig-off date is
absent.

**⚠ Status: draft — not yet agreed.** Per business_rules §11 it is **not defined** whether a
completion date may legitimately be recorded before rig-off is captured, for example where
drilling is tracked in a separate system. Activate this rule only once the business confirms it.

---

## RULE DQ-A10 - Well is not linked to a project

- category: Reference integrity
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: reference, project

**What is wrong**
A well has no project reference recorded.

**Why it matters**
Every well belongs to exactly two construction projects (business_rules §1). A well with no
project link cannot be rolled up into any project total, so project progress is computed from an
incomplete set of wells without anything indicating the gap.

**How to detect**
Examine every well. Flag the well when the project reference is absent or blank.

**Do NOT flag**
Wells whose project reference is present but points at a project that does not exist — that is
a broken reference, already covered by the structural `GEN-FK` layer.

---

## RULE DQ-A11 - Well is not linked to a cluster

- category: Reference integrity
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: reference, cluster

**What is wrong**
A well has no cluster reference recorded.

**Why it matters**
Cluster is the geographic grouping every regional report aggregates by. A well without one is
excluded from cluster reporting silently.

**How to detect**
Examine every well. Flag the well when the cluster reference is absent.

**Do NOT flag**
A cluster value that is present but invalid — covered by `GEN-FK`.

---

# GROUP B — Pegging sheet, the 60-day rule

## RULE DQ-B01 - Pegging sheet missed its 60-day deadline

- category: Milestone deadlines
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- deadline_days: 60
- tags: pegging, deadline, PDO

**What is wrong**
The pegging sheet was not issued by its deadline. The approved deadline is **60 days before the
master expected rig-on date** (business_rules §4).

**Why it matters**
Pegging is PDO's input and Location Construction cannot start without it. A late or absent
pegging sheet pushes the whole construction sequence toward the rig date.

**How to detect**
The deadline is the expected rig-on date minus 60 days. A well has missed it in **either** of
two ways, and both must be counted:

1. the pegging date exists but falls **after** the deadline; or
2. the pegging date is **absent** and the deadline has already passed as of today.

Scope is every well with an expected rig-on date. Severity is how many days late — for a missing
date, measure from the deadline to today.

**⚠ The missing-date case must be counted, not filtered out.** Per business_rules §4 a NULL past
its deadline **is** a miss. Adding a "not null" restriction would silently remove exactly the
worst offenders.

**Do NOT flag**
Wells whose deadline has not yet arrived and whose pegging date is still absent — those are not
late, they are simply not due.

---

## RULE DQ-B02 - Pegging sheet issued after the rig was due on the well

- category: Milestone deadlines
- severity: critical
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: pegging, deadline, PDO

**What is wrong**
The pegging date falls after the expected rig-on date itself — not merely past the 60-day
deadline, but past the date the rig was due.

**Why it matters**
This is a far more extreme failure than ordinary lateness: the input that must precede all
construction arrived after construction was supposed to be finished. It is separated from
DQ-B01 so it cannot be lost among ordinary late-pegging findings.

**How to detect**
Restrict the scope to wells where both the pegging date and the expected rig-on date are
present. Flag those where pegging falls after the expected rig-on. Severity is the number of
days between them.

**Do NOT flag**
Wells that are merely past the 60-day deadline but still before the expected rig-on — those
belong to DQ-B01.

---

## RULE DQ-B03 - Pegging recorded but no deadline can be computed

- category: Milestone deadlines
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: pegging, deadline, planning

**What is wrong**
A pegging date exists but the expected rig-on date is absent, so the 60-day deadline cannot be
derived and the milestone can never be judged on time.

**Why it matters**
The well looks compliant because nothing can prove it isn't. It is excluded from DQ-B01 by the
absence of the very date that check depends on.

**How to detect**
Restrict the scope to wells with a pegging date. Flag those whose expected rig-on date is
absent.

**Do NOT flag**
Wells with no pegging date — those are covered by DQ-A01 and DQ-B01.

---

## RULE DQ-B04 - Pegging issued unusually far ahead of the rig date

- category: Milestone deadlines
- severity: low
- entity: well
- method: statistical
- sql_mode: authored
- status: draft
- tolerance: auto
- tags: pegging, undefined

**What is wrong**
The pegging sheet was issued very much earlier than the 60 days the rule requires.

**Why it matters**
Being early is normally good and is explicitly **not** an anomaly (business_rules §5). An
extreme outlier may indicate a wrong year in the date rather than genuine early delivery.

**How to detect**
Compare the gap between pegging and expected rig-on across all wells and flag only extreme
outliers, with the threshold derived from the population's own distribution — never a fixed
number of days.

**⚠ Status: draft — no agreed threshold.** The business has not defined how early is "too
early". Until it does, this stays off rather than inventing a limit.

---

# GROUP C — FLAF, the 90-day rule

## RULE DQ-C01 - FLAF missed its 90-day deadline

- category: Milestone deadlines
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- deadline_days: 90
- tags: flaf, deadline, PDO

**What is wrong**
The FLAF was not issued by its deadline. The approved deadline is **90 days before the master
expected rig-on date** (business_rules §4).

**Why it matters**
FLAF is PDO's input for Flowline Construction. Late or absent FLAF delays flowline work — and
per business_rules §6 the resulting delay is *not* charged to Al Tasnim, so recording it
correctly matters commercially as well as operationally.

**How to detect**
The deadline is the expected rig-on date minus 90 days. A well has missed it in **either** of
two ways, and both must be counted:

1. the FLAF issue date exists but falls **after** the deadline; or
2. the FLAF issue date is **absent** and the deadline has already passed as of today.

Scope is every well with an expected rig-on date. Severity is how many days late — for a missing
date, measure from the deadline to today.

**⚠ The missing-date case must be counted, not filtered out** (business_rules §4).

**Do NOT flag**
Wells whose deadline has not yet arrived and whose FLAF date is still absent.

---

## RULE DQ-C02 - FLAF issued after the rig was due on the well

- category: Milestone deadlines
- severity: critical
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: flaf, deadline, PDO

**What is wrong**
The FLAF issue date falls after the expected rig-on date itself.

**Why it matters**
The input required 90 days before the rig arrived instead arrived after it was due. Kept
separate from DQ-C01 so this extreme case is visible on its own.

**How to detect**
Restrict the scope to wells where both the FLAF issue date and the expected rig-on date are
present. Flag those where FLAF falls after the expected rig-on. Severity is the days between.

**Do NOT flag**
Wells merely past the 90-day deadline but still before the expected rig-on — those are DQ-C01.

---

## RULE DQ-C03 - FLAF recorded but no deadline can be computed

- category: Milestone deadlines
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: flaf, deadline, planning

**What is wrong**
A FLAF issue date exists but the expected rig-on date is absent, so the 90-day deadline cannot
be derived.

**Why it matters**
The milestone can never be judged, and the well silently escapes DQ-C01.

**How to detect**
Restrict the scope to wells with a FLAF issue date. Flag those whose expected rig-on date is
absent.

**Do NOT flag**
Wells with no FLAF date at all.

---

## RULE DQ-C04 - FLAF issued after the pegging sheet

- category: Milestone deadlines
- severity: low
- entity: well
- method: rule
- sql_mode: authored
- status: draft
- tags: flaf, pegging, undefined

**What is wrong**
The FLAF was issued later than the pegging sheet, although its deadline (90 days) is earlier
than pegging's (60 days).

**Why it matters**
The two deadlines imply FLAF should normally come first. Whether the reverse order is actually
a defect is a business question, not a logical one — both are PDO inputs and may legitimately
arrive in either order.

**How to detect**
Restrict the scope to wells where both dates are present. Flag those where FLAF falls after
pegging.

**⚠ Status: draft — needs business confirmation.** business_rules §4 states the two deadlines
but does not state that the order between them is mandatory. Do not activate until confirmed.

---

# GROUP D — Task execution and progress

## RULE DQ-D01 - Task planned to finish before it starts

- category: Task integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, dates, planning

**What is wrong**
A task's planned (target) start falls after its planned finish.

**Why it matters**
Every planned duration derived from the pair is negative, which silently corrupts averages and
schedule forecasts rather than failing visibly.

**How to detect**
Restrict the scope to tasks where both target dates are present. Flag those where the target
start is later than the target end. Severity is the size of the inversion in days.

**Do NOT flag**
Tasks missing either target date — that is DQ-D10.

---

## RULE DQ-D02 - Task actually finished before it actually started

- category: Task integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- placeholder_date: 1900-01-01
- tags: task, dates, execution

**What is wrong**
A task's actual start falls after its actual finish.

**Why it matters**
Actual execution cannot finish before it begins. Every productivity and duration figure computed
from the pair is wrong, and the norm comparison in DQ-D14 silently inherits the error.

**How to detect**
Restrict the scope to tasks where both actual dates are present. Flag those where the actual
start is later than the actual end. Severity is the size of the inversion in days.

**Do NOT flag**
Tasks where either actual date is the known placeholder value — that is DQ-D06, and counting it
here as well would double-report the same record.

---

## RULE DQ-D03 - Task reports full progress but is not marked complete

- category: Task integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, progress, completion

**What is wrong**
A task's progress has reached 100% while its completion flag still says incomplete.

**Why it matters**
A direct logical contradiction between two fields that must agree. Whichever is wrong, every
rollup that counts completed tasks disagrees with every rollup that sums progress — and the
two appear in different reports.

**How to detect**
Examine tasks at their current state, one row per logical task (see DQ-D11 — this table keeps
history, so the latest record per task must be selected before judging it). Flag a task whose
progress is at its full-scale maximum while its completion flag is not set.

⚠ Read the scale from NUMERIC HINTS first. Progress may be recorded as a 0-1 fraction or as a
0-100 percentage; comparing against the wrong one flags either every task or none.

**Do NOT flag**
Tasks whose progress is below full.

---

## RULE DQ-D04 - Task marked complete with no actual finish date

- category: Task integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, completion, dates

**What is wrong**
A task's completion flag is set but no actual end date was recorded.

**Why it matters**
The task counts as done in every completion total, yet contributes nothing to duration or
productivity analysis — it is complete and unmeasurable at the same time.

**How to detect**
Select the current record per logical task. Flag those marked complete whose actual end date is
absent.

**Do NOT flag**
Tasks not marked complete.

---

## RULE DQ-D05 - Task has an actual finish date but is marked incomplete

- category: Task integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- placeholder_date: 1900-01-01
- tags: task, completion, dates

**What is wrong**
An actual end date exists while the completion flag still says incomplete — the opposite
contradiction to DQ-D04.

**Why it matters**
The work appears to have finished but is still counted as outstanding, overstating remaining
work in every forecast.

**How to detect**
Select the current record per logical task. Flag those with an actual end date whose completion
flag is not set.

**Do NOT flag**
Tasks whose actual end date is the known placeholder value — see DQ-D06.

---

## RULE DQ-D06 - Placeholder date used as a real execution date

- category: Task integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- placeholder_date: 1900-01-01
- tags: task, dates, placeholder

**What is wrong**
A default placeholder date — `1900-01-01` — has been stored in a field meant to hold a real
execution date.

**Why it matters**
This is a known pattern in this data. It is not a missing value and not a real date: it passes
every "is it null" check while being decades wrong, so it silently poisons any duration or
variance computed from it. Every such record must be treated as unusable rather than as an
execution date.

**How to detect**
Examine task records. Flag any whose actual start or actual end equals the placeholder date. The
date itself is a stated data convention, so using it as a literal is correct here.

**Do NOT flag**
Genuinely absent dates — missing and placeholder are different findings, and conflating them
hides which one needs fixing.

---

## RULE DQ-D07 - Quantity executed with no hours recorded

- category: Productivity data
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, productivity, quantity

**What is wrong**
A task records a positive executed quantity while recording zero execution hours.

**Why it matters**
Productivity is quantity per hour. A record with quantity and no hours makes productivity
infinite, distorting any crew or activity average that includes it.

**How to detect**
Restrict the scope to task records where both the executed quantity and the hours figure are
present. Flag those with a quantity greater than zero and hours equal to zero.

**Do NOT flag**
Records where hours are absent rather than zero — unrecorded is not the same as zero, and
business_rules forbids treating a NULL as a zero.

---

## RULE DQ-D08 - Hours recorded with no quantity executed

- category: Productivity data
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, productivity, hours

**What is wrong**
A task records positive execution hours while recording zero executed quantity.

**Why it matters**
This may be legitimate non-productive time, or it may be a missing quantity. It should be
surfaced for a person to decide rather than silently averaged into productivity as a zero.

**How to detect**
Restrict the scope to task records where both figures are present. Flag those with hours greater
than zero and quantity equal to zero.

**Do NOT flag**
Records where the quantity is absent rather than zero.

---

## RULE DQ-D09 - Work remains but no remaining duration is planned

- category: Task integrity
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, forecast

**What is wrong**
A task still has outstanding quantity while its remaining duration has reached zero.

**Why it matters**
A logical contradiction in the forecast: work is left but no time is allocated to it, so the
forecast completion date for this task is unachievable by construction.

**How to detect**
Select the current record per logical task. Flag those where outstanding quantity is greater
than zero while remaining duration is zero.

**Do NOT flag**
Tasks already marked complete.

---

## RULE DQ-D10 - Active task has no planned dates

- category: Task integrity
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, planning, dates

**What is wrong**
A task that is not yet complete has no target start and/or no target end date.

**Why it matters**
Schedule risk and slippage cannot be computed for a task with no plan to measure against. The
task is invisible to every forward-looking analysis.

**How to detect**
Restrict the scope to tasks that are not marked complete. Flag those missing either target date.

**Do NOT flag**
Completed tasks — a plan is no longer needed once the work is done.

---

## RULE DQ-D11 - Conflicting current records for the same logical task

- category: Task grain
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, grain, history

**What is wrong**
The same logical task — the same well and task code — has more than one record that would each
be read as current, and they disagree about progress, completion, dates or crew.

**Why it matters**
⚠ **The anomaly is not that history exists.** This table legitimately keeps one row per update,
and business_rules requires selecting the latest record per task before analysing it. The
anomaly is that two records cannot be ordered into a single current state — so any analysis
picks one arbitrarily, and two reports reading the same data disagree.

**How to detect**
Group task records by well and task code. Within each group identify the records that tie for
most recent. Flag a group where more than one record ties **and** those tied records disagree on
progress, completion flag, or actual dates.

**Do NOT flag**
A task with many historical records that resolve cleanly to one latest record. That is normal
and correct.

---

## RULE DQ-D12 - Task activity cannot be resolved

- category: Reference integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, mapping, activity

**What is wrong**
A task's activity code cannot be resolved through the activity mapping, so the task cannot be
classified.

**Why it matters**
An unclassified task belongs to no activity and therefore to no WBS, so it contributes to no
rollup. It silently disappears from progress reporting rather than appearing as a gap.

**How to detect**
Follow the mapping chain described in business_rules §3: derive the activity identifier from the
task code — the text before the **first** dash — and resolve it through the mapping table. Flag
tasks whose activity identifier cannot be derived, or resolves to nothing.

⚠ Keep unmatched tasks visible: the join must preserve them rather than dropping them, or the
count of unmapped tasks reads zero for exactly the wrong reason (business_rules §3).

**Do NOT flag**
Tasks that resolve successfully.

---

## RULE DQ-D13 - Activity is not mapped to a WBS

- category: Reference integrity
- severity: high
- entity: activity
- method: rule
- sql_mode: authored
- status: active
- tags: mapping, wbs

**What is wrong**
An activity resolves through the mapping but has no WBS group, so its work belongs to no part
of the breakdown structure.

**Why it matters**
WBS is how progress rolls up to the project. An activity outside the structure contributes to
nothing, and the project percentage is computed from an incomplete base.

**How to detect**
Resolve activities to their WBS group as described in business_rules §3, using the **old**
activity code — the mapping is keyed on it, and the new code matches nothing. Flag activities
whose WBS group is absent.

⚠ Report the unmapped tally **beside** the WBS count, never inside it. Counting "(unmapped)" as
if it were a WBS adds a phantom group (business_rules §3).

**Do NOT flag**
Activities with a WBS group.

---

## RULE DQ-D14 - Task duration does not match the published norm

- category: Schedule integrity
- severity: medium
- entity: task
- method: statistical
- sql_mode: authored
- status: active
- tolerance: auto
- min_sample: 30
- tags: schedule, norms, duration

**What is wrong**
A task's actual start-to-end duration differs from the norm published for its activity by far
more than tasks normally do. Both directions matter: **over** the norm is an overrun, **under**
the norm is usually a data-entry error such as an end date copied from a start date.

**Why it matters**
Norms drive planning. A task recorded at a fraction of its norm inflates apparent productivity
and corrupts every forward estimate built on it.

**How to detect**
Resolve each task to its activity and to the norm published for that activity, then compare the
actual number of days elapsed. The threshold must be **self-calibrating**: flag a task whose
absolute deviation is an extreme outlier against the deviations of all other tasks, with a
minimum-sample guard so a rarely-run activity is not judged from a handful of observations. Do
not use a fixed percentage — no business tolerance is defined.

⚠ More than one table in this database publishes activity norms, and they do not all agree.
Choose the one with the most complete, genuinely numeric coverage, and state in the threshold
note which source was used.

**Do NOT flag**
Tasks with a missing or placeholder start or end date — those are DQ-D02 and DQ-D06. Norms that
are not numeric are DQ-D15.

---

## RULE DQ-D15 - Published activity norm is not a usable number

- category: Reference data
- severity: medium
- entity: activity
- method: rule
- sql_mode: authored
- status: active
- tags: norms, reference

**What is wrong**
An activity norm is stored as text and some values cannot be read as a number.

**Why it matters**
A norm that cannot be read is not a loose check — it is *no* check. Those activities vanish from
the duration comparison in DQ-D14 without appearing anywhere as a gap, because the safe numeric
conversion turns the bad value into a null and the row is quietly dropped.

**How to detect**
Examine the published norms. Flag any non-blank value that cannot be converted to a number.

**Do NOT flag**
Norms that are genuinely absent — "not published yet" is a different thing from "published but
unreadable", and conflating them hides the one that is actually a defect.

---

## RULE DQ-D16 - Task crew cannot be resolved

- category: Reference integrity
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: crew, mapping

**What is wrong**
A task carries a crew assignment that cannot be resolved to a real crew.

**Why it matters**
Crew productivity and utilisation are reported per crew. An unresolvable assignment means the
work is counted in the totals but attributed to nobody.

**How to detect**
Restrict the scope to tasks that carry a crew assignment. Flag those whose crew cannot be
resolved through the crew reference.

**Do NOT flag**
Tasks with no crew assignment at all — unassigned is a different condition from misassigned.

---

## RULE DQ-D17 - Employee is not linked to a crew

- category: Reference integrity
- severity: low
- entity: employee
- method: rule
- sql_mode: authored
- status: active
- tags: crew, employee, mapping

**What is wrong**
An employee assignment cannot be resolved through the approved crew-to-employee relationship.

**Why it matters**
Manpower reporting rolls up through the crew relationship. An employee outside it is invisible
to those totals.

**How to detect**
Examine employee-to-crew assignments and flag those that do not resolve to both a real employee
and a real crew.

**Do NOT flag**
Employees legitimately not assigned to any crew.

---

# GROUP E — Construction and delivery deadlines

## RULE DQ-E01 - Construction deadline passed and the rig has still not arrived

- category: Construction deadlines
- severity: critical
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: construction, deadline, rig

**What is wrong**
The construction deadline has passed and the rig has still not come on the well.

**Why it matters**
This is the approved construction-missed rule (business_rules §4). Location and Flowline
Construction must both finish before the rig arrives; neither has a stored completion date, so
the rig itself is the evidence. A well past its deadline with no rig is construction that has
overrun, with the ownership consequences in business_rules §6.

**How to detect**
Per business_rules §4, the construction deadline is **one day before the master expected rig-on
date**, computed — never read from a stored column, and specifically never from the location
start or finish dates, which are not the approved source. A well has missed it when today is
past that deadline **and** its actual rig-on date is still absent. Severity is the days elapsed
since the deadline.

**Do NOT flag**
Wells whose rig has arrived. Once the actual rig-on date is populated the construction deadline
is **not** treated as missed, even if the rig was late — this rule identifies wells still
waiting, not construction delays on wells the rig has already reached.

---

## RULE DQ-E02 - Hook-up completed after its deadline

- category: Construction deadlines
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- grace_days: 2
- tags: hookup, deadline, completion

**What is wrong**
Hook-up finished later than the deadline that applies to the well.

**Why it matters**
Hook-up is Al Tasnim's final delivery and its lateness is directly attributable
(business_rules §6).

**How to detect**
Per business_rules §4 the deadline has **two forms and the actual one takes precedence**:

- while the rig is still on the well, the deadline is the **expected** rig-off date plus 2 days;
- once the actual rig-off date is recorded, the deadline is the **actual** rig-off date plus 2
  days, and this form wins.

Choose the applicable form per well, then flag a well whose completion date falls after it, or
whose completion date is absent once the deadline has passed. Severity is the days late.

**Do NOT flag**
Wells whose deadline has not yet arrived. A hook-up *deadline* and a *completed well* are
different things and must not be conflated (business_rules §8).

---

## RULE DQ-E03 - Completed well still being treated as active

- category: Lifecycle consistency
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: lifecycle, completion, slippage

**What is wrong**
A well that is complete — it has a hook-up completion date (business_rules §8) — still carries
an open or in-progress status, or still has incomplete construction activities attached to it.

**Why it matters**
Completed wells must be excluded from live slippage analysis. A completed well left in the
active set inflates the outstanding-work figure indefinitely and never resolves, because nothing
further will ever happen to it.

**How to detect**
Restrict the scope to wells with a completion date recorded. Flag those whose status still
indicates active or in progress, or which still have construction activities below full
progress. Severity is the number of records still open against the well.

**Do NOT flag**
Wells without a completion date — by definition they are not complete and belong in the active
set.

---

## RULE DQ-E04 - Rig arrived later than planned

- category: Schedule variance
- severity: low
- entity: well
- method: rule
- sql_mode: authored
- status: draft
- tags: schedule, variance, delay

**What is wrong**
The actual rig-on date is later than the expected rig-on date.

**Why it matters**
⚠ **This is a schedule delay, not a data-quality defect.** The data is correct; the schedule
slipped. It is listed here so the distinction is explicit rather than assumed.

**How to detect**
Restrict the scope to wells where both rig-on dates are present, and flag those where the actual
falls after the expected. The variance must be **signed**, so the direction carries the meaning:
negative is ahead of schedule, positive is behind (business_rules §5).

**⚠ Status: draft — deliberately not run in a data-quality report.** Being early is explicitly
not an anomaly, and being late is a performance fact rather than a defect. Activate this only if
the report is meant to cover schedule performance as well as data quality.

**Do NOT flag**
Wells that arrived early. Never describe an early actual date as a delay or an anomaly, and
never take the absolute value and call it days of delay (business_rules §5).

---

## RULE DQ-E05 - Drilling finished later than planned

- category: Schedule variance
- severity: low
- entity: well
- method: rule
- sql_mode: authored
- status: draft
- tags: schedule, variance, delay

**What is wrong**
The actual rig-off date is later than the expected rig-off date.

**Why it matters**
As with DQ-E04 this is a schedule delay, not a data defect — and drilling is PDO's activity, not
Al Tasnim's (business_rules §7).

**How to detect**
Restrict the scope to wells where both rig-off dates are present and compute the signed variance
between them.

**⚠ Status: draft** — same reasoning as DQ-E04.

**Do NOT flag**
Wells that finished drilling early.

---

# GROUP F — WBS and progress rollup

## RULE DQ-F01 - Rig-off recorded but pre-rig-on construction is incomplete

- category: Lifecycle consistency
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: schedule, lifecycle, rig

**What is wrong**
Drilling is finished — the actual rig-off date is recorded — but Location and/or Flowline
construction activities are still below full completion.

**Why it matters**
Construction must complete before the rig arrives, so by rig-off it cannot legitimately still be
open. Either progress was never updated, which understates completion everywhere it rolls up, or
the work genuinely ran past the rig.

**How to detect**
Restrict the scope to wells whose actual rig-off date is recorded. Flag those with at least one
construction activity below full progress, and those with no construction activities recorded at
all, which is itself a gap.

⚠ Two things decide whether this rule works at all:

- **Use the well master table**, the one that actually holds well records. This database also
  contains an empty table with a nearly identical name and the same columns; grounding onto it
  produces a probe that examines zero rows and reports a clean result.
- **The construction-stream classification is largely unrecorded.** Joining to it in a way that
  requires a match discards most of the table. Keep unclassified activities in scope — work
  whose stream is unrecorded is itself a gap, not something to drop.

**Do NOT flag**
Hook-up activities. Per business_rules §7 hook-up legitimately runs **after** rig-off, so the
filter must name the construction streams explicitly rather than excluding hook-up — a new
stream added later must not silently start being treated as pre-rig-on work.

---

## RULE DQ-F02 - All activities of a WBS are complete but the WBS is not

- category: Rollup consistency
- severity: high
- entity: wbs
- method: rollup
- sql_mode: authored
- status: active
- epsilon: 0.0001
- tags: wbs, progress, rollup

**What is wrong**
Every activity mapped to a WBS within a well reports full completion, yet the WBS itself
computes to less than complete.

**Why it matters**
The WBS percentage feeds project progress. A WBS stuck below complete when its work is finished
understates the project indefinitely, and no individual activity looks wrong — so nobody
investigating activity by activity will ever find it.

**How to detect**
Resolve each activity to its WBS through the two-hop mapping in business_rules §3, using the
**old** activity code. Per well and WBS, compare how many activities are at full completion
against the total, and compute the WBS figure using §9's rule that **activities within one WBS
carry equal weight** — each contributes one divided by the number of activities in that WBS.
Flag a WBS where every activity is complete but the computed figure is not.

The `epsilon` above is a floating-point allowance for comparing two decimals that should be
exactly equal. It is a precision tolerance, **not** a business threshold — say so in the
threshold note.

**Do NOT flag**
WBS groups with no mapped activities — an unmapped task is a mapping gap (DQ-D13), not a rollup
error, and reporting it twice helps nobody.

---

## RULE DQ-F03 - WBS weightage or rollup disagrees with recorded well progress

- category: Rollup consistency
- severity: critical
- entity: well
- method: rollup
- sql_mode: authored
- status: active
- epsilon: 0.0001
- tolerance: auto
- tags: wbs, pms, weightage, progress

**What is wrong**
Two related defects, both of which corrupt the overall progress percentage of a construction
project:

1. **the weightage total is wrong** — a well's PMS weightages do not add up to the same total as
   every other well's; or
2. **the rollup disagrees** — the weighted progress recomputed from the activities does not
   match the progress actually recorded against the well.

**Why it matters**
Overall progress is the number the client sees. If the weights are wrong, or the stored figure
was never recalculated after its parts changed, every report built on it is wrong — and nothing
at the activity level looks out of place.

**How to detect**
Recompute each well's progress as the weighted average of its activities, using the PMS weight
per activity (business_rules §9), and compare it against the **latest** recorded progress for
that well — a snapshot table keeps one row per week, so the most recent row per well must be
selected before comparing.

Two thresholds, of two different kinds, and the difference matters:

- the **weightage total** is **self-calibrating** — the correct total is not declared anywhere,
  so take it to be the population median and flag a well that sits beyond the population's own
  spread. Where every well agrees, the spread is zero and any disagreement at all is flagged,
  which is the desired behaviour;
- the **rollup gap** uses `epsilon` only, because the correct gap is **definitionally zero** — a
  recomputed total and a stored total should be equal, so the only allowance needed is for
  floating-point noise.

⚠ **Check the scale before comparing.** The activity progress and the recorded well progress may
be held on different scales. Read NUMERIC HINTS and normalise; comparing a fraction against a
percentage silently flags every well.

**Do NOT flag**
Wells with no activity records or no recorded progress — there is nothing to reconcile, and
"missing" is a different finding from "inconsistent".
