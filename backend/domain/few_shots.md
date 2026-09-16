WORKED PROBE EXAMPLES (an anomaly stated in business language -> the two queries that detect it).

These are PATTERNS, not a schema reference. The SCHEMA block is the only authority on which
tables and columns exist: where an example disagrees with it, the SCHEMA block wins. Table and
column names below are placeholders in angle brackets, never real names — copy the SHAPE, then
resolve every identifier against the schema you were given.

Each example exists to show one trap that is easy to fall into and expensive to get wrong.

Each declares `- applies:` under its heading, which decides when it is sent: `always`, or one
of `scale` (a number in scope has measured statistics), `grain` (something in scope keeps more
than one record per thing), `threshold` (the rule is statistical or self-calibrating), `nulls`
(a column in scope is substantially empty). Anything else is treated as `always`. When in
doubt write `always` — see app/graph/context.py for why this is declared rather than guessed.

---

## 1. Scope is what you EXAMINED, not what you flagged

- applies: always

Anomaly: "a record that has reached a milestone but is missing the date that proves it".

```sql
-- SUMMARY
WITH scoped AS (
    SELECT e.<entity_key>,
           CASE WHEN e.<proof_date> IS NULL THEN 1 ELSE 0 END AS is_anomaly,
           CAST(1 AS float)                                   AS sev
    FROM <entity_table> e
    WHERE e.<milestone_reached_flag> IS NOT NULL      -- SCOPE: what makes a record eligible
)
SELECT 'RULE-ID'                                                   AS rule_id,
       COUNT(*)                                                    AS scope_total,
       SUM(is_anomaly)                                             AS anomaly_count,
       CAST(100.0 * SUM(is_anomaly) / NULLIF(COUNT(*),0) AS decimal(9,4)) AS anomaly_pct,
       MAX(CASE WHEN is_anomaly = 1 THEN sev END)                  AS worst_severity_val
FROM scoped;
```

⚠ The WHERE clause carries the SCOPE condition only. Putting the anomaly condition there too
makes `scope_total` equal `anomaly_count`, so the percentage is always 100% and nobody can tell
whether two findings out of five or two out of fifty thousand were found.

---

## 2. A missing value past its deadline IS the finding — never filter it out

- applies: always

Anomaly: "a milestone document was not issued by its deadline, where the deadline is a fixed
number of days before a planned date".

```sql
-- SUMMARY
WITH scoped AS (
    SELECT e.<entity_key>,
           DATEADD(day, -<deadline_days>, e.<planned_date>) AS deadline,
           e.<issue_date>
    FROM <entity_table> e
    WHERE e.<planned_date> IS NOT NULL                -- a deadline must be computable
),
judged AS (
    SELECT s.*,
           CASE
             -- issued, but late
             WHEN s.<issue_date> IS NOT NULL AND s.<issue_date> > s.deadline THEN 1
             -- never issued, and the deadline has already passed
             WHEN s.<issue_date> IS NULL AND CAST(GETDATE() AS date) > s.deadline THEN 1
             ELSE 0
           END AS is_anomaly,
           CASE
             WHEN s.<issue_date> IS NOT NULL THEN DATEDIFF(day, s.deadline, s.<issue_date>)
             ELSE DATEDIFF(day, s.deadline, CAST(GETDATE() AS date))
           END AS days_late
    FROM scoped s
)
SELECT 'RULE-ID'                                                   AS rule_id,
       COUNT(*)                                                    AS scope_total,
       SUM(is_anomaly)                                             AS anomaly_count,
       CAST(100.0 * SUM(is_anomaly) / NULLIF(COUNT(*),0) AS decimal(9,4)) AS anomaly_pct,
       MAX(CASE WHEN is_anomaly = 1 THEN CAST(days_late AS float) END) AS worst_severity_val
FROM judged;
```

⚠ Adding `AND <issue_date> IS NOT NULL` removes exactly the worst offenders — the records where
the document was never issued at all. A NULL past its deadline is a miss, not an unknown.

---

## 3. Read the measured SCALE before comparing a proportion

- applies: scale

Anomaly: "a task reports full completion but is not marked complete".

NUMERIC HINTS states the scale of every numeric column. A progress column may be a 0-1 fraction
or a 0-100 percentage, and the declared type never says which.

```sql
-- when NUMERIC HINTS says FRACTION_1 (0-1):
WHERE t.<progress> >= 1.0 AND t.<complete_flag> = 0

-- when it says PERCENT_100 (0-100):
WHERE t.<progress> >= 100 AND t.<complete_flag> = 0
```

⚠ `>= 1.0` against a 0-100 column flags almost every row; `>= 100` against a 0-1 column flags
none. Both look like working queries and neither fails. Use `>=` rather than `=`, so a
recorded value above full still counts as complete.

---

## 4. Collapse history to one current record before judging an entity

- applies: grain

Anomaly: "a record's current state is self-contradictory", in a table that keeps one row per
update.

```sql
-- DETAIL
WITH current_row AS (
    SELECT t.*,
           ROW_NUMBER() OVER (PARTITION BY t.<entity_key>
                              ORDER BY t.<updated_at> DESC) AS rn
    FROM <history_table> t
)
SELECT c.<entity_key>                       AS entity_key,
       CAST(c.<label> AS nvarchar(200))     AS entity_label,
       CAST(1 AS float)                     AS severity_value,
       c.<field_a>                          AS evidence_field_a,
       c.<field_b>                          AS evidence_field_b,
       CONCAT('<field_a> says ', CAST(c.<field_a> AS nvarchar(50)),
              ' while <field_b> says ', CAST(c.<field_b> AS nvarchar(50)))
                                            AS explain_text
FROM current_row c
WHERE c.rn = 1
  AND <the contradiction>
ORDER BY severity_value DESC;
```

⚠ The SCHEMA block marks such a table `MANY ROWS PER <key>`. Querying it without collapsing to
one row per entity counts the same entity many times, so `anomaly_count` exceeds the number of
real entities — and the headline figure in the report is simply wrong.

---

## 5. A threshold must come from the data or from the rule — never from nowhere

- applies: threshold

Anomaly: "a measured value is far outside what is normal for its population".

```sql
-- DETAIL
WITH observed AS (
    SELECT t.<entity_key>, t.<group_key>, CAST(t.<measure> AS float) AS measure
    FROM <table> t
    WHERE t.<measure> IS NOT NULL
),
stats AS (
    -- per GROUP, not across the whole table: a norm for one activity says nothing about another
    SELECT <group_key>,
           AVG(measure) AS mean_val,
           STDEV(measure) AS sd_val,
           COUNT(*) AS n_obs
    FROM observed GROUP BY <group_key>
)
SELECT o.<entity_key>                                   AS entity_key,
       CAST(o.<group_key> AS nvarchar(200))             AS entity_label,
       ABS(o.measure - s.mean_val)                      AS severity_value,
       o.measure                                        AS evidence_value,
       CAST(s.mean_val AS decimal(18,4))                AS evidence_group_average,
       CAST(s.mean_val + 2 * s.sd_val AS decimal(18,4)) AS evidence_threshold,
       s.n_obs                                          AS evidence_sample_size,
       CONCAT('Value ', CAST(CAST(o.measure AS decimal(18,2)) AS varchar(20)),
              ' against a group average of ',
              CAST(CAST(s.mean_val AS decimal(18,2)) AS varchar(20)))  AS explain_text
FROM observed o
JOIN stats s ON s.<group_key> = o.<group_key>
WHERE s.n_obs >= 30                                  -- minimum sample: state this in the note
  AND s.sd_val > 0                                   -- no spread means no outliers
  AND ABS(o.measure - s.mean_val) > 2 * s.sd_val     -- the threshold IS the data
ORDER BY severity_value DESC;
```

⚠ Two separate traps. Computing the statistics across the whole table rather than per group
compares unrelated things. And a bare constant with no stated source is rejected — a threshold
must be derived from the data as above, or taken from a value the rule itself declares.

---

## 6. Keep unmatched records visible when absence is the finding

- applies: always

Anomaly: "a record cannot be resolved through its mapping".

```sql
-- SUMMARY
WITH scoped AS (
    SELECT t.<entity_key>,
           CASE WHEN m.<mapped_key> IS NULL THEN 1 ELSE 0 END AS is_anomaly
    FROM <fact_table> t
    LEFT JOIN <mapping_table> m ON m.<mapped_key> = t.<lookup_key>
)
SELECT 'RULE-ID' AS rule_id, COUNT(*) AS scope_total, SUM(is_anomaly) AS anomaly_count,
       CAST(100.0 * SUM(is_anomaly) / NULLIF(COUNT(*),0) AS decimal(9,4)) AS anomaly_pct,
       CAST(SUM(is_anomaly) AS float) AS worst_severity_val
FROM scoped;
```

⚠ An inner join here drops every unmapped record, so the count of unmapped records reads zero —
the query reports perfection precisely because the problem exists. The same applies to a filter
placed in the WHERE clause of a LEFT JOIN: it silently turns the join back into an inner one.
Put such a condition in the `ON` clause instead.

---

## 7. A column that is mostly NULL cannot be joined as though it were populated

- applies: nulls

Anomaly: "a record of a particular classification is in the wrong lifecycle state".

NUMERIC HINTS reports a null rate for every column. When a classification column is, say, 71%
NULL, requiring a match discards most of the table and the probe silently reports on the
remaining fraction as if it were everything.

```sql
-- keep unclassified records IN SCOPE; their missing classification is itself a gap
FROM <fact_table> f
LEFT JOIN <lookup_table> l ON l.<id> = f.<classification_id>
WHERE l.<name> IN ('<value_a>', '<value_b>') OR f.<classification_id> IS NULL
```

⚠ Name the values that qualify, rather than excluding the ones that do not. A value added to
the lookup table later must not silently start being treated as qualifying.

---

## 8. Divide safely, and never let a NULL become a zero

- applies: always

```sql
-- a probe runs unattended, so a divide-by-zero is a failed rule, not a visible error
SUM(<weight> * <value>) / NULLIF(SUM(<weight>), 0)   AS weighted_average
```

⚠ `ISNULL(x, 0)` is correct only where the business rule says an absent value counts as zero.
Everywhere else a NULL means "unknown", and turning it into a zero invents data — a missing
progress figure is not zero progress, and a missing date is not the epoch.

---

## 9. Prove the finding in the row itself

- applies: always

Every DETAIL row must let a reader verify the finding without re-running anything.

```sql
SELECT e.<entity_key>                       AS entity_key,
       CAST(e.<name> AS nvarchar(200))      AS entity_label,
       CAST(<how_bad> AS float)             AS severity_value,
       e.<date_a>                           AS evidence_first_date,
       e.<date_b>                           AS evidence_second_date,
       DATEDIFF(day, e.<date_a>, e.<date_b>) AS evidence_gap_days,
       CONCAT('<date_b> (', CONVERT(varchar(10), e.<date_b>, 23),
              ') precedes <date_a> (', CONVERT(varchar(10), e.<date_a>, 23), ')')
                                            AS explain_text
FROM <entity_table> e
WHERE <the condition>
ORDER BY severity_value DESC;
```

⚠ `explain_text` is read straight into the report, and near-identical sentences are grouped and
counted there. Write it as one complete sentence naming the actual values, so a reader who never
opens the database still understands what is wrong with that record.

⚠ `ORDER BY severity_value DESC` is required. The runner caps how many rows it keeps, so without
an explicit worst-first ordering the report shows an arbitrary sample instead of the records
that matter most.
