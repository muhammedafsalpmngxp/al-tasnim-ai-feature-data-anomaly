"""Phase 9 - existing DQ engine artifacts + whole-DB null/dead-column sweep."""
import json
from dbx import connect, rows, scalar, save, OUT, ALLOWED_SCHEMAS, is_table_excluded

cn = connect()


def show(t, sql, n=20):
    print(f"\n--- {t} ---")
    try:
        rs = rows(cn, sql)
    except Exception as e:  # noqa: BLE001
        print("   ERROR:", str(e)[:300]); return
    if not rs:
        print("   (no rows)"); return
    hdr = list(rs[0].keys()); print("   " + " | ".join(hdr))
    for r in rs[:n]:
        print("   " + " | ".join(str(r[h])[:60] for h in hdr))
    if len(rs) > n:
        print(f"   ... {len(rs)-n} more")


print("=" * 100); print("EXISTING RULES-ENGINE ARTIFACTS"); print("=" * 100)
show("dbo.DataQualityCheckLog - contents", "SELECT * FROM dbo.DataQualityCheckLog")
show("dbo.source_master", "SELECT * FROM dbo.source_master")
show("dbo.well_priority_source_master", "SELECT * FROM dbo.well_priority_source_master")
show("dbo.mapping_master - sample", "SELECT TOP 12 * FROM dbo.mapping_master")
show("stored procedures / functions present", """
SELECT s.name AS [schema], o.name, o.type_desc, o.modify_date
FROM sys.objects o JOIN sys.schemas s ON s.schema_id=o.schema_id
WHERE o.type IN ('P','FN','IF','TF','TR') ORDER BY s.name, o.name""", 40)

print("\n" + "=" * 100); print("WHOLE-DB SWEEP: dead / near-dead columns per in-scope table"); print("=" * 100)
cols_all = json.loads((OUT / "02_columns.json").read_text(encoding="utf-8"))
objs = json.loads((OUT / "01_objects.json").read_text(encoding="utf-8"))
tbl_rows = {f"{o['schema']}.{o['object']}": o["approx_rows"] for o in objs
            if o["type_desc"].startswith("USER")}

sweep = []
for key, nrows in sorted(tbl_rows.items()):
    sch, tab = key.split(".", 1)
    if sch not in ALLOWED_SCHEMAS and sch not in ("wbs",):
        continue
    tcols = [c for c in cols_all if c["schema"] == sch and c["table"] == tab]
    if not tcols or nrows == 0:
        sweep.append({"table": key, "rows": nrows, "cols": len(tcols), "all_null": None,
                      "note": "empty table" if nrows == 0 else ""})
        continue
    parts = []
    usable = []
    for c in tcols:
        if c["data_type"] in ("xml", "geography", "geometry", "hierarchyid", "image", "sql_variant"):
            continue
        parts.append(f"SUM(CASE WHEN [{c['column']}] IS NULL THEN 1 ELSE 0 END) AS [{c['column']}]")
        usable.append(c["column"])
    try:
        r = rows(cn, f"SELECT COUNT_BIG(*) AS __n, {', '.join(parts)} FROM [{sch}].[{tab}]")[0]
    except Exception as e:  # noqa: BLE001
        sweep.append({"table": key, "rows": nrows, "cols": len(tcols), "note": f"ERR {str(e)[:60]}"})
        continue
    n = r["__n"] or 0
    all_null = [c for c in usable if n and r[c] == n]
    high_null = [(c, round(100.0 * r[c] / n, 1)) for c in usable if n and 0.9 <= r[c] / n < 1.0]
    sweep.append({"table": key, "rows": int(n), "cols": len(usable),
                  "all_null_count": len(all_null), "all_null": all_null,
                  "over_90pct_null": high_null,
                  "dead_pct": round(100.0 * len(all_null) / len(usable), 1) if usable else None})

save("09_dead_columns.json", sweep)
print(f"{'table':40}{'rows':>12}{'cols':>6}{'allNULL':>9}{'dead%':>7}  >90% null columns")
print("-" * 130)
for s in sweep:
    if s.get("note") == "empty table":
        print(f"{s['table']:40}{s['rows']:>12}{s['cols']:>6}{'-':>9}{'-':>7}  (EMPTY TABLE)")
        continue
    if "all_null_count" not in s:
        print(f"{s['table']:40}{s.get('rows',0):>12}{s.get('cols',0):>6}   {s.get('note','')}")
        continue
    hi = ", ".join(f"{c}({p}%)" for c, p in s["over_90pct_null"][:4])
    if len(s["over_90pct_null"]) > 4:
        hi += f" +{len(s['over_90pct_null'])-4} more"
    print(f"{s['table']:40}{s['rows']:>12}{s['cols']:>6}{s['all_null_count']:>9}{s['dead_pct']:>7}  {hi}")

tot_cols = sum(s.get("cols", 0) for s in sweep)
tot_dead = sum(s.get("all_null_count", 0) for s in sweep)
print(f"\nTOTAL profiled columns={tot_cols}  fully-NULL columns={tot_dead} "
      f"({round(100.0*tot_dead/tot_cols,1) if tot_cols else 0}%)")
