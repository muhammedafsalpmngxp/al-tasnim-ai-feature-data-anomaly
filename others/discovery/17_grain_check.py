"""Phase 17 - is well.task_daily a TASK list or a DAILY LOG? This decides whether the
'completed=0 but progress>=1' finding (42,831 rows) is real or a grain error on my part."""
from dbx import connect, rows
cn = connect()


def show(t, sql, n=18):
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
        print("   " + " | ".join(str(r[h])[:38] for h in hdr))
    if len(rs) > n:
        print(f"   ... {len(rs)-n} more")


show("Grain: rows per (task_code, well_id) and per (task_code, well_id, ActionOn)", """
SELECT COUNT(*) AS rows_,
 COUNT(DISTINCT CONCAT(task_code,'|',well_id)) AS distinct_task_per_well,
 COUNT(DISTINCT CONCAT(task_code,'|',well_id,'|',CONVERT(varchar(10),ActionOn,120))) AS distinct_task_per_well_per_day,
 CAST(COUNT(*)*1.0/NULLIF(COUNT(DISTINCT CONCAT(task_code,'|',well_id)),0) AS decimal(8,2)) AS rows_per_task
FROM well.task_daily WHERE task_code IS NOT NULL AND well_id IS NOT NULL""")

show("Is (task_code, well_id, ActionOn) unique? (true daily-log grain)", """
SELECT COUNT(*) AS dup_groups, SUM(n)-COUNT(*) AS excess_rows, MAX(n) AS worst FROM (
 SELECT task_code, well_id, ActionOn, COUNT(*) AS n FROM well.task_daily
 WHERE task_code IS NOT NULL AND well_id IS NOT NULL
 GROUP BY task_code, well_id, ActionOn HAVING COUNT(*)>1) x""")

show("One task's full history (proves the shape)", """
SELECT TOP 12 CONVERT(varchar(10),ActionOn,120) AS action_on, task_code, well_id,
 progress, completed, daily_completed,
 CONVERT(varchar(10),actual_start,120) AS act_start, CONVERT(varchar(10),actual_end,120) AS act_end,
 CONVERT(varchar(10),target_end,120) AS tgt_end, remaining_duration
FROM well.task_daily WHERE task_code='HUP1780-29437' ORDER BY ActionOn, id""")

show("LATEST row per task: does the contradiction survive? (the honest ACT-001 number)", """
WITH latest AS (
 SELECT *, ROW_NUMBER() OVER (PARTITION BY task_code, well_id ORDER BY ActionOn DESC, id DESC) rn
 FROM well.task_daily WHERE task_code IS NOT NULL AND well_id IS NOT NULL)
SELECT COUNT(*) AS tasks_at_latest_row,
 SUM(CASE WHEN completed=0 AND progress>=1 THEN 1 ELSE 0 END) AS completed0_progress1,
 SUM(CASE WHEN completed=1 AND progress<1 THEN 1 ELSE 0 END) AS completed1_progress_lt1,
 SUM(CASE WHEN actual_end IS NOT NULL AND completed=0 THEN 1 ELSE 0 END) AS actual_end_not_completed,
 SUM(CASE WHEN completed=1 AND actual_end IS NULL THEN 1 ELSE 0 END) AS completed_no_actual_end,
 SUM(CASE WHEN progress>1 THEN 1 ELSE 0 END) AS progress_over_1,
 SUM(CASE WHEN actual_start>actual_end THEN 1 ELSE 0 END) AS start_after_end
FROM latest WHERE rn=1""")

show("Compare: per-ROW (what I reported) vs per-TASK-latest (the truth)", """
SELECT 'per ROW (all 107,484)' AS basis,
 SUM(CASE WHEN completed=0 AND progress>=1 THEN 1 ELSE 0 END) AS completed0_progress1,
 SUM(CASE WHEN actual_end IS NOT NULL AND completed=0 THEN 1 ELSE 0 END) AS actual_end_not_completed
FROM well.task_daily""")

show("Does a task ever go BACKWARDS in progress between days?", """
WITH s AS (
 SELECT task_code, well_id, ActionOn, progress,
        LAG(progress) OVER (PARTITION BY task_code, well_id ORDER BY ActionOn, id) AS prev_progress
 FROM well.task_daily WHERE task_code IS NOT NULL AND well_id IS NOT NULL)
SELECT COUNT(*) AS transitions,
 SUM(CASE WHEN progress < prev_progress THEN 1 ELSE 0 END) AS progress_WENT_BACKWARDS,
 COUNT(DISTINCT CASE WHEN progress < prev_progress THEN CONCAT(task_code,'|',well_id) END) AS tasks_affected,
 MIN(progress - prev_progress) AS worst_drop
FROM s WHERE prev_progress IS NOT NULL""")

show("Does `completed` ever revert from 1 back to 0?", """
WITH s AS (
 SELECT task_code, well_id, ActionOn, id, CAST(completed AS int) AS c,
        LAG(CAST(completed AS int)) OVER (PARTITION BY task_code, well_id ORDER BY ActionOn, id) AS prev_c
 FROM well.task_daily WHERE task_code IS NOT NULL AND well_id IS NOT NULL)
SELECT SUM(CASE WHEN prev_c=1 AND c=0 THEN 1 ELSE 0 END) AS completed_REVERTED,
 COUNT(DISTINCT CASE WHEN prev_c=1 AND c=0 THEN CONCAT(task_code,'|',well_id) END) AS tasks_affected
FROM s WHERE prev_c IS NOT NULL""")

show("Zero-length TARGET: is it really just a 1-day task? (duration=1 check)", """
SELECT bucket, COUNT(*) AS rows_, MIN(duration) AS dur_min, MAX(duration) AS dur_max FROM (
 SELECT CASE WHEN DATEDIFF(day,target_start,target_end)=0 THEN 'target_start = target_end'
             ELSE 'target span > 0 days' END AS bucket, duration
 FROM well.task_daily WHERE target_start IS NOT NULL AND target_end IS NOT NULL) x
GROUP BY bucket""")

show("The 16,471-day span outlier - who has it?", """
SELECT TOP 6 task_code, well_id,
 CONVERT(varchar(10),startDate,120) AS p6_start, CONVERT(varchar(10),endDate,120) AS p6_end,
 CONVERT(varchar(10),target_start,120) AS tgt_start, CONVERT(varchar(10),target_end,120) AS tgt_end,
 DATEDIFF(day,target_start,target_end) AS tgt_span, duration
FROM well.task_daily
WHERE DATEDIFF(day,target_start,target_end) > 3000 OR DATEDIFF(day,startDate,endDate) > 3000
ORDER BY DATEDIFF(day,target_start,target_end) DESC""")

show("TARGET inverted (46 rows) + far-future (3) - the real target defects", """
SELECT TOP 12 task_code, well_id,
 CONVERT(varchar(10),target_start,120) AS tgt_start, CONVERT(varchar(10),target_end,120) AS tgt_end,
 DATEDIFF(day,target_start,target_end) AS span, progress, completed
FROM well.task_daily WHERE target_start > target_end ORDER BY DATEDIFF(day,target_start,target_end)""")
