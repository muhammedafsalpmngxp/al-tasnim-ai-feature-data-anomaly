import { useCallback, useEffect, useState } from 'react'
import { api, type RunSummary } from '../lib/api'
import { GenerateReportPanel } from '../components/GenerateReportPanel'
import { RunsTable } from '../components/RunsTable'

export function Dashboard() {
  const [runs, setRuns] = useState<RunSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(() => {
    api
      .listRuns(50)
      .then(setRuns)
      .catch(() => setError('Could not reach the API. Is the backend running?'))
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  const latest = runs?.[0]

  return (
    <div className="mx-auto max-w-6xl px-6 py-8">
      <div className="mb-6">
        <h1 className="text-xl font-semibold text-slate-900">Data Quality &amp; Anomaly Sentinel</h1>
        <p className="mt-1 text-sm text-slate-500">
          Live data-quality and business-rule monitoring for the PDO / Al Tasnim well-construction project.
        </p>
      </div>

      <div className="mb-6 grid grid-cols-2 gap-4 sm:grid-cols-4">
        <StatTile label="Total runs" value={runs?.length ?? '—'} />
        <StatTile label="Latest findings" value={latest?.findings_total ?? '—'} />
        <StatTile
          label="Latest critical"
          value={latest?.severity_counts.critical ?? 0}
          tone={latest && (latest.severity_counts.critical ?? 0) > 0 ? 'critical' : undefined}
        />
        <StatTile
          label="Latest high"
          value={latest?.severity_counts.high ?? 0}
          tone={latest && (latest.severity_counts.high ?? 0) > 0 ? 'high' : undefined}
        />
      </div>

      <div className="mb-8">
        <GenerateReportPanel onSettled={refresh} />
      </div>

      {error && <p className="mb-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}

      <h2 className="mb-3 text-sm font-semibold text-slate-900">Run history</h2>
      {runs ? <RunsTable runs={runs} /> : <p className="text-sm text-slate-400">Loading…</p>}
    </div>
  )
}

function StatTile({
  label,
  value,
  tone,
}: {
  label: string
  value: string | number
  tone?: 'critical' | 'high'
}) {
  const valueColor =
    tone === 'critical' ? 'text-red-600' : tone === 'high' ? 'text-orange-600' : 'text-slate-900'
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-400">{label}</p>
      <p className={`mt-1 text-2xl font-semibold ${valueColor}`}>{value}</p>
    </div>
  )
}
