"""Phase 19 - PROTOTYPE of the generated-invariant engine.

Hand-writing one check per column pair does not scale and will miss cases. Instead:
declare column ROLES once, then GENERATE every invariant from the role combinations.

This script proves the approach works by generating and running the date-order invariant
family across every in-scope table, and sub-classifying what it finds.
"""
import json
from collections import defaultdict
from dbx import connect, rows, OUT, ALLOWED_SCHEMAS, is_table_excluded

cn = connect()
cols = json.loads((OUT / "02_columns.json").read_text(encoding="utf-8"))
objs = json.loads((OUT / "01_objects.json").read_text(encoding="utf-8"))
rowcount = {f"{o['schema']}.{o['object']}": o["approx_rows"] for o in objs}

DATEISH = {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset"}
SKIP_TABLES = {"dbo.activity_task_plan", "dbo.engineering_acc_dump", "well.well_details",
               "well.wmr_conversion", "dbo.sysdiagrams"}

# ---------------------------------------------------------------- role inference
# In production these roles come from config/column_semantics.yaml, reviewed by the
# business. Here they are inferred from naming so the prototype can run unattended.
START_TOKENS = ("start", "_on_", "on_date", "begin", "raised", "issue", "recvd", "received", "po_")
END_TOKENS = ("end", "finish", "off_date", "_off_", "complete", "completion", "approved", "delivery")


def role_of(name):
    n = name.lower()
    if any(t in n for t in ("target_",)):
        base = "target"
    elif any(t in n for t in ("actual", "act_")):
        base = "actual"
    elif n.startswith("ex_") or "expect" in n or "plan" in n:
        base = "plan"
    elif "committed" in n or "commit_" in n:
        base = "committed"
    else:
        base = "other"
    if any(t in n for t in END_TOKENS):
        return base, "end"
    if any(t in n for t in START_TOKENS):
        return base, "start"
    return base, "point"


date_cols = defaultdict(list)
for c in cols:
    key = f"{c['schema']}.{c['table']}"
    if c["schema"] not in ALLOWED_SCHEMAS and c["schema"] != "wbs":
        continue
    if is_table_excluded(c["schema"], c["table"]) or key in SKIP_TABLES:
        continue
    if c["data_type"] in DATEISH and rowcount.get(key, 0) > 0:
        date_cols[key].append(c["column"])

print("=" * 112)
print("STEP 1 - how many date columns exist, and how many invariants can be generated")
print("=" * 112)
tot_cols = sum(len(v) for v in date_cols.values())
print(f"{'table':46}{'date cols':>11}{'ordered pairs':>15}{'rows':>12}")
print("-" * 112)
pair_total = 0
for key in sorted(date_cols, key=lambda k: -len(date_cols[k])):
    n = len(date_cols[key])
    pairs = n * (n - 1) // 2
    pair_total += pairs
    if n >= 2:
        print(f"{key:46}{n:>11}{pairs:>15}{rowcount.get(key,0):>12,}")
print("-" * 112)
print(f"{'TOTAL':46}{tot_cols:>11}{pair_total:>15}")
print(f"\n=> {pair_total:,} candidate date-order invariants from {tot_cols} columns across "
      f"{len([k for k in date_cols if len(date_cols[k])>=2])} tables.")
print("   Hand-writing these one at a time is where coverage gaps come from.")

# ------------------------------------------------- generate the SAME-ROLE start<=end family
print("\n" + "=" * 112)
print("STEP 2 - generate + run the SAME-ROLE 'start <= end' family (the screenshot's class)")
print("=" * 112)

generated = []
for key, cnames in sorted(date_cols.items()):
    by_role = defaultdict(dict)
    for cn_ in cnames:
        base, kind = role_of(cn_)
        if kind in ("start", "end"):
            by_role[base].setdefault(kind, []).append(cn_)
    for base, kinds in by_role.items():
        for s in kinds.get("start", []):
            for e in kinds.get("end", []):
                generated.append((key, base, s, e))

print(f"generated {len(generated)} same-role start/end invariants\n")
print(f"{'table':30}{'role':10}{'start col':24}{'end col':24}{'violations':>11}{'rows':>10}")
print("-" * 112)
results = []
for key, base, s, e in generated:
    sch, tab = key.split(".", 1)
    try:
        r = rows(cn, f"""
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN [{s}] > [{e}] THEN 1 ELSE 0 END) AS bad,
                   SUM(CASE WHEN [{s}] IS NOT NULL AND [{e}] IS NULL THEN 1 ELSE 0 END) AS start_no_end,
                   SUM(CASE WHEN [{e}] IS NOT NULL AND [{s}] IS NULL THEN 1 ELSE 0 END) AS end_no_start,
                   MIN(DATEDIFF(day, [{s}], [{e}])) AS worst_negative_days
            FROM [{sch}].[{tab}]""")[0]
    except Exception as ex:  # noqa: BLE001
        print(f"{key[:29]:30}{base:10}{s[:23]:24}{e[:23]:24}  ERR {str(ex)[:40]}")
        continue
    bad = int(r["bad"] or 0)
    results.append({"table": key, "role": base, "start": s, "end": e, "violations": bad,
                    "start_no_end": int(r["start_no_end"] or 0),
                    "end_no_start": int(r["end_no_start"] or 0),
                    "worst_negative_days": r["worst_negative_days"]})
    if bad or r["end_no_start"]:
        flag = f"  worst={r['worst_negative_days']}d  end_without_start={r['end_no_start']}"
        print(f"{key[:29]:30}{base:10}{s[:23]:24}{e[:23]:24}{bad:>11}{r['n']:>10}{flag}")

hits = [x for x in results if x["violations"] > 0]
print("-" * 112)
print(f"{len(generated)} invariants run -> {len(hits)} with violations, "
      f"{sum(x['violations'] for x in hits):,} violating rows total")

# ------------------------------------------------- sub-classify the screenshot's anomaly
print("\n" + "=" * 112)
print("STEP 3 - sub-classify task_daily actual_start > actual_end (the screenshot rows)")
print("=" * 112)

for t, sql in [
    ("Root-cause split of the inversion", """
     WITH latest AS (
       SELECT *, ROW_NUMBER() OVER (PARTITION BY task_code, well_id
                                    ORDER BY ActionOn DESC, id DESC) rn
       FROM well.task_daily WHERE task_code IS NOT NULL AND well_id IS NOT NULL)
     SELECT cause, COUNT(*) AS tasks, MIN(gap_days) AS min_gap, MAX(gap_days) AS max_gap FROM (
       SELECT CASE
         WHEN DATEDIFF(day, actual_end, actual_start) BETWEEN 1 AND 7   THEN 'A. off by 1-7 days'
         WHEN DATEDIFF(day, actual_end, actual_start) BETWEEN 8 AND 60  THEN 'B. off by 8-60 days'
         WHEN YEAR(actual_start) = YEAR(actual_end) + 1
              AND MONTH(actual_start) = MONTH(actual_end)              THEN 'C. YEAR typed +1 (same month)'
         WHEN YEAR(actual_start) > YEAR(actual_end)                     THEN 'D. year out by 1+ (other)'
         ELSE 'E. other' END AS cause,
         DATEDIFF(day, actual_end, actual_start) AS gap_days
       FROM latest WHERE rn=1 AND actual_start > actual_end) x
     GROUP BY cause ORDER BY cause"""),

    ("Does actual_end match target_end on the inverted rows? (which side is wrong)", """
     WITH latest AS (
       SELECT *, ROW_NUMBER() OVER (PARTITION BY task_code, well_id
                                    ORDER BY ActionOn DESC, id DESC) rn
       FROM well.task_daily WHERE task_code IS NOT NULL AND well_id IS NOT NULL)
     SELECT COUNT(*) AS inverted_tasks,
       SUM(CASE WHEN actual_end = target_end THEN 1 ELSE 0 END) AS actual_end_MATCHES_target_end,
       SUM(CASE WHEN actual_start = target_start THEN 1 ELSE 0 END) AS actual_start_MATCHES_target_start,
       SUM(CASE WHEN actual_end = target_end AND actual_start <> target_start THEN 1 ELSE 0 END) AS likely_START_is_wrong,
       SUM(CASE WHEN actual_start = target_start AND actual_end <> target_end THEN 1 ELSE 0 END) AS likely_END_is_wrong
     FROM latest WHERE rn=1 AND actual_start > actual_end"""),

    ("Sample inverted rows (compare with the screenshot)", """
     WITH latest AS (
       SELECT *, ROW_NUMBER() OVER (PARTITION BY task_code, well_id
                                    ORDER BY ActionOn DESC, id DESC) rn
       FROM well.task_daily WHERE task_code IS NOT NULL AND well_id IS NOT NULL)
     SELECT TOP 12 task_code, well_id,
       CONVERT(varchar(10),target_start,120) AS tgt_start,
       CONVERT(varchar(10),target_end,120)   AS tgt_end,
       CONVERT(varchar(10),actual_start,120) AS act_start,
       CONVERT(varchar(10),actual_end,120)   AS act_end,
       DATEDIFF(day, actual_end, actual_start) AS inverted_by_days, progress, completed
     FROM latest WHERE rn=1 AND actual_start > actual_end
     ORDER BY DATEDIFF(day, actual_end, actual_start) DESC"""),
]:
    print(f"\n--- {t} ---")
    try:
        rs = rows(cn, sql)
    except Exception as ex:  # noqa: BLE001
        print("   ERROR:", str(ex)[:300]); continue
    if not rs:
        print("   (no rows)"); continue
    print("   " + " | ".join(rs[0].keys()))
    for r in rs[:14]:
        print("   " + " | ".join(str(v)[:38] for v in r.values()))

# ------------------------------------------------- other generated families, sized
print("\n" + "=" * 112)
print("STEP 4 - the other generated families and how many invariants each yields")
print("=" * 112)

num_cols = [c for c in cols
            if c["schema"] in ALLOWED_SCHEMAS
            and not is_table_excluded(c["schema"], c["table"])
            and f"{c['schema']}.{c['table']}" not in SKIP_TABLES
            and c["data_type"] in {"int", "bigint", "smallint", "tinyint", "decimal",
                                   "numeric", "float", "real", "money"}
            and rowcount.get(f"{c['schema']}.{c['table']}", 0) > 0]
pct_cols = [c for c in num_cols if any(t in c["column"].lower()
                                       for t in ("progress", "percent", "pct", "weightage", "weight"))]
qty_cols = [c for c in num_cols if any(t in c["column"].lower()
                                       for t in ("qty", "quantity", "manhour", "hours", "duration",
                                                 "length", "required", "planned"))]
id_cols = [c for c in cols
           if c["schema"] in ALLOWED_SCHEMAS and not is_table_excluded(c["schema"], c["table"])
           and f"{c['schema']}.{c['table']}" not in SKIP_TABLES
           and c["column"].lower().endswith("_id")
           and rowcount.get(f"{c['schema']}.{c['table']}", 0) > 0]
actual_dates = [c for c in cols
                if c["schema"] in ALLOWED_SCHEMAS and not is_table_excluded(c["schema"], c["table"])
                and f"{c['schema']}.{c['table']}" not in SKIP_TABLES
                and c["data_type"] in DATEISH and role_of(c["column"])[0] == "actual"
                and rowcount.get(f"{c['schema']}.{c['table']}", 0) > 0]

fam = [
    ("F1  date order: same-role start <= end", len(generated), "RUN ABOVE"),
    ("F2  date order: cross-role (target vs actual vs plan)", pair_total - len(generated), "generated"),
    ("F3  actual date not in the future", len(actual_dates), "generated"),
    ("F4  percentage/weight within [0,1] or [0,100]", len(pct_cols), "generated"),
    ("F5  quantity/measure >= 0", len(qty_cols), "generated"),
    ("F6  *_id orphan check where no FK is declared", len(id_cols), "generated"),
    ("F7  text column whose content contradicts its type/name", "936 cols scanned", "generated"),
    ("F8  business-rule lifecycle order (sec 7)", "~12", "HAND-WRITTEN"),
    ("F9  WBS weightage rollup (sec 9)", "~14", "HAND-WRITTEN"),
    ("F10 milestone deadlines (sec 4)", "~10", "HAND-WRITTEN"),
]
print(f"{'family':56}{'invariants':>16}{'source':>16}")
print("-" * 112)
for a, b, c in fam:
    print(f"{a:56}{str(b):>16}{c:>16}")
print("-" * 112)
print("""
Generated families (F1-F7) give COVERAGE - every column pair is tested, so nothing is
missed by omission. Hand-written families (F8-F10) encode business MEANING, which cannot
be inferred from column names. Production needs both.""")
