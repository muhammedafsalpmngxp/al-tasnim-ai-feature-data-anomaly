import { Link } from 'react-router-dom'
import type { RunSummary } from '../lib/api'
import { api } from '../lib/api'
import { formatDateTime, formatNumber } from '../lib/format'
import { StatusBadge } from './Badges'

export function RunsTable({ runs }: { runs: RunSummary[] }) {
  if (runs.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-slate-300 bg-white p-10 text-center text-sm text-slate-500">
        No runs yet — click "Generate Report" above to run the first live scan.
      </div>
    )
  }

  return (
    <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white shadow-sm">
      <table className="w-full min-w-[880px] text-left text-sm">
        <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th className="px-4 py-3 font-medium">Run</th>
            <th className="px-4 py-3 font-medium">Started</th>
            <th className="px-4 py-3 font-medium">Status</th>
            <th className="px-4 py-3 font-medium">Checks</th>
            <th className="px-4 py-3 font-medium">Findings</th>
            <th className="px-4 py-3 font-medium">Critical / High</th>
            <th className="px-4 py-3 font-medium">Reports</th>
            <th className="px-4 py-3" />
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {runs.map((r) => (
            <tr key={r.run_id} className="hover:bg-slate-50">
              <td className="px-4 py-3">
                <div className="font-mono text-xs text-slate-700">{r.run_id}</div>
                <div className="text-xs text-slate-400">by {r.triggered_by ?? 'unknown'}</div>
              </td>
              <td className="px-4 py-3 whitespace-nowrap text-slate-600">{formatDateTime(r.started_at)}</td>
              <td className="px-4 py-3">
                <StatusBadge status={r.status} />
              </td>
              <td className="px-4 py-3 whitespace-nowrap text-slate-600">
                {r.checks_passed}/{r.checks_run} passed
              </td>
              <td className="px-4 py-3 text-slate-900 font-medium">{formatNumber(r.findings_total)}</td>
              <td className="px-4 py-3">
                <span className="font-medium text-red-600">{r.severity_counts.critical ?? 0}</span>
                {' / '}
                <span className="font-medium text-orange-600">{r.severity_counts.high ?? 0}</span>
              </td>
              <td className="px-4 py-3">
                <div className="flex gap-2">
                  {r.has_excel && (
                    <a
                      href={api.excelUrl(r.run_id)}
                      className="text-xs font-medium text-slate-500 hover:text-slate-800"
                      title="Download Excel"
                    >
                      Excel
                    </a>
                  )}
                  {r.has_word && (
                    <a
                      href={api.wordUrl(r.run_id)}
                      className="text-xs font-medium text-slate-500 hover:text-slate-800"
                      title="Download Word"
                    >
                      Word
                    </a>
                  )}
                  {!r.has_excel && !r.has_word && <span className="text-xs text-slate-300">—</span>}
                </div>
              </td>
              <td className="px-4 py-3 text-right">
                <Link
                  to={`/runs/${r.run_id}`}
                  className="text-sm font-medium text-blue-600 hover:text-blue-800"
                >
                  View →
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
