"""Findings store — SQLite.

BIuser is `db_datareader` only (measured: CREATE SCHEMA 0, CREATE TABLE 0, INSERT 0), so a
`dq` schema inside AlTasnimBI is impossible and the feature must not require new grants on
a production database to work.

SQLite fits: findings are thousands of rows not millions, one writer (the run), one reader
(the API), fully SQL-queryable, zero infrastructure, and the whole result set is one
portable file. Everything goes through this class, so moving to a real database later is a
single-file change.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import structlog

from app.config import Settings, get_settings
from app.domain.models import (
    CheckResult,
    Finding,
    NormalisationAction,
    Run,
    RunStatus,
)

log = structlog.get_logger(__name__)

SCHEMA_VERSION = 3  # v3 adds suggestion + agent_trace (Tier 2 suggestion agent)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL,
    applied_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS run (
    run_id           TEXT PRIMARY KEY,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL,
    triggered_by     TEXT,
    db_name          TEXT,
    server_now       TEXT,
    as_of_date       TEXT,
    snapshot_pinned  TEXT,
    scope            TEXT,
    rows_scanned     INTEGER NOT NULL DEFAULT 0,
    checks_run       INTEGER NOT NULL DEFAULT 0,
    checks_passed    INTEGER NOT NULL DEFAULT 0,
    checks_skipped   INTEGER NOT NULL DEFAULT 0,
    findings_total   INTEGER NOT NULL DEFAULT 0,
    llm_model        TEXT,
    llm_tokens       INTEGER NOT NULL DEFAULT 0,
    llm_summary      TEXT,
    xlsx_path        TEXT,
    docx_path        TEXT,
    error_text       TEXT
);

CREATE TABLE IF NOT EXISTS finding (
    finding_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id               TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    check_id             TEXT NOT NULL,
    family               TEXT NOT NULL,
    severity             TEXT NOT NULL,
    finding_class        TEXT NOT NULL,
    title                TEXT NOT NULL,
    entity_type          TEXT NOT NULL,
    entity_id            TEXT,
    entity_label         TEXT,
    well_id              INTEGER,
    affected_count       INTEGER NOT NULL DEFAULT 0,
    grain                TEXT NOT NULL DEFAULT '',
    baseline             TEXT NOT NULL DEFAULT '',
    business_rule_ref    TEXT,
    owner                TEXT,
    why_it_matters       TEXT,
    evidence             TEXT,
    sql_text             TEXT,
    sampled              INTEGER NOT NULL DEFAULT 0,
    verified_by          TEXT,
    counter_query_count  INTEGER,
    llm_explanation      TEXT,
    llm_root_cause       TEXT,
    llm_remediation      TEXT,
    -- lifecycle across runs: what is new this week vs recurring vs resolved
    first_seen_run_id    TEXT,
    status               TEXT NOT NULL DEFAULT 'new',
    resolved_at          TEXT
);
CREATE INDEX IF NOT EXISTS ix_finding_run       ON finding(run_id);
CREATE INDEX IF NOT EXISTS ix_finding_well      ON finding(run_id, well_id);
CREATE INDEX IF NOT EXISTS ix_finding_check     ON finding(run_id, check_id);
CREATE INDEX IF NOT EXISTS ix_finding_severity  ON finding(run_id, severity);
CREATE INDEX IF NOT EXISTS ix_finding_class     ON finding(run_id, finding_class);

CREATE TABLE IF NOT EXISTS incident (
    incident_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    title          TEXT NOT NULL,
    root_cause     TEXT,
    severity       TEXT NOT NULL,
    finding_ids    TEXT,
    llm_narrative  TEXT
);
CREATE INDEX IF NOT EXISTS ix_incident_run ON incident(run_id);

CREATE TABLE IF NOT EXISTS check_result (
    run_id       TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    check_id     TEXT NOT NULL,
    family       TEXT NOT NULL,
    status       TEXT NOT NULL,
    rows_scanned INTEGER NOT NULL DEFAULT 0,
    violations   INTEGER NOT NULL DEFAULT 0,
    duration_ms  INTEGER NOT NULL DEFAULT 0,
    grain        TEXT NOT NULL DEFAULT '',
    baseline     TEXT NOT NULL DEFAULT '',
    skip_reason  TEXT,
    error_text   TEXT,
    PRIMARY KEY (run_id, check_id)
);

CREATE TABLE IF NOT EXISTS normalisation_action (
    action_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,
    target         TEXT NOT NULL,
    rows_affected  INTEGER NOT NULL,
    rows_total     INTEGER NOT NULL,
    pct            REAL,
    check_id       TEXT,
    detail         TEXT
);
CREATE INDEX IF NOT EXISTS ix_norm_run ON normalisation_action(run_id);

-- the drift baseline: what makes run n+1 better than run n
CREATE TABLE IF NOT EXISTS metric (
    run_id        TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    table_name    TEXT NOT NULL,
    column_name   TEXT NOT NULL DEFAULT '',
    metric_name   TEXT NOT NULL,
    metric_value  REAL,
    PRIMARY KEY (run_id, table_name, column_name, metric_name)
);
CREATE INDEX IF NOT EXISTS ix_metric_lookup ON metric(table_name, column_name, metric_name);

-- Tier 2 (suggestion agent, 2026-09-08): candidate NEW checks the agent proposes after
-- bounded, read-only exploration. NEVER read by the report generators -- a suggestion
-- only ever affects a report after a human approves it AND a person moves the emitted
-- boilerplate into the real check modules and reviews it as code. This table is the
-- human-review queue, not a findings source.
CREATE TABLE IF NOT EXISTS suggestion (
    suggestion_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    title           TEXT NOT NULL,
    family          TEXT NOT NULL,
    severity_guess  TEXT NOT NULL,
    hypothesis      TEXT NOT NULL,
    table_ref       TEXT,
    sql_text        TEXT NOT NULL,
    test_row_count  INTEGER,
    test_sample     TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    reviewed_at     TEXT,
    review_note     TEXT,
    boilerplate_path TEXT
);
CREATE INDEX IF NOT EXISTS ix_suggestion_status ON suggestion(status);

-- The full tool-call trace for one suggestion session -- every SQL statement the agent
-- ran and what came back, so a human reviewing a suggestion can see exactly how it was
-- derived, not just the final hypothesis (docs/04 `agent_trace`, built here for Tier 2).
CREATE TABLE IF NOT EXISTS agent_trace (
    trace_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    step_no      INTEGER NOT NULL,
    tool_name    TEXT NOT NULL,
    tool_input   TEXT,
    tool_output  TEXT,
    ok           INTEGER NOT NULL,
    elapsed_ms   INTEGER
);
CREATE INDEX IF NOT EXISTS ix_agent_trace_session ON agent_trace(session_id);
"""


def _iso(v: datetime | date | None) -> str | None:
    return None if v is None else v.isoformat()


def _js(v: Any) -> str | None:
    if v is None:
        return None
    return json.dumps(v, default=str)


class FindingsStore:
    """Write-side and read-side for the findings database."""

    def __init__(self, settings: Settings | None = None, path: Path | None = None) -> None:
        self._s = settings or get_settings()
        self.path = Path(path) if path else self._s.store_path
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._cn.row_factory = sqlite3.Row
        self._cn.execute("PRAGMA foreign_keys = ON")
        self._cn.execute("PRAGMA journal_mode = WAL")
        self._cn.execute("PRAGMA synchronous = NORMAL")
        self.migrate()

    # ---------------------------------------------------------------- lifecycle
    def _ensure_column(self, table: str, column: str, ddl_type: str) -> None:
        """Add a column to an existing SQLite file if it predates this version.

        `CREATE TABLE IF NOT EXISTS` in `_SCHEMA` only creates a table the first time --
        it does nothing for a table that already exists with an older column set, which
        is exactly the state of a `sentinel.db` written by an earlier version of this
        code. This is what lets that file be upgraded in place instead of requiring a
        manual delete.
        """
        cols = {r["name"] for r in self._cn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            self._cn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
            log.info("store.column_added", table=table, column=column)

    def migrate(self) -> int:
        self._cn.executescript(_SCHEMA)
        cur = self._cn.execute("SELECT MAX(version) AS v FROM schema_version")
        current = cur.fetchone()["v"]
        if current is None:
            self._cn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, datetime.now().isoformat()),
            )
            log.info("store.migrated", version=SCHEMA_VERSION, path=str(self.path))
        elif current < SCHEMA_VERSION:
            if current < 2:
                self._ensure_column("run", "llm_summary", "TEXT")
            self._cn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, datetime.now().isoformat()),
            )
            log.info("store.upgraded", from_=current, to=SCHEMA_VERSION)
        self._cn.commit()
        return SCHEMA_VERSION

    def close(self) -> None:
        self._cn.commit()
        self._cn.close()

    def __enter__(self) -> FindingsStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --------------------------------------------------------------------- runs
    @staticmethod
    def new_run_id() -> str:
        return f"run_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"

    def create_run(self, run: Run) -> str:
        self._cn.execute(
            """
            INSERT INTO run (run_id, started_at, status, triggered_by, db_name,
                             server_now, as_of_date, snapshot_pinned, scope, llm_model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                _iso(run.started_at),
                run.status.value,
                run.triggered_by,
                run.db_name,
                _iso(run.server_now),
                _iso(run.as_of_date),
                _js(run.snapshot_pinned),
                _js(run.scope),
                run.llm_model,
            ),
        )
        self._cn.commit()
        log.info("store.run_created", run_id=run.run_id)
        return run.run_id

    def finish_run(
        self,
        run_id: str,
        status: RunStatus,
        *,
        rows_scanned: int = 0,
        checks_run: int = 0,
        checks_passed: int = 0,
        checks_skipped: int = 0,
        error_text: str | None = None,
        snapshot_pinned: dict[str, Any] | None = None,
    ) -> None:
        total = self._cn.execute(
            "SELECT COUNT(*) AS n FROM finding WHERE run_id = ?", (run_id,)
        ).fetchone()["n"]
        sets = [
            "finished_at = ?", "status = ?", "rows_scanned = ?", "checks_run = ?",
            "checks_passed = ?", "checks_skipped = ?", "findings_total = ?",
            "error_text = ?",
        ]
        args: list[Any] = [
            datetime.now().isoformat(), status.value, rows_scanned, checks_run,
            checks_passed, checks_skipped, total, error_text,
        ]
        if snapshot_pinned is not None:
            sets.append("snapshot_pinned = ?")
            args.append(_js(snapshot_pinned))
        args.append(run_id)
        self._cn.execute(f"UPDATE run SET {', '.join(sets)} WHERE run_id = ?", args)
        self._cn.commit()
        log.info("store.run_finished", run_id=run_id, status=status.value, findings=total)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._cn.execute("SELECT * FROM run WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def latest_run(self, *, completed_only: bool = True) -> dict[str, Any] | None:
        sql = "SELECT * FROM run"
        if completed_only:
            sql += " WHERE status = 'completed'"
        sql += " ORDER BY started_at DESC LIMIT 1"
        row = self._cn.execute(sql).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        rs = self._cn.execute(
            "SELECT * FROM run ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rs]

    # ----------------------------------------------------------------- findings
    def add_findings(self, run_id: str, findings: Iterable[Finding]) -> int:
        batch = list(findings)
        if not batch:
            return 0
        self._cn.executemany(
            """
            INSERT INTO finding (
                run_id, check_id, family, severity, finding_class, title,
                entity_type, entity_id, entity_label, well_id, affected_count,
                grain, baseline, business_rule_ref, owner, why_it_matters,
                evidence, sql_text, sampled, verified_by, counter_query_count,
                llm_explanation, llm_root_cause, llm_remediation
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    run_id, f.check_id, f.family, f.severity.value, f.finding_class.value,
                    f.title, f.entity_type, f.entity_id, f.entity_label, f.well_id,
                    f.affected_count, f.grain, f.baseline, f.business_rule_ref, f.owner,
                    f.why_it_matters, f.evidence_json(), f.sql_text, int(f.sampled),
                    _js(f.verified_by), f.counter_query_count,
                    f.llm_explanation, f.llm_root_cause, f.llm_remediation,
                )
                for f in batch
            ],
        )
        self._cn.commit()
        return len(batch)

    def mark_finding_lifecycle(self, run_id: str) -> dict[str, int]:
        """Set each finding's status to new/recurring by comparing with the previous run.

        This is what makes the report say "12 new since last week" instead of showing the
        same 1,278 rows again.
        """
        prev = self._cn.execute(
            "SELECT run_id FROM run WHERE status='completed' AND run_id <> ?"
            " ORDER BY started_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if prev is None:
            self._cn.execute(
                "UPDATE finding SET status='new', first_seen_run_id=? WHERE run_id=?",
                (run_id, run_id),
            )
            self._cn.commit()
            n = self._cn.execute(
                "SELECT COUNT(*) AS n FROM finding WHERE run_id=?", (run_id,)
            ).fetchone()["n"]
            return {"new": n, "recurring": 0, "resolved": 0}

        prev_id = prev["run_id"]
        # A finding is "the same" if its check and entity match.
        self._cn.execute(
            """
            UPDATE finding SET status='recurring',
                   first_seen_run_id = COALESCE((
                       SELECT p.first_seen_run_id FROM finding p
                       WHERE p.run_id = ?
                         AND p.check_id = finding.check_id
                         AND IFNULL(p.entity_id,'') = IFNULL(finding.entity_id,'')
                       LIMIT 1), ?)
            WHERE run_id = ? AND EXISTS (
                SELECT 1 FROM finding p
                WHERE p.run_id = ?
                  AND p.check_id = finding.check_id
                  AND IFNULL(p.entity_id,'') = IFNULL(finding.entity_id,'')
            )
            """,
            (prev_id, prev_id, run_id, prev_id),
        )
        self._cn.execute(
            "UPDATE finding SET status='new', first_seen_run_id=?"
            " WHERE run_id=? AND status <> 'recurring'",
            (run_id, run_id),
        )
        self._cn.commit()
        counts = {
            r["status"]: r["n"]
            for r in self._cn.execute(
                "SELECT status, COUNT(*) AS n FROM finding WHERE run_id=? GROUP BY status",
                (run_id,),
            ).fetchall()
        }
        resolved = self._cn.execute(
            """
            SELECT COUNT(*) AS n FROM finding p
            WHERE p.run_id = ? AND NOT EXISTS (
                SELECT 1 FROM finding c
                WHERE c.run_id = ? AND c.check_id = p.check_id
                  AND IFNULL(c.entity_id,'') = IFNULL(p.entity_id,'')
            )
            """,
            (prev_id, run_id),
        ).fetchone()["n"]
        return {
            "new": counts.get("new", 0),
            "recurring": counts.get("recurring", 0),
            "resolved": resolved,
        }

    def findings(
        self,
        run_id: str,
        *,
        family: str | None = None,
        severity: str | None = None,
        finding_class: str | None = None,
        well_id: int | None = None,
        actionable_only: bool = False,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where = ["run_id = ?"]
        args: list[Any] = [run_id]
        if family:
            where.append("family = ?"); args.append(family)
        if severity:
            where.append("severity = ?"); args.append(severity)
        if finding_class:
            where.append("finding_class = ?"); args.append(finding_class)
        if well_id is not None:
            where.append("well_id = ?"); args.append(well_id)
        if actionable_only:
            where.append("finding_class IN ('violation','defect','gap')")
        args += [limit, offset]
        rs = self._cn.execute(
            f"""
            SELECT * FROM finding WHERE {' AND '.join(where)}
            ORDER BY CASE severity
                       WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2
                       WHEN 'low' THEN 3 WHEN 'review' THEN 4 ELSE 5 END,
                     affected_count DESC
            LIMIT ? OFFSET ?
            """,
            args,
        ).fetchall()
        return [dict(r) for r in rs]

    def severity_counts(self, run_id: str) -> dict[str, int]:
        return {
            r["severity"]: r["n"]
            for r in self._cn.execute(
                "SELECT severity, COUNT(*) AS n FROM finding WHERE run_id=? GROUP BY severity",
                (run_id,),
            ).fetchall()
        }

    def class_counts(self, run_id: str) -> dict[str, int]:
        return {
            r["finding_class"]: r["n"]
            for r in self._cn.execute(
                "SELECT finding_class, COUNT(*) AS n FROM finding WHERE run_id=?"
                " GROUP BY finding_class",
                (run_id,),
            ).fetchall()
        }

    def well_scorecard(self, run_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        rs = self._cn.execute(
            """
            SELECT well_id,
                   COUNT(*) AS findings,
                   SUM(affected_count) AS affected_rows,
                   SUM(CASE WHEN severity='critical' THEN 1 ELSE 0 END) AS critical,
                   SUM(CASE WHEN severity='high' THEN 1 ELSE 0 END) AS high,
                   SUM(CASE WHEN finding_class IN ('violation','defect','gap')
                            THEN 1 ELSE 0 END) AS actionable,
                   MAX(entity_label) AS label
            FROM finding
            WHERE run_id = ? AND well_id IS NOT NULL
            GROUP BY well_id
            ORDER BY actionable DESC, findings DESC
            LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()
        return [dict(r) for r in rs]

    def update_finding_narration(
        self, finding_id: int, *, explanation: str, root_cause: str, remediation: str
    ) -> None:
        """Write validated LLM narration back onto a finding row.

        Only called after `validate_narration()` passes -- a narration that failed
        citation validation is never written, so a finding either has real,
        evidence-grounded prose or has none (the deterministic title/why_it_matters still
        stand alone in the report either way).
        """
        self._cn.execute(
            """UPDATE finding SET llm_explanation=?, llm_root_cause=?, llm_remediation=?
               WHERE finding_id=?""",
            (explanation, root_cause, remediation, finding_id),
        )
        self._cn.commit()

    # -------------------------------------------------------------------- incidents
    def add_incident(
        self, run_id: str, *, title: str, root_cause: str, severity: str,
        finding_ids: list[str], narrative: str,
    ) -> int:
        cur = self._cn.execute(
            """INSERT INTO incident (run_id, title, root_cause, severity, finding_ids, llm_narrative)
               VALUES (?,?,?,?,?,?)""",
            (run_id, title, root_cause, severity, _js(finding_ids), narrative),
        )
        self._cn.commit()
        return int(cur.lastrowid)

    def incidents(self, run_id: str) -> list[dict[str, Any]]:
        rs = self._cn.execute(
            "SELECT * FROM incident WHERE run_id=? ORDER BY "
            "CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 "
            "WHEN 'low' THEN 3 WHEN 'review' THEN 4 ELSE 5 END",
            (run_id,),
        ).fetchall()
        out = []
        for r in rs:
            d = dict(r)
            try:
                d["finding_ids"] = json.loads(d.get("finding_ids") or "[]")
            except (TypeError, ValueError):
                d["finding_ids"] = []
            out.append(d)
        return out

    # -------------------------------------------------------------- run-level LLM usage
    def set_run_llm_usage(
        self, run_id: str, *, calls_made: int = 0, calls_failed: int = 0,
        prompt_tokens: int = 0, completion_tokens: int = 0, total_tokens: int = 0,
    ) -> None:
        self._cn.execute(
            "UPDATE run SET llm_tokens=? WHERE run_id=?", (total_tokens, run_id)
        )
        self._cn.commit()

    def set_run_report_paths(
        self, run_id: str, *, xlsx_path: str | None = None, docx_path: str | None = None
    ) -> None:
        sets, args = [], []
        if xlsx_path is not None:
            sets.append("xlsx_path = ?"); args.append(xlsx_path)
        if docx_path is not None:
            sets.append("docx_path = ?"); args.append(docx_path)
        if not sets:
            return
        args.append(run_id)
        self._cn.execute(f"UPDATE run SET {', '.join(sets)} WHERE run_id = ?", args)
        self._cn.commit()

    def set_run_summary(self, run_id: str, summary: dict[str, Any]) -> None:
        """Store the executive-summary JSON on the run row for the report generators."""
        self._cn.execute(
            "UPDATE run SET llm_summary=? WHERE run_id=?", (_js(summary), run_id)
        )
        self._cn.commit()

    def get_run_summary(self, run_id: str) -> dict[str, Any] | None:
        row = self._cn.execute(
            "SELECT llm_summary FROM run WHERE run_id=?", (run_id,)
        ).fetchone()
        if not row or not row["llm_summary"]:
            return None
        try:
            return json.loads(row["llm_summary"])
        except (TypeError, ValueError):
            return None

    # ----------------------------------------------------------- check results
    def add_check_results(self, run_id: str, results: Iterable[CheckResult]) -> int:
        batch = list(results)
        if not batch:
            return 0
        self._cn.executemany(
            """
            INSERT OR REPLACE INTO check_result
                (run_id, check_id, family, status, rows_scanned, violations,
                 duration_ms, grain, baseline, skip_reason, error_text)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (run_id, c.check_id, c.family, c.status, c.rows_scanned, c.violations,
                 c.duration_ms, c.grain, c.baseline, c.skip_reason, c.error_text)
                for c in batch
            ],
        )
        self._cn.commit()
        return len(batch)

    def check_results(self, run_id: str) -> list[dict[str, Any]]:
        rs = self._cn.execute(
            "SELECT * FROM check_result WHERE run_id=? ORDER BY family, check_id", (run_id,)
        ).fetchall()
        return [dict(r) for r in rs]

    # ------------------------------------------------------- normalisation log
    def add_normalisation_actions(
        self, run_id: str, actions: Iterable[NormalisationAction]
    ) -> int:
        batch = list(actions)
        if not batch:
            return 0
        self._cn.executemany(
            """
            INSERT INTO normalisation_action
                (run_id, kind, target, rows_affected, rows_total, pct, check_id, detail)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            [
                (run_id, a.kind.value, a.target, a.rows_affected, a.rows_total,
                 a.pct, a.check_id, _js(a.detail))
                for a in batch
            ],
        )
        self._cn.commit()
        return len(batch)

    def normalisation_actions(self, run_id: str) -> list[dict[str, Any]]:
        rs = self._cn.execute(
            "SELECT * FROM normalisation_action WHERE run_id=?"
            " ORDER BY rows_affected DESC", (run_id,)
        ).fetchall()
        return [dict(r) for r in rs]

    # --------------------------------------------------------------- metrics
    def add_metrics(self, run_id: str, metrics: Sequence[tuple[str, str, str, float]]) -> int:
        if not metrics:
            return 0
        self._cn.executemany(
            "INSERT OR REPLACE INTO metric"
            " (run_id, table_name, column_name, metric_name, metric_value)"
            " VALUES (?,?,?,?,?)",
            [(run_id, t, c, m, v) for t, c, m, v in metrics],
        )
        self._cn.commit()
        return len(metrics)

    def baseline_metric(
        self, table_name: str, column_name: str, metric_name: str, exclude_run: str
    ) -> float | None:
        row = self._cn.execute(
            """
            SELECT m.metric_value AS v FROM metric m
            JOIN run r ON r.run_id = m.run_id
            WHERE m.table_name=? AND m.column_name=? AND m.metric_name=?
              AND m.run_id <> ? AND r.status='completed'
            ORDER BY r.started_at DESC LIMIT 1
            """,
            (table_name, column_name, metric_name, exclude_run),
        ).fetchone()
        return None if row is None else row["v"]

    # ------------------------------------------------------ Tier 2: suggestion agent
    def add_suggestion(
        self, *, title: str, family: str, severity_guess: str, hypothesis: str,
        sql_text: str, table_ref: str | None = None, test_row_count: int | None = None,
        test_sample: list[dict] | None = None,
    ) -> int:
        cur = self._cn.execute(
            """INSERT INTO suggestion
               (created_at, title, family, severity_guess, hypothesis, table_ref,
                sql_text, test_row_count, test_sample)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (datetime.now().isoformat(), title, family, severity_guess, hypothesis,
             table_ref, sql_text, test_row_count, _js(test_sample) if test_sample else None),
        )
        self._cn.commit()
        return int(cur.lastrowid)

    def suggestions(self, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM suggestion"
        args: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        rs = self._cn.execute(sql, args).fetchall()
        out = []
        for r in rs:
            d = dict(r)
            if d.get("test_sample"):
                try:
                    d["test_sample"] = json.loads(d["test_sample"])
                except (TypeError, ValueError):
                    d["test_sample"] = None
            out.append(d)
        return out

    def get_suggestion(self, suggestion_id: int) -> dict[str, Any] | None:
        row = self._cn.execute(
            "SELECT * FROM suggestion WHERE suggestion_id = ?", (suggestion_id,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        if d.get("test_sample"):
            try:
                d["test_sample"] = json.loads(d["test_sample"])
            except (TypeError, ValueError):
                d["test_sample"] = None
        return d

    def review_suggestion(
        self, suggestion_id: int, *, status: str, note: str | None = None,
        boilerplate_path: str | None = None,
    ) -> None:
        if status not in ("approved", "rejected", "pending"):
            raise ValueError(f"invalid suggestion status: {status!r}")
        self._cn.execute(
            """UPDATE suggestion
               SET status=?, reviewed_at=?, review_note=?, boilerplate_path=?
               WHERE suggestion_id=?""",
            (status, datetime.now().isoformat(), note, boilerplate_path, suggestion_id),
        )
        self._cn.commit()

    # --------------------------------------------------------------- agent trace
    def add_agent_trace(self, session_id: str, records: Iterable[Any]) -> int:
        """`records` are `ToolCallRecord` (or anything with the same attributes)."""
        batch = list(records)
        if not batch:
            return 0
        self._cn.executemany(
            """INSERT INTO agent_trace
               (session_id, step_no, tool_name, tool_input, tool_output, ok, elapsed_ms)
               VALUES (?,?,?,?,?,?,?)""",
            [
                (session_id, r.step_no, r.tool_name, r.tool_input, r.tool_output,
                 int(r.ok), r.elapsed_ms)
                for r in batch
            ],
        )
        self._cn.commit()
        return len(batch)

    def agent_trace(self, session_id: str) -> list[dict[str, Any]]:
        rs = self._cn.execute(
            "SELECT * FROM agent_trace WHERE session_id=? ORDER BY step_no", (session_id,)
        ).fetchall()
        return [dict(r) for r in rs]
