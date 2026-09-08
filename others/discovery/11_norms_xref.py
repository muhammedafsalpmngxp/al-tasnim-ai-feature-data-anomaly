"""Phase 11 - cross-source norms/UOM disagreement (mapping_master uses deprecated `text` type)."""
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
        print("   " + " | ".join(str(r[h])[:44] for h in hdr))
    if len(rs) > n:
        print(f"   ... {len(rs)-n} more")


show("ref.uom columns", """
SELECT c.name, ty.name AS ty FROM sys.columns c
JOIN sys.types ty ON ty.user_type_id=c.user_type_id
WHERE c.object_id = OBJECT_ID('ref.uom')""")

show("deprecated text/ntext columns anywhere in DB", """
SELECT s.name AS [schema], t.name AS [table], c.name AS [column], ty.name AS data_type
FROM sys.columns c JOIN sys.objects t ON t.object_id=c.object_id
JOIN sys.schemas s ON s.schema_id=t.schema_id
JOIN sys.types ty ON ty.user_type_id=c.user_type_id
WHERE ty.name IN ('text','ntext','image') AND t.type='U' ORDER BY s.name,t.name""", 30)

show("NORMS / CODE disagreement: mapping_master vs activity_master_mapping", """
SELECT COUNT(*) AS shared_activity_ids,
  SUM(CASE WHEN ABS(ISNULL(TRY_CAST(CAST(mm.Norms AS nvarchar(50)) AS float),-1)
                  - ISNULL(amm.norms,-1)) > 0.0001 THEN 1 ELSE 0 END) AS norms_disagree,
  SUM(CASE WHEN ISNULL(CAST(mm.UOM AS nvarchar(50)),'~') <> ISNULL(amm.uom,'~') THEN 1 ELSE 0 END) AS uom_disagree,
  SUM(CASE WHEN ISNULL(CAST(mm.New_Activity_Code AS nvarchar(60)),'~')
              <> ISNULL(amm.activity_code,'~') THEN 1 ELSE 0 END) AS code_disagree
FROM dbo.mapping_master mm
JOIN dbo.activity_master_mapping amm ON amm.activity_id = CAST(mm.Activity_ID AS nvarchar(50))""")

show("NORMS disagreement samples", """
SELECT TOP 12 CAST(mm.Activity_ID AS nvarchar(50)) AS activity_id,
       TRY_CAST(CAST(mm.Norms AS nvarchar(50)) AS float) AS norms_mapping_master, amm.norms AS norms_amm,
       CAST(mm.UOM AS nvarchar(20)) AS uom_mm, amm.uom AS uom_amm,
       CAST(mm.New_Activity_Code AS nvarchar(40)) AS code_mm, amm.activity_code AS code_amm
FROM dbo.mapping_master mm
JOIN dbo.activity_master_mapping amm ON amm.activity_id = CAST(mm.Activity_ID AS nvarchar(50))
WHERE ABS(ISNULL(TRY_CAST(CAST(mm.Norms AS nvarchar(50)) AS float),-1) - ISNULL(amm.norms,-1)) > 0.0001
   OR ISNULL(CAST(mm.New_Activity_Code AS nvarchar(60)),'~') <> ISNULL(amm.activity_code,'~')""")

show("UOM vocabulary drift across sources", """
SELECT src, uom, SUM(n) AS n FROM (
  SELECT 'mapping_master' AS src, CAST(UOM AS nvarchar(50)) AS uom, COUNT(*) AS n
    FROM dbo.mapping_master GROUP BY CAST(UOM AS nvarchar(50))
  UNION ALL
  SELECT 'activity_master_mapping', uom, COUNT(*) FROM dbo.activity_master_mapping GROUP BY uom
) x GROUP BY src, uom ORDER BY uom, src""", 60)

show("activity_code present in mapping but MISSING from activity_master_csv (broken hop 2)", """
SELECT COUNT(DISTINCT amm.activity_code) AS codes_not_in_csv
FROM dbo.activity_master_mapping amm
WHERE amm.activity_code IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM dbo.activity_master_csv amc WHERE amc.activity_code = amm.activity_code)""")

show("activity_ids used by task_daily but absent from activity_master_mapping (broken hop 1)", """
WITH a AS (SELECT DISTINCT LEFT(task_code, NULLIF(CHARINDEX('-',task_code),0)-1) AS activity_id
           FROM well.task_daily WHERE task_code IS NOT NULL)
SELECT COUNT(*) AS distinct_unmapped_activity_ids FROM a
WHERE a.activity_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM dbo.activity_master_mapping m WHERE m.activity_id = a.activity_id)""")

show("sample unmapped activity_ids + how much work hangs off them", """
WITH a AS (SELECT LEFT(task_code, NULLIF(CHARINDEX('-',task_code),0)-1) AS activity_id,
                  task_code, well_id FROM well.task_daily WHERE task_code IS NOT NULL)
SELECT TOP 15 a.activity_id, COUNT(*) AS task_rows, COUNT(DISTINCT a.well_id) AS wells
FROM a WHERE NOT EXISTS (SELECT 1 FROM dbo.activity_master_mapping m WHERE m.activity_id=a.activity_id)
GROUP BY a.activity_id ORDER BY task_rows DESC""")
