// Mirrors the JSON the backend actually returns. Kept deliberately close to the Python shapes
// in app/rules/spec.py and app/graph/run_state.py: a UI type that "tidies up" the server's
// vocabulary makes every later change a translation exercise in two places.

export type Severity = 'critical' | 'high' | 'medium' | 'low'

export interface Totals {
  probes_run: number
  probes_failed: number
  probes_clean: number
  probes_with_findings: number
  probes_empty_scope: number
  records_examined: number
  records_flagged: number
  not_running: number
}

export interface Finding {
  rule_id: string
  title: string
  category: string
  severity: Severity
  scope_total: number
  anomaly_count: number
  anomaly_pct: number
  share: number
  contribution: number
  detail_total: number
  concerns: string[]
}

// Which database a run measured. Optional because runs recorded before the field existed do
// not have it - and an absent value means UNKNOWN, never "the current one". See RunSummary in
// backend/app/runner.py for why guessing is the thing to avoid.
export interface DatabaseRef {
  name: string
  server: string
}

export interface NotRunning {
  rule_id: string
  title: string
  status: string
  reason: string
}

export interface RunResult {
  run_id: string
  started_at: string
  database?: DatabaseRef
  seconds: number
  score: number
  score_basis: string
  summary: string
  totals: Totals
  by_severity: Record<string, number>
  by_category: { category: string; rules: number; anomalies: number }[]
  ranked: Finding[]
  // Probes that examined ZERO records. Neither clean nor flagged - unmonitored. The UI must
  // never fold these into a pass, which is why they arrive as their own list.
  empty_scope: string[]
  not_running: NotRunning[]
  failed: { rule_id: string; error: string }[]
  report_paths: Record<string, string>
  catalog_stale: boolean
  catalog_note: string
  error: string
}

export interface RunRow {
  run_id: string
  started_at: string
  database?: DatabaseRef
  seconds: number
  score: number
  totals: Totals
  by_severity: Record<string, number>
  report_paths: Record<string, string>
  llm_calls: number
  error: string
}

export interface RuleRow {
  rule_id: string
  title: string
  category: string
  severity: Severity
  entity: string
  method: string
  sql_mode: string
  source: 'declared' | 'generic'
  status: string
  compiled_status: string
  compiled_at: string
  tables: string[]
  error: string
  llm_calls: number
}

export interface RuleDetail {
  rule_id: string
  title: string
  category: string
  severity: string
  method: string
  sql_mode: string
  source: string
  body: string
  compiled: {
    summary_sql: string
    detail_sql: string
    status: string
    tables: string[]
    grounding_note: string
    verifier_note: string
    threshold_note: string
    error: string
    llm_calls: number
    compiled_at: string
  } | null
}

// The one heavy operation this server is running, as anybody may read it - not only the tab
// that started it. `elapsed` is computed SERVER-side; the client's clock is not the server's.
export interface JobSnapshot {
  job: 'compile' | 'run'
  started_at: string
  elapsed: number
  done: number
  total: number
  label: string
}

export interface Status {
  database: DatabaseRef
  schemas: string[]
  models: { main: string; fast: string }
  catalog: {
    compiled_at: string
    total: number
    active: number
    failed: number
    not_applicable: number
  }
  runs: number
  // Split by database, decided on the SERVER so the dashboard and `python -m app.cli runs`
  // cannot give different answers to "how much history is there for this database?".
  runs_here: number
  runs_elsewhere: number
  latest_run: RunRow | null
  busy: boolean
  job: JobSnapshot | null
}
