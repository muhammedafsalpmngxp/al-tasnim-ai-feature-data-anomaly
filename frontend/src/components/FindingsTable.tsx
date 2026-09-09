import { useMemo, useState } from 'react'
import type { Finding } from '../lib/api'
import { formatNumber, SEVERITY_ORDER } from '../lib/format'
import { ClassBadge, SeverityBadge } from './Badges'

export function FindingsTable({ findings }: { findings: Finding[] }) {
  const [severity, setSeverity] = useState('')
  const [family, setFamily] = useState('')
  const [findingClass, setFindingClass] = useState('')
  const [search, setSearch] = useState('')
  const [expanded, setExpanded] = useState<number | null>(null)

  const families = useMemo(
    () => Array.from(new Set(findings.map((f) => f.family))).sort(),
    [findings],
  )
  const classes = useMemo(
    () => Array.from(new Set(findings.map((f) => f.finding_class))).sort(),
    [findings],
  )

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return findings.filter((f) => {
      if (severity && f.severity !== severity) return false
      if (family && f.family !== family) return false
      if (findingClass && f.finding_class !== findingClass) return false
      if (q && !f.title.toLowerCase().includes(q) && !f.check_id.toLowerCase().includes(q)) return false
      return true
    })
  }, [findings, severity, family, findingClass, search])

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search title or check id…"
          className="rounded-lg border border-slate-300 px-3 py-1.5 text-sm placeholder:text-slate-400 focus:border-slate-400 focus:outline-none"
        />
        <Select value={severity} onChange={setSeverity} placeholder="All severities" options={SEVERITY_ORDER as unknown as string[]} />
        <Select value={family} onChange={setFamily} placeholder="All families" options={families} />
        <Select value={findingClass} onChange={setFindingClass} placeholder="All classes" options={classes} />
        <span className="ml-auto text-xs text-slate-400">
          {filtered.length} of {findings.length} findings
        </span>
      </div>

      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
        {filtered.length === 0 ? (
          <p className="p-8 text-center text-sm text-slate-400">No findings match these filters.</p>
        ) : (
          <ul className="divide-y divide-slate-100">
            {filtered.map((f) => {
              const isOpen = expanded === f.finding_id
              return (
                <li key={f.finding_id}>
                  <button
                    onClick={() => setExpanded(isOpen ? null : f.finding_id)}
                    className="flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-slate-50"
                  >
                    <span className="mt-0.5 font-mono text-xs text-slate-400 w-20 shrink-0">{f.check_id}</span>
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium text-slate-900">{f.title}</p>
                      <div className="mt-1 flex flex-wrap items-center gap-1.5">
                        <SeverityBadge severity={f.severity} />
                        <ClassBadge findingClass={f.finding_class} />
                        {f.business_rule_ref && (
                          <span className="text-xs text-slate-400">{f.business_rule_ref}</span>
                        )}
                      </div>
                    </div>
                    <span className="shrink-0 text-sm font-medium text-slate-400">
                      {formatNumber(f.affected_count)} affected
                    </span>
                  </button>
                  {isOpen && (
                    <div className="border-t border-slate-100 bg-slate-50 px-4 py-4 text-sm">
                      {f.why_it_matters && (
                        <p className="mb-3">
                          <span className="font-medium text-slate-700">Why it matters: </span>
                          <span className="text-slate-600">{f.why_it_matters}</span>
                        </p>
                      )}
                      {f.llm_explanation && (
                        <p className="mb-3">
                          <span className="font-medium text-slate-700">Explanation: </span>
                          <span className="text-slate-600">{f.llm_explanation}</span>
                        </p>
                      )}
                      {f.llm_root_cause && (
                        <p className="mb-3">
                          <span className="font-medium text-slate-700">Root cause: </span>
                          <span className="text-slate-600">{f.llm_root_cause}</span>
                        </p>
                      )}
                      {f.llm_remediation && (
                        <p className="mb-3">
                          <span className="font-medium text-slate-700">Remediation: </span>
                          <span className="text-slate-600">{f.llm_remediation}</span>
                        </p>
                      )}
                      <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs text-slate-500 sm:grid-cols-4">
                        <div>Grain: {f.grain || '—'}</div>
                        <div>Baseline: {f.baseline || '—'}</div>
                        <div>Owner: {f.owner ?? '—'}</div>
                        <div>Entity: {f.entity_label ?? f.entity_id ?? '—'}</div>
                      </div>
                      {f.evidence.length > 0 && (
                        <pre className="mt-3 overflow-x-auto rounded-lg bg-slate-900 p-3 text-xs text-slate-100">
                          {JSON.stringify(f.evidence, null, 2)}
                        </pre>
                      )}
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </div>
  )
}

function Select({
  value,
  onChange,
  placeholder,
  options,
}: {
  value: string
  onChange: (v: string) => void
  placeholder: string
  options: string[]
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-700 focus:border-slate-400 focus:outline-none"
    >
      <option value="">{placeholder}</option>
      {options.map((o) => (
        <option key={o} value={o}>
          {o}
        </option>
      ))}
    </select>
  )
}
