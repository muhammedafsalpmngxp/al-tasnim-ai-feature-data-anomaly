// Typed client for the FastAPI backend (backend/app/api). Field names mirror
// backend/app/api/schemas.py exactly -- keep the two in sync by hand, there is no
// codegen step for this project.

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? ''

export interface RunSummary {
  run_id: string
  started_at: string
  finished_at: string | null
  status: 'running' | 'completed' | 'failed' | 'cancelled'
  triggered_by: string | null
  db_name: string | null
  as_of_date: string | null
  rows_scanned: number
  checks_run: number
  checks_passed: number
  checks_skipped: number
  findings_total: number
  llm_model: string | null
  llm_tokens: number
  has_excel: boolean
  has_word: boolean
  error_text: string | null
  severity_counts: Record<string, number>
  class_counts: Record<string, number>
}

export interface Finding {
  finding_id: number
  run_id: string
  check_id: string
  family: string
  severity: 'critical' | 'high' | 'medium' | 'low' | 'review' | 'info'
  finding_class: 'violation' | 'defect' | 'gap' | 'pending' | 'design' | 'risk' | 'review'
  title: string
  entity_type: string
  entity_id: string | null
  entity_label: string | null
  well_id: number | null
  affected_count: number
  grain: string
  baseline: string
  business_rule_ref: string | null
  owner: string | null
  why_it_matters: string | null
  evidence: Record<string, unknown>[]
  sampled: boolean
  llm_explanation: string | null
  llm_root_cause: string | null
  llm_remediation: string | null
  status: string
}

export interface Incident {
  incident_id: number
  run_id: string
  title: string
  root_cause: string | null
  severity: string
  finding_ids: string[]
  llm_narrative: string | null
}

export interface CheckResultRow {
  check_id: string
  family: string
  status: 'pass' | 'fail' | 'skipped' | 'error'
  rows_scanned: number
  violations: number
  duration_ms: number
  grain: string
  baseline: string
  skip_reason: string | null
  error_text: string | null
}

export interface NormalisationAction {
  kind: string
  target: string
  rows_affected: number
  rows_total: number
  [key: string]: unknown
}

export interface CheckCatalogueEntry {
  id: string
  family: string
  title: string
  severity: string
  business_rule_ref: string | null
  grain: string
  baseline: string
}

export interface Job {
  job_id: string
  status: 'running' | 'completed' | 'failed'
  run_id: string | null
  phase: string | null
  phase_state: string | null
  phase_detail: Record<string, unknown>
  error: string | null
  started_at: number
  finished_at: number | null
  elapsed_seconds: number
}

export interface StartRunOptions {
  tables?: string[]
  triggered_by?: string
  enrich?: boolean
  generate_reports?: boolean
}

class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    const body = await res.text()
    let message = body
    try {
      message = JSON.parse(body).detail ?? body
    } catch {
      /* not JSON, use raw text */
    }
    throw new ApiError(res.status, message || res.statusText)
  }
  return res.json() as Promise<T>
}

export const api = {
  health: () => req<{ status: string; db_name: string; llm_model: string }>('/api/health'),

  listRuns: (limit = 50) => req<RunSummary[]>(`/api/runs?limit=${limit}`),
  getRun: (runId: string) => req<RunSummary>(`/api/runs/${runId}`),

  getFindings: (
    runId: string,
    params: { family?: string; severity?: string; finding_class?: string; actionable_only?: boolean } = {},
  ) => {
    const qs = new URLSearchParams()
    if (params.family) qs.set('family', params.family)
    if (params.severity) qs.set('severity', params.severity)
    if (params.finding_class) qs.set('finding_class', params.finding_class)
    if (params.actionable_only) qs.set('actionable_only', 'true')
    qs.set('limit', '2000')
    return req<Finding[]>(`/api/runs/${runId}/findings?${qs.toString()}`)
  },

  getIncidents: (runId: string) => req<Incident[]>(`/api/runs/${runId}/incidents`),
  getCheckResults: (runId: string) => req<CheckResultRow[]>(`/api/runs/${runId}/checks`),
  getNormalisationActions: (runId: string) =>
    req<NormalisationAction[]>(`/api/runs/${runId}/normalisation`),
  getCheckCatalogue: () => req<CheckCatalogueEntry[]>('/api/checks'),

  excelUrl: (runId: string) => `${API_BASE}/api/runs/${runId}/report/excel`,
  wordUrl: (runId: string) => `${API_BASE}/api/runs/${runId}/report/word`,

  startRun: (opts: StartRunOptions = {}) =>
    req<Job>('/api/runs', { method: 'POST', body: JSON.stringify(opts) }),
  getJob: (jobId: string) => req<Job>(`/api/jobs/${jobId}`),
}

export { ApiError }
