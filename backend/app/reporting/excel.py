"""Excel workbook -- the working deliverable. xlsxwriter, built entirely from a run's rows
in `FindingsStore`; nothing here computes a number itself, it only formats what the checks
and (optionally) the LLM layer already produced. If the LLM layer was skipped, the sheets
still render correctly from the deterministic `title`/`why_it_matters` fields alone --
narration columns are simply blank, never a crash or a placeholder claiming false analysis.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import xlsxwriter

from app.db.store import FindingsStore
from app.logging import get_logger
from app.reporting.labels import NO_DB_CHANGE_NOTE, action_label
from app.sentinel.business_rules_index import describe, referenced_sections

log = get_logger(__name__)

SEVERITY_COLOR = {
    "critical": "#C00000", "high": "#E36C09", "medium": "#BF9000",
    "low": "#548235", "review": "#7030A0", "info": "#2E75B6",
}


class ExcelReport:
    def __init__(self, store: FindingsStore, run_id: str) -> None:
        self.store = store
        self.run_id = run_id
        self.run = store.get_run(run_id)
        if self.run is None:
            raise ValueError(f"no such run: {run_id}")

    def build(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        wb = xlsxwriter.Workbook(str(path))
        fmt = self._formats(wb)
        self._sheet_summary(wb, fmt)
        self._sheet_findings(wb, fmt)
        self._sheet_incidents(wb, fmt)
        self._sheet_well_scorecard(wb, fmt)
        self._sheet_normalisation(wb, fmt)
        self._sheet_check_catalogue(wb, fmt)
        self._sheet_business_rules(wb, fmt)
        wb.close()
        log.info("excel.built", run_id=self.run_id, path=str(path))
        return path

    # ------------------------------------------------------------------------- formats
    def _formats(self, wb: xlsxwriter.Workbook) -> dict[str, Any]:
        f: dict[str, Any] = {}
        f["title"] = wb.add_format({"bold": True, "font_size": 16})
        f["subtitle"] = wb.add_format({"italic": True, "font_color": "#666666"})
        f["h2"] = wb.add_format({"bold": True, "font_size": 12, "bg_color": "#D9E1F2"})
        f["header"] = wb.add_format({
            "bold": True, "bg_color": "#305496", "font_color": "white",
            "border": 1, "text_wrap": True, "valign": "top",
        })
        f["wrap"] = wb.add_format({"text_wrap": True, "valign": "top"})
        f["num"] = wb.add_format({"num_format": "#,##0", "valign": "top"})
        f["pct"] = wb.add_format({"num_format": "0.0%", "valign": "top"})
        for sev, color in SEVERITY_COLOR.items():
            f[f"sev_{sev}"] = wb.add_format({
                "bold": True, "font_color": "white", "bg_color": color,
                "align": "center", "valign": "vcenter",
            })
        return f

    # ---------------------------------------------------------------- 1. Exec Summary
    def _sheet_summary(self, wb, fmt) -> None:
        ws = wb.add_worksheet("Executive Summary")
        ws.set_column("A:A", 28)
        ws.set_column("B:B", 70)
        r = self.run
        ws.write(0, 0, "Data Quality & Anomaly Sentinel", fmt["title"])
        ws.write(1, 0, f"Run {r['run_id']}", fmt["subtitle"])
        row = 3
        for label, value in [
            ("Database", r.get("db_name")),
            ("As-of date (live, from the server)", r.get("as_of_date")),
            ("Run started", r.get("started_at")),
            ("Run finished", r.get("finished_at")),
            ("Status", r.get("status")),
            ("Rows scanned", f"{r.get('rows_scanned', 0):,}"),
            ("Checks run", r.get("checks_run")),
            ("Checks passed cleanly", r.get("checks_passed")),
            ("Total findings", r.get("findings_total")),
        ]:
            ws.write(row, 0, label)
            ws.write(row, 1, value)
            row += 1

        summary = self.store.get_run_summary(self.run_id)
        row += 1
        if summary:
            ws.merge_range(row, 0, row, 1, "AI-Generated Executive Summary", fmt["h2"])
            row += 1
            ws.write(row, 0, "Headline")
            ws.write(row, 1, summary.get("headline", ""), fmt["wrap"])
            row += 2
            for para in summary.get("paragraphs", []):
                ws.merge_range(row, 0, row, 1, para, fmt["wrap"])
                row += 1
            row += 1
            if summary.get("top_priorities"):
                ws.write(row, 0, "Top priorities", fmt["h2"])
                row += 1
                for p in summary["top_priorities"]:
                    ws.write(row, 1, f"• {p}", fmt["wrap"])
                    row += 1
        else:
            ws.merge_range(
                row, 0, row, 1,
                "LLM enrichment was not run for this report (no API key configured, or "
                "disabled). All figures below are the deterministic engine's own output "
                "-- unaffected by this.", fmt["subtitle"],
            )
            row += 2

        row += 1
        ws.write(row, 0, "Findings by severity", fmt["h2"])
        row += 1
        for sev, n in sorted(
            self.store.severity_counts(self.run_id).items(),
            key=lambda kv: list(SEVERITY_COLOR).index(kv[0]) if kv[0] in SEVERITY_COLOR else 9,
        ):
            ws.write(row, 0, sev.upper(), fmt.get(f"sev_{sev}", fmt["wrap"]))
            ws.write(row, 1, n)
            row += 1
        row += 1
        ws.write(row, 0, "Findings by class", fmt["h2"])
        row += 1
        for cls, n in self.store.class_counts(self.run_id).items():
            ws.write(row, 0, cls)
            ws.write(row, 1, n)
            row += 1

    # ---------------------------------------------------------------- 2. Findings
    def _sheet_findings(self, wb, fmt) -> None:
        ws = wb.add_worksheet("Findings Register")
        headers = [
            "check_id", "family", "severity", "class", "title", "affected_count",
            "business_rule_ref", "owner", "grain", "baseline", "status",
            "why_it_matters", "llm_explanation", "llm_root_cause", "llm_remediation",
            "entity_id", "well_id",
        ]
        widths = [12, 8, 10, 10, 55, 12, 34, 10, 26, 12, 10, 45, 45, 40, 40, 24, 8]
        for i, (h, w) in enumerate(zip(headers, widths)):
            ws.write(0, i, h, fmt["header"])
            ws.set_column(i, i, w)
        ws.freeze_panes(1, 0)
        ws.autofilter(0, 0, 0, len(headers) - 1)

        for r, f in enumerate(self.store.findings(self.run_id, limit=5000), start=1):
            ws.write(r, 0, f["check_id"])
            ws.write(r, 1, f["family"])
            ws.write(r, 2, f["severity"], fmt.get(f"sev_{f['severity']}"))
            ws.write(r, 3, f["finding_class"])
            ws.write(r, 4, f["title"], fmt["wrap"])
            ws.write_number(r, 5, f["affected_count"] or 0, fmt["num"])
            ws.write(r, 6, describe(f.get("business_rule_ref")), fmt["wrap"])
            ws.write(r, 7, f.get("owner") or "")
            ws.write(r, 8, f.get("grain") or "", fmt["wrap"])
            ws.write(r, 9, f.get("baseline") or "")
            ws.write(r, 10, f.get("status") or "")
            ws.write(r, 11, f.get("why_it_matters") or "", fmt["wrap"])
            ws.write(r, 12, f.get("llm_explanation") or "", fmt["wrap"])
            ws.write(r, 13, f.get("llm_root_cause") or "", fmt["wrap"])
            ws.write(r, 14, f.get("llm_remediation") or "", fmt["wrap"])
            ws.write(r, 15, f.get("entity_id") or "")
            if f.get("well_id") is not None:
                ws.write_number(r, 16, f["well_id"])

    # --------------------------------------------------------------- 3. Incidents
    def _sheet_incidents(self, wb, fmt) -> None:
        ws = wb.add_worksheet("Incidents")
        headers = ["title", "severity", "root_cause", "narrative", "finding_ids"]
        widths = [40, 10, 45, 60, 40]
        for i, (h, w) in enumerate(zip(headers, widths)):
            ws.write(0, i, h, fmt["header"])
            ws.set_column(i, i, w)
        ws.freeze_panes(1, 0)
        incidents = self.store.incidents(self.run_id)
        if not incidents:
            ws.merge_range(
                1, 0, 1, len(headers) - 1,
                "No incidents correlated for this report (LLM enrichment skipped, or no "
                "findings shared a clear common root cause).", fmt["wrap"],
            )
            return
        for r, inc in enumerate(incidents, start=1):
            ws.write(r, 0, inc["title"], fmt["wrap"])
            ws.write(r, 1, inc["severity"], fmt.get(f"sev_{inc['severity']}"))
            ws.write(r, 2, inc.get("root_cause") or "", fmt["wrap"])
            ws.write(r, 3, inc.get("llm_narrative") or "", fmt["wrap"])
            ws.write(r, 4, ", ".join(inc.get("finding_ids", [])), fmt["wrap"])

    # ---------------------------------------------------------- 4. Well Scorecard
    def _sheet_well_scorecard(self, wb, fmt) -> None:
        ws = wb.add_worksheet("Well Scorecard")
        headers = ["well_id", "label", "findings", "actionable", "critical", "high", "affected_rows"]
        widths = [10, 20, 10, 11, 10, 8, 14]
        for i, (h, w) in enumerate(zip(headers, widths)):
            ws.write(0, i, h, fmt["header"])
            ws.set_column(i, i, w)
        ws.freeze_panes(1, 0)
        ws.autofilter(0, 0, 0, len(headers) - 1)
        rows = self.store.well_scorecard(self.run_id, limit=2000)
        if not rows:
            ws.merge_range(
                1, 0, 1, len(headers) - 1,
                "No well-linked findings yet -- more hand-written checks in "
                "docs/03-ANOMALY-TAXONOMY.md attach a well_id; this sheet populates as "
                "those are built.", fmt["wrap"],
            )
            return
        for r, w in enumerate(rows, start=1):
            ws.write_number(r, 0, w["well_id"])
            ws.write(r, 1, w.get("label") or "")
            ws.write_number(r, 2, w["findings"], fmt["num"])
            ws.write_number(r, 3, w["actionable"] or 0, fmt["num"])
            ws.write_number(r, 4, w["critical"] or 0, fmt["num"])
            ws.write_number(r, 5, w["high"] or 0, fmt["num"])
            ws.write_number(r, 6, w["affected_rows"] or 0, fmt["num"])

    # ------------------------------------------------------- 5. Normalisation log
    def _sheet_normalisation(self, wb, fmt) -> None:
        ws = wb.add_worksheet("Normalisation Log")
        headers = ["issue found", "kind", "target", "rows_affected", "rows_total", "pct",
                   "check_id"]
        widths = [30, 16, 40, 16, 14, 10, 12]
        for i, (h, w) in enumerate(zip(headers, widths)):
            ws.write(0, i, h, fmt["header"])
            ws.set_column(i, i, w)
        ws.freeze_panes(1, 0)
        r = 0
        for r, a in enumerate(self.store.normalisation_actions(self.run_id), start=1):
            ws.write(r, 0, action_label(a["kind"]))
            ws.write(r, 1, a["kind"])
            ws.write(r, 2, a["target"])
            ws.write_number(r, 3, a["rows_affected"], fmt["num"])
            ws.write_number(r, 4, a["rows_total"], fmt["num"])
            if a.get("pct") is not None:
                ws.write_number(r, 5, a["pct"] / 100.0, fmt["pct"])
            ws.write(r, 6, a.get("check_id") or "")
        ws.merge_range(r + 2, 0, r + 2, len(headers) - 1, NO_DB_CHANGE_NOTE, fmt["wrap"])

    # ------------------------------------------------------- 6. Check catalogue
    def _sheet_check_catalogue(self, wb, fmt) -> None:
        ws = wb.add_worksheet("Check Catalogue")
        headers = ["check_id", "family", "status", "rows_scanned", "violations",
                   "duration_ms", "grain", "baseline", "skip_reason", "error_text"]
        widths = [30, 8, 9, 14, 12, 12, 26, 12, 30, 30]
        for i, (h, w) in enumerate(zip(headers, widths)):
            ws.write(0, i, h, fmt["header"])
            ws.set_column(i, i, w)
        ws.freeze_panes(1, 0)
        ws.autofilter(0, 0, 0, len(headers) - 1)
        for r, c in enumerate(self.store.check_results(self.run_id), start=1):
            ws.write(r, 0, c["check_id"])
            ws.write(r, 1, c["family"])
            ws.write(r, 2, c["status"])
            ws.write_number(r, 3, c["rows_scanned"] or 0, fmt["num"])
            ws.write_number(r, 4, c["violations"] or 0, fmt["num"])
            ws.write_number(r, 5, c["duration_ms"] or 0, fmt["num"])
            ws.write(r, 6, c.get("grain") or "", fmt["wrap"])
            ws.write(r, 7, c.get("baseline") or "")
            ws.write(r, 8, c.get("skip_reason") or "", fmt["wrap"])
            ws.write(r, 9, c.get("error_text") or "", fmt["wrap"])

    # ------------------------------------------------ 7. Business Rules Reference
    def _sheet_business_rules(self, wb, fmt) -> None:
        """The exact wording of every business-rule section this run's findings cite --
        pulled live from docs/BUSINESS_RULES.md, not retyped -- so a reader never has to
        go find that file separately to see what "§4" actually says.
        """
        ws = wb.add_worksheet("Business Rules Reference")
        ws.set_column("A:A", 10)
        ws.set_column("B:B", 32)
        ws.set_column("C:C", 90)
        headers = ["section", "title", "rule text (verbatim from BUSINESS_RULES.md)"]
        for i, h in enumerate(headers):
            ws.write(0, i, h, fmt["header"])
        ws.freeze_panes(1, 0)
        refs = [f.get("business_rule_ref") for f in self.store.findings(self.run_id, limit=5000)]
        cited = referenced_sections(refs)
        if not cited:
            ws.merge_range(
                1, 0, 1, len(headers) - 1,
                "No findings this run cite a specific business-rule section.", fmt["wrap"],
            )
            return
        for r, sec in enumerate(cited, start=1):
            ws.write(r, 0, f"§{sec.number}")
            ws.write(r, 1, sec.title, fmt["wrap"])
            ws.write(r, 2, sec.text, fmt["wrap"])
            ws.set_row(r, min(300, 15 * (sec.text.count("\n") + 2)))


def build_excel_report(store: FindingsStore, run_id: str, path: Path) -> Path:
    return ExcelReport(store, run_id).build(path)
