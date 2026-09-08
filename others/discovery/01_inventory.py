"""Phase 1 - inventory: server info, schemas, tables, row counts, column counts."""
import sys
from dbx import connect, rows, scalar, save, ALLOWED_SCHEMAS, is_table_excluded

cn = connect()

info = rows(cn, """
SELECT @@VERSION AS version, DB_NAME() AS db, SUSER_SNAME() AS login_name,
       SERVERPROPERTY('Collation') AS collation,
       CAST(GETDATE() AS datetime) AS server_now,
       CAST(GETUTCDATE() AS datetime) AS server_utc_now
""")[0]

all_schemas = [r["name"] for r in rows(cn, """
SELECT s.name FROM sys.schemas s
WHERE s.name NOT IN ('guest','INFORMATION_SCHEMA','sys','db_owner','db_accessadmin',
  'db_securityadmin','db_ddladmin','db_backupoperator','db_datareader','db_datawriter',
  'db_denydatareader','db_denydatawriter')
ORDER BY s.name""")]

# object inventory across ALL schemas (so we can see what ALLOWED_SCHEMAS omits)
objs = rows(cn, """
SELECT s.name AS [schema], o.name AS [object], o.type_desc,
       ISNULL(p.rows, 0) AS approx_rows,
       (SELECT COUNT(*) FROM sys.columns c WHERE c.object_id = o.object_id) AS col_count,
       o.create_date, o.modify_date
FROM sys.objects o
JOIN sys.schemas s ON s.schema_id = o.schema_id
OUTER APPLY (SELECT SUM(pt.rows) AS rows FROM sys.partitions pt
             WHERE pt.object_id = o.object_id AND pt.index_id IN (0,1)) p
WHERE o.type IN ('U','V')
ORDER BY s.name, o.name""")

for o in objs:
    o["allowed_schema"] = o["schema"] in ALLOWED_SCHEMAS
    o["excluded_table"] = is_table_excluded(o["schema"], o["object"])
    o["in_llm_scope"] = o["allowed_schema"] and not o["excluded_table"]

save("01_server_info.json", info)
save("01_schemas.json", {"all_schemas": all_schemas, "allowed_schemas": ALLOWED_SCHEMAS,
                         "allowed_but_missing": [s for s in ALLOWED_SCHEMAS if s not in all_schemas],
                         "present_but_not_allowed": [s for s in all_schemas if s not in ALLOWED_SCHEMAS]})
save("01_objects.json", objs)

print("\n=== SERVER ===")
for k, v in info.items():
    print(f"{k:16}: {str(v)[:110]}")

print("\n=== SCHEMAS ===")
print("all      :", ", ".join(all_schemas))
print("allowed  :", ", ".join(ALLOWED_SCHEMAS))
print("allowed-but-absent  :", [s for s in ALLOWED_SCHEMAS if s not in all_schemas])
print("present-not-allowed :", [s for s in all_schemas if s not in ALLOWED_SCHEMAS])

print("\n=== TABLES / VIEWS  (S=in llm scope, x=excluded table, -=schema not allowed) ===")
print(f"{'':3} {'schema.object':52} {'type':6} {'rows':>10} {'cols':>5}")
tot_scope = 0
for o in objs:
    flag = "S" if o["in_llm_scope"] else ("x" if o["excluded_table"] else "-")
    tot_scope += 1 if o["in_llm_scope"] else 0
    t = "TABLE" if o["type_desc"].startswith("USER") else "VIEW"
    print(f"{flag:3} {o['schema']+'.'+o['object']:52} {t:6} {o['approx_rows']:>10} {o['col_count']:>5}")
print(f"\ntotal objects={len(objs)}  in-llm-scope={tot_scope}")
