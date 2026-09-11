"""Command-line interface.

    python -m app.cli check         connectivity + configuration readout
    python -m app.cli introspect    rebuild schema.txt / value_hints.txt / numeric_hints.txt
    python -m app.cli show <what>   print a cached artefact (schema | values | numbers)
    python -m app.cli list          parse domain/data_anomalies.md and show every rule

Commands added in later phases: compile, run, explain. `check` deliberately comes first:
almost every failure in this system is a connection, driver or ALLOWED_SCHEMAS problem, and
diagnosing that should never require running an expensive compile to find out. `list` needs no
database at all, so a rule file can be corrected offline.
"""
from __future__ import annotations

import argparse
import os
import sys

# Windows' console defaults to a narrow codepage (cp1252) that cannot represent characters a
# schema or an LLM answer routinely contains - an em dash, a curly quote. print() then raises
# UnicodeEncodeError and kills the run, discarding everything gathered so far. errors="replace"
# swaps only the unrepresentable character rather than losing the whole run.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app.config import settings  # noqa: E402
from app.observability import setup_logging  # noqa: E402


def _console():
    from rich.console import Console

    return Console()


def cmd_check(_args) -> int:
    """Connectivity plus every setting that decides what the engine can see.

    Prints the configuration BEFORE attempting the connection, so a failure is diagnosable from
    the same output that caused it.
    """
    from rich.table import Table

    from app.db.connection import ping

    console = _console()

    cfg = Table(title="Configuration", show_header=True, header_style="bold cyan")
    cfg.add_column("Setting")
    cfg.add_column("Value", overflow="fold")
    cfg.add_row("database", f"{settings.db_name} @ {settings.db_server}:{settings.db_port}")
    cfg.add_row("user", settings.db_user or "[red]not set[/]")
    cfg.add_row("driver", settings.db_driver or "(auto-detect)")
    cfg.add_row("allowed schemas", ", ".join(settings.allowed_schemas) or "[red]none set[/]")
    cfg.add_row("excluded tables", ", ".join(settings.excluded_tables) or "(none)")
    cfg.add_row("excluded columns", ", ".join(settings.excluded_columns) or "(none)")
    cfg.add_row("llm provider", settings.llm_provider)
    cfg.add_row("main model", settings.active_model)
    fast = settings.fast_model
    cfg.add_row(
        "fast model",
        fast if fast != settings.active_model else f"{fast} [dim](not set - uses main)[/]",
    )
    cfg.add_row("read uncommitted", str(settings.read_uncommitted))
    cfg.add_row(
        "row caps",
        f"llm sample {settings.sample_rows} / word {settings.report_rows:,} / "
        f"excel {settings.export_max_rows:,}",
    )
    cfg.add_row("timeouts", f"summary {settings.query_timeout}s / detail {settings.detail_timeout}s")
    console.print(cfg)

    if not settings.db_server or not settings.db_name:
        console.print("[red]DB_SERVER / DB_NAME are not set - copy .env.example to .env[/]")
        return 2
    if not settings.allowed_schemas:
        console.print("[red]ALLOWED_SCHEMAS is empty - nothing would be introspected[/]")
        return 2

    console.print("\nConnecting...")
    ok, message = ping()
    if not ok:
        console.print(f"[red]FAILED[/]: {message}")
        console.print(
            "\n[yellow]Common causes:[/] the Microsoft ODBC driver is not installed "
            "(see README), the server is unreachable from this machine, or DB_USER/DB_PASSWORD "
            "are wrong."
        )
        return 1
    console.print(f"[green]OK[/]: {message}")

    if settings._is_openai and not settings.openai_api_key:
        console.print(
            "[yellow]Note:[/] OPENAI_API_KEY is blank. `check` and `introspect` work without "
            "it; `compile` and `run` will not."
        )
    return 0


def cmd_introspect(args) -> int:
    """Rebuild every cached description of the database.

    Always a full rebuild - that is the point of the command. Normal operation refreshes these
    automatically when the fingerprint moves; this is the manual override for when you want to
    see the effect of an ALLOWED_SCHEMAS or EXCLUDED_TABLES change immediately.
    """
    from app.db.connection import ping
    from app.db.introspect import refresh

    console = _console()
    ok, message = ping()
    if not ok:
        console.print(f"[red]Cannot connect:[/] {message}")
        return 1

    console.print(
        f"Introspecting [bold]{settings.db_name}[/] on {settings.db_server} "
        f"(schemas: {', '.join(settings.allowed_schemas)})"
    )
    console.print(
        "[dim]numeric_hints.txt runs one aggregate pass per table - on a large database this "
        "is the slow step, and it is cached afterwards.[/]"
    )
    schema, _values, _numbers = refresh(verbose=True)
    if not schema.strip():
        console.print("[red]Empty schema.[/] Check DB_NAME and ALLOWED_SCHEMAS.")
        return 1
    console.print(f"[green]Done.[/] Written to {os.path.relpath(_cache_dir())}")
    if args.print:
        console.print("\n" + schema)
    return 0


def _cache_dir() -> str:
    from app.db import introspect

    return introspect._CACHE_DIR


_SHOW_FILES = {
    "schema": "schema.txt",
    "values": "value_hints.txt",
    "numbers": "numeric_hints.txt",
    "fingerprint": "schema.fingerprint",
    # The compiled SQL itself. Inspectable on purpose: the probes decide what the report says,
    # so being able to read exactly what will run - without a database or a model - is what
    # makes a finding auditable.
    "catalog": "anomaly_catalog.json",
}


def cmd_show(args) -> int:
    """Print a cached artefact, so what the agents will actually see is inspectable."""
    console = _console()
    name = _SHOW_FILES.get(args.what)
    path = os.path.join(_cache_dir(), name)
    if not os.path.exists(path):
        console.print(
            f"[yellow]{name} does not exist yet.[/] Run: python -m app.cli introspect"
        )
        return 1
    with open(path, encoding="utf-8") as fh:
        sys.stdout.write(fh.read())
    return 0


_SEVERITY_COLOUR = {"critical": "bold red", "high": "red", "medium": "yellow", "low": "dim"}


def cmd_list(args) -> int:
    """Parse the rule files and show what the engine would run. Needs no database.

    Returns non-zero when any rule failed to parse. A malformed rule is never quietly skipped:
    an operator believing a check is running when it is not is the worst failure mode a
    data-quality tool has.
    """
    from rich.table import Table

    from app.rules.loader import load_rules

    console = _console()
    rules, errors = load_rules()

    if not rules and not errors:
        console.print(
            "[yellow]No rules found.[/] Expected '## RULE <id> - <title>' headings in "
            "domain/data_anomalies.md"
        )
        return 1

    if rules:
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("ID")
        table.add_column("Title", overflow="fold", max_width=44)
        table.add_column("Category")
        table.add_column("Sev")
        table.add_column("Method")
        table.add_column("SQL mode")
        table.add_column("Status")
        for r in sorted(rules, key=lambda x: (x.severity_rank, x.rule_id)):
            colour = _SEVERITY_COLOUR.get(r.severity, "white")
            status = r.status if r.runnable else f"[dim]{r.status}[/]"
            table.add_row(
                r.rule_id, r.title, r.category,
                f"[{colour}]{r.severity}[/]", r.method, r.sql_mode, status,
            )
        console.print(table)

        active = [r for r in rules if r.runnable]
        pinned = sum(1 for r in active if r.sql_mode == "pinned")
        seeded = sum(1 for r in active if r.sql_mode == "seed")
        authored = sum(1 for r in active if r.sql_mode == "authored")
        # The estimate is what makes the sql_mode trade-off concrete before anyone spends it.
        est = pinned * 1 + seeded * 3 + authored * 4
        console.print(
            f"\n{len(active)} active of {len(rules)} rule(s): "
            f"{pinned} pinned, {seeded} seed, {authored} authored"
        )
        console.print(
            f"[dim]Estimated compile cost: ~{est} LLM calls (a detection run costs 1, "
            f"regardless).[/]"
        )

    if errors:
        console.print(f"\n[red]{len(errors)} rule(s) could NOT be parsed and will NOT run:[/]")
        for e in errors:
            console.print(f"  [red]x[/] {e}")
        return 1

    if args.verbose:
        for r in sorted(rules, key=lambda x: x.rule_id):
            console.print(f"\n[bold]{r.rule_id}[/] {r.title}  [dim]({r.rule_hash})[/]")
            if r.params:
                console.print(
                    "  params: " + ", ".join(f"{k}={v}" for k, v in sorted(r.params.items()))
                )
            console.print(f"  summary sql: {len(r.summary_sql)} chars")
            console.print(f"  detail sql : {len(r.detail_sql)} chars")
    return 0


def cmd_compile(args) -> int:
    """Turn every rule into a validated, executed, reviewed pair of SQL probes.

    The expensive command, and the only one that spends LLM calls in bulk. It is also the one
    that should almost never need running: a compiled probe survives a data reload, and only a
    rule edit or a real structure change makes one stale.

    --dry-run answers the question people actually have before running it ("what will this
    cost?") without spending anything, by making the same staleness decisions the real compile
    would and stopping there.
    """
    from rich.table import Table

    from app.compiler import compile_rules
    from app.db.connection import ping
    from app.rules import catalog as catalog_store

    console = _console()

    # Checked BEFORE the connection, so "what would this cost?" can be answered offline. It
    # degrades rather than failing: without a database the fingerprint is unavailable, so it
    # reports what it can and says which part it could not determine.
    if args.dry_run:
        return _compile_dry_run(console, args)

    ok, message = ping()
    if not ok:
        console.print(f"[red]Cannot connect:[/] {message}")
        return 1

    if settings._is_openai and not settings.openai_api_key:
        console.print("[red]OPENAI_API_KEY is blank[/] - compiling needs a model. Set it in .env.")
        return 2

    console.print(
        f"Compiling against [bold]{settings.db_name}[/] "
        f"(budget: {settings.max_compile_calls} LLM calls, "
        f"main model {settings.active_model})"
    )

    catalog, report, errors = compile_rules(
        only=args.rule or None, force=args.force, retry_failed=args.retry_failed,
        source=args.source,
    )

    table = Table(show_header=True, header_style="bold cyan", title="Compile result")
    table.add_column("Outcome")
    table.add_column("Rules", justify="right")
    table.add_row("[green]compiled[/]", str(len(report.compiled)))
    table.add_row("[dim]reused (already current)[/]", str(len(report.reused)))
    table.add_row("[yellow]not applicable[/]", str(len(report.not_applicable)))
    table.add_row("[red]failed[/]", str(len(report.failed)))
    if report.removed:
        table.add_row("[dim]removed (rule deleted)[/]", str(len(report.removed)))
    console.print(table)
    console.print(
        f"{report.llm_calls} LLM call(s) in {report.seconds:.1f}s. "
        f"Catalog: {catalog_store.path()}"
    )

    if report.usage:
        usage = Table(show_header=True, header_style="bold cyan", title="Tokens")
        usage.add_column("Agent")
        usage.add_column("Model")
        usage.add_column("Calls", justify="right")
        usage.add_column("In", justify="right")
        usage.add_column("Out", justify="right")
        for row in report.usage:
            usage.add_row(
                row["agent"], row["model"], str(row["calls"]),
                f"{row['input']:,}", f"{row['output']:,}",
            )
        console.print(usage)

    # Anything NOT running is printed last, where it cannot be scrolled past. A rule the
    # operator believes is running when it is not is the worst failure this tool has.
    for rule_id in report.not_applicable:
        probe = catalog.get(rule_id)
        console.print(f"  [yellow]-[/] {rule_id} not applicable: {probe.error if probe else ''}")
    for rule_id in report.failed:
        probe = catalog.get(rule_id)
        console.print(f"  [red]x[/] {rule_id} FAILED: {(probe.error if probe else '')[:200]}")
    if report.stopped_early:
        console.print(f"[yellow]Stopped early:[/] {report.stopped_early}")
    for problem in errors:
        console.print(f"  [red]x[/] {problem}")

    # Non-zero when a check the operator asked for is not running, so CI notices.
    return 1 if (report.failed or errors) else 0


def _compile_dry_run(console, args) -> int:
    """Show what a compile WOULD do, and what it would cost. Spends nothing."""
    from rich.table import Table

    from app.compiler import _all_rules
    from app.db import introspect
    from app.rules import catalog as catalog_store

    rules, errors = _all_rules()
    if args.source:
        rules = [r for r in rules if r.source == args.source]
    if args.rule:
        wanted = {r.strip().lower() for r in args.rule}
        rules = [r for r in rules if r.rule_id.lower() in wanted]

    try:
        fingerprint = introspect.structure_fingerprint()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Structure fingerprint unavailable:[/] {exc}")
        fingerprint = ""

    catalog = catalog_store.load()
    # What each sql_mode costs, matching the graph's own routing: generic probes make no call
    # at all, pinned rules are grounded and reviewed, the rest are also authored (and may be
    # authored again if the reviewer sends them back).
    cost = {"generic": 0, "pinned": 2, "seed": 3, "authored": 4}

    stale: list[tuple[str, str, int]] = []
    current = 0
    for rule in rules:
        if not rule.runnable:
            continue
        needs, why = catalog_store.is_stale(
            catalog.get(rule.rule_id), rule, fingerprint, args.retry_failed
        )
        if args.force:
            needs, why = True, "forced"
        if not needs:
            current += 1
            continue
        key = "generic" if rule.source == "generic" else rule.sql_mode
        stale.append((rule.rule_id, why, cost.get(key, 4)))

    table = Table(show_header=True, header_style="bold cyan", title="Would compile")
    table.add_column("Rule")
    table.add_column("Why")
    table.add_column("LLM calls", justify="right")
    for rule_id, why, calls in stale[:60]:
        table.add_row(rule_id, why, str(calls) if calls else "[green]0[/]")
    if stale:
        console.print(table)
        if len(stale) > 60:
            console.print(f"[dim]... and {len(stale) - 60} more[/]")

    estimate = sum(c for _, _, c in stale)
    console.print(
        f"\n{len(stale)} rule(s) would be compiled, {current} already current.\n"
        f"Estimated cost: [bold]~{estimate}[/] LLM call(s) "
        f"(budget {settings.max_compile_calls}). A detection run costs 1, regardless."
    )
    if estimate > settings.max_compile_calls:
        console.print(
            "[yellow]The estimate exceeds ANOMALY_MAX_COMPILE_CALLS[/] - the compile would stop "
            "early. Raise the budget, or compile in batches with --rule."
        )
    for problem in errors:
        console.print(f"  [red]x[/] {problem}")
    return 1 if errors else 0


_SEVERITY_ORDER = ("critical", "high", "medium", "low")


def cmd_run(args) -> int:
    """Execute every compiled probe and write the Word and Excel reports.

    The cheap command, and the one meant to run on a schedule: one LLM call for the executive
    summary, no matter how many probes ran or how much they found.
    """
    from rich.table import Table

    from app.db.connection import ping
    from app.runner import run_detection

    console = _console()
    ok, message = ping()
    if not ok:
        console.print(f"[red]Cannot connect:[/] {message}")
        return 1

    formats = [f for f in (args.format or []) if f]
    final, summary = run_detection(only=args.rule or None, formats=formats)

    if final.get("catalog_note"):
        console.print(f"[yellow]{final['catalog_note']}[/]")

    totals = final.get("totals") or {}
    score = final.get("score", 0)
    colour = "green" if score >= 90 else "yellow" if score >= 70 else "red"
    console.print(f"\nData quality score: [{colour} bold]{score}[/] / 100")

    head = Table(show_header=True, header_style="bold cyan")
    head.add_column("Checks run", justify="right")
    head.add_column("With findings", justify="right")
    head.add_column("Clean", justify="right")
    head.add_column("Failed", justify="right")
    head.add_column("Records examined", justify="right")
    head.add_column("Records flagged", justify="right")
    head.add_row(
        str(totals.get("probes_run", 0)), str(totals.get("probes_with_findings", 0)),
        str(totals.get("probes_clean", 0)), str(totals.get("probes_failed", 0)),
        f"{totals.get('records_examined', 0):,}", f"{totals.get('records_flagged', 0):,}",
    )
    console.print(head)

    ranked = final.get("ranked") or []
    if ranked:
        table = Table(show_header=True, header_style="bold cyan", title="Findings, worst first")
        table.add_column("Rule")
        table.add_column("What was found", overflow="fold", max_width=48)
        table.add_column("Sev")
        table.add_column("Affected", justify="right")
        table.add_column("Of", justify="right")
        table.add_column("%", justify="right")
        for row in ranked[: args.top]:
            style = _SEVERITY_COLOUR.get(row["severity"], "white")
            table.add_row(
                row["rule_id"], row["title"], f"[{style}]{row['severity']}[/]",
                f"{row['anomaly_count']:,}", f"{row['scope_total']:,}",
                f"{row['anomaly_pct']:.2f}",
            )
        console.print(table)
        if len(ranked) > args.top:
            console.print(f"[dim]... and {len(ranked) - args.top} more (see the report)[/]")

    if final.get("summary"):
        console.print("\n[bold]Executive summary[/]")
        console.print(final["summary"])

    # Printed LAST, where it cannot be scrolled past: an unmonitored area is the finding a
    # reader is most likely to miss, because nothing appears to be wrong.
    gaps = final.get("empty_scope") or []
    if gaps:
        console.print(
            f"\n[yellow]{len(gaps)} check(s) examined NO records - this is a coverage gap, "
            f"not a clean result:[/] {', '.join(gaps[:12])}"
        )
    failures = [r for r in (final.get("results") or []) if not r.ok]
    for result in failures[:10]:
        console.print(f"  [red]x[/] {result.rule_id} failed: {result.error[:160]}")
    not_running = final.get("not_running") or []
    if not_running:
        console.print(
            f"[yellow]{len(not_running)} check(s) are not running at all[/] "
            f"(not compiled, failed to compile, or not applicable)."
        )

    for fmt, path in (final.get("report_paths") or {}).items():
        console.print(f"[green]{fmt}[/] -> {path}")
    if final.get("report_error"):
        console.print(f"[red]{final['report_error']}[/]")

    return 1 if (failures or final.get("report_error")) else 0


def cmd_runs(args) -> int:
    """Show the recorded run history - the trend, which one score alone cannot give."""
    from rich.table import Table

    from app.runner import history

    console = _console()
    runs = history()
    if not runs:
        console.print("[yellow]No runs recorded yet.[/] Run: python -m app.cli run")
        return 1

    table = Table(show_header=True, header_style="bold cyan", title="Run history")
    table.add_column("Run")
    table.add_column("When")
    table.add_column("Score", justify="right")
    table.add_column("Findings", justify="right")
    table.add_column("Flagged", justify="right")
    table.add_column("Took", justify="right")
    for run in runs[: args.limit]:
        score = run.score
        colour = "green" if score >= 90 else "yellow" if score >= 70 else "red"
        table.add_row(
            run.run_id, run.started_at[:19], f"[{colour}]{score}[/]",
            str(run.totals.get("probes_with_findings", 0)),
            f"{run.totals.get('records_flagged', 0):,}",
            f"{run.seconds:.0f}s",
        )
    console.print(table)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="anomaly",
        description="Data Quality & Anomaly Sentinel - agentic anomaly detection over any "
                    "MS SQL database.",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="suppress the colour pipeline trace"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="test the connection and print the configuration").set_defaults(
        func=cmd_check
    )

    p_intro = sub.add_parser(
        "introspect", help="rebuild schema.txt, value_hints.txt and numeric_hints.txt"
    )
    p_intro.add_argument("--print", action="store_true", help="also print the schema block")
    p_intro.set_defaults(func=cmd_introspect)

    p_show = sub.add_parser("show", help="print a cached artefact")
    p_show.add_argument("what", choices=sorted(_SHOW_FILES))
    p_show.set_defaults(func=cmd_show)

    p_list = sub.add_parser("list", help="parse the rule files and show every rule")
    p_list.add_argument("-v", "--verbose", action="store_true", help="show params and SQL sizes")
    p_list.set_defaults(func=cmd_list)

    p_compile = sub.add_parser(
        "compile", help="turn the rules into validated SQL probes (the expensive step)"
    )
    p_compile.add_argument(
        "--rule", action="append", metavar="ID",
        help="compile only this rule id; repeatable",
    )
    p_compile.add_argument(
        "--force", action="store_true",
        help="recompile even when the stored probe is still current",
    )
    p_compile.add_argument(
        "--retry-failed", action="store_true",
        help="also retry rules that failed to compile last time",
    )
    p_compile.add_argument(
        "--source", choices=("declared", "generic"),
        help="compile only the declared rules, or only the generated structural probes",
    )
    p_compile.add_argument(
        "--dry-run", action="store_true",
        help="show what would be compiled and what it would cost, without spending anything",
    )
    p_compile.set_defaults(func=cmd_compile)

    p_run = sub.add_parser(
        "run", help="execute the compiled probes and write the Word + Excel reports"
    )
    p_run.add_argument(
        "--rule", action="append", metavar="ID", help="run only this rule id; repeatable"
    )
    p_run.add_argument(
        "--format", action="append", choices=("xlsx", "docx"),
        help="produce only this format; repeatable. Default: both",
    )
    p_run.add_argument(
        "--top", type=int, default=20, help="how many findings to print (default 20)"
    )
    p_run.set_defaults(func=cmd_run)

    p_runs = sub.add_parser("runs", help="show the recorded run history and score trend")
    p_runs.add_argument("--limit", type=int, default=20, help="how many runs to show")
    p_runs.set_defaults(func=cmd_runs)

    args = parser.parse_args()
    setup_logging(console=not args.quiet)
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
