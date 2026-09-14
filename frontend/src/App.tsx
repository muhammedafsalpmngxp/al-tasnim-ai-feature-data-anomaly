import { useCallback, useEffect, useState } from 'react'
import { api, downloadReport, stream } from './api/client'
import { CategoryBar, SEVERITY_COLOURS, ScoreTrend, SeverityDonut } from './components/Charts'
import type { RuleDetail, RuleRow, RunResult, RunRow, Status } from './types'

type Tab = 'dashboard' | 'findings' | 'rules' | 'runs'

const n = (v: number | undefined) => (v ?? 0).toLocaleString()

function scoreClass(score: number): string {
  return score >= 90 ? 'good' : score >= 70 ? 'warn' : 'bad'
}

function Sev({ value }: { value: string }) {
  return (
    <span className="sev" style={{ background: SEVERITY_COLOURS[value] ?? '#888' }}>
      {value}
    </span>
  )
}

// ── Action bar: compile and run, with live progress ────────────────────────────
// Both operations stream. A compile can take twenty minutes and a run several minutes, and a
// button that simply goes grey for that long is indistinguishable from one that has crashed.

function Actions({ onDone, busy }: { onDone: () => void; busy: boolean }) {
  const [active, setActive] = useState<'' | 'compile' | 'run'>('')
  const [progress, setProgress] = useState('')
  const [error, setError] = useState('')

  const go = (job: 'compile' | 'run', path: string) => {
    setActive(job)
    setError('')
    setProgress('starting…')
    stream(path, {
      onProgress: (d) => {
        if (job === 'compile') {
          setProgress(`${d.done} of ${d.total}${d.rule_id ? ` — ${d.rule_id}` : ''}`)
        } else {
          setProgress(`stage ${d.step} of ${d.total}${d.stage ? ` — ${d.stage}` : ''}`)
        }
      },
      onResult: (d) => {
        setProgress(
          job === 'compile'
            ? `compiled ${d.compiled?.length ?? 0}, reused ${d.reused?.length ?? 0}, failed ${
                d.failed?.length ?? 0
              } — ${d.llm_calls} LLM call(s)`
            : `score ${d.score} — ${d.totals?.records_flagged?.toLocaleString() ?? 0} record(s) flagged`,
        )
        // A detection run exists to produce the report, so hand it over rather than leaving the
        // reader to find the download button. Compile produces no report at all - it only
        // rebuilds the SQL - so there is deliberately nothing to deliver on that path.
        if (job === 'run' && d.run_id && d.report_paths?.docx) {
          downloadReport(d.run_id, 'docx').catch((e) =>
            setError(`The run finished but the Word report could not be downloaded: ${e}`),
          )
        }
      },
      onError: setError,
      onEnd: () => {
        setActive('')
        onDone()
      },
    })
  }

  const disabled = !!active || busy
  return (
    <div className="actions">
      <button disabled={disabled} onClick={() => go('run', '/api/run')}>
        {active === 'run' ? 'Running…' : 'Run detection'}
      </button>
      <button className="secondary" disabled={disabled} onClick={() => go('compile', '/api/compile')}>
        {active === 'compile' ? 'Compiling…' : 'Compile rules'}
      </button>
      {progress && <span className="muted">{progress}</span>}
      {busy && !active && <span className="muted">another operation is in progress</span>}
      {error && <span className="error">{error}</span>}
    </div>
  )
}

// ── Coverage gaps ──────────────────────────────────────────────────────────────
// Rendered as a WARNING, never folded into the clean count. A check that examined nothing has
// proved nothing, and showing it as a pass is the single most misleading thing this UI could
// do - the reader would conclude an unmonitored area is healthy.

function Gaps({ run }: { run: RunResult }) {
  const gaps = run.empty_scope ?? []
  const notRunning = run.not_running ?? []
  const failed = run.failed ?? []
  if (!gaps.length && !notRunning.length && !failed.length) return null

  return (
    <section className="card warn-card">
      <h2>Checks that did not report</h2>
      <p className="muted">
        These produced no verdict about their subject. Absence of a finding here does not mean
        absence of a problem — these areas are currently unmonitored.
      </p>
      {gaps.length > 0 && (
        <p>
          <strong>{gaps.length} examined no records at all:</strong> {gaps.join(', ')}
        </p>
      )}
      {failed.map((f) => (
        <p key={f.rule_id}>
          <strong>{f.rule_id}</strong> failed: <span className="muted">{f.error}</span>
        </p>
      ))}
      {notRunning.map((x) => (
        <p key={x.rule_id}>
          <strong>{x.rule_id}</strong> {x.status}: <span className="muted">{x.reason}</span>
        </p>
      ))}
    </section>
  )
}

function FindingsTable({ run, onPick }: { run: RunResult; onPick: (id: string) => void }) {
  if (!run.ranked?.length) return <p className="muted">No check found anything to report.</p>
  return (
    <table>
      <thead>
        <tr>
          <th>Rule</th>
          <th>What was found</th>
          <th>Severity</th>
          <th className="num">Affected</th>
          <th className="num">Of examined</th>
          <th className="num">Share</th>
        </tr>
      </thead>
      <tbody>
        {run.ranked.map((f) => (
          <tr key={f.rule_id} className="clickable" onClick={() => onPick(f.rule_id)}>
            <td className="mono">{f.rule_id}</td>
            <td>
              {f.title}
              {f.concerns?.length > 0 && (
                <div className="note">⚠ {f.concerns[0]}</div>
              )}
            </td>
            <td><Sev value={f.severity} /></td>
            <td className="num">{n(f.anomaly_count)}</td>
            <td className="num">{n(f.scope_total)}</td>
            <td className="num">{f.anomaly_pct.toFixed(2)}%</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

// ── Rule drawer: the exact SQL that runs ───────────────────────────────────────
// Exposed deliberately. The probes decide what the report says, so being able to read precisely
// what executes is what makes a finding auditable instead of something taken on trust.

function RuleDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const [rule, setRule] = useState<RuleDetail | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    api.rule(id).then(setRule).catch((e) => setError(String(e)))
  }, [id])

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()}>
        <button className="close" onClick={onClose}>×</button>
        {error && <p className="error">{error}</p>}
        {!rule && !error && <p className="muted">Loading…</p>}
        {rule && (
          <>
            <h2>{rule.rule_id}</h2>
            <p className="lead">{rule.title}</p>
            <p className="muted">
              {rule.category} · {rule.severity} · {rule.method} · {rule.sql_mode} · {rule.source}
            </p>
            {rule.body && <pre className="prose">{rule.body.replace(/\*\*/g, '')}</pre>}
            {rule.compiled ? (
              <>
                {rule.compiled.grounding_note && (
                  <p><strong>Grounding:</strong> {rule.compiled.grounding_note}</p>
                )}
                {rule.compiled.threshold_note && (
                  <p><strong>Threshold:</strong> {rule.compiled.threshold_note}</p>
                )}
                {rule.compiled.verifier_note && (
                  <p><strong>Reviewer:</strong> {rule.compiled.verifier_note}</p>
                )}
                {rule.compiled.error && <p className="error">{rule.compiled.error}</p>}
                <h3>Summary query</h3>
                <pre className="sql">{rule.compiled.summary_sql}</pre>
                <h3>Detail query</h3>
                <pre className="sql">{rule.compiled.detail_sql}</pre>
              </>
            ) : (
              <p className="muted">This rule has not been compiled yet.</p>
            )}
          </>
        )}
      </aside>
    </div>
  )
}

function RulesTab({ onPick }: { onPick: (id: string) => void }) {
  const [rules, setRules] = useState<RuleRow[]>([])
  const [filter, setFilter] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    api.rules().then((d) => setRules(d.rules)).catch((e) => setError(String(e)))
  }, [])

  const needle = filter.trim().toLowerCase()
  const shown = needle
    ? rules.filter(
        (r) =>
          r.rule_id.toLowerCase().includes(needle) ||
          r.title.toLowerCase().includes(needle) ||
          r.category.toLowerCase().includes(needle),
      )
    : rules

  return (
    <section className="card">
      <div className="row-between">
        <h2>Rules ({shown.length} of {rules.length})</h2>
        <input
          placeholder="Filter by id, title or category…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
      </div>
      {error && <p className="error">{error}</p>}
      <table>
        <thead>
          <tr>
            <th>Rule</th>
            <th>Title</th>
            <th>Severity</th>
            <th>Source</th>
            <th>Mode</th>
            <th>Compiled</th>
          </tr>
        </thead>
        <tbody>
          {shown.slice(0, 400).map((r) => (
            <tr key={r.rule_id} className="clickable" onClick={() => onPick(r.rule_id)}>
              <td className="mono">{r.rule_id}</td>
              <td>{r.title}</td>
              <td><Sev value={r.severity} /></td>
              <td>{r.source}</td>
              <td>{r.sql_mode}</td>
              <td className={r.compiled_status === 'active' ? 'ok' : 'warnText'}>
                {r.compiled_status}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {shown.length > 400 && (
        <p className="muted">Showing the first 400 — use the filter to narrow it down.</p>
      )}
    </section>
  )
}

function RunsTab({
  runs,
  onOpen,
  onError,
}: {
  runs: RunRow[]
  onOpen: (id: string) => void
  onError: (message: string) => void
}) {
  return (
    <>
      <section className="card">
        <h2>Score over time</h2>
        <ScoreTrend runs={runs} />
      </section>
      <section className="card">
        <h2>Run history</h2>
        <table>
          <thead>
            <tr>
              <th>Run</th>
              <th>When</th>
              <th className="num">Score</th>
              <th className="num">With findings</th>
              <th className="num">Flagged</th>
              <th className="num">Took</th>
              <th>Reports</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.run_id}>
                <td className="mono clickable" onClick={() => onOpen(r.run_id)}>{r.run_id}</td>
                <td>{r.started_at.slice(0, 19).replace('T', ' ')}</td>
                <td className={'num ' + scoreClass(r.score)}>{r.score}</td>
                <td className="num">{n(r.totals?.probes_with_findings)}</td>
                <td className="num">{n(r.totals?.records_flagged)}</td>
                <td className="num">{r.seconds}s</td>
                <td>
                  {(['xlsx', 'docx'] as const).map((f) =>
                    r.report_paths?.[f] ? (
                      <button
                        key={f}
                        className="link"
                        onClick={() =>
                          downloadReport(r.run_id, f).catch((e) => onError(String(e)))
                        }
                      >
                        {f}
                      </button>
                    ) : null,
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!runs.length && <p className="muted">No run has been recorded yet.</p>}
      </section>
    </>
  )
}

// **double asterisks** become bold, matching exactly what the summarizer is told it may use
// and what the Word report renders. Printing the line verbatim showed the reader
// "**89.3 out of 100**", asterisks included.
//
// Split on the delimiter rather than setting innerHTML: the summary is model-written text and
// must never reach the page as markup.
function renderEmphasis(text: string) {
  return text.split(/\*\*(.+?)\*\*/g).map((part, i) =>
    i % 2 === 1 ? <strong key={i}>{part}</strong> : <span key={i}>{part}</span>
  )
}

export default function App() {
  const [tab, setTab] = useState<Tab>('dashboard')
  const [status, setStatus] = useState<Status | null>(null)
  const [run, setRun] = useState<RunResult | null>(null)
  const [runs, setRuns] = useState<RunRow[]>([])
  const [picked, setPicked] = useState('')
  const [error, setError] = useState('')

  const refresh = useCallback(() => {
    api.status().then(setStatus).catch((e) => setError(String(e)))
    api.runs().then((d) => setRuns(d.runs)).catch(() => setRuns([]))
    // A 404 here is the normal state before the first run, not an error worth shouting about.
    api.latestRun().then(setRun).catch(() => setRun(null))
  }, [])

  useEffect(refresh, [refresh])

  const openRun = (id: string) => {
    api.run(id).then((r) => { setRun(r); setTab('findings') }).catch((e) => setError(String(e)))
  }

  return (
    <div className="app">
      <header>
        <div>
          <h1>Data Quality &amp; Anomaly Sentinel</h1>
          {status && (
            <p className="muted">
              {status.database.name} @ {status.database.server} · {status.catalog.active} active
              probe(s) · {status.models.main}
            </p>
          )}
        </div>
        <Actions onDone={refresh} busy={!!status?.busy} />
      </header>

      {error && <p className="error banner">{error}</p>}
      {run?.catalog_stale && <p className="error banner">{run.catalog_note}</p>}

      <nav>
        {(['dashboard', 'findings', 'rules', 'runs'] as Tab[]).map((t) => (
          <button key={t} className={tab === t ? 'active' : ''} onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
      </nav>

      {tab === 'dashboard' && (
        <>
          {!run && <section className="card"><p className="muted">
            No run yet. Compile the rules, then press <strong>Run detection</strong>.
          </p></section>}
          {run && (
            <>
              <section className="cards">
                <div className={'card metric ' + scoreClass(run.score)}>
                  <span className="big">{run.score}</span>
                  <span>out of 100</span>
                  <span className="muted small">{run.score_basis}</span>
                </div>
                <div className="card metric">
                  <span className="big">{n(run.totals?.records_flagged)}</span>
                  <span>records flagged</span>
                  <span className="muted small">
                    of {n(run.totals?.records_examined)} examined
                  </span>
                </div>
                <div className="card metric">
                  <span className="big">{run.totals?.probes_with_findings ?? 0}</span>
                  <span>checks with findings</span>
                  <span className="muted small">
                    {run.totals?.probes_clean ?? 0} clean · {run.totals?.probes_failed ?? 0} failed
                  </span>
                </div>
              </section>

              <section className="card">
                <h2>Executive summary</h2>
                {run.summary
                  ? run.summary.split('\n').filter(Boolean).map((line, i) => {
                      const bullet = /^[-*•]\s+/.test(line)
                      const text = line.replace(/^[-*•]\s*/, '')
                      return (
                        <p key={i} className={bullet ? 'summary-bullet' : undefined}>
                          {renderEmphasis(text)}
                        </p>
                      )
                    })
                  : <p className="muted">No summary was written for this run.</p>}
              </section>

              <section className="cards">
                <div className="card">
                  <h2>Findings by severity</h2>
                  <SeverityDonut bySeverity={run.by_severity ?? {}} />
                </div>
                <div className="card">
                  <h2>Records affected by category</h2>
                  <CategoryBar findings={run.ranked ?? []} />
                </div>
              </section>

              <Gaps run={run} />

              <section className="card">
                <div className="row-between">
                  <h2>Reports</h2>
                  <div>
                    {(['xlsx', 'docx'] as const).map((f) =>
                      run.report_paths?.[f] ? (
                        <button
                          key={f}
                          onClick={() =>
                            downloadReport(run.run_id, f).catch((e) => setError(String(e)))
                          }
                        >
                          Download {f === 'xlsx' ? 'Excel' : 'Word'}
                        </button>
                      ) : null,
                    )}
                  </div>
                </div>
              </section>
            </>
          )}
        </>
      )}

      {tab === 'findings' && run && (
        <>
          <section className="card">
            <h2>Findings — run {run.run_id}</h2>
            <FindingsTable run={run} onPick={setPicked} />
          </section>
          <Gaps run={run} />
        </>
      )}
      {tab === 'findings' && !run && (
        <section className="card"><p className="muted">No run to show yet.</p></section>
      )}

      {tab === 'rules' && <RulesTab onPick={setPicked} />}
      {tab === 'runs' && <RunsTab runs={runs} onOpen={openRun} onError={setError} />}

      {picked && <RuleDrawer id={picked} onClose={() => setPicked('')} />}
    </div>
  )
}
