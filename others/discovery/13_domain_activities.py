"""Phase 13 - ACTIVITY domain: the full mapping chain, code formats, WBS vocabulary,
plus the reference-table columns phase 12 could not name."""
from dbx import connect, rows
cn = connect()


def show(t, sql, n=25):
    print(f"\n{'-'*112}\n{t}\n{'-'*112}")
    try:
        rs = rows(cn, sql)
    except Exception as e:  # noqa: BLE001
        print("   ERROR:", str(e)[:300]); return
    if not rs:
        print("   (no rows)"); return
    hdr = list(rs[0].keys())
    print("   " + " | ".join(hdr))
    for r in rs[:n]:
        print("   " + " | ".join(str(r[h])[:44] for h in hdr))
    if len(rs) > n:
        print(f"   ... {len(rs)-n} more")


print("#" * 112)
print("# FIXES FROM PHASE 12")
print("#" * 112)

show("actual columns of the small reference tables", """
SELECT s.name AS [schema], t.name AS [table], c.column_id AS ord, c.name AS [column], ty.name AS ty
FROM sys.columns c JOIN sys.objects t ON t.object_id=c.object_id
JOIN sys.schemas s ON s.schema_id=t.schema_id
JOIN sys.types ty ON ty.user_type_id=c.user_type_id
WHERE t.name IN ('well_status','well_category','well_function','well_completion_type','rig',
                 'station','well_type','well_location','cluster','uom','crew','crew_type')
ORDER BY s.name, t.name, c.column_id""", 60)

show("well_progress.well_name format patterns", """
SELECT pattern, COUNT(DISTINCT well_name) AS distinct_names, MIN(well_name) AS example FROM (
  SELECT well_name,
    CASE WHEN well_name IS NULL THEN '(NULL)'
         WHEN CHARINDEX('_',well_name)>0 AND CHARINDEX('-',well_name)>0 THEN 'has BOTH _ and -'
         WHEN CHARINDEX('_',well_name)>0 THEN 'underscore_separated'
         WHEN CHARINDEX('-',well_name)>0 THEN 'hyphen-separated'
         WHEN CHARINDEX(' ',well_name)>0 THEN 'contains SPACES'
         ELSE 'single token' END AS pattern
  FROM well.well_progress) x GROUP BY pattern ORDER BY distinct_names DESC""")

show("well_name hygiene: leading/trailing space, double space, mixed case duplicates", """
SELECT COUNT(DISTINCT well_name) AS distinct_names,
  COUNT(DISTINCT CASE WHEN well_name <> LTRIM(RTRIM(well_name)) THEN well_name END) AS untrimmed,
  COUNT(DISTINCT CASE WHEN CHARINDEX('  ',well_name)>0 THEN well_name END) AS double_space,
  COUNT(DISTINCT UPPER(LTRIM(RTRIM(well_name)))) AS distinct_after_normalise
FROM well.well_progress WHERE well_name IS NOT NULL""")

print("\n" + "#" * 112)
print("# ACTIVITY DOMAIN")
print("#" * 112)

show("task_code format: how many segments, and what is the suffix?", """
SELECT segments, COUNT(*) AS task_rows, COUNT(DISTINCT task_code) AS distinct_codes,
       MIN(task_code) AS example FROM (
  SELECT task_code, LEN(task_code)-LEN(REPLACE(task_code,'-',''))+1 AS segments
  FROM well.task_daily WHERE task_code IS NOT NULL) x
GROUP BY segments ORDER BY segments""")

show("task_code suffix == well_id?  (does the code agree with the well_id column)", """
WITH a AS (SELECT task_code, well_id,
             LEFT(task_code, NULLIF(CHARINDEX('-',task_code),0)-1) AS activity_id,
             SUBSTRING(task_code, CHARINDEX('-',task_code)+1, 50) AS rest
           FROM well.task_daily WHERE task_code IS NOT NULL AND well_id IS NOT NULL),
b AS (SELECT *, LEFT(rest, CASE WHEN CHARINDEX('-',rest)>0 THEN CHARINDEX('-',rest)-1 ELSE LEN(rest) END) AS suffix FROM a)
SELECT COUNT(*) AS rows_,
  SUM(CASE WHEN TRY_CAST(suffix AS int) IS NULL THEN 1 ELSE 0 END) AS suffix_not_numeric,
  SUM(CASE WHEN TRY_CAST(suffix AS int) = well_id THEN 1 ELSE 0 END) AS suffix_MATCHES_well_id,
  SUM(CASE WHEN TRY_CAST(suffix AS int) IS NOT NULL AND TRY_CAST(suffix AS int) <> well_id THEN 1 ELSE 0 END) AS suffix_MISMATCH
FROM b""")

show("activity_id format patterns (the prefix vocabulary)", """
SELECT prefix, COUNT(DISTINCT activity_id) AS distinct_activity_ids, COUNT(*) AS task_rows FROM (
  SELECT LEFT(task_code, NULLIF(CHARINDEX('-',task_code),0)-1) AS activity_id,
         LEFT(task_code, PATINDEX('%[0-9]%', task_code + '0')-1) AS prefix
  FROM well.task_daily WHERE task_code IS NOT NULL) x
GROUP BY prefix ORDER BY task_rows DESC""", 40)

show("MAPPING CHAIN health per activity prefix (where does hop 1 break?)", """
WITH a AS (SELECT LEFT(task_code, NULLIF(CHARINDEX('-',task_code),0)-1) AS activity_id, task_code, well_id
           FROM well.task_daily WHERE task_code IS NOT NULL)
SELECT LEFT(a.activity_id, PATINDEX('%[0-9]%', a.activity_id + '0')-1) AS prefix,
       COUNT(*) AS task_rows,
       SUM(CASE WHEN m.activity_id IS NULL THEN 1 ELSE 0 END) AS UNMAPPED_rows,
       CAST(100.0*SUM(CASE WHEN m.activity_id IS NULL THEN 1 ELSE 0 END)/COUNT(*) AS decimal(5,1)) AS unmapped_pct,
       COUNT(DISTINCT a.well_id) AS wells
FROM a LEFT JOIN dbo.activity_master_mapping m ON m.activity_id=a.activity_id
GROUP BY LEFT(a.activity_id, PATINDEX('%[0-9]%', a.activity_id + '0')-1)
ORDER BY UNMAPPED_rows DESC""", 40)

show("WBS vocabulary: the 37 activity_group_description values + activity counts", """
SELECT amc.descipline, amc.activity_group_description AS wbs,
       COUNT(*) AS activities_in_wbs,
       COUNT(DISTINCT amc.crew_code) AS crews
FROM dbo.activity_master_csv amc
GROUP BY amc.descipline, amc.activity_group_description
ORDER BY amc.descipline, activities_in_wbs DESC""", 45)

show("WBS reachable from real task work (vs the 37 defined)", """
WITH a AS (SELECT DISTINCT LEFT(task_code, NULLIF(CHARINDEX('-',task_code),0)-1) AS activity_id
           FROM well.task_daily WHERE task_code IS NOT NULL)
SELECT COUNT(DISTINCT amc.activity_group_description) AS wbs_reachable_from_tasks,
       (SELECT COUNT(DISTINCT activity_group_description) FROM dbo.activity_master_csv) AS wbs_defined
FROM a JOIN dbo.activity_master_mapping amm ON amm.activity_id=a.activity_id
       JOIN dbo.activity_master_csv amc ON amc.activity_code=amm.activity_code""")

show("activity_master_csv: the 2 all-NULL rows + crew_code coverage", """
SELECT SUM(CASE WHEN activity_code IS NULL THEN 1 ELSE 0 END) AS null_activity_code,
       SUM(CASE WHEN activity_group_description IS NULL THEN 1 ELSE 0 END) AS null_wbs,
       SUM(CASE WHEN crew_code IS NULL THEN 1 ELSE 0 END) AS null_crew,
       SUM(CASE WHEN sl_no IS NULL THEN 1 ELSE 0 END) AS null_sl_no,
       COUNT(*) AS rows_ FROM dbo.activity_master_csv""")

show("activity_master_csv.crew_code -> ref.crew / crew_type resolution", """
SELECT COUNT(DISTINCT amc.crew_code) AS distinct_crew_codes,
  COUNT(DISTINCT CASE WHEN c.crew_code IS NULL THEN amc.crew_code END) AS crew_codes_NOT_in_ref_crew
FROM dbo.activity_master_csv amc
LEFT JOIN ref.crew c ON c.crew_code = amc.crew_code
WHERE amc.crew_code IS NOT NULL""")

show("activity_master_mapping.project_type vocabulary", """
SELECT project_type, COUNT(*) AS n, COUNT(DISTINCT activity_id) AS activities,
       SUM(CASE WHEN activity_code IS NULL THEN 1 ELSE 0 END) AS unmapped_to_wbs
FROM dbo.activity_master_mapping GROUP BY project_type ORDER BY n DESC""")

show("norms sanity: activities with norms 0 or absurdly large", """
SELECT SUM(CASE WHEN norms IS NULL THEN 1 ELSE 0 END) AS norms_null,
       SUM(CASE WHEN norms = 0 THEN 1 ELSE 0 END) AS norms_zero,
       SUM(CASE WHEN norms < 0 THEN 1 ELSE 0 END) AS norms_negative,
       SUM(CASE WHEN norms > 100 THEN 1 ELSE 0 END) AS norms_over_100,
       MIN(norms) AS min_norm, MAX(norms) AS max_norm, COUNT(*) AS rows_
FROM dbo.activity_master_mapping""")

show("activity durations by activity_id - the statistical peer groups for norm checks", """
SELECT TOP 15 LEFT(t.task_code, NULLIF(CHARINDEX('-',t.task_code),0)-1) AS activity_id,
  COUNT(*) AS n,
  MIN(DATEDIFF(day,t.actual_start,t.actual_end)) AS dur_min,
  MAX(DATEDIFF(day,t.actual_start,t.actual_end)) AS dur_max,
  CAST(AVG(DATEDIFF(day,t.actual_start,t.actual_end)*1.0) AS decimal(8,2)) AS dur_avg,
  SUM(CASE WHEN DATEDIFF(day,t.actual_start,t.actual_end)<0 THEN 1 ELSE 0 END) AS NEGATIVE
FROM well.task_daily t
WHERE t.actual_start IS NOT NULL AND t.actual_end IS NOT NULL AND t.task_code IS NOT NULL
GROUP BY LEFT(t.task_code, NULLIF(CHARINDEX('-',t.task_code),0)-1)
HAVING COUNT(*) >= 30 ORDER BY COUNT(*) DESC""")

show("planned vs actual duration divergence (norm under/over calculation - user rule 2)", """
SELECT COUNT(*) AS comparable_rows,
 SUM(CASE WHEN DATEDIFF(day,target_start,target_end) = 0 THEN 1 ELSE 0 END) AS zero_len_plan,
 SUM(CASE WHEN DATEDIFF(day,actual_start,actual_end) > 3*NULLIF(DATEDIFF(day,target_start,target_end),0)
          THEN 1 ELSE 0 END) AS actual_over_3x_plan,
 SUM(CASE WHEN DATEDIFF(day,target_start,target_end) > 3*NULLIF(DATEDIFF(day,actual_start,actual_end),0)
          THEN 1 ELSE 0 END) AS plan_over_3x_actual,
 SUM(CASE WHEN duration IS NOT NULL AND ABS(duration - DATEDIFF(day,target_start,target_end)) > 1
          THEN 1 ELSE 0 END) AS duration_col_disagrees_with_target_dates
FROM well.task_daily
WHERE target_start IS NOT NULL AND target_end IS NOT NULL
  AND actual_start IS NOT NULL AND actual_end IS NOT NULL""")

show("rig overlap examples (fixed column names)", """
WITH d AS (SELECT well_id, rig_id, rig_on_date, rig_off_date FROM well.well_master
           WHERE rig_id IS NOT NULL AND rig_on_date IS NOT NULL AND rig_off_date IS NOT NULL
             AND rig_on_date <= rig_off_date)
SELECT TOP 12 a.rig_id, a.well_id AS well_a,
       CONVERT(varchar(10),a.rig_on_date,120) AS a_on, CONVERT(varchar(10),a.rig_off_date,120) AS a_off,
       b.well_id AS well_b,
       CONVERT(varchar(10),b.rig_on_date,120) AS b_on, CONVERT(varchar(10),b.rig_off_date,120) AS b_off,
       DATEDIFF(day, CASE WHEN a.rig_on_date>b.rig_on_date THEN a.rig_on_date ELSE b.rig_on_date END,
                     CASE WHEN a.rig_off_date<b.rig_off_date THEN a.rig_off_date ELSE b.rig_off_date END)+1 AS overlap_days
FROM d a JOIN d b ON a.rig_id=b.rig_id AND a.well_id<b.well_id
WHERE a.rig_on_date <= b.rig_off_date AND b.rig_on_date <= a.rig_off_date
ORDER BY overlap_days DESC""")

show("well reference vocabularies (fixed)", """
SELECT 'well_status' AS tbl, CAST(status_id AS varchar(10)) AS id, status AS val FROM well.well_status
UNION ALL SELECT 'rig', CAST(rig_id AS varchar(10)), rig FROM well.rig
ORDER BY tbl, id""", 40)
