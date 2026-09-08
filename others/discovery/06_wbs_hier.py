"""Phase 6 - WBS hierarchy + weightage rollup in core.engineering_task_plan / activity_task_plan."""
from dbx import connect, rows

cn = connect()
def show(t, sql, n=20):
    print(f"\n--- {t} ---")
    try: rs = rows(cn, sql)
    except Exception as e: print("   ERROR:", str(e)[:400]); return
    if not rs: print("   (no rows)"); return
    hdr = list(rs[0].keys()); print("   " + " | ".join(hdr))
    for r in rs[:n]: print("   " + " | ".join(str(r[h])[:44] for h in hdr))
    if len(rs) > n: print(f"   ... {len(rs)-n} more")

show("engineering_task_plan: sample rows per type", """
SELECT type, code, parent, LEFT(ancestor,40) AS ancestor, LEFT(text,34) AS txt,
       weightage, progress, duration, qty, manhours,
       CONVERT(varchar(10),start_date,120) AS start_, CONVERT(varchar(10),end_date,120) AS end_,
       CONVERT(varchar(10),actual_start,120) AS a_start, CONVERT(varchar(10),actual_end,120) AS a_end
FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY type ORDER BY id) rn FROM core.engineering_task_plan) x
WHERE rn <= 3 ORDER BY type, code""", 30)

show("engineering_task_plan: is it snapshotted per Time_Stamp? (grain check)", """
SELECT COUNT(*) AS rows_, COUNT(DISTINCT CAST(Time_Stamp AS date)) AS snapshot_days,
       COUNT(DISTINCT CONCAT(project_id,'|',code)) AS project_code_pairs,
       CAST(COUNT(*) * 1.0 / NULLIF(COUNT(DISTINCT CONCAT(project_id,'|',code)),0) AS decimal(10,1)) AS rows_per_code
FROM core.engineering_task_plan""")

show("engineering_task_plan: WBS(type=W) weightage sum per project+snapshot", """
SELECT project_id, CAST(Time_Stamp AS date) AS snap, COUNT(DISTINCT code) AS wbs_codes,
       CAST(SUM(w) AS decimal(12,4)) AS sum_weightage
FROM (SELECT DISTINCT project_id, Time_Stamp, code, weightage AS w
      FROM core.engineering_task_plan WHERE type='W') x
GROUP BY project_id, CAST(Time_Stamp AS date) ORDER BY snap DESC""", 15)

show("engineering_task_plan: latest snapshot per project", """
SELECT project_id, schedule_id, COUNT(*) AS rows_,
       CONVERT(varchar(20),MAX(Time_Stamp),120) AS latest
FROM core.engineering_task_plan GROUP BY project_id, schedule_id""")

show("engineering_task_plan: activity vs parent WBS progress mismatch (latest snapshot)", """
WITH latest AS (SELECT MAX(Time_Stamp) ts FROM core.engineering_task_plan),
sn AS (SELECT * FROM core.engineering_task_plan e CROSS JOIN latest l WHERE e.Time_Stamp = l.ts),
w AS (SELECT DISTINCT code, progress AS wbs_progress, weightage AS wbs_weight FROM sn WHERE type='W'),
a AS (SELECT parent, COUNT(*) AS acts,
             SUM(CASE WHEN progress >= 1 THEN 1 ELSE 0 END) AS acts_done,
             CAST(AVG(progress) AS decimal(6,4)) AS avg_act_progress
      FROM sn WHERE type='A' GROUP BY parent)
SELECT TOP 20 w.code, w.wbs_progress, w.wbs_weight, a.acts, a.acts_done, a.avg_act_progress,
       CAST(w.wbs_progress - a.avg_act_progress AS decimal(8,4)) AS delta
FROM w JOIN a ON a.parent = w.code
WHERE ABS(w.wbs_progress - a.avg_act_progress) > 0.01
ORDER BY ABS(w.wbs_progress - a.avg_act_progress) DESC""", 20)

show("RULE 3 CANDIDATE: all activities 100% but WBS < 100% (latest snapshot)", """
WITH latest AS (SELECT MAX(Time_Stamp) ts FROM core.engineering_task_plan),
sn AS (SELECT * FROM core.engineering_task_plan e CROSS JOIN latest l WHERE e.Time_Stamp = l.ts),
w AS (SELECT DISTINCT code, progress AS wbs_progress FROM sn WHERE type='W'),
a AS (SELECT parent, COUNT(*) AS acts, MIN(progress) AS min_act
      FROM sn WHERE type='A' GROUP BY parent)
SELECT w.code, w.wbs_progress, a.acts, a.min_act
FROM w JOIN a ON a.parent=w.code
WHERE a.min_act >= 1 AND w.wbs_progress < 1 ORDER BY w.code""", 25)

show("activity_taskplan_job_progress: parent_wbs_code list + task_type", """
SELECT task_type, COUNT(*) AS n, COUNT(DISTINCT parent_wbs_code) AS wbs,
       COUNT(DISTINCT well_id) AS wells FROM dbo.activity_taskplan_job_progress
GROUP BY task_type ORDER BY n DESC""")

show("activity_taskplan_job_progress: sample", """
SELECT TOP 6 well_id, task_code, LEFT(task_name,30) AS nm, task_type, parent_wbs_code, parent_id,
       progress, weightage, duration,
       CONVERT(varchar(10),planned_start_date,120) AS p_start,
       CONVERT(varchar(10),actual_end_date,120) AS a_end
FROM dbo.activity_taskplan_job_progress ORDER BY row_id""")

show("activity_task_plan(17.9M): rows per well - the duplication", """
SELECT well_id, COUNT_BIG(*) AS rows_, COUNT(DISTINCT task_code) AS task_codes,
       COUNT(DISTINCT parent_wbs_code) AS wbs,
       CAST(COUNT_BIG(*)*1.0/NULLIF(COUNT(DISTINCT task_code),0) AS decimal(12,1)) AS dup_factor
FROM dbo.activity_task_plan GROUP BY well_id ORDER BY rows_ DESC""", 15)
