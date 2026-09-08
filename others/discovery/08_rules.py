"""Phase 8 - probe the four business-rule anomaly classes the user named, against real data."""
from dbx import connect, rows
cn = connect()


def show(t, sql, n=15):
    print(f"\n{'='*104}\n{t}\n{'='*104}")
    try:
        rs = rows(cn, sql)
    except Exception as e:  # noqa: BLE001
        print("   ERROR:", str(e)[:400]); return
    if not rs:
        print("   (no rows)"); return
    hdr = list(rs[0].keys()); print("   " + " | ".join(hdr))
    for r in rs[:n]:
        print("   " + " | ".join(str(r[h])[:38] for h in hdr))
    if len(rs) > n:
        print(f"   ... {len(rs)-n} more")


show("RULE 1: rig_off_date present (drilling done) but pre-rig_on milestones missing", """
SELECT COUNT(*) AS wells_with_rig_off,
  SUM(CASE WHEN pegged_date  IS NULL THEN 1 ELSE 0 END) AS no_pegged_date,
  SUM(CASE WHEN flaf_issue_date IS NULL THEN 1 ELSE 0 END) AS no_flaf_date,
  SUM(CASE WHEN rig_on_date  IS NULL THEN 1 ELSE 0 END) AS no_rig_on_date,
  SUM(CASE WHEN loc_start_date IS NULL THEN 1 ELSE 0 END) AS no_loc_start,
  SUM(CASE WHEN loc_finish_date IS NULL THEN 1 ELSE 0 END) AS no_loc_finish,
  SUM(CASE WHEN tie_in_ready_date IS NULL THEN 1 ELSE 0 END) AS no_tie_in_ready,
  SUM(CASE WHEN const_complete_date IS NULL THEN 1 ELSE 0 END) AS no_const_complete
FROM well.well_master WHERE rig_off_date IS NOT NULL""")

show("RULE 1b: rig_off present but rig_on NULL (impossible lifecycle)", """
SELECT well_id, ramz_id, CONVERT(varchar(10),ex_rig_on_date,120) AS ex_rig_on,
       CONVERT(varchar(10),rig_on_date,120) AS rig_on, CONVERT(varchar(10),rig_off_date,120) AS rig_off,
       CONVERT(varchar(10),pegged_date,120) AS pegged, CONVERT(varchar(10),flaf_issue_date,120) AS flaf,
       CONVERT(varchar(10),eng_completion_date,120) AS eng_compl
FROM well.well_master WHERE rig_off_date IS NOT NULL AND rig_on_date IS NULL""")

show("RULE 1c: hook-up complete but rig never off / never on", """
SELECT SUM(CASE WHEN rig_off_date IS NULL THEN 1 ELSE 0 END) AS complete_but_no_rig_off,
       SUM(CASE WHEN rig_on_date  IS NULL THEN 1 ELSE 0 END) AS complete_but_no_rig_on,
       SUM(CASE WHEN pegged_date  IS NULL THEN 1 ELSE 0 END) AS complete_but_no_pegged,
       SUM(CASE WHEN flaf_issue_date IS NULL THEN 1 ELSE 0 END) AS complete_but_no_flaf,
       COUNT(*) AS completed_wells
FROM well.well_master WHERE eng_completion_date IS NOT NULL""")

show("RULE 2: date-order violations in well_master (lifecycle sequence, sec 7)", """
SELECT
 SUM(CASE WHEN rig_on_date > rig_off_date THEN 1 ELSE 0 END) AS rig_on_AFTER_rig_off,
 SUM(CASE WHEN pegged_date > rig_on_date THEN 1 ELSE 0 END) AS pegged_AFTER_rig_on,
 SUM(CASE WHEN flaf_issue_date > rig_on_date THEN 1 ELSE 0 END) AS flaf_AFTER_rig_on,
 SUM(CASE WHEN eng_completion_date < rig_off_date THEN 1 ELSE 0 END) AS hookup_BEFORE_rig_off,
 SUM(CASE WHEN eng_completion_date < rig_on_date THEN 1 ELSE 0 END) AS hookup_BEFORE_rig_on,
 SUM(CASE WHEN loc_start_date > loc_finish_date THEN 1 ELSE 0 END) AS loc_start_AFTER_finish,
 SUM(CASE WHEN ex_rig_on_date > ex_rig_off_date THEN 1 ELSE 0 END) AS ex_on_AFTER_ex_off,
 SUM(CASE WHEN hoist_on_date > hoist_off_date THEN 1 ELSE 0 END) AS hoist_on_AFTER_off,
 SUM(CASE WHEN const_complete_date > rig_on_date THEN 1 ELSE 0 END) AS const_complete_AFTER_rig_on
FROM well.well_master""")

show("RULE 2b: actual dates in the FUTURE (impossible actuals)", """
SELECT
 SUM(CASE WHEN rig_on_date  > CAST(GETDATE() AS date) THEN 1 ELSE 0 END) AS rig_on_future,
 SUM(CASE WHEN rig_off_date > CAST(GETDATE() AS date) THEN 1 ELSE 0 END) AS rig_off_future,
 SUM(CASE WHEN pegged_date  > CAST(GETDATE() AS date) THEN 1 ELSE 0 END) AS pegged_future,
 SUM(CASE WHEN flaf_issue_date > CAST(GETDATE() AS date) THEN 1 ELSE 0 END) AS flaf_future,
 SUM(CASE WHEN eng_completion_date > CAST(GETDATE() AS date) THEN 1 ELSE 0 END) AS hookup_future,
 SUM(CASE WHEN location_po_recvd_date > CAST(GETDATE() AS date) THEN 1 ELSE 0 END) AS loc_po_future,
 SUM(CASE WHEN const_complete_date > CAST(GETDATE() AS date) THEN 1 ELSE 0 END) AS const_compl_future,
 CONVERT(varchar(10), GETDATE(), 120) AS today
FROM well.well_master""")

show("RULE 2c: duration norms - drilling span (outlier detection input)", """
SELECT COUNT(*) AS n,
 MIN(DATEDIFF(day,rig_on_date,rig_off_date)) AS drill_min,
 MAX(DATEDIFF(day,rig_on_date,rig_off_date)) AS drill_max,
 AVG(DATEDIFF(day,rig_on_date,rig_off_date)*1.0) AS drill_avg,
 SUM(CASE WHEN DATEDIFF(day,rig_on_date,rig_off_date)=0 THEN 1 ELSE 0 END) AS zero_day_drill,
 SUM(CASE WHEN DATEDIFF(day,rig_on_date,rig_off_date)<0 THEN 1 ELSE 0 END) AS negative_drill,
 SUM(CASE WHEN DATEDIFF(day,rig_on_date,rig_off_date)>200 THEN 1 ELSE 0 END) AS over_200_days
FROM well.well_master WHERE rig_on_date IS NOT NULL AND rig_off_date IS NOT NULL""")

show("RULE 2d: hook-up lag after rig-off (deadline = rig_off + 2 days)", """
SELECT COUNT(*) AS n,
 MIN(DATEDIFF(day,rig_off_date,eng_completion_date)) AS lag_min,
 MAX(DATEDIFF(day,rig_off_date,eng_completion_date)) AS lag_max,
 AVG(DATEDIFF(day,rig_off_date,eng_completion_date)*1.0) AS lag_avg,
 SUM(CASE WHEN DATEDIFF(day,rig_off_date,eng_completion_date)<0 THEN 1 ELSE 0 END) AS NEGATIVE_lag,
 SUM(CASE WHEN DATEDIFF(day,rig_off_date,eng_completion_date)>2 THEN 1 ELSE 0 END) AS missed_2day_deadline
FROM well.well_master WHERE rig_off_date IS NOT NULL AND eng_completion_date IS NOT NULL""")

show("RULE 2e: pegging / FLAF lead time vs ex_rig_on (norms -60d / -90d)", """
SELECT
 SUM(CASE WHEN pegged_date > DATEADD(day,-60,ex_rig_on_date) THEN 1 ELSE 0 END) AS pegging_late,
 SUM(CASE WHEN pegged_date IS NULL AND CAST(GETDATE() AS date) > DATEADD(day,-60,ex_rig_on_date) THEN 1 ELSE 0 END) AS pegging_missing_past_due,
 SUM(CASE WHEN flaf_issue_date > DATEADD(day,-90,ex_rig_on_date) THEN 1 ELSE 0 END) AS flaf_late,
 SUM(CASE WHEN flaf_issue_date IS NULL AND CAST(GETDATE() AS date) > DATEADD(day,-90,ex_rig_on_date) THEN 1 ELSE 0 END) AS flaf_missing_past_due,
 COUNT(*) AS total_wells FROM well.well_master""")

show("RULE 3/4: task_daily - completed flag vs progress contradiction", """
SELECT
 SUM(CASE WHEN completed=1 AND progress<1 THEN 1 ELSE 0 END) AS completed_but_progress_lt_1,
 SUM(CASE WHEN completed=0 AND progress>=1 THEN 1 ELSE 0 END) AS not_completed_but_progress_1,
 SUM(CASE WHEN completed=1 AND actual_end IS NULL THEN 1 ELSE 0 END) AS completed_but_no_actual_end,
 SUM(CASE WHEN actual_end IS NOT NULL AND completed=0 THEN 1 ELSE 0 END) AS actual_end_but_not_completed,
 SUM(CASE WHEN progress>1 THEN 1 ELSE 0 END) AS progress_over_1,
 SUM(CASE WHEN progress<0 THEN 1 ELSE 0 END) AS progress_negative,
 SUM(CASE WHEN actual_start>actual_end THEN 1 ELSE 0 END) AS actual_start_after_end,
 SUM(CASE WHEN actual_end IS NOT NULL AND actual_start IS NULL THEN 1 ELSE 0 END) AS end_without_start,
 SUM(CASE WHEN target_start>target_end THEN 1 ELSE 0 END) AS target_start_after_end,
 COUNT(*) AS rows_ FROM well.task_daily""")

show("RULE 3/4: task_daily ActionOn sanity + daily payload", """
SELECT SUM(CASE WHEN ActionOn IS NULL THEN 1 ELSE 0 END) AS actionon_null,
 SUM(CASE WHEN ActionOn > CAST(GETDATE() AS date) THEN 1 ELSE 0 END) AS actionon_future,
 SUM(CASE WHEN ActionOn < '2020-01-01' THEN 1 ELSE 0 END) AS actionon_ancient,
 SUM(CASE WHEN daily_completed=1 AND daily_actual_quantity IS NULL THEN 1 ELSE 0 END) AS daily_done_no_qty,
 SUM(CASE WHEN daily_actual_hours<0 OR daily_actual_quantity<0 THEN 1 ELSE 0 END) AS negative_daily,
 SUM(CASE WHEN well_id IS NULL THEN 1 ELSE 0 END) AS well_id_null,
 COUNT(*) AS rows_ FROM well.task_daily""")

show("RULE 1/3: wells with rig OFF but task_daily activities still incomplete", """
WITH t AS (SELECT well_id, COUNT(*) AS tasks,
             SUM(CASE WHEN ISNULL(completed,0)=1 THEN 1 ELSE 0 END) AS done
           FROM well.task_daily WHERE well_id IS NOT NULL GROUP BY well_id)
SELECT COUNT(*) AS wells, SUM(t.tasks) AS tasks, SUM(t.tasks-t.done) AS open_tasks
FROM well.well_master m JOIN t ON t.well_id=m.well_id
WHERE m.rig_off_date IS NOT NULL AND t.done < t.tasks""")

show("RULE 1/3: worst offenders - rig off long ago, tasks still open", """
WITH t AS (SELECT well_id, COUNT(*) AS tasks,
             SUM(CASE WHEN ISNULL(completed,0)=1 THEN 1 ELSE 0 END) AS done
           FROM well.task_daily WHERE well_id IS NOT NULL GROUP BY well_id)
SELECT TOP 12 m.well_id, m.ramz_id, CONVERT(varchar(10),m.rig_off_date,120) AS rig_off,
       DATEDIFF(day,m.rig_off_date,GETDATE()) AS days_since_rig_off,
       t.tasks, t.done, t.tasks-t.done AS open_tasks,
       CONVERT(varchar(10),m.eng_completion_date,120) AS hookup_done
FROM well.well_master m JOIN t ON t.well_id=m.well_id
WHERE m.rig_off_date IS NOT NULL AND t.done < t.tasks
ORDER BY days_since_rig_off DESC""")
