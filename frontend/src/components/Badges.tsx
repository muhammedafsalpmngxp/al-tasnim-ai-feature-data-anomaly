import { CLASS_STYLES, SEVERITY_STYLES, STATUS_STYLES } from '../lib/format'

export function SeverityBadge({ severity }: { severity: string }) {
  const style = SEVERITY_STYLES[severity] ?? SEVERITY_STYLES.info
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ${style.bg} ${style.text}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${style.dot}`} />
      {style.label}
    </span>
  )
}

export function ClassBadge({ findingClass }: { findingClass: string }) {
  const style = CLASS_STYLES[findingClass] ?? CLASS_STYLES.design
  return (
    <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${style.bg} ${style.text}`}>
      {style.label}
    </span>
  )
}

export function StatusBadge({ status }: { status: string }) {
  const style = STATUS_STYLES[status] ?? STATUS_STYLES.cancelled
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ${style.bg} ${style.text}`}>
      {status === 'running' && (
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-blue-600" />
      )}
      {style.label}
    </span>
  )
}
