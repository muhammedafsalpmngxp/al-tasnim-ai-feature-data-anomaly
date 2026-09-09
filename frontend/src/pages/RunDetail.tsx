import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, ApiError, type Finding, type Incident, type RunSummary } from '../lib/api'
import { formatDateTime, formatNumber, SEVERITY_ORDER, SEVERITY_STYLES } from '../lib/format'
import { StatusBadge } from '../components/Badges'
import { FindingsTable } from '../components/FindingsTable'
import { IncidentsList } from '../components/IncidentsList'

type Tab = 'findings' | 'incidents'

export function RunDetail() {
  const { runId } = useParams<{ runId: string }>()
  const [run, setRun] = useState<RunSummary | null>(null)
  const [findings, setFindings] = useState<Finding[] | null>(null)
  const [incidents, setIncidents] = useState<Incident[] | null>(null)
  const [tab, setTab] = useState<Tab>('findings')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!runId) return
    setError(null)
    Promise.all([api.getRun(runId), api.getFindings(runId), api.getIncidents(runId)])
      .then(([r, f, i]) => {
        setRun(r)
        setFindings(f)
        setIncidents(i)
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Could not load this run.'))
  }, [runId])

  if (error) {
    return (
      <div className="mx-auto max-w-6xl px-6 py-8">
        <Link to="/" className="text-sm font-medium text-blue-600 hover:text-blue-800">
          ← Back to dashboard
        </Link>
        <p className="mt-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>
      </div>
    )
  }

  if (!run) {
    return <div className="mx-auto max-w-6xl px-6 py-8 text-sm text-slate-400">Loading…</div>
  }

  return (
    <div className="mx-auto max-w-6xl px-6 py-8">
      <Link to="/" className="text-sm font-medium text-blue-600 hover:text-blue-800">
        ← Back to dashboard
      </Link>

      <div className="mt-3 mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="font-mono text-lg font-semibold text-slate-900">{run.run_id}</h1>
          <p className="mt-1 text-sm text-slate-500">
            {formatDateTime(run.started_at)} · {run.db_name} · triggered by {run.triggered_by ?? 'unknown'}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <StatusBadge status={run.status} />
          {run.has_excel && (
            <a
              href={api.excelUrl(run.run_id)}
              className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
            >
              ⬇ Excel
            </a>
          )}
          {run.has_word && (
            <a
              href={api.wordUrl(run.run_id)}
              className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
            >
              ⬇ Word
            </a>
          )}
        </div>
      </div>

      <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Tile label="Rows scanned" value={formatNumber(run.rows_scanned)} />
        <Tile label="Checks" value={`${run.checks_passed}/${run.checks_run} passed`} />
        <Tile label="Findings" value={formatNumber(run.findings_total)} />
        <Tile label="LLM tokens" value={formatNumber(run.llm_tokens)} />
      </div>

      <div className="mb-6 flex flex-wrap gap-2">
        {SEVERITY_ORDER.map((sev) => {
          const count = run.severity_counts[sev] ?? 0
          if (count === 0) return null
          const style = SEVERITY_STYLES[sev]
          return (
            <span
              key={sev}
              className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-sm font-medium ${style.bg} ${style.text}`}
            >
              <span className={`h-2 w-2 rounded-full ${style.dot}`} />
              {count} {style.label}
            </span>
          )
        })}
      </div>

      <div className="mb-4 flex gap-1 border-b border-slate-200">
        <TabButton active={tab === 'findings'} onClick={() => setTab('findings')}>
          Findings ({findings?.length ?? 0})
        </TabButton>
        <TabButton active={tab === 'incidents'} onClick={() => setTab('incidents')}>
          Incidents ({incidents?.length ?? 0})
        </TabButton>
      </div>

      {tab === 'findings' && (findings ? <FindingsTable findings={findings} /> : <Loading />)}
      {tab === 'incidents' && (incidents ? <IncidentsList incidents={incidents} /> : <Loading />)}
    </div>
  )
}

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-400">{label}</p>
      <p className="mt-1 text-xl font-semibold text-slate-900">{value}</p>
    </div>
  )
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      className={`px-3 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
        active ? 'border-slate-900 text-slate-900' : 'border-transparent text-slate-400 hover:text-slate-600'
      }`}
    >
      {children}
    </button>
  )
}

function Loading() {
  return <p className="text-sm text-slate-400">Loading…</p>
}
