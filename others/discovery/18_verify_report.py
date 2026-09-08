"""Phase 18 - verify every claim in the external profiling report before designing around it.
Each block prints the CLAIM then the MEASURED value so they can be compared directly."""
from dbx import connect, rows
cn = connect()


def check(claim, sql):
    print(f"\n{'='*112}\nCLAIM: {claim}\n{'-'*112}")
    try:
        rs = rows(cn, sql)
    except Exception as e:  # noqa: BLE001
        print("   ERROR:", str(e)[:300]); return
    if not rs:
        print("   (no rows)"); return
    hdr = list(rs[0].keys())
    print("MEASURED:")
    print("   " + " | ".join(hdr))
    for r in rs[:12]:
        print("   " + " | ".join(str(r[h])[:44] for h in hdr))


check("engineering task plan: 128,643 rows but only 3,359 distinct IDs -> 125,284 duplicate IDs", """
SELECT COUNT(*) AS rows_,
       COUNT(DISTINCT id) AS distinct_id,
       COUNT(*) - COUNT(DISTINCT id) AS duplicate_id_occurrences,
       COUNT(DISTINCT CONCAT(project_id,'|',code)) AS distinct_project_plus_code,
       COUNT(DISTINCT Time_Stamp) AS distinct_snapshots,
       COUNT(DISTINCT CONCAT(project_id,'|',code,'|',CONVERT(varchar(30),Time_Stamp,121))) AS distinct_proj_code_snapshot
FROM core.engineering_task_plan""")

check("...and is (project_id, code, Time_Stamp) unique? i.e. is it snapshot history, not duplication", """
SELECT COUNT(*) AS dup_groups, ISNULL(SUM(n)-COUNT(*),0) AS excess_rows FROM (
  SELECT project_id, code, Time_Stamp, COUNT(*) AS n FROM core.engineering_task_plan
  GROUP BY project_id, code, Time_Stamp HAVING COUNT(*)>1) x""")

check("documents table (FLAF/pegging/MOC): all primary/key values empty for its 1,009 rows", """
SELECT c.name AS column_name, ty.name AS ty FROM sys.columns c
JOIN sys.types ty ON ty.user_type_id=c.user_type_id
WHERE c.object_id=OBJECT_ID('dbo.flaf_po_peg_scr_moc_document_data') ORDER BY c.column_id""")

check("...document table: null counts on every candidate key column", """
SELECT COUNT(*) AS rows_,
 SUM(CASE WHEN well_id IS NULL THEN 1 ELSE 0 END) AS well_id_null,
 COUNT(DISTINCT well_id) AS well_id_distinct,
 SUM(CASE WHEN PEGjson IS NULL THEN 1 ELSE 0 END) AS pegjson_null,
 SUM(CASE WHEN FLAFjson IS NULL THEN 1 ELSE 0 END) AS flafjson_null,
 SUM(CASE WHEN MOCjson IS NULL THEN 1 ELSE 0 END) AS mocjson_null
FROM dbo.flaf_po_peg_scr_moc_document_data""")

check("task log: 3 duplicate IDs (id distinct = 107,481 vs rows 107,484)", """
SELECT COUNT(*) AS rows_, COUNT(DISTINCT id) AS distinct_id,
       COUNT(*) - COUNT(DISTINCT id) AS duplicate_id_occurrences
FROM well.task_daily""")

check("task log: startDate max = 2032-06-15, endDate max = 2071-03-24", """
SELECT CONVERT(varchar(10),MIN(startDate),120) AS start_min, CONVERT(varchar(10),MAX(startDate),120) AS start_max,
       CONVERT(varchar(10),MIN(endDate),120) AS end_min, CONVERT(varchar(10),MAX(endDate),120) AS end_max,
       SUM(CASE WHEN endDate > '2030-01-01' THEN 1 ELSE 0 END) AS end_after_2030,
       COUNT(DISTINCT CASE WHEN endDate > '2030-01-01' THEN task_code END) AS task_codes_affected,
       COUNT(DISTINCT CASE WHEN endDate > '2030-01-01' THEN well_id END) AS wells_affected
FROM well.task_daily""")

check("...which rows carry the far-future end dates", """
SELECT TOP 10 task_code, well_id, CONVERT(varchar(10),startDate,120) AS p6_start,
       CONVERT(varchar(10),endDate,120) AS p6_end, CONVERT(varchar(10),ActionOn,120) AS action_on
FROM well.task_daily WHERE endDate > '2030-01-01' ORDER BY endDate DESC""")

check("plant table: 15 rows but only 5 distinct plant codes -> 10 duplicate keys", """
SELECT 'core.plant_description' AS tbl, COUNT(*) AS rows_ FROM core.plant_description
UNION ALL SELECT 'dbo.plant_description', COUNT(*) FROM dbo.plant_description
UNION ALL SELECT 'wbs.Plant_master', COUNT(*) FROM wbs.Plant_master""")

check("...core.plant_description columns + distinctness", """
SELECT c.name AS column_name, ty.name AS ty FROM sys.columns c
JOIN sys.types ty ON ty.user_type_id=c.user_type_id
WHERE c.object_id=OBJECT_ID('core.plant_description') ORDER BY c.column_id""")

check("revenue: 4 distinct PMS values range 0.00-0.30; 36 distinct created_at in a 35-second window", """
SELECT COUNT(*) AS rows_, COUNT(DISTINCT pms) AS distinct_pms,
       MIN(pms) AS pms_min, MAX(pms) AS pms_max,
       COUNT(DISTINCT task_code) AS distinct_task_code,
       COUNT(DISTINCT revenue_id) AS distinct_revenue_id
FROM core.revenue""")

check("wells: 209 no project (25.7%), 6 distinct projects; pegging 254 missing (31.2%)", """
SELECT COUNT(*) AS wells,
 SUM(CASE WHEN project_id IS NULL THEN 1 ELSE 0 END) AS no_project,
 COUNT(DISTINCT project_id) AS distinct_projects,
 SUM(CASE WHEN pegged_date IS NULL THEN 1 ELSE 0 END) AS pegging_missing,
 SUM(CASE WHEN flaf_issue_date IS NULL THEN 1 ELSE 0 END) AS flaf_missing,
 SUM(CASE WHEN rig_on_date IS NULL THEN 1 ELSE 0 END) AS rig_on_missing,
 SUM(CASE WHEN eng_completion_date IS NULL THEN 1 ELSE 0 END) AS hookup_missing,
 SUM(CASE WHEN cluster_code IS NULL THEN 1 ELSE 0 END) AS cluster_missing
FROM well.well_master""")

check("...are those 6 projects real? do they exist in project.project_mstr", """
SELECT COUNT(*) AS projects_in_mstr,
 (SELECT COUNT(DISTINCT m.project_id) FROM well.well_master m WHERE m.project_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM project.project_mstr p WHERE p.project_id=m.project_id)) AS well_projects_NOT_in_mstr,
 (SELECT COUNT(*) FROM project.project_mstr p
   WHERE NOT EXISTS (SELECT 1 FROM well.well_master m WHERE m.project_id=p.project_id)) AS mstr_projects_with_no_wells
FROM project.project_mstr""")

check("activity mapping: 379 rows, ~375 distinct activity IDs", """
SELECT COUNT(*) AS rows_, COUNT(DISTINCT activity_id) AS distinct_activity_id,
       COUNT(*)-COUNT(DISTINCT activity_id) AS duplicates
FROM dbo.activity_master_mapping""")
