"""Regression tests for `Normaliser._report_value_cleanups` -- found 2026-09-09.

`_select_list` has always nulled placeholder dates, blank strings and sentinel strings so
no check mistakes them for real values, but that rewrite was SILENT: nothing measured or
reported it. A column that is 51% "1900-01-01" therefore looked simply 51% empty in the
report, with no finding saying the emptiness was disguised as a real date -- even though
`normalisation.yaml` already declared PLC-001 as *critical* for exactly this.

The two properties that matter and are asserted here:
  1. the rewrite is now measured and reported as a finding, at the configured severity;
  2. the measuring predicate is the SAME condition `_select_list` uses to null the value
     -- if those two ever drift, the reported count stops describing the actual rewrite.

No live database: a fake source returns canned aggregate rows and records the SQL it was
asked to run, which is what lets property 2 be asserted directly.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.domain.models import NormalisationKind, Severity
from app.sentinel.normalise.normaliser import Normaliser
from app.sentinel.normalise.spec import (
    ColumnTargetRule,
    NormalisationSpec,
    PlaceholderRule,
    SentinelRule,
)


class FakeSource:
    """Canned aggregates + a record of every statement, so the generated predicate can
    be asserted rather than guessed at."""

    def __init__(self, one_row: dict[str, Any] | None = None):
        self._one = one_row or {}
        self.statements: list[str] = []

    def rows(self, sql: str, params=()):  # noqa: ANN001, ANN201
        self.statements.append(sql)
        return [{"column_name": "actual_end_date", "data_type": "date", "is_nullable": 1}]

    def one(self, sql: str, params=()):  # noqa: ANN001, ANN201
        self.statements.append(sql)
        return dict(self._one)

    def table_row_counts(self):  # noqa: ANN201
        return {"dbo.jobs": 100}


class FakeScope:
    def source_expression(self, ref, rows_raw):  # noqa: ANN001, ANN201
        return f"[{ref.schema}].[{ref.table}]", False

    def table_allowed(self, schema, table):  # noqa: ANN001, ANN201
        return True

    def column_allowed(self, schema, table, column):  # noqa: ANN001, ANN201
        return True


def _spec_with_placeholders() -> NormalisationSpec:
    spec = NormalisationSpec()
    spec.placeholder_dates = PlaceholderRule(
        values=["1900-01-01", "1899-12-30"],
        check_id="PLC-001",
        severity=Severity.CRITICAL,
        targets=[ColumnTargetRule(table="dbo.jobs", columns=["actual_end_date"])],
    )
    return spec


def _normaliser(source: FakeSource, spec: NormalisationSpec) -> Normaliser:
    n = Normaliser(source, spec, FakeScope())  # type: ignore[arg-type]
    n._columns["dbo.jobs"] = [
        {"column_name": "actual_end_date", "data_type": "date", "is_nullable": 1}
    ]
    n._row_counts = {"dbo.jobs": 100}
    return n


@pytest.fixture()
def ref():  # noqa: ANN201
    from app.sentinel.scope import TableRef

    return TableRef.parse("dbo.jobs")


def test_a_measured_placeholder_rewrite_becomes_a_finding_at_the_configured_severity(ref):
    src = FakeSource({"rows_total": 100, "rows_any": 51, "actual_end_date": 51})
    n = _normaliser(src, _spec_with_placeholders())

    n._report_value_cleanups(ref, "[dbo].[jobs]")

    assert len(n.findings) == 1
    f = n.findings[0]
    assert f.check_id == "PLC-001"
    assert f.severity == Severity.CRITICAL   # from normalisation.yaml, not hardcoded here
    assert f.affected_count == 51
    assert "51" in f.title
    assert f.evidence[0]["by_column"] == {"actual_end_date": 51}
    assert f.evidence[0]["percent_of_rows"] == 51.0


def test_it_is_also_recorded_as_a_normalisation_action(ref):
    src = FakeSource({"rows_total": 100, "rows_any": 51, "actual_end_date": 51})
    n = _normaliser(src, _spec_with_placeholders())

    n._report_value_cleanups(ref, "[dbo].[jobs]")

    assert len(n.actions) == 1
    a = n.actions[0]
    assert a.kind is NormalisationKind.PLACEHOLDER_DATE
    assert a.rows_affected == 51
    assert a.rows_total == 100
    assert a.check_id == "PLC-001"


def test_the_measuring_predicate_matches_what_select_list_nulls(ref):
    """The whole point: `_select_list` nulls a placeholder with NULLIF(expr, '<value>')
    per configured value, so the measurement must test equality against those same
    values. A count produced by a different condition would not describe the rewrite.
    """
    src = FakeSource({"rows_total": 100, "rows_any": 0, "actual_end_date": 0})
    n = _normaliser(src, _spec_with_placeholders())

    n._report_value_cleanups(ref, "[dbo].[jobs]")
    measured = "\n".join(src.statements)
    select_list = n._select_list(ref)

    for value in ("1900-01-01", "1899-12-30"):
        assert value in measured, f"measurement ignores configured value {value}"
        assert value in select_list, f"_select_list stopped nulling {value}"


def test_nothing_is_reported_when_no_value_was_actually_rewritten(ref):
    """A clean column must not produce a zero-count finding -- the report says what is
    wrong, not what was checked and found fine (that is the check catalogue's job)."""
    src = FakeSource({"rows_total": 100, "rows_any": 0, "actual_end_date": 0})
    n = _normaliser(src, _spec_with_placeholders())

    n._report_value_cleanups(ref, "[dbo].[jobs]")

    assert n.findings == []
    assert n.actions == []


def test_a_failed_measurement_never_breaks_the_run(ref):
    """Normalisation must still produce its source even if this extra aggregate fails --
    losing the measurement is acceptable, losing the run is not."""

    class Exploding(FakeSource):
        def one(self, sql, params=()):  # noqa: ANN001, ANN201
            raise RuntimeError("arithmetic overflow")

    n = _normaliser(Exploding(), _spec_with_placeholders())
    n._report_value_cleanups(ref, "[dbo].[jobs]")  # must not raise
    assert n.findings == []


def test_sentinel_predicate_is_case_and_whitespace_insensitive_like_select_list(ref):
    """`_select_list` compares UPPER(LTRIM(RTRIM(...))) -- so ' no flaf ' is a sentinel.
    The measurement has to normalise identically or it undercounts."""
    spec = NormalisationSpec()
    spec.sentinels = SentinelRule(
        values=["NO FLAF", "#N/A"],
        check_id="SNT-001",
        severity=Severity.MEDIUM,
        targets=[ColumnTargetRule(table="dbo.jobs", columns=["actual_end_date"])],
    )
    src = FakeSource({"rows_total": 100, "rows_any": 25, "actual_end_date": 25})
    n = _normaliser(src, spec)

    n._report_value_cleanups(ref, "[dbo].[jobs]")
    measured = "\n".join(src.statements)

    assert "UPPER(LTRIM(RTRIM(" in measured
    assert "'NO FLAF'" in measured
    assert n.findings[0].check_id == "SNT-001"
    assert n.findings[0].severity == Severity.MEDIUM
