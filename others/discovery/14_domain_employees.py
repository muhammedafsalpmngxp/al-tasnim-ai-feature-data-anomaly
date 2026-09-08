"""Phase 14 - EMPLOYEE / CREW / EQUIPMENT domain + WBS name-collision proof."""
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
        print("   " + " | ".join(str(r[h])[:44] for h in hdr))
    if len(rs) > n:
        print(f"   ... {len(rs)-n} more")


print("#" * 112)
print("# WBS NAME COLLISIONS  (whitespace / newline / typo variants of the SAME WBS)")
print("#" * 112)

show("WBS values containing control characters", """
SELECT REPLACE(REPLACE(REPLACE(activity_group_description,CHAR(13),'<CR>'),CHAR(10),'<LF>'),CHAR(9),'<TAB>') AS wbs_escaped,
       LEN(activity_group_description) AS len_, COUNT(*) AS activities
FROM dbo.activity_master_csv
WHERE activity_group_description LIKE '%'+CHAR(13)+'%' OR activity_group_description LIKE '%'+CHAR(10)+'%'
   OR activity_group_description LIKE '%'+CHAR(9)+'%'
GROUP BY activity_group_description ORDER BY wbs_escaped""")

show("WBS collapsing to the SAME normalised name (whitespace-only differences)", """
WITH n AS (SELECT activity_group_description AS raw,
             UPPER(REPLACE(REPLACE(REPLACE(REPLACE(activity_group_description,
               CHAR(13),' '),CHAR(10),' '),CHAR(9),' '),'  ',' ')) AS norm1
           FROM dbo.activity_master_csv WHERE activity_group_description IS NOT NULL),
m AS (SELECT raw, LTRIM(RTRIM(REPLACE(REPLACE(norm1,'  ',' '),'  ',' '))) AS norm FROM n)
SELECT norm AS normalised_wbs, COUNT(DISTINCT raw) AS raw_variants, COUNT(*) AS activities
FROM m GROUP BY norm HAVING COUNT(DISTINCT raw) > 1 ORDER BY raw_variants DESC""")

show("WBS: exact distinct count vs normalised distinct count (the denominator error)", """
WITH n AS (SELECT activity_group_description AS raw,
             LTRIM(RTRIM(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
               UPPER(activity_group_description),CHAR(13),' '),CHAR(10),' '),CHAR(9),' '),'  ',' '),'  ',' '))) AS norm
           FROM dbo.activity_master_csv WHERE activity_group_description IS NOT NULL)
SELECT COUNT(DISTINCT raw) AS wbs_as_stored, COUNT(DISTINCT norm) AS wbs_after_normalising,
       COUNT(DISTINCT raw) - COUNT(DISTINCT norm) AS PHANTOM_WBS FROM n""")

show("Likely typo pairs across WBS names (Commisoning etc.)", """
SELECT DISTINCT descipline, activity_group_description AS wbs
FROM dbo.activity_master_csv
WHERE activity_group_description LIKE '%Commis%' OR activity_group_description LIKE '%Paint%'
   OR activity_group_description LIKE '%Hydrotest%' OR activity_group_description LIKE '%PE Pull%'
   OR activity_group_description LIKE '%Struct%'
ORDER BY wbs, descipline""")

print("\n" + "#" * 112)
print("# EMPLOYEE DOMAIN")
print("#" * 112)

show("ref.employee columns", """
SELECT c.column_id AS ord, c.name, ty.name AS ty, c.is_nullable
FROM sys.columns c JOIN sys.types ty ON ty.user_type_id=c.user_type_id
WHERE c.object_id=OBJECT_ID('ref.employee') ORDER BY c.column_id""", 20)

show("Nationality x employee_type x company (business rule sec 2 - three groups)", """
SELECT ISNULL(e.nationality_type,'(NULL - unrecorded)') AS nationality_type,
       COUNT(*) AS employees, COUNT(DISTINCT e.company_id) AS companies,
       COUNT(DISTINCT e.employee_type_id) AS types
FROM ref.employee e GROUP BY e.nationality_type ORDER BY employees DESC""")

show("Nationality values - EXACT strings and casing (rule: only 'National'/'Expat'/NULL)", """
SELECT '[' + nationality_type + ']' AS exact_value, LEN(nationality_type) AS len_, COUNT(*) AS n
FROM ref.employee WHERE nationality_type IS NOT NULL
GROUP BY nationality_type ORDER BY n DESC""")

show("Employee identity completeness", """
SELECT COUNT(*) AS employees,
  SUM(CASE WHEN employee_name IS NULL OR LTRIM(RTRIM(employee_name))='' THEN 1 ELSE 0 END) AS no_name,
  SUM(CASE WHEN company_id IS NULL THEN 1 ELSE 0 END) AS no_company,
  SUM(CASE WHEN employee_type_id IS NULL THEN 1 ELSE 0 END) AS no_type,
  SUM(CASE WHEN nationality_type IS NULL THEN 1 ELSE 0 END) AS no_nationality,
  COUNT(DISTINCT employee_id) AS distinct_ids
FROM ref.employee""")

show("Duplicate employees by normalised name", """
SELECT COUNT(*) AS dup_name_groups, SUM(n)-COUNT(*) AS extra_rows, MAX(n) AS worst FROM (
  SELECT UPPER(LTRIM(RTRIM(employee_name))) AS nm, COUNT(*) AS n
  FROM ref.employee WHERE employee_name IS NOT NULL AND LTRIM(RTRIM(employee_name))<>''
  GROUP BY UPPER(LTRIM(RTRIM(employee_name))) HAVING COUNT(*)>1) x""")

show("Duplicate employee name examples", """
SELECT TOP 10 UPPER(LTRIM(RTRIM(employee_name))) AS nm, COUNT(*) AS n,
       COUNT(DISTINCT company_id) AS companies, COUNT(DISTINCT nationality_type) AS nationalities
FROM ref.employee WHERE employee_name IS NOT NULL AND LTRIM(RTRIM(employee_name))<>''
GROUP BY UPPER(LTRIM(RTRIM(employee_name))) HAVING COUNT(*)>1 ORDER BY n DESC""")

show("Employee -> company / type referential health", """
SELECT
 (SELECT COUNT(DISTINCT e.company_id) FROM ref.employee e WHERE e.company_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM ref.company c WHERE c.company_id=e.company_id)) AS orphan_company_ids,
 (SELECT COUNT(DISTINCT e.employee_type_id) FROM ref.employee e WHERE e.employee_type_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM ref.employee_type t WHERE t.employee_type_id=e.employee_type_id)) AS orphan_type_ids,
 (SELECT COUNT(*) FROM ref.company c
   WHERE NOT EXISTS (SELECT 1 FROM ref.employee e WHERE e.company_id=c.company_id)) AS companies_with_no_employees,
 (SELECT COUNT(*) FROM ref.employee_type t
   WHERE NOT EXISTS (SELECT 1 FROM ref.employee e WHERE e.employee_type_id=t.employee_type_id)) AS types_with_no_employees""")

show("employee_contact: 612 rows vs 18476 employees", """
SELECT (SELECT COUNT(*) FROM dbo.employee_contact) AS contact_rows,
       (SELECT COUNT(*) FROM ref.employee) AS employees,
       CAST(100.0*(SELECT COUNT(*) FROM dbo.employee_contact)/(SELECT COUNT(*) FROM ref.employee)
            AS decimal(5,2)) AS coverage_pct""")

print("\n" + "#" * 112)
print("# CREW DOMAIN")
print("#" * 112)

show("ref.crew: employees/equipments stored as delimited STRINGS not rows", """
SELECT TOP 8 crew_id, code, crew_type_id, supervisor_id,
       LEFT(employees,50) AS employees_raw, LEFT(equipments,40) AS equipments_raw,
       LEFT(composition,30) AS composition FROM ref.crew ORDER BY crew_id""")

show("ref.crew health", """
SELECT COUNT(*) AS crews,
 SUM(CASE WHEN code IS NULL OR LTRIM(RTRIM(code))='' THEN 1 ELSE 0 END) AS no_code,
 SUM(CASE WHEN crew_type_id IS NULL THEN 1 ELSE 0 END) AS no_crew_type,
 SUM(CASE WHEN supervisor_id IS NULL THEN 1 ELSE 0 END) AS no_supervisor,
 SUM(CASE WHEN employees IS NULL OR LTRIM(RTRIM(employees))='' OR employees='[]' THEN 1 ELSE 0 END) AS no_employees,
 COUNT(DISTINCT code) AS distinct_codes
FROM ref.crew""")

show("crew code duplicates", """
SELECT TOP 10 code, COUNT(*) AS n FROM ref.crew WHERE code IS NOT NULL
GROUP BY code HAVING COUNT(*)>1 ORDER BY n DESC""")

show("bridge.crew_employee vs ref.crew.employees string - two sources for one fact", """
SELECT (SELECT COUNT(*) FROM bridge.crew_employee) AS bridge_rows,
       (SELECT COUNT(DISTINCT crew_id) FROM bridge.crew_employee) AS crews_in_bridge,
       (SELECT COUNT(*) FROM ref.crew) AS crews_in_ref,
       (SELECT COUNT(DISTINCT b.employee_id) FROM bridge.crew_employee b) AS employees_in_bridge""")

show("bridge.crew_employee orphans", """
SELECT
 (SELECT COUNT(DISTINCT b.crew_id) FROM bridge.crew_employee b
   WHERE NOT EXISTS (SELECT 1 FROM ref.crew c WHERE c.crew_id=b.crew_id)) AS orphan_crew_ids,
 (SELECT COUNT(DISTINCT b.employee_id) FROM bridge.crew_employee b
   WHERE NOT EXISTS (SELECT 1 FROM ref.employee e WHERE e.employee_id=b.employee_id)) AS orphan_employee_ids""")

show("An employee assigned to MANY crews at once", """
SELECT TOP 10 employee_id, COUNT(DISTINCT crew_id) AS crews FROM bridge.crew_employee
GROUP BY employee_id HAVING COUNT(DISTINCT crew_id)>1 ORDER BY crews DESC""")

show("activity_master_csv.crew_code -> ref.crew.code resolution (fixed col name)", """
SELECT COUNT(DISTINCT amc.crew_code) AS distinct_crew_codes_in_activity_master,
  COUNT(DISTINCT CASE WHEN c.code IS NULL THEN amc.crew_code END) AS crew_codes_NOT_in_ref_crew
FROM dbo.activity_master_csv amc LEFT JOIN ref.crew c ON c.code = amc.crew_code
WHERE amc.crew_code IS NOT NULL""")

show("task_daily crew/employee attribution completeness", """
SELECT COUNT(*) AS task_rows,
 SUM(CASE WHEN crew_id IS NULL THEN 1 ELSE 0 END) AS no_crew_id,
 SUM(CASE WHEN emp_id IS NULL THEN 1 ELSE 0 END) AS no_emp_id,
 SUM(CASE WHEN crew_type_id IS NULL THEN 1 ELSE 0 END) AS no_crew_type_id,
 SUM(CASE WHEN daily_employee_ids IS NULL OR LTRIM(RTRIM(daily_employee_ids))='' THEN 1 ELSE 0 END) AS no_daily_employees,
 SUM(CASE WHEN daily_actual_hours IS NULL THEN 1 ELSE 0 END) AS no_hours,
 SUM(CASE WHEN daily_actual_hours > 24 THEN 1 ELSE 0 END) AS hours_over_24
FROM well.task_daily""")

show("Manhours plausibility: daily hours per employee-day", """
SELECT COUNT(*) AS rows_with_hours,
 MIN(daily_actual_hours) AS min_h, MAX(daily_actual_hours) AS max_h,
 CAST(AVG(daily_actual_hours) AS decimal(10,2)) AS avg_h,
 SUM(CASE WHEN daily_actual_hours=0 THEN 1 ELSE 0 END) AS zero_hours,
 SUM(CASE WHEN daily_actual_hours>24 THEN 1 ELSE 0 END) AS over_24h,
 SUM(CASE WHEN daily_actual_hours>500 THEN 1 ELSE 0 END) AS over_500h
FROM well.task_daily WHERE daily_actual_hours IS NOT NULL""")

show("ref.equipment health", """
SELECT COUNT(*) AS equipment,
 SUM(CASE WHEN status_id IS NULL THEN 1 ELSE 0 END) AS no_status,
 SUM(CASE WHEN equipment_type_id IS NULL THEN 1 ELSE 0 END) AS no_type,
 COUNT(DISTINCT status_id) AS statuses_used,
 (SELECT COUNT(*) FROM ref.equipment_status) AS statuses_defined,
 (SELECT COUNT(*) FROM ref.equipment_type t
   WHERE NOT EXISTS (SELECT 1 FROM ref.equipment e WHERE e.equipment_type_id=t.equipment_type_id)) AS unused_types
FROM ref.equipment""")
