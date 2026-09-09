"""Word narrative report -- python-docx, built from the same run rows as the Excel
workbook. This is the document a manager reads once, top to bottom; the Excel workbook is
the working list a team filters and assigns from. Same source of truth, same run_id,
different shape for a different reader.
"""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor

from app.db.store import FindingsStore
from app.logging import get_logger
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


class WordReport:
    def __init__(self, store: FindingsStore, run_id: str) -> None:
        self.store = store
        self.run_id = run_id
        self.run = store.get_run(run_id)
        if self.run is None:
            raise ValueError(f"no such run: {run_id}")
        self.doc = Document()

    def build(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._title_page()
        self._executive_summary()
        self._scope_and_method()
        self._findings_by_severity()
        self._incidents()
        self._business_rule_compliance()
        self._appendix()
        self.doc.save(str(path))
        log.info("word.built", run_id=self.run_id, path=str(path))
        return path

    # ------------------------------------------------------------------------ helpers
    def _h1(self, text: str) -> None:
        self.doc.add_heading(text, level=1)

    def _h2(self, text: str) -> None:
        self.doc.add_heading(text, level=2)

    def _p(self, text: str) -> None:
        self.doc.add_paragraph(text)

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
            f"Run {r['run_id']}  ·  generated {r.get('finished_at')}  ·  "
            f"{r.get('rows_scanned', 0):,} rows scanned  ·  "
            f"{r.get('checks_run', 0)} checks  ·  {r.get('findings_total', 0)} findings"
        ).font.size = Pt(9)
        d.add_page_break()

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
            note = self.doc.add_paragraph()
            note.add_run(
                "LLM enrichment was not run for this report (no API key configured, or "
                "disabled). The findings below are the deterministic engine's own output, "
                "unaffected by this — every count and evidence row is still exact."
            ).italic = True

        self._h2("At a glance")
        sev = self.store.severity_counts(self.run_id)
        cls = self.store.class_counts(self.run_id)
        table = self.doc.add_table(rows=1, cols=2)
        table.style = "Light Grid Accent 1"
        table.rows[0].cells[0].text = "Severity"
        table.rows[0].cells[1].text = "Count"
        for s in SEVERITY_ORDER:
            if s in sev:
                row = table.add_row()
                row.cells[0].text = s.upper()
                row.cells[1].text = str(sev[s])
        self._p("")
        self._p(
            "Finding class: " + ", ".join(f"{k} = {v}" for k, v in cls.items())
        )

    # --------------------------------------------------------------- 3. scope/method
    def _scope_and_method(self) -> None:
        self._h1("Scope & Method")
        r = self.run
        self._p(
            f"This report was generated live against {r.get('db_name')} at "
            f"{r.get('started_at')} (server date {r.get('as_of_date')}). Every figure "
            "below was computed by a query executed at that moment — nothing is cached "
            "or reused from a prior run."
        )
        norm = self.store.normalisation_actions(self.run_id)
        if norm:
            self._h2("Data normalised before analysis")
            for a in norm:
                self._p(
                    f"• {a['target']}: {a['rows_affected']:,} of {a['rows_total']:,} rows "
                    f"affected by {a['kind']} ({a.get('check_id') or 'n/a'})"
                )
        self._p(
            "Read-only source access: the account used to read this database is "
            "verified `db_datareader`-only and cannot write. Findings are stored "
            "separately and never written back to the source."
        )

    # -------------------------------------------------------- 4. findings by severity
    def _findings_by_severity(self) -> None:
        self._h1("Findings by Severity")
        findings = self.store.findings(self.run_id, limit=5000)
        by_sev: dict[str, list[dict]] = {}
        for f in findings:
            by_sev.setdefault(f["severity"], []).append(f)

        for sev in SEVERITY_ORDER:
            rows = by_sev.get(sev, [])
            if not rows:
                continue
            h = self.doc.add_heading(f"{sev.upper()}  ({len(rows)})", level=2)
            for run_obj in h.runs:
                run_obj.font.color.rgb = SEVERITY_RGB.get(sev, RGBColor(0, 0, 0))
            for f in rows:
                p = self.doc.add_paragraph()
                p.add_run(f"{f['check_id']} — {f['title']}").bold = True
                if f.get("business_rule_ref"):
                    self._p(f"Business rule: {describe(f['business_rule_ref'])}")
                if f.get("owner"):
                    self._p(f"Owner: {f['owner']}")
                text = f.get("llm_explanation") or f.get("why_it_matters") or ""
                if text:
                    self._p(text)
                if f.get("llm_root_cause"):
                    self._p(f"Likely cause: {f['llm_root_cause']}")
                if f.get("llm_remediation"):
                    self._p(f"Remediation: {f['llm_remediation']}")
                self._p(f"Grain: {f.get('grain') or 'n/a'}  |  Baseline: {f.get('baseline') or 'n/a'}")
                self.doc.add_paragraph("")

    # ------------------------------------------------------------------ 5. incidents
    def _incidents(self) -> None:
        incidents = self.store.incidents(self.run_id)
        self._h1("Incident Analysis")
        if not incidents:
            self._p(
                "No incidents were correlated for this report (LLM enrichment skipped, "
                "or no findings shared a clear common root cause)."
            )
            return
        for inc in incidents:
            self._h2(inc["title"])
            self._p(f"Root cause: {inc.get('root_cause') or 'n/a'}")
            if inc.get("llm_narrative"):
                self._p(inc["llm_narrative"])
            self._p("Related findings: " + ", ".join(inc.get("finding_ids", [])))

    # -------------------------------------------------------- 6. business-rule section
    def _business_rule_compliance(self) -> None:
        self._h1("Business-Rule Compliance")
        self._p(
            "Every rule quoted below is the exact wording from docs/BUSINESS_RULES.md -- "
            "the same document the AI layer is given verbatim as ground truth -- so this "
            "section can never describe a rule differently than the analysis above did."
        )
        findings = self.store.findings(self.run_id, limit=5000)
        refs = [f.get("business_rule_ref") for f in findings]
        cited = referenced_sections(refs)
        if not cited:
            self._p("No findings this run cite a specific business-rule section.")
            return
        for sec in cited:
            self._h2(f"§{sec.number}  {sec.title}")
            self._add_rule_text(sec.text)
            matching = [f for f in findings if cites(f.get("business_rule_ref"), sec.number)]
            if matching:
                bold = self.doc.add_paragraph()
                bold.add_run("Findings citing this rule:").bold = True
                for f in matching:
                    self.doc.add_paragraph(f"{f['check_id']}: {f['title']}", style="List Bullet")

    def _add_rule_text(self, text: str) -> None:
        """Prints a BUSINESS_RULES.md section body as-is (its own wording, not a
        paraphrase) -- one paragraph per non-blank line, since a single paragraph would
        collapse the source's line breaks (including its markdown tables) into one
        unreadable run of text.
        """
        for line in text.splitlines():
            line = line.strip()
            if line:
                self._p(line)

    # ------------------------------------------------------------------- 7. appendix
    def _appendix(self) -> None:
        self._h1("Appendix — Check Catalogue")
        results = self.store.check_results(self.run_id)
        table = self.doc.add_table(rows=1, cols=5)
        table.style = "Light Grid Accent 1"
        hdr = table.rows[0].cells
        for i, h in enumerate(["check_id", "family", "status", "rows_scanned", "violations"]):
            hdr[i].text = h
        for c in results:
            row = table.add_row().cells
            row[0].text = c["check_id"]
            row[1].text = c["family"]
            row[2].text = c["status"]
            row[3].text = str(c.get("rows_scanned") or 0)
            row[4].text = str(c.get("violations") or 0)


def build_word_report(store: FindingsStore, run_id: str, path: Path) -> Path:
    return WordReport(store, run_id).build(path)
