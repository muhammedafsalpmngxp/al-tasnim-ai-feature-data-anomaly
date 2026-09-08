"""Phase 3 - keyword search across every column name, to locate concepts (progress, weightage, wbs, dates)."""
import json, re, sys
from pathlib import Path
from collections import defaultdict
from dbx import OUT, ALLOWED_SCHEMAS, is_table_excluded

cols = json.loads((OUT / "02_columns.json").read_text(encoding="utf-8"))
pats = sys.argv[1:] or ["progress", "weight|pms|wtg", "wbs", "percent|pct|perc", "complete|completion",
                        "date|dt$", "status", "plan", "actual", "qty|quantity", "norm"]
by = defaultdict(list)
for c in cols:
    key = f"{c['schema']}.{c['table']}"
    by[key].append(c)

for p in pats:
    rx = re.compile(p, re.I)
    print(f"\n########## /{p}/ ##########")
    for key in sorted(by):
        hits = [c["column"] for c in by[key] if rx.search(c["column"])]
        if hits:
            sch, tab = key.split(".", 1)
            scope = "S " if (sch in ALLOWED_SCHEMAS and not is_table_excluded(sch, tab)) else "x "
            print(f"{scope}{key:44} {', '.join(hits)}")
