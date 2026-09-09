export function formatDateTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso.includes('Z') || iso.includes('+') ? iso : `${iso}Z`)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

export function formatNumber(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  return n.toLocaleString()
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return `${m}m ${s}s`
}

export const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low', 'review', 'info'] as const

export const SEVERITY_STYLES: Record<string, { bg: string; text: string; dot: string; label: string }> = {
  critical: { bg: 'bg-red-50', text: 'text-red-700', dot: 'bg-red-600', label: 'Critical' },
  high: { bg: 'bg-orange-50', text: 'text-orange-700', dot: 'bg-orange-500', label: 'High' },
  medium: { bg: 'bg-amber-50', text: 'text-amber-700', dot: 'bg-amber-500', label: 'Medium' },
  low: { bg: 'bg-sky-50', text: 'text-sky-700', dot: 'bg-sky-500', label: 'Low' },
  review: { bg: 'bg-purple-50', text: 'text-purple-700', dot: 'bg-purple-500', label: 'Review' },
  info: { bg: 'bg-slate-100', text: 'text-slate-600', dot: 'bg-slate-400', label: 'Info' },
}

export const CLASS_STYLES: Record<string, { bg: string; text: string; label: string }> = {
  violation: { bg: 'bg-red-50', text: 'text-red-700', label: 'Violation' },
  defect: { bg: 'bg-orange-50', text: 'text-orange-700', label: 'Defect' },
  gap: { bg: 'bg-amber-50', text: 'text-amber-700', label: 'Gap' },
  pending: { bg: 'bg-sky-50', text: 'text-sky-700', label: 'Pending' },
  design: { bg: 'bg-slate-100', text: 'text-slate-600', label: 'Design' },
  risk: { bg: 'bg-purple-50', text: 'text-purple-700', label: 'Risk' },
  review: { bg: 'bg-purple-50', text: 'text-purple-700', label: 'Review' },
}

export const STATUS_STYLES: Record<string, { bg: string; text: string; label: string }> = {
  running: { bg: 'bg-blue-50', text: 'text-blue-700', label: 'Running' },
  completed: { bg: 'bg-emerald-50', text: 'text-emerald-700', label: 'Completed' },
  failed: { bg: 'bg-red-50', text: 'text-red-700', label: 'Failed' },
  cancelled: { bg: 'bg-slate-100', text: 'text-slate-600', label: 'Cancelled' },
}

export const PHASE_LABELS: Record<string, string> = {
  schema: 'Scanning database schema',
  normalise: 'Normalising & cleaning data',
  checks: 'Running data-quality checks',
  stats: 'Computing statistics',
  verify: 'Verifying findings',
  llm: 'Analyzing findings with AI',
  persist: 'Saving results',
  reports: 'Building Excel & Word reports',
}

export function phaseLabel(phase: string | null): string {
  if (!phase) return 'Starting…'
  return PHASE_LABELS[phase] ?? phase
}
