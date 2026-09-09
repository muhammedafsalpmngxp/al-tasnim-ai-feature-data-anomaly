import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError, type Job } from '../lib/api'
import { formatDuration, phaseLabel } from '../lib/format'

const POLL_MS = 1500

// The whole point of this component: clicking "Generate Report" kicks off a LIVE scan
// against the source database -- normalise, run all checks, send findings to the LLM for
// narration, then build the Excel/Word files -- so it genuinely takes minutes, not
// milliseconds. This panel exists so that wait is visible (phase-by-phase) instead of the
// button just hanging with no feedback.
export function GenerateReportPanel({ onSettled }: { onSettled?: (runId: string | null) => void }) {
  const [job, setJob] = useState<Job | null>(null)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    return () => {
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [])

  function stopPolling() {
    if (timerRef.current) {
      clearInterval(timerRef.current)
      timerRef.current = null
    }
  }

  function poll(jobId: string) {
    stopPolling() // defensive: never let two intervals run at once for this component
    timerRef.current = setInterval(async () => {
      try {
        const latest = await api.getJob(jobId)
        setJob(latest)
        if (latest.status !== 'running') {
          stopPolling()
          onSettled?.(latest.run_id)
        }
      } catch {
        // transient network hiccup during a long-running poll -- keep trying, the next
        // tick will either recover or the user can see the panel stop updating
      }
    }, POLL_MS)
  }

  async function handleGenerate() {
    // Guards against a double-submission (a real double-click, or a second click landing
    // before the first request's response has re-rendered the button away) starting two
    // concurrent live runs against the same source database.
    if (starting || job) return
    setStarting(true)
    setStartError(null)
    try {
      const started = await api.startRun({ triggered_by: 'ui' })
      setJob(started)
      poll(started.job_id)
    } catch (err) {
      setStartError(err instanceof ApiError ? err.message : 'Could not start the run.')
    } finally {
      setStarting(false)
    }
  }

  function handleReset() {
    setJob(null)
    setStartError(null)
  }

  const isRunning = job?.status === 'running'
  const isDone = job?.status === 'completed'
  const isFailed = job?.status === 'failed'

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      {!job && (
        <div className="flex items-center justify-between gap-4">
          <div>
            <h2 className="text-sm font-semibold text-slate-900">Generate a new report</h2>
            <p className="mt-1 text-sm text-slate-500">
              Runs a live scan of the database (normalise → 52+ checks → AI analysis → Excel
              &amp; Word), then makes both files available to download. Takes a few minutes.
            </p>
          </div>
          <button
            onClick={handleGenerate}
            disabled={starting}
            className="shrink-0 rounded-lg bg-slate-900 px-4 py-2.5 text-sm font-medium text-white hover:bg-slate-700 active:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {starting ? 'Starting…' : 'Generate Report'}
          </button>
        </div>
      )}

      {startError && (
        <p className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{startError}</p>
      )}

      {isRunning && (
        <div>
          <div className="flex items-center gap-3">
            <span className="relative flex h-3 w-3">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-blue-400 opacity-75" />
              <span className="relative inline-flex h-3 w-3 rounded-full bg-blue-600" />
            </span>
            <div>
              <p className="text-sm font-medium text-slate-900">{phaseLabel(job.phase)}…</p>
              <p className="text-xs text-slate-500">
                Analyzing the data before the report is built — elapsed {formatDuration(job.elapsed_seconds)}
              </p>
            </div>
          </div>
          <PhaseProgressBar phase={job.phase} />
        </div>
      )}

      {isDone && job.run_id && (
        <div>
          <div className="flex items-center gap-2 text-sm font-medium text-emerald-700">
            <span className="h-2 w-2 rounded-full bg-emerald-500" />
            Report ready — analysis complete in {formatDuration(job.elapsed_seconds)}
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <a
              href={api.excelUrl(job.run_id)}
              className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
            >
              ⬇ Download Excel
            </a>
            <a
              href={api.wordUrl(job.run_id)}
              className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
            >
              ⬇ Download Word
            </a>
            <Link
              to={`/runs/${job.run_id}`}
              className="rounded-lg px-3 py-2 text-sm font-medium text-slate-600 hover:bg-slate-100"
            >
              View findings →
            </Link>
            <button
              onClick={handleReset}
              className="ml-auto text-sm font-medium text-slate-400 hover:text-slate-600"
            >
              Run again
            </button>
          </div>
        </div>
      )}

      {isFailed && (
        <div>
          <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">
            The run failed: {job.error ?? 'unknown error'}
          </p>
          <button
            onClick={handleReset}
            className="mt-3 rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700"
          >
            Try again
          </button>
        </div>
      )}
    </div>
  )
}

const PHASE_SEQUENCE = ['schema', 'normalise', 'checks', 'llm', 'persist', 'reports']

function PhaseProgressBar({ phase }: { phase: string | null }) {
  const idx = phase ? PHASE_SEQUENCE.indexOf(phase) : -1
  const pct = idx < 0 ? 5 : Math.round(((idx + 1) / PHASE_SEQUENCE.length) * 100)
  return (
    <div className="mt-4">
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
        <div
          className="h-full rounded-full bg-blue-600 transition-all duration-700 ease-out"
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="mt-2 flex justify-between text-[11px] text-slate-400">
        {PHASE_SEQUENCE.map((p) => (
          <span key={p} className={p === phase ? 'font-medium text-slate-600' : ''}>
            {p}
          </span>
        ))}
      </div>
    </div>
  )
}
