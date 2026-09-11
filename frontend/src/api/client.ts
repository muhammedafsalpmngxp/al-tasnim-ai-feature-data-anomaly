import type { RuleDetail, RuleRow, RunResult, RunRow, Status } from '../types'

// Blank during development: vite.config.ts proxies /api to the backend, so the browser talks
// to one origin and CORS never enters the picture. Set it only for a deployment where the UI
// and the API are served separately.
const BASE = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '')
const API_KEY = import.meta.env.VITE_API_KEY ?? ''

function headers(): Record<string, string> {
  const h: Record<string, string> = { 'Content-Type': 'application/json' }
  if (API_KEY) h['X-API-Key'] = API_KEY
  return h
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(BASE + path, { headers: headers() })
  if (!res.ok) {
    // The backend puts a human-readable reason in `detail`; surfacing the raw status alone
    // turns "your API key is wrong" into an unexplained 401 the user cannot act on.
    let detail = res.statusText
    try {
      detail = (await res.json()).detail ?? detail
    } catch {
      /* a non-JSON error body is still worth reporting as the status text */
    }
    throw new Error(`${res.status}: ${detail}`)
  }
  return res.json() as Promise<T>
}

export const api = {
  status: () => get<Status>('/api/status'),
  rules: (source?: string) =>
    get<{ rules: RuleRow[]; errors: string[]; total: number }>(
      '/api/rules' + (source ? `?source=${source}` : ''),
    ),
  rule: (id: string) => get<RuleDetail>(`/api/rules/${encodeURIComponent(id)}`),
  runs: () => get<{ runs: RunRow[] }>('/api/runs'),
  latestRun: () => get<RunResult>('/api/runs/latest'),
  run: (id: string) => get<RunResult>(`/api/runs/${encodeURIComponent(id)}`),
  reportUrl: (runId: string, fmt: 'xlsx' | 'docx') =>
    `${BASE}/api/runs/${encodeURIComponent(runId)}/report/${fmt}`,
}

/**
 * Download a report.
 *
 * Fetched and turned into a blob rather than opened as a plain link, because the API may
 * require an X-API-Key header and a browser navigation cannot carry one. A bare <a href> would
 * work only while the backend has no key set, and would break the moment one was configured -
 * the sort of failure that shows up in production and never in development.
 */
export async function downloadReport(runId: string, fmt: 'xlsx' | 'docx'): Promise<void> {
  const res = await fetch(api.reportUrl(runId, fmt), { headers: headers() })
  if (!res.ok) throw new Error(`${res.status}: could not download the ${fmt} report`)
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `data-quality-${runId}.${fmt}`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

export interface SseHandlers {
  onProgress?: (data: any) => void
  onResult?: (data: any) => void
  onError?: (message: string) => void
  onEnd?: () => void
}

/**
 * POST an operation and consume its Server-Sent Events.
 *
 * Written over fetch rather than EventSource, which only supports GET and cannot send the
 * X-API-Key header. Compile and run are both POSTs that need the key, so EventSource was never
 * an option here.
 *
 * The parser buffers until a blank line because an SSE event may arrive split across several
 * network chunks; treating each chunk as an event drops data at random under load, and only
 * under load, which is the hardest kind of bug to reproduce.
 */
export async function stream(path: string, handlers: SseHandlers): Promise<void> {
  const res = await fetch(BASE + path, { method: 'POST', headers: headers() })
  if (!res.ok || !res.body) {
    handlers.onError?.(`${res.status}: ${res.statusText}`)
    handlers.onEnd?.()
    return
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      let split: number
      while ((split = buffer.indexOf('\n\n')) !== -1) {
        const chunk = buffer.slice(0, split)
        buffer = buffer.slice(split + 2)

        let event = 'message'
        const dataLines: string[] = []
        for (const line of chunk.split('\n')) {
          if (line.startsWith('event:')) event = line.slice(6).trim()
          else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
        }
        if (!dataLines.length) continue

        let data: any
        try {
          data = JSON.parse(dataLines.join('\n'))
        } catch {
          continue
        }
        if (event === 'progress') handlers.onProgress?.(data)
        else if (event === 'result') handlers.onResult?.(data)
        else if (event === 'error') handlers.onError?.(data.message ?? 'Unknown error')
      }
    }
  } catch (err) {
    handlers.onError?.(err instanceof Error ? err.message : String(err))
  } finally {
    handlers.onEnd?.()
  }
}
