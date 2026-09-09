import type { Incident } from '../lib/api'
import { SeverityBadge } from './Badges'

export function IncidentsList({ incidents }: { incidents: Incident[] }) {
  if (incidents.length === 0) {
    return (
      <p className="rounded-xl border border-dashed border-slate-300 bg-white p-6 text-center text-sm text-slate-400">
        No incidents were correlated for this run.
      </p>
    )
  }
  return (
    <ul className="space-y-3">
      {incidents.map((inc) => (
        <li key={inc.incident_id} className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <div className="mb-2 flex items-start justify-between gap-3">
            <p className="text-sm font-semibold text-slate-900">{inc.title}</p>
            <SeverityBadge severity={inc.severity} />
          </div>
          {inc.llm_narrative && <p className="mb-2 text-sm text-slate-600">{inc.llm_narrative}</p>}
          {inc.root_cause && (
            <p className="mb-2 text-xs text-slate-500">
              <span className="font-medium">Root cause: </span>
              {inc.root_cause}
            </p>
          )}
          <div className="flex flex-wrap gap-1">
            {inc.finding_ids.map((id) => (
              <span key={id} className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-500">
                {id}
              </span>
            ))}
          </div>
        </li>
      ))}
    </ul>
  )
}
