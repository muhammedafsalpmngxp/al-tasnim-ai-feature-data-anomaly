"""Phase 12 - WELL domain: what a well actually looks like, lifecycle stage distribution,
identifier formats, and the shape of every value we will have to parse."""
from dbx import connect, rows
cn = connect()


def show(t, sql, n=20):
    print(f"\n{'-'*112}\n{t}\n{'-'*112}")
    try:
        rs = rows(cn, sql)
    except Exception as e:  # noqa: BLE001
        print("   ERROR:", str(e)[:400]); return
    if not rs:
        print("   (no rows)"); return
    hdr = list(rs[0].keys())
    print("   " + " | ".join(hdr))
    for r in rs[:n]:
        print("   " + " | ".join(str(r[h])[:40] for h in hdr))
    if len(rs) > n:
        print(f"   ... {len(rs)-n} more")


print("#" * 112)
print("# WELL DOMAIN")
print("#" * 112)

show("Lifecycle stage distribution (derived from business rule sec 7 order)", """
SELECT stage, COUNT(*) AS wells FROM (
  SELECT CASE
    WHEN eng_completion_date IS NOT NULL              THEN '7. COMPLETED (hook-up done)'
    WHEN rig_off_date IS NOT NULL                     THEN '6. drilled, awaiting hook-up'
    WHEN rig_on_date IS NOT NULL                      THEN '5. drilling (rig on)'
    WHEN pegged_date IS NOT NULL AND flaf_issue_date IS NOT NULL THEN '4. pegged + FLAF, construction'
    WHEN flaf_issue_date IS NOT NULL                  THEN '3. FLAF only'
    WHEN pegged_date IS NOT NULL                      THEN '2. pegged only'
    ELSE                                                   '1. nothing started'
  END AS stage FROM well.well_master) x
GROUP BY stage ORDER BY stage""")

show("Well-count sanity: how many wells are in each source of well identity", """
SELECT 'well.well_master'         AS src, COUNT(DISTINCT well_id) AS wells FROM well.well_master
UNION ALL SELECT 'well.well_progress',      COUNT(DISTINCT well_id) FROM well.well_progress
UNION ALL SELECT 'well.well_classification',COUNT(DISTINCT well_id) FROM well.well_classification
UNION ALL SELECT 'well.task_daily',         COUNT(DISTINCT well_id) FROM well.task_daily
UNION ALL SELECT 'well.well_location (ref)',COUNT(DISTINCT well_location_id) FROM well.well_location
UNION ALL SELECT 'well.well_type (ref)',    COUNT(DISTINCT well_type_id) FROM well.well_type
UNION ALL SELECT 'dbo.engineering_well_priority', COUNT(DISTINCT well_id) FROM dbo.engineering_well_priority
UNION ALL SELECT 'dbo.activity_taskplan_job_progress', COUNT(DISTINCT well_id) FROM dbo.activity_taskplan_job_progress
UNION ALL SELECT 'dsq.drilling_sequence',   COUNT(DISTINCT well_location_id) FROM dsq.drilling_sequence""")

show("ramz_id FORMAT patterns (what shapes does the well code take)", """
SELECT pattern, COUNT(*) AS n, MIN(ramz_id) AS example FROM (
  SELECT ramz_id,
    CASE
      WHEN ramz_id IS NULL THEN '(NULL)'
      WHEN LTRIM(RTRIM(ramz_id))='' THEN '(BLANK)'
      WHEN ramz_id LIKE 'SONRAKDS[0-9][0-9][0-9][0-9]' THEN 'SONRAKDS####'
      WHEN ramz_id LIKE 'SON000NM[0-9][0-9][0-9][0-9]' THEN 'SON000NM####'
      WHEN ramz_id LIKE 'SON%' THEN 'SON<other>'
      WHEN ramz_id LIKE '%[0-9]%' THEN 'contains digits, other shape'
      ELSE 'NO DIGITS AT ALL (suspicious)'
    END AS pattern FROM well.well_master) x
GROUP BY pattern ORDER BY n DESC""")

show("well_progress.well_name FORMAT patterns (the OTHER naming system)", """
SELECT pattern, COUNT(DISTINCT well_name) AS distinct_names, MIN(well_name) AS example FROM (
  SELECT well_name,
    CASE
      WHEN well_name IS NULL THEN '(NULL)'
      WHEN well_name LIKE '%\_%' ESCAPE '\' AND well_name LIKE '%-%' THEN 'has BOTH _ and -'
      WHEN well_name LIKE '%\_%' ESCAPE '\' THEN 'underscore_separated'
      WHEN well_name LIKE '%-%' THEN 'hyphen-separated'
      WHEN well_name LIKE '% %' THEN 'contains SPACES'
      ELSE 'single token'
    END AS pattern FROM well.well_progress) x
GROUP BY pattern ORDER BY distinct_names DESC""")

show("Wells with NO usable name at all (both identity sources empty/sentinel)", """
SELECT COUNT(*) AS wells_with_no_name FROM well.well_master m
WHERE (m.ramz_id IS NULL OR LTRIM(RTRIM(m.ramz_id))='' OR m.ramz_id LIKE 'NO %')
  AND NOT EXISTS (SELECT 1 FROM well.well_progress p
                  WHERE p.well_id=m.well_id AND p.well_name IS NOT NULL
                    AND LTRIM(RTRIM(p.well_name))<>'')""")

show("well_id numeric range + gaps (is it a real surrogate key?)", """
SELECT MIN(well_id) AS min_id, MAX(well_id) AS max_id, COUNT(*) AS wells,
       MAX(well_id)-MIN(well_id)+1 AS id_span,
       CAST(100.0*COUNT(*)/(MAX(well_id)-MIN(well_id)+1) AS decimal(5,2)) AS density_pct
FROM well.well_master""")

show("buffer_status - 177 distinct values in a varchar(20). What is in there?", """
SELECT TOP 20 buffer_status, COUNT(*) AS n FROM well.well_master
GROUP BY buffer_status ORDER BY n DESC""")

show("buffer_status value SHAPE (is it a status, a date, or free text?)", """
SELECT shape, COUNT(*) AS n, MIN(buffer_status) AS example FROM (
  SELECT buffer_status,
    CASE WHEN TRY_CAST(buffer_status AS date) IS NOT NULL THEN 'parses as DATE'
         WHEN TRY_CAST(buffer_status AS float) IS NOT NULL THEN 'parses as NUMBER'
         WHEN buffer_status IS NULL THEN '(NULL)'
         ELSE 'text' END AS shape
  FROM well.well_master) x GROUP BY shape ORDER BY n DESC""")

show("Which well_master TEXT columns actually hold dates/numbers (type mis-assignment)", """
SELECT col, total_nonnull, as_date, as_number, as_text FROM (
  SELECT 'progress' AS col, COUNT(progress) AS total_nonnull,
         SUM(CASE WHEN TRY_CAST(progress AS date) IS NOT NULL THEN 1 ELSE 0 END) AS as_date,
         SUM(CASE WHEN TRY_CAST(progress AS date) IS NULL AND TRY_CAST(progress AS float) IS NOT NULL THEN 1 ELSE 0 END) AS as_number,
         SUM(CASE WHEN TRY_CAST(progress AS date) IS NULL AND TRY_CAST(progress AS float) IS NULL THEN 1 ELSE 0 END) AS as_text
  FROM well.well_master
  UNION ALL SELECT 'buffer_status', COUNT(buffer_status),
         SUM(CASE WHEN TRY_CAST(buffer_status AS date) IS NOT NULL THEN 1 ELSE 0 END),
         SUM(CASE WHEN TRY_CAST(buffer_status AS date) IS NULL AND TRY_CAST(buffer_status AS float) IS NOT NULL THEN 1 ELSE 0 END),
         SUM(CASE WHEN TRY_CAST(buffer_status AS date) IS NULL AND TRY_CAST(buffer_status AS float) IS NULL THEN 1 ELSE 0 END)
  FROM well.well_master
  UNION ALL SELECT 'fl_length_m', COUNT(fl_length_m),
         SUM(CASE WHEN TRY_CAST(fl_length_m AS date) IS NOT NULL THEN 1 ELSE 0 END),
         SUM(CASE WHEN TRY_CAST(fl_length_m AS date) IS NULL AND TRY_CAST(fl_length_m AS float) IS NOT NULL THEN 1 ELSE 0 END),
         SUM(CASE WHEN TRY_CAST(fl_length_m AS date) IS NULL AND TRY_CAST(fl_length_m AS float) IS NULL THEN 1 ELSE 0 END)
  FROM well.well_master
  UNION ALL SELECT 'ssfd_value', COUNT(ssfd_value),
         SUM(CASE WHEN TRY_CAST(ssfd_value AS date) IS NOT NULL THEN 1 ELSE 0 END),
         SUM(CASE WHEN TRY_CAST(ssfd_value AS date) IS NULL AND TRY_CAST(ssfd_value AS float) IS NOT NULL THEN 1 ELSE 0 END),
         SUM(CASE WHEN TRY_CAST(ssfd_value AS date) IS NULL AND TRY_CAST(ssfd_value AS float) IS NULL THEN 1 ELSE 0 END)
  FROM well.well_master
  UNION ALL SELECT 'fl_dia', COUNT(fl_dia),
         SUM(CASE WHEN TRY_CAST(fl_dia AS date) IS NOT NULL THEN 1 ELSE 0 END),
         SUM(CASE WHEN TRY_CAST(fl_dia AS date) IS NULL AND TRY_CAST(fl_dia AS float) IS NOT NULL THEN 1 ELSE 0 END),
         SUM(CASE WHEN TRY_CAST(fl_dia AS date) IS NULL AND TRY_CAST(fl_dia AS float) IS NULL THEN 1 ELSE 0 END)
  FROM well.well_master
  UNION ALL SELECT 'ipm', COUNT(ipm),
         SUM(CASE WHEN TRY_CAST(ipm AS date) IS NOT NULL THEN 1 ELSE 0 END),
         SUM(CASE WHEN TRY_CAST(ipm AS date) IS NULL AND TRY_CAST(ipm AS float) IS NOT NULL THEN 1 ELSE 0 END),
         SUM(CASE WHEN TRY_CAST(ipm AS date) IS NULL AND TRY_CAST(ipm AS float) IS NULL THEN 1 ELSE 0 END)
  FROM well.well_master) x ORDER BY col""")

show("fl_dia + lift_type + tie_in_port_avail - the low-cardinality vocabularies", """
SELECT 'fl_dia' AS col, ISNULL(fl_dia,'(NULL)') AS val, COUNT(*) AS n FROM well.well_master GROUP BY fl_dia
UNION ALL SELECT 'lift_type', ISNULL(lift_type,'(NULL)'), COUNT(*) FROM well.well_master GROUP BY lift_type
UNION ALL SELECT 'tie_in_port_avail', ISNULL(tie_in_port_avail,'(NULL)'), COUNT(*) FROM well.well_master GROUP BY tie_in_port_avail
UNION ALL SELECT 'ipm', ISNULL(ipm,'(NULL)'), COUNT(*) FROM well.well_master GROUP BY ipm
ORDER BY col, n DESC""", 45)

show("Reference-table vocabularies the wells point at", """
SELECT 'well.well_status' AS tbl, CAST(status_id AS varchar(10)) AS id, status_name AS val FROM well.well_status
UNION ALL SELECT 'well.well_category', CAST(well_category_id AS varchar(10)), category_name FROM well.well_category
UNION ALL SELECT 'well.well_function', CAST(well_function_id AS varchar(10)), function_name FROM well.well_function
UNION ALL SELECT 'well.well_completion_type', CAST(well_completion_type_id AS varchar(10)), completion_type_name FROM well.well_completion_type
ORDER BY tbl, id""", 45)

show("well_type usage: 414 rows in ref, how many actually used?", """
SELECT (SELECT COUNT(*) FROM well.well_type) AS ref_rows,
       (SELECT COUNT(DISTINCT well_type_id) FROM well.well_master WHERE well_type_id IS NOT NULL) AS used_by_wells,
       (SELECT COUNT(*) FROM well.well_type t
        WHERE NOT EXISTS (SELECT 1 FROM well.well_master m WHERE m.well_type_id=t.well_type_id)) AS unused_ref_rows,
       (SELECT COUNT(DISTINCT m.well_type_id) FROM well.well_master m
        WHERE m.well_type_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM well.well_type t WHERE t.well_type_id=m.well_type_id)) AS ORPHAN_type_ids""")

show("well_location usage (790 ref rows)", """
SELECT (SELECT COUNT(*) FROM well.well_location) AS ref_rows,
       (SELECT COUNT(DISTINCT well_location_id) FROM well.well_progress WHERE well_location_id IS NOT NULL) AS used_in_progress,
       (SELECT COUNT(DISTINCT well_location_id) FROM well.well_classification WHERE well_location_id IS NOT NULL) AS used_in_class,
       (SELECT COUNT(*) FROM well.well_location l
        WHERE NOT EXISTS (SELECT 1 FROM well.well_progress p WHERE p.well_location_id=l.well_location_id)
          AND NOT EXISTS (SELECT 1 FROM well.well_classification c WHERE c.well_location_id=l.well_location_id)) AS unused_ref_rows""")

show("Wells per cluster + rig (is the workload distribution plausible?)", """
SELECT ISNULL(CAST(m.cluster_code AS varchar(10)),'(NULL)') AS cluster_code,
       c.cluster_name, COUNT(*) AS wells,
       COUNT(DISTINCT m.rig_id) AS rigs_used,
       SUM(CASE WHEN m.eng_completion_date IS NOT NULL THEN 1 ELSE 0 END) AS completed
FROM well.well_master m LEFT JOIN ref.cluster c ON c.cluster_code=m.cluster_code
GROUP BY m.cluster_code, c.cluster_name ORDER BY wells DESC""")

show("ref.cluster: 10 rows but wells only use 2 - which are dead?", """
SELECT c.cluster_code, c.cluster_name,
       (SELECT COUNT(*) FROM well.well_master m WHERE m.cluster_code=c.cluster_code) AS wells
FROM ref.cluster c ORDER BY wells DESC, c.cluster_code""")

show("Rig utilisation: overlapping rig assignments (a rig cannot be on 2 wells at once)", """
WITH d AS (SELECT well_id, rig_id, rig_on_date, rig_off_date FROM well.well_master
           WHERE rig_id IS NOT NULL AND rig_on_date IS NOT NULL AND rig_off_date IS NOT NULL
             AND rig_on_date <= rig_off_date)
SELECT COUNT(*) AS overlapping_pairs, COUNT(DISTINCT a.rig_id) AS rigs_affected
FROM d a JOIN d b ON a.rig_id=b.rig_id AND a.well_id<b.well_id
WHERE a.rig_on_date <= b.rig_off_date AND b.rig_on_date <= a.rig_off_date""")

show("Rig overlap examples", """
WITH d AS (SELECT well_id, rig_id, rig_on_date, rig_off_date FROM well.well_master
           WHERE rig_id IS NOT NULL AND rig_on_date IS NOT NULL AND rig_off_date IS NOT NULL
             AND rig_on_date <= rig_off_date)
SELECT TOP 12 a.rig_id, r.rig_name, a.well_id AS well_a,
       CONVERT(varchar(10),a.rig_on_date,120) AS a_on, CONVERT(varchar(10),a.rig_off_date,120) AS a_off,
       b.well_id AS well_b,
       CONVERT(varchar(10),b.rig_on_date,120) AS b_on, CONVERT(varchar(10),b.rig_off_date,120) AS b_off,
       DATEDIFF(day, CASE WHEN a.rig_on_date>b.rig_on_date THEN a.rig_on_date ELSE b.rig_on_date END,
                     CASE WHEN a.rig_off_date<b.rig_off_date THEN a.rig_off_date ELSE b.rig_off_date END)+1 AS overlap_days
FROM d a JOIN d b ON a.rig_id=b.rig_id AND a.well_id<b.well_id
LEFT JOIN well.rig r ON r.rig_id=a.rig_id
WHERE a.rig_on_date <= b.rig_off_date AND b.rig_on_date <= a.rig_off_date
ORDER BY overlap_days DESC""")
