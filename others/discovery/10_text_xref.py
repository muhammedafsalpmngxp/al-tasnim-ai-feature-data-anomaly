"""Phase 10 - text/encoding/format anomalies + cross-table consistency of the same fact."""
from dbx import connect, rows
cn = connect()


def show(t, sql, n=15):
    print(f"\n--- {t} ---")
    try:
        rs = rows(cn, sql)
    except Exception as e:  # noqa: BLE001
        print("   ERROR:", str(e)[:300]); return
    if not rs:
        print("   (no rows)"); return
    hdr = list(rs[0].keys()); print("   " + " | ".join(hdr))
    for r in rs[:n]:
        print("   " + " | ".join(str(r[h])[:52] for h in hdr))
    if len(rs) > n:
        print(f"   ... {len(rs)-n} more")


show("ENCODING: mojibake / replacement chars in mapping_master + activity masters", """
SELECT 'mapping_master.UOM' AS col, UOM AS val, COUNT(*) AS n FROM dbo.mapping_master
WHERE UOM LIKE '%?%' OR UOM LIKE '%' + CHAR(65533) + '%' GROUP BY UOM
UNION ALL
SELECT 'activity_master_mapping.uom', uom, COUNT(*) FROM dbo.activity_master_mapping
WHERE uom LIKE '%?%' OR uom LIKE '%' + CHAR(65533) + '%' GROUP BY uom""")

show("ENCODING: HTML entities left in task/activity text", """
SELECT 'job_progress.task_name' AS col, COUNT(*) AS n FROM dbo.activity_taskplan_job_progress
WHERE task_name LIKE '%&amp;%' OR task_name LIKE '%&lt;%' OR task_name LIKE '%&gt;%' OR task_name LIKE '%&#%'
UNION ALL SELECT 'etp.text', COUNT(*) FROM core.engineering_task_plan
WHERE text LIKE '%&amp;%' OR text LIKE '%&lt;%' OR text LIKE '%&gt;%' OR text LIKE '%&#%'
UNION ALL SELECT 'activity_master_csv.activity_description', COUNT(*) FROM dbo.activity_master_csv
WHERE activity_description LIKE '%&amp;%' OR activity_description LIKE '%&#%'""")

show("UOM vocabulary drift for the SAME concept across mapping tables", """
SELECT 'mapping_master' AS src, UOM AS uom, COUNT(*) AS n FROM dbo.mapping_master GROUP BY UOM
UNION ALL SELECT 'activity_master_mapping', uom, COUNT(*) FROM dbo.activity_master_mapping GROUP BY uom
UNION ALL SELECT 'ref.uom', uom_name, COUNT(*) FROM ref.uom GROUP BY uom_name
ORDER BY uom, src""", 60)

show("NORMS disagreement: mapping_master vs activity_master_mapping for same Activity_ID", """
SELECT COUNT(*) AS shared_activity_ids,
  SUM(CASE WHEN ABS(ISNULL(mm.Norms,-1) - ISNULL(amm.norms,-1)) > 0.0001 THEN 1 ELSE 0 END) AS norms_disagree,
  SUM(CASE WHEN ISNULL(mm.UOM,'~') <> ISNULL(amm.uom,'~') THEN 1 ELSE 0 END) AS uom_disagree,
  SUM(CASE WHEN ISNULL(mm.New_Activity_Code,'~') <> ISNULL(amm.activity_code,'~') THEN 1 ELSE 0 END) AS code_disagree
FROM dbo.mapping_master mm
JOIN dbo.activity_master_mapping amm ON amm.activity_id = mm.Activity_ID""")

show("NORMS disagreement samples", """
SELECT TOP 12 mm.Activity_ID, mm.Norms AS norms_mapping_master, amm.norms AS norms_amm,
       mm.UOM AS uom_mm, amm.uom AS uom_amm,
       mm.New_Activity_Code AS code_mm, amm.activity_code AS code_amm
FROM dbo.mapping_master mm
JOIN dbo.activity_master_mapping amm ON amm.activity_id = mm.Activity_ID
WHERE ABS(ISNULL(mm.Norms,-1) - ISNULL(amm.norms,-1)) > 0.0001
   OR ISNULL(mm.New_Activity_Code,'~') <> ISNULL(amm.activity_code,'~')""")

show("WELL NAME consistency: well_master.ramz_id vs well_progress.well_name", """
SELECT COUNT(*) AS wells_compared,
  SUM(CASE WHEN m.ramz_id IS NULL AND p.well_name IS NOT NULL THEN 1 ELSE 0 END) AS name_only_in_progress,
  SUM(CASE WHEN m.ramz_id IS NOT NULL AND p.well_name IS NULL THEN 1 ELSE 0 END) AS name_only_in_master,
  SUM(CASE WHEN m.ramz_id IS NOT NULL AND p.well_name IS NOT NULL
                AND LTRIM(RTRIM(m.ramz_id)) <> LTRIM(RTRIM(p.well_name)) THEN 1 ELSE 0 END) AS names_DIFFER
FROM well.well_master m
CROSS APPLY (SELECT TOP 1 well_name FROM well.well_progress wp
             WHERE wp.well_id = m.well_id ORDER BY week_number DESC) p""")

show("WELL NAME differing samples", """
SELECT TOP 12 m.well_id, m.ramz_id AS master_ramz_id, p.well_name AS progress_well_name
FROM well.well_master m
CROSS APPLY (SELECT TOP 1 well_name FROM well.well_progress wp
             WHERE wp.well_id = m.well_id ORDER BY week_number DESC) p
WHERE m.ramz_id IS NOT NULL AND p.well_name IS NOT NULL
  AND LTRIM(RTRIM(m.ramz_id)) <> LTRIM(RTRIM(p.well_name))""")

show("SENTINEL strings stuffed into ID columns", """
SELECT 'well_master.ramz_id' AS col, ramz_id AS val, COUNT(*) AS n
FROM well.well_master WHERE ramz_id IS NOT NULL
  AND (ramz_id LIKE '%NO %' OR ramz_id LIKE '%N/A%' OR ramz_id LIKE '%TBC%'
       OR ramz_id LIKE '%TBA%' OR ramz_id LIKE '%?%' OR LEN(LTRIM(RTRIM(ramz_id)))=0)
GROUP BY ramz_id ORDER BY n DESC""")

show("well_master coordinate validity (Oman UTM 40N: E 200k-800k, N 1.8M-2.8M)", """
SELECT COUNT(*) AS with_coords,
  SUM(CASE WHEN easting=0 OR northing=0 THEN 1 ELSE 0 END) AS zero_coord,
  SUM(CASE WHEN easting NOT BETWEEN 200000 AND 800000 THEN 1 ELSE 0 END) AS easting_out_of_range,
  SUM(CASE WHEN northing NOT BETWEEN 1800000 AND 2800000 THEN 1 ELSE 0 END) AS northing_out_of_range
FROM well.well_master WHERE easting IS NOT NULL AND northing IS NOT NULL""")

show("well_master.fl_length_m stored as TEXT - non-numeric values", """
SELECT TOP 15 fl_length_m, COUNT(*) AS n
FROM well.well_master
WHERE fl_length_m IS NOT NULL AND TRY_CAST(fl_length_m AS float) IS NULL
GROUP BY fl_length_m ORDER BY n DESC""")

show("well_master.progress stored as TEXT - value shapes", """
SELECT TOP 15 progress, COUNT(*) AS n FROM well.well_master
WHERE progress IS NOT NULL GROUP BY progress ORDER BY n DESC""")

show("FK-less soft references: task_daily.crew_id / emp_id orphans", """
SELECT
 (SELECT COUNT(DISTINCT t.crew_id) FROM well.task_daily t
   WHERE t.crew_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM ref.crew c WHERE c.crew_id=t.crew_id)) AS orphan_crew_ids,
 (SELECT COUNT(DISTINCT t.emp_id) FROM well.task_daily t
   WHERE t.emp_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM ref.employee e WHERE e.employee_id=t.emp_id)) AS orphan_emp_ids,
 (SELECT COUNT(DISTINCT t.project_id) FROM well.task_daily t
   WHERE t.project_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM project.project_mstr p WHERE p.project_id=t.project_id)) AS orphan_project_ids""")

show("ref.employee nationality_type distribution (business rule sec 2)", """
SELECT ISNULL(nationality_type,'(NULL)') AS nationality_type, COUNT(*) AS n
FROM ref.employee GROUP BY nationality_type ORDER BY n DESC""")
