"""Build the Word document: the narrative report a person reads end to end.

A DOCUMENT, NOT A DATA DUMP. Excel carries every affected record for whoever will filter and fix
them; that is the right tool for eighty thousand rows. Word answers a different question - what
is wrong, how bad is it, and what should be done - and it answers it for a reader who will not
scroll past page three.

An earlier version put up to ANOMALY_REPORT_ROWS (2,000) evidence rows under every finding. With
41 findings that is 80,000 table rows, and because a probe's explanation is computed per record
the same sentence repeated for hundreds of pages: "rig_number = 'REP' does not parse as a
number", over and over. The document was 300 KB of noise in which the actual findings were
invisible. Volume is not evidence.

So each finding now shows:

  * the headline - how many records, out of how many, and how severe;
  * what is wrong and why it matters, in prose, from the rule itself;
  * the DISTINCT shapes the problem takes, with a count for each. Five hundred rows saying the
    same thing are one finding with a count of five hundred, not five hundred findings;
  * a handful of real sample records, so the reader can go and look at one;
  * a pointer to the workbook sheet holding the rest.

THE TRANSPARENCY SECTION IS NOT OPTIONAL. Checks that failed, and checks that examined nothing,
appear here with their reasons. A report listing only what it managed to check tells the reader
their data is healthier than it has been shown to be.

python-docx is imported inside build(), so a missing dependency degrades to a clear message
about the Word format alone rather than breaking the application at import time.
"""
from __future__ import annotations

import re
from collections import Counter

from app.config import settings
from app.observability import get_logger
from app.report.format import (
    SEVERITY_COLOURS,
    cell,
    emphasis_runs,
    report_filename,
    sheet_title,
)

log = get_logger()

# Real records shown per finding. Small on purpose: a sample exists so the reader can go and
# look at one record, which takes three, not two thousand.
SAMPLES_PER_FINDING = 4
# Distinct explanation shapes listed per finding before the rest are summarised as "other".
PATTERNS_PER_FINDING = 6
# Findings given a full section. Beyond this the tail is listed compactly - a reader who has
# read thirty sections has stopped reading, and the workbook holds everything regardless.
FULL_SECTIONS = 30

_DIGITS = re.compile(r"\d+")
_QUOTED = re.compile(r"'[^']*'")


def _shape_of(text: str) -> str:
    """The explanation with its variable parts masked, so repetitions collapse into one shape.

    "rig_number = 'REP' does not parse" and "rig_number = 'N/A' does not parse" are the same
    finding said twice. Masking the quoted value and the numbers turns a thousand near-identical
    sentences into one line with a count - which is what the reader actually needs to see.
    """
    masked = _QUOTED.sub("'<value>'", str(text or ""))
    return _DIGITS.sub("<n>", masked).strip()


def _severity_run(paragraph, severity: str):
    from docx.shared import RGBColor

    run = paragraph.add_run(severity.upper())
    run.bold = True
    run.font.color.rgb = RGBColor.from_string(SEVERITY_COLOURS.get(severity, "808080"))
    return run


def _small(doc, text: str, italic: bool = True):
    from docx.shared import Pt

    p = doc.add_paragraph()
    run = p.add_run(text)
    run.italic = italic
    run.font.size = Pt(8)
    return p


def _kv_table(doc, pairs):
    table = doc.add_table(rows=0, cols=2)
    table.style = "Light List Accent 1"
    for key, value in pairs:
        cells = table.add_row().cells
        cells[0].paragraphs[0].add_run(str(key)).bold = True
        cells[1].paragraphs[0].add_run(str(value))
    return table


def _prose(body: str) -> dict[str, str]:
    """Split a rule body into its ** ** sections, so the document can pick the useful ones.

    A rule body is written for the SQL Author and contains sections a reader does not need -
    "How to detect" is implementation detail. Printing the whole body verbatim, as the earlier
    version did, put SQL-authoring instructions into a management report.
    """
    out: dict[str, str] = {}
    current = ""
    for line in (body or "").splitlines():
        text = line.strip()
        if not text:
            continue
        heading = re.match(r"^\*\*(.+?)\*\*\s*$", text)
        if heading:
            current = heading.group(1).strip().lower()
            out[current] = ""
            continue
        if current:
            out[current] = (out[current] + " " + text.replace("**", "")).strip()
    return out


def _samples(result, limit: int) -> list[tuple[str, str]]:
    """(label, explanation) for the first few affected records - real values, never masked."""
    columns = [str(c).lower() for c in (result.detail_columns or [])]

    def index(name: str) -> int | None:
        return columns.index(name) if name in columns else None

    i_key, i_label = index("entity_key"), index("entity_label")
    i_text = index("explain_text")
    i_evidence = [n for n, c in enumerate(columns) if c.startswith("evidence_")]

    out: list[tuple[str, str]] = []
    for row in (result.detail_rows or [])[:limit]:
        label = ""
        if i_label is not None and row[i_label] not in (None, ""):
            label = cell(row[i_label])
        elif i_key is not None:
            label = cell(row[i_key])
        if i_key is not None and label != cell(row[i_key]):
            label = f"{label} ({cell(row[i_key])})"

        if i_text is not None and row[i_text] not in (None, ""):
            explanation = cell(row[i_text])
        else:
            explanation = ", ".join(
                f"{columns[n][len('evidence_'):]} = {cell(row[n])}"
                for n in i_evidence[:4]
                if row[n] not in (None, "")
            )
        out.append((label or "(unidentified record)", explanation))
    return out


def _patterns(result) -> list[tuple[str, int]]:
    """Distinct explanation shapes, most frequent first, each shown as a REAL sentence.

    The mask is used to GROUP, never to display. Printing the masked form put
    "(<n>-<n>-<n>) is <n> day(s) BEFORE ex_rig_on_date" into the report - a sentence with every
    fact removed, which is the opposite of evidence. The first real sentence that produced each
    shape is kept and shown instead, so the reader sees actual dates and actual counts, with the
    number of records that read the same way beside it.
    """
    columns = [str(c).lower() for c in (result.detail_columns or [])]
    if "explain_text" not in columns:
        return []
    i = columns.index("explain_text")

    counts: Counter = Counter()
    representative: dict[str, str] = {}
    for row in result.detail_rows or []:
        text = row[i]
        if text in (None, ""):
            continue
        shape = _shape_of(text)
        counts[shape] += 1
        representative.setdefault(shape, cell(text))
    return [(representative[shape], n) for shape, n in counts.most_common()]


def build(state, path: str | None = None) -> str:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt

    target = path or report_filename(state.get("run_id", "run"), "docx")
    doc = Document()
    section = doc.sections[0]
    # PORTRAIT, deliberately. The old landscape page existed only to fit wide evidence tables;
    # with the tables gone, portrait is what a report is read in.
    section.left_margin = Inches(0.9)
    section.right_margin = Inches(0.9)
    section.top_margin = Inches(0.8)
    section.bottom_margin = Inches(0.8)

    totals = state.get("totals") or {}
    by_severity = state.get("by_severity") or {}

    # ── Cover ──
    doc.add_heading("Data Quality Report", level=0)
    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = subtitle.add_run(
        f"{settings.db_name or 'database'}  ·  run {state.get('run_id', '')}"
    )
    run.italic = True

    _kv_table(doc, [
        ("Overall score", f"{state.get('score', 0)} / 100"),
        ("Checks run", f"{totals.get('probes_run', 0):,}"),
        ("Checks with findings", f"{totals.get('probes_with_findings', 0):,}"),
        ("Records examined", f"{totals.get('records_examined', 0):,}"),
        ("Records flagged", f"{totals.get('records_flagged', 0):,}"),
    ])

    if by_severity:
        doc.add_heading("Findings by severity", level=1)
        _kv_table(doc, [
            (name.capitalize(), f"{by_severity.get(name, 0):,} check(s)")
            for name in ("critical", "high", "medium", "low")
            if by_severity.get(name)
        ])

    # ── Executive summary (written by the model, from aggregates only) ──
    doc.add_heading("Executive summary", level=1)
    for line in str(state.get("summary") or "").splitlines():
        text = line.strip()
        if not text:
            continue
        bullet = text.startswith(("- ", "* ", "• "))
        p = doc.add_paragraph(style="List Bullet" if bullet else None)
        # **emphasis** becomes a bold run rather than literal asterisks on the page.
        for fragment, bold in emphasis_runs(text[2:].strip() if bullet else text):
            p.add_run(fragment).bold = bold

    _small(doc, "How the score is calculated: " + str(state.get("score_basis", "")))
    if state.get("catalog_stale") and state.get("catalog_note"):
        p = doc.add_paragraph()
        p.add_run("WARNING: " + str(state["catalog_note"])).bold = True

    # ── Findings ──
    ranked = state.get("ranked") or []
    results = {r.rule_id: r for r in (state.get("results") or [])}
    rules = state.get("rules") or {}

    doc.add_page_break()
    doc.add_heading("Findings", level=1)
    if not ranked:
        doc.add_paragraph("No check found anything to report.")

    for position, row in enumerate(ranked):
        if position == FULL_SECTIONS:
            doc.add_heading("Further findings", level=2)
            doc.add_paragraph(
                "Ranked below the findings above. Full detail for each is in the workbook."
            )
            for tail in ranked[FULL_SECTIONS:]:
                p = doc.add_paragraph(style="List Bullet")
                p.add_run(f"{tail['rule_id']} - {tail['title']}: ").bold = True
                p.add_run(
                    f"{tail['anomaly_count']:,} of {tail['scope_total']:,} "
                    f"({tail['anomaly_pct']:.2f}%), {tail['severity']}"
                )
            break

        result = results.get(row["rule_id"])
        rule = rules.get(row["rule_id"])
        doc.add_heading(f"{row['rule_id']} - {row['title']}", level=2)

        headline = doc.add_paragraph()
        _severity_run(headline, row["severity"])
        headline.add_run(
            f"  ·  {row['anomaly_count']:,} of {row['scope_total']:,} records "
            f"({row['anomaly_pct']:.2f}%)  ·  {row['category']}"
        )

        prose = _prose(getattr(rule, "body", "") or "")
        for heading in ("what is wrong", "why it matters"):
            if prose.get(heading):
                p = doc.add_paragraph()
                p.add_run(heading.capitalize() + ". ").bold = True
                p.add_run(prose[heading])

        if result is None:
            continue

        patterns = _patterns(result)
        if len(patterns) > 1:
            doc.add_paragraph("How the problem presents", style="Intense Quote")
            for shape, count in patterns[:PATTERNS_PER_FINDING]:
                p = doc.add_paragraph(style="List Bullet")
                p.add_run(f"{count:,}x ").bold = True
                p.add_run(shape)
            remaining = len(patterns) - PATTERNS_PER_FINDING
            if remaining > 0:
                _small(doc, f"... and {remaining:,} further variation(s).")
        elif patterns:
            p = doc.add_paragraph()
            p.add_run("Every affected record reports: ").bold = True
            p.add_run(patterns[0][0])

        samples = _samples(result, SAMPLES_PER_FINDING)
        if samples:
            doc.add_paragraph("Sample records", style="Intense Quote")
            for label, explanation in samples:
                p = doc.add_paragraph(style="List Bullet")
                p.add_run(f"{label}: ").bold = True
                p.add_run(explanation)

        shown = len(result.detail_rows or [])
        if result.detail_total > shown or row["anomaly_count"] > len(samples):
            _small(
                doc,
                f"All {row['anomaly_count']:,} affected records are in the workbook, "
                f"sheet \"{sheet_title(row['rule_id'])}\".",
            )
        for concern in result.concerns:
            _small(doc, "Note: " + concern)

    # ── Transparency ──
    gaps = state.get("empty_scope") or []
    not_running = state.get("not_running") or []
    failed = [r for r in (state.get("results") or []) if not r.ok]
    if gaps or not_running or failed:
        doc.add_page_break()
        doc.add_heading("Checks that did not report", level=1)
        doc.add_paragraph(
            "These checks produced no finding, but not because the data is clean. They are "
            "listed so the coverage of this report is not overstated."
        )
        if gaps:
            doc.add_heading("Examined no records", level=2)
            doc.add_paragraph(
                "The check ran but its scope matched nothing, so it can prove nothing about "
                "its subject. This is a coverage gap, not a clean result."
            )
            for item in gaps:
                rule_id, title = (item if isinstance(item, (list, tuple)) else (item, ""))[:2]
                doc.add_paragraph(f"{rule_id} - {title}", style="List Bullet")
        if failed:
            doc.add_heading("Failed while running", level=2)
            for result in failed:
                p = doc.add_paragraph(style="List Bullet")
                p.add_run(f"{result.rule_id}: ").bold = True
                p.add_run(str(result.error)[:300])
        if not_running:
            doc.add_heading("Not applicable to this database", level=2)
            for item in not_running:
                p = doc.add_paragraph(style="List Bullet")
                p.add_run(f"{item.get('rule_id', '')} - {item.get('title', '')}: ").bold = True
                p.add_run(str(item.get("reason", ""))[:400])

    doc.save(target)
    log.info(
        "report: docx written - %d finding section(s), %d sample(s) each -> %s",
        min(len(ranked), FULL_SECTIONS), SAMPLES_PER_FINDING, target,
    )
    return target
