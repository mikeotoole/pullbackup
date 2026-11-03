# Successful-backup freshness and what Uptime Kuma DOWN means

One question, one answer, used by the task API (`freshness` on each task),
the task list UI, `GET /api/system/scheduler` and every Uptime Kuma push:

> Is the newest **successful** backup recent enough for this task's schedule?

It is deliberately *not* "did the last attempt work" and *not* "is the
scheduler alive". Those are separate facts that stay separate.

The single derivation is `backend/pullbackup/services/freshness.py`
(`project` is pure, `project_task` reads the database). Kuma delivery is
`services/kuma_sync.py`. Nothing else computes freshness.

## Root cause this replaces

| Before | Where | Problem |
|---|---|---|
| `_kuma_push(token, "up" if ok else "down")` on every run end | `services/notify.py` (`dispatch`) | One failed attempt pushed `down` although the previous success was still inside its window; a retry succeeding or a scheduler tick could never express "stale". The push described the *attempt*. |
| Monitor `interval` = bare cadence | `services/kuma.py` `cron_interval_seconds` via `cadence.cadence_seconds` | A run a little longer than the previous one, jitter, or a 25-hour DST day let the push-monitor expire: flapping. |
| `_sync_kuma` swallowed every failure into a log line | `api/tasks.py` | A refused monitor edit left Kuma wrong with nothing visible. |

## Contract

**Occurrence.** A cron firing as the live APScheduler trigger computes it
(`due.next_occurrence`, in the scheduler zone `$TZ`, so both DST folds and the
spring-forward gap follow what actually fires).

**Covered.** An occurrence is covered by a successful run whose `started_at`
is at or after it (minus `due.TICK_EARLY_TOLERANCE`, 1 s, the same tolerance
the scheduler already uses). A run that *started before* an occurrence and
finished after it does not pay it. Only `RunState.success` counts; failed and
cancelled attempts never cover anything.

**Epoch.** `Task.schedule_epoch_at` (NULL means `created_at`) is set by a
schedule or enable/disable edit (`due.on_task_edited`). Nothing is owed for
occurrences before it, and a success older than the epoch is shown as
`schedule_changed`, not `fresh`.

**Owed.** The earliest *uncovered* occurrence that is already in the past.

**Deadline.** `occurrence + grace`.

**Grace** (`freshness.grace_seconds`), bounded on both terms:

```
slack     = clamp(10% of the cycle, 10 min, 2 h)
allowance = min(1.5 x longest of the last 10 successful durations, cycle / 2)
            (cycle / 2 when there is no success history)
grace     = slack + allowance          # always < one whole cycle for cycles >= 1 h
```

`cycle` is the nominal cadence (`cadence.cadence_seconds`), never the gap to
the next occurrence, because the repeated hour of a fall-back day would
otherwise shrink a daily cycle to one hour.

Derived values (measured on the formulas above): per-minute 10.5 min; hourly 40 min
(10-minute jobs: 25 min); daily with 1 h runs 3 h 30 min; daily with no history 14 h;
weekly with 1 h runs 3 h 30 min. A missed daily backup is therefore `stale` at
most ~14 h after its time and usually ~3.5 h; a missed weekly one ~3.5 h, never
"a day late". The 2 h slack ceiling also absorbs a one-hour DST shift.

**States** (`freshness.STATES`):

| state | meaning | Kuma push |
|---|---|---|
| `fresh` | every occurrence due so far is covered by a success under the current schedule | `up` |
| `not_yet_due` | nothing owed yet and either no history (`no_history`) or the only success predates the current schedule (`schedule_changed`) | nothing |
| `delayed` | owed, still inside its grace | nothing |
| `stale` | owed and past the deadline, or the cron is invalid | `down` |
| `disabled` | task disabled | nothing |

`reason` explains *why* it is owed. It never changes `state`:

| reason | evidence |
|---|---|
| `covered` / `disabled` / `no_history` / `schedule_changed` | fresh / disabled / not_yet_due |
| `awaiting_start` | delayed, nothing else known |
| `queued` | due-work row `due` (source/destination/capacity wait) |
| `running` | a run is pending/running, or due-work `dispatched` |
| `retrying` | due-work `retry` |
| `retries_exhausted` | due-work `exhausted` |
| `blocked` | due-work `blocked` (invalid stored configuration) |
| `last_attempt_failed` / `last_attempt_cancelled` | newest failed/cancelled run is newer than the owed occurrence |
| `missed` / `never_succeeded` | stale with no other evidence (a run history exists / none ever succeeded) |
| `invalid_schedule` | stale: persisted cron cannot be parsed |

### Lifecycle branches

| situation | result |
|---|---|
| New task, before its first occurrence | `not_yet_due` / `no_history` |
| First occurrence passed, nothing ran yet | `delayed` / `awaiting_start`, then `stale` / `never_succeeded` at the deadline |
| Success covers the latest occurrence | `fresh`; `occurrence_at` is the next obligation |
| Last success spans an occurrence (started before it) | that occurrence is still owed |
| Run in progress, inside grace | `delayed` / `running`; past grace it is `stale` / `running` (a long job is not exempt) |
| Retry scheduled / exhausted / blocked, inside grace | `delayed` with the matching reason |
| Same, past grace | `stale` with the same reason |
| Repeated failed/cancelled attempts, scheduler healthy, queue busy | cannot change `state` or `last_success_*` |
| Task disabled | `disabled`; re-enabling sets a new epoch |
| Schedule edited | new epoch; old occurrences are not owed |
| Invalid cron | `stale` / `invalid_schedule` (enabled) |
| Unknown due-work status | treated as no evidence; never promoted to fresh |

`last_success_run_id`, `last_success_started_at` and `last_success_at` always
identify the newest successful run, whatever the state.

## What Kuma DOWN means

A push monitor named `pullback: <task>` is **DOWN** when the task is `stale`:
a scheduled backup is overdue beyond its bounded grace and **no successful
backup has covered it**. It is *not* "HTTP is down", *not* "the scheduler
stopped" (`/api/system/health` `scheduler_alive` and the heartbeat job report
that, independently), and *not* "the latest attempt failed". To tell them apart:

1. Open the task row or `GET /api/tasks/<id>` and read `freshness.reason`.
2. `last_attempt_failed` / `retrying` / `retries_exhausted`: the backup is stale
   *and* attempts are failing; read the run log.
3. `queued` / `running`: the backup is stale because it is blocked or slow; check
   `wait_reason` and `GET /api/system/scheduler` (`queued_task_ids`).
4. `missed`: nothing was attempted; check scheduler liveness and `missed_schedule`.
5. A single failed attempt inside grace is `delayed`, not DOWN.

The monitor's own expiry (`freshness.kuma_expiry_seconds`: cadence + the largest
grace + 1 h) is only a dead-man backstop for a Pullbackup process that is gone.
It is intentionally longer than the old cadence-length expiry.

Nothing pushes an unconditional `up`. A sweep every 5 minutes (a scheduler job
separate from the liveness heartbeat) re-asserts only what the projection says,
and retries a failed delivery. `GET /api/system/scheduler` returns
`stale_task_ids` and `kuma_failures` (task, `push` or `configuration`, error class,
attempts); a failed push or monitor edit is never reported as a successful sync,
and error text is the exception class or HTTP status only (never the push URL).

## Reconciling existing monitors

Existing monitors still have a cadence-length expiry. `python -m
pullbackup.tools.kuma_reconcile` is a dry run (read-only `getMonitor`); with
`--apply` it sets only `interval`, three bounded tries per monitor, reports any
failure and exits non-zero. It never pauses/resumes, creates or deletes a
monitor and never pushes a heartbeat, so ids, tokens, parent group,
notification wiring and an operator's paused state are preserved. Running it
against the live Kuma is a separately approved operational step.
