"""Phase 2 - full column dictionary, PKs, FKs, unique constraints, indexes."""
import sys
from collections import defaultdict
from dbx import connect, rows, save, ALLOWED_SCHEMAS, is_table_excluded, is_column_excluded

cn = connect()
FOCUS = [s.strip() for s in (sys.argv[1].split(",") if len(sys.argv) > 1 else [])]

cols = rows(cn, """
SELECT s.name AS [schema], t.name AS [table], c.column_id AS ord, c.name AS [column],
       ty.name AS data_type, c.max_length, c.precision, c.scale,
       c.is_nullable, c.is_identity, c.is_computed,
       OBJECT_DEFINITION(c.default_object_id) AS default_def
FROM sys.columns c
JOIN sys.objects t ON t.object_id = c.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.types ty ON ty.user_type_id = c.user_type_id
WHERE t.type IN ('U','V')
ORDER BY s.name, t.name, c.column_id""")

pks = rows(cn, """
SELECT s.name AS [schema], t.name AS [table], i.name AS index_name,
       i.is_primary_key, i.is_unique, i.is_unique_constraint, i.type_desc,
       STUFF((SELECT ', ' + c2.name
              FROM sys.index_columns ic2
              JOIN sys.columns c2 ON c2.object_id = ic2.object_id AND c2.column_id = ic2.column_id
              WHERE ic2.object_id = i.object_id AND ic2.index_id = i.index_id AND ic2.is_included_column = 0
              ORDER BY ic2.key_ordinal FOR XML PATH('')), 1, 2, '') AS key_cols
FROM sys.indexes i
JOIN sys.objects t ON t.object_id = i.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE t.type = 'U' AND i.type > 0
ORDER BY s.name, t.name, i.is_primary_key DESC, i.name""")

fks = rows(cn, """
SELECT fk.name AS fk_name,
       ps.name AS parent_schema, pt.name AS parent_table,
       STUFF((SELECT ', ' + pc.name FROM sys.foreign_key_columns fkc2
              JOIN sys.columns pc ON pc.object_id = fkc2.parent_object_id AND pc.column_id = fkc2.parent_column_id
              WHERE fkc2.constraint_object_id = fk.object_id
              ORDER BY fkc2.constraint_column_id FOR XML PATH('')),1,2,'') AS parent_cols,
       rs.name AS ref_schema, rt.name AS ref_table,
       STUFF((SELECT ', ' + rc.name FROM sys.foreign_key_columns fkc3
              JOIN sys.columns rc ON rc.object_id = fkc3.referenced_object_id AND rc.column_id = fkc3.referenced_column_id
              WHERE fkc3.constraint_object_id = fk.object_id
              ORDER BY fkc3.constraint_column_id FOR XML PATH('')),1,2,'') AS ref_cols,
       fk.delete_referential_action_desc, fk.update_referential_action_desc, fk.is_disabled
FROM sys.foreign_keys fk
JOIN sys.objects pt ON pt.object_id = fk.parent_object_id
JOIN sys.schemas ps ON ps.schema_id = pt.schema_id
JOIN sys.objects rt ON rt.object_id = fk.referenced_object_id
JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
ORDER BY ps.name, pt.name""")

checks = rows(cn, """
SELECT s.name AS [schema], t.name AS [table], cc.name AS ck_name, cc.definition, cc.is_disabled
FROM sys.check_constraints cc
JOIN sys.objects t ON t.object_id = cc.parent_object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id ORDER BY s.name, t.name""")

save("02_columns.json", cols)
save("02_indexes.json", pks)
save("02_foreign_keys.json", fks)
save("02_check_constraints.json", checks)

print(f"\n=== KEY / CONSTRAINT COVERAGE ===")
tabs = sorted({(c["schema"], c["table"]) for c in cols})
pk_by = {(p["schema"], p["table"]) for p in pks if p["is_primary_key"]}
in_scope = [t for t in tabs if t[0] in ALLOWED_SCHEMAS and not is_table_excluded(*t)]
print(f"tables+views total={len(tabs)}  with PK={len(pk_by)}  FKs defined={len(fks)}  CHECKs={len(checks)}")
print("in-scope tables WITHOUT a primary key:")
for t in in_scope:
    if t not in pk_by:
        print("   ", f"{t[0]}.{t[1]}")

print("\n=== FOREIGN KEYS ===")
if not fks:
    print("   (none declared)")
for f in fks:
    print(f"   {f['parent_schema']}.{f['parent_table']}({f['parent_cols']}) -> "
          f"{f['ref_schema']}.{f['ref_table']}({f['ref_cols']})  del={f['delete_referential_action_desc']}"
          f"{'  [DISABLED]' if f['is_disabled'] else ''}")

print("\n=== CHECK CONSTRAINTS ===")
for c in checks:
    print(f"   {c['schema']}.{c['table']}: {c['definition']}{'  [DISABLED]' if c['is_disabled'] else ''}")
if not checks:
    print("   (none declared)")

if FOCUS:
    by_tab = defaultdict(list)
    for c in cols:
        by_tab[f"{c['schema']}.{c['table']}"].append(c)
    for key in FOCUS:
        print(f"\n=== COLUMNS {key} ===")
        for c in by_tab.get(key, []):
            t = c["data_type"]
            if t in ("varchar", "nvarchar", "char", "nchar", "varbinary"):
                ln = c["max_length"] // 2 if t.startswith("n") else c["max_length"]
                t += f"({'max' if c['max_length'] == -1 else ln})"
            elif t in ("decimal", "numeric"):
                t += f"({c['precision']},{c['scale']})"
            hid = " HIDDEN" if is_column_excluded(c["schema"], c["table"], c["column"]) else ""
            print(f"  {c['ord']:>3} {c['column']:42} {t:18} {'NULL' if c['is_nullable'] else 'NOT NULL':8}"
                  f"{' IDENT' if c['is_identity'] else ''}{' COMPUTED' if c['is_computed'] else ''}"
                  f"{('  DEF=' + str(c['default_def'])) if c['default_def'] else ''}{hid}")
