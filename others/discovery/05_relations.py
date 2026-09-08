"""Phase 5 - grain, duplicates, orphans and cross-table coverage for the well spine."""
from dbx import connect, rows, scalar

cn = connect()

def show(title, sql):
    print(f"\n--- {title} ---")
    try:
        rs = rows(cn, sql)
    except Exception as e:
        print("   ERROR:", str(e)[:300]); return
    if not rs:
        print("   (no rows)"); return
    hdr = list(rs[0].keys())
    print("   " + " | ".join(f"{h}" for h in hdr))
    for r in rs[:25]:
        print("   " + " | ".join(str(r[h])[:46] for h in hdr))
    if len(rs) > 25:
        print(f"   ... {len(rs)-25} more")

print("="*100); print("GRAIN / DUPLICATES"); print("="*100)
show("well_master: rows vs distinct well_id (no PK!)", """
SELECT COUNT(*) AS rows_, COUNT(DISTINCT well_id) AS distinct_well_id,
       COUNT(*) - COUNT(DISTINCT well_id) AS dup_rows FROM well.well_master""")

show("well_master: duplicate ramz_id (well name)", """
SELECT ramz_id, COUNT(*) AS n FROM well.well_master
WHERE ramz_id IS NOT NULL GROUP BY ramz_id HAVING COUNT(*)>1 ORDER BY n DESC""")

show("well_progress: duplicate (well_id, week_number)", """
SELECT COUNT(*) AS dup_groups, SUM(n)-COUNT(*) AS extra_rows FROM (
  SELECT well_id, week_number, COUNT(*) n FROM well.well_progress
  GROUP BY well_id, week_number HAVING COUNT(*)>1) x""")

show("well_progress: week_number distribution head", """
SELECT TOP 8 week_number, COUNT(*) AS n, COUNT(DISTINCT well_id) AS wells
FROM well.well_progress GROUP BY week_number ORDER BY week_number""")

show("activity_master_mapping: duplicate activity_id", """
SELECT activity_id, COUNT(*) AS n, COUNT(DISTINCT activity_code) AS codes
FROM dbo.activity_master_mapping GROUP BY activity_id HAVING COUNT(*)>1""")

show("activity_master_csv: duplicate activity_code", """
SELECT activity_code, COUNT(*) AS n,
       COUNT(DISTINCT activity_group_description) AS wbs_variants
FROM dbo.activity_master_csv WHERE activity_code IS NOT NULL
GROUP BY activity_code HAVING COUNT(*)>1""")

print("\n"+"="*100); print("WELL SPINE COVERAGE (base = well.well_master, 814 wells)"); print("="*100)
show("wells present in each satellite table", """
SELECT 'well.well_progress' AS tbl, COUNT(DISTINCT p.well_id) AS wells_in_tbl,
       COUNT(DISTINCT CASE WHEN m.well_id IS NULL THEN p.well_id END) AS orphan_wells
FROM well.well_progress p LEFT JOIN well.well_master m ON m.well_id = p.well_id
UNION ALL SELECT 'well.task_daily', COUNT(DISTINCT t.well_id),
       COUNT(DISTINCT CASE WHEN m.well_id IS NULL THEN t.well_id END)
FROM well.task_daily t LEFT JOIN well.well_master m ON m.well_id = t.well_id
UNION ALL SELECT 'well.well_classification', COUNT(DISTINCT c.well_id),
       COUNT(DISTINCT CASE WHEN m.well_id IS NULL THEN c.well_id END)
FROM well.well_classification c LEFT JOIN well.well_master m ON m.well_id = c.well_id
UNION ALL SELECT 'dbo.engineering_well_priority', COUNT(DISTINCT w.well_id),
       COUNT(DISTINCT CASE WHEN m.well_id IS NULL THEN w.well_id END)
FROM dbo.engineering_well_priority w LEFT JOIN well.well_master m ON m.well_id = w.well_id""")

show("wells in master with NO tasks / NO progress", """
SELECT (SELECT COUNT(*) FROM well.well_master m
        WHERE NOT EXISTS (SELECT 1 FROM well.task_daily t WHERE t.well_id=m.well_id)) AS wells_without_tasks,
       (SELECT COUNT(*) FROM well.well_master m
        WHERE NOT EXISTS (SELECT 1 FROM well.well_progress p WHERE p.well_id=m.well_id)) AS wells_without_progress,
       (SELECT COUNT(*) FROM well.well_master m
        WHERE NOT EXISTS (SELECT 1 FROM well.well_classification c WHERE c.well_id=m.well_id)) AS wells_without_class""")

print("\n"+"="*100); print("TASK -> ACTIVITY -> WBS RESOLUTION (business rule s3)"); print("="*100)
show("task_daily task_code -> activity_id -> activity_code -> WBS", """
WITH a AS (SELECT task_code, well_id,
             LEFT(task_code, NULLIF(CHARINDEX('-', task_code),0)-1) AS activity_id
           FROM well.task_daily)
SELECT COUNT(*) AS task_rows,
       COUNT(DISTINCT a.task_code) AS distinct_task_codes,
       SUM(CASE WHEN a.activity_id IS NULL THEN 1 ELSE 0 END) AS no_dash_in_task_code,
       SUM(CASE WHEN amm.activity_id IS NULL THEN 1 ELSE 0 END) AS unmapped_at_hop1,
       SUM(CASE WHEN amm.activity_id IS NOT NULL AND amc.activity_code IS NULL THEN 1 ELSE 0 END) AS unmapped_at_hop2,
       COUNT(DISTINCT amc.activity_group_description) AS wbs_resolved
FROM a LEFT JOIN dbo.activity_master_mapping amm ON amm.activity_id = a.activity_id
       LEFT JOIN dbo.activity_master_csv amc ON amc.activity_code = amm.activity_code""")

show("task_codes that resolve to MULTIPLE WBS (fan-out from duplicate masters)", """
WITH a AS (SELECT DISTINCT task_code,
             LEFT(task_code, NULLIF(CHARINDEX('-', task_code),0)-1) AS activity_id
           FROM well.task_daily)
SELECT TOP 10 a.task_code, COUNT(DISTINCT amc.activity_group_description) AS wbs_count
FROM a JOIN dbo.activity_master_mapping amm ON amm.activity_id=a.activity_id
       JOIN dbo.activity_master_csv amc ON amc.activity_code=amm.activity_code
GROUP BY a.task_code HAVING COUNT(DISTINCT amc.activity_group_description)>1
ORDER BY wbs_count DESC""")

print("\n"+"="*100); print("TASK PLAN TABLES - which is authoritative?"); print("="*100)
show("core.engineering_task_plan shape", """
SELECT COUNT(*) AS rows_, COUNT(DISTINCT project_id) AS projects, COUNT(DISTINCT schedule_id) AS schedules,
       COUNT(DISTINCT code) AS codes, COUNT(DISTINCT type) AS types,
       CONVERT(varchar(20),MIN(Time_Stamp),120) AS min_ts, CONVERT(varchar(20),MAX(Time_Stamp),120) AS max_ts
FROM core.engineering_task_plan""")
show("core.engineering_task_plan type breakdown", """
SELECT type, COUNT(*) AS n, COUNT(DISTINCT code) AS codes,
       SUM(CASE WHEN weightage IS NULL THEN 1 ELSE 0 END) AS wt_null,
       MIN(weightage) AS wt_min, MAX(weightage) AS wt_max,
       MIN(progress) AS pr_min, MAX(progress) AS pr_max
FROM core.engineering_task_plan GROUP BY type ORDER BY n DESC""")
show("dbo.activity_task_plan shape (17.9M rows)", """
SELECT COUNT_BIG(*) AS rows_, COUNT(DISTINCT well_id) AS wells, COUNT(DISTINCT project_id) AS projects,
       COUNT(DISTINCT schedule_id) AS schedules, COUNT(DISTINCT source_id) AS sources,
       CONVERT(varchar(20),MIN(created_at),120) AS min_created,
       CONVERT(varchar(20),MAX(created_at),120) AS max_created
FROM dbo.activity_task_plan""")
show("dbo.activity_taskplan_job_progress shape", """
SELECT COUNT(*) AS rows_, COUNT(DISTINCT well_id) AS wells, COUNT(DISTINCT project_id) AS projects,
       COUNT(DISTINCT parent_wbs_code) AS wbs_codes, COUNT(DISTINCT task_type) AS task_types,
       CONVERT(varchar(20),MIN(created_at),120) AS min_created,
       CONVERT(varchar(20),MAX(created_at),120) AS max_created
FROM dbo.activity_taskplan_job_progress""")
