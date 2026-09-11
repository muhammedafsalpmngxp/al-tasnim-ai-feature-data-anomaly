"""Build the Word document: the narrative report a person reads end to end.

DIFFERENT JOB FROM THE SPREADSHEET, SO DIFFERENT CONTENT. Excel carries every affected record
for someone who will filter and fix them. Word explains what is wrong and why it matters, and
shows the worst few thousand records as evidence. A Word table of fifty thousand rows is both
enormous and unopenable, so it is capped - and the cap is stated on the page, because a table
that silently shows a fraction of the data is how a reader concludes a problem is small.

THE TRANSPARENCY SECTION IS NOT OPTIONAL. Checks that failed, and checks that examined nothing,
appear in this document with their reasons. A data-quality report that lists only what it
managed to check tells the reader their data is healthier than it has been shown to be.

python-docx is imported inside build(), so a missing dependency degrades to a clear message
about the Word format alone rather than breaking the application at import time.
"""
from __future__ import annotations

from app.config import settings
from app.observability import get_logger
from app.report.format import (
    SEVERITY_COLOURS,
    cap_note,
    cell,
    content_widths,
    header,
    report_filename,
)

log = get_logger()

# How many evidence rows per rule. Word builds the whole document tree in memory, so this is
# a real limit rather than a stylistic one.
def _row_cap() -> int:
    return max(1, settings.report_rows)


def _add_kv_table(doc, pairs):
    table = doc.add_table(rows=0, cols=2)
    table.style = "Light List Accent 1"
    for key, value in pairs:
        cells = table.add_row().cells
        cells[0].paragraphs[0].add_run(str(key)).bold = True
        cells[1].paragraphs[0].add_run(str(value))
    return table


def _severity_run(paragraph, severity: str):
    from docx.shared import RGBColor

    run = paragraph.add_run(severity.upper())
    run.bold = True
    colour = SEVERITY_COLOURS.get(severity, "808080")
    run.font.color.rgb = RGBColor.from_string(colour)
    return run


def _evidence_table(doc, result, section):
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Emu, Pt

    columns = result.detail_columns
    shown = result.detail_rows[: _row_cap()]
    if not columns or not shown:
        return

    table = doc.add_table(rows=1, cols=len(columns))
    table.style = "Light Grid Accent 1"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    # Explicit widths, or Word divides the page evenly and gives a date column as much room as
    # a remarks column. autofit must be off or Word recomputes and ignores them.
    table.autofit = False
    # 99.5% of the printable width, not 100%: widths are stored in twips and the rounding can
    # land a couple past the margin, which makes Word reflow the entire table.
    printable = int((section.page_width - section.left_margin - section.right_margin) * 0.995)
    shares = content_widths(columns, shown)
    total = sum(shares) or 1
    widths = [Emu(int(printable * s / total)) for s in shares]
    for i, width in enumerate(widths):
        table.columns[i].width = width

    size = Pt(9) if len(columns) <= 6 else Pt(8) if len(columns) <= 10 else Pt(7)
    for i, name in enumerate(columns):
        c = table.rows[0].cells[i]
        c.width = widths[i]              # python-docx needs the width on CELLS too
        run = c.paragraphs[0].add_run(header(name))
        run.bold = True
        run.font.size = size
    # Repeat the header on every page - an evidence table spans many pages, and one whose
    # columns are unlabelled after page 1 is unusable.
    tr_pr = table.rows[0]._tr.get_or_add_trPr()
    tbl_header = OxmlElement("w:tblHeader")
    tbl_header.set(qn("w:val"), "true")
    tr_pr.append(tbl_header)

    for row in shown:
        cells = table.add_row().cells
        for i, value in enumerate(row):
            if i < len(cells):
                cells[i].width = widths[i]
                run = cells[i].paragraphs[0].add_run(cell(value))
                run.font.size = size


def build(state, path: str | None = None) -> str:
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.shared import Inches, Pt

    target = path or report_filename(state.get("run_id", "run"), "docx")
    doc = Document()
    section = doc.sections[0]
    # Landscape: evidence tables are wide, routinely ten columns or more.
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = section.page_height, section.page_width
    section.left_margin = Inches(0.5)
    section.right_margin = Inches(0.5)
    section.top_margin = Inches(0.5)
    section.bottom_margin = Inches(0.6)

    totals = state.get("totals") or {}
    doc.add_heading("Data Quality Report", level=0)
    _add_kv_table(doc, [
        ("Run", state.get("run_id", "")),
        ("Overall score", f"{state.get('score', 0)} / 100"),
        ("Checks run", totals.get("probes_run", 0)),
        ("Checks with findings", totals.get("probes_with_findings", 0)),
        ("Checks clean", totals.get("probes_clean", 0)),
        ("Records examined", f"{totals.get('records_examined', 0):,}"),
        ("Records flagged", f"{totals.get('records_flagged', 0):,}"),
    ])

    doc.add_heading("Executive summary", level=1)
    for line in str(state.get("summary") or "").splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith(("- ", "* ", "• ")):
            doc.add_paragraph(text[2:].strip(), style="List Bullet")
        else:
            doc.add_paragraph(text)

    note = doc.add_paragraph()
    run = note.add_run("How the score is calculated: " + str(state.get("score_basis", "")))
    run.italic = True
    run.font.size = Pt(8)

    if state.get("catalog_stale") and state.get("catalog_note"):
        warning = doc.add_paragraph()
        run = warning.add_run("WARNING: " + str(state["catalog_note"]))
        run.bold = True

    # ── Findings ──
    ranked = state.get("ranked") or []
    results = {r.rule_id: r for r in (state.get("results") or [])}
    rules = state.get("rules") or {}

    doc.add_heading("Findings", level=1)
    if not ranked:
        doc.add_paragraph("No check found anything to report.")
    for row in ranked:
        result = results.get(row["rule_id"])
        rule = rules.get(row["rule_id"])
        doc.add_heading(f"{row['rule_id']} - {row['title']}", level=2)

        meta = doc.add_paragraph()
        meta.add_run("Severity: ")
        _severity_run(meta, row["severity"])
        meta.add_run(
            f"    Category: {row['category']}    "
            f"Affected: {row['anomaly_count']:,} of {row['scope_total']:,} examined "
            f"({row['anomaly_pct']:.2f}%)"
        )

        body = (getattr(rule, "body", "") or "").strip()
        if body:
            for line in body.splitlines():
                text = line.strip().replace("**", "")
                if text:
                    doc.add_paragraph(text)

        if result is None:
            continue
        for concern in result.concerns:
            p = doc.add_paragraph()
            r = p.add_run("Note: " + concern)
            r.italic = True
            r.font.size = Pt(8)

        message = cap_note(result.detail_total, len(result.detail_rows[: _row_cap()]),
                           "ANOMALY_REPORT_ROWS")
        if message:
            p = doc.add_paragraph()
            r = p.add_run(message)
            r.italic = True
            r.font.size = Pt(8)
        _evidence_table(doc, result, section)

    # ── Transparency ──
    gaps = state.get("empty_scope") or []
    not_running = state.get("not_running") or []
    failed = [r for r in (state.get("results") or []) if not r.ok]
    if gaps or not_running or failed:
        doc.add_heading("Checks that did not report", level=1)
        doc.add_paragraph(
            "The following checks produced no verdict about their subject matter. They are "
            "listed because absence of a finding here does NOT mean absence of a problem - "
            "these areas are currently unmonitored."
        )
        if gaps:
            doc.add_heading("Examined no records", level=2)
            for rule_id in gaps:
                title = getattr(rules.get(rule_id), "title", "")
                doc.add_paragraph(f"{rule_id} - {title}", style="List Bullet")
        if failed:
            doc.add_heading("Failed while running", level=2)
            for result in failed:
                title = getattr(rules.get(result.rule_id), "title", "")
                doc.add_paragraph(
                    f"{result.rule_id} - {title}: {result.error[:300]}", style="List Bullet"
                )
        if not_running:
            doc.add_heading("Not compiled, or not applicable to this database", level=2)
            for item in not_running:
                doc.add_paragraph(
                    f"{item['rule_id']} - {item['title']} ({item['status']}): "
                    f"{item['reason'][:300]}",
                    style="List Bullet",
                )

    doc.save(target)
    log.info("report: docx written - %d finding section(s) -> %s", len(ranked), target)
    return target
