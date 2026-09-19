"""Regression harness: does the engine still measure what it measured last time?

    python -m eval.run_eval --record     establish the baseline from the current catalog
    python -m eval.run_eval              check the engine against that baseline
    python -m eval.run_eval --offline    the checks that need no database

WHAT THIS CATCHES THAT eval/test_rules.py CANNOT
------------------------------------------------
test_rules.py reads the SQL. It proves a probe is well-formed. It cannot prove the probe still
MEASURES the right thing, because that depends on a live database, and the failures that matter
most here are all invisible in the SQL text:

  * a join quietly matching nothing after a column is renamed, so scope_total collapses to 0
    and the probe reports a confident clean result about nothing;
  * a condition inverting, so a rule that flagged 0.2% of rows now flags 99%;
  * a scale change - a column moving from 0-1 to 0-100 - turning a correct threshold into one
    that matches everything or nothing;
  * a prompt or model change quietly degrading what the SQL Author produces.

Each of those passes every static check and produces a plausible-looking number.

WHY IT RUNS ONLY THE SUMMARY QUERY
-----------------------------------
SUMMARY returns exactly one row by contract. So the entire baseline for two hundred probes
costs two hundred cheap queries and no LLM calls at all - which is what makes this runnable in
CI on every change rather than once a quarter when somebody remembers.

HOW THE BANDS ARE CHOSEN
------------------------
Data changes daily, so an exact-count assertion would fail every morning and be switched off
within a week - the classic way a regression suite dies. The baseline therefore records a RATIO
and checks it inside a wide band. The band is deliberately loose: it is tuned to catch an
inversion or a broken join, not a slow drift, because a suite that cries wolf is worse than no
suite at all.

The one assertion with NO tolerance is scope_total > 0. A probe that examines nothing has
proved nothing, and no amount of legitimate data movement makes that acceptable.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from app.config import settings  # noqa: E402
from app.observability import get_logger, setup_logging  # noqa: E402
from app.db import identity  # noqa: E402
from app.rules import catalog as catalog_store  # noqa: E402
from app.rules.contract import (  # noqa: E402
    check_summary,
    columns_of,
    static_problems,
    summary_values,
)

log = get_logger()

_EVAL_DIR = os.path.dirname(os.path.abspath(__file__))


def _golden_path() -> str:
    """THIS database's baseline. One file per database, and deliberately no fallback.

    A baseline is a set of RATIOS measured against a particular database. Kept in one shared
    file it is silently wrong the moment a second database is pointed at: the rule ids are
    identical across databases - every installation has DQ-D03 and DQ-G01-001 - so the harness
    would not skip the rows as "unknown probe", it would compare them. A probe correctly
    flagging 0.2% here would be checked against a band recorded somewhere else and reported as
    a regression, or, worse, a genuine inversion would land inside the foreign band and pass.

    There is no legacy fallback, for the same reason app.db.identity.read_cache has none:
    attribution has to be structural, not a promise. An unattributed eval/golden.jsonl from
    before the split simply stops being read, and `--record` writes this database its own.
    """
    return os.path.join(_EVAL_DIR, f"golden.{identity.slug()}.jsonl")

# How far the anomaly share may move before it is treated as a regression rather than as the
# data changing. Wide on purpose - see the module docstring.
SHARE_LOWER = 0.25
SHARE_UPPER = 4.0
# Below this share, ratio comparison is meaningless: a probe going from 1 finding to 3 is a
# 3x move and almost certainly just data. An absolute floor is compared instead.
SHARE_FLOOR = 0.005  # 0.5% of scope
# How far scope_total may shrink before it looks like a broken join rather than deleted rows.
SCOPE_LOWER = 0.5


class Outcome:
    """Results, with REGRESSIONS held apart from standing COVERAGE GAPS.

    Both are failures and both keep the exit code non-zero - a probe that proves nothing must
    never read as a pass. But they call for completely different action, and a flat list buries
    that: a regression means something broke since the baseline and needs investigating today,
    while a gap has been true all along and needs the RULE fixing or retiring. Mixing sixty
    standing gaps in with one genuine regression is how the regression gets missed.
    """

    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.gaps: list[tuple[str, str]] = []
        self.skipped: list[tuple[str, str]] = []

    def ok(self, name: str) -> None:
        self.passed.append(name)

    def fail(self, name: str, why: str) -> None:
        self.failed.append((name, why))

    def gap(self, name: str, why: str) -> None:
        self.gaps.append((name, why))

    def skip(self, name: str, why: str) -> None:
        self.skipped.append((name, why))

    @property
    def total(self) -> int:
        return len(self.passed) + len(self.failed) + len(self.gaps) + len(self.skipped)


def load_golden() -> dict[str, dict]:
    if not os.path.exists(_golden_path()):
        return {}
    out: dict[str, dict] = {}
    with open(_golden_path(), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("rule_id"):
                out[row["rule_id"]] = row
    return out


def save_golden(rows: list[dict]) -> None:
    rows.sort(key=lambda r: r["rule_id"])
    with open(_golden_path(), "w", encoding="utf-8") as fh:
        fh.write("// Baseline for python -m eval.run_eval. Regenerate with --record after a\n")
        fh.write("// deliberate change to the rules, the schema, or the prompts. One probe per\n")
        fh.write("// line; `share` is anomaly_count / scope_total at the time it was recorded.\n")
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# ── Offline: what can be proved without touching the database ──────────────────

def check_offline(catalog, golden: dict[str, dict], out: Outcome) -> None:
    """The catalog's own integrity. No database, no LLM."""
    if not catalog.probes:
        out.fail("catalog", "no probes are compiled - run: python -m app.cli compile")
        return
    out.ok("catalog loads")

    for probe in catalog.probes.values():
        name = f"{probe.rule_id} static"
        if probe.status != "active":
            out.skip(name, f"status is {probe.status}")
            continue
        problems = static_problems(probe.summary_sql, probe.detail_sql, probe.rule_id)
        if problems:
            # A stored probe violating the contract means something got into the catalog
            # without passing the gate that is supposed to make that impossible.
            out.fail(name, problems[0])
        else:
            out.ok(name)

    # A rule that was in the baseline and has since vanished is worth saying out loud: it is
    # usually a deleted rule, but it is occasionally a compile that silently dropped one.
    missing = [rid for rid in golden if rid not in catalog.probes]
    if missing:
        out.fail(
            "baseline coverage",
            f"{len(missing)} probe(s) in the baseline are no longer in the catalog: "
            + ", ".join(sorted(missing)[:8]),
        )
    elif golden:
        out.ok("baseline coverage")

    # How many model calls a rule needed is the cheapest available proxy for AUTHORING quality.
    # A rule that compiled in 3 calls and now needs 5 was rewritten twice more than it used to
    # be, which means the validator, the contract or the reviewer rejected it more often - and
    # that is a prompt regression showing up as cost before it ever shows up as a wrong number.
    # Free to check, because the count is already stored in the catalog.
    for rule_id, base in golden.items():
        probe = catalog.probes.get(rule_id)
        expected = base.get("llm_calls")
        if probe is None or expected is None:
            continue
        name = f"{rule_id} compile cost"
        if probe.llm_calls > expected:
            out.fail(
                name,
                f"needed {probe.llm_calls} model call(s) to compile, up from {expected}. More "
                f"rewrites means the author is being rejected more often - check whether a "
                f"prompt, the model, or the rule's own wording changed.",
            )
        else:
            out.ok(name)


# ── Live: run each SUMMARY and compare against the baseline ────────────────────

def measure(probe) -> dict:
    """Execute one SUMMARY query. Returns the measured values, or an error."""
    from app.db.connection import get_connection

    conn = None
    try:
        conn = get_connection(timeout=settings.query_timeout)
        cur = conn.cursor()
        started = time.perf_counter()
        cur.execute(probe.summary_sql)
        columns = columns_of(cur.description)
        rows = cur.fetchmany(2)
        elapsed = time.perf_counter() - started

        problems = check_summary(columns, len(rows))
        if problems:
            return {"error": problems[0]}
        values = summary_values(columns, list(rows[0]))
        scope = int(values.get("scope_total") or 0)
        count = int(values.get("anomaly_count") or 0)
        return {
            "scope_total": scope,
            "anomaly_count": count,
            "share": (count / scope) if scope else 0.0,
            "seconds": round(elapsed, 2),
        }
    except Exception as exc:  # noqa: BLE001 - a failing probe is a result, not a crash
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def compare(rule_id: str, now: dict, base: dict, out: Outcome) -> None:
    if now.get("error"):
        out.fail(rule_id, now["error"])
        return

    scope, share = now["scope_total"], now["share"]

    # NO TOLERANCE. A probe examining nothing has proved nothing, whatever the data did.
    # Which BUCKET it lands in depends on whether this is new: a probe that used to examine
    # rows and now examines none broke today and needs investigating; one that never examined
    # any is a standing gap in coverage, and the fix is to the rule, not to the database.
    if scope <= 0:
        was_working = int((base or {}).get("scope_total") or 0) > 0
        if was_working:
            out.fail(
                rule_id,
                f"scope_total fell from {base['scope_total']:,} to 0 - the probe now examines "
                f"NO records. Something broke since the baseline: a renamed column, or a "
                f"filter matching a value that no longer exists.",
            )
        else:
            out.gap(
                rule_id,
                "examines no records, and did not at baseline either. This check proves "
                "nothing about its subject - fix the rule's scope or retire it.",
            )
        return

    if now["anomaly_count"] > scope:
        out.fail(
            rule_id,
            f"anomaly_count ({now['anomaly_count']:,}) exceeds scope_total ({scope:,}), which "
            f"is impossible - the two are being counted at different grains.",
        )
        return

    if not base:
        out.skip(rule_id, "not in the baseline - run with --record to add it")
        return

    base_scope = int(base.get("scope_total") or 0)
    base_share = float(base.get("share") or 0.0)

    if base_scope and scope < base_scope * SCOPE_LOWER:
        out.fail(
            rule_id,
            f"scope_total fell from {base_scope:,} to {scope:,} (more than half). Rows are "
            f"rarely deleted in these volumes - suspect a join that stopped matching.",
        )
        return

    # Compare a ratio where the numbers are large enough for a ratio to mean anything, and an
    # absolute share otherwise. A probe moving from 1 finding to 3 is not a regression.
    if base_share >= SHARE_FLOOR or share >= SHARE_FLOOR:
        low, high = base_share * SHARE_LOWER, base_share * SHARE_UPPER
        if base_share == 0 and share >= SHARE_FLOOR:
            out.fail(
                rule_id,
                f"this probe found nothing at baseline and now flags {share:.2%} of records. "
                f"Either the data degraded sharply, or the condition inverted.",
            )
            return
        if base_share and not (low <= share <= high):
            out.fail(
                rule_id,
                f"the anomaly share moved from {base_share:.2%} to {share:.2%}, outside the "
                f"{SHARE_LOWER:g}x-{SHARE_UPPER:g}x band. Check whether the condition or the "
                f"scale of a compared column changed.",
            )
            return
    out.ok(rule_id)


def run_live(catalog, golden: dict[str, dict], out: Outcome, record: bool,
             only: list[str] | None) -> list[dict]:
    from app.db.connection import ping

    ok, message = ping()
    if not ok:
        out.fail("database", message)
        return []

    probes = [p for p in catalog.probes.values() if p.status == "active"]
    if only:
        wanted = {r.lower() for r in only}
        probes = [p for p in probes if p.rule_id.lower() in wanted]
    probes.sort(key=lambda p: p.rule_id)

    recorded: list[dict] = []
    for index, probe in enumerate(probes, 1):
        now = measure(probe)
        if record:
            if now.get("error"):
                print(f"  [{index:>3}/{len(probes)}] {probe.rule_id}: NOT recorded - {now['error'][:90]}")
                continue
            recorded.append({
                "rule_id": probe.rule_id,
                "scope_total": now["scope_total"],
                "anomaly_count": now["anomaly_count"],
                "share": round(now["share"], 6),
                "llm_calls": probe.llm_calls,
            })
            print(
                f"  [{index:>3}/{len(probes)}] {probe.rule_id}: scope {now['scope_total']:,}, "
                f"anomalies {now['anomaly_count']:,} ({now['share']:.2%})"
            )
        else:
            compare(probe.rule_id, now, golden.get(probe.rule_id, {}), out)
    return recorded


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="run_eval",
        description="Regression harness for the compiled anomaly probes.",
    )
    parser.add_argument(
        "--record", action="store_true",
        help="overwrite the baseline with what the probes measure right now",
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="only the checks that need no database",
    )
    parser.add_argument(
        "--rule", action="append", metavar="ID", help="restrict to this rule id; repeatable"
    )
    args = parser.parse_args()
    # Explicitly quiet: this report is meant to be read by a person scanning for FAIL lines, or
    # by CI. Importing app.* already attached a console handler (get_logger falls back to
    # LOG_CONSOLE), so this call REMOVES it rather than merely declining to add one.
    setup_logging(console=False)

    catalog = catalog_store.load()
    golden = load_golden()
    out = Outcome()

    print(f"Catalog: {len(catalog.probes)} probe(s) - {catalog.counts()}")
    print(f"Baseline: {len(golden)} probe(s) in {os.path.relpath(_golden_path())}\n")

    check_offline(catalog, golden, out)

    if args.record:
        print("Recording the baseline from live measurements...")
        recorded = run_live(catalog, golden, out, record=True, only=args.rule)
        if args.rule:
            # A targeted re-record must not delete every other probe's baseline.
            merged = {r["rule_id"]: r for r in golden.values()}
            merged.update({r["rule_id"]: r for r in recorded})
            recorded = list(merged.values())
        save_golden(recorded)
        print(f"\nRecorded {len(recorded)} probe(s) to {os.path.relpath(_golden_path())}")
        return 0

    if not args.offline:
        run_live(catalog, golden, out, record=False, only=args.rule)
    elif not golden:
        print("No baseline yet. Run: python -m eval.run_eval --record\n")

    # Regressions first and in full: they are the reason to run this, and they are the thing a
    # long list of standing gaps would otherwise hide.
    for name, why in out.failed:
        print(f"  [REGRESSION] {name}: {why}")

    if out.gaps:
        print(f"\n  {len(out.gaps)} probe(s) examine NO records - standing coverage gaps:")
        for name, _ in out.gaps[:15]:
            print(f"    - {name}")
        if len(out.gaps) > 15:
            print(f"    ... and {len(out.gaps) - 15} more")
        print(
            "    These are NOT clean results. Each one's rule needs its scope fixed, or the\n"
            "    rule retired - a check that examines nothing tells you nothing."
        )

    if out.skipped:
        print(f"\n  {len(out.skipped)} skipped (not compiled, or not in the baseline)")

    print(
        f"\n{out.total} check(s): {len(out.passed)} passed, {len(out.failed)} regression(s), "
        f"{len(out.gaps)} coverage gap(s), {len(out.skipped)} skipped"
    )
    if out.failed:
        print("\nFAILED: something measures differently than it did at baseline.")
        return 1
    if out.gaps:
        print("\nFAILED: no regression, but some checks prove nothing about their subject.")
        return 2
    print("All passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
