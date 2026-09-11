"""Command-line entry point.

    python -m app.cli doctor                      # connection, permissions, config
    python -m app.cli run                         # full normalisation pass
    python -m app.cli run --tables well.well_progress --dry-run
    python -m app.cli show --run latest           # what the last run found
    python -m app.cli sql well.task_daily         # print the normalised SELECT
    python -m app.cli introspect                  # rebuild schema.txt/hint_data.txt
    python -m app.cli compiled --list             # review the compiled-agent's checks
    python -m app.cli compiled --activate CMP-1   # human sign-off before it can ever run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from app.config import get_settings
from app.db.source import SourceDatabase
from app.db.store import FindingsStore
from app.logging import configure, get_logger
from app.sentinel.normalise.normaliser import Normaliser
from app.sentinel.normalise.spec import load_spec
from app.sentinel.orchestrator import Orchestrator
from app.sentinel.scope import Scope, TableRef

log = get_logger(__name__)

# Report output goes to STDOUT; log records go to STDERR (app/logging.py). Keeping them on
# separate streams is what lets `python -m app.cli run > run.txt` capture the tables while
# the live log still streams to the terminal.
out = Console()

SEVERITY_STYLE = {
    "critical": "bold red",
    "high": "dark_orange",
    "medium": "yellow",
    "low": "green",
    "review": "magenta",
    "info": "cyan",
}
STATUS_STYLE = {
    "completed": "bold green", "pass": "green",
    "failed": "bold red", "fail": "red", "error": "bold red",
    "running": "yellow", "skipped": "dim", "cancelled": "dim",
}


def _hr(title: str) -> None:
    out.print()
    out.rule(f"[bold cyan]{title}", style="cyan")


def _fmt(n: Any) -> str:
    return f"{n:,}" if isinstance(n, int) else str(n)


def _sev(severity: str) -> Text:
    return Text(severity.upper(), style=SEVERITY_STYLE.get(severity, ""))


def _kv(pairs: list[tuple[str, Any]], *, key_style: str = "dim") -> None:
    """Two-column key/value block -- no borders, so it reads as a definition list.

    A `str` value is markup-parsed (so a caller can pass "[yellow]SKIPPED[/]"); pass a
    `Text` for anything read straight out of the database, since arbitrary content --
    an `error_text` holding "[...]", a path with a bracket -- would otherwise be parsed
    as markup and either vanish or raise.
    """
    t = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2))
    t.add_column(style=key_style, no_wrap=True)
    t.add_column(overflow="fold")
    for k, v in pairs:
        t.add_row(k, Text("—") if v is None else v if isinstance(v, (str, Text)) else Text(str(v)))
    out.print(t)


def _counts_line(label: str, counts: dict[str, int], styles: dict[str, str]) -> None:
    """`severity: 7 CRITICAL · 25 HIGH · …` with each token in its own colour."""
    if not counts:
        out.print(f"[dim]{label}: (none)[/]")
        return
    line = Text(f"{label}: ", style="dim")
    for i, (k, n) in enumerate(counts.items()):
        if i:
            line.append(" · ", style="dim")
        line.append(f"{n} {k.upper()}", style=styles.get(k, ""))
    out.print(line)


def _findings_table(findings: list[dict], *, title: str | None = None) -> None:
    # Every DB-sourced string is wrapped in Text(): finding titles genuinely contain
    # square brackets (CMP-016's is "... carry well_id = [0] — kept, ...", and every
    # GEN-RANGE title ends in "[1800000.0, 2800000.0]"), which rich would otherwise
    # parse as a markup tag and silently swallow.
    if not findings:
        out.print("[dim]  (no findings)[/]")
        return
    t = Table(title=title, box=box.SIMPLE_HEAD, header_style="bold", expand=True)
    t.add_column("severity", no_wrap=True)
    t.add_column("class", no_wrap=True, style="dim")
    t.add_column("check", no_wrap=True, style="cyan")
    t.add_column("affected", justify="right", no_wrap=True)
    t.add_column("title", overflow="fold")
    for f in findings:
        t.add_row(
            _sev(f["severity"]), Text(f["finding_class"]), Text(f["check_id"]),
            Text(_fmt(f.get("affected_count") or 0)), Text(f["title"]),
        )
    out.print(t)


def _normalisation_table(actions: list[dict]) -> None:
    if not actions:
        out.print("[dim]  (nothing removed or rewritten this run)[/]")
        return
    t = Table(box=box.SIMPLE_HEAD, header_style="bold", expand=True)
    t.add_column("kind", no_wrap=True, style="cyan")
    t.add_column("target", overflow="fold")
    t.add_column("rows removed", justify="right", no_wrap=True, style="red")
    t.add_column("of total", justify="right", no_wrap=True, style="dim")
    for a in actions:
        t.add_row(Text(a["kind"]), Text(a["target"]),
                  Text(_fmt(a["rows_affected"])), Text(_fmt(a["rows_total"])))
    out.print(t)


# --------------------------------------------------------------------------- doctor
def cmd_doctor(_: argparse.Namespace) -> int:
    s = get_settings()
    ok = True

    _hr("CONFIGURATION")
    print(f"  source          : {s.db_server}:{s.db_port}/{s.db_name} as {s.db_user}")
    print(f"  findings store  : {s.store_path}")
    print(f"  output dir      : {s.output_dir}")
    print(f"  config dir      : {s.config_dir}")

    _hr("SOURCE DATABASE")
    src = SourceDatabase(s)
    try:
        info = src.server_info()
        print(f"  connected       : {info.get('db_name')} "
              f"(SQL Server {info.get('product_version')})")
        print(f"  login           : {info.get('login_name')}")
        print(f"  collation       : {info.get('collation')}")
        print(f"  server date     : {info.get('server_today')}  "
              f"(all deadlines are judged against this)")
        print(f"  db_datareader   : {bool(info.get('is_datareader'))}")
        print(f"  db_owner        : {bool(info.get('is_db_owner'))}")
        print(f"  db_datawriter   : {bool(info.get('is_datawriter'))}")
        print(f"  can INSERT      : {bool(info.get('can_insert'))}")
        print(f"  can CREATE TABLE: {bool(info.get('can_create_table'))}")
        try:
            src.assert_read_only_account()
            print("  read-only       : VERIFIED — the account cannot write")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  read-only       : FAILED — {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"  connection      : FAILED — {exc}")
        return 1

    _hr("READ-ONLY GUARD")
    from app.db.source import ReadOnlyGuard, ReadOnlyViolation

    probes = [
        ("SELECT 1", True),
        ("WITH x AS (SELECT 1 AS a) SELECT * FROM x", True),
        ("DELETE FROM well.well_master", False),
        ("SELECT 1; DROP TABLE t", False),
        ("SELECT 1 -- ; DROP TABLE t", True),
        ("UPDATE t SET a = 1", False),
        ("EXEC sp_who", False),
        ("SELECT * FROM t WHERE name = 'DROP TABLE x'", True),
    ]
    for sql, should_pass in probes:
        try:
            ReadOnlyGuard.validate(sql)
            passed = True
        except ReadOnlyViolation:
            passed = False
        mark = "ok  " if passed == should_pass else "FAIL"
        if passed != should_pass:
            ok = False
        print(f"  [{mark}] {'allow' if should_pass else 'block'}: {sql[:46]}")

    _hr("SCOPE")
    scope = Scope(s)
    counts = src.table_row_counts()
    tables = scope.filter_tables(counts)
    in_scope_rows = sum(counts.get(t.full, 0) for t in tables)
    print(f"  tables in scope : {len(tables)} of {len(counts)}")
    print(f"  rows in scope   : {in_scope_rows:,}")
    print(f"  sampled above   : {scope.large_table_row_limit:,} rows "
          f"({scope.sample_percent}% TABLESAMPLE)")
    sampled = [t.full for t in tables if counts.get(t.full, 0) > scope.large_table_row_limit]
    if sampled:
        print(f"  will be sampled : {', '.join(sampled)}")

    _hr("NORMALISATION SPEC")
    try:
        spec = load_spec()
        print(f"  snapshot rules  : {len(spec.snapshots)}")
        for r in spec.snapshots:
            print(f"      {r.table} pinned on {r.timestamp_column} "
                  f"per {r.partition_by or '(whole table)'}")
        active = [r for r in spec.dedup if r.enabled]
        print(f"  dedup rules     : {len(active)} active, "
              f"{len(spec.dedup) - len(active)} disabled")
        for r in spec.dedup:
            flag = "" if r.enabled else "  [DISABLED]"
            print(f"      {r.table} on ({', '.join(r.keys)}) mode={r.mode}{flag}")
        print(f"  tables with declared semantics : {len(spec.tables)}")
        print(f"  PII patterns    : {len(spec.pii_columns)}")
        missing = [
            t for t in sorted(spec.tables_touched)
            if t not in counts
        ]
        if missing:
            ok = False
            print(f"  CONFIG ERROR    : rules name tables that do not exist: {missing}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  spec            : FAILED — {exc}")

    _hr("FINDINGS STORE")
    try:
        store = FindingsStore(s)
        runs = store.list_runs(limit=3)
        print(f"  path            : {store.path}")
        print(f"  schema version  : {store.migrate()}")
        print(f"  runs recorded   : {len(store.list_runs(limit=10_000))}")
        for r in runs:
            print(f"      {r['run_id']}  {r['status']:9}  findings={r['findings_total']}")
        store.close()
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  store           : FAILED — {exc}")

    src.close()
    print(f"\n{'=' * 78}\n  {'ALL CHECKS PASSED' if ok else 'PROBLEMS FOUND — see FAIL lines above'}\n{'=' * 78}")
    return 0 if ok else 1


# ------------------------------------------------------------------------------ run
def cmd_run(args: argparse.Namespace) -> int:
    orch = Orchestrator()
    try:
        outcome = orch.run(
            tables=args.tables,
            triggered_by=args.by,
            dry_run=args.dry_run,
            enrich=not args.no_llm,
            generate_reports=not args.no_reports,
        )
    finally:
        orch.close()

    n = outcome.normalisation
    status_style = STATUS_STYLE.get(outcome.status.value, "")
    _hr(f"RUN {outcome.run_id} — [{status_style}]{outcome.status.value.upper()}[/]"
        + ("  [yellow](DRY RUN, nothing persisted)[/]" if args.dry_run else ""))
    rows: list[tuple[str, Any]] = [
        ("duration", f"{outcome.seconds}s"),
        ("tables normalised", n.get("tables_normalised", 0)),
        ("rows raw", _fmt(n.get("rows_raw", 0))),
        ("rows effective", _fmt(n.get("rows_effective", 0))),
        ("rows set aside", f"{_fmt(n.get('rows_removed', 0))}"
                           "   [dim]<- excluded from the analysis, NOT from the database[/]"),
        ("findings", outcome.findings_written),
        ("normalisation log", f"{outcome.actions_written} actions"),
    ]
    if outcome.lifecycle:
        lc = outcome.lifecycle
        rows.append(("lifecycle", f"{lc.get('new', 0)} new, "
                                  f"{lc.get('recurring', 0)} recurring, "
                                  f"{lc.get('resolved', 0)} resolved"))
    if outcome.phases_skipped:
        rows.append(("phases skipped", f"[dim]{', '.join(outcome.phases_skipped)}[/]"))
    if outcome.llm_enabled:
        rows.append(("LLM enrichment",
                     f"{outcome.llm_findings_narrated} narrated, "
                     f"{outcome.llm_findings_reused} reused from cache, "
                     f"{outcome.llm_incidents} incidents, "
                     f"{outcome.llm_usage.get('total_tokens', 0):,} tokens"))
    elif outcome.llm_skip_reason:
        rows.append(("LLM enrichment", f"[yellow]SKIPPED[/] ({outcome.llm_skip_reason})"))
    if outcome.xlsx_path:
        rows.append(("Excel report", outcome.xlsx_path))
    if outcome.docx_path:
        rows.append(("Word report", outcome.docx_path))
    _kv(rows)

    if snaps := n.get("snapshots_pinned"):
        _hr("SNAPSHOTS PINNED")
        for table, detail in snaps.items():
            parts = detail.get("partitions", [])
            print(f"  {table}  on {detail.get('timestamp_column')}"
                  f" per {detail.get('partition_by')}")
            for p in parts[:10]:
                avail = p.get("snapshots_available")
                pin = p.get("pinned_at")
                keys = {k: v for k, v in p.items()
                        if k not in ("pinned_at", "snapshots_available")}
                print(f"      {keys} -> {pin}  ({avail} snapshots available)")

    if not args.dry_run:
        store = FindingsStore()
        _hr("NORMALISATION ACTIONS")
        _normalisation_table(store.normalisation_actions(outcome.run_id))
        _hr("FINDINGS BY SEVERITY / CLASS")
        _counts_line("severity", store.severity_counts(outcome.run_id), SEVERITY_STYLE)
        _counts_line("class", store.class_counts(outcome.run_id), {})
        _hr("FINDINGS")
        _findings_table(store.findings(outcome.run_id, limit=40))
        store.close()
    return 0 if outcome.status.value == "completed" else 1


# ----------------------------------------------------------------------------- show
def cmd_show(args: argparse.Namespace) -> int:
    store = FindingsStore()
    try:
        run = (
            store.latest_run(completed_only=False)
            if args.run in (None, "latest")
            else store.get_run(args.run)
        )
        if not run:
            print("no runs recorded — try: python -m app.cli run")
            return 1
        status_style = STATUS_STYLE.get(run["status"], "")
        _hr(f"RUN {run['run_id']} — [{status_style}]{run['status'].upper()}[/]")
        _kv([
            (k, Text(str(run[k])))  # raw DB values -- never markup-parsed, see _kv
            for k in (
                "started_at", "finished_at", "db_name", "as_of_date",
                "rows_scanned", "findings_total", "checks_skipped", "error_text",
            ) if run.get(k) is not None
        ])
        _hr("SEVERITY / CLASS")
        _counts_line("severity", store.severity_counts(run["run_id"]), SEVERITY_STYLE)
        _counts_line("class", store.class_counts(run["run_id"]), {})
        _hr("NORMALISATION ACTIONS")
        _normalisation_table(store.normalisation_actions(run["run_id"]))
        _hr(f"FINDINGS (top {args.limit})")
        findings = store.findings(run["run_id"], limit=args.limit)
        _findings_table(findings)
        if args.evidence:
            for f in findings:
                out.print()
                out.print(Text(f"{f['check_id']}  ", style="bold cyan").append(f["title"]))
                if f.get("why_it_matters"):
                    out.print(Text("  why: ", style="dim").append(f["why_it_matters"][:300]))
                if f.get("evidence"):
                    # evidence is a raw JSON string -- always contains brackets.
                    out.print(Text("  evidence: ", style="dim").append(str(f["evidence"])[:400]))
        if args.wells:
            _hr("WELL SCORECARD (top 20)")
            rows = store.well_scorecard(run["run_id"], limit=20)
            if rows:
                t = Table(box=box.SIMPLE_HEAD, header_style="bold")
                for col in ("well_id", "findings", "actionable", "critical"):
                    t.add_column(col, justify="right", no_wrap=True)
                for r in rows:
                    t.add_row(str(r["well_id"]), str(r["findings"]),
                              str(r["actionable"] or 0), str(r["critical"] or 0))
                out.print(t)
            else:
                out.print("[dim]  (no well-linked findings yet — Phase 2 adds those)[/]")
        return 0
    finally:
        store.close()


# ------------------------------------------------------------------------------ sql
def cmd_sql(args: argparse.Namespace) -> int:
    src = SourceDatabase()
    try:
        n = Normaliser(src)
        ref = TableRef.parse(args.table)
        source = n.build(ref)
        _hr(f"NORMALISED SOURCE — {source.name}")
        print(f"  grain      : {source.grain.describe()}")
        print(f"  rows raw   : {source.rows_raw:,}")
        print(f"  rows after : {source.rows_effective:,}  (removed {source.rows_removed:,})")
        print(f"  sampled    : {source.sampled}")
        print(f"  transforms : {source.transforms or ['none']}")
        print(f"\n{source.sql}\n")
        if args.json:
            print(json.dumps(n.summary(), indent=2, default=str))
        return 0
    finally:
        src.close()


# ---------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="app.cli", description="Data Quality & Anomaly Sentinel"
    )
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="verify connection, permissions, guard, scope, config")
    d.set_defaults(func=cmd_doctor)

    r = sub.add_parser("run", help="execute a full live run: normalise, check, enrich, report")
    r.add_argument("--tables", nargs="*", help="limit to these schema.table names")
    r.add_argument("--dry-run", action="store_true", help="do not write to the store")
    r.add_argument("--by", default="cli", help="who triggered this run")
    r.add_argument("--no-llm", action="store_true", help="skip LLM enrichment (Phase 4)")
    r.add_argument("--no-reports", action="store_true", help="skip Excel/Word generation")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("show", help="display a recorded run")
    s.add_argument("--run", default="latest", help="run_id, or 'latest'")
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--evidence", action="store_true")
    s.add_argument("--wells", action="store_true", help="include the well scorecard")
    s.set_defaults(func=cmd_show)

    q = sub.add_parser("sql", help="print the normalised SELECT for one table")
    q.add_argument("table", help="schema.table")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_sql)

    sc = sub.add_parser("schema", help="capture the schema and show drift vs the last run")
    sc.add_argument("--persist", action="store_true", help="write the snapshot under a probe run_id")
    sc.set_defaults(func=cmd_schema)

    ins = sub.add_parser(
        "introspect",
        help="rebuild .cache/{schema,hint_data}.txt -- the compile-time agent's knowledge base",
    )
    ins.set_defaults(func=cmd_introspect)

    rep = sub.add_parser(
        "report", help="rebuild Excel/Word from an existing run without re-scanning the DB"
    )
    rep.add_argument("--run", default="latest", help="run_id, or 'latest'")
    rep.add_argument("--llm", action="store_true", help="also (re-)run LLM enrichment first")
    rep.set_defaults(func=cmd_report)

    sug = sub.add_parser(
        "suggest", help="Tier 2: bounded agent proposes NEW candidate checks for review"
    )
    sug.add_argument("--list", action="store_true", help="list suggestions instead of running a session")
    sug.add_argument("--status", default=None, help="filter --list by status (pending|approved|rejected)")
    sug.add_argument("--approve", type=int, default=None, metavar="ID")
    sug.add_argument("--reject", type=int, default=None, metavar="ID")
    sug.add_argument("--note", default=None, help="review note for --approve/--reject")
    sug.add_argument("--max-calls", type=int, default=20)
    sug.add_argument("--max-seconds", type=int, default=120)
    sug.set_defaults(func=cmd_suggest)

    cmp_ = sub.add_parser(
        "compiled",
        help="review the compiled-agent's generated_check table (list/activate/disable)",
    )
    cmp_.add_argument("--list", action="store_true", help="list compiled checks")
    cmp_.add_argument(
        "--status", default=None,
        help="filter --list by status (needs_review|active|disabled)",
    )
    cmp_.add_argument("--activate", default=None, metavar="CHECK_ID")
    cmp_.add_argument("--disable", default=None, metavar="CHECK_ID")
    cmp_.set_defaults(func=cmd_compiled)
    return p


def main(argv: list[str] | None = None) -> int:
    configure()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


# --------------------------------------------------------------------------- report
def cmd_report(args: argparse.Namespace) -> int:
    """Regenerate Excel/Word for a run already in the store -- no database re-scan.

    Useful for iterating on report layout/wording without paying the cost of a full
    ~20M-row live run each time, and for adding LLM enrichment to a run that was
    generated with --no-llm.
    """
    from app.sentinel.llm.client import LLMClient
    from app.sentinel.llm.enrich import enrich_run
    from app.reporting.excel import build_excel_report
    from app.reporting.word import build_word_report

    s = get_settings()
    store = FindingsStore(s)
    try:
        run = (
            store.latest_run(completed_only=False)
            if args.run in (None, "latest")
            else store.get_run(args.run)
        )
        if not run:
            print("no runs recorded — try: python -m app.cli run")
            return 1
        run_id = run["run_id"]

        if args.llm:
            _hr("LLM ENRICHMENT")
            outcome = enrich_run(store, run_id, LLMClient(s))
            if outcome.skipped:
                print(f"  skipped: {outcome.skip_reason}")
            else:
                print(f"  narrated {outcome.findings_narrated} findings, "
                      f"{outcome.incidents_written} incidents, "
                      f"{outcome.usage.get('total_tokens', 0):,} tokens")
                if outcome.summary:
                    store.set_run_summary(run_id, outcome.summary)

        out_dir = s.output_dir / run_id
        xlsx_path = build_excel_report(store, run_id, out_dir / "Data_Anomaly_Report.xlsx")
        docx_path = build_word_report(store, run_id, out_dir / "Data_Anomaly_Report.docx")
        store.set_run_report_paths(run_id, xlsx_path=str(xlsx_path), docx_path=str(docx_path))

        _hr(f"REPORTS BUILT — {run_id}")
        print(f"  Excel : {xlsx_path}")
        print(f"  Word  : {docx_path}")
        return 0
    finally:
        store.close()


# -------------------------------------------------------------------------- suggest
def cmd_suggest(args: argparse.Namespace) -> int:
    """Tier 2 suggestion agent: propose, list, approve, or reject candidate NEW checks.

    Nothing this command touches can affect a report by itself -- see suggest.py's
    module docstring for the three-tier model and why that boundary is enforced in code,
    not just by convention.
    """
    s = get_settings()
    store = FindingsStore(s)
    try:
        if args.approve is not None:
            return _suggest_approve(store, s, args.approve, args.note)
        if args.reject is not None:
            sug = store.get_suggestion(args.reject)
            if not sug:
                print(f"no suggestion #{args.reject}")
                return 1
            store.review_suggestion(args.reject, status="rejected", note=args.note)
            print(f"suggestion #{args.reject} rejected"
                  + (f": {args.note}" if args.note else ""))
            return 0
        if args.list:
            rows = store.suggestions(status=args.status)
            _hr(f"SUGGESTIONS{f' (status={args.status})' if args.status else ''}")
            if not rows:
                out.print("[dim]  (none)[/]")
                return 0
            t = Table(box=box.SIMPLE_HEAD, header_style="bold", expand=True)
            t.add_column("#", justify="right", no_wrap=True)
            t.add_column("status", no_wrap=True)
            t.add_column("severity", no_wrap=True)
            t.add_column("family", no_wrap=True, style="dim")
            t.add_column("table", no_wrap=True, style="dim")
            t.add_column("rows", justify="right", no_wrap=True, style="dim")
            t.add_column("title", overflow="fold")
            review_style = {"pending": "yellow", "approved": "green", "rejected": "dim red"}
            for r in rows:
                t.add_row(
                    str(r["suggestion_id"]),
                    Text(r["status"], style=review_style.get(r["status"], "")),
                    _sev(r["severity_guess"]),
                    Text(r["family"] or ""),
                    Text(str(r.get("table_ref") or "")),
                    Text(str(r.get("test_row_count") if r.get("test_row_count") is not None else "")),
                    Text(r["title"]),
                )
            out.print(t)
            return 0

        # default: run a new exploration session
        from app.sentinel.checks import business_rules  # noqa: F401 -- populate the registry
        from app.sentinel.llm.client import LLMClient
        from app.sentinel.llm.suggest import run_suggestion_session

        src = SourceDatabase(s)
        try:
            outcome = run_suggestion_session(
                store, src, Scope(s), LLMClient(s),
                max_calls=args.max_calls, max_seconds_total=args.max_seconds,
            )
        finally:
            src.close()

        _hr(f"SUGGESTION SESSION {outcome.session_id}")
        if outcome.skipped:
            print(f"  skipped: {outcome.skip_reason}")
            return 1
        print(f"  rounds          : {outcome.rounds_used}")
        print(f"  tool calls used : {outcome.tool_calls_used}")
        print(f"  stopped because : {outcome.stopped_reason}")
        print(f"  tokens          : {outcome.usage.get('total_tokens', 0):,}")
        print(f"  PROPOSALS MADE  : {outcome.proposals_made}")
        if outcome.proposals_made:
            print(f"\n  review with: python -m app.cli suggest --list --status pending")
        return 0
    finally:
        store.close()


def cmd_compiled(args: argparse.Namespace) -> int:
    """Human review queue for `generated_check` (checks/compiled.py).

    A row only ever runs as a live check once it is 'active' -- this command is the ONLY
    supported way to move it there. There is deliberately no `--add` here: writing new SQL
    is the compile-time agent's job (or a human editing the table directly for a hand-
    written proof, as tests/test_compiled_checks.py does); this command is about the
    human-in-the-loop gate on what a written row is allowed to do, not about writing rows.
    """
    s = get_settings()
    store = FindingsStore(s)
    try:
        if args.activate is not None:
            row = store.get_generated_check(args.activate)
            if not row:
                print(f"no generated_check {args.activate!r}")
                return 1
            store.set_generated_check_status(args.activate, "active")
            print(f"{args.activate}: -> active (will register on the next run)")
            return 0
        if args.disable is not None:
            row = store.get_generated_check(args.disable)
            if not row:
                print(f"no generated_check {args.disable!r}")
                return 1
            store.set_generated_check_status(args.disable, "disabled")
            print(f"{args.disable}: -> disabled")
            return 0

        rows = store.generated_checks(status=args.status)
        _hr(f"COMPILED CHECKS{f' (status={args.status})' if args.status else ''}")
        if not rows:
            out.print("[dim]  (none)[/]")
            return 0
        t = Table(box=box.SIMPLE_HEAD, header_style="bold", expand=True)
        t.add_column("check_id", no_wrap=True)
        t.add_column("status", no_wrap=True)
        t.add_column("severity", no_wrap=True)
        t.add_column("table", no_wrap=True, style="dim")
        t.add_column("grain", no_wrap=True, style="dim")
        t.add_column("created_by", no_wrap=True, style="dim")
        t.add_column("title", overflow="fold")
        status_style = {"needs_review": "yellow", "active": "green", "disabled": "dim red"}
        for r in rows:
            t.add_row(
                r["check_id"],
                Text(r["status"], style=status_style.get(r["status"], "")),
                _sev(r["severity"]),
                Text(r["table_ref"] or ""),
                Text(r["grain_kind"] or ""),
                Text(r["created_by"] or ""),
                Text(r["title"]),
            )
        out.print(t)
        return 0
    finally:
        store.close()


def _suggest_approve(store: FindingsStore, s, suggestion_id: int, note: str | None) -> int:
    from app.sentinel.llm.suggest import generate_boilerplate

    sug = store.get_suggestion(suggestion_id)
    if not sug:
        print(f"no suggestion #{suggestion_id}")
        return 1
    out_dir = s.output_dir / "suggested_checks"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"suggestion_{suggestion_id:04d}.py.suggested"
    path.write_text(generate_boilerplate(sug), encoding="utf-8")
    store.review_suggestion(
        suggestion_id, status="approved", note=note, boilerplate_path=str(path)
    )
    print(f"suggestion #{suggestion_id} approved. Boilerplate written to:\n  {path}")
    print("\nThis file ends in .py.suggested and will NOT be imported or run as-is.")
    print("Read it, fix the TODOs, rename to .py, and move it into a real check module")
    print("before it can ever appear in a report.")
    return 0


# --------------------------------------------------------------------------- schema
def cmd_schema(args: argparse.Namespace) -> int:
    """Capture the current in-scope schema and show drift vs the last run.

    Proves the "new table/column handled automatically" property concretely: run once,
    add a column in the source DB (or just re-run against a changed schema), run again --
    the second run reports the drift without any code change.
    """
    from app.sentinel.schema_snapshot import SchemaSnapshotter

    src = SourceDatabase()
    store = FindingsStore()
    try:
        snap = SchemaSnapshotter(src, Scope())
        snapshots = snap.capture()
        _hr(f"SCHEMA SNAPSHOT — {len(snapshots)} tables in scope")
        cols_total = sum(len(s.columns) for s in snapshots.values())
        print(f"  columns total : {cols_total}")

        run_id_probe = FindingsStore.new_run_id()
        if args.persist:
            from app.domain.models import Run, utcnow
            # metrics carry a foreign key to run(run_id) -- create a real run row
            # first, exactly as the orchestrator does before calling persist().
            store.create_run(Run(run_id=run_id_probe, started_at=utcnow(),
                                  triggered_by="cli schema --persist"))
        diff = snap.diff_against_baseline(store, run_id_probe, snapshots)
        if diff.baseline_run_id is None:
            print("  no previous completed run to diff against — this establishes the baseline")
        else:
            _hr(f"DRIFT vs {diff.baseline_run_id}")
            if not diff.has_drift:
                print("  no drift — schema is unchanged")
            else:
                for t in diff.tables_added:
                    print(f"  [new table]      {t}")
                for t in diff.tables_removed:
                    print(f"  [removed table]  {t}")
                for t, cols in diff.columns_added.items():
                    print(f"  [new columns]    {t}: {', '.join(cols)}")
                for t, cols in diff.columns_removed.items():
                    print(f"  [removed cols]   {t}: {', '.join(cols)}")
                for t, cols in diff.columns_changed.items():
                    print(f"  [changed cols]   {t}: {', '.join(cols)}")
            findings = snap.findings_for(diff)
            print(f"\n  would emit {len(findings)} drift finding(s) on a real run")
        if args.persist:
            n = snap.persist(store, run_id_probe, snapshots)
            from app.domain.models import RunStatus as _RS
            store.finish_run(run_id_probe, _RS.COMPLETED)
            print(f"\n  persisted {n} metric rows under probe run_id {run_id_probe}"
                  "  (not a real run — for inspection only)")
        return 0
    finally:
        src.close()
        store.close()


# ------------------------------------------------------------------------ introspect
def cmd_introspect(_: argparse.Namespace) -> int:
    """Rebuild the compile-time agent's knowledge base: `.cache/schema.txt` (columns,
    PK/FK, declared or measured-candidate grain) and `.cache/hint_data.txt` (null%,
    distinct counts, small-lookup values -- PII already excluded).

    Read-only, and independent of a normal report run: this never registers a check or
    writes a finding. Re-run it whenever the source schema changes; `fingerprints.json`
    is what a later compile step uses to tell which tables actually need re-compiling.
    """
    from app.sentinel.introspect import build_knowledge_base

    _hr("INTROSPECT — rebuilding the agent's knowledge base")
    paths = build_knowledge_base()
    for label, path in paths.items():
        size = Path(path).stat().st_size
        print(f"  {label:12}: {path}  ({size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
