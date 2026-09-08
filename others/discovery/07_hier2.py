"""Phase 7 - nail down the WBS parent/child linkage + weightage semantics."""
from dbx import connect, rows
cn = connect()
def show(t, sql, n=25):
    print(f"\n--- {t} ---")
    try: rs = rows(cn, sql)
    except Exception as e: print("   ERROR:", str(e)[:400]); return
    if not rs: print("   (no rows)"); return
    hdr = list(rs[0].keys()); print("   " + " | ".join(hdr))
    for r in rs[:n]: print("   " + " | ".join(str(r[h])[:52] for h in hdr))
    if len(rs) > n: print(f"   ... {len(rs)-n} more")

show("etp: distinct W codes (are they dotted?)", """
SELECT TOP 25 code, COUNT(*) AS n, MIN(LEFT(text,34)) AS txt,
       MIN(weightage) AS wt_min, MAX(weightage) AS wt_max, MIN(parent) AS parent
FROM core.engineering_task_plan WHERE type='W' GROUP BY code ORDER BY code""")

show("etp: how many W codes contain a dot", """
SELECT SUM(CASE WHEN code LIKE '%.%' THEN 1 ELSE 0 END) AS dotted,
       SUM(CASE WHEN code NOT LIKE '%.%' THEN 1 ELSE 0 END) AS not_dotted,
       COUNT(*) AS n FROM (SELECT DISTINCT code FROM core.engineering_task_plan WHERE type='W') x""")

show("etp: exact latest Time_Stamp per project + W weightage sum (fixed grain)", """
WITH last AS (SELECT project_id, MAX(Time_Stamp) ts FROM core.engineering_task_plan GROUP BY project_id)
SELECT e.project_id, CONVERT(varchar(20),e.Time_Stamp,120) AS ts,
       COUNT(DISTINCT e.code) AS wbs_codes, COUNT(*) AS w_rows,
       CAST(SUM(x.w) AS decimal(14,4)) AS sum_wt_distinct_code
FROM core.engineering_task_plan e JOIN last l ON l.project_id=e.project_id AND l.ts=e.Time_Stamp
CROSS APPLY (SELECT TOP 1 weightage AS w FROM core.engineering_task_plan e2
             WHERE e2.project_id=e.project_id AND e2.Time_Stamp=e.Time_Stamp AND e2.code=e.code AND e2.type='W') x
WHERE e.type='W' GROUP BY e.project_id, e.Time_Stamp""")

show("etp: duplicate (project,Time_Stamp,code,type) rows == pure duplication?", """
WITH last AS (SELECT MAX(Time_Stamp) ts FROM core.engineering_task_plan)
SELECT COUNT(*) AS groups_dup, SUM(n) AS rows_in_dup_groups, SUM(n)-COUNT(*) AS extra_rows,
       MAX(n) AS worst, SUM(CASE WHEN distinct_payload>1 THEN 1 ELSE 0 END) AS groups_with_DIFFERENT_values
FROM (SELECT e.project_id, e.code, e.type, COUNT(*) n,
             COUNT(DISTINCT CONCAT(weightage,'|',progress,'|',start_date,'|',end_date)) distinct_payload
      FROM core.engineering_task_plan e CROSS JOIN last l WHERE e.Time_Stamp=l.ts
      GROUP BY e.project_id, e.code, e.type HAVING COUNT(*)>1) x""")

show("etp: PW1 root across snapshots - weightage drift", """
SELECT CONVERT(varchar(20),Time_Stamp,120) AS ts, project_id, COUNT(*) AS n,
       MIN(weightage) AS wt_min, MAX(weightage) AS wt_max, MIN(progress) AS pr_min, MAX(progress) AS pr_max
FROM core.engineering_task_plan WHERE type='W' AND code='PW1'
GROUP BY Time_Stamp, project_id ORDER BY Time_Stamp DESC""", 12)

show("etp: does `parent` match another row's `id`?", """
WITH last AS (SELECT MAX(Time_Stamp) ts FROM core.engineering_task_plan)
SELECT COUNT(*) AS child_rows,
       SUM(CASE WHEN p.id IS NULL THEN 1 ELSE 0 END) AS parent_id_NOT_found_in_same_snapshot
FROM core.engineering_task_plan e CROSS JOIN last l
LEFT JOIN core.engineering_task_plan p ON p.id = TRY_CAST(e.parent AS bigint) AND p.Time_Stamp = e.Time_Stamp
WHERE e.Time_Stamp=l.ts AND e.parent IS NOT NULL AND e.parent <> '0'""")

show("job_progress: dotted task_code hierarchy for ONE well", """
SELECT TOP 30 task_code, LEFT(task_name,30) AS nm, task_type, parent_wbs_code,
       progress, weightage,
       CONVERT(varchar(10),planned_start_date,120) AS p_start,
       CONVERT(varchar(10),planned_end_date,120) AS p_end,
       CONVERT(varchar(10),actual_start_date,120) AS a_start,
       CONVERT(varchar(10),actual_end_date,120) AS a_end
FROM dbo.activity_taskplan_job_progress WHERE well_id='33760' ORDER BY task_code""", 30)

show("job_progress: A-type rows for same well (activities under WBS)", """
SELECT TOP 15 task_code, LEFT(task_name,32) AS nm, task_type, parent_id, progress, weightage,
       CONVERT(varchar(10),actual_start_date,120) AS a_start,
       CONVERT(varchar(10),actual_end_date,120) AS a_end
FROM dbo.activity_taskplan_job_progress WHERE well_id='33760' AND task_type='A' ORDER BY task_code""", 15)

show("job_progress: progress column - all zero?", """
SELECT task_type, COUNT(*) AS n, COUNT(DISTINCT progress) AS distinct_progress,
       MIN(TRY_CAST(progress AS float)) AS pr_min, MAX(TRY_CAST(progress AS float)) AS pr_max,
       SUM(CASE WHEN TRY_CAST(progress AS float) IS NULL AND progress IS NOT NULL THEN 1 ELSE 0 END) AS non_numeric,
       MIN(TRY_CAST(weightage AS float)) AS wt_min, MAX(TRY_CAST(weightage AS float)) AS wt_max
FROM dbo.activity_taskplan_job_progress GROUP BY task_type""")

show("job_progress: 1900-01-01 placeholder dates", """
SELECT SUM(CASE WHEN CAST(planned_start_date AS date)='1900-01-01' THEN 1 ELSE 0 END) AS ps_1900,
       SUM(CASE WHEN CAST(planned_end_date AS date)='1900-01-01' THEN 1 ELSE 0 END) AS pe_1900,
       SUM(CASE WHEN CAST(actual_start_date AS date)='1900-01-01' THEN 1 ELSE 0 END) AS as_1900,
       SUM(CASE WHEN CAST(actual_end_date AS date)='1900-01-01' THEN 1 ELSE 0 END) AS ae_1900,
       SUM(CASE WHEN CAST(target_start_date AS date)='1900-01-01' THEN 1 ELSE 0 END) AS ts_1900,
       SUM(CASE WHEN CAST(target_end_date AS date)='1900-01-01' THEN 1 ELSE 0 END) AS te_1900,
       COUNT(*) AS n FROM dbo.activity_taskplan_job_progress""")
