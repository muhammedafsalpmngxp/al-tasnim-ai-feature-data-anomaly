"""Phase 15 - verify the three gaps phase 14 exposed: crew_code target, dual employee id,
and where manhours actually live."""
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
        print("   " + " | ".join(str(r[h])[:46] for h in hdr))
    if len(rs) > n:
        print(f"   ... {len(rs)-n} more")


show("CREW_CODE: which ref.crew column does activity_master_csv.crew_code actually match?", """
SELECT
 (SELECT COUNT(DISTINCT amc.crew_code) FROM dbo.activity_master_csv amc WHERE amc.crew_code IS NOT NULL) AS codes_total,
 (SELECT COUNT(DISTINCT amc.crew_code) FROM dbo.activity_master_csv amc
   JOIN ref.crew c ON c.code = amc.crew_code) AS match_ref_crew_code,
 (SELECT COUNT(DISTINCT amc.crew_code) FROM dbo.activity_master_csv amc
   JOIN ref.crew c ON c.composition = amc.crew_code) AS match_ref_crew_composition,
 (SELECT COUNT(DISTINCT amc.crew_code) FROM dbo.activity_master_csv amc
   JOIN ref.crew_type ct ON ct.crew_type_code = amc.crew_code) AS match_crew_type_code,
 (SELECT COUNT(DISTINCT amc.crew_code) FROM dbo.activity_master_csv amc
   JOIN ref.crew_type ct ON ct.Crew_group_code = amc.crew_code) AS match_crew_group_code""")

show("crew_code samples vs the candidate targets", """
SELECT TOP 12 amc.crew_code,
  (SELECT COUNT(*) FROM ref.crew c WHERE c.composition = amc.crew_code) AS in_composition,
  (SELECT COUNT(*) FROM ref.crew_type ct WHERE ct.crew_type_code = amc.crew_code) AS in_crew_type_code,
  (SELECT COUNT(*) FROM ref.crew_type ct WHERE ct.Crew_group_code = amc.crew_code) AS in_crew_group_code
FROM (SELECT DISTINCT crew_code FROM dbo.activity_master_csv WHERE crew_code IS NOT NULL) amc
ORDER BY amc.crew_code""")

show("ref.crew_type sample (to see the code vocabularies)", """
SELECT TOP 12 crew_type_id, crew_type_code, LEFT(crew_type_name,34) AS nm, Crew_group_code FROM ref.crew_type""")

show("EMPLOYEE dual id: `id` vs `employee_id` - are they the same?", """
SELECT COUNT(*) AS employees,
 COUNT(DISTINCT id) AS distinct_id,
 COUNT(DISTINCT employee_id) AS distinct_employee_id,
 SUM(CASE WHEN employee_id IS NULL THEN 1 ELSE 0 END) AS employee_id_NULL,
 SUM(CASE WHEN employee_id = id THEN 1 ELSE 0 END) AS the_two_AGREE,
 SUM(CASE WHEN employee_id IS NOT NULL AND employee_id <> id THEN 1 ELSE 0 END) AS the_two_DISAGREE
FROM ref.employee""")

show("Which id do the child tables actually point at?", """
SELECT
 (SELECT COUNT(DISTINCT b.employee_id) FROM bridge.crew_employee b
   WHERE NOT EXISTS (SELECT 1 FROM ref.employee e WHERE e.id = b.employee_id)) AS bridge_orphans_vs_ID,
 (SELECT COUNT(DISTINCT b.employee_id) FROM bridge.crew_employee b
   WHERE NOT EXISTS (SELECT 1 FROM ref.employee e WHERE e.employee_id = b.employee_id)) AS bridge_orphans_vs_EMPLOYEE_ID,
 (SELECT COUNT(DISTINCT t.emp_id) FROM well.task_daily t WHERE t.emp_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM ref.employee e WHERE e.id = t.emp_id)) AS taskdaily_orphans_vs_ID,
 (SELECT COUNT(DISTINCT t.emp_id) FROM well.task_daily t WHERE t.emp_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM ref.employee e WHERE e.employee_id = t.emp_id)) AS taskdaily_orphans_vs_EMPLOYEE_ID""")

show("employee name / uid / email completeness (correct column names)", """
SELECT COUNT(*) AS employees,
 SUM(CASE WHEN emp_name IS NULL OR LTRIM(RTRIM(emp_name))='' THEN 1 ELSE 0 END) AS no_name,
 SUM(CASE WHEN emp_uid IS NULL OR LTRIM(RTRIM(emp_uid))='' THEN 1 ELSE 0 END) AS no_uid,
 SUM(CASE WHEN email IS NULL THEN 1 ELSE 0 END) AS no_email,
 SUM(CASE WHEN emp_status=1 THEN 1 ELSE 0 END) AS active,
 SUM(CASE WHEN emp_status=0 THEN 1 ELSE 0 END) AS inactive,
 SUM(CASE WHEN location_id IS NULL THEN 1 ELSE 0 END) AS no_location,
 SUM(CASE WHEN atnm_sc IS NULL THEN 1 ELSE 0 END) AS no_atnm_sc
FROM ref.employee""")

show("Duplicate employees by normalised emp_name", """
SELECT COUNT(*) AS dup_groups, SUM(n)-COUNT(*) AS extra_rows, MAX(n) AS worst FROM (
  SELECT UPPER(LTRIM(RTRIM(emp_name))) AS nm, COUNT(*) AS n FROM ref.employee
  WHERE emp_name IS NOT NULL AND LTRIM(RTRIM(emp_name))<>''
  GROUP BY UPPER(LTRIM(RTRIM(emp_name))) HAVING COUNT(*)>1) x""")

show("Duplicate emp_name examples (same person twice?)", """
SELECT TOP 10 UPPER(LTRIM(RTRIM(emp_name))) AS nm, COUNT(*) AS n,
  COUNT(DISTINCT emp_uid) AS uids, COUNT(DISTINCT company_id) AS companies,
  COUNT(DISTINCT ISNULL(nationality_type,'?')) AS nationalities
FROM ref.employee WHERE emp_name IS NOT NULL AND LTRIM(RTRIM(emp_name))<>''
GROUP BY UPPER(LTRIM(RTRIM(emp_name))) HAVING COUNT(*)>1 ORDER BY n DESC""")

show("Duplicate emp_uid (the business identifier)", """
SELECT TOP 10 emp_uid, COUNT(*) AS n, COUNT(DISTINCT UPPER(LTRIM(RTRIM(emp_name)))) AS distinct_names
FROM ref.employee WHERE emp_uid IS NOT NULL AND LTRIM(RTRIM(emp_uid))<>''
GROUP BY emp_uid HAVING COUNT(*)>1 ORDER BY n DESC""")

show("Nationality conflict: same emp_uid recorded as both National and Expat", """
SELECT COUNT(*) AS conflicting_uids FROM (
  SELECT emp_uid FROM ref.employee WHERE emp_uid IS NOT NULL AND nationality_type IS NOT NULL
  GROUP BY emp_uid HAVING COUNT(DISTINCT nationality_type)>1) x""")

show("MANHOURS: where do they actually live and how populated?", """
SELECT 'well.task_daily.daily_actual_hours' AS src, COUNT(*) AS rows_,
       COUNT(daily_actual_hours) AS populated, MIN(daily_actual_hours) AS min_, MAX(daily_actual_hours) AS max_
FROM well.task_daily
UNION ALL SELECT 'well.task_daily.data_hours', COUNT(*), COUNT(data_hours), MIN(data_hours), MAX(data_hours)
FROM well.task_daily
UNION ALL SELECT 'core.engineering_task_plan.manhours', COUNT(*), COUNT(manhours), MIN(manhours), MAX(manhours)
FROM core.engineering_task_plan
UNION ALL SELECT 'core.engineering_task_plan.manhoursactual', COUNT(*), COUNT(manhoursactual), MIN(manhoursactual), MAX(manhoursactual)
FROM core.engineering_task_plan
UNION ALL SELECT 'dbo.activity_taskplan_job_progress.actual_manhours', COUNT(*),
       COUNT(TRY_CAST(actual_manhours AS float)), MIN(TRY_CAST(actual_manhours AS float)), MAX(TRY_CAST(actual_manhours AS float))
FROM dbo.activity_taskplan_job_progress""")

show("QUANTITY: same question for qty (norm checks need qty OR manhours)", """
SELECT 'well.task_daily.daily_actual_quantity' AS src, COUNT(*) AS rows_,
       COUNT(daily_actual_quantity) AS populated, MIN(daily_actual_quantity) AS min_, MAX(daily_actual_quantity) AS max_
FROM well.task_daily
UNION ALL SELECT 'well.task_daily.data_qty', COUNT(*), COUNT(data_qty), MIN(data_qty), MAX(data_qty) FROM well.task_daily
UNION ALL SELECT 'well.task_daily.required', COUNT(*), COUNT(required), MIN(required), MAX(required) FROM well.task_daily
UNION ALL SELECT 'well.task_daily.planned', COUNT(*), COUNT(planned), MIN(planned), MAX(planned) FROM well.task_daily
UNION ALL SELECT 'core.engineering_task_plan.qty', COUNT(*), COUNT(qty), MIN(qty), MAX(qty) FROM core.engineering_task_plan
UNION ALL SELECT 'core.engineering_task_plan.qtyactual', COUNT(*), COUNT(qtyactual), MIN(qtyactual), MAX(qtyactual)
FROM core.engineering_task_plan""")

show("task_daily: which columns are actually usable (populated %)", """
SELECT col, populated, CAST(100.0*populated/107484 AS decimal(5,1)) AS pct FROM (
 SELECT 'progress' AS col, COUNT(progress) AS populated FROM well.task_daily
 UNION ALL SELECT 'completed', COUNT(completed) FROM well.task_daily
 UNION ALL SELECT 'actual_start', COUNT(actual_start) FROM well.task_daily
 UNION ALL SELECT 'actual_end', COUNT(actual_end) FROM well.task_daily
 UNION ALL SELECT 'target_start', COUNT(target_start) FROM well.task_daily
 UNION ALL SELECT 'target_end', COUNT(target_end) FROM well.task_daily
 UNION ALL SELECT 'committed_start', COUNT(committed_start) FROM well.task_daily
 UNION ALL SELECT 'committed_end', COUNT(committed_end) FROM well.task_daily
 UNION ALL SELECT 'startDate', COUNT(startDate) FROM well.task_daily
 UNION ALL SELECT 'endDate', COUNT(endDate) FROM well.task_daily
 UNION ALL SELECT 'duration', COUNT(duration) FROM well.task_daily
 UNION ALL SELECT 'remaining_duration', COUNT(remaining_duration) FROM well.task_daily
 UNION ALL SELECT 'required', COUNT(required) FROM well.task_daily
 UNION ALL SELECT 'planned', COUNT(planned) FROM well.task_daily
 UNION ALL SELECT 'ready', COUNT(ready) FROM well.task_daily
 UNION ALL SELECT 'daily_completed', COUNT(daily_completed) FROM well.task_daily
 UNION ALL SELECT 'planned_crew', COUNT(planned_crew) FROM well.task_daily
 UNION ALL SELECT 'task_assignee', COUNT(task_assignee) FROM well.task_daily
 UNION ALL SELECT 'uom_id', COUNT(uom_id) FROM well.task_daily
 UNION ALL SELECT 'created_at', COUNT(created_at) FROM well.task_daily
 UNION ALL SELECT 'updated_at', COUNT(updated_at) FROM well.task_daily
) x ORDER BY populated DESC""", 25)
