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

**What is wrong**
A well has no expected rig-on date recorded.

**Why it matters**
This is the master date the whole schedule is measured against. Every construction deadline,
the pegging deadline and the flowline approval deadline are worked out from it. A well without
it cannot be assessed for lateness at all — it silently disappears from every deadline check
instead of appearing as a problem.

**How to detect**
Examine every well. Flag the well when the expected rig-on date is absent.

**Do NOT flag**
Nothing. There is no legitimate state in which a planned well lacks this date.

---

## RULE DQ-A02 - Expected rig-off date is missing

- category: Milestone dates
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, planning

**What is wrong**
A well has no expected rig-off date recorded.

**Why it matters**
The hook-up deadline is worked out from it until drilling actually finishes. Without it,
hook-up cannot be judged on time.

**How to detect**
Examine every well. Flag the well when the expected rig-off date is absent.

**Do NOT flag**
Wells where drilling has already finished. From that point the actual date takes over for every
deadline, so the missing plan no longer blocks anything.

---

## RULE DQ-A03 - Rig arrived but no rig-on date was ever planned

- category: Milestone dates
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, planning

**What is wrong**
The rig is recorded as having arrived, but no expected arrival date was ever set.

**Why it matters**
The event happened but was never planned, so no variance can be worked out and the well is
invisible to schedule reporting. It also means every deadline derived from the master date was
never enforced for this well.

**How to detect**
Look only at wells where the rig has actually arrived. Flag those with no expected arrival date.

**Do NOT flag**
Wells where the rig has not arrived. A missing plan on those is a different check.

---

## RULE DQ-A04 - Drilling finished but no rig-off date was ever planned

- category: Milestone dates
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, planning

**What is wrong**
Drilling is recorded as finished, but no expected finish date was ever set.

**Why it matters**
Drilling performance for this well cannot be measured against any plan.

**How to detect**
Look only at wells where drilling has actually finished. Flag those with no expected finish date.

**Do NOT flag**
Wells where drilling has not finished.

---

## RULE DQ-A05 - Rig arrived after drilling was supposed to be finished

- category: Milestone dates
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, lifecycle

**What is wrong**
The rig actually arrived later than the date drilling was planned to be complete.

**Why it matters**
The planned window has been overtaken by events. Any forecast still built on the planned finish
is meaningless for this well.

**How to detect**
Look at wells where both the actual arrival and the planned finish are known. Flag those where
arrival came after the planned finish.

**Do NOT flag**
Wells missing either date — those are separate checks above.

---

## RULE DQ-A06 - Drilling finished before the rig was due to arrive

- category: Milestone dates
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, rig, lifecycle

**What is wrong**
Drilling is recorded as complete before the rig was even due on the well.

**Why it matters**
This is very unlikely to be a real sequence and much more likely a mistyped year, or a date
written into the wrong place. Left alone it makes the well appear finished far ahead of plan and
distorts every completion statistic it feeds.

**How to detect**
Look at wells where both the actual drilling finish and the planned arrival are known. Flag
those where drilling finished before the rig was due.

**Do NOT flag**
A well that is genuinely ahead of schedule by a normal margin. This check is about the
impossible ordering, not about being early.

---

## RULE DQ-A07 - Hook-up completed before the rig arrived

- category: Milestone dates
- severity: critical
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, lifecycle, completion

**What is wrong**
The well is recorded as handed over before the rig arrived on it.

**Why it matters**
Hook-up is the last step in the well's life and cannot come before the rig arrives. A well
marked complete before it was drilled corrupts every completion count and every progress total
that reads it.

**How to detect**
Look at wells where both the completion and the rig arrival are known. Flag those completed
before the rig arrived.

**Do NOT flag**
Wells with no completion recorded. An unfinished well is not an anomaly.

---

## RULE DQ-A08 - Hook-up completed before drilling finished

- category: Milestone dates
- severity: critical
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: milestone, lifecycle, completion

**What is wrong**
The well is recorded as handed over before drilling finished on it.

**Why it matters**
Hook-up is handed over only after the rig is off and the well has been cleaned. Completing
before that is impossible and points to a wrong date.

**How to detect**
Look at wells where both the completion and the drilling finish are known. Flag those completed
before drilling finished.

**Do NOT flag**
Wells missing either date.

---

## RULE DQ-A09 - Well completed while drilling is not recorded as finished

- category: Milestone dates
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: draft
- tags: milestone, lifecycle, undefined

**What is wrong**
The well is recorded as handed over, but drilling was never recorded as finished at all.

**Why it matters**
Either the drilling finish was never captured, or the completion is wrong. Both distort the
well's history.

**How to detect**
Look at wells with a completion recorded. Flag those where drilling has no recorded finish.

**Draft — not yet agreed.** It is not established whether a completion may legitimately be
recorded before the drilling finish is captured, for example where drilling is tracked in a
separate system. Make this active once the business confirms it.

---

## RULE DQ-A10 - Well is not linked to a project

- category: Reference integrity
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: reference, project

**What is wrong**
A well has no project recorded against it.

**Why it matters**
Every well belongs to a construction project. A well with no project cannot be counted into any
project total, so project progress is worked out from an incomplete set of wells with nothing
indicating the gap.

**How to detect**
Examine every well. Flag those where the project is absent or blank.

**Do NOT flag**
Wells where a project is recorded but points at something that does not exist. That is a broken
link and is already covered by the structural checks in Group G.

---

## RULE DQ-A11 - Well is not linked to a cluster

- category: Reference integrity
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: reference, cluster

**What is wrong**
A well has no cluster recorded against it.

**Why it matters**
Cluster is the geographic grouping every regional report adds up by. A well without one is left
out of cluster reporting silently.

**How to detect**
Examine every well. Flag those where the cluster is absent.

**Do NOT flag**
A cluster that is recorded but invalid — already covered by the structural checks in Group G.

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

**What is wrong**
A well date that records something which has **already happened** is later than today.

**Why it matters**
Usually a typo in the year. It makes completed work look outstanding, or pushes a milestone
years into the future, and every deadline worked out from that date inherits the error silently.

**How to detect**
Use only the well dates the business rules explicitly classify as recording an outcome. Flag a
well where any of those is later than the database's own current date, so the check uses the
same clock the data was written against. Report which date is at fault.

**Do NOT flag**
Any date the business rules classify as planned or expected — being in the future is exactly
what those are for. Any date the business rules do not classify at all: whether it records an
outcome or an intention has not been established, and assuming it is an outcome is how a
schedule date comes to be reported as thousands of defects. Dates holding the known placeholder
value, which is a separate check.

**A date is covered here only once the business rules say it records an outcome.** Several well
dates are not yet classified and are therefore not checked. Classify them and they are covered
immediately, with no change to this description and no change to any code.

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

**What is wrong**
The pegging sheet was not issued by its deadline, which is 60 days before the rig is due on the
well.

**Why it matters**
Pegging is the client's input and location construction cannot start without it. A late or
absent pegging sheet pushes the whole construction sequence toward the rig date.

**How to detect**
The deadline is 60 days before the expected rig-on date. A well has missed it in either of two
ways, and both must be counted:

1. the pegging was issued, but after the deadline; or
2. the pegging is missing and the deadline has already passed.

Look at every well with an expected rig-on date. Severity is how many days late — for a missing
pegging, measure from the deadline to today.

**The missing case must be counted, not filtered out.** A missing pegging past its deadline is a
miss. Excluding blanks would silently remove exactly the worst offenders.

**Do NOT flag**
Wells whose deadline has not yet arrived and whose pegging is still outstanding. They are not
late, they are simply not due.

---

## RULE DQ-B02 - Pegging sheet issued after the rig was due on the well

- category: Milestone deadlines
- severity: critical
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: pegging, deadline, PDO

**What is wrong**
The pegging sheet was issued after the date the rig was due — not merely past its own deadline,
but past the date construction was supposed to be finished.

**Why it matters**
A far more extreme failure than ordinary lateness: the input that must come before all
construction arrived after construction should have been complete. It is kept separate so it
cannot be lost among ordinary late-pegging findings.

**How to detect**
Look at wells where both the pegging and the expected rig-on date are known. Flag those where
pegging came after the rig was due. Severity is the number of days between them.

**Do NOT flag**
Wells merely past the 60-day deadline but still before the rig was due — those belong to the
ordinary late-pegging check.

---

## RULE DQ-B03 - Pegging recorded but no deadline can be worked out

- category: Milestone deadlines
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: pegging, deadline, planning

**What is wrong**
A pegging sheet exists but the expected rig-on date is missing, so its deadline cannot be worked
out and the milestone can never be judged on time.

**Why it matters**
The well looks compliant because nothing can prove it is not. It escapes the lateness check
through the absence of the very date that check depends on.

**How to detect**
Look at wells with a pegging sheet. Flag those with no expected rig-on date.

**Do NOT flag**
Wells with no pegging sheet at all — covered by other checks.

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

**What is wrong**
The pegging sheet was issued very much earlier than required.

**Why it matters**
Being early is normally good and is explicitly not an anomaly. But an extreme outlier may mean a
wrong year in the date rather than genuine early delivery.

**How to detect**
Compare the gap between pegging and the expected rig-on date across all wells, and flag only
extreme outliers, with the threshold worked out from the spread of the wells themselves — never
a fixed number of days.

**Draft — no agreed threshold.** The business has not defined how early is too early. Until it
does, this stays off rather than inventing a limit.

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

**What is wrong**
The flowline approval was not issued by its deadline, which is 90 days before the rig is due on
the well.

**Why it matters**
It is the client's input for flowline construction. Late or absent approval delays flowline
work — and the resulting delay is not charged to us, so recording it correctly matters
commercially as well as operationally.

**How to detect**
The deadline is 90 days before the expected rig-on date. A well has missed it in either of two
ways, and both must be counted:

1. the approval was issued, but after the deadline; or
2. the approval is missing and the deadline has already passed.

Look at every well with an expected rig-on date. Severity is how many days late — for a missing
approval, measure from the deadline to today.

**The missing case must be counted, not filtered out.**

**Do NOT flag**
Wells whose deadline has not yet arrived and whose approval is still outstanding.

---

## RULE DQ-C02 - Flowline approval issued after the rig was due on the well

- category: Milestone deadlines
- severity: critical
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: flaf, deadline, PDO

**What is wrong**
The flowline approval was issued after the date the rig was due.

**Why it matters**
The input required three months before the rig arrived instead came after it was due. Kept
separate so this extreme case is visible on its own.

**How to detect**
Look at wells where both the approval and the expected rig-on date are known. Flag those where
the approval came after the rig was due. Severity is the days between.

**Do NOT flag**
Wells merely past the 90-day deadline but still before the rig was due.

---

## RULE DQ-C03 - Flowline approval recorded but no deadline can be worked out

- category: Milestone deadlines
- severity: medium
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: flaf, deadline, planning

**What is wrong**
A flowline approval exists but the expected rig-on date is missing, so its deadline cannot be
worked out.

**Why it matters**
The milestone can never be judged, and the well silently escapes the lateness check.

**How to detect**
Look at wells with a flowline approval. Flag those with no expected rig-on date.

**Do NOT flag**
Wells with no approval at all.

---

## RULE DQ-C04 - Flowline approval issued after the pegging sheet

- category: Milestone deadlines
- severity: low
- entity: well
- method: rule
- sql_mode: authored
- status: draft
- tags: flaf, pegging, undefined

**What is wrong**
The flowline approval came later than the pegging sheet, although its deadline is the earlier of
the two.

**Why it matters**
The two deadlines imply the approval should normally come first. Whether the reverse order is
actually a defect is a business question, not a logical one — both are client inputs and may
legitimately arrive in either order.

**How to detect**
Look at wells where both are known. Flag those where the approval came after the pegging sheet.

**Draft — needs business confirmation.** The two deadlines are agreed, but nothing says the
order between them is mandatory. Do not activate until confirmed.

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

**What is wrong**
A task's planned start is after its planned finish.

**Why it matters**
Every planned duration worked out from the pair is negative, which quietly corrupts averages and
forecasts rather than failing visibly.

**How to detect**
Look at tasks where both planned dates are known. Flag those starting after they finish.
Severity is the size of the inversion in days.

**Do NOT flag**
Tasks missing either planned date.

**Disabled — this was being reported twice.** The structural checks in Group G already compare
every start/end date pair in the database, including this one, and both found the same records.
Two entries over one set of records inflates the totals in the report. Coverage is unchanged;
only the duplicate reporting is gone.

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

**What is wrong**
A task's actual start is after its actual finish.

**Why it matters**
Work cannot finish before it begins. Every duration and productivity figure worked out from the
pair is wrong.

**How to detect**
Look at tasks where both actual dates are known. Flag those starting after they finish. Severity
is the size of the inversion in days.

**Do NOT flag**
Tasks where either date is the known placeholder value.

**Disabled — this was being reported twice**, same as the check above. One thing is lost by
disabling this rather than the structural check, and it is recorded so the decision can be
revisited: this description excluded the placeholder date from its scope and the structural
check does not.

---

## RULE DQ-D03 - Task shows full progress but is not marked complete

- category: Task integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, progress, completion

**What is wrong**
A task's progress has reached its maximum while the task is still marked incomplete.

**Why it matters**
A direct contradiction between two things that must agree. Whichever is wrong, every total that
counts completed tasks disagrees with every total that adds up progress — and the two appear in
different reports.

**How to detect**
Judge each task at its current state. This data keeps a history, so the most recent record for
each task must be chosen before judging it. Flag a task at full progress that is not marked
complete.

Check the progress scale before comparing. Progress may be held as a fraction between nought and
one, or as a percentage out of a hundred, and comparing against the wrong one flags either every
task or none.

**Do NOT flag**
Tasks below full progress.

---

## RULE DQ-D04 - Task marked complete with no finish date

- category: Task integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, completion, dates

**What is wrong**
A task is marked complete but no finish date was recorded.

**Why it matters**
The task counts as done in every completion total, yet contributes nothing to duration or
productivity analysis — complete and unmeasurable at the same time.

**How to detect**
Take the most recent record for each task. Flag those marked complete with no finish date.

**Do NOT flag**
Tasks not marked complete.

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

**What is wrong**
A task has a finish date recorded while still being marked incomplete — the opposite
contradiction to the check above.

**Why it matters**
The work appears to have finished but is still counted as outstanding, overstating remaining
work in every forecast.

**How to detect**
Take the most recent record for each task. Flag those with a finish date that are not marked
complete.

**Do NOT flag**
Tasks whose finish date is the known placeholder value.

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

**What is wrong**
A default placeholder date has been stored where a real execution date belongs.

**Why it matters**
This is a known pattern in this data. It is neither a missing value nor a real date: it passes
every "is it blank" check while being decades wrong, so it silently poisons any duration worked
out from it. Every such record must be treated as unusable rather than as a real date.

**How to detect**
Examine task records. Flag any whose start or finish equals the placeholder date. The date
itself is a stated convention, so using it directly is correct here.

**Do NOT flag**
Genuinely missing dates. Missing and placeholder are different problems, and treating them as
one hides which needs fixing.

---

## RULE DQ-D07 - Quantity delivered with no hours recorded

- category: Productivity data
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, productivity, quantity

**What is wrong**
A task records work delivered while recording no hours spent.

**Why it matters**
Productivity is work per hour. A record with work and no hours makes productivity infinite,
distorting any crew or activity average that includes it.

**How to detect**
Look at task records where both the quantity and the hours are recorded. Flag those with a
quantity above zero and hours at zero.

**Do NOT flag**
Records where hours are missing rather than zero. Unrecorded is not the same as none.

---

## RULE DQ-D08 - Hours recorded with no quantity delivered

- category: Productivity data
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, productivity, hours

**What is wrong**
A task records hours spent while recording no work delivered.

**Why it matters**
This may be legitimate non-productive time, or it may be a missing quantity. It should be put in
front of a person to decide rather than silently averaged into productivity as a zero.

**How to detect**
Look at task records where both figures are recorded. Flag those with hours above zero and
quantity at zero.

**Do NOT flag**
Records where the quantity is missing rather than zero.

---

## RULE DQ-D09 - Work remains but no time is left to do it

- category: Task integrity
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, forecast

**What is wrong**
A task still has outstanding work while the time remaining for it has reached zero.

**Why it matters**
A contradiction in the forecast: work is left but no time is allowed for it, so the forecast
completion date for this task cannot be met.

**How to detect**
Take the most recent record for each task. Flag those with outstanding work above zero and no
time remaining.

**Do NOT flag**
Tasks already marked complete.

---

## RULE DQ-D10 - Unfinished task has no plan

- category: Task integrity
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, planning, dates

**What is wrong**
A task that is not yet complete has no planned start and/or no planned finish.

**Why it matters**
Schedule risk and slippage cannot be worked out for a task with no plan to measure against. The
task is invisible to every forward-looking analysis.

**How to detect**
Look at tasks not marked complete. Flag those missing either planned date.

**Do NOT flag**
Completed tasks. A plan is no longer needed once the work is done.

---

## RULE DQ-D11 - Conflicting current records for the same task

- category: Task grain
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, grain, history

**What is wrong**
The same task has more than one record that would each be read as the current one, and they
disagree about progress, completion, dates or crew.

**Why it matters**
The problem is not that a history exists — this data legitimately keeps one record per update,
and the most recent should be used. The problem is that two records cannot be put in order into
a single current state, so any analysis picks one arbitrarily and two reports reading the same
data disagree.

**How to detect**
Group task records by the well and the task they belong to. Within each group find the records
that tie for most recent. Flag a group where more than one record ties and those tied records
disagree on progress, completion or dates.

**Do NOT flag**
A task with many historical records that resolve cleanly to one most recent record. That is
normal and correct.

---

## RULE DQ-D12 - Task cannot be traced to an activity

- category: Reference integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, mapping, activity

**What is wrong**
A task cannot be traced through to a known activity, so it cannot be classified.

**Why it matters**
An unclassified task belongs to no activity and therefore to no part of the work breakdown, so
it counts toward nothing. It silently disappears from progress reporting rather than showing up
as a gap.

**How to detect**
Follow the activity mapping the business rules describe. Flag tasks whose activity cannot be
worked out, or resolves to nothing.

Keep unmatched tasks visible: the lookup must preserve them rather than dropping them, or the
count of unmapped tasks reads zero for exactly the wrong reason.

**Do NOT flag**
Tasks that trace successfully.

---

## RULE DQ-D13 - Activity does not belong to any part of the work breakdown

- category: Reference integrity
- severity: high
- entity: activity
- method: rule
- sql_mode: authored
- status: active
- tags: mapping, wbs

**What is wrong**
An activity can be traced, but it belongs to no group in the work breakdown structure.

**Why it matters**
The work breakdown is how progress rolls up to the project. An activity outside it contributes
to nothing, and the project percentage is worked out from an incomplete base.

**How to detect**
Trace activities to their work-breakdown group as the business rules describe, taking care to
use the code the mapping is actually keyed on. Flag activities with no group.

Report the unmapped tally beside the group counts, never inside them. Counting "unmapped" as if
it were a group of its own adds a phantom to every breakdown.

**Do NOT flag**
Activities that have a group.

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

**What is wrong**
A task took far longer, or far less time, than the published norm for that kind of work — well
beyond the variation tasks normally show. Both directions matter: over the norm is an overrun,
under the norm is usually a data-entry error such as a finish date copied from a start date.

**Why it matters**
Norms drive planning. A task recorded at a fraction of its norm inflates apparent productivity
and corrupts every forward estimate built on it.

**How to detect**
Trace each task to its activity and to the norm published for that activity, then compare the
number of days it actually took.

The threshold must calibrate itself from the data. For each activity, look at how much its tasks
normally deviate from the norm, and flag only the tasks that sit far outside that activity's own
normal spread. Require at least the stated minimum number of observed tasks for an activity
before judging any of them, so a rarely-run activity is not judged from a handful of records. Do
not pick a multiplier or a percentage yourself; derive the cut-off from the observed spread.

More than one place in this database publishes norms, and they do not all agree. Choose the one
with the most complete, genuinely numeric coverage, and say in the threshold note which was
used.

**Do NOT flag**
Tasks with a missing or placeholder start or finish. Norms that are not usable numbers — that is
the next check.

---

## RULE DQ-D15 - Published norm is not a usable number

- category: Reference data
- severity: medium
- entity: activity
- method: rule
- sql_mode: authored
- status: active
- tags: norms, reference

**What is wrong**
A published norm is stored as text and cannot be read as a number.

**Why it matters**
A norm that cannot be read is not a loose check — it is no check. Those activities vanish from
the duration comparison without appearing anywhere as a gap, because the unreadable value
quietly becomes a blank and the record is dropped.

**How to detect**
Examine the published norms. Flag any non-blank value that cannot be read as a number.

**Do NOT flag**
Norms that are genuinely absent. "Not published yet" is a different thing from "published but
unreadable", and treating them as one hides the one that is actually a defect.

---

## RULE DQ-D16 - Task crew cannot be traced

- category: Reference integrity
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: crew, mapping

**What is wrong**
A task names a crew that cannot be traced to a real crew.

**Why it matters**
Crew productivity and utilisation are reported per crew. An untraceable crew means the work is
counted in the totals but attributed to nobody.

**How to detect**
Look at tasks that name a crew. Flag those whose crew cannot be traced.

**Do NOT flag**
Tasks with no crew named at all. Unassigned is a different condition from misassigned.

---

## RULE DQ-D17 - Employee is not linked to a crew

- category: Reference integrity
- severity: low
- entity: employee
- method: rule
- sql_mode: authored
- status: active
- tags: crew, employee, mapping

**What is wrong**
An employee-to-crew assignment does not trace to both a real employee and a real crew.

**Why it matters**
Manpower reporting rolls up through the crew relationship. An employee outside it is invisible
to those totals.

**How to detect**
Examine the employee-to-crew assignments and flag those that do not trace to both.

**Do NOT flag**
Employees legitimately not assigned to any crew.

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

**What is wrong**
Work began after the date it was supposed to have been completed.

**Why it matters**
The task was never going to meet its plan, and the schedule showed no warning of it. Both dates
are individually valid, so nothing catches this unless the plan and the outcome are compared
against each other.

**How to detect**
Look at tasks where both the actual start and the planned finish are known. Flag those where
work began after the planned finish. Severity is the size of the overrun in days.

**Do NOT flag**
Tasks missing either date, or where either is the known placeholder value.

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

**What is wrong**
Work was recorded as complete before the plan said it should even begin.

**Why it matters**
Either the work was logged against the wrong task, or the plan was written after the fact. Both
make the schedule a record of neither intention nor outcome.

**How to detect**
Look at tasks where both the actual finish and the planned start are known. Flag those finishing
before they were due to start. Severity is the size of the gap in days.

**Do NOT flag**
Tasks missing either date, or carrying the placeholder value.

---

## RULE DQ-D20 - Progress recorded but the task never started

- category: Task integrity
- severity: medium
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, progress, execution

**What is wrong**
A task shows progress above zero while having no start date. Work has demonstrably begun, yet
nothing records when.

**Why it matters**
Every duration, productivity and delay figure for the task is worked out from its start.
Without one the task is progressing but cannot be measured, and it is invisible to any report
built on execution dates.

**How to detect**
Take the most recent record for each task. Flag those with progress above zero and no start
date. Check the progress scale before comparing — it may be a fraction rather than a percentage.

**Do NOT flag**
Tasks at zero progress. Not started is a legitimate state, not a defect. Tasks with no progress
recorded at all — a missing measure is a different finding from a contradicted one.

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

**What is wrong**
A task has a finish date while its progress is still zero — the opposite contradiction to the
check above.

**Why it matters**
A finished task reporting no progress is counted as outstanding in every total, so completed
work is reported as remaining and the overall figure understates what has been achieved.

**How to detect**
Take the most recent record for each task. Flag those with a finish date and zero progress.
Check the progress scale before comparing.

**Do NOT flag**
Records whose finish date is the known placeholder value. Tasks with no progress recorded at all.

---

## RULE DQ-D22 - Task marked complete while progress is below full

- category: Task integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, progress, completion

**What is wrong**
A task is marked complete while its progress has not reached its maximum.

**Why it matters**
Completion counts and progress totals then disagree about the same task. One says the work is
done, the other says it is not, and a report built on either alone is confidently wrong.

**How to detect**
Take the most recent record for each task. Flag tasks marked complete whose progress is below
full.

Work out what "full" means from the measured scale of the progress figure before comparing —
using the wrong one silently flags or clears every task.

**Do NOT flag**
Tasks with no progress recorded at all. A missing measure cannot contradict the mark, and it is
a different finding.

---

## RULE DQ-D23 - Negative time remaining

- category: Value integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, duration, impossible-value

**What is wrong**
The time left to complete a task is recorded as a negative number. Time left cannot be less than
none.

**Why it matters**
A negative value does not merely misreport its own task: it subtracts from any total that adds
up remaining time, so it understates the outstanding work of every group it belongs to. The
error spreads silently into figures that look perfectly reasonable.

**How to detect**
Flag task records where the time remaining is below zero. No threshold — negative is impossible,
not merely unusual.

**Do NOT flag**
Records where no figure is recorded. Zero is legitimate and is not negative.

---

## RULE DQ-D24 - Negative hours

- category: Value integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, hours, impossible-value

**What is wrong**
A task record holds a negative figure for hours worked. Work performed cannot be less than none.

**Why it matters**
Manpower totals and every productivity ratio worked out from hours are reduced by the negative
value, so the reported effort is lower than the effort actually spent.

**How to detect**
Flag task records where the hours figure is below zero. No threshold.

**Do NOT flag**
Records where no hours are recorded. Zero hours is legitimate — a quantity delivered against
zero hours is a separate check.

---

## RULE DQ-D25 - Negative quantity delivered

- category: Value integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: active
- tags: task, quantity, impossible-value

**What is wrong**
A task record holds a negative quantity delivered. Work delivered cannot be less than none.

**Why it matters**
Quantity delivered drives progress and productivity. A negative figure reduces the totals it
feeds, so delivered work is understated wherever it is added up.

**How to detect**
Flag task records where the quantity delivered is below zero. No threshold.

**Do NOT flag**
Records where no quantity is recorded. Zero is legitimate — hours against zero quantity is a
separate check.

---

## RULE DQ-D26 - Negative quantity outstanding

- category: Value integrity
- severity: high
- entity: task
- method: rule
- sql_mode: authored
- status: draft
- tags: task, quantity, impossible-value

**What is wrong**
The quantity still outstanding on a task is recorded as a negative number.

**Why it matters**
Outstanding work cannot be less than none, and a negative value subtracts from every total that
adds up remaining work — understating what is still to be done.

**How to detect**
Flag task records where the outstanding quantity is below zero.

**Do NOT flag**
Records where no figure is recorded. Zero is legitimate.

**Draft — the figure has not been formally identified.** Several numbers held against a task
could be the outstanding quantity, and the business rules do not yet say which one it is.
Guessing would produce a check that runs, returns a confident number, and measures the wrong
thing. Name it in the business rules, then make this active.

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

**What is wrong**
The construction deadline has passed and the rig has still not come on the well.

**Why it matters**
Location and flowline construction must both finish before the rig arrives. Neither has a stored
completion date, so the rig itself is the evidence. A well past its deadline with no rig is
construction that has overrun.

**How to detect**
The construction deadline is one day before the expected rig-on date, and must be worked out —
never read from a stored date, and specifically never from the location start or finish, which
are not the agreed source. A well has missed it when today is past that deadline and the rig has
still not arrived. Severity is the days elapsed since the deadline.

**Do NOT flag**
Wells whose rig has arrived. Once the rig is on the well the construction deadline is not
treated as missed, even if the rig was late — this check identifies wells still waiting, not
construction delays on wells the rig has already reached.

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

**What is wrong**
Hook-up finished later than the deadline that applies to the well.

**Why it matters**
Hook-up is our final delivery and its lateness is directly attributable to us.

**How to detect**
The deadline has two forms, and the one based on what actually happened takes precedence:

- while the rig is still on the well, the deadline is the expected drilling finish plus 2 days;
- once drilling has actually finished, the deadline is the actual finish plus 2 days, and this
  form wins.

Choose the applicable form for each well, then flag a well completed after it, or with no
completion once the deadline has passed. Severity is the days late.

**Do NOT flag**
Wells whose deadline has not yet arrived. A hook-up deadline and a completed well are different
things and must not be treated as one.

---

## RULE DQ-E03 - Completed well still being treated as active

- category: Lifecycle consistency
- severity: high
- entity: well
- method: rule
- sql_mode: authored
- status: active
- tags: lifecycle, completion, slippage

**What is wrong**
A well that is complete still carries an open or in-progress status, or still has unfinished
construction work attached to it.

**Why it matters**
Completed wells must be left out of live slippage analysis. A completed well left in the active
set inflates the outstanding-work figure indefinitely and never resolves, because nothing
further will ever happen to it.

**How to detect**
Look at wells recorded as complete. Flag those whose status still says active or in progress, or
which still have construction work below full progress. Severity is the number of records still
open against the well.

Match the status against the exact values the database actually uses rather than searching for a
word inside the text, so a status that merely contains the word is not misread.

**Do NOT flag**
Wells not yet complete. By definition they belong in the active set.

---

## RULE DQ-E04 - Rig arrived later than planned

- category: Schedule variance
- severity: low
- entity: well
- method: rule
- sql_mode: authored
- status: draft
- tags: schedule, variance, delay

**What is wrong**
The rig arrived later than it was planned to.

**Why it matters**
This is a schedule delay, not a data-quality defect. The data is correct; the schedule slipped.
It is listed here so the distinction is explicit rather than assumed.

**How to detect**
Look at wells where both arrival dates are known and flag those where the actual came after the
planned. The variance must keep its sign, so the direction carries the meaning: negative is
ahead of schedule, positive is behind.

**Do NOT flag**
Wells that arrived early. Never describe an early date as a delay, and never take the size of
the difference and call it days of delay.

**Draft — deliberately not run in a data-quality report.** Being early is not an anomaly, and
being late is a performance fact rather than a defect. Activate this only if the report is meant
to cover schedule performance as well as data quality.

---

## RULE DQ-E05 - Drilling finished later than planned

- category: Schedule variance
- severity: low
- entity: well
- method: rule
- sql_mode: authored
- status: draft
- tags: schedule, variance, delay

**What is wrong**
Drilling finished later than it was planned to.

**Why it matters**
As with the check above this is a schedule delay, not a data defect — and drilling is the
client's activity, not ours.

**How to detect**
Look at wells where both finish dates are known and work out the signed difference between them.

**Do NOT flag**
Wells that finished drilling early.

**Draft** — same reasoning as the check above.

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

**What is wrong**
Drilling has finished on the well, but location and/or flowline construction is still recorded
as below full completion.

**Why it matters**
Construction must complete before the rig arrives, so by the time drilling is finished it cannot
legitimately still be open. Either progress was never updated, which understates completion
everywhere it rolls up, or the work genuinely ran past the rig.

**How to detect**
Look at wells where drilling has finished. Flag those with at least one construction activity
below full progress, and those with no construction work recorded at all, which is itself a gap.

Two things decide whether this check works:

- Use the table that actually holds well records. This database also contains an empty one with
  a nearly identical name and the same columns; choosing it produces a check that examines
  nothing and reports a clean result.
- Which stream a piece of work belongs to is largely unrecorded. Requiring a match would discard
  most of the data. Keep unclassified work in scope — work whose stream is unrecorded is itself
  a gap, not something to drop.

**Do NOT flag**
Hook-up work. Hook-up legitimately runs after drilling finishes, so name the construction
streams explicitly rather than excluding hook-up — a new stream added later must not silently
start being treated as pre-rig work.

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

**What is wrong**
Every activity in a work-breakdown group on a well reports full completion, yet the group itself
works out to less than complete.

**Why it matters**
The group percentage feeds project progress. A group stuck below complete when its work is
finished understates the project indefinitely, and no individual activity looks wrong — so
nobody investigating activity by activity will ever find it.

**How to detect**
Trace each activity to its group as the business rules describe. For each well and group,
compare how many activities are at full completion against the total, and work out the group
figure on the basis that activities within one group carry equal weight. Flag a group where
every activity is complete but the computed figure is not.

The tolerance above is an allowance for rounding when comparing two decimals that should be
exactly equal. It is a precision allowance, not a business threshold — say so in the threshold
note.

**Do NOT flag**
Groups with no activities traced to them. An untraced task is a mapping gap, covered by its own
check, not a rollup error.

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

**What is wrong**
Two related problems, both of which corrupt the overall progress of a construction project:

1. the weightings for a well do not add up to the same total as every other well's; or
2. the progress recomputed from the individual activities does not match the progress actually
   recorded against the well.

**Why it matters**
Overall progress is the number the client sees. If the weightings are wrong, or the recorded
figure was never recalculated after its parts changed, every report built on it is wrong — and
nothing at the activity level looks out of place.

**How to detect**
Recompute each well's progress as the weighted average of its activities, using the weighting
per activity the business rules describe, and compare it against the most recently recorded
progress for that well. That progress is kept as a series of snapshots, so the most recent for
each well must be chosen before comparing.

Two thresholds, of two different kinds, and the difference matters:

- the **weighting total** calibrates itself. The correct total is not stated anywhere, so take
  it to be the middle of what the wells actually show and flag a well sitting outside their own
  spread. Where every well agrees, the spread is nothing and any disagreement at all is flagged,
  which is the desired behaviour;
- the **recomputed gap** uses the rounding allowance only, because the correct gap is zero by
  definition — a recomputed total and a recorded total should be equal.

Check the scale before comparing. The activity progress and the recorded well progress may be
held on different scales, and comparing a fraction against a percentage silently flags every
well.

**Do NOT flag**
Wells with no activities or no recorded progress. There is nothing to reconcile, and missing is
a different finding from inconsistent.

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

**What is wrong**
A record refers to another record that is not there. The relationship is declared in the
database, so this should be impossible — where it happens, the constraint is untrusted or was
added after the bad data.

**Why it matters**
Every lookup through this link silently drops the record, so it vanishes from reports rather
than appearing as an error.

**How to detect**
Keep the records whose link has no matching target, ignoring records where the link is not set
at all. No threshold: one is a defect.

**Do NOT flag**
An unset link. "Not linked yet" is a different finding from "linked to something that does not
exist", and treating them as one hides both.

---

## RULE DQ-G02 - The same thing appears more than once
- category: Grain integrity
- severity: high
- entity: row
- method: rule
- expands_over: duplicate_key
- status: draft

**What is wrong**
Something that should appear once appears on more than one record.

**Why it matters**
Any count, sum or average over that data double-counts it. This is the single most damaging
silent error in this kind of database, because the query runs perfectly and simply returns a
number that is too big.

**How to detect**
Group by the thing that should be unique and keep the groups holding more than one record. No
threshold.

**Do NOT flag**
An unset value, and data where repetition is legitimate — a history or snapshot is supposed to
hold many records per thing.

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

**What is wrong**
A finish date falls before the start date it is paired with.

**Why it matters**
Every duration worked out from the pair is negative, which quietly corrupts averages and totals
rather than failing.

**How to detect**
Compare the two directly. No threshold: a negative duration is impossible, not merely unusual.

**Do NOT flag**
Records where either date is missing — a different finding. Never compare a plan against an
outcome here: that measures the plan slipping, not a broken record.

---

## RULE DQ-G04 - A date for something that already happened is in the future
- category: Date integrity
- severity: medium
- entity: row
- method: rule
- expands_over: future_date
- status: draft

**What is wrong**
A date recording something that has already happened is later than today.

**Why it matters**
Usually a typo in the year. It makes completed work look outstanding, or pulls a forecast years
out.

**How to detect**
Compare against the database's own current date, so the check uses the same clock the data was
written against.

**Do NOT flag**
Any date holding a target, schedule, forecast or expectation. Those are supposed to be in the
future — that is what they are for. Where it is not established which of the two a date is, it
must be left unchecked and reported as a gap in coverage, never assumed.

**Draft — waiting on a statement of which dates record an outcome.** The database cannot say:
both kinds look identical. An earlier version guessed from the name and reported thousands of
perfectly good schedule dates as defects. The business rules already answer this for the well
milestones; extend the same statement to the task dates and this check can be built with nothing
guessed.

---

## RULE DQ-G05 - A number is outside the range it should occupy
- category: Value range
- severity: medium
- entity: row
- method: rule
- expands_over: numeric_range
- status: active

**What is wrong**
A number falls outside the range it is supposed to occupy — most often a percentage above a
hundred, or a negative quantity.

**Why it matters**
An out-of-range progress figure spreads into every total that averages or weights it.

**How to detect**
Compare against the bounds that were **measured** from the data rather than assumed, so a
fraction is judged against its own scale and a percentage against its own. Never substitute a
bound of your own — the measured scale is the authority, and assuming the wrong one silently
flags or clears everything.

**Do NOT flag**
Numbers with no measured scale. Something unmeasured has no bounds to be outside of.

---

## RULE DQ-G06 - A number stored as text cannot be read as a number
- category: Type integrity
- severity: medium
- entity: row
- method: rule
- expands_over: text_numeric
- status: active

**What is wrong**
Something holding a quantity as text contains a value that cannot be read as a number.

**Why it matters**
Every calculation has to convert it safely, and a safe conversion turns the bad value into a
blank — so the record is silently dropped from the calculation instead of failing it. The defect
is invisible precisely because the safe conversion hides it.

**How to detect**
Attempt a numeric conversion that yields a blank on failure. A value that is present and
non-blank but fails to convert is the anomaly. No threshold.

**Do NOT flag**
Blank values, and data where no value at all reads as a number — that holds codes or
identifiers, was never numeric, and flagging it would report everything.
