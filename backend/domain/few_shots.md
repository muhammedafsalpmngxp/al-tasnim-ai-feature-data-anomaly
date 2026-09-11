EXAMPLES (question -> correct T-SQL).

These are worked patterns, NOT a schema reference. The SCHEMA block above is the only authority on
which tables and columns exist: if an example here ever disagrees with it, the SCHEMA block wins.
(An earlier version of this file claimed every example had been run successfully against the
database. That guarantee could not survive the database being changed underneath it — columns were
renamed and a snapshot table became one-row-per-entity, leaving five examples referencing names
that no longer existed. Trust the SCHEMA block, not this note.)

De-duplicate ONLY where the SCHEMA block marks a table "⚠ MANY ROWS PER <key>". Do not add a
GROUP BY to a table that carries no such marker.

Q: How many wells are there?
```sql
SELECT COUNT(DISTINCT well_id) AS well_count
FROM well.well_master;
```

Q: How many wells are completed?
```sql
SELECT COUNT(DISTINCT well_id) AS completed_wells
FROM well.well_master
WHERE eng_completion_date IS NOT NULL;
```

Q: How many wells are there per project?
```sql
SELECT p.project_id, p.project_name, COUNT(DISTINCT wm.well_id) AS well_count
FROM well.well_master wm
JOIN project.project_mstr p ON wm.project_id = p.project_id
GROUP BY p.project_id, p.project_name
ORDER BY well_count DESC;
```
-- Join on the declared FK column (project_id), and select the real label column (project_name).
-- GROUP BY the KEY as well as the label: project names are NOT unique here (19 projects share 12
-- names), so grouping by name alone silently merges two different projects into one row. Group by
-- the identifier and carry the name along for display. This applies to any label column whose
-- table has a separate primary key.
-- COUNT(DISTINCT ...) is safe regardless of grain: no CTE, no ROW_NUMBER.

Q: List 5 wells with their rig-on and completion dates.
```sql
SELECT TOP 5
       well_id,
       rig_on_date,
       eng_completion_date
FROM well.well_master
ORDER BY well_id;
```
-- No GROUP BY: well.well_master carries no "MANY ROWS PER well_id" marker, so it is already one
-- row per well. Adding a GROUP BY here would be wrong-headed, not merely redundant.

Q: Show the latest overall progress for each well.
```sql
WITH latest_progress AS (
    SELECT well_id, overall_progress, week_number,
           ROW_NUMBER() OVER (PARTITION BY well_id ORDER BY week_number DESC) AS rn
    FROM well.well_progress
)
SELECT well_id, overall_progress, week_number
FROM latest_progress
WHERE rn = 1
ORDER BY overall_progress DESC;
```
-- well.well_progress IS the weekly table (many rows per well), so ROW_NUMBER() by week_number is
-- the right way to pick one row per well — and it names WHICH row was chosen, unlike MAX().

Q: How many wells are in each cluster?
```sql
SELECT c.cluster_name, COUNT(DISTINCT wm.well_id) AS well_count
FROM well.well_master wm
LEFT JOIN ref.cluster c ON wm.cluster_code = c.cluster_code
GROUP BY c.cluster_name
ORDER BY well_count DESC;
```
-- Follow the FK that actually exists: cluster_code is on well.well_master and points straight at
-- ref.cluster. Do not route this through project.project_mstr — it has no cluster column.
-- LEFT JOIN keeps wells not yet mapped to a cluster; they group under NULL. Report that unmapped
-- group rather than dropping it, so the total still reconciles.

Q: List 5 wells with their names.
```sql
WITH latest AS (
    SELECT well_id, well_name,
           ROW_NUMBER() OVER (PARTITION BY well_id ORDER BY week_number DESC) AS rn
    FROM well.well_progress
)
SELECT TOP 5 well_id, well_name
FROM latest
WHERE rn = 1
ORDER BY well_name;
```
-- Pick the table that HAS the column: well_name lives on well.well_progress, not on
-- well.well_master. Because that table is many-rows-per-well, take the latest row per well
-- explicitly instead of letting an arbitrary one through.

Q: List employees named Amal (or any variation like "whose name is Amal").
```sql
SELECT TOP 10
       id AS employee_id,
       emp_name
FROM ref.employee
WHERE LOWER(emp_name) = 'amal'
   OR LOWER(emp_name) LIKE 'amal %'
   OR LOWER(emp_name) LIKE '% amal %'
   OR LOWER(emp_name) LIKE '% amal'
ORDER BY emp_name;
```
-- When filtering by a specific person's name, DO NOT use a blanket `LIKE '%name%'`, as it incorrectly matches substrings (e.g. 'Jamal' when searching for 'Amal'). Always use exact word boundaries as shown above, regardless of how the user phrases the request.
