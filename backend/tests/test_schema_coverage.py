"""Tests for `SchemaSnapshotter.coverage_findings` -- the two facts the check layer
structurally cannot report about itself.

PIP-010  a table exists but holds no rows at all
PIP-011  a table holds data but no declared check ever opens it

Both come free from the row counts `capture()` already fetched, so these tests need no
database: they hand the method snapshots directly.
"""
from __future__ import annotations

from app.domain.models import FindingClass, Severity
from app.sentinel.schema_snapshot import SchemaSnapshotter, TableSnapshot
from app.sentinel.scope import TableRef


def _snap(name: str, rows: int) -> TableSnapshot:
    return TableSnapshot(ref=TableRef.parse(name), row_count=rows)


class _CountingSource:
    """Answers the confirming COUNT(*) that coverage_findings issues for each candidate
    zero. `actual` maps table -> true row count; anything absent counts as 0."""

    def __init__(self, actual: dict[str, int] | None = None):
        self.actual = actual or {}
        self.counted: list[str] = []

    def one(self, sql: str, params=()):  # noqa: ANN001, ANN201
        name = sql.split("FROM ")[1].strip().replace("[", "").replace("]", "")
        self.counted.append(name)
        return {"n": self.actual.get(name, 0)}


def _snapshotter(
    known_counts: dict[str, int] | None = None, actual: dict[str, int] | None = None
) -> SchemaSnapshotter:
    s = SchemaSnapshotter.__new__(SchemaSnapshotter)
    s.src = _CountingSource(actual)
    # Which names have a MEASURED row count. Absent = a view, which must take no part.
    s._row_counts = known_counts if known_counts is not None else {}
    return s


def _all_measured(snaps) -> dict[str, int]:
    return {n: s.row_count for n, s in snaps.items()}


def _find(findings, check_id):
    return next((f for f in findings if f.check_id == check_id), None)


def test_empty_tables_are_reported():
    snaps = {
        "ref.division": _snap("ref.division", 0),
        "ref.location": _snap("ref.location", 0),
        "well.well_master": _snap("well.well_master", 814),
    }
    out = _snapshotter(_all_measured(snaps)).coverage_findings(snaps, {"well.well_master"})
    f = _find(out, "PIP-010")
    assert f is not None
    assert f.affected_count == 2
    assert f.finding_class is FindingClass.GAP
    assert f.severity is Severity.MEDIUM
    assert "ref.division" in f.why_it_matters and "ref.location" in f.why_it_matters
    assert f.evidence[0]["empty_tables"] == ["ref.division", "ref.location"]


def test_no_empty_table_finding_when_every_table_has_rows():
    snaps = {"well.well_master": _snap("well.well_master", 814)}
    assert _find(_snapshotter(_all_measured(snaps)).coverage_findings(snaps, {"well.well_master"}), "PIP-010") is None


def test_tables_with_data_but_no_declaration_are_reported():
    snaps = {
        "well.well_master": _snap("well.well_master", 814),      # declared
        "dbo.big_dump": _snap("dbo.big_dump", 1_750_000),        # not declared
        "ref.crew": _snap("ref.crew", 7_582),                    # not declared
    }
    out = _snapshotter(_all_measured(snaps)).coverage_findings(snaps, {"well.well_master"})
    f = _find(out, "PIP-011")
    assert f is not None
    assert f.affected_count == 2
    assert f.finding_class is FindingClass.DESIGN
    assert f.severity is Severity.INFO          # a scope disclosure, not a data defect
    assert f.evidence[0]["tables_checked"] == 1
    assert f.evidence[0]["tables_in_scope"] == 3
    assert f.evidence[0]["rows_unchecked"] == 1_757_582


def test_unchecked_tables_are_listed_largest_first():
    snaps = {
        "a.small": _snap("a.small", 10),
        "a.huge": _snap("a.huge", 9_000_000),
        "a.mid": _snap("a.mid", 5_000),
        # (schema-qualified: TableRef.parse requires schema.table)
    }
    f = _find(_snapshotter(_all_measured(snaps)).coverage_findings(snaps, set()), "PIP-011")
    assert [e["table"] for e in f.evidence[0]["largest"]] == ["a.huge", "a.mid", "a.small"]


def test_an_empty_table_is_not_also_counted_as_unchecked():
    """An empty table is PIP-010's business. Counting it again under PIP-011 would
    double-report one table and make the "checked N of M" arithmetic wrong."""
    snaps = {
        "ref.dead": _snap("ref.dead", 0),
        "well.well_master": _snap("well.well_master", 814),
    }
    out = _snapshotter(_all_measured(snaps)).coverage_findings(snaps, {"well.well_master"})
    pip011 = _find(out, "PIP-011")
    assert pip011 is None  # the only undeclared table here is the empty one


def test_declaration_matching_is_case_insensitive():
    """column_semantics.yaml casing does not always match sys.objects casing."""
    snaps = {"WBS.WBS_Master": _snap("WBS.WBS_Master", 81_846)}
    out = _snapshotter(_all_measured(snaps)).coverage_findings(snaps, {"wbs.wbs_master"})
    assert _find(out, "PIP-011") is None


def test_checked_count_excludes_both_empty_and_undeclared():
    snaps = {
        "s.d1": _snap("s.d1", 100), "s.d2": _snap("s.d2", 100),  # declared, have data
        "s.u1": _snap("s.u1", 50),                               # undeclared
        "s.e1": _snap("s.e1", 0),                                # empty
    }
    f = _find(_snapshotter(_all_measured(snaps)).coverage_findings(snaps, {"s.d1", "s.d2"}), "PIP-011")
    assert f.evidence[0]["tables_checked"] == 2
    assert f.evidence[0]["tables_in_scope"] == 4


# ------------------------------------------------------- regression: false "empty"
# Found live 2026-09-10, before this shipped: PIP-010 named 7 empty tables and 3 were
# populated views (742, 36 and 11 rows). `table_row_counts()` covers user TABLES only
# (sys.objects type 'U') while `capture()` snapshots views too ('V'), and the missing
# count was defaulted to 0 -- so every view looked empty. A medium-severity claim that
# someone's table is empty has to be measured, not inferred from an absent value.
def test_a_view_with_no_measured_row_count_is_never_called_empty():
    snaps = {
        "dbo.vw_chart": _snap("dbo.vw_chart", 0),        # a view: count defaulted to 0
        "well.well_master": _snap("well.well_master", 814),
    }
    # only the real table has a measured count
    out = _snapshotter({"well.well_master": 814}).coverage_findings(snaps, {"well.well_master"})
    assert _find(out, "PIP-010") is None


def test_a_view_is_not_counted_as_an_unchecked_table_either():
    snaps = {
        "dbo.vw_chart": _snap("dbo.vw_chart", 0),
        "well.well_master": _snap("well.well_master", 814),
        "ref.crew": _snap("ref.crew", 7_582),
    }
    out = _snapshotter({"well.well_master": 814, "ref.crew": 7_582}).coverage_findings(
        snaps, {"well.well_master"}
    )
    f = _find(out, "PIP-011")
    assert f.affected_count == 1                       # ref.crew only, not the view
    assert f.evidence[0]["tables_in_scope"] == 2       # the view is not in scope arithmetic
    assert f.evidence[0]["views_not_counted"] == 1


def test_a_candidate_zero_is_confirmed_by_counting_before_being_reported():
    """sys.partitions is documented as approximate. A candidate that turns out to hold
    rows must be dropped, not reported."""
    snaps = {
        "ref.truly_empty": _snap("ref.truly_empty", 0),
        "dbo.stale_stats": _snap("dbo.stale_stats", 0),   # stats say 0, really has 11
    }
    snapshotter = _snapshotter(
        _all_measured(snaps), actual={"dbo.stale_stats": 11}
    )
    f = _find(snapshotter.coverage_findings(snaps, set()), "PIP-010")
    assert f.affected_count == 1
    assert f.evidence[0]["empty_tables"] == ["ref.truly_empty"]
    # and it really did go and count both candidates
    assert sorted(snapshotter.src.counted) == ["dbo.stale_stats", "ref.truly_empty"]


def test_confirmation_only_counts_candidates_not_every_table():
    """The confirming COUNT(*) must not turn into a full-database scan."""
    snaps = {
        "a.empty": _snap("a.empty", 0),
        "a.big": _snap("a.big", 5_000_000),
        "a.mid": _snap("a.mid", 900),
    }
    snapshotter = _snapshotter(_all_measured(snaps))
    snapshotter.coverage_findings(snaps, set())
    assert snapshotter.src.counted == ["a.empty"]
