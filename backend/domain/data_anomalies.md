# DATA ANOMALIES

Every data-quality problem this engine looks for is described here, in plain business language.

To add a check, copy an existing one and describe the problem in your own words. To switch one
off, change its status to `disabled`. You never need to write SQL, and you never need to know a
table or column name — the AI reads the live database and the business rules and works it out.

Each entry has four parts:

- **What is wrong** — the problem, in one or two sentences.
- **Why it matters** — the consequence, so the severity can be justified.
- **How to detect** — the logic in words. Which business facts to compare, and how.
- **Do NOT flag** — the cases that are legitimate, or already reported elsewhere.

The heading code (`DQ-A01`) is how the engine tracks the check across runs and labels it in the
report. It is not something you need to refer to when writing.

Status: `active` runs. `draft` is listed but never run — use it when the business has not yet
agreed the definition, so the gap stays visible instead of being quietly dropped. `disabled` is
switched off deliberately.

---

# GROUP A — Well milestone dates

## RULE DQ-A01 - Master expected rig-on date is missing

- category: Milestone dates
- severity: critical
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, planning

Wrong: A well has no expected rig-on date recorded.
Matters: This is the master date the whole schedule is measured against. Every construction
  deadline, the pegging deadline and the flowline approval deadline are worked out from it. A
  well without it cannot be assessed for lateness at all — it silently disappears from every
  deadline check instead of appearing as a problem.
Detect: Examine every well. Flag the well when the expected rig-on date is absent.
Never flag: Nothing. There is no legitimate state in which a planned well lacks this date.
---

## RULE DQ-A02 - Expected rig-off date is missing

- category: Milestone dates
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, planning

Wrong: A well has no expected rig-off date recorded.
Matters: The hook-up deadline is worked out from it until drilling actually finishes. Without
  it, hook-up cannot be judged on time.
Detect: Examine every well. Flag the well when the expected rig-off date is absent.
Never flag: Wells where drilling has already finished. From that point the actual date takes
  over for every deadline, so the missing plan no longer blocks anything.
---

## RULE DQ-A03 - Rig arrived but no rig-on date was ever planned

- category: Milestone dates
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, planning

Wrong: The rig is recorded as having arrived, but no expected arrival date was ever set.
Matters: The event happened but was never planned, so no variance can be worked out and the well
  is invisible to schedule reporting. It also means every deadline derived from the master date
  was never enforced for this well.
Detect: Look only at wells where the rig has actually arrived. Flag those with no expected
  arrival date.
Never flag: Wells where the rig has not arrived. A missing plan on those is a different check.
---

## RULE DQ-A04 - Drilling finished but no rig-off date was ever planned

- category: Milestone dates
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, planning

Wrong: Drilling is recorded as finished, but no expected finish date was ever set.
Matters: Drilling performance for this well cannot be measured against any plan.
Detect: Look only at wells where drilling has actually finished. Flag those with no expected
  finish date.
Never flag: Wells where drilling has not finished.
---

## RULE DQ-A05 - Rig arrived after drilling was supposed to be finished

- category: Milestone dates
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, lifecycle

Wrong: The rig actually arrived later than the date drilling was planned to be complete.
Matters: The planned window has been overtaken by events. Any forecast still built on the
  planned finish is meaningless for this well.
Detect: Look at wells where both the actual arrival and the planned finish are known. Flag those
  where arrival came after the planned finish.
Never flag: Wells missing either date — those are separate checks above.
---

## RULE DQ-A06 - Rig came off the well before it was due to arrive

- category: Milestone dates
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, rig-off, lifecycle

Wrong: The actual **rig-off** date — the date drilling finished and the rig left — falls before
  the expected **rig-on** date, so the rig is recorded as leaving before it was due to arrive.
Matters: This is very unlikely to be a real sequence and much more likely a mistyped year, or a
  date written into the wrong place. Left alone it makes the well appear finished far ahead of
  plan and distorts every completion statistic it feeds.
Detect: Look at wells where both the actual rig-off date and the expected rig-on date are known.
  Flag those where rig-off falls before the expected rig-on.
Never flag: A well that is genuinely ahead of schedule by a normal margin. This check is about
  the impossible ordering, not about being early.
---

## RULE DQ-A07 - Hook-up completed before the rig arrived

- category: Milestone dates
- severity: critical
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, lifecycle, completion

Wrong: The well is recorded as handed over before the rig arrived on it.
Matters: Hook-up is the last step in the well's life and cannot come before the rig arrives. A
  well marked complete before it was drilled corrupts every completion count and every progress
  total that reads it.
Detect: Look at wells where both the completion and the rig arrival are known. Flag those
  completed before the rig arrived.
Never flag: Wells with no completion recorded. An unfinished well is not an anomaly.
---

## RULE DQ-A08 - Hook-up completed before drilling finished

- category: Milestone dates
- severity: critical
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, lifecycle, completion

Wrong: The well is recorded as handed over before drilling finished on it.
Matters: Hook-up is handed over only after the rig is off and the well has been cleaned.
  Completing before that is impossible and points to a wrong date.
Detect: Look at wells where both the completion and the drilling finish are known. Flag those
  completed before drilling finished.
Never flag: Wells missing either date.
---

## RULE DQ-A09 - Well completed while drilling is not recorded as finished

- category: Milestone dates
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: draft
- tags: milestone, lifecycle, undefined

Wrong: The well is recorded as handed over, but drilling was never recorded as finished at all.
Matters: Either the drilling finish was never captured, or the completion is wrong. Both distort
  the well's history.
Detect: Look at wells with a completion recorded. Flag those where drilling has no recorded
  finish. **Draft — not yet agreed.** It is not established whether a completion may
  legitimately be recorded before the drilling finish is captured, for example where drilling is
  tracked in a separate system. Make this active once the business confirms it.
---

## RULE DQ-A10 - Well is not linked to a project

- category: Reference integrity
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: reference, project

Wrong: A well has no project recorded against it.
Matters: Every well belongs to a construction project. A well with no project cannot be counted
  into any project total, so project progress is worked out from an incomplete set of wells with
  nothing indicating the gap.
Detect: Examine every well. Flag those where the project is absent or blank.
Never flag: Wells where a project is recorded but points at something that does not exist. That
  is a broken link and is already covered by the structural checks in Group G.
---

## RULE DQ-A11 - Well is not linked to a cluster

- category: Reference integrity
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: reference, cluster

Wrong: A well has no cluster recorded against it.
Matters: Cluster is the geographic grouping every regional report adds up by. A well without one
  is left out of cluster reporting silently.
Detect: Examine every well. Flag those where the cluster is absent.
Never flag: A cluster that is recorded but invalid — already covered by the structural checks in
  Group G.
---

## RULE DQ-A12 - A date for something that already happened is in the future

- category: Milestone dates
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- placeholder_date: 1900-01-01
- tags: milestone, dates, impossible-value

Wrong: A well date that records something which has **already happened** is later than today.
Matters: Usually a typo in the year. It makes completed work look outstanding, or pushes a
  milestone years into the future, and every deadline worked out from that date inherits the
  error silently.
Detect: Use only the well dates the business rules explicitly classify as recording an outcome.
  Flag a well where any of those is later than the database's own current date, so the check
  uses the same clock the data was written against. Report which date is at fault.
Never flag: Any date the business rules classify as planned or expected — being in the future is
  exactly what those are for. Any date the business rules do not classify at all: whether it
  records an outcome or an intention has not been established, and assuming it is an outcome is
  how a schedule date comes to be reported as thousands of defects. Dates holding the known
  placeholder value, which is a separate check. **A date is covered here only once the business
  rules say it records an outcome.** Several well dates are not yet classified and are therefore
  not checked. Classify them and they are covered immediately, with no change to this
  description and no change to any code.
---

## RULE DQ-A13 - Rig left the well before it arrived

- category: Milestone dates
- severity: critical
- entity: well
- method: rule
- sql_mode: authored
- status: disabled
- tags: milestone, rig, rig-off, lifecycle

Wrong: The date the rig actually left the well falls before the date it actually arrived. Both
  of these record what happened, not what was planned.
Matters: A rig cannot leave a well before it arrives, so one of the two dates is wrong. Drilling
  is measured between exactly these two events, so the well reports a negative drilling period
  and every duration, variance and lifecycle figure built on it is wrong by the size of the
  inversion.
Detect: Look at wells where both actual dates are known. Flag those where the rig left before it
  arrived. Severity is the size of the inversion in days.
Never flag: Wells missing either actual date — a well that has not been drilled yet is not an
  inversion. Being ahead of the expected schedule is not an anomaly: this check is about two
  recorded facts contradicting each other, not about earliness. **Disabled — this exact pair of
  dates is already checked**, by the structural check in Group G that compares every start/end
  date pair the database holds. Switching this on as well would report the same wells twice and
  inflate the flagged-record count, which is the one error a data-quality report cannot afford
  to make. It is kept here, switched off, so the business concern is on record rather than lost,
  and so the decision can be revisited: change the status to `active` the moment the structural
  check stops covering it. One thing is lost by disabling this rather than the structural check,
  and it is recorded so the trade is visible — this description carries the business severity
  and the business explanation, and the structural check reports the same wells in generic
  terms. The earlier check in this group that compares the actual rig-off against the
  **expected** rig-on is a genuinely different question and stays active. That one asks whether
  a well finished before it was due to start; this one asks whether two recorded facts
  contradict each other.
---

## RULE DQ-A14 - Rig left the well but was never recorded as arriving

- category: Milestone dates
- severity: critical
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, rig-on, rig-off, lifecycle, completeness

Wrong: A well has an actual rig-off date recorded while its actual rig-on date is absent.
Matters: Drilling is measured from the rig arriving to the rig leaving, so a well with only the
  second of those two facts has no measurable drilling period at all. It does not appear as a
  bad number — it drops out of every duration and variance figure silently, and the well reads
  as if drilling never happened on it.
Detect: Look at wells where the actual rig-off date is present. Flag those where the actual
  rig-on date is absent.
Never flag: Wells with no actual rig-off recorded — a well that has not finished drilling is not
  expected to show an arrival for this purpose. Wells holding both actual dates, whether or not
  they ran early or late against the plan: earliness is not an anomaly and a contradiction
  between the two dates is a different check in this group.
---

## RULE DQ-A15 - Active well has no task activities

- category: Structural completeness
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: well, task, traceability, coverage

Wrong: A well that has not been completed has no task records that can be traced to it.
Matters: Progress, delay and resource analysis for a live well is built entirely from its task
  records. A well with none of them is not reported as having a problem — it is absent from the
  analysis altogether, so nobody looking at task-level reporting can tell it is being missed.
Detect: Look only at wells that are not yet completed. Flag a well where no task record can be
  traced to it at all. Report **one row per well**.
Never flag: Completed wells — their work is finished and their absence from live task analysis
  is expected. A well whose tasks exist but where one individual task fails to resolve: that is
  a task-level traceability problem and is checked separately.
---

## RULE DQ-A16 - Well has no usable location

- category: Reference integrity
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: draft
- tags: reference, location, well

Wrong: A well has no location recorded, or the location it records cannot be traced to the
  authoritative source of locations.
Matters: Location identifies where the work is physically happening, and anything reported by
  area, cluster or site silently omits a well that has none.
Detect: Look at wells and check that the location is both present and resolves against whichever
  source the business names as authoritative for locations.
Never flag: Wells the business has confirmed do not require a location. **Draft — there is no
  agreed authoritative source of locations yet.** More than one place in this database holds
  something location-shaped and they do not agree; one of them is empty. Activating this against
  the wrong one would report every well as defective, or none. Name the authoritative source in
  the business rules and change the status to `active`; nothing else here needs to change.
---

# GROUP B — The pegging sheet

## RULE DQ-B01 - Pegging sheet missed its deadline

- category: Milestone deadlines
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- deadline_days: 60
- tags: pegging, deadline, PDO

Wrong: The pegging sheet was not issued by its deadline, which is 60 days before the rig is due
  on the well.
Matters: Pegging is the client's input and location construction cannot start without it. A late
  or absent pegging sheet pushes the whole construction sequence toward the rig date.
Detect: The deadline is 60 days before the expected rig-on date. A well has missed it in either
  of two ways, and both must be counted: 1. the pegging was issued, but after the deadline; or
  2. the pegging is missing and the deadline has already passed. Look at every well with an
  expected rig-on date. Severity is how many days late — for a missing pegging, measure from the
  deadline to today. **The missing case must be counted, not filtered out.** A missing pegging
  past its deadline is a miss. Excluding blanks would silently remove exactly the worst
  offenders.
Never flag: Wells whose deadline has not yet arrived and whose pegging is still outstanding.
  They are not late, they are simply not due.
---

## RULE DQ-B02 - Pegging sheet issued after the rig was due on the well

- category: Milestone deadlines
- severity: critical
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: pegging, deadline, PDO

Wrong: The pegging sheet was issued after the date the rig was due — not merely past its own
  deadline, but past the date construction was supposed to be finished.
Matters: A far more extreme failure than ordinary lateness: the input that must come before all
  construction arrived after construction should have been complete. It is kept separate so it
  cannot be lost among ordinary late-pegging findings.
Detect: Look at wells where both the pegging and the expected rig-on date are known. Flag those
  where pegging came after the rig was due. Severity is the number of days between them.
Never flag: Wells merely past the 60-day deadline but still before the rig was due — those
  belong to the ordinary late-pegging check.
---

## RULE DQ-B03 - Pegging recorded but no deadline can be worked out

- category: Milestone deadlines
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: pegging, deadline, planning

Wrong: A pegging sheet exists but the expected rig-on date is missing, so its deadline cannot be
  worked out and the milestone can never be judged on time.
Matters: The well looks compliant because nothing can prove it is not. It escapes the lateness
  check through the absence of the very date that check depends on.
Detect: Look at wells with a pegging sheet. Flag those with no expected rig-on date.
Never flag: Wells with no pegging sheet at all — covered by other checks.
---

## RULE DQ-B04 - Pegging issued unusually far ahead of the rig date

- category: Milestone deadlines
- severity: low
- entity: well
- method: statistical
- sql_mode: authored
- status: draft
- tolerance: auto
- tags: pegging, undefined

Wrong: The pegging sheet was issued very much earlier than required.
Matters: Being early is normally good and is explicitly not an anomaly. But an extreme outlier
  may mean a wrong year in the date rather than genuine early delivery.
Detect: Compare the gap between pegging and the expected rig-on date across all wells, and flag
  only extreme outliers, with the threshold worked out from the spread of the wells themselves —
  never a fixed number of days. **Draft — no agreed threshold.** The business has not defined
  how early is too early. Until it does, this stays off rather than inventing a limit.
---

# GROUP C — The flowline approval

## RULE DQ-C01 - Flowline approval missed its deadline

- category: Milestone deadlines
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- deadline_days: 90
- tags: flaf, deadline, PDO

Wrong: The flowline approval was not issued by its deadline, which is 90 days before the rig is
  due on the well.
Matters: It is the client's input for flowline construction. Late or absent approval delays
  flowline work — and the resulting delay is not charged to us, so recording it correctly
  matters commercially as well as operationally.
Detect: The deadline is 90 days before the expected rig-on date. A well has missed it in either
  of two ways, and both must be counted: 1. the approval was issued, but after the deadline; or
  2. the approval is missing and the deadline has already passed. Look at every well with an
  expected rig-on date. Severity is how many days late — for a missing approval, measure from
  the deadline to today.
**The missing case must be counted, not filtered out.**
Never flag: Wells whose deadline has not yet arrived and whose approval is still outstanding.
---

## RULE DQ-C02 - Flowline approval issued after the rig was due on the well

- category: Milestone deadlines
- severity: critical
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: flaf, deadline, PDO

Wrong: The **FLAF** — the flowline approval the client issues — was dated after the expected
  rig-on date, instead of the 90 days before it that the deadline requires.
Matters: The input required three months before the rig arrived instead came after it was due.
  Kept separate so this extreme case is visible on its own.
Detect: Look at wells where both the FLAF issue date and the expected rig-on date are known.
  Flag those where the FLAF was issued after the expected rig-on date. Severity is the days
  between.
Never flag: Wells merely past the 90-day deadline but still before the rig was due.
---

## RULE DQ-C03 - Flowline approval recorded but no deadline can be worked out

- category: Milestone deadlines
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: flaf, deadline, planning

Wrong: A flowline approval exists but the expected rig-on date is missing, so its deadline
  cannot be worked out.
Matters: The milestone can never be judged, and the well silently escapes the lateness check.
Detect: Look at wells with a flowline approval. Flag those with no expected rig-on date.
Never flag: Wells with no approval at all.
---

## RULE DQ-C04 - Flowline approval issued after the pegging sheet

- category: Milestone deadlines
- severity: low
- entity: well
- method: rule
- sql_mode: authored
- status: draft
- tags: flaf, pegging, undefined

Wrong: The flowline approval came later than the pegging sheet, although its deadline is the
  earlier of the two.
Matters: The two deadlines imply the approval should normally come first. Whether the reverse
  order is actually a defect is a business question, not a logical one — both are client inputs
  and may legitimately arrive in either order.
Detect: Look at wells where both are known. Flag those where the approval came after the pegging
  sheet. **Draft — needs business confirmation.** The two deadlines are agreed, but nothing says
  the order between them is mandatory. Do not activate until confirmed.
---

# GROUP D — Task execution and progress

## RULE DQ-D01 - Task planned to finish before it starts

- category: Task integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: disabled
- tags: task, dates, planning

Wrong: A task's planned start is after its planned finish.
Matters: Every planned duration worked out from the pair is negative, which quietly corrupts
  averages and forecasts rather than failing visibly.
Detect: Look at tasks where both planned dates are known. Flag those starting after they finish.
  Severity is the size of the inversion in days.
Never flag: Tasks missing either planned date. **Disabled — this was being reported twice.** The
  structural checks in Group G already compare every start/end date pair in the database,
  including this one, and both found the same records. Two entries over one set of records
  inflates the totals in the report. Coverage is unchanged; only the duplicate reporting is
  gone.
---

## RULE DQ-D02 - Task finished before it started

- category: Task integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: disabled
- placeholder_date: 1900-01-01
- tags: task, dates, execution

Wrong: A task's actual start is after its actual finish.
Matters: Work cannot finish before it begins. Every duration and productivity figure worked out
  from the pair is wrong.
Detect: Look at tasks where both actual dates are known. Flag those starting after they finish.
  Severity is the size of the inversion in days.
Never flag: Tasks where either date is the known placeholder value. **Disabled — this was being
  reported twice**, same as the check above. One thing is lost by disabling this rather than the
  structural check, and it is recorded so the decision can be revisited: this description
  excluded the placeholder date from its scope and the structural check does not.
---

## RULE DQ-D03 - Task shows full progress but is not marked complete

- category: Task integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, progress, completion

Wrong: A task's progress has reached its maximum while the task is still marked incomplete.
Matters: A direct contradiction between two things that must agree. Whichever is wrong, every
  total that counts completed tasks disagrees with every total that adds up progress — and the
  two appear in different reports.
Detect: Judge each task at its current state. This data keeps a history, so the most recent
  record for each task must be chosen before judging it. Flag a task at full progress that is
  not marked complete. Check the progress scale before comparing. Progress may be held as a
  fraction between nought and one, or as a percentage out of a hundred, and comparing against
  the wrong one flags either every task or none.
Never flag: Tasks below full progress.
---

## RULE DQ-D04 - Task marked complete with no finish date

- category: Task integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, completion, dates

Wrong: A task is marked complete but no finish date was recorded.
Matters: The task counts as done in every completion total, yet contributes nothing to duration
  or productivity analysis — complete and unmeasurable at the same time.
Detect: Take the most recent record for each task. Flag those marked complete with no finish
  date.
Never flag: Tasks not marked complete.
---

## RULE DQ-D05 - Task has a finish date but is marked incomplete

- category: Task integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- placeholder_date: 1900-01-01
- tags: task, completion, dates

Wrong: A task has a finish date recorded while still being marked incomplete — the opposite
  contradiction to the check above.
Matters: The work appears to have finished but is still counted as outstanding, overstating
  remaining work in every forecast.
Detect: Take the most recent record for each task. Flag those with a finish date that are not
  marked complete.
Never flag: Tasks whose finish date is the known placeholder value.
---

## RULE DQ-D06 - Placeholder date used as a real execution date

- category: Task integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- placeholder_date: 1900-01-01
- tags: task, dates, placeholder

Wrong: A default placeholder date has been stored where a real execution date belongs.
Matters: This is a known pattern in this data. It is neither a missing value nor a real date: it
  passes every "is it blank" check while being decades wrong, so it silently poisons any
  duration worked out from it. Every such record must be treated as unusable rather than as a
  real date.
Detect: Examine task records. Flag any whose start or finish equals the placeholder date. The
  date itself is a stated convention, so using it directly is correct here.
Never flag: Genuinely missing dates. Missing and placeholder are different problems, and
  treating them as one hides which needs fixing.
---

## RULE DQ-D07 - Quantity delivered with no hours recorded

- category: Productivity data
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, productivity, quantity

Wrong: A task records work delivered while recording no hours spent.
Matters: Productivity is work per hour. A record with work and no hours makes productivity
  infinite, distorting any crew or activity average that includes it.
Detect: Look at task records where both the quantity and the hours are recorded. Flag those with
  a quantity above zero and hours at zero.
Never flag: Records where hours are missing rather than zero. Unrecorded is not the same as
  none.
---

## RULE DQ-D08 - Hours recorded with no quantity delivered

- category: Productivity data
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, productivity, hours

Wrong: A task records hours spent while recording no work delivered.
Matters: This may be legitimate non-productive time, or it may be a missing quantity. It should
  be put in front of a person to decide rather than silently averaged into productivity as a
  zero.
Detect: Look at task records where both figures are recorded. Flag those with hours above zero
  and quantity at zero.
Never flag: Records where the quantity is missing rather than zero.
---

## RULE DQ-D09 - Work remains but no time is left to do it

- category: Task integrity
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, forecast

Wrong: A task still has outstanding work while the time remaining for it has reached zero.
Matters: A contradiction in the forecast: work is left but no time is allowed for it, so the
  forecast completion date for this task cannot be met.
Detect: Take the most recent record for each task. Flag those with outstanding work above zero
  and no time remaining.
Never flag: Tasks already marked complete.
---

## RULE DQ-D10 - Unfinished task has no plan

- category: Task integrity
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, planning, dates

Wrong: A task that is not yet complete has no planned start and/or no planned finish.
Matters: Schedule risk and slippage cannot be worked out for a task with no plan to measure
  against. The task is invisible to every forward-looking analysis.
Detect: Look at tasks not marked complete. Flag those missing either planned date.
Never flag: Completed tasks. A plan is no longer needed once the work is done.
---

## RULE DQ-D11 - Conflicting current records for the same task

- category: Task grain
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, grain, history

Wrong: The same task has more than one record that would each be read as the current one, and
  they disagree about progress, completion, dates or crew.
Matters: The problem is not that a history exists — this data legitimately keeps one record per
  update, and the most recent should be used. The problem is that two records cannot be put in
  order into a single current state, so any analysis picks one arbitrarily and two reports
  reading the same data disagree.
Detect: Group task records by the well and the task they belong to. Within each group find the
  records that tie for most recent. Flag a group where more than one record ties and those tied
  records disagree on progress, completion or dates.
Never flag: A task with many historical records that resolve cleanly to one most recent record.
  That is normal and correct.
---

## RULE DQ-D12 - Task cannot be traced to an activity

- category: Reference integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, mapping, activity

Wrong: A task cannot be traced through to a known activity, so it cannot be classified.
Matters: An unclassified task belongs to no activity and therefore to no part of the work
  breakdown, so it counts toward nothing. It silently disappears from progress reporting rather
  than showing up as a gap.
Detect: Follow the activity mapping the business rules describe. Flag tasks whose activity
  cannot be worked out, or resolves to nothing. Keep unmatched tasks visible: the lookup must
  preserve them rather than dropping them, or the count of unmapped tasks reads zero for exactly
  the wrong reason.
Never flag: Tasks that trace successfully.
---

## RULE DQ-D13 - Activity does not belong to any part of the work breakdown

- category: Reference integrity
- severity: high
- entity: activity
- method: rule
- sql_mode: authored
- status: active
- tags: mapping, wbs

Wrong: An activity can be traced, but it belongs to no group in the work breakdown structure.
Matters: The work breakdown is how progress rolls up to the project. An activity outside it
  contributes to nothing, and the project percentage is worked out from an incomplete base.
Detect: Trace activities to their work-breakdown group as the business rules describe, taking
  care to use the code the mapping is actually keyed on. Flag activities with no group. Report
  the unmapped tally beside the group counts, never inside them. Counting "unmapped" as if it
  were a group of its own adds a phantom to every breakdown.
Never flag: Activities that have a group.
---

## RULE DQ-D14 - Task took far longer or far shorter than the published norm

- category: Schedule integrity
- severity: medium
- entity: task
- method: statistical
- sql_mode: authored
- status: active
- tolerance: auto
- min_sample: 30
- tags: schedule, norms, duration

Wrong: A task took far longer, or far less time, than the published norm for that kind of work —
  well beyond the variation tasks normally show. Both directions matter: over the norm is an
  overrun, under the norm is usually a data-entry error such as a finish date copied from a
  start date.
Matters: Norms drive planning. A task recorded at a fraction of its norm inflates apparent
  productivity and corrupts every forward estimate built on it.
Detect: Trace each task to its activity and to the norm published for that activity, then
  compare the number of days it actually took. The threshold must calibrate itself from the
  data. For each activity, look at how much its tasks normally deviate from the norm, and flag
  only the tasks that sit far outside that activity's own normal spread. Require at least the
  stated minimum number of observed tasks for an activity before judging any of them, so a
  rarely-run activity is not judged from a handful of records. Do not pick a multiplier or a
  percentage yourself; derive the cut-off from the observed spread. More than one place in this
  database publishes norms, and they do not all agree. Choose the one with the most complete,
  genuinely numeric coverage, and say in the threshold note which was used.
Never flag: Tasks with a missing or placeholder start or finish. Norms that are not usable
  numbers — that is the next check.
---

## RULE DQ-D15 - Published norm is not a usable number

- category: Reference data
- severity: medium
- entity: activity
- method: rule
- sql_mode: authored
- status: active
- tags: norms, reference

Wrong: A published norm is stored as text and cannot be read as a number.
Matters: A norm that cannot be read is not a loose check — it is no check. Those activities
  vanish from the duration comparison without appearing anywhere as a gap, because the
  unreadable value quietly becomes a blank and the record is dropped.
Detect: Examine the published norms. Flag any non-blank value that cannot be read as a number.
Never flag: Norms that are genuinely absent. "Not published yet" is a different thing from
  "published but unreadable", and treating them as one hides the one that is actually a defect.
---

## RULE DQ-D16 - Task crew cannot be traced

- category: Reference integrity
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: crew, mapping

Wrong: A task names a crew that cannot be traced to a real crew.
Matters: Crew productivity and utilisation are reported per crew. An untraceable crew means the
  work is counted in the totals but attributed to nobody.
Detect: Look at tasks that name a crew. Flag those whose crew cannot be traced.
Never flag: Tasks with no crew named at all. Unassigned is a different condition from
  misassigned.
---

## RULE DQ-D17 - Employee is not linked to a crew

- category: Reference integrity
- severity: low
- entity: employee
- method: rule
- sql_mode: authored
- status: active
- tags: crew, employee, mapping

Wrong: An employee-to-crew assignment does not trace to both a real employee and a real crew.
Matters: Manpower reporting rolls up through the crew relationship. An employee outside it is
  invisible to those totals.
Detect: Examine the employee-to-crew assignments and flag those that do not trace to both.
Never flag: Employees legitimately not assigned to any crew.
---

## RULE DQ-D18 - Task started after it was due to finish

- category: Task integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- placeholder_date: 1900-01-01
- tags: task, dates, plan-vs-actual

Wrong: A task's **actual start** is later than its **target finish** — work began after the date
  the plan said it should already have been completed.
Matters: The task was never going to meet its plan, and the schedule showed no warning of it.
  Both dates are individually valid, so nothing catches this unless the plan and the outcome are
  compared against each other.
Detect: Look at tasks where both the actual start date and the target finish date are recorded.
  Flag those where the actual start falls after the target finish. Take the most recent record
  for each task before judging it. Severity is the size of the overrun in days.
Never flag: Tasks missing either date, or where either is the known placeholder value.
---

## RULE DQ-D19 - Task finished before it was due to start

- category: Task integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- placeholder_date: 1900-01-01
- tags: task, dates, plan-vs-actual

Wrong: Work was recorded as complete before the plan said it should even begin.
Matters: Either the work was logged against the wrong task, or the plan was written after the
  fact. Both make the schedule a record of neither intention nor outcome.
Detect: Look at tasks where both the actual finish and the planned start are known. Flag those
  finishing before they were due to start. Severity is the size of the gap in days.
Never flag: Tasks missing either date, or carrying the placeholder value.
---

## RULE DQ-D20 - Progress recorded but the task never started

- category: Task integrity
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, progress, execution

Wrong: A task shows progress above zero while having no start date. Work has demonstrably begun,
  yet nothing records when.
Matters: Every duration, productivity and delay figure for the task is worked out from its
  start. Without one the task is progressing but cannot be measured, and it is invisible to any
  report built on execution dates.
Detect: Take the most recent record for each task. Flag those with progress above zero and no
  start date. Check the progress scale before comparing — it may be a fraction rather than a
  percentage.
Never flag: Tasks at zero progress. Not started is a legitimate state, not a defect. Tasks with
  no progress recorded at all — a missing measure is a different finding from a contradicted
  one.
---

## RULE DQ-D21 - Task finished but reports no progress

- category: Task integrity
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- placeholder_date: 1900-01-01
- tags: task, progress, execution

Wrong: A task has a finish date while its progress is still zero — the opposite contradiction to
  the check above.
Matters: A finished task reporting no progress is counted as outstanding in every total, so
  completed work is reported as remaining and the overall figure understates what has been
  achieved.
Detect: Take the most recent record for each task. Flag those with a finish date and zero
  progress. Check the progress scale before comparing.
Never flag: Records whose finish date is the known placeholder value. Tasks with no progress
  recorded at all.
---

## RULE DQ-D22 - Task marked complete while progress is below full

- category: Task integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, progress, completion

Wrong: A task is marked complete while its progress has not reached its maximum.
Matters: Completion counts and progress totals then disagree about the same task. One says the
  work is done, the other says it is not, and a report built on either alone is confidently
  wrong.
Detect: Take the most recent record for each task. Flag tasks marked complete whose progress is
  below full. Work out what "full" means from the measured scale of the progress figure before
  comparing — using the wrong one silently flags or clears every task.
Never flag: Tasks with no progress recorded at all. A missing measure cannot contradict the
  mark, and it is a different finding.
---

## RULE DQ-D23 - Negative time remaining

- category: Value integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, duration, impossible-value

Wrong: The time left to complete a task is recorded as a negative number. Time left cannot be
  less than none.
Matters: A negative value does not merely misreport its own task: it subtracts from any total
  that adds up remaining time, so it understates the outstanding work of every group it belongs
  to. The error spreads silently into figures that look perfectly reasonable.
Detect: Flag task records where the time remaining is below zero. No threshold — negative is
  impossible, not merely unusual.
Never flag: Records where no figure is recorded. Zero is legitimate and is not negative.
---

## RULE DQ-D24 - Negative hours

- category: Value integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, hours, impossible-value

Wrong: A task record holds a negative figure for hours worked. Work performed cannot be less
  than none.
Matters: Manpower totals and every productivity ratio worked out from hours are reduced by the
  negative value, so the reported effort is lower than the effort actually spent.
Detect: Flag task records where the hours figure is below zero. No threshold.
Never flag: Records where no hours are recorded. Zero hours is legitimate — a quantity delivered
  against zero hours is a separate check.
---

## RULE DQ-D25 - Negative quantity delivered

- category: Value integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, quantity, impossible-value

Wrong: A task record holds a negative quantity delivered. Work delivered cannot be less than
  none.
Matters: Quantity delivered drives progress and productivity. A negative figure reduces the
  totals it feeds, so delivered work is understated wherever it is added up.
Detect: Flag task records where the quantity delivered is below zero. No threshold.
Never flag: Records where no quantity is recorded. Zero is legitimate — hours against zero
  quantity is a separate check.
---

## RULE DQ-D26 - Negative quantity outstanding

- category: Value integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: draft
- tags: task, quantity, impossible-value

Wrong: The quantity still outstanding on a task is recorded as a negative number.
Matters: Outstanding work cannot be less than none, and a negative value subtracts from every
  total that adds up remaining work — understating what is still to be done.
Detect: Flag task records where the outstanding quantity is below zero.
Never flag: Records where no figure is recorded. Zero is legitimate. **Draft — the figure has
  not been formally identified.** Several numbers held against a task could be the outstanding
  quantity, and the business rules do not yet say which one it is. Guessing would produce a
  check that runs, returns a confident number, and measures the wrong thing. Name it in the
  business rules, then make this active.
---

## RULE DQ-D27 - Task went backwards from complete to incomplete

- category: Task history
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, history, completion, regression

Wrong: A task was recorded as complete at some point, and a later record for the same task shows
  it as incomplete again.
Matters: Completion should not move backwards. Every total that counts completed work disagrees
  with every total that counts outstanding work, depending on which record was read — and the
  two appear in different reports, so neither reader can see the contradiction.
Detect: This check needs the HISTORY, not the current record. Put each task's records in time
  order and flag a task where a record showing it complete is followed by a later record showing
  it incomplete. Report **one row per task**, not one per record — the finding is the task that
  regressed, and the evidence is when it was complete and when it went back.
Never flag: Tasks never recorded as complete. A task that has simply never finished is not a
  regression.
---

## RULE DQ-D28 - Task start date was removed after being recorded

- category: Task history
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- placeholder_date: 1900-01-01
- tags: task, history, dates, regression

Wrong: A task had an actual start date recorded, and a later record for the same task has none.
Matters: The execution history has gone backwards: a task known to have started becomes one that
  apparently never did. Every duration and progress figure worked out from the start date
  changes retrospectively, and nothing indicates that it did.
Detect: Put each task's records in time order and flag a task where an actual start date is
  present in one record and absent in a later one. Report **one row per task**, with both the
  date that was recorded and when it disappeared.
Never flag: Tasks that have never had an actual start date. Records where the date is the known
  placeholder value rather than a real one.
---

## RULE DQ-D29 - Task finish date was removed after being recorded

- category: Task history
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- placeholder_date: 1900-01-01
- tags: task, history, dates, regression

Wrong: A task had an actual finish date recorded, and a later record for the same task has none.
Matters: A completion that was recorded has been removed. Work that was finished reads as
  outstanding again, and historical productivity and schedule figures change after the fact —
  which is worse than a missing date, because the earlier reports cannot be reconciled with the
  later ones.
Detect: Put each task's records in time order and flag a task where an actual finish date is
  present in one record and absent in a later one. Report **one row per task**, with the finish
  date that was recorded and when it disappeared.
Never flag: Tasks that have never had an actual finish date. Records carrying the known
  placeholder value.
---

## RULE DQ-D30 - Activity belongs to more than one part of the work breakdown

- category: Reference integrity
- severity: high
- entity: activity
- method: rule
- sql_mode: authored
- status: active
- tags: mapping, wbs, activity

Wrong: One activity traces through to more than one work-breakdown group.
Matters: An activity must sit in exactly one place in the breakdown for progress to roll up
  correctly. Mapped to two, its work is either counted twice or attributed to whichever group a
  particular query happened to pick — so two reports on the same data disagree and neither is
  wrong on its own terms.
Detect: Trace each activity to its work-breakdown group as the business rules describe, taking
  care to use the code the mapping is keyed on. Flag an activity that resolves to more than one
  distinct group, and report which groups it resolved to. Report **one row per activity**.
Never flag: An activity with exactly one group. An activity with no group at all — that is a
  separate check, and reporting it here as well would count one mapping gap twice.
---

## RULE DQ-D31 - The two schedule sources disagree about when a task should start

- category: Cross-source consistency
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: schedule, cross-source, planning, dates

Wrong: The same task has one planned start date in the task planning source and a different
  planned start date in the project schedule.
Matters: Two systems describe the same work and disagree about when it was meant to begin. Any
  variance or delay figure then depends on which source was read, and nobody reading a single
  report can tell which planned date it used.
Detect: Match the same logical task across the two planning sources and flag it where both hold
  a planned start date and the two differ. Report both dates and the size of the gap. Report
  **one row per task**.
Never flag: Tasks where either source has no planned start date. A missing date is a
  completeness problem, not a disagreement, and the two need different fixes.
---

## RULE DQ-D32 - The two schedule sources disagree about when a task should finish

- category: Cross-source consistency
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: schedule, cross-source, planning, dates

Wrong: The same task has one planned finish date in the task planning source and a different
  planned finish date in the project schedule.
Matters: Delay, forecast and schedule-variance results all change depending on which source is
  used, and the difference is invisible in any report built on one of them.
Detect: Match the same logical task across the two planning sources and flag it where both hold
  a planned finish date and the two differ. Report both dates and the size of the gap. Report
  **one row per task**.
Never flag: Tasks where either source has no planned finish date.
---

## RULE DQ-D33 - Task finished but was never recorded as starting

- category: Task integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- placeholder_date: 1900-01-01
- tags: task, dates, execution, completeness

Wrong: A task has an actual finish date recorded while its actual start date is absent.
Matters: Work that finished must have started. Without the start, the task's duration cannot be
  worked out at all, so it drops silently out of every duration and productivity figure instead
  of appearing as a gap — the totals still look complete while being short by however many tasks
  read this way.
Detect: Take the most recent record for each task. Flag those holding an actual finish date
  where the actual start date is absent. Report **one row per task**, not one per record.
Never flag: Tasks with no actual finish date — work that has not finished is not expected to
  show a start here, and unstarted work is a separate concern. Tasks where either date is the
  known placeholder value rather than a real one.
---

## RULE DQ-D34 - Task progress went backwards

- category: Task history
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: draft
- tags: task, history, progress, regression

Wrong: The latest progress recorded for a task is lower than the progress recorded for it before
  — for example 80% followed by 60%.
Matters: Progress falling is one of three different things: a correction, a rollback, or two
  conflicting records for the same task. Each needs a different response, and none of them is
  visible in a report that only ever reads the newest value. Where it is a conflict, every
  roll-up built on that task is wrong by the difference.
Detect: This check needs the HISTORY, not the current record. Put each task's records in time
  order and flag a task whose latest progress is lower than the progress in the record
  immediately before it. Report **one row per task**, with both figures and when each was
  recorded.
Never flag: Tasks with only one record — there is nothing to compare. Tasks where the earlier
  progress is absent rather than higher: a value appearing for the first time is not a decrease.
  **Draft — listed but not run.** The business has not yet confirmed whether progress is allowed
  to fall legitimately. Until it rules, this stays unrun so it cannot report a routine
  correction as a defect, and stays listed so the gap is visible rather than quietly dropped.
  Change the status to `active` once the business has decided.
---

## RULE DQ-D35 - Task is not attached to any well

- category: Reference integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, well, traceability, completeness

Wrong: A task records no well at all, so there is no way to say which well the work belongs to.
Matters: Everything reported per well — progress, slippage, crew effort — is assembled by
  grouping tasks under their well. A task with no well is not reported against the wrong well;
  it is reported against none, so the work it represents quietly disappears from every
  well-level total while still counting in the overall ones.
Detect: Take the most recent record for each task. Flag those where no well is recorded. Report
  **one row per task**, not one per record.
Never flag: Tasks that record a well which cannot be traced — naming something that does not
  exist is a different condition from naming nothing, and the two have different fixes. That
  case is the next check.
---

## RULE DQ-D36 - Task is attached to a well that does not exist

- category: Reference integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, well, reference, traceability

Wrong: A task records a well, but that well cannot be traced to a real one.
Matters: The task looks correctly attributed until someone tries to follow the link. Every
  report that joins tasks to wells drops the task silently rather than raising an error, so the
  work is missing from well-level figures while appearing perfectly healthy in task-level ones.
Detect: Take the most recent record for each task that records a well. Flag those whose well
  cannot be traced to the master list of wells. Report **one row per task**, with the value that
  could not be traced.
Never flag: Tasks recording no well at all — that is the previous check. Differences of spacing
  or letter case alone, where the well is otherwise the same one.
---

## RULE DQ-D37 - Task has no crew assigned

- category: Reference integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, crew, resource, completeness

Wrong: A task that has to be carried out by somebody names no crew.
Matters: Crew productivity, workload and utilisation are all worked out per crew. Work with no
  crew is counted in the totals but attributed to nobody, so every crew appears less busy than
  it is and the effort cannot be planned against.
Detect: Take the most recent record for each task that represents work to be executed. Flag
  those naming no crew. Report **one row per task**.
Never flag: Tasks that name a crew which cannot be traced — that is a separate check in this
  group, and the two must not both report the same task. Task types the business has confirmed
  need no crew.
---

## RULE DQ-D38 - Project has no work breakdown

- category: Structural completeness
- severity: high
- entity: project
- method: rule
- sql_mode: authored
- status: active
- tags: project, wbs, structure

Wrong: A project has no work-breakdown groups beneath it.
Matters: The work breakdown is how work is organised and how progress is rolled up into a
  project figure. A project without one cannot produce a progress number at all, so it is absent
  from project reporting rather than showing as incomplete.
Detect: Look at projects that are in scope for construction reporting. Flag a project where no
  work-breakdown group can be traced to it. Report **one row per project**.
Never flag: Projects the business has placed outside construction reporting. Do not read an
  empty or unrelated mapping table as proof that a project has no breakdown — if the
  relationship cannot be established from the business rules, report that rather than reporting
  every project as broken.
---

## RULE DQ-D39 - Work-breakdown group holds no activities

- category: Structural completeness
- severity: medium
- entity: wbs
- method: rule
- sql_mode: authored
- status: active
- tags: wbs, activity, structure

Wrong: A work-breakdown group exists but no activity belongs to it.
Matters: An empty group contributes no work to project progress while still carrying its share
  of the weighting. The project's progress is then divided among groups, one of which can never
  advance, so the total is permanently held below what the work actually justifies.
Detect: Look at work-breakdown groups belonging to a project in scope. Flag a group where no
  activity can be traced to it. Report **one row per group**.
Never flag: Groups the business defines as structural headings that are not meant to hold
  activities directly.
---

## RULE DQ-D40 - Activity has no work recorded against it

- category: Structural completeness
- severity: medium
- entity: activity
- method: rule
- sql_mode: authored
- status: active
- tags: activity, task, traceability, coverage

Wrong: An activity is defined in the work breakdown, but no task has ever been recorded against
  it.
Matters: The activity is planned work that execution data never touches, so it cannot be
  monitored, measured or reported as late. It sits in the plan looking complete-able while
  nothing exists to complete it.
Detect: Follow each activity through to the tasks recorded against it. Flag activities with
  none. Report **one row per activity**.
Never flag: Activities the business defines as summary, planning-only or otherwise not executed.
  Activities whose tasks exist but fail to resolve — that is the task-side traceability check,
  and reporting both would count the same broken link twice.
---

## RULE DQ-D41 - Active task has not been updated for a long time

- category: Task history
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: draft
- tags: task, freshness, stale

Wrong: A task that is still open has had no update for an unusually long time.
Matters: A stale task carries dates and a progress figure that look perfectly valid, so nothing
  about it appears wrong — but the figures describe a situation that may have moved on weeks
  ago. Every forecast built on it is confidently out of date, which is worse than an obviously
  missing value.
Detect: For each task still open, measure how long it has been since its most recent update,
  against the latest date the data itself reaches rather than against today's clock. Flag those
  older than the threshold the business has agreed. Report **one row per task**, with the age.
Never flag: Completed tasks. Tasks within the agreed window. **Draft — no freshness threshold
  has been agreed.** How long is too long is a business judgement about how often this data is
  expected to be maintained, and it differs by work type. Do not pick a number here: agree one,
  record it in the business rules, and change the status to `active`.
---

## RULE DQ-D42 - Task names equipment that does not exist

- category: Reference integrity
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: draft
- tags: task, equipment, resource, reference

Wrong: A task names a piece of equipment that cannot be traced to the equipment register.
Matters: Utilisation and cost are reported per item of equipment. Work attributed to something
  that is not in the register is counted in the totals but cannot be charged, scheduled or
  maintained against anything real.
Detect: Take the most recent record for each task that names equipment. Flag those whose
  equipment cannot be traced to the authoritative register. Report **one row per task**.
Never flag: Tasks naming no equipment, where equipment is not required for that kind of work.
  **Draft — the task-to-equipment relationship is not established.** The equipment register
  exists and is populated, but nothing states how a task refers to an item in it, nor which
  kinds of work are required to name one. Both have to be recorded in the business rules before
  this can be anything other than a guess. Change the status to `active` once they are.
---

# GROUP E — Construction and delivery deadlines

## RULE DQ-E01 - Construction deadline passed and the rig has still not arrived

- category: Construction deadlines
- severity: critical
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: construction, deadline, rig

Wrong: The construction deadline has passed and the rig has still not come on the well.
Matters: Location and flowline construction must both finish before the rig arrives. Neither has
  a stored completion date, so the rig itself is the evidence. A well past its deadline with no
  rig is construction that has overrun.
Detect: The construction deadline is one day before the expected rig-on date, and must be worked
  out — never read from a stored date, and specifically never from the location start or finish,
  which are not the agreed source. A well has missed it when today is past that deadline and the
  rig has still not arrived. Severity is the days elapsed since the deadline.
Never flag: Wells whose rig has arrived. Once the rig is on the well the construction deadline
  is not treated as missed, even if the rig was late — this check identifies wells still
  waiting, not construction delays on wells the rig has already reached.
---

## RULE DQ-E02 - Hook-up completed after its deadline

- category: Construction deadlines
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- grace_days: 2
- tags: hookup, deadline, completion

Wrong: Hook-up finished later than the deadline that applies to the well.
Matters: Hook-up is our final delivery and its lateness is directly attributable to us.
Detect: The deadline has two forms, and the one based on what actually happened takes
  precedence: - while the rig is still on the well, the deadline is the expected drilling finish
  plus 2 days; - once drilling has actually finished, the deadline is the actual finish plus 2
  days, and this form wins. Choose the applicable form for each well, then flag a well completed
  after it, or with no completion once the deadline has passed. Severity is the days late.
Never flag: Wells whose deadline has not yet arrived. A hook-up deadline and a completed well
  are different things and must not be treated as one.
---

## RULE DQ-E03 - Completed well still being treated as active

- category: Lifecycle consistency
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: lifecycle, completion, slippage

Wrong: A well that is complete still carries an open or in-progress status, or still has
  unfinished construction work attached to it.
Matters: Completed wells must be left out of live slippage analysis. A completed well left in
  the active set inflates the outstanding-work figure indefinitely and never resolves, because
  nothing further will ever happen to it.
Detect: Look at wells recorded as complete. Flag those whose status still says active or in
  progress, or which still have construction work below full progress. Severity is the number of
  records still open against the well. Match the status against the exact values the database
  actually uses rather than searching for a word inside the text, so a status that merely
  contains the word is not misread.
Never flag: Wells not yet complete. By definition they belong in the active set.
---

## RULE DQ-E04 - Rig arrived later than planned

- category: Schedule variance
- severity: low
- entity: well
- method: rule
- sql_mode: authored
- status: draft
- tags: schedule, variance, delay

Wrong: The rig arrived later than it was planned to.
Matters: This is a schedule delay, not a data-quality defect. The data is correct; the schedule
  slipped. It is listed here so the distinction is explicit rather than assumed.
Detect: Look at wells where both arrival dates are known and flag those where the actual came
  after the planned. The variance must keep its sign, so the direction carries the meaning:
  negative is ahead of schedule, positive is behind.
Never flag: Wells that arrived early. Never describe an early date as a delay, and never take
  the size of the difference and call it days of delay. **Draft — deliberately not run in a
  data-quality report.** Being early is not an anomaly, and being late is a performance fact
  rather than a defect. Activate this only if the report is meant to cover schedule performance
  as well as data quality.
---

## RULE DQ-E05 - Drilling finished later than planned

- category: Schedule variance
- severity: low
- entity: well
- method: rule
- sql_mode: authored
- status: draft
- tags: schedule, variance, delay

Wrong: Drilling finished later than it was planned to.
Matters: As with the check above this is a schedule delay, not a data defect — and drilling is
  the client's activity, not ours.
Detect: Look at wells where both finish dates are known and work out the signed difference
  between them.
Never flag: Wells that finished drilling early. **Draft** — same reasoning as the check above.
---

# GROUP F — Rollup and progress consistency

## RULE DQ-F01 - Drilling finished but construction is still open

- category: Lifecycle consistency
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: schedule, lifecycle, rig

Wrong: Drilling has finished on the well, but location and/or flowline construction is still
  recorded as below full completion.
Matters: Construction must complete before the rig arrives, so by the time drilling is finished
  it cannot legitimately still be open. Either progress was never updated, which understates
  completion everywhere it rolls up, or the work genuinely ran past the rig.
Detect: Look at wells where drilling has finished. Flag those with at least one construction
  activity below full progress, and those with no construction work recorded at all, which is
  itself a gap. Two things decide whether this check works: - Use the table that actually holds
  well records. This database also contains an empty one with a nearly identical name and the
  same columns; choosing it produces a check that examines nothing and reports a clean result. -
  Which stream a piece of work belongs to is largely unrecorded. Requiring a match would discard
  most of the data. Keep unclassified work in scope — work whose stream is unrecorded is itself
  a gap, not something to drop.
Never flag: Hook-up work. Hook-up legitimately runs after drilling finishes, so name the
  construction streams explicitly rather than excluding hook-up — a new stream added later must
  not silently start being treated as pre-rig work.
---

## RULE DQ-F02 - All the work in a group is done but the group is not

- category: Rollup consistency
- severity: high
- entity: wbs
- method: rollup
- sql_mode: authored
- status: active
- epsilon: 0.0001
- tags: wbs, progress, rollup

Wrong: Every activity in a work-breakdown group on a well reports full completion, yet the group
  itself works out to less than complete.
Matters: The group percentage feeds project progress. A group stuck below complete when its work
  is finished understates the project indefinitely, and no individual activity looks wrong — so
  nobody investigating activity by activity will ever find it.
Detect: Trace each activity to its group as the business rules describe. For each well and
  group, compare how many activities are at full completion against the total, and work out the
  group figure on the basis that activities within one group carry equal weight. Flag a group
  where every activity is complete but the computed figure is not. The tolerance above is an
  allowance for rounding when comparing two decimals that should be exactly equal. It is a
  precision allowance, not a business threshold — say so in the threshold note.
Never flag: Groups with no activities traced to them. An untraced task is a mapping gap, covered
  by its own check, not a rollup error.
---

## RULE DQ-F03 - Recomputed progress disagrees with the recorded progress

- category: Rollup consistency
- severity: critical
- entity: well
- method: rollup
- sql_mode: authored
- status: active
- epsilon: 0.0001
- tolerance: auto
- tags: wbs, pms, weightage, progress

Wrong: Two related problems, both of which corrupt the overall progress of a construction
  project: 1. the weightings for a well do not add up to the same total as every other well's;
  or 2. the progress recomputed from the individual activities does not match the progress
  actually recorded against the well.
Matters: Overall progress is the number the client sees. If the weightings are wrong, or the
  recorded figure was never recalculated after its parts changed, every report built on it is
  wrong — and nothing at the activity level looks out of place.
Detect: Recompute each well's progress as the weighted average of its activities, using the
  weighting per activity the business rules describe, and compare it against the most recently
  recorded progress for that well. That progress is kept as a series of snapshots, so the most
  recent for each well must be chosen before comparing. Two thresholds, of two different kinds,
  and the difference matters: - the **weighting total** calibrates itself. The correct total is
  not stated anywhere, so take it to be the middle of what the wells actually show and flag a
  well sitting outside their own spread. Where every well agrees, the spread is nothing and any
  disagreement at all is flagged, which is the desired behaviour; - the **recomputed gap** uses
  the rounding allowance only, because the correct gap is zero by definition — a recomputed
  total and a recorded total should be equal. Check the scale before comparing. The activity
  progress and the recorded well progress may be held on different scales, and comparing a
  fraction against a percentage silently flags every well.
Never flag: Wells with no activities or no recorded progress. There is nothing to reconcile, and
  missing is a different finding from inconsistent.
---

# GROUP G — Checks applied across the whole database

Every check above is about one specific thing: a named milestone, a particular deadline, a known
rollup. The six below are different. Each is written once and applied automatically to every
matching part of the database — one check per relationship, per date pair, per measured number —
so a finding still names the individual thing that is broken rather than collapsing dozens of
unrelated problems into a single number.

`expands_over` names the kind of thing to apply the check to. It is never a table name.

| `expands_over` | Applied to |
|---|---|
| `foreign_key` | every declared relationship between two tables |
| `duplicate_key` | every table expected to hold one record per thing |
| `date_pair` | every start/finish date pair |
| `future_date` | every date recording something that already happened |
| `numeric_range` | every number with a measured range |
| `text_numeric` | every piece of text holding numbers |

---

## RULE DQ-G01 - Record points at something that does not exist
- category: Referential integrity
- severity: high
- entity: row
- method: rule
- expands_over: foreign_key
- status: active

Wrong: A record refers to another record that is not there. The relationship is declared in the
  database, so this should be impossible — where it happens, the constraint is untrusted or was
  added after the bad data.
Matters: Every lookup through this link silently drops the record, so it vanishes from reports
  rather than appearing as an error.
Detect: Keep the records whose link has no matching target, ignoring records where the link is
  not set at all. No threshold: one is a defect.
Never flag: An unset link. "Not linked yet" is a different finding from "linked to something
  that does not exist", and treating them as one hides both.
---

## RULE DQ-G02 - The same thing appears more than once
- category: Grain integrity
- severity: high
- entity: row
- method: rule
- expands_over: duplicate_key
- status: draft

Wrong: Something that should appear once appears on more than one record.
Matters: Any count, sum or average over that data double-counts it. This is the single most
  damaging silent error in this kind of database, because the query runs perfectly and simply
  returns a number that is too big.
Detect: Group by the thing that should be unique and keep the groups holding more than one
  record. No threshold.
Never flag: An unset value, and data where repetition is legitimate — a history or snapshot is
  supposed to hold many records per thing.
**Draft — this check is circular as it stands, and must not be activated until that is fixed.**
It is currently applied wherever repetition was *detected*, so it reports repetition in exactly
the places already known to repeat. On the last measurement it flagged over 99% of one table and
over 90% of two others — which describes those tables' shape, not a defect. To make it work, the
business must name the data that genuinely holds one record per thing. Something is unique
because the business says so, never because a measurement says it is not.

---

## RULE DQ-G03 - A finish date comes before its start date
- category: Date integrity
- severity: high
- entity: row
- method: rule
- expands_over: date_pair
- status: active

Wrong: A finish date falls before the start date it is paired with.
Matters: Every duration worked out from the pair is negative, which quietly corrupts averages
  and totals rather than failing.
Detect: Compare the two directly. No threshold: a negative duration is impossible, not merely
  unusual.
Never flag: Records where either date is missing — a different finding. Never compare a plan
  against an outcome here: that measures the plan slipping, not a broken record.
---

## RULE DQ-G04 - A date for something that already happened is in the future
- category: Date integrity
- severity: medium
- entity: row
- method: rule
- expands_over: future_date
- status: draft

Wrong: A date recording something that has already happened is later than today.
Matters: Usually a typo in the year. It makes completed work look outstanding, or pulls a
  forecast years out.
Detect: Compare against the database's own current date, so the check uses the same clock the
  data was written against.
Never flag: Any date holding a target, schedule, forecast or expectation. Those are supposed to
  be in the future — that is what they are for. Where it is not established which of the two a
  date is, it must be left unchecked and reported as a gap in coverage, never assumed. **Draft —
  waiting on a statement of which dates record an outcome.** The database cannot say: both kinds
  look identical. An earlier version guessed from the name and reported thousands of perfectly
  good schedule dates as defects. The business rules already answer this for the well
  milestones; extend the same statement to the task dates and this check can be built with
  nothing guessed.
---

## RULE DQ-G05 - A number is outside the range it should occupy
- category: Value range
- severity: medium
- entity: row
- method: rule
- expands_over: numeric_range
- status: active

Wrong: A number falls outside the range it is supposed to occupy — most often a percentage above
  a hundred, or a negative quantity.
Matters: An out-of-range progress figure spreads into every total that averages or weights it.
Detect: Compare against the bounds that were **measured** from the data rather than assumed, so
  a fraction is judged against its own scale and a percentage against its own. Never substitute
  a bound of your own — the measured scale is the authority, and assuming the wrong one silently
  flags or clears everything.
Never flag: Numbers with no measured scale. Something unmeasured has no bounds to be outside of.
---

## RULE DQ-G06 - A number stored as text cannot be read as a number
- category: Type integrity
- severity: medium
- entity: row
- method: rule
- expands_over: text_numeric
- status: active

Wrong: Something holding a quantity as text contains a value that cannot be read as a number.
Matters: Every calculation has to convert it safely, and a safe conversion turns the bad value
  into a blank — so the record is silently dropped from the calculation instead of failing it.
  The defect is invisible precisely because the safe conversion hides it.
Detect: Attempt a numeric conversion that yields a blank on failure. A value that is present and
  non-blank but fails to convert is the anomaly. No threshold.
Never flag: Blank values, and data where no value at all reads as a number — that holds codes or
  identifiers, was never numeric, and flagging it would report everything.