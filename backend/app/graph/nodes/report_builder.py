"""Report Builder node (deterministic) - writes the .xlsx and .docx artefacts.

EACH FORMAT FAILS ALONE. The two builders import their heavy dependency separately and are
called independently, so a missing python-docx costs the Word document and nothing else. A run
that produced a complete set of findings must not throw them away because one output format is
unavailable - the findings are the work, the file is the packaging.

The spool files are removed once both formats are written. They are an intermediate; the
reports are the artefacts worth keeping.
"""
from __future__ import annotations

from app.graph.run_state import RunState
from app.observability import get_logger
from app.report import spool as spool_store

log = get_logger()

_BUILDERS = {
    "xlsx": ("app.report.xlsx_report", "the Excel workbook", "openpyxl"),
    "docx": ("app.report.docx_report", "the Word document", "python-docx"),
}


def report_builder_node(state: RunState) -> dict:
    wanted = [f for f in (state.get("formats") or []) if f in _BUILDERS] or list(_BUILDERS)
    paths: dict[str, str] = {}
    errors: list[str] = []

    for fmt in wanted:
        module_name, label, package = _BUILDERS[fmt]
        try:
            module = __import__(module_name, fromlist=["build"])
            paths[fmt] = module.build(state)
        except ImportError as exc:
            message = f"{label} was not written: {package} is not installed ({exc})"
            log.warning("report: %s", message)
            errors.append(message)
        except Exception as exc:  # noqa: BLE001 - one format must not cost the other
            message = f"{label} could not be written: {exc}"
            log.warning("report: %s", message)
            errors.append(message)

    if paths:
        spool_store.cleanup(state.get("run_id", ""))
    else:
        # Every format failed, so the spool is the only surviving copy of the findings. Keeping
        # it means the reports can be rebuilt without re-running every probe against the
        # production database.
        log.warning(
            "report: no format could be written - the raw findings were kept in the run's "
            "spool directory rather than deleted"
        )

    return {"report_paths": paths, "report_error": "; ".join(errors)}
