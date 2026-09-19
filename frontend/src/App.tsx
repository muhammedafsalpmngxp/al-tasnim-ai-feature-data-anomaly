import { useCallback, useEffect, useRef, useState } from 'react'
import { api, downloadReport, stream } from './api/client'
import { CategoryBar, SEVERITY_COLOURS, ScoreTrend, SeverityDonut } from './components/Charts'
import type {
  DatabaseRef,
  Discoveries,
  JobSnapshot,
  RuleDetail,
  RuleRow,
  RunResult,
  RunRow,
  Status,
} from './types'

type Tab = 'dashboard' | 'findings' | 'discover' | 'rules' | 'runs'

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

// Elapsed seconds since `since`, ticking once a second. Returns 0 when nothing is running.
//
// A compile takes tens of minutes. Without a clock the only evidence it is alive is a line of
// text that changes every ~20 seconds, which is not distinguishable from a hang for long enough
// that people kill the job - and killing it loses the whole run.
function useElapsed(since: number | null): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (since === null) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [since])
  return since === null ? 0 : Math.max(0, Math.floor((now - since) / 1000))
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  if (m < 60) return `${m}m ${String(s).padStart(2, '0')}s`
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`
}

function sameDatabase(a: DatabaseRef | undefined, b: DatabaseRef | undefined): boolean {
  if (!a?.name || !b?.name) return false
  return (
    a.name.toLowerCase() === b.name.toLowerCase() &&
    (a.server ?? '').toLowerCase() === (b.server ?? '').toLowerCase()
  )
}

function Actions({
  onDone,
  busy,
  failed,
  job,
}: {
  onDone: () => void
  busy: boolean
  failed: number
  // The server's view of whatever is running, which may be a job this tab never started.
  job: JobSnapshot | null
}) {
  const [active, setActive] = useState<'' | 'compile' | 'run'>('')
  const [progress, setProgress] = useState('')
  const [error, setError] = useState('')
  // done/total drive the bar; startedAt drives the clock. Kept as numbers rather than parsed
  // back out of the label, so the bar cannot disagree with the text beside it.
  const [done, setDone] = useState(0)
  const [total, setTotal] = useState(0)
  const [startedAt, setStartedAt] = useState<number | null>(null)
  const elapsed = useElapsed(startedAt)

  // A poll is up to three seconds old, so THIS tab's job can finish while the last snapshot
  // still describes it as running. Without this the bar would jump back for one poll and hide
  // the line that says what the job actually produced - the one thing the operator was waiting
  // for. Set when our own stream ends, cleared as soon as the server agrees nothing is running.
  const [ownJobEnded, setOwnJobEnded] = useState(false)
  useEffect(() => {
    if (!job) setOwnJobEnded(false)
  }, [job])
  const serverJob = ownJobEnded ? null : job

  const go = (job: 'compile' | 'run', path: string) => {
    setActive(job)
    setError('')
    setProgress('starting…')
    setDone(0)
    setTotal(0)
    setStartedAt(Date.now())
    stream(path, {
      onProgress: (d) => {
        const at = Number(job === 'compile' ? d.done : d.step) || 0
        const of = Number(d.total) || 0
        setDone(at)
        setTotal(of)
        if (job === 'compile') {
          setProgress(`${at} of ${of}${d.rule_id ? ` — ${d.rule_id}` : ''}`)
        } else {
          setProgress(`stage ${at} of ${of}${d.stage ? ` — ${d.stage}` : ''}`)
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
        setStartedAt(null)
        setOwnJobEnded(true)
        onDone()
      },
    })
  }

  const disabled = !!active || busy

  // WHAT TO DRAW. This tab's own stream when it has one, otherwise the server's snapshot of
  // whatever is running - which is what a reloaded page, a second tab or a colleague's browser
  // sees. Without the second case the whole element disappeared the moment the stream was lost,
  // leaving a disabled button and the words "another operation is in progress" beside it for
  // the remaining twenty minutes: the exact "is it working or is it hung?" this bar exists to
  // answer, reached by closing a tab.
  const adopted = !active && !!serverJob
  const shownKind = active || serverJob?.job || ''
  const shownDone = active ? done : serverJob?.done ?? 0
  const shownTotal = active ? total : serverJob?.total ?? 0
  const shownElapsed = active ? elapsed : Math.round(serverJob?.elapsed ?? 0)
  const shownText = active
    ? progress
    : serverJob
      ? shownTotal > 0
        ? `${shownDone} of ${shownTotal}${serverJob.label ? ` — ${serverJob.label}` : ''}`
        : 'starting…'
      : ''

  const pct = shownTotal > 0 ? Math.min(100, Math.round((shownDone / shownTotal) * 100)) : 0
  // Projected from the rate actually observed, not from an assumed cost per rule: rules differ
  // enormously (a cloned family member costs no model call at all), so a fixed estimate would
  // be wrong in both directions. Shown only once there is enough evidence to mean anything.
  const remaining =
    shownKind === 'compile' && shownDone >= 3 && shownTotal > shownDone && shownElapsed > 0
      ? Math.round((shownElapsed / shownDone) * (shownTotal - shownDone))
      : 0

  return (
    <div className="actions">
      <div className="actions-row">
        <button disabled={disabled} onClick={() => go('run', '/api/run')}>
          {active === 'run' ? 'Running…' : 'Run detection'}
        </button>
        <button
          className="secondary"
          disabled={disabled}
          onClick={() => go('compile', '/api/compile')}
        >
          {active === 'compile' ? 'Compiling…' : 'Compile rules'}
        </button>
        {/*
          Shown only when something is actually failed, and labelled with the count.

          A rule that failed to compile is NOT retried by an ordinary compile, on purpose: it
          failed for a real semantic reason (the verifier rejected its SQL), so retrying it
          every time would spend the full authoring cost on every compile to fail again.

          But that left the only way to retry it on the command line, which for an operator who
          works in this UI means it may as well not exist. A separate button keeps the default
          cheap while making the deliberate, occasional retry a click - the count is on the
          label because retrying five rules costs about five authoring cycles, and that is worth
          seeing before pressing it.
        */}
        {failed > 0 && (
          <button
            className="secondary"
            disabled={disabled}
            title={
              `${failed} rule(s) failed to compile and are not retried by an ordinary compile. ` +
              `This re-attempts them from scratch, which costs roughly ${failed} authoring cycle(s).`
            }
            onClick={() => go('compile', '/api/compile?retry_failed=true')}
          >
            {active === 'compile' ? 'Retrying…' : `Retry ${failed} failed`}
          </button>
        )}
        {busy && !active && !serverJob && (
          <span className="muted">another operation is in progress</span>
        )}
        {error && <span className="error">{error}</span>}
      </div>

      {(active || serverJob) && (
        <div className="job" role="status" aria-live="polite">
          <div
            className="job-bar"
            role="progressbar"
            aria-valuenow={pct}
            aria-valuemin={0}
            aria-valuemax={100}
          >
            {/* Indeterminate until the first progress event: a bar sitting at 0% for twenty
                seconds reads as stuck, which is the impression this whole element exists to
                avoid. */}
            <div
              className={shownTotal > 0 ? 'job-fill' : 'job-fill indeterminate'}
              style={shownTotal > 0 ? { width: `${pct}%` } : undefined}
            />
          </div>
          <div className="job-line">
            <span>
              {shownTotal > 0 && <strong>{pct}%</strong>} {shownText}
              {/* Said plainly. Someone who did not press the button needs to know the work is
                  real and already under way, not that their own click went astray. */}
              {adopted && (
                <span className="muted">
                  {' '}— {shownKind === 'compile' ? 'compile' : 'detection run'} already in
                  progress, started {serverJob?.started_at?.slice(11, 16)}
                </span>
              )}
            </span>
            <span className="muted">
              {formatDuration(shownElapsed)} elapsed
              {remaining > 0 && ` · about ${formatDuration(remaining)} left`}
            </span>
          </div>
        </div>
      )}
      {!active && !serverJob && progress && <span className="muted">{progress}</span>}
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

// ── Discover ───────────────────────────────────────────────────────────────────
// Proposals from the Scout, and what has been decided about them. Three lists that are always
// visible - pending, accepted, refused - because the decision history IS the feature: without
// the refused list on screen, nobody can tell whether a proposal is new or one they already
// turned down.
//
// NOTHING HERE COMPILES OR RUNS ANYTHING. Accepting writes a rule into a file; the ordinary
// Compile button is what turns it into SQL, through the same author and reviewer as every
// hand-written rule. The banner says so, because a user who expects Accept to do the whole job
// will wonder why nothing appeared in their findings.

function DiscoverTab({ busy, onRun }: { busy: boolean; onRun: () => void }) {
  const [data, setData] = useState<Discoveries | null>(null)
  const [error, setError] = useState('')
  const [working, setWorking] = useState('')
  const [reason, setReason] = useState<Record<string, string>>({})

  const load = useCallback(() => {
    api.discoveries().then(setData).catch((e) => setError(String(e)))
  }, [])
  useEffect(load, [load])
  // Re-read when a Scout run finishes: the pending list is written by the backend at the end of
  // the job, so the screen would otherwise keep showing what was there before it started.
  useEffect(() => { if (!busy) load() }, [busy, load])

  async function act(label: string, fn: () => Promise<unknown>) {
    setWorking(label)
    setError('')
    try {
      await fn()
      load()
    } catch (e) {
      setError(String(e))
    } finally {
      setWorking('')
    }
  }

  const pending = data?.pending ?? []
  const accepted = data?.accepted ?? []
  const rejected = data?.rejected ?? []

  return (
    <>
      <section className="card">
        <div className="row-between">
          <h2>Discover new anomalies</h2>
          <button className="primary" disabled={busy} onClick={onRun}>
            {busy ? 'Looking…' : 'Run discovery'}
          </button>
        </div>
        <p className="muted">
          The Scout reads what has been measured about this database and proposes checks nobody
          has written a rule for yet. It proposes only — nothing runs until you accept it, and
          an accepted rule still has to be compiled before it produces findings.
        </p>
        {error && <p className="error">{error}</p>}
        {data && (
          <p className="muted small">
            Decisions are recorded permanently in <span className="mono">{data.path}</span>, so
            they survive clearing the cache and a refused idea is never proposed again.
          </p>
        )}
      </section>

      <section className="card">
        <h2>Awaiting a decision ({pending.length})</h2>
        {pending.length === 0 && (
          <p className="muted">
            Nothing is waiting. Run discovery to look for anomalies that have no rule yet.
          </p>
        )}
        {pending.map((p) => (
          <div key={p.hash} className="finding">
            <div className="row-between">
              <h3>{p.title}</h3>
              <Sev value={String(p.severity)} />
            </div>
            <p><strong>What is wrong.</strong> {p.what_is_wrong}</p>
            <p><strong>Why it matters.</strong> {p.why_it_matters}</p>
            <p><strong>How to detect.</strong> {p.how_to_detect}</p>
            {p.do_not_flag && <p><strong>Do NOT flag.</strong> {p.do_not_flag}</p>}
            {/* The profiler's own words, not the model's paraphrase of a number. This is what
                makes the proposal checkable rather than merely plausible. */}
            {p.observation_fact && (
              <p className="muted small">
                <strong>Measured:</strong> {p.observation_fact}
              </p>
            )}
            <div className="row-between">
              <input
                placeholder="Why are you refusing it? (shown to the Scout so it never asks again)"
                value={reason[p.hash] ?? ''}
                onChange={(e) => setReason({ ...reason, [p.hash]: e.target.value })}
              />
              <span>
                <button
                  className="primary"
                  disabled={!!working}
                  onClick={() => act(p.hash, () => api.acceptProposal(p.hash))}
                >
                  {working === p.hash ? 'Working…' : 'Accept'}
                </button>{' '}
                <button
                  disabled={!!working}
                  onClick={() => act(p.hash, () => api.rejectProposal(p.hash, reason[p.hash] ?? ''))}
                >
                  Reject
                </button>
              </span>
            </div>
          </div>
        ))}
        {(data?.dropped?.length ?? 0) > 0 && (
          <details>
            <summary className="muted">
              {data!.dropped.length} proposal(s) were filtered out before you saw them
            </summary>
            <ul className="muted small">
              {data!.dropped.map((d, i) => (
                <li key={i}><strong>{d.reason}:</strong> {d.title} — {d.detail}</li>
              ))}
            </ul>
          </details>
        )}
      </section>

      <section className="card">
        <h2>Accepted ({accepted.length})</h2>
        <p className="muted">
          A rule on trial runs and its findings are shown in their own section, but it counts
          towards nothing on the dashboard — not the score, not the flagged total — until you
          promote it. Compile after accepting to give it SQL.
        </p>
        {accepted.length === 0 && <p className="muted">Nothing accepted yet.</p>}
        {accepted.length > 0 && (
          <table>
            <thead>
              <tr><th>Rule</th><th>Anomaly</th><th>Status</th><th>Decided</th><th /></tr>
            </thead>
            <tbody>
              {accepted.map((r) => (
                <tr key={r.rule_id}>
                  <td className="mono">{r.rule_id}</td>
                  <td>{r.title}</td>
                  <td className={r.status === 'active' ? 'ok' : 'warnText'}>{r.status}</td>
                  <td className="muted small">{r.decided}</td>
                  <td>
                    {r.status === 'probation' && (
                      <button
                        disabled={!!working}
                        onClick={() => act(r.rule_id, () =>
                          api.setDiscoveredStatus(r.rule_id, 'active'))}
                      >
                        Promote
                      </button>
                    )}{' '}
                    <button
                      disabled={!!working}
                      onClick={() => act(r.rule_id, () =>
                        api.setDiscoveredStatus(r.rule_id, 'rejected', 'withdrawn after trial'))}
                    >
                      Reject
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <h2>Refused ({rejected.length})</h2>
        <p className="muted">
          Kept on record so the Scout cannot propose the same idea again. Restore one to put it
          back on trial.
        </p>
        {rejected.length === 0 && <p className="muted">Nothing refused yet.</p>}
        {rejected.length > 0 && (
          <table>
            <thead>
              <tr><th>Rule</th><th>Anomaly</th><th>Why it was refused</th><th /></tr>
            </thead>
            <tbody>
              {rejected.map((r) => (
                <tr key={r.rule_id}>
                  <td className="mono">{r.rule_id}</td>
                  <td>{r.title}</td>
                  <td className="muted">{r.reason || '—'}</td>
                  <td>
                    <button
                      disabled={!!working}
                      onClick={() => act(r.rule_id, () =>
                        api.setDiscoveredStatus(r.rule_id, 'probation'))}
                    >
                      Restore
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
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
            {/* THE RULE'S LIFECYCLE, shown beside the compiled state and not instead of it.
                They answer different questions - "is this check trusted?" versus "did its SQL
                build?" - and a rule on trial compiles perfectly well, so showing only the
                second made a probation rule read as `active` here while the Discover screen
                correctly called it `probation`. Two screens contradicting each other about
                whether a check counts is worse than either answer alone. */}
            <th>Status</th>
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
              <td className={r.status === 'active' ? 'ok' : 'warnText'}>
                {r.status}
                {r.status === 'probation' && (
                  <span className="muted small"> — not scored</span>
                )}
              </td>
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
  database,
  onOpen,
  onError,
}: {
  runs: RunRow[]
  database: DatabaseRef | undefined
  onOpen: (id: string) => void
  onError: (message: string) => void
}) {
  // THE TREND PLOTS ONE DATABASE, NEVER TWO.
  //
  // A trend line answers "is the data getting better or worse?", and that question only means
  // something if every point measured the same thing. Point DB_NAME at a different database and
  // the next run lands in the same series as the last: the chart draws a step between two
  // systems and the reader takes it for a change in quality. A run whose database was never
  // recorded is excluded for the same reason - it might be from either, and the honest answer
  // to "which?" is to leave it out of a comparison rather than to guess.
  //
  // Excluded from the CHART only. The table below lists every run there has ever been.
  const mine = runs.filter((r) => sameDatabase(r.database, database))
  const others = runs.length - mine.length

  return (
    <>
      <section className="card">
        <h2>Score over time — {database?.name || 'this database'}</h2>
        {mine.length > 0 ? (
          <ScoreTrend runs={mine} />
        ) : (
          <p className="muted">
            No run has been recorded against {database?.name || 'this database'} yet.
          </p>
        )}
        {others > 0 && (
          <p className="muted">
            {others} earlier run(s) are not plotted: they measured a different database, or were
            recorded before runs noted which database they measured. Mixing them into one line
            would show the change of database as a change in data quality.
          </p>
        )}
      </section>
      <section className="card">
        <h2>Run history</h2>
        <table>
          <thead>
            <tr>
              <th>Run</th>
              <th>When</th>
              <th>Database</th>
              <th className="num">Score</th>
              <th className="num">With findings</th>
              <th className="num">Flagged</th>
              <th className="num">Took</th>
              <th>Reports</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => {
              const current = sameDatabase(r.database, database)
              return (
                <tr key={r.run_id}>
                  <td className="mono clickable" onClick={() => onOpen(r.run_id)}>{r.run_id}</td>
                  <td>{r.started_at.slice(0, 19).replace('T', ' ')}</td>
                  <td className={current ? undefined : 'warnText'}>
                    {r.database?.name || 'not recorded'}
                  </td>
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
              )
            })}
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
  // Whether the last poll saw work in flight, so the moment it STOPS can be noticed once.
  const working = useRef(false)

  const refresh = useCallback(() => {
    api.status()
      .then((s) => {
        setStatus(s)
        working.current = s.busy || !!s.job
      })
      .catch((e) => setError(String(e)))
    api.runs().then((d) => setRuns(d.runs)).catch(() => setRuns([]))
    // A 404 here is the normal state before the first run against this database, not an error
    // worth shouting about.
    api.latestRun().then(setRun).catch(() => setRun(null))
  }, [])

  useEffect(refresh, [refresh])

  // ── Poll the server's status ───────────────────────────────────────────────
  //
  // BUSY IS A SERVER FACT AND IT CHANGES WITHOUT THIS BROWSER DOING ANYTHING. It was read once
  // on mount and then only when a job THIS TAB started ended - so a page opened during a
  // compile latched `busy` on for ever: both buttons greyed out, "another operation is in
  // progress" beside them, and no way back but a manual reload. Nothing was wrong with the
  // server; the page simply never asked again.
  //
  // Two rates, because the two situations want different things. While something is running the
  // bar has to move, so 3s. While nothing is, the only thing polling can discover is a compile
  // somebody started on the command line or in another tab, which is worth noticing within
  // half a minute and not worth re-reading the catalog for more often than that.
  const activity = !!status?.busy || !!status?.job

  // Discovery streams like a compile and shares the same server-side lock, so it is started
  // here beside the other long jobs rather than inside the tab: the tab is unmounted whenever
  // the user looks elsewhere, and a stream that dies on a tab change would leave the button
  // stuck and the proposals unwritten. Progress reaches the screen through the same polled
  // status the action bar already renders.
  const runDiscovery = useCallback(() => {
    setError('')
    stream('/api/discover', {
      onError: setError,
      onEnd: () => refresh(),
    })
  }, [refresh])

  useEffect(() => {
    const id = setInterval(async () => {
      try {
        const next = await api.status()
        setStatus(next)
        const busyNow = next.busy || !!next.job
        // Work just finished - the catalog counts, the run list and the latest run on screen
        // all describe the world before it ran.
        if (working.current && !busyNow) refresh()
        working.current = busyNow
      } catch {
        /* A failed poll is not worth a banner: the next one will either work or the user's own
           next action will surface the real error. */
      }
    }, activity ? 3000 : 30000)
    return () => clearInterval(id)
  }, [activity, refresh])

  // Runs from a database other than the one this server is now pointed at. Counted rather than
  // hidden: "no run yet" is misleading when there is a whole history sitting behind it that
  // simply measured something else.
  const foreignRuns = status?.runs_elsewhere ?? 0
  const runIsForeign = !!(run && status && !sameDatabase(run.database, status.database))

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
              probe(s)
              {/* Counted apart from the trusted probes, because they are exactly what the
                  score does NOT include - folding them into one number here would advertise a
                  coverage the headline figures do not actually have. */}
              {(status.catalog.on_trial ?? 0) > 0 && (
                <> · <span className="warnText">{status.catalog.on_trial} on trial</span></>
              )}{' '}
              · {status.models.main}
            </p>
          )}
        </div>
        <Actions
          onDone={refresh}
          busy={!!status?.busy}
          failed={status?.catalog.failed ?? 0}
          job={status?.job ?? null}
        />
      </header>

      {error && <p className="error banner">{error}</p>}
      {/* A run opened from the history may predate a change of DB_NAME. Every number below it
          then describes a different database, which nothing else on the page would reveal. */}
      {runIsForeign && (
        <p className="error banner">
          This run measured{' '}
          <strong>{run?.database?.name || 'a database that was not recorded'}</strong>, not{' '}
          <strong>{status?.database.name}</strong>. Its score and findings say nothing about the
          database this server is connected to now.
        </p>
      )}
      {run?.catalog_stale && <p className="error banner">{run.catalog_note}</p>}

      <nav>
        {(['dashboard', 'findings', 'discover', 'rules', 'runs'] as Tab[]).map((t) => (
          <button key={t} className={tab === t ? 'active' : ''} onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
      </nav>

      {tab === 'dashboard' && (
        <>
          {!run && <section className="card"><p className="muted">
            {foreignRuns > 0 ? (
              <>
                No run has been recorded against <strong>{status?.database.name}</strong> yet.
                The history holds {foreignRuns} run(s) from another database — they are kept,
                and shown on the <strong>runs</strong> tab, but none of them describes this one.
                Compile the rules, then press <strong>Run detection</strong>.
              </>
            ) : (
              <>No run yet. Compile the rules, then press <strong>Run detection</strong>.</>
            )}
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
          {/* ITS OWN CARD, never folded into the table above. These rules were proposed
              automatically and accepted only for trial; their counts are in no headline figure,
              so the section has to say so or a reader will act on a number nobody has vetted. */}
          {(run.discovered_ranked?.length ?? 0) > 0 && (
            <section className="card">
              <h2>Findings from rules on trial ({run.discovered_ranked!.length})</h2>
              <p className="muted">
                Proposed automatically and accepted for trial. <strong>Not</strong> included in
                the score or any figure on the dashboard — {n(run.discovered_totals?.records_flagged)}{' '}
                record(s) flagged of {n(run.discovered_totals?.records_examined)} examined. Use
                this to decide whether each check is worth promoting.
              </p>
              <table>
                <thead>
                  <tr>
                    <th>Rule</th><th>What was found</th><th>Severity</th>
                    <th className="num">Affected</th><th className="num">Of examined</th>
                    <th className="num">Share</th>
                  </tr>
                </thead>
                <tbody>
                  {run.discovered_ranked!.map((f) => (
                    <tr key={f.rule_id} className="clickable" onClick={() => setPicked(f.rule_id)}>
                      <td className="mono">{f.rule_id}</td>
                      <td>{f.title}</td>
                      <td><Sev value={f.severity} /></td>
                      <td className="num">{n(f.anomaly_count)}</td>
                      <td className="num">{n(f.scope_total)}</td>
                      <td className="num">{f.anomaly_pct.toFixed(2)}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}
          <Gaps run={run} />
        </>
      )}
      {tab === 'findings' && !run && (
        <section className="card"><p className="muted">No run to show yet.</p></section>
      )}

      {tab === 'discover' && (
        <DiscoverTab busy={activity} onRun={() => runDiscovery()} />
      )}
      {tab === 'rules' && <RulesTab onPick={setPicked} />}
      {tab === 'runs' && (
        <RunsTab
          runs={runs}
          database={status?.database}
          onOpen={openRun}
          onError={setError}
        />
      )}

      {picked && <RuleDrawer id={picked} onClose={() => setPicked('')} />}
    </div>
  )
}
