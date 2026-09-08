"""Phase 4 - per-column data profile: null%, distinct, min/max, blank/zero counts.
Usage: 04_profile.py schema.table[,schema.table...] [row_limit_for_full_scan]
Builds ONE aggregate query per table so a wide table costs a single scan."""
import json, sys
from dbx import connect, rows, scalar, save, OUT

cols_all = json.loads((OUT / "02_columns.json").read_text(encoding="utf-8"))
targets = [t.strip() for t in sys.argv[1].split(",") if t.strip()]
SAMPLE = int(sys.argv[2]) if len(sys.argv) > 2 else 0   # TABLESAMPLE percent, 0 = full

cn = connect()
NUMERIC = {"int","bigint","smallint","tinyint","decimal","numeric","float","real","money","smallmoney","bit"}
DATEISH = {"date","datetime","datetime2","smalldatetime","datetimeoffset","time"}
TEXTISH = {"varchar","nvarchar","char","nchar","text","ntext"}
report = {}

for tgt in targets:
    sch, tab = tgt.split(".", 1)
    tcols = [c for c in cols_all if c["schema"] == sch and c["table"] == tab]
    if not tcols:
        print(f"!! {tgt} not found"); continue
    src = f"[{sch}].[{tab}]" + (f" TABLESAMPLE ({SAMPLE} PERCENT)" if SAMPLE else "")
    total = scalar(cn, f"SELECT COUNT_BIG(*) FROM {src}")
    parts, meta = ["COUNT_BIG(*) AS __n"], []
    for c in tcols:
        n, dt = c["column"], c["data_type"]
        if dt in ("xml", "geography", "geometry", "hierarchyid", "image", "sql_variant"):
            continue
        q = f"[{n}]"
        big = dt in ("text","ntext") or (dt in TEXTISH and c["max_length"] == -1)
        cast = f"CAST({q} AS nvarchar(400))" if big else q
        parts.append(f"SUM(CASE WHEN {q} IS NULL THEN 1 ELSE 0 END) AS [{n}__null]")
        if not big:
            parts.append(f"COUNT(DISTINCT {q}) AS [{n}__dist]")
        if dt in TEXTISH:
            parts.append(f"SUM(CASE WHEN LTRIM(RTRIM({cast}))='' THEN 1 ELSE 0 END) AS [{n}__blank]")
            parts.append(f"SUM(CASE WHEN LOWER(LTRIM(RTRIM({cast}))) IN "
                         f"('null','n/a','na','none','-','#n/a','nan','tbc','tba','?','unknown','#value!','#ref!') "
                         f"THEN 1 ELSE 0 END) AS [{n}__sentinel]")
            parts.append(f"MAX(LEN({cast})) AS [{n}__maxlen]")
            parts.append(f"SUM(CASE WHEN {cast} <> LTRIM(RTRIM({cast})) THEN 1 ELSE 0 END) AS [{n}__ws]")
        if dt in NUMERIC and dt != "bit":
            parts.append(f"MIN(CAST({q} AS float)) AS [{n}__min]")
            parts.append(f"MAX(CAST({q} AS float)) AS [{n}__max]")
            parts.append(f"SUM(CASE WHEN {q}=0 THEN 1 ELSE 0 END) AS [{n}__zero]")
            parts.append(f"SUM(CASE WHEN {q}<0 THEN 1 ELSE 0 END) AS [{n}__neg]")
        if dt in DATEISH:
            parts.append(f"CONVERT(varchar(30), MIN({q}), 120) AS [{n}__min]")
            parts.append(f"CONVERT(varchar(30), MAX({q}), 120) AS [{n}__max]")
            parts.append(f"SUM(CASE WHEN CAST({q} AS date) > CAST(GETDATE() AS date) THEN 1 ELSE 0 END) AS [{n}__future]")
            parts.append(f"SUM(CASE WHEN CAST({q} AS date) < '2000-01-01' THEN 1 ELSE 0 END) AS [{n}__ancient]")
        meta.append(c)
    r = rows(cn, "SELECT " + ", ".join(parts) + f" FROM {src}")[0]
    n = r["__n"] or 0
    tbl = {"table": tgt, "rows_scanned": int(n), "rows_total": int(total), "sampled_pct": SAMPLE, "columns": []}
    for c in meta:
        nm = c["column"]
        g = lambda suf: r.get(f"{nm}__{suf}")
        nulls = int(g("null") or 0)
        e = {"column": nm, "type": c["data_type"], "nullable": bool(c["is_nullable"]),
             "nulls": nulls, "null_pct": round(100.0 * nulls / n, 2) if n else None,
             "distinct": g("dist"), "min": g("min"), "max": g("max"),
             "zeros": g("zero"), "negatives": g("neg"), "blanks": g("blank"),
             "sentinels": g("sentinel"), "max_len": g("maxlen"), "untrimmed": g("ws"),
             "future_dates": g("future"), "pre_2000_dates": g("ancient")}
        tbl["columns"].append({k: v for k, v in e.items() if v is not None})
    report[tgt] = tbl

    print(f"\n{'='*118}\n{tgt}   rows={total:,}" + (f" (sampled {SAMPLE}% -> {n:,})" if SAMPLE else ""))
    print(f"{'column':38}{'type':14}{'null%':>7}{'distinct':>10}  {'min':<22}{'max':<22} flags")
    print("-"*118)
    for e in tbl["columns"]:
        flags = []
        if e.get("blanks"): flags.append(f"blank={e['blanks']}")
        if e.get("sentinels"): flags.append(f"SENTINEL={e['sentinels']}")
        if e.get("untrimmed"): flags.append(f"untrimmed={e['untrimmed']}")
        if e.get("negatives"): flags.append(f"neg={e['negatives']}")
        if e.get("future_dates"): flags.append(f"FUTURE={e['future_dates']}")
        if e.get("pre_2000_dates"): flags.append(f"PRE2000={e['pre_2000_dates']}")
        if e.get("zeros"): flags.append(f"zero={e['zeros']}")
        if e.get("distinct") == 1: flags.append("CONSTANT")
        if e.get("null_pct") == 100.0: flags.append("ALL-NULL")
        print(f"{e['column'][:37]:38}{e['type'][:13]:14}{str(e.get('null_pct','')):>7}{str(e.get('distinct','')):>10}  "
              f"{str(e.get('min',''))[:21]:<22}{str(e.get('max',''))[:21]:<22}{' '.join(flags)}")

save("04_profile.json", report)
