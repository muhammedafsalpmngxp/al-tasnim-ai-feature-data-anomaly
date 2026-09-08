"""Phase 20 - well-centric anomaly rollup, cumulative-progress monotonicity,
reference-data decay, and the business's own two queries."""
from dbx import connect, rows
cn = connect()


def show(t, sql, n=20):
    print(f"\n{'-'*114}\n{t}\n{'-'*114}")
    try:
        rs = rows(cn, sql)
    except Exception as e:  # noqa: BLE001
        print("   ERROR:", str(e)[:300]); return
    if not rs:
        print("   (no rows)"); return
    print("   " + " | ".join(rs[0].keys()))
    for r in rs[:n]:
        print("   " + " | ".join(str(v)[:34] for v in r.values()))
    if len(rs) > n:
        print(f"   ... {len(rs)-n} more")


print("#" * 114)
print("# A. REFERENCE-DATA DECAY  (ref.crew_type, from the screenshot)")
print("#" * 114)

show("Completeness pattern - reference rows decaying to empty", """
SELECT pattern, COUNT(*) AS crew_types, MIN(crew_type_id) AS first_id, MAX(crew_type_id) AS last_id FROM (
 SELECT crew_type_id,
   CASE WHEN crew_type_code IS NOT NULL AND crew_type_name IS NOT NULL AND Crew_group_code IS NOT NULL THEN '1. fully populated'
        WHEN crew_type_code IS NOT NULL AND crew_type_name IS NOT NULL AND Crew_group_code IS NULL     THEN '2. no group code'
        WHEN crew_type_code IS NOT NULL AND crew_type_name IS NULL                                     THEN '3. code only, NO NAME'
        WHEN crew_type_code IS NULL AND Crew_group_code IS NOT NULL                                    THEN '4. GROUP CODE ONLY - unusable'
        ELSE '5. empty shell' END AS pattern
 FROM ref.crew_type) x GROUP BY pattern ORDER BY pattern""")

show("Is Crew_group_code just crew_type_code with a dash? where does it disagree?", """
SELECT crew_type_id, crew_type_code, Crew_group_code,
  REPLACE(Crew_group_code,'-','') AS group_undashed,
  CASE WHEN crew_type_code = REPLACE(Crew_group_code,'-','') THEN 'MATCH' ELSE 'MISMATCH' END AS verdict
FROM ref.crew_type
WHERE crew_type_code IS NOT NULL AND Crew_group_code IS NOT NULL
  AND crew_type_code <> REPLACE(Crew_group_code,'-','')""")

show("...and the overall match rate", """
SELECT COUNT(*) AS both_present,
 SUM(CASE WHEN crew_type_code = REPLACE(Crew_group_code,'-','') THEN 1 ELSE 0 END) AS match_,
 SUM(CASE WHEN crew_type_code <> REPLACE(Crew_group_code,'-','') THEN 1 ELSE 0 END) AS MISMATCH
FROM ref.crew_type WHERE crew_type_code IS NOT NULL AND Crew_group_code IS NOT NULL""")

show("Are the unusable crew_types actually referenced by real work?", """
SELECT COUNT(*) AS unusable_crew_types,
 SUM(CASE WHEN EXISTS (SELECT 1 FROM well.task_daily t WHERE t.crew_type_id=ct.crew_type_id)
          THEN 1 ELSE 0 END) AS referenced_by_task_daily,
 SUM(CASE WHEN EXISTS (SELECT 1 FROM ref.crew c WHERE c.crew_type_id=ct.crew_type_id)
          THEN 1 ELSE 0 END) AS referenced_by_crew
FROM ref.crew_type ct WHERE ct.crew_type_code IS NULL""")

print("\n" + "#" * 114)
print("# B. CUMULATIVE PROGRESS MUST NEVER DECREASE  (deduped well_progress)")
print("#" * 114)

show("Progress reversals per measure, per well, week over week", """
WITH d AS (
 SELECT *, ROW_NUMBER() OVER (PARTITION BY well_id, week_number ORDER BY progress_id DESC) rn
 FROM well.well_progress WHERE week_number > '2000-01-01'),
s AS (
 SELECT well_id, week_number, overall_progress, construction_progress, location_prep_progress,
        prev_week_cum_progress,
        LAG(overall_progress)      OVER (PARTITION BY well_id ORDER BY week_number) AS p_overall,
        LAG(construction_progress) OVER (PARTITION BY well_id ORDER BY week_number) AS p_constr,
        LAG(location_prep_progress)OVER (PARTITION BY well_id ORDER BY week_number) AS p_locprep
 FROM d WHERE rn=1)
SELECT
 SUM(CASE WHEN overall_progress       < p_overall THEN 1 ELSE 0 END) AS overall_DECREASED,
 COUNT(DISTINCT CASE WHEN overall_progress < p_overall THEN well_id END) AS wells_overall,
 SUM(CASE WHEN construction_progress  < p_constr  THEN 1 ELSE 0 END) AS construction_DECREASED,
 COUNT(DISTINCT CASE WHEN construction_progress < p_constr THEN well_id END) AS wells_constr,
 SUM(CASE WHEN location_prep_progress < p_locprep THEN 1 ELSE 0 END) AS locprep_DECREASED,
 COUNT(DISTINCT CASE WHEN location_prep_progress < p_locprep THEN well_id END) AS wells_locprep,
 COUNT(*) AS week_transitions
FROM s WHERE p_overall IS NOT NULL""")

show("Worst reversals - overall_progress going backwards", """
WITH d AS (
 SELECT *, ROW_NUMBER() OVER (PARTITION BY well_id, week_number ORDER BY progress_id DESC) rn
 FROM well.well_progress WHERE week_number > '2000-01-01'),
s AS (
 SELECT well_id, well_name, week_number, overall_progress,
        LAG(overall_progress) OVER (PARTITION BY well_id ORDER BY week_number) AS prev_progress,
        LAG(week_number)      OVER (PARTITION BY well_id ORDER BY week_number) AS prev_week
 FROM d WHERE rn=1)
SELECT TOP 12 well_id, well_name, CONVERT(varchar(10),prev_week,120) AS prev_week,
 prev_progress, CONVERT(varchar(10),week_number,120) AS this_week, overall_progress,
 CAST(overall_progress - prev_progress AS decimal(6,3)) AS drop_
FROM s WHERE overall_progress < prev_progress ORDER BY overall_progress - prev_progress""")

show("Internal contradiction: prev_week_cum_progress vs the actual previous row", """
WITH d AS (
 SELECT *, ROW_NUMBER() OVER (PARTITION BY well_id, week_number ORDER BY progress_id DESC) rn
 FROM well.well_progress WHERE week_number > '2000-01-01'),
s AS (
 SELECT well_id, week_number, prev_week_cum_progress, overall_progress,
        LAG(overall_progress) OVER (PARTITION BY well_id ORDER BY week_number) AS actual_prev
 FROM d WHERE rn=1)
SELECT COUNT(*) AS comparable,
 SUM(CASE WHEN ABS(prev_week_cum_progress - actual_prev) > 0.005 THEN 1 ELSE 0 END) AS stored_prev_WRONG,
 SUM(CASE WHEN prev_week_cum_progress > overall_progress THEN 1 ELSE 0 END) AS prev_GREATER_than_current
FROM s WHERE actual_prev IS NOT NULL AND prev_week_cum_progress IS NOT NULL""")

show("Progress vs lifecycle: completed well not at 100%, or 100% with no completion date", """
WITH d AS (
 SELECT *, ROW_NUMBER() OVER (PARTITION BY well_id ORDER BY week_number DESC, progress_id DESC) rn
 FROM well.well_progress WHERE week_number > '2000-01-01')
SELECT COUNT(*) AS wells,
 SUM(CASE WHEN m.eng_completion_date IS NOT NULL AND d.overall_progress < 1 THEN 1 ELSE 0 END) AS COMPLETED_but_under_100,
 SUM(CASE WHEN m.eng_completion_date IS NULL AND d.overall_progress >= 1 THEN 1 ELSE 0 END) AS AT_100_but_not_completed,
 SUM(CASE WHEN m.rig_off_date IS NOT NULL AND d.construction_progress < 1 THEN 1 ELSE 0 END) AS RIG_OFF_but_construction_under_100
FROM d JOIN well.well_master m ON m.well_id=d.well_id WHERE d.rn=1""")

print("\n" + "#" * 114)
print("# C. THE BUSINESS'S QUERY 1 - incomplete wells with no task activity")
print("#" * 114)

show("Incomplete wells: task activity status", """
SELECT task_activity_status, COUNT(*) AS wells,
 SUM(CASE WHEN ex_rig_on_date < CAST(GETDATE() AS date) THEN 1 ELSE 0 END) AS master_date_ALREADY_PASSED,
 SUM(CASE WHEN rig_on_date IS NOT NULL THEN 1 ELSE 0 END) AS rig_already_on
FROM (
 SELECT wm.well_id, wm.ex_rig_on_date, wm.rig_on_date,
   CASE WHEN COUNT(td.id)=0 THEN 'NO TASK ACTIVITIES' ELSE 'HAS TASK ACTIVITIES' END AS task_activity_status
 FROM well.well_master wm LEFT JOIN well.task_daily td ON td.well_id = wm.well_id
 WHERE wm.eng_completion_date IS NULL
 GROUP BY wm.well_id, wm.project_id, wm.ex_rig_on_date, wm.rig_on_date, wm.eng_completion_date) x
GROUP BY task_activity_status""")

show("The severe cases: no tasks AND the master date has already passed", """
SELECT TOP 15 wm.well_id, wm.ramz_id,
 CONVERT(varchar(10),wm.ex_rig_on_date,120) AS ex_rig_on,
 DATEDIFF(day, wm.ex_rig_on_date, GETDATE()) AS days_past_master_date,
 CONVERT(varchar(10),wm.rig_on_date,120) AS rig_on,
 CONVERT(varchar(10),wm.pegged_date,120) AS pegged,
 CONVERT(varchar(10),wm.flaf_issue_date,120) AS flaf,
 COUNT(td.id) AS task_rows
FROM well.well_master wm LEFT JOIN well.task_daily td ON td.well_id = wm.well_id
WHERE wm.eng_completion_date IS NULL
GROUP BY wm.well_id, wm.ramz_id, wm.ex_rig_on_date, wm.rig_on_date, wm.pegged_date, wm.flaf_issue_date
HAVING COUNT(td.id)=0 AND wm.ex_rig_on_date < CAST(GETDATE() AS date)
ORDER BY DATEDIFF(day, wm.ex_rig_on_date, GETDATE()) DESC""")

print("\n" + "#" * 114)
print("# D. THE BUSINESS'S QUERY 2 - duplicate depth, generalised past well 33776")
print("#" * 114)

show("Well 33776 as the business queried it", """
SELECT td.well_id, COUNT(*) AS total_task_daily_rows,
 COUNT(DISTINCT td.task_code) AS distinct_task_codes,
 COUNT(DISTINCT CONCAT(CAST(td.well_id AS varchar(20)),'|',td.task_code)) AS logical_tasks,
 MIN(td.ActionOn) AS first_action_date, MAX(td.ActionOn) AS latest_action_date,
 CAST(COUNT(*)*1.0/NULLIF(COUNT(DISTINCT td.task_code),0) AS decimal(8,2)) AS rows_per_task
FROM well.task_daily td WHERE td.well_id = 33776 GROUP BY td.well_id""")

show("Same shape for every well - worst duplication factors", """
SELECT TOP 15 td.well_id, COUNT(*) AS rows_,
 COUNT(DISTINCT td.task_code) AS distinct_task_codes,
 COUNT(DISTINCT CONCAT(CAST(td.well_id AS varchar(20)),'|',td.task_code,'|',
        CONVERT(varchar(10),td.ActionOn,120))) AS task_days,
 CAST(COUNT(*)*1.0/NULLIF(COUNT(DISTINCT CONCAT(CAST(td.well_id AS varchar(20)),'|',td.task_code,'|',
        CONVERT(varchar(10),td.ActionOn,120))),0) AS decimal(8,2)) AS TRUE_dup_factor,
 DATEDIFF(day, MIN(td.ActionOn), MAX(td.ActionOn))+1 AS window_days
FROM well.task_daily td WHERE td.well_id IS NOT NULL GROUP BY td.well_id
HAVING COUNT(*) > COUNT(DISTINCT CONCAT(CAST(td.well_id AS varchar(20)),'|',td.task_code,'|',
        CONVERT(varchar(10),td.ActionOn,120)))
ORDER BY COUNT(*)*1.0/NULLIF(COUNT(DISTINCT CONCAT(CAST(td.well_id AS varchar(20)),'|',td.task_code,'|',
        CONVERT(varchar(10),td.ActionOn,120))),0) DESC""")

show("Are the same-day duplicates identical, or do they disagree?", """
SELECT COUNT(*) AS dup_groups,
 SUM(CASE WHEN payloads > 1 THEN 1 ELSE 0 END) AS groups_with_DIFFERENT_values,
 SUM(CASE WHEN payloads = 1 THEN 1 ELSE 0 END) AS groups_identical
FROM (
 SELECT well_id, task_code, ActionOn, COUNT(*) AS n,
        COUNT(DISTINCT CONCAT(progress,'|',completed,'|',actual_start,'|',actual_end)) AS payloads
 FROM well.task_daily WHERE well_id IS NOT NULL AND task_code IS NOT NULL
 GROUP BY well_id, task_code, ActionOn HAVING COUNT(*)>1) x""")

print("\n" + "#" * 114)
print("# E. WELL-CENTRIC ANOMALY SCORECARD  (the report's primary view)")
print("#" * 114)

show("Anomaly count per well across families - top offenders", """
WITH t AS (
 SELECT *, ROW_NUMBER() OVER (PARTITION BY task_code, well_id ORDER BY ActionOn DESC, id DESC) rn
 FROM well.task_daily WHERE well_id IS NOT NULL AND task_code IS NOT NULL),
task_flags AS (
 SELECT well_id,
  SUM(CASE WHEN actual_start > actual_end THEN 1 ELSE 0 END) AS a_date_inverted,
  SUM(CASE WHEN target_start > target_end THEN 1 ELSE 0 END) AS a_target_inverted,
  SUM(CASE WHEN completed=0 AND progress>=1 THEN 1 ELSE 0 END) AS a_progress_vs_flag,
  SUM(CASE WHEN progress>1 OR progress<0 THEN 1 ELSE 0 END) AS a_progress_range,
  COUNT(*) AS tasks
 FROM t WHERE rn=1 GROUP BY well_id),
map AS (
 SELECT well_id,
  SUM(CASE WHEN m.activity_id IS NULL THEN 1 ELSE 0 END) AS a_unmapped_tasks
 FROM (SELECT DISTINCT well_id, task_code,
         LEFT(task_code, NULLIF(CHARINDEX('-',task_code),0)-1) AS activity_id
       FROM well.task_daily WHERE well_id IS NOT NULL AND task_code IS NOT NULL) a
 LEFT JOIN dbo.activity_master_mapping m ON m.activity_id = a.activity_id
 GROUP BY well_id)
SELECT TOP 15 wm.well_id, ISNULL(wm.ramz_id,'(no ramz_id)') AS ramz_id,
 CASE WHEN wm.pegged_date IS NULL AND CAST(GETDATE() AS date) > DATEADD(day,-60,wm.ex_rig_on_date)
      THEN 1 ELSE 0 END AS a_peg60_missed,
 CASE WHEN wm.flaf_issue_date IS NULL AND CAST(GETDATE() AS date) > DATEADD(day,-90,wm.ex_rig_on_date)
      THEN 1 ELSE 0 END AS a_flaf90_missed,
 CASE WHEN wm.rig_on_date > wm.rig_off_date THEN 1 ELSE 0 END AS a_rig_inverted,
 ISNULL(tf.a_date_inverted,0) AS a_date_inv, ISNULL(tf.a_target_inverted,0) AS a_tgt_inv,
 ISNULL(tf.a_progress_vs_flag,0) AS a_prog_flag, ISNULL(mp.a_unmapped_tasks,0) AS a_unmapped,
 ISNULL(tf.tasks,0) AS tasks,
 (CASE WHEN wm.pegged_date IS NULL AND CAST(GETDATE() AS date) > DATEADD(day,-60,wm.ex_rig_on_date) THEN 1 ELSE 0 END
 + CASE WHEN wm.flaf_issue_date IS NULL AND CAST(GETDATE() AS date) > DATEADD(day,-90,wm.ex_rig_on_date) THEN 1 ELSE 0 END
 + CASE WHEN wm.rig_on_date > wm.rig_off_date THEN 1 ELSE 0 END
 + ISNULL(tf.a_date_inverted,0) + ISNULL(tf.a_target_inverted,0)
 + ISNULL(tf.a_progress_vs_flag,0) + ISNULL(mp.a_unmapped_tasks,0)) AS TOTAL_ANOMALIES
FROM well.well_master wm
LEFT JOIN task_flags tf ON tf.well_id = wm.well_id
LEFT JOIN map mp ON mp.well_id = wm.well_id
ORDER BY TOTAL_ANOMALIES DESC""")

show("Distribution: how many wells carry how many anomalies", """
WITH t AS (
 SELECT *, ROW_NUMBER() OVER (PARTITION BY task_code, well_id ORDER BY ActionOn DESC, id DESC) rn
 FROM well.task_daily WHERE well_id IS NOT NULL AND task_code IS NOT NULL),
tf AS (SELECT well_id,
  SUM(CASE WHEN actual_start>actual_end THEN 1 ELSE 0 END)
 +SUM(CASE WHEN target_start>target_end THEN 1 ELSE 0 END)
 +SUM(CASE WHEN completed=0 AND progress>=1 THEN 1 ELSE 0 END) AS n
 FROM t WHERE rn=1 GROUP BY well_id),
w AS (SELECT wm.well_id,
  ISNULL(tf.n,0)
 + CASE WHEN wm.pegged_date IS NULL AND CAST(GETDATE() AS date)>DATEADD(day,-60,wm.ex_rig_on_date) THEN 1 ELSE 0 END
 + CASE WHEN wm.flaf_issue_date IS NULL AND CAST(GETDATE() AS date)>DATEADD(day,-90,wm.ex_rig_on_date) THEN 1 ELSE 0 END
 AS total FROM well.well_master wm LEFT JOIN tf ON tf.well_id=wm.well_id)
SELECT band, COUNT(*) AS wells FROM (
 SELECT CASE WHEN total=0 THEN '0  clean'
             WHEN total=1 THEN '1'
             WHEN total BETWEEN 2 AND 3 THEN '2-3'
             WHEN total BETWEEN 4 AND 9 THEN '4-9'
             ELSE '10+' END AS band FROM w) x
GROUP BY band ORDER BY band""")
