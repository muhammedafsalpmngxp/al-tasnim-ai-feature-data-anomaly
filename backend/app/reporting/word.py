"""Word narrative report -- python-docx, built from the same run rows as the Excel
workbook. This is the document a manager reads once, top to bottom; the Excel workbook is
the working list a team filters and assigns from. Same source of truth, same run_id,
different shape for a different reader.

DELIBERATELY SHORT. An earlier version printed eight paragraphs per finding for all 52
findings plus every cited business rule's full verbatim text, which came to 550 paragraphs
and 30+ pages -- a document nobody reads. The rule now:

  * the REGISTER TABLE lists every finding, so nothing is hidden;
  * only `NARRATIVE_SEVERITIES` get a written block, because a medium-severity title like
    "northing: 4 rows outside [1800000, 2800000]" already is the whole finding;
  * full business-rule text and the complete narration for every severity live in the
    Excel workbook, which this document points to rather than duplicating.

Nothing here changes what is detected or counted -- this module only formats rows that
`FindingsStore` already holds.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt, RGBColor

from app.db.store import FindingsStore
from app.logging import get_logger
from app.reporting.labels import action_label
from app.sentinel.business_rules_index import cites, describe, referenced_sections

log = get_logger(__name__)

SEVERITY_RGB = {
    "critical": RGBColor(0xC0, 0x00, 0x00),
    "high": RGBColor(0xE3, 0x6C, 0x09),
    "medium": RGBColor(0xBF, 0x90, 0x00),
    "low": RGBColor(0x54, 0x82, 0x35),
    "review": RGBColor(0x70, 0x30, 0xA0),
    "info": RGBColor(0x2E, 0x75, 0xB6),
}
SEVERITY_ORDER = ["critical", "high", "medium", "low", "review", "info"]
GREY = RGBColor(0x60, 0x60, 0x60)

# How much prose each severity earns. Severity drives DETAIL, never coverage -- every
# finding at every severity is in the register table and in the Excel workbook regardless.
#   "full"    explanation + likely cause + fix
#   "brief"   the fix only, on one line -- the actionable half, without the write-up
#   (absent)  register table only
NARRATIVE_DEPTH = {"critical": "full", "high": "brief"}
TABLE_STYLE = "Light Grid Accent 1"


class WordReport:
    def __init__(self, store: FindingsStore, run_id: str) -> None:
        self.store = store
        self.run_id = run_id
        self.run = store.get_run(run_id)
        if self.run is None:
            raise ValueError(f"no such run: {run_id}")
        self.doc = Document()
        self._tighten_styles()

    def build(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        findings = self.store.findings(self.run_id, limit=5000)
        self._title_page()
        self._executive_summary()
        self._scope_and_method(findings)
        self._findings_register(findings)
        self._priority_findings(findings)
        self._incidents()
        self._business_rule_compliance(findings)
        self._appendix()
        self.doc.save(str(path))
        log.info(
            "word.built", run_id=self.run_id, path=str(path),
            paragraphs=len(self.doc.paragraphs), tables=len(self.doc.tables),
        )
        return path

    # ------------------------------------------------------------------------ helpers
    def _tighten_styles(self) -> None:
        """Narrower margins and a 10pt body: the same content on noticeably fewer pages,
        without shrinking type to something a manager has to squint at."""
        for section in self.doc.sections:
            section.top_margin = Inches(0.7)
            section.bottom_margin = Inches(0.7)
            section.left_margin = Inches(0.8)
            section.right_margin = Inches(0.8)
        normal = self.doc.styles["Normal"]
        normal.font.size = Pt(10)
        normal.paragraph_format.space_after = Pt(4)
        normal.paragraph_format.space_before = Pt(0)

    def _h1(self, text: str, *, page_break: bool = False) -> None:
        if page_break:
            self.doc.add_page_break()
        self.doc.add_heading(text, level=1)

    def _h2(self, text: str) -> None:
        self.doc.add_heading(text, level=2)

    def _p(self, text: str, *, size: int | None = None, color: RGBColor | None = None,
           italic: bool = False) -> None:
        p = self.doc.add_paragraph()
        run = p.add_run(text)
        if size:
            run.font.size = Pt(size)
        if color is not None:
            run.font.color.rgb = color
        run.italic = italic

    @staticmethod
    def _when(iso: str | None) -> str:
        """'2026-09-09T08:00:43.426782+00:00' -> '9 September 2026 at 08:00'.

        A raw ISO timestamp is machine output and this document is read by people. Built
        from the parts rather than strftime("%-d ...") -- that directive does not exist on
        Windows, which is where this runs.
        """
        if not iso:
            return "—"
        try:
            dt = datetime.fromisoformat(iso)
        except (TypeError, ValueError):
            return iso
        return f"{dt.day} {dt:%B %Y} at {dt:%H:%M}"

    def _table(self, headers: list[str], widths: list[float] | None = None):  # noqa: ANN201
        t = self.doc.add_table(rows=1, cols=len(headers))
        t.style = TABLE_STYLE
        for i, h in enumerate(headers):
            cell = t.rows[0].cells[i]
            cell.text = ""
            run = cell.paragraphs[0].add_run(h)
            run.bold = True
            run.font.size = Pt(9)
        if widths:
            for row in t.rows:
                for i, w in enumerate(widths):
                    row.cells[i].width = Inches(w)
        return t

    def _cell(self, cell, text: str, *, size: int = 9, bold: bool = False,
              color: RGBColor | None = None) -> None:
        cell.text = ""
        run = cell.paragraphs[0].add_run(text)
        run.font.size = Pt(size)
        run.bold = bold
        if color is not None:
            run.font.color.rgb = color

    # ---------------------------------------------------------------------- 1. title
    def _title_page(self) -> None:
        d, r = self.doc, self.run
        title = d.add_heading("Data Quality & Anomaly Report", level=0)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sub = d.add_paragraph()
        sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = sub.add_run(f"{r.get('db_name')}  —  as of {r.get('as_of_date')}")
        run.italic = True
        run.font.size = Pt(12)
        meta = d.add_paragraph()
        meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
        meta.add_run(
            f"Generated {self._when(r.get('finished_at'))}  ·  "
            f"{r.get('rows_scanned', 0):,} rows examined  ·  "
            f"{r.get('checks_run', 0)} checks  ·  {r.get('findings_total', 0)} findings"
        ).font.size = Pt(9)
        ref = d.add_paragraph()
        ref.alignment = WD_ALIGN_PARAGRAPH.CENTER
        ref_run = ref.add_run(f"Reference: {r['run_id']}")
        ref_run.font.size = Pt(8)
        ref_run.font.color.rgb = GREY

    # ------------------------------------------------------------- 2. exec summary
    def _executive_summary(self) -> None:
        self._h1("Executive Summary")
        summary = self.store.get_run_summary(self.run_id)
        if summary:
            head = self.doc.add_paragraph()
            head.add_run(summary.get("headline", "")).bold = True
            for para in summary.get("paragraphs", []):
                self._p(para)
            if summary.get("top_priorities"):
                self._h2("Top priorities")
                for item in summary["top_priorities"]:
                    self.doc.add_paragraph(item, style="List Bullet")
        else:
            self._p(
                "LLM enrichment was not run for this report (no API key configured, or "
                "disabled). The findings below are the deterministic engine's own output, "
                "unaffected by this — every count and evidence row is still exact.",
                italic=True,
            )

        sev = self.store.severity_counts(self.run_id)
        cls = self.store.class_counts(self.run_id)
        t = self._table(["severity", "count", "finding class", "count"], [1.4, 0.8, 1.6, 0.8])
        pairs = [(s, sev[s]) for s in SEVERITY_ORDER if s in sev]
        classes = list(cls.items())
        for i in range(max(len(pairs), len(classes))):
            cells = t.add_row().cells
            if i < len(pairs):
                self._cell(cells[0], pairs[i][0].upper(), bold=True,
                           color=SEVERITY_RGB.get(pairs[i][0]))
                self._cell(cells[1], str(pairs[i][1]))
            if i < len(classes):
                self._cell(cells[2], classes[i][0])
                self._cell(cells[3], str(classes[i][1]))

        self._p(
            "How to read this document: the register on the next page lists every finding "
            "this run produced. Findings rated CRITICAL or HIGH are then written up "
            "individually. The accompanying Excel workbook carries the complete register "
            "with the full explanation, cause and remediation for every severity, the "
            "per-well scorecard, and the verbatim text of every business rule cited here.",
            size=9, color=GREY, italic=True,
        )

    # --------------------------------------------------------------- 3. scope/method
    def _scope_and_method(self, findings: list[dict]) -> None:
        self._h1("Scope & Method")
        r = self.run
        self._p(
            f"This report was produced on {self._when(r.get('started_at'))} by reading the "
            f"live {r.get('db_name')} database directly, and every deadline in it is judged "
            f"against that day's date ({r.get('as_of_date')}). Every number was measured at "
            "that moment — nothing is carried over from a previous report."
        )
        self._p(
            "The database is only ever read, never written to. The connection used has no "
            "permission to add, change or delete anything, so producing this report cannot "
            "have altered a single record. Findings are stored in a separate file of their "
            "own.",
            size=9, color=GREY,
        )
        self._coverage_note(findings)
        norm = self.store.normalisation_actions(self.run_id)
        if not norm:
            return
        self._h2("Data quality issues found in the raw data")
        # (coverage note is printed above this heading, by _coverage_note)
        self._p(
            "Before any rule was tested, the data was checked for problems that would "
            "distort every figure that follows — the same record entered twice, or a "
            "stand-in date such as 1900-01-01 sitting where a real date belongs. Each is "
            "counted below and reported as a finding in its own right.",
            size=9, color=GREY,
        )
        t = self._table(
            ["issue found", "where", "rows affected", "of total"], [1.9, 2.4, 1.1, 1.0]
        )
        for a in norm:
            cells = t.add_row().cells
            self._cell(cells[0], action_label(a["kind"]))
            self._cell(cells[1], a["target"])
            self._cell(cells[2], f"{a['rows_affected']:,}")
            self._cell(cells[3], f"{a['rows_total']:,}")
        self._p(
            "To keep the figures in this report honest, affected rows and values were set "
            "aside for the analysis only — a repeated record was counted once, a stand-in "
            "date was treated as blank. NOTHING IN THE SOURCE DATABASE WAS CHANGED, and "
            "nothing was deleted: the connection used to read it has no permission to "
            "write, so the underlying records remain exactly as your teams entered them. "
            "Every row set aside is accounted for in the table above.",
            size=9, color=GREY,
        )

    def _coverage_note(self, findings: list[dict]) -> None:
        """States how much of the database this report actually examined.

        A reader who sees "55 findings" will otherwise assume the whole database was
        covered. It wasn't: a check only runs on a table declared in
        column_semantics.yaml. PIP-011 measures that gap, so the numbers here come from
        the finding itself rather than being restated (and can never disagree with it).
        """
        pip = next((f for f in findings if f["check_id"] == "PIP-011"), None)
        empty = next((f for f in findings if f["check_id"] == "PIP-010"), None)
        if not pip and not empty:
            return
        self._h2("How much of the database this covers")
        if pip:
            ev = pip.get("evidence") or []
            if isinstance(ev, str):
                try:
                    ev = json.loads(ev)
                except (TypeError, ValueError):
                    ev = []
            e = ev[0] if ev else {}
            checked = e.get("tables_checked")
            in_scope = e.get("tables_in_scope")
            unchecked = e.get("tables_unchecked")
            rows = e.get("rows_unchecked")
            if checked is not None and in_scope:
                self._p(
                    f"This report examined {checked} of the {in_scope} tables in scope. "
                    f"The other {unchecked}, holding {rows:,} rows between them, have no "
                    "column definitions recorded yet, so no check has looked at them — "
                    "that does not mean they are clean, it means they are unexamined. "
                    "Bringing one into scope is a configuration change, not new code."
                )
        if empty:
            self._p(
                f"A further {empty.get('affected_count', 0)} tables exist but contain no "
                "rows at all. They are listed in the register below; each needs a decision "
                "on whether it is genuinely unused or something failed to fill it."
            )
    # ----------------------------------------------------------- 4. findings register
    def _findings_register(self, findings: list[dict]) -> None:
        self._h1("Findings Register", page_break=True)
        self._p(
            f"All {len(findings)} findings from this run, most severe first.",
            size=9, color=GREY,
        )
        t = self._table(
            ["severity", "class", "check", "affected", "finding"],
            [0.75, 0.75, 1.15, 0.75, 3.7],
        )
        for f in findings:
            cells = t.add_row().cells
            self._cell(cells[0], f["severity"].upper(), bold=True,
                       color=SEVERITY_RGB.get(f["severity"]))
            self._cell(cells[1], f["finding_class"], color=GREY)
            self._cell(cells[2], f["check_id"], size=8)
            self._cell(cells[3], f"{f.get('affected_count') or 0:,}")
            self._cell(cells[4], f["title"])

    # ---------------------------------------------------------- 5. priority findings
    def _priority_findings(self, findings: list[dict]) -> None:
        priority = [f for f in findings if f["severity"] in NARRATIVE_DEPTH]
        self._h1("Priority Findings", page_break=True)
        if not priority:
            self._p("No findings at these severities this run.", italic=True)
            return
        self._p(
            f"{len(priority)} of {len(findings)} findings. CRITICAL means a reported "
            "figure is already wrong or a stored fact is impossible, and gets a full "
            "write-up; HIGH means a business rule is broken on real records, and gets the "
            "recommended fix. Every finding at every severity is in the register above, "
            "with its full explanation in the Excel workbook.",
            size=9, color=GREY,
        )
        for sev, depth in NARRATIVE_DEPTH.items():
            rows = [f for f in priority if f["severity"] == sev]
            if not rows:
                continue
            h = self.doc.add_heading(f"{sev.upper()}  ({len(rows)})", level=2)
            for run_obj in h.runs:
                run_obj.font.color.rgb = SEVERITY_RGB.get(sev, RGBColor(0, 0, 0))
            for f in rows:
                if depth == "full":
                    self._finding_full(f)
                else:
                    self._finding_brief(f)

    def _finding_full(self, f: dict) -> None:
        """Two paragraphs instead of the eight an earlier version used: a heading line
        carrying check id, title and metadata, then one body paragraph running the
        explanation, cause and fix together. Same content, a quarter of the height.
        """
        head = self.doc.add_paragraph()
        head.paragraph_format.space_before = Pt(6)
        head.add_run(f"{f['check_id']}  ").bold = True
        head.add_run(f["title"]).bold = True
        meta_bits = [f"{f.get('affected_count') or 0:,} affected"]
        if f.get("business_rule_ref"):
            meta_bits.append(describe(f["business_rule_ref"]))
        if f.get("owner"):
            meta_bits.append(f"owner: {f['owner']}")
        meta = head.add_run(f"\n{'  ·  '.join(meta_bits)}")
        meta.font.size = Pt(8)
        meta.font.color.rgb = GREY

        body = self.doc.add_paragraph()
        text = f.get("llm_explanation") or f.get("why_it_matters") or ""
        if text:
            body.add_run(text)
        if f.get("llm_root_cause"):
            body.add_run("  Likely cause: ").bold = True
            body.add_run(f["llm_root_cause"])
        if f.get("llm_remediation"):
            body.add_run("  Fix: ").bold = True
            body.add_run(f["llm_remediation"])

    def _finding_brief(self, f: dict) -> None:
        """One paragraph: the title, and the fix if one was written. The explanation is
        deliberately omitted -- at HIGH the title already states what is wrong ("313
        wells completed LATE (past rig_off + 2 days)"); what a reader needs added is what
        to do about it.
        """
        p = self.doc.add_paragraph(style="List Bullet")
        p.paragraph_format.space_after = Pt(3)
        p.add_run(f["title"]).bold = True
        detail = f.get("llm_remediation") or ""
        rule = describe(f["business_rule_ref"]) if f.get("business_rule_ref") else ""
        tail_bits = [b for b in (f"{f['check_id']}", rule) if b]
        if detail:
            p.add_run(f"  {detail}")
        tail = p.add_run(f"  ({'  ·  '.join(tail_bits)})")
        tail.font.size = Pt(8)
        tail.font.color.rgb = GREY

    # ------------------------------------------------------------------ 6. incidents
    def _incidents(self) -> None:
        incidents = self.store.incidents(self.run_id)
        self._h1("Incident Analysis", page_break=True)
        if not incidents:
            self._p(
                "No incidents were correlated for this report (LLM enrichment skipped, "
                "or no findings shared a clear common root cause).", italic=True,
            )
            return
        self._p(
            "Findings grouped where they share one underlying cause — fixing the cause "
            "clears the group.", size=9, color=GREY,
        )
        for inc in incidents:
            head = self.doc.add_paragraph()
            head.paragraph_format.space_before = Pt(6)
            head.add_run(inc["title"]).bold = True
            ids = ", ".join(inc.get("finding_ids", []))
            tail = head.add_run(f"\n{ids}")
            tail.font.size = Pt(8)
            tail.font.color.rgb = GREY
            body = self.doc.add_paragraph()
            if inc.get("llm_narrative"):
                body.add_run(inc["llm_narrative"])
            if inc.get("root_cause"):
                body.add_run("  Root cause: ").bold = True
                body.add_run(inc["root_cause"])

    # -------------------------------------------------------- 7. business-rule section
    def _business_rule_compliance(self, findings: list[dict]) -> None:
        self._h1("Business-Rule Compliance")
        cited = referenced_sections([f.get("business_rule_ref") for f in findings])
        if not cited:
            self._p("No findings this run cite a specific business-rule section.", italic=True)
            return
        self._p(
            "Which sections of docs/BUSINESS_RULES.md this run's findings touch. The "
            "verbatim text of each section is in the Excel workbook's \"Business Rules "
            "Reference\" sheet — quoted there in full rather than reprinted here, so this "
            "document stays readable and the two can never disagree.",
            size=9, color=GREY,
        )
        t = self._table(["rule", "subject", "findings citing it"], [0.6, 2.2, 4.4])
        for sec in cited:
            matching = [f for f in findings if cites(f.get("business_rule_ref"), sec.number)]
            cells = t.add_row().cells
            self._cell(cells[0], f"§{sec.number}", bold=True)
            self._cell(cells[1], sec.title)
            self._cell(
                cells[2],
                ", ".join(sorted({f["check_id"] for f in matching})) or "—",
                size=8,
            )

    # ------------------------------------------------------------------- 8. appendix
    def _appendix(self) -> None:
        self._h1("Appendix — What Was Checked", page_break=True)
        results = self.store.check_results(self.run_id)
        by_status: dict[str, list[dict]] = {}
        for c in results:
            by_status.setdefault(c["status"], []).append(c)
        passed = len(by_status.get("pass", []))
        self._p(
            f"{len(results)} checks ran: {passed} passed cleanly, "
            f"{len(by_status.get('fail', []))} found violations, "
            f"{len(by_status.get('skipped', []))} skipped, "
            f"{len(by_status.get('error', []))} errored. Passing checks are counted here "
            "but not listed individually — the full catalogue, with each check's grain and "
            "baseline, is the Excel workbook's \"Check Catalogue\" sheet.",
            size=9, color=GREY,
        )

        # Anything that did NOT pass is listed: a skipped or errored check is a hole in
        # this run's coverage, and a reader has to be able to see which one.
        notable = (by_status.get("fail", []) + by_status.get("skipped", [])
                   + by_status.get("error", []))
        if not notable:
            return
        t = self._table(["check", "status", "violations", "note"], [1.6, 0.9, 1.0, 3.4])
        for c in notable:
            cells = t.add_row().cells
            self._cell(cells[0], c["check_id"], size=8)
            self._cell(cells[1], c["status"], bold=c["status"] in ("error", "skipped"))
            self._cell(cells[2], f"{c.get('violations') or 0:,}")
            self._cell(cells[3], c.get("skip_reason") or c.get("error_text") or "", size=8)


def build_word_report(store: FindingsStore, run_id: str, path: Path) -> Path:
    return WordReport(store, run_id).build(path)
