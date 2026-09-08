"""Phase 16 - P6-generated dates vs planner-owned TARGET dates.

The business says: startDate/endDate come from P6 software; target_start/target_end are set
by the human planner and are what the business actually watches. This decides which pair is
the baseline for every delay check - and whether two earlier findings were false positives.
"""
from dbx import connect, rows
cn = connect()


def show(t, sql, n=20):
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
        print("   " + " | ".join(str(r[h])[:40] for h in hdr))
    if len(rs) > n:
        print(f"   ... {len(rs)-n} more")


show("Q1  Which date pair does the `duration` column actually describe?", """
SELECT COUNT(*) AS rows_,
 SUM(CASE WHEN ABS(duration - (DATEDIFF(day,startDate,endDate)+1)) <= 0.001 THEN 1 ELSE 0 END) AS matches_P6_inclusive,
 SUM(CASE WHEN ABS(duration - DATEDIFF(day,startDate,endDate)) <= 0.001 THEN 1 ELSE 0 END) AS matches_P6_exclusive,
 SUM(CASE WHEN ABS(duration - (DATEDIFF(day,target_start,target_end)+1)) <= 0.001 THEN 1 ELSE 0 END) AS matches_TARGET_inclusive,
 SUM(CASE WHEN ABS(duration - DATEDIFF(day,target_start,target_end)) <= 0.001 THEN 1 ELSE 0 END) AS matches_TARGET_exclusive
FROM well.task_daily
WHERE startDate IS NOT NULL AND endDate IS NOT NULL
  AND target_start IS NOT NULL AND target_end IS NOT NULL AND duration IS NOT NULL""")

show("Q2  Do P6 dates and TARGET dates agree at all?", """
SELECT COUNT(*) AS rows_,
 SUM(CASE WHEN startDate = target_start AND endDate = target_end THEN 1 ELSE 0 END) AS identical_both,
 SUM(CASE WHEN startDate = target_start THEN 1 ELSE 0 END) AS start_identical,
 SUM(CASE WHEN endDate = target_end THEN 1 ELSE 0 END) AS end_identical,
 SUM(CASE WHEN target_start > startDate THEN 1 ELSE 0 END) AS target_start_LATER_than_p6,
 SUM(CASE WHEN target_start < startDate THEN 1 ELSE 0 END) AS target_start_EARLIER_than_p6,
 SUM(CASE WHEN target_end > endDate THEN 1 ELSE 0 END) AS target_end_LATER_than_p6,
 SUM(CASE WHEN target_end < endDate THEN 1 ELSE 0 END) AS target_end_EARLIER_than_p6
FROM well.task_daily
WHERE startDate IS NOT NULL AND endDate IS NOT NULL
  AND target_start IS NOT NULL AND target_end IS NOT NULL""")

show("Q3  Span comparison: P6 span vs TARGET span (is target a re-plan or a copy?)", """
SELECT COUNT(*) AS rows_,
 MIN(DATEDIFF(day,startDate,endDate)) AS p6_min, MAX(DATEDIFF(day,startDate,endDate)) AS p6_max,
 CAST(AVG(DATEDIFF(day,startDate,endDate)*1.0) AS decimal(8,2)) AS p6_avg,
 MIN(DATEDIFF(day,target_start,target_end)) AS tgt_min, MAX(DATEDIFF(day,target_start,target_end)) AS tgt_max,
 CAST(AVG(DATEDIFF(day,target_start,target_end)*1.0) AS decimal(8,2)) AS tgt_avg,
 SUM(CASE WHEN DATEDIFF(day,startDate,endDate)=0 THEN 1 ELSE 0 END) AS p6_zero_len,
 SUM(CASE WHEN DATEDIFF(day,target_start,target_end)=0 THEN 1 ELSE 0 END) AS TARGET_zero_len
FROM well.task_daily
WHERE startDate IS NOT NULL AND endDate IS NOT NULL
  AND target_start IS NOT NULL AND target_end IS NOT NULL""")

show("Q4  Zero-length: is it P6, target, or both? (was this a false positive?)", """
SELECT bucket, COUNT(*) AS rows_ FROM (
 SELECT CASE
   WHEN DATEDIFF(day,startDate,endDate)=0 AND DATEDIFF(day,target_start,target_end)=0 THEN 'BOTH zero-length'
   WHEN DATEDIFF(day,target_start,target_end)=0 THEN 'TARGET only zero-length'
   WHEN DATEDIFF(day,startDate,endDate)=0 THEN 'P6 only zero-length'
   ELSE 'neither' END AS bucket
 FROM well.task_daily
 WHERE startDate IS NOT NULL AND endDate IS NOT NULL
   AND target_start IS NOT NULL AND target_end IS NOT NULL) x
GROUP BY bucket ORDER BY rows_ DESC""")

show("Q5  Sample rows: see all four date pairs side by side", """
SELECT TOP 15 task_code,
 CONVERT(varchar(10),startDate,120) AS p6_start, CONVERT(varchar(10),endDate,120) AS p6_end,
 CONVERT(varchar(10),target_start,120) AS tgt_start, CONVERT(varchar(10),target_end,120) AS tgt_end,
 CONVERT(varchar(10),committed_start,120) AS com_start, CONVERT(varchar(10),committed_end,120) AS com_end,
 CONVERT(varchar(10),actual_start,120) AS act_start, CONVERT(varchar(10),actual_end,120) AS act_end,
 duration, progress, completed
FROM well.task_daily
WHERE target_start IS NOT NULL AND actual_end IS NOT NULL AND committed_start IS NOT NULL
ORDER BY id""")

show("Q6  What is committed_start/end? (52.9% populated - a third planning layer?)", """
SELECT COUNT(*) AS rows_,
 SUM(CASE WHEN committed_start = target_start AND committed_end = target_end THEN 1 ELSE 0 END) AS same_as_TARGET,
 SUM(CASE WHEN committed_start = startDate AND committed_end = endDate THEN 1 ELSE 0 END) AS same_as_P6,
 SUM(CASE WHEN committed_end > target_end THEN 1 ELSE 0 END) AS committed_LATER_than_target,
 SUM(CASE WHEN committed_end < target_end THEN 1 ELSE 0 END) AS committed_EARLIER_than_target,
 SUM(CASE WHEN committed_start > committed_end THEN 1 ELSE 0 END) AS committed_INVERTED
FROM well.task_daily
WHERE committed_start IS NOT NULL AND committed_end IS NOT NULL
  AND target_start IS NOT NULL AND target_end IS NOT NULL""")

show("Q7  DELAY measured against TARGET (the authoritative baseline)", """
SELECT COUNT(*) AS finished_tasks,
 SUM(CASE WHEN actual_end > target_end THEN 1 ELSE 0 END) AS LATE_vs_target,
 SUM(CASE WHEN actual_end = target_end THEN 1 ELSE 0 END) AS on_target,
 SUM(CASE WHEN actual_end < target_end THEN 1 ELSE 0 END) AS EARLY_vs_target,
 MAX(DATEDIFF(day,target_end,actual_end)) AS worst_days_late,
 MIN(DATEDIFF(day,target_end,actual_end)) AS most_days_early,
 CAST(AVG(DATEDIFF(day,target_end,actual_end)*1.0) AS decimal(8,2)) AS avg_signed_variance
FROM well.task_daily
WHERE actual_end IS NOT NULL AND target_end IS NOT NULL""")

show("Q8  Same delay measured against P6 - how different is the answer?", """
SELECT COUNT(*) AS finished_tasks,
 SUM(CASE WHEN actual_end > endDate THEN 1 ELSE 0 END) AS LATE_vs_p6,
 SUM(CASE WHEN actual_end < endDate THEN 1 ELSE 0 END) AS EARLY_vs_p6,
 CAST(AVG(DATEDIFF(day,endDate,actual_end)*1.0) AS decimal(8,2)) AS avg_signed_variance_p6
FROM well.task_daily
WHERE actual_end IS NOT NULL AND endDate IS NOT NULL""")

show("Q9  TARGET date integrity (the planner-owned columns - our real subject)", """
SELECT COUNT(*) AS rows_,
 SUM(CASE WHEN target_start IS NULL AND target_end IS NOT NULL THEN 1 ELSE 0 END) AS end_without_start,
 SUM(CASE WHEN target_end IS NULL AND target_start IS NOT NULL THEN 1 ELSE 0 END) AS start_without_end,
 SUM(CASE WHEN target_start IS NULL AND target_end IS NULL THEN 1 ELSE 0 END) AS BOTH_MISSING,
 SUM(CASE WHEN target_start > target_end THEN 1 ELSE 0 END) AS INVERTED,
 SUM(CASE WHEN target_end < '2020-01-01' THEN 1 ELSE 0 END) AS ancient,
 SUM(CASE WHEN target_start > DATEADD(year,3,GETDATE()) THEN 1 ELSE 0 END) AS more_than_3y_future
FROM well.task_daily""")

show("Q10 Target vs the well's master date: task planned AFTER the rig is due on", """
SELECT COUNT(*) AS tasks_compared,
 SUM(CASE WHEN t.target_end > m.ex_rig_on_date THEN 1 ELSE 0 END) AS target_end_after_ex_rig_on,
 COUNT(DISTINCT CASE WHEN t.target_end > m.ex_rig_on_date THEN t.well_id END) AS wells_affected,
 MAX(DATEDIFF(day, m.ex_rig_on_date, t.target_end)) AS worst_days_past_master_date
FROM well.task_daily t JOIN well.well_master m ON m.well_id = t.well_id
WHERE t.target_end IS NOT NULL AND m.ex_rig_on_date IS NOT NULL""")

show("Q11 core.engineering_task_plan carries BOTH pairs too - same question there", """
SELECT COUNT(*) AS rows_,
 SUM(CASE WHEN start_date IS NOT NULL AND end_date IS NOT NULL THEN 1 ELSE 0 END) AS has_p6_pair,
 SUM(CASE WHEN target_start IS NOT NULL AND target_end IS NOT NULL THEN 1 ELSE 0 END) AS has_target_pair,
 SUM(CASE WHEN committed_start IS NOT NULL AND committed_end IS NOT NULL THEN 1 ELSE 0 END) AS has_committed_pair,
 SUM(CASE WHEN CAST(target_start AS date)=CAST(start_date AS date)
            AND CAST(target_end AS date)=CAST(end_date AS date) THEN 1 ELSE 0 END) AS target_identical_to_p6
FROM core.engineering_task_plan""")

show("Q12 Which planning columns are populated across the three task tables", """
SELECT src, col, populated, total, CAST(100.0*populated/NULLIF(total,0) AS decimal(5,1)) AS pct FROM (
 SELECT 'task_daily' AS src,'startDate/endDate (P6)' AS col,
        SUM(CASE WHEN startDate IS NOT NULL AND endDate IS NOT NULL THEN 1 ELSE 0 END) AS populated,
        COUNT(*) AS total FROM well.task_daily
 UNION ALL SELECT 'task_daily','target_start/target_end (PLANNER)',
        SUM(CASE WHEN target_start IS NOT NULL AND target_end IS NOT NULL THEN 1 ELSE 0 END), COUNT(*) FROM well.task_daily
 UNION ALL SELECT 'task_daily','committed_start/end',
        SUM(CASE WHEN committed_start IS NOT NULL AND committed_end IS NOT NULL THEN 1 ELSE 0 END), COUNT(*) FROM well.task_daily
 UNION ALL SELECT 'task_daily','actual_start/actual_end',
        SUM(CASE WHEN actual_start IS NOT NULL AND actual_end IS NOT NULL THEN 1 ELSE 0 END), COUNT(*) FROM well.task_daily
 UNION ALL SELECT 'job_progress','planned_start/end (P6)',
        SUM(CASE WHEN planned_start_date IS NOT NULL AND planned_end_date IS NOT NULL THEN 1 ELSE 0 END), COUNT(*)
        FROM dbo.activity_taskplan_job_progress
 UNION ALL SELECT 'job_progress','target_start/end (PLANNER)',
        SUM(CASE WHEN target_start_date IS NOT NULL AND CAST(target_start_date AS date)<>'1900-01-01' THEN 1 ELSE 0 END), COUNT(*)
        FROM dbo.activity_taskplan_job_progress
) x ORDER BY src, col""", 15)
