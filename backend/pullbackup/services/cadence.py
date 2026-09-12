# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""How often a task is supposed to run, and when it is late enough to matter.

INCIDENT 2026-09-06..10. The task list showed `last_run_state: success` on
every row for four days while nothing executed. That value is not wrong — it
describes the last COMPLETED run — it simply answers a different question from
the one an operator is asking when they open the page. "Did the last backup
work?" and "is this task still backing up?" are independent, and until this
module existed only the first had an answer.

The second is derived, not stored: an enabled task whose next scheduled firing
is materially in the past, with nothing in flight, has skipped its window.
"Materially" has to be proportional — five minutes late is meaningless to a
daily backup and is most of a cycle to one that runs every two minutes — and it
has to be bounded, because a window proportional all the way up would let a
daily task sit silent for half a day before anybody heard about it.

This is also the single definition of a task's cadence. `kuma` sized push
monitor intervals from cron long before this card; two implementations of the
same question drift, and the drift would be invisible — a monitor interval and
a missed-schedule window quietly disagreeing about the same expression.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from croniter import croniter

# Cadence assumed for an expression that cannot be parsed.
#
# An hour: the same fallback `kuma.cron_interval_seconds` shipped with, kept so
# adopting this module changed no monitor's interval. A row with an
# unschedulable cron never fires at all, which the missed-schedule check
# reports through its own `next_run is None` branch — this value only decides
# how wide that report's grace window is.
DEFAULT_CADENCE_SECONDS = 3600

# Shortest cadence that can be expressed. A cron cannot fire more often than
# once a minute, and a smaller value would only ever be an artifact of a
# malformed expression.
MIN_CADENCE_SECONDS = 60

# Floor on the lateness that counts as a missed schedule.
#
# Half of a one-minute cadence is thirty seconds, which is inside ordinary
# APScheduler tick jitter, container clock skew and the `misfire_grace_time`
# the jobs are already registered with. A window that tight reports healthy
# scheduling as a fault, and a flag that cries wolf is worse than no flag —
# the September incident's whole cost was that nobody was looking.
MIN_SCHEDULE_GRACE_SECONDS = 300

# Ceiling on the same.
#
# The card's acceptance is that a wedged scheduler becomes visible within one
# cadence cycle. A purely proportional window misses that for anything slower
# than about two hours: half of a day is twelve hours of silence. An hour past
# the scheduled time is unambiguous for any task at all — nothing in this
# system legitimately fires eleven hours late — and it keeps a daily task's
# detection comfortably inside its own cycle.
MAX_SCHEDULE_GRACE_SECONDS = 3600


def cadence_seconds(cron: str, default: int = DEFAULT_CADENCE_SECONDS) -> int:
    """Seconds between this expression's next two firings.

    Measured rather than pattern-matched: an arbitrary cron has no closed form,
    and the gap between the upcoming two firings is what "how often does this
    run" means for every schedule a user can write.
    """
    try:
        iterator = croniter(cron, datetime.now(timezone.utc))
        first = iterator.get_next(datetime)
        second = iterator.get_next(datetime)
        return max(int((second - first).total_seconds()), MIN_CADENCE_SECONDS)
    except Exception:
        return default


def schedule_grace_seconds(cron: str) -> int:
    """How late this task's next run may be before the row is flagged.

    Half a cycle, clamped. Half rather than a whole one so a missed window is
    reported inside the cycle it was missed in, instead of at the moment the
    next one would also have been missed.
    """
    half = cadence_seconds(cron) // 2
    return max(MIN_SCHEDULE_GRACE_SECONDS, min(half, MAX_SCHEDULE_GRACE_SECONDS))


def schedule_is_missed(
    *,
    cron: str,
    enabled: bool,
    next_run: Optional[datetime],
    has_active_run: bool,
    last_activity_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Whether this task's schedule has silently stopped firing.

    TWO independent signals, because one of them does not catch the incident
    this exists for. Measured against the real app and real APScheduler on
    2026-09-12, reproducing the September mechanism (a run wedged in `running`
    holding its source, admission denying every later fire at `source_active`):

        task 'scrutiny'  next_run_time 2026-09-12 01:15   stale? False
        task 'sibling'   next_run_time 2026-09-12 01:45   stale? False

    `next_run_time` never went stale. APScheduler advances the trigger on every
    tick regardless of what the job body does, and `_execute` returned
    immediately for four days because admission refused it. Same for a
    coroutine that never returns: `max_instances=1` SKIPS the fire and still
    advances. A stale-next_run check on its own would have shown a clean task
    list throughout the entire outage — this card's own failure, rebuilt.

    So:

      1. **Elapsed activity.** Nothing has happened for longer than a cadence
         plus its grace window. This is the Kuma-shaped signal — the one thing
         that actually noticed in September — and it is what catches a
         scheduler whose triggers look perfectly healthy while no work occurs.
         Cadence PLUS grace, not cadence exactly: a task fires at its cadence,
         so elapsed time crosses one cadence every cycle by definition and
         flagging at exactly one would leave every task in the system flagged
         for the instant before each run.

      2. **A stale next_run.** Kept, and not redundant: it catches what elapsed
         activity cannot see quickly — a job never registered, a scheduler that
         never started, a trigger that genuinely stopped advancing — and it
         fires without waiting a full cycle.

    Three cases are deliberately NOT a missed schedule:

      * **Disabled.** Not scheduling a disabled task is the intended state.
      * **A run in flight.** The scheduler fired; the work is still going. A
        long transfer legitimately holds its window open, and a run wedged in
        `running` is the *other* failure — bounded by the execution timeout,
        reclaimed by the watchdog, already visible as a run that will not end.
      * **Unknown activity.** A caller with no `last_activity_at` gets the
        next_run check alone. Absent evidence is not evidence of a fault.

    An enabled task with NO next_run at all IS missed, and is the deadest
    signal available: the job is absent from the scheduler entirely.
    """
    if not enabled or has_active_run:
        return False
    moment = now or datetime.now(timezone.utc)
    grace = schedule_grace_seconds(cron)

    if last_activity_at is not None:
        # SQLite drops tzinfo on read, so every `Run.started_at` arrives naive.
        # The stored contract is UTC.
        if last_activity_at.tzinfo is None:
            last_activity_at = last_activity_at.replace(tzinfo=timezone.utc)
        silent_for = (moment - last_activity_at).total_seconds()
        if silent_for > cadence_seconds(cron) + grace:
            return True

    if next_run is None:
        return True
    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=timezone.utc)
    return next_run < moment - timedelta(seconds=grace)
