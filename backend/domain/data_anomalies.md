# DATA ANOMALIES

Every anomaly this engine looks for is declared here. This file is the control surface: adding,
editing or disabling a check is a change to this file, never to Python.

## How to write a rule

    ## RULE <id> - <title>
    - category:  groups the section in the report
    - severity:  critical | high | medium | low
    - entity:    what one finding is about (well, task, wbs, ...)
    - method:    rule | statistical | rollup
    - sql_mode:  pinned | seed | authored
    - status:    active | draft | disabled
    - <anything else>: becomes a {{placeholder}} usable in the SQL below

    **What is wrong** / **Why it matters** / **How to detect** / **Do NOT flag**

    ```sql summary
    ```sql detail

`sql_mode` decides how much the agents are involved, and therefore what a compile costs:

| mode | behaviour | LLM calls |
|---|---|---|
| `seed` **(default — use this)** | The SQL below is a **reference, not an answer**. Grounding re-resolves the intent against the live schema, and the SQL Author writes the query — adapting, improving or fully rewriting the reference. | 2-3 |
| `authored` | No SQL at all; written from the prose. | 3-4 |
| `pinned` | SQL used verbatim; the Author is skipped. Still validated, executed, contract-checked and verified. | 1 |

**Prefer `seed`.** The SQL written here was correct against the schema as it stood on the day
it was written — which is exactly the guarantee that decays. In `seed` mode a renamed column,
a changed grain or a whole different database is repaired by the Author instead of silently
producing a wrong or failing probe. The reference SQL is how the business intent is expressed
precisely; the agent is what keeps it true.

Use `pinned` only for a query you have hand-tuned against *this* database and want frozen, and
expect to switch it back to `seed` the first time the schema moves under it.

## The two-query contract

Every probe is a **pair**, and this is what keeps cost and memory bounded.

**SUMMARY** must return **exactly one row**:

| column | meaning |
|---|---|
| `rule_id` | literal, use `'{{rule_id}}'` |
| `scope_total` | how many entities were EXAMINED (never 0 if the query works) |
| `anomaly_count` | how many were flagged |
| `anomaly_pct` | `100.0 * anomaly_count / NULLIF(scope_total,0)` |
| `worst_severity_val` | the largest deviation seen, or NULL |

**DETAIL** returns the offending rows:

| column | meaning |
|---|---|
| `entity_key` | stable identifier of the thing that is wrong |
| `entity_label` | human-readable name |
| `severity_value` | numeric size of the deviation (days, points, count) |
| `evidence_*` | one or more columns PROVING it — the actual values |
| `explain_text` | one computed sentence describing the breach |

`ORDER BY severity_value DESC` is required: a capped read must still show the worst rows.

Do **not** add `TOP` to DETAIL — the executor caps it, and a hand-added `TOP` would make
`anomaly_count` and the row count disagree.

## Thresholds

`tolerance: auto` means **the query must derive its own threshold from the data**
(`AVG`, `STDEV`, `PERCENTILE_CONT`) — never a number pulled from the air. A bare invented
constant is rejected by the Verifier.

A threshold that is *definitional* is not invented and may be a literal: an `epsilon` allowing
for floating-point noise when two figures should be exactly equal is a numeric-precision
allowance, not a business judgement. Say which you are using in **How to detect**.

---

## PATTERNS

Worked shapes shown to the SQL Author when it has to write a probe from prose. These are
patterns, not a schema reference: the SCHEMA block is the only authority on what exists.

### Pattern A — the contract, minimal

```sql
-- SUMMARY: aggregate over the SCOPE, never over the anomalies. Filtering down to the
-- anomalies first makes scope_total equal anomaly_count and the percentage meaningless.
WITH scoped AS (
    SELECT x.some_key,
           CASE WHEN <the anomalous condition> THEN 1 ELSE 0 END AS is_anomaly,
           <a numeric measure of how bad it is>                  AS sev
    FROM <table> x
    WHERE <what puts a row IN SCOPE — not what makes it anomalous>
)
SELECT 'RULE-ID'                                                   AS rule_id,
       COUNT(*)                                                    AS scope_total,
       SUM(is_anomaly)                                             AS anomaly_count,
       CAST(100.0 * SUM(is_anomaly) / NULLIF(COUNT(*),0) AS decimal(9,4)) AS anomaly_pct,
       MAX(CASE WHEN is_anomaly = 1 THEN sev END)                  AS worst_severity_val
FROM scoped;
```

### Pattern B — statistical, self-calibrating

Both tails, with a minimum-sample guard. Declaring an outlier from six observations is noise,
not a finding.

```sql
WITH observed AS (
    SELECT x.some_key, CAST(x.measure AS float) AS measure
    FROM <table> x
    WHERE x.measure IS NOT NULL
),
stats AS (
    SELECT AVG(measure) AS mean_val, STDEV(measure) AS sd_val, COUNT(*) AS n_obs
    FROM observed
)
SELECT o.some_key, o.measure, s.mean_val, s.sd_val,
       ABS(o.measure - s.mean_val) AS deviation,
       CASE WHEN o.measure > s.mean_val THEN 'HIGH' ELSE 'LOW' END AS direction
FROM observed o
CROSS JOIN stats s
WHERE s.n_obs >= 30                                    -- minimum-sample guard
  AND s.sd_val > 0                                     -- no spread => no outliers
  AND ABS(o.measure - s.mean_val) > 2 * s.sd_val;      -- threshold FROM the data
```

`PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x) OVER ()` gives a median when the mean is
distorted by outliers. It is a window function, so wrap it in `SELECT DISTINCT` to get one row.

### Pattern C — parent/child rollup

```sql
WITH child AS (
    SELECT c.parent_key, c.child_key, c.progress, c.weight
    FROM <child table> c
),
rolled AS (
    SELECT parent_key,
           COUNT(*)                                              AS children,
           SUM(CASE WHEN progress >= 100 THEN 1 ELSE 0 END)      AS children_done,
           -- weighted where a weight exists; equal-weighted (1/N) where the business says
           -- children of one parent weigh the same
           SUM(weight * progress) / NULLIF(SUM(weight), 0)       AS computed_progress
    FROM child GROUP BY parent_key
)
SELECT r.*, p.stored_progress, ABS(r.computed_progress - p.stored_progress) AS gap
FROM rolled r JOIN <parent table> p ON p.parent_key = r.parent_key;
```

Read the scale from NUMERIC HINTS before comparing two progress figures: one side may be a
0-1 fraction and the other a 0-100 percentage, and the comparison is meaningless until they
match.

---

## RULE DQ-001 - Rig-off recorded but pre-rig-on construction incomplete

- category: Lifecycle consistency
- severity: high
- entity: well
- method: rule
- sql_mode: seed
- status: active
- tags: schedule, lifecycle, rig

**What is wrong**
`rig_off_date` is populated, so PDO has finished drilling — but Location (LOC) and/or Flowline
(FLC) construction activities are still below 100%. Construction must complete before rig-on,
so by rig-off it cannot legitimately still be open.

**Why it matters**
Either construction progress was never updated (a reporting gap that understates completion
everywhere it is rolled up), or the work genuinely ran past the rig. Both distort project
progress and the penalty position under business_rules §6.

**How to detect**
A well with `rig_off_date IS NOT NULL` that has at least one `core.revenue` row of step type
LOC or FLC below 100% — or no construction rows at all, which is itself a gap. No threshold is
involved: "below 100%" is the business definition of incomplete.

**Do NOT flag**
Hook-up activities. Per business_rules §7 hook-up legitimately runs after rig-off, which is why
the step-type filter is an explicit `IN ('LOC','FLC')` rather than an exclusion — a new step
type added later must not silently start being treated as pre-rig-on work.

```sql summary
WITH constr AS (
    SELECT r.well_id,
           SUM(CASE WHEN ISNULL(r.actual_progress, 0) < 1 THEN 1 ELSE 0 END) AS open_tasks
    FROM core.revenue r
    LEFT JOIN ref.step_type st ON st.step_type_id = r.step_type_id
    -- step_type_id is 71% NULL (measured): an INNER JOIN would discard most of the
    -- table. An unclassified row is construction work whose stream is unrecorded -
    -- that is itself a gap, so it stays in scope rather than vanishing.
    WHERE st.step_type IN ('LOC','FLC') OR r.step_type_id IS NULL
    GROUP BY r.well_id
),
scoped AS (
    SELECT wm.well_id,
           CASE WHEN c.well_id IS NULL OR c.open_tasks > 0 THEN 1 ELSE 0 END AS is_anomaly,
           CAST(ISNULL(c.open_tasks, 0) AS float) AS sev
    FROM well.well_master wm
    LEFT JOIN constr c ON c.well_id = wm.well_id
    WHERE wm.rig_off_date IS NOT NULL
)
SELECT '{{rule_id}}'                                                      AS rule_id,
       COUNT(*)                                                           AS scope_total,
       SUM(is_anomaly)                                                    AS anomaly_count,
       CAST(100.0 * SUM(is_anomaly) / NULLIF(COUNT(*),0) AS decimal(9,4)) AS anomaly_pct,
       MAX(CASE WHEN is_anomaly = 1 THEN sev END)                         AS worst_severity_val
FROM scoped;
```

```sql detail
WITH constr AS (
    SELECT r.well_id,
           SUM(CASE WHEN ISNULL(r.actual_progress, 0) < 1 THEN 1 ELSE 0 END) AS open_tasks,
           COUNT(*)              AS total_tasks,
           MIN(r.actual_progress) AS lowest_pct
    FROM core.revenue r
    LEFT JOIN ref.step_type st ON st.step_type_id = r.step_type_id
    -- step_type_id is 71% NULL (measured): an INNER JOIN would discard most of the
    -- table. An unclassified row is construction work whose stream is unrecorded -
    -- that is itself a gap, so it stays in scope rather than vanishing.
    WHERE st.step_type IN ('LOC','FLC') OR r.step_type_id IS NULL
    GROUP BY r.well_id
)
SELECT wm.well_id                                                    AS entity_key,
       CAST(wm.ramz_id AS nvarchar(100))                             AS entity_label,
       CAST(ISNULL(c.open_tasks, ISNULL(c.total_tasks, 0)) AS float) AS severity_value,
       wm.rig_off_date                                               AS evidence_rig_off_date,
       wm.rig_on_date                                                AS evidence_rig_on_date,
       wm.ex_rig_on_date                                             AS evidence_ex_rig_on_date,
       ISNULL(c.total_tasks, 0)                                      AS evidence_construction_tasks,
       ISNULL(c.open_tasks, 0)                                       AS evidence_open_tasks,
       c.lowest_pct                                                  AS evidence_lowest_activity_pct,
       CASE WHEN c.well_id IS NULL
            THEN CONCAT('Rig off ', CONVERT(varchar(10), wm.rig_off_date, 23),
                        ' but this well has NO LOC/FLC construction rows at all')
            ELSE CONCAT('Rig off ', CONVERT(varchar(10), wm.rig_off_date, 23), ': ',
                        c.open_tasks, ' of ', c.total_tasks,
                        ' LOC/FLC activities still below 100%')
       END                                                           AS explain_text
FROM well.well_master wm
LEFT JOIN constr c ON c.well_id = wm.well_id
WHERE wm.rig_off_date IS NOT NULL
  AND (c.well_id IS NULL OR c.open_tasks > 0)
ORDER BY severity_value DESC;
```

---

## RULE DQ-002 - Task duration does not meet the activity norm

- category: Schedule integrity
- severity: medium
- entity: task
- method: statistical
- sql_mode: seed
- status: active
- tolerance: auto
- min_sample: 30
- tags: schedule, norms, duration

**What is wrong**
A task's actual start-to-end duration differs from the norm published for its activity by far
more than tasks normally do. Both directions matter: **over** the norm is an overrun, **under**
the norm is usually a data-entry error (a date typed wrong, or an end date copied from a start).

**Why it matters**
Norms drive planning. A task recorded at a fraction of its norm inflates apparent productivity
and corrupts every forward estimate built on it.

**How to detect**
Derive `activity_id` from `task_code` (business_rules §3), join it to the published norm, and
compare the actual day count. The threshold is **self-calibrating**: flag a task whose absolute
deviation exceeds the population's own mean absolute deviation by more than two standard
deviations, with a minimum-sample guard so a rarely-run activity is not "analysed" from a
handful of observations. No fixed percentage is used, because no business tolerance is defined.

**Do NOT flag**
Tasks with a missing start or end date — that is a different anomaly (a missing value), and
counting it here would double-report the same record. Norms that do not parse as a number are
also excluded here and covered by DQ-002B.

```sql summary
WITH t AS (
    SELECT td.well_id, td.task_code,
           LEFT(td.task_code, NULLIF(CHARINDEX('-', td.task_code), 0) - 1) AS activity_id,
           CAST(DATEDIFF(day, td.actual_start, td.actual_end) + 1 AS float) AS actual_days
    FROM well.task_daily td
    WHERE td.actual_start IS NOT NULL
      AND td.actual_end IS NOT NULL
      AND td.task_code IS NOT NULL
),
n AS (
    SELECT CAST(m.Activity_ID AS nvarchar(50))  AS activity_id,
           TRY_CAST(m.Norms AS float)           AS norm_days
    FROM dbo.mapping_master m
    WHERE TRY_CAST(m.Norms AS float) > 0
),
j AS (
    SELECT t.task_code, ABS(t.actual_days - n.norm_days) AS abs_dev
    FROM t JOIN n ON n.activity_id = t.activity_id
),
stats AS (
    SELECT AVG(abs_dev) AS mean_dev, STDEV(abs_dev) AS sd_dev, COUNT(*) AS n_obs FROM j
),
scoped AS (
    SELECT CASE WHEN s.n_obs >= {{min_sample}} AND s.sd_dev > 0
                 AND j.abs_dev > s.mean_dev + 2 * s.sd_dev
                THEN 1 ELSE 0 END AS is_anomaly,
           j.abs_dev              AS sev
    FROM j CROSS JOIN stats s
)
SELECT '{{rule_id}}'                                                      AS rule_id,
       COUNT(*)                                                           AS scope_total,
       SUM(is_anomaly)                                                    AS anomaly_count,
       CAST(100.0 * SUM(is_anomaly) / NULLIF(COUNT(*),0) AS decimal(9,4)) AS anomaly_pct,
       MAX(CASE WHEN is_anomaly = 1 THEN sev END)                         AS worst_severity_val
FROM scoped;
```

```sql detail
WITH t AS (
    SELECT td.well_id, td.task_code,
           LEFT(td.task_code, NULLIF(CHARINDEX('-', td.task_code), 0) - 1) AS activity_id,
           td.actual_start, td.actual_end,
           CAST(DATEDIFF(day, td.actual_start, td.actual_end) + 1 AS float) AS actual_days
    FROM well.task_daily td
    WHERE td.actual_start IS NOT NULL
      AND td.actual_end IS NOT NULL
      AND td.task_code IS NOT NULL
),
n AS (
    SELECT CAST(m.Activity_ID AS nvarchar(50))  AS activity_id,
           TRY_CAST(m.Norms AS float)           AS norm_days
    FROM dbo.mapping_master m
    WHERE TRY_CAST(m.Norms AS float) > 0
),
j AS (
    SELECT t.*, n.norm_days,
           t.actual_days - n.norm_days       AS dev_days,
           ABS(t.actual_days - n.norm_days)  AS abs_dev
    FROM t JOIN n ON n.activity_id = t.activity_id
),
stats AS (
    SELECT AVG(abs_dev) AS mean_dev, STDEV(abs_dev) AS sd_dev, COUNT(*) AS n_obs FROM j
)
SELECT j.task_code                                             AS entity_key,
       CAST(j.activity_id AS nvarchar(100))                    AS entity_label,
       j.abs_dev                                               AS severity_value,
       j.well_id                                               AS evidence_well_id,
       j.actual_start                                          AS evidence_actual_start,
       j.actual_end                                            AS evidence_actual_end,
       j.actual_days                                           AS evidence_actual_days,
       j.norm_days                                             AS evidence_norm_days,
       j.dev_days                                              AS evidence_deviation_days,
       CAST(s.mean_dev + 2 * s.sd_dev AS decimal(18,4))        AS evidence_threshold_days,
       CONCAT(CASE WHEN j.dev_days > 0 THEN 'OVER' ELSE 'UNDER' END,
              ' norm by ', CAST(CAST(j.abs_dev AS decimal(18,1)) AS varchar(20)),
              ' days (actual ', CAST(CAST(j.actual_days AS decimal(18,1)) AS varchar(20)),
              ' vs norm ', CAST(CAST(j.norm_days AS decimal(18,1)) AS varchar(20)),
              '); population threshold ',
              CAST(CAST(s.mean_dev + 2 * s.sd_dev AS decimal(18,1)) AS varchar(20)))
                                                               AS explain_text
FROM j CROSS JOIN stats s
WHERE s.n_obs >= {{min_sample}}
  AND s.sd_dev > 0
  AND j.abs_dev > s.mean_dev + 2 * s.sd_dev
ORDER BY severity_value DESC;
```

---

## RULE DQ-002B - Published activity norm is not a number

- category: Reference data
- severity: medium
- entity: activity
- method: rule
- sql_mode: seed
- status: active
- tags: norms, reference

**What is wrong**
The norm is stored in a text column and some values do not parse as a number, so every
duration check silently skips those activities.

**Why it matters**
A norm that cannot be read is not a loose check — it is *no* check. The activities involved
disappear from DQ-002 without appearing anywhere as a gap.

**How to detect**
`TRY_CAST` the norm; a non-NULL value that fails to cast is the anomaly. No threshold.

**Do NOT flag**
Rows where the norm is genuinely NULL — "not published yet" is a different thing from
"published but unreadable", and conflating them hides the one that is actually a defect.

```sql summary
WITH scoped AS (
    SELECT CASE WHEN TRY_CAST(m.Norms AS float) IS NULL THEN 1 ELSE 0 END AS is_anomaly
    FROM dbo.mapping_master m
    WHERE m.Norms IS NOT NULL AND LTRIM(RTRIM(m.Norms)) <> ''
)
SELECT '{{rule_id}}'                                                      AS rule_id,
       COUNT(*)                                                           AS scope_total,
       SUM(is_anomaly)                                                    AS anomaly_count,
       CAST(100.0 * SUM(is_anomaly) / NULLIF(COUNT(*),0) AS decimal(9,4)) AS anomaly_pct,
       CAST(SUM(is_anomaly) AS float)                                     AS worst_severity_val
FROM scoped;
```

```sql detail
SELECT CAST(m.Activity_ID AS nvarchar(100))       AS entity_key,
       m.Old_Activity_Code                        AS entity_label,
       CAST(1 AS float)                           AS severity_value,
       m.Norms                                    AS evidence_norm_raw_value,
       m.UOM                                      AS evidence_uom,
       m.New_Activity_Code                        AS evidence_new_activity_code,
       CONCAT('Norm value ''', m.Norms, ''' is not numeric, so every duration check for this ',
              'activity is silently skipped')     AS explain_text
FROM dbo.mapping_master m
WHERE m.Norms IS NOT NULL
  AND LTRIM(RTRIM(m.Norms)) <> ''
  AND TRY_CAST(m.Norms AS float) IS NULL
ORDER BY severity_value DESC;
```

---

## RULE DQ-003 - All activities of a WBS are complete but the WBS is not

- category: Rollup consistency
- severity: high
- entity: wbs
- method: rollup
- sql_mode: seed
- status: active
- epsilon: 0.01
- tags: wbs, progress, rollup

**What is wrong**
Every activity mapped to a WBS within a well reports 100%, yet the WBS itself computes to less
than 100%.

**Why it matters**
The WBS percentage feeds project progress. A WBS stuck below 100% when its work is finished
understates the project indefinitely, and no individual activity looks wrong.

**How to detect**
Resolve each `core.revenue.task_code` to its WBS through the two-hop mapping in
business_rules §3 (`Old_Activity_Code`, never `New_`). Per well and WBS, compare the count of
activities at 100% against the total, and compute the WBS percentage using §9's rule that
activities within one WBS carry **equal** weight (`1/N`). `epsilon` is a floating-point
allowance for comparing two decimals that should be exactly equal — a precision tolerance, not
a business threshold.

**Do NOT flag**
WBS groups with no mapped activities — an unmapped task is a mapping gap, not a rollup error,
and is covered separately by the generic mapping probes.

```sql summary
WITH act AS (
    SELECT r.well_id, r.task_code, r.actual_progress,
           amc.activity_group_description AS wbs
    FROM core.revenue r
    LEFT JOIN dbo.mapping_master m
           ON CAST(m.Activity_ID AS nvarchar(50)) =
              LEFT(r.task_code, NULLIF(CHARINDEX('-', r.task_code), 0) - 1)
    LEFT JOIN dbo.activity_master_csv amc
           ON amc.activity_code = m.Old_Activity_Code
),
roll AS (
    SELECT well_id, wbs,
           COUNT(*)                                                   AS activities,
           SUM(CASE WHEN actual_progress >= 1 THEN 1 ELSE 0 END)     AS activities_done,
           CAST(SUM(actual_progress) AS float) / NULLIF(COUNT(*), 0)   AS wbs_pct_computed
    FROM act
    WHERE wbs IS NOT NULL
    GROUP BY well_id, wbs
),
scoped AS (
    SELECT CASE WHEN activities_done = activities
                 AND wbs_pct_computed < 1 - {{epsilon}}
                THEN 1 ELSE 0 END                    AS is_anomaly,
           1 - wbs_pct_computed                    AS sev
    FROM roll
    WHERE activities > 0
)
SELECT '{{rule_id}}'                                                      AS rule_id,
       COUNT(*)                                                           AS scope_total,
       SUM(is_anomaly)                                                    AS anomaly_count,
       CAST(100.0 * SUM(is_anomaly) / NULLIF(COUNT(*),0) AS decimal(9,4)) AS anomaly_pct,
       MAX(CASE WHEN is_anomaly = 1 THEN sev END)                         AS worst_severity_val
FROM scoped;
```

```sql detail
WITH act AS (
    SELECT r.well_id, r.task_code, r.actual_progress,
           amc.activity_group_description AS wbs
    FROM core.revenue r
    LEFT JOIN dbo.mapping_master m
           ON CAST(m.Activity_ID AS nvarchar(50)) =
              LEFT(r.task_code, NULLIF(CHARINDEX('-', r.task_code), 0) - 1)
    LEFT JOIN dbo.activity_master_csv amc
           ON amc.activity_code = m.Old_Activity_Code
),
roll AS (
    SELECT well_id, wbs,
           COUNT(*)                                                   AS activities,
           SUM(CASE WHEN actual_progress >= 1 THEN 1 ELSE 0 END)     AS activities_done,
           CAST(SUM(actual_progress) AS float) / NULLIF(COUNT(*), 0)   AS wbs_pct_computed,
           MIN(actual_progress)                                        AS lowest_activity_pct
    FROM act
    WHERE wbs IS NOT NULL
    GROUP BY well_id, wbs
)
SELECT CONCAT(CAST(r.well_id AS varchar(20)), ' | ', r.wbs)   AS entity_key,
       CAST(r.wbs AS nvarchar(100))                           AS entity_label,
       CAST(1 - r.wbs_pct_computed AS float)                AS severity_value,
       r.well_id                                              AS evidence_well_id,
       r.activities                                           AS evidence_activities,
       r.activities_done                                      AS evidence_activities_at_100,
       CAST(r.wbs_pct_computed AS decimal(18,4))              AS evidence_wbs_pct_computed,
       r.lowest_activity_pct                                  AS evidence_lowest_activity_pct,
       CONCAT('All ', r.activities, ' activities report 100% but the WBS computes to ',
              CAST(CAST(r.wbs_pct_computed AS decimal(18,2)) AS varchar(20)), '%')
                                                              AS explain_text
FROM roll r
WHERE r.activities > 0
  AND r.activities_done = r.activities
  AND r.wbs_pct_computed < 1 - {{epsilon}}
ORDER BY severity_value DESC;
```

---

## RULE DQ-004 - WBS weightage or rollup disagrees with recorded well progress

- category: Rollup consistency
- severity: critical
- entity: well
- method: rollup
- sql_mode: seed
- status: active
- epsilon: 0.01
- tags: wbs, pms, weightage, progress

**What is wrong**
Two related defects, both of which corrupt the overall progress percentage of a construction
project:

1. **Weightage total is wrong** — a well's PMS weightages do not add up to the same total as
   every other well's.
2. **Rollup disagrees** — the weighted progress recomputed from the activities does not match
   the progress actually recorded against the well.

**Why it matters**
Overall progress is the number the client sees. If the weights are wrong, or the stored figure
was not recalculated after the parts changed, every report built on it is wrong — and nothing
at the activity level looks out of place.

**How to detect**
Recompute `SUM(pms * actual_progress) / SUM(pms)` per well and compare against the latest
recorded `well_progress.overall_progress`. Two thresholds, of two different kinds:

- The weightage total is **self-calibrating** — the correct total is not declared anywhere, so
  it is taken to be the population median and a well is flagged when it sits beyond two
  standard deviations of that. Where every well agrees the deviation is zero, so any
  disagreement at all is flagged, which is the desired behaviour.
- The rollup gap uses `epsilon` only, because the correct gap is **definitionally zero** — a
  recomputed total and a stored total should be equal, so the only allowance needed is for
  floating-point noise.

⚠ **Scale — MEASURED, do not re-derive.** NUMERIC HINTS reports `core.revenue.actual_progress`
and `well.well_progress.overall_progress` BOTH as 0-1 fractions, so they are compared directly
with no rescaling. Note `actual_percent` is a different thing entirely: a 0-0.14 column holding
the activity's WEIGHTED CONTRIBUTION (roughly `pms x actual_progress`), not its completion.
Using it as a completion figure is wrong in both directions — `< 100` flags every row and
`>= 100` flags none. Re-read NUMERIC HINTS before changing any comparison here.

**Do NOT flag**
Wells with no activity rows or no recorded progress — there is nothing to reconcile, and
"missing" is a different finding from "inconsistent".

```sql summary
WITH per_well AS (
    SELECT r.well_id,
           SUM(CAST(r.pms AS float))                                            AS pms_total,
           SUM(CAST(r.pms AS float) * CAST(r.actual_progress AS float))
               / NULLIF(SUM(CAST(r.pms AS float)), 0)                           AS computed_pct
    FROM core.revenue r
    WHERE r.pms IS NOT NULL
    GROUP BY r.well_id
),
latest AS (
    SELECT well_id, CAST(overall_progress AS float) AS overall_progress
    FROM (
        SELECT well_id, overall_progress,
               ROW_NUMBER() OVER (PARTITION BY well_id ORDER BY week_number DESC) AS rn
        FROM well.well_progress
    ) w
    WHERE rn = 1
),
norm AS (
    SELECT DISTINCT
           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pms_total) OVER () AS median_total
    FROM per_well
    WHERE pms_total IS NOT NULL
),
spread AS (
    SELECT ISNULL(STDEV(pms_total), 0) AS sd_total FROM per_well
),
scoped AS (
    SELECT CASE
             WHEN ABS(p.pms_total - n.median_total) > 2 * s.sd_total
               OR ABS(p.computed_pct - l.overall_progress) > {{epsilon}}
             THEN 1 ELSE 0 END                                     AS is_anomaly,
           ABS(p.computed_pct - l.overall_progress)        AS sev
    FROM per_well p
    JOIN latest l ON l.well_id = p.well_id
    CROSS JOIN norm n
    CROSS JOIN spread s
    WHERE p.pms_total > 0 AND l.overall_progress IS NOT NULL
)
SELECT '{{rule_id}}'                                                      AS rule_id,
       COUNT(*)                                                           AS scope_total,
       SUM(is_anomaly)                                                    AS anomaly_count,
       CAST(100.0 * SUM(is_anomaly) / NULLIF(COUNT(*),0) AS decimal(9,4)) AS anomaly_pct,
       MAX(CASE WHEN is_anomaly = 1 THEN sev END)                         AS worst_severity_val
FROM scoped;
```

```sql detail
WITH per_well AS (
    SELECT r.well_id,
           COUNT(*)                                                            AS activities,
           SUM(CAST(r.pms AS float))                                           AS pms_total,
           SUM(CAST(r.pms AS float) * CAST(r.actual_progress AS float))
               / NULLIF(SUM(CAST(r.pms AS float)), 0)                          AS computed_pct
    FROM core.revenue r
    WHERE r.pms IS NOT NULL
    GROUP BY r.well_id
),
latest AS (
    SELECT well_id, overall_progress, week_number
    FROM (
        SELECT well_id, CAST(overall_progress AS float) AS overall_progress, week_number,
               ROW_NUMBER() OVER (PARTITION BY well_id ORDER BY week_number DESC) AS rn
        FROM well.well_progress
    ) w
    WHERE rn = 1
),
norm AS (
    SELECT DISTINCT
           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pms_total) OVER () AS median_total
    FROM per_well
    WHERE pms_total IS NOT NULL
),
spread AS (
    SELECT ISNULL(STDEV(pms_total), 0) AS sd_total FROM per_well
)
SELECT p.well_id                                                     AS entity_key,
       CAST(wm.ramz_id AS nvarchar(100))                             AS entity_label,
       CAST(ABS(p.computed_pct - l.overall_progress) AS float) AS severity_value,
       p.activities                                                  AS evidence_activities,
       CAST(p.pms_total AS decimal(18,4))                            AS evidence_pms_total,
       CAST(n.median_total AS decimal(18,4))                         AS evidence_expected_pms_total,
       CAST(p.computed_pct AS decimal(18,4))                         AS evidence_computed_progress_pct,
       CAST(l.overall_progress AS decimal(18,4))             AS evidence_recorded_progress_pct,
       l.week_number                                                 AS evidence_progress_week,
       CONCAT('Recomputed progress ',
              CAST(CAST(p.computed_pct AS decimal(18,2)) AS varchar(20)),
              '% vs recorded ',
              CAST(CAST(l.overall_progress AS decimal(18,2)) AS varchar(20)),
              '%; PMS weightage totals ',
              CAST(CAST(p.pms_total AS decimal(18,2)) AS varchar(20)),
              ' against a population median of ',
              CAST(CAST(n.median_total AS decimal(18,2)) AS varchar(20)))
                                                                     AS explain_text
FROM per_well p
JOIN latest l ON l.well_id = p.well_id
LEFT JOIN well.well_master wm ON wm.well_id = p.well_id
CROSS JOIN norm n
CROSS JOIN spread s
WHERE p.pms_total > 0
  AND l.overall_progress IS NOT NULL
  AND (ABS(p.pms_total - n.median_total) > 2 * s.sd_total
       OR ABS(p.computed_pct - l.overall_progress) > {{epsilon}})
ORDER BY severity_value DESC;
```
