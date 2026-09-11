# GENERIC PROBES

Structural anomaly templates. These name **no business table or column** — every identifier is
a `{{placeholder}}` that `app/rules/generic.py` fills in from `schema.txt` and
`numeric_hints.txt`.

They use **no LLM at all**. One template becomes as many probes as the schema has matching
features: one per declared foreign key, one per grain warning, one per date pair, and so on.
On a database with 35 foreign keys, `GEN-FK` alone yields 35 working probes for zero tokens.

They run **first** in a detection run, and the set of things they cover is handed to the SQL
Author as *already covered — do not re-express*. That is how "sequenced after the deterministic
rules, not duplicating them" is enforced mechanically rather than by hoping a prompt behaves.

## How a template is expanded

`applies_to` names the schema feature to iterate, which determines the placeholders available:

| `applies_to` | Iterates | Placeholders |
|---|---|---|
| `foreign_key` | every `FK:` line in schema.txt | `child_table` `child_column` `parent_table` `parent_column` `entity_column` |
| `duplicate_key` | every `MANY ROWS PER` marker | `table` `key_column` |
| `date_pair` | date columns pairing as start/end, on/off, issue/complete | `table` `start_column` `end_column` `entity_column` |
| `future_date` | date columns holding actuals (not `ex_*`, `target_*`, `planned_*`) | `table` `column` `entity_column` |
| `numeric_range` | numeric columns with a measured scale in numeric_hints.txt | `table` `column` `lower_bound` `upper_bound` `entity_column` |
| `text_numeric` | text columns whose values should parse as numbers | `table` `column` `entity_column` |

`entity_column` resolves to the table's single-column primary key, else its first `*_id`
column, else a literal row number — so `entity_key` is always populated.

`{{rule_id}}` is generated as `GEN-<FAMILY>-<n>` and is unique per expansion.

Each template must satisfy the same SUMMARY/DETAIL contract as a declared rule. See the
contract table in `data_anomalies.md`.

---

## TEMPLATE GEN-FK - Orphan child rows

- applies_to: foreign_key
- category: Referential integrity
- severity: high
- entity: row
- method: rule

**What is wrong**
A child row holds a foreign-key value that has no matching parent. The relationship is
*declared* in the database, so this should be impossible — where it happens, the constraint is
untrusted or was added after the bad data.

**Why it matters**
Every join through this key silently drops the orphan, so it vanishes from reports rather than
appearing as an error.

**How to detect**
`LEFT JOIN` the parent and keep the rows where the parent side is NULL while the child value is
not. No threshold: one orphan is a defect.

```sql summary
WITH scoped AS (
    SELECT CASE WHEN p.[{{parent_column}}] IS NULL THEN 1 ELSE 0 END AS is_anomaly
    FROM {{child_table}} c
    LEFT JOIN {{parent_table}} p ON p.[{{parent_column}}] = c.[{{child_column}}]
    WHERE c.[{{child_column}}] IS NOT NULL
)
SELECT '{{rule_id}}'                                                      AS rule_id,
       COUNT(*)                                                           AS scope_total,
       SUM(is_anomaly)                                                    AS anomaly_count,
       CAST(100.0 * SUM(is_anomaly) / NULLIF(COUNT(*),0) AS decimal(9,4)) AS anomaly_pct,
       CAST(SUM(is_anomaly) AS float)                                     AS worst_severity_val
FROM scoped;
```

```sql detail
SELECT CAST(c.[{{entity_column}}] AS nvarchar(200))     AS entity_key,
       CAST(c.[{{child_column}}] AS nvarchar(200))      AS entity_label,
       CAST(1 AS float)                                 AS severity_value,
       c.[{{child_column}}]                             AS evidence_orphan_value,
       CONCAT('{{child_table}}.{{child_column}} = ''',
              CAST(c.[{{child_column}}] AS nvarchar(200)),
              ''' has no matching row in {{parent_table}}.{{parent_column}}')
                                                        AS explain_text
FROM {{child_table}} c
LEFT JOIN {{parent_table}} p ON p.[{{parent_column}}] = c.[{{child_column}}]
WHERE c.[{{child_column}}] IS NOT NULL
  AND p.[{{parent_column}}] IS NULL
ORDER BY severity_value DESC;
```

---

## TEMPLATE GEN-DUP - Repeated key in a table expected to be one row per entity

- applies_to: duplicate_key
- category: Grain integrity
- severity: high
- entity: row
- method: rule

**What is wrong**
A key that identifies an entity appears on more than one row.

**Why it matters**
Any count, sum or average over this table double-counts the duplicated entity. This is the
single most damaging silent error in this kind of database, because the query runs perfectly
and simply returns a number that is too big.

**How to detect**
Group by the key and keep groups of more than one. No threshold. Where duplication is
*legitimate* (a history or snapshot table), disable the generated rule rather than widening
the condition — the grain is a property of the table, not of this check.

```sql summary
WITH grouped AS (
    SELECT [{{key_column}}] AS k, COUNT(*) AS n
    FROM {{table}}
    WHERE [{{key_column}}] IS NOT NULL
    GROUP BY [{{key_column}}]
)
SELECT '{{rule_id}}'                                                      AS rule_id,
       COUNT(*)                                                           AS scope_total,
       SUM(CASE WHEN n > 1 THEN 1 ELSE 0 END)                             AS anomaly_count,
       CAST(100.0 * SUM(CASE WHEN n > 1 THEN 1 ELSE 0 END)
            / NULLIF(COUNT(*),0) AS decimal(9,4))                         AS anomaly_pct,
       CAST(MAX(CASE WHEN n > 1 THEN n END) AS float)                     AS worst_severity_val
FROM grouped;
```

```sql detail
SELECT CAST([{{key_column}}] AS nvarchar(200))          AS entity_key,
       CAST([{{key_column}}] AS nvarchar(200))          AS entity_label,
       CAST(COUNT(*) AS float)                          AS severity_value,
       COUNT(*)                                         AS evidence_row_count,
       CONCAT('{{table}} holds ', COUNT(*),
              ' rows for {{key_column}} = ',
              CAST([{{key_column}}] AS nvarchar(200)),
              ' - every count over this table inflates it')
                                                        AS explain_text
FROM {{table}}
WHERE [{{key_column}}] IS NOT NULL
GROUP BY [{{key_column}}]
HAVING COUNT(*) > 1
ORDER BY severity_value DESC;
```

---

## TEMPLATE GEN-DATE - End date precedes start date

- applies_to: date_pair
- category: Date integrity
- severity: high
- entity: row
- method: rule

**What is wrong**
A paired end date falls before its start date.

**Why it matters**
Every duration derived from the pair is negative, which silently corrupts averages and totals
rather than failing.

**How to detect**
Compare the two directly. Both must be present — a missing date is a different finding. No
threshold: a negative duration is impossible, not merely unusual.

```sql summary
WITH scoped AS (
    SELECT CASE WHEN [{{end_column}}] < [{{start_column}}] THEN 1 ELSE 0 END AS is_anomaly,
           CAST(ABS(DATEDIFF(day, [{{start_column}}], [{{end_column}}])) AS float) AS sev
    FROM {{table}}
    WHERE [{{start_column}}] IS NOT NULL AND [{{end_column}}] IS NOT NULL
)
SELECT '{{rule_id}}'                                                      AS rule_id,
       COUNT(*)                                                           AS scope_total,
       SUM(is_anomaly)                                                    AS anomaly_count,
       CAST(100.0 * SUM(is_anomaly) / NULLIF(COUNT(*),0) AS decimal(9,4)) AS anomaly_pct,
       MAX(CASE WHEN is_anomaly = 1 THEN sev END)                         AS worst_severity_val
FROM scoped;
```

```sql detail
SELECT CAST([{{entity_column}}] AS nvarchar(200))                       AS entity_key,
       CAST([{{entity_column}}] AS nvarchar(200))                       AS entity_label,
       CAST(DATEDIFF(day, [{{end_column}}], [{{start_column}}]) AS float) AS severity_value,
       [{{start_column}}]                                               AS evidence_start_date,
       [{{end_column}}]                                                 AS evidence_end_date,
       DATEDIFF(day, [{{start_column}}], [{{end_column}}])              AS evidence_duration_days,
       CONCAT('{{end_column}} (', CONVERT(varchar(10), [{{end_column}}], 23),
              ') is ', DATEDIFF(day, [{{end_column}}], [{{start_column}}]),
              ' day(s) BEFORE {{start_column}} (',
              CONVERT(varchar(10), [{{start_column}}], 23), ')')         AS explain_text
FROM {{table}}
WHERE [{{start_column}}] IS NOT NULL
  AND [{{end_column}}] IS NOT NULL
  AND [{{end_column}}] < [{{start_column}}]
ORDER BY severity_value DESC;
```

---

## TEMPLATE GEN-FUT - Actual date recorded in the future

- applies_to: future_date
- category: Date integrity
- severity: medium
- entity: row
- method: rule

**What is wrong**
A column recording something that has *already happened* holds a date later than today.

**Why it matters**
Usually a typo in the year. It makes completed work look outstanding, or pulls a forecast
years out.

**How to detect**
Compare against `CAST(GETDATE() AS date)`. Applied only to columns holding actuals — planned
and expected dates are *supposed* to be in the future and are excluded when the probes are
generated, not filtered out here.

```sql summary
WITH scoped AS (
    SELECT CASE WHEN [{{column}}] > CAST(GETDATE() AS date) THEN 1 ELSE 0 END AS is_anomaly,
           CAST(DATEDIFF(day, CAST(GETDATE() AS date), [{{column}}]) AS float) AS sev
    FROM {{table}}
    WHERE [{{column}}] IS NOT NULL
)
SELECT '{{rule_id}}'                                                      AS rule_id,
       COUNT(*)                                                           AS scope_total,
       SUM(is_anomaly)                                                    AS anomaly_count,
       CAST(100.0 * SUM(is_anomaly) / NULLIF(COUNT(*),0) AS decimal(9,4)) AS anomaly_pct,
       MAX(CASE WHEN is_anomaly = 1 THEN sev END)                         AS worst_severity_val
FROM scoped;
```

```sql detail
SELECT CAST([{{entity_column}}] AS nvarchar(200))                          AS entity_key,
       CAST([{{entity_column}}] AS nvarchar(200))                          AS entity_label,
       CAST(DATEDIFF(day, CAST(GETDATE() AS date), [{{column}}]) AS float) AS severity_value,
       [{{column}}]                                                       AS evidence_recorded_date,
       CAST(GETDATE() AS date)                                            AS evidence_today,
       CONCAT('{{column}} is ',
              DATEDIFF(day, CAST(GETDATE() AS date), [{{column}}]),
              ' day(s) in the future (',
              CONVERT(varchar(10), [{{column}}], 23),
              ') but records something that has already happened')        AS explain_text
FROM {{table}}
WHERE [{{column}}] IS NOT NULL
  AND [{{column}}] > CAST(GETDATE() AS date)
ORDER BY severity_value DESC;
```

---

## TEMPLATE GEN-RANGE - Numeric value outside its measured bounds

- applies_to: numeric_range
- category: Value range
- severity: medium
- entity: row
- method: rule

**What is wrong**
A value falls outside the range its column is supposed to occupy — most often a percentage
above 100 or below 0.

**Why it matters**
An out-of-range progress figure propagates into every rollup that averages or weights it.

**How to detect**
Compare against bounds taken from `numeric_hints.txt`, which measured the column's real scale.
The bounds are **derived, not invented**: a 0-1 fraction column is checked against `[0,1]` and
a 0-100 percentage against `[0,100]`, decided by what the data actually holds rather than by
the declared type, which says nothing about range.

```sql summary
WITH scoped AS (
    SELECT CASE WHEN CAST([{{column}}] AS float) < {{lower_bound}}
                   OR CAST([{{column}}] AS float) > {{upper_bound}}
                THEN 1 ELSE 0 END AS is_anomaly,
           CASE WHEN CAST([{{column}}] AS float) > {{upper_bound}}
                THEN CAST([{{column}}] AS float) - {{upper_bound}}
                ELSE {{lower_bound}} - CAST([{{column}}] AS float)
           END AS sev
    FROM {{table}}
    WHERE [{{column}}] IS NOT NULL
)
SELECT '{{rule_id}}'                                                      AS rule_id,
       COUNT(*)                                                           AS scope_total,
       SUM(is_anomaly)                                                    AS anomaly_count,
       CAST(100.0 * SUM(is_anomaly) / NULLIF(COUNT(*),0) AS decimal(9,4)) AS anomaly_pct,
       MAX(CASE WHEN is_anomaly = 1 THEN sev END)                         AS worst_severity_val
FROM scoped;
```

```sql detail
SELECT CAST([{{entity_column}}] AS nvarchar(200))       AS entity_key,
       CAST([{{entity_column}}] AS nvarchar(200))       AS entity_label,
       CASE WHEN CAST([{{column}}] AS float) > {{upper_bound}}
            THEN CAST([{{column}}] AS float) - {{upper_bound}}
            ELSE {{lower_bound}} - CAST([{{column}}] AS float)
       END                                              AS severity_value,
       [{{column}}]                                     AS evidence_value,
       CAST({{lower_bound}} AS float)                   AS evidence_lower_bound,
       CAST({{upper_bound}} AS float)                   AS evidence_upper_bound,
       CONCAT('{{column}} = ', CAST([{{column}}] AS nvarchar(50)),
              ' is outside the measured range {{lower_bound}} to {{upper_bound}}')
                                                        AS explain_text
FROM {{table}}
WHERE [{{column}}] IS NOT NULL
  AND (CAST([{{column}}] AS float) < {{lower_bound}}
       OR CAST([{{column}}] AS float) > {{upper_bound}})
ORDER BY severity_value DESC;
```

---

## TEMPLATE GEN-CAST - Text column holding a value that is not a number

- applies_to: text_numeric
- category: Type integrity
- severity: medium
- entity: row
- method: rule

**What is wrong**
A column storing a quantity as text holds a value that cannot be read as a number.

**Why it matters**
Every calculation on the column has to `TRY_CAST`, and `TRY_CAST` turns the bad value into
NULL — so the row is silently dropped from the calculation instead of failing it. The defect
is invisible precisely because the safe cast hides it.

**How to detect**
`TRY_CAST` to float; a non-NULL, non-blank value that fails is the anomaly. No threshold.

```sql summary
WITH scoped AS (
    SELECT CASE WHEN TRY_CAST([{{column}}] AS float) IS NULL THEN 1 ELSE 0 END AS is_anomaly
    FROM {{table}}
    WHERE [{{column}}] IS NOT NULL AND LTRIM(RTRIM([{{column}}])) <> ''
)
SELECT '{{rule_id}}'                                                      AS rule_id,
       COUNT(*)                                                           AS scope_total,
       SUM(is_anomaly)                                                    AS anomaly_count,
       CAST(100.0 * SUM(is_anomaly) / NULLIF(COUNT(*),0) AS decimal(9,4)) AS anomaly_pct,
       CAST(SUM(is_anomaly) AS float)                                     AS worst_severity_val
FROM scoped;
```

```sql detail
SELECT CAST([{{entity_column}}] AS nvarchar(200))       AS entity_key,
       CAST([{{entity_column}}] AS nvarchar(200))       AS entity_label,
       CAST(1 AS float)                                 AS severity_value,
       [{{column}}]                                     AS evidence_raw_value,
       CONCAT('{{table}}.{{column}} = ''',
              CAST([{{column}}] AS nvarchar(200)),
              ''' does not parse as a number, so TRY_CAST silently drops this row from every ',
              'calculation on the column')              AS explain_text
FROM {{table}}
WHERE [{{column}}] IS NOT NULL
  AND LTRIM(RTRIM([{{column}}])) <> ''
  AND TRY_CAST([{{column}}] AS float) IS NULL
ORDER BY severity_value DESC;
```
