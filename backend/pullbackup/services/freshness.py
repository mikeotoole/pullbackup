# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""Successful-backup freshness: ONE projection for the API, the UI and Kuma.

The question is "is the newest SUCCESSFUL backup recent enough for this
schedule?". It is deliberately not "did the last attempt work" (a failed retry
says nothing about the success before it) and not "is the scheduler alive"
(`scheduler.liveness` is independent, and a healthy scheduler running failing
or queued work must never make a stale backup look healthy).

The contract is documented in docs/freshness.md. In one paragraph:

* An OCCURRENCE is a cron firing as the live APScheduler trigger computes it
  (`due.next_occurrence`, zone-aware, both DST folds). It is COVERED by a
  success whose `started_at` is at or after it (less the one-second early-tick
  tolerance the scheduler already uses). The earliest uncovered occurrence
  that is already in the past is the debt; its DEADLINE is that occurrence plus
  a bounded GRACE.
* GRACE = slack + allowance. slack is 10 % of the cycle clamped to
  [10 min, 2 h]; allowance is 1.5 x the longest of the last ten successful
  durations (half a cycle when there is no history), never more than half a
  cycle. Both are bounded, so grace stays below a whole cycle for every cycle
  of an hour or more: a missed daily or weekly obligation cannot hide.
* Before the deadline an owed backup is `delayed`; after it, `stale`. Only
  `stale` is a Kuma DOWN. Attempts, retries, queue state and active runs
  choose the human-readable REASON; none of them moves the deadline or renews
  the success.

Nothing in this module reads the wall clock: callers pass `now`.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlmodel import Session, select

from ..models import Run, RunState, Task, TaskDue
from . import cadence, due

# States of the projection.
FRESH = "fresh"
NOT_YET_DUE = "not_yet_due"
DELAYED = "delayed"
STALE = "stale"
DISABLED = "disabled"
STATES = (FRESH, NOT_YET_DUE, DELAYED, STALE, DISABLED)

# What Kuma is told per state. `None` means "push nothing": the monitor keeps
# whatever it last knew and its own expiry is the dead-man backstop.
KUMA_STATUS = {FRESH: "up", NOT_YET_DUE: None, DELAYED: None, STALE: "down", DISABLED: None}

SLACK_FLOOR_SECONDS = 600
SLACK_CEILING_SECONDS = 7200
SLACK_FRACTION = 0.10
ALLOWANCE_MULTIPLIER = 1.5
# Successful durations considered when sizing the allowance.
DURATION_SAMPLES = 10
# Added to the Kuma backstop so a 25-hour DST day cannot expire the monitor
# before the projection (which walks real occurrences) has spoken.
KUMA_DST_MARGIN_SECONDS = 3600

# due.py status -> why the backup is still owed. Every production status has a
# row (tests derive the set from due.py).
DUE_STATUS_REASON = {
    due.DUE: "queued",
    due.DISPATCHED: "running",
    due.RETRY: "retrying",
    due.EXHAUSTED: "retries_exhausted",
    due.BLOCKED: "blocked",
}
# Terminal non-success RunStates and what they explain. pending/running are
# "active"; success is the thing being measured.
ATTEMPT_REASON = {
    RunState.failed: "last_attempt_failed",
    RunState.cancelled: "last_attempt_cancelled",
}


@dataclass(frozen=True)
class SuccessInfo:
    run_id: int
    started_at: datetime
    finished_at: datetime


@dataclass(frozen=True)
class Freshness:
    state: str
    reason: str
    kuma_status: Optional[str]
    # fresh: the NEXT obligation; delayed/stale: the occurrence that is owed;
    # not_yet_due/disabled: None.
    occurrence_at: Optional[datetime] = None
    deadline_at: Optional[datetime] = None
    last_success_run_id: Optional[int] = None
    last_success_started_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "kuma_status": self.kuma_status,
            "occurrence_at": self.occurrence_at,
            "deadline_at": self.deadline_at,
            "last_success_run_id": self.last_success_run_id,
            "last_success_started_at": self.last_success_started_at,
            "last_success_at": self.last_success_at,
        }


def _slack(gap_seconds: float) -> float:
    return min(max(gap_seconds * SLACK_FRACTION, SLACK_FLOOR_SECONDS), SLACK_CEILING_SECONDS)


def grace_seconds(gap_seconds: float, durations: Iterable[float] = ()) -> int:
    """Bounded grace for a cycle of `gap_seconds` (see module docstring)."""
    samples = [d for d in durations if d and d > 0]
    half = gap_seconds / 2
    allowance = min(ALLOWANCE_MULTIPLIER * max(samples), half) if samples else half
    return int(_slack(gap_seconds) + allowance)


def kuma_expiry_seconds(cron: str) -> int:
    """Kuma push-monitor interval: the backstop, never tighter than the projection.

    cadence + the LARGEST grace the projection can grant + a DST margin. The
    old value was exactly the cadence, so a normal run that took a little
    longer than the last one, or a 25-hour day, expired the monitor.
    """
    gap = cadence.cadence_seconds(cron)
    return int(gap + _slack(gap) + gap / 2 + KUMA_DST_MARGIN_SECONDS)


def _aware(value):
    return due.aware(value)


def project(
    *,
    cron: str,
    enabled: bool,
    now: datetime,
    epoch_at: datetime,
    last_success: Optional[SuccessInfo],
    success_durations: Iterable[float] = (),
    due_status: Optional[str] = None,
    active_run: bool = False,
    latest_attempt: Optional[tuple] = None,
) -> Freshness:
    """The projection. Pure: every input is a value the caller read."""
    now = _aware(now)
    identity = {}
    if last_success is not None:
        identity = dict(
            last_success_run_id=last_success.run_id,
            last_success_started_at=_aware(last_success.started_at),
            last_success_at=_aware(last_success.finished_at or last_success.started_at),
        )
    if not enabled:
        return Freshness(DISABLED, "disabled", KUMA_STATUS[DISABLED], **identity)
    if due._trigger(cron) is None:
        return Freshness(STALE, "invalid_schedule", KUMA_STATUS[STALE], **identity)

    epoch = _aware(epoch_at)
    started = None if last_success is None else _aware(last_success.started_at)
    after = epoch if started is None else max(epoch, started + due.TICK_EARLY_TOLERANCE)
    first = due.next_occurrence(cron, after)

    if first is None or first > now:
        if last_success is None:
            return Freshness(NOT_YET_DUE, "no_history", KUMA_STATUS[NOT_YET_DUE])
        if started < epoch:
            # Nothing has succeeded under THIS schedule yet (edited/re-enabled).
            return Freshness(NOT_YET_DUE, "schedule_changed", KUMA_STATUS[NOT_YET_DUE], **identity)
        return Freshness(FRESH, "covered", KUMA_STATUS[FRESH], occurrence_at=first, **identity)

    following = due.next_occurrence(cron, first)
    # NOMINAL gap, never smaller than the cron's cadence: the repeated hour of
    # a fall-back day puts two occurrences of a daily cron one hour apart, and
    # sizing grace from that hour would flap a perfectly healthy daily task.
    spacing = (following - first).total_seconds() if following is not None else 0
    gap = max(spacing, cadence.cadence_seconds(cron))
    deadline = first + timedelta(seconds=grace_seconds(gap, success_durations))
    reason = _owed_reason(
        first, due_status, active_run, latest_attempt,
        default="never_succeeded" if last_success is None else "missed",
    )
    if now <= deadline:
        if reason in ("never_succeeded", "missed"):
            reason = "awaiting_start"
        return Freshness(DELAYED, reason, KUMA_STATUS[DELAYED], first, deadline, **identity)
    return Freshness(STALE, reason, KUMA_STATUS[STALE], first, deadline, **identity)


def _owed_reason(occurrence, due_status, active_run, latest_attempt, *, default) -> str:
    """Why an owed backup has not been delivered. Evidence only: never changes state."""
    if due_status == due.BLOCKED:
        return DUE_STATUS_REASON[due.BLOCKED]
    if due_status == due.EXHAUSTED:
        return DUE_STATUS_REASON[due.EXHAUSTED]
    if active_run or due_status == due.DISPATCHED:
        return DUE_STATUS_REASON[due.DISPATCHED]
    if due_status == due.RETRY:
        return DUE_STATUS_REASON[due.RETRY]
    if due_status == due.DUE:
        return DUE_STATUS_REASON[due.DUE]
    if latest_attempt is not None:
        state, started_at = latest_attempt
        if state in ATTEMPT_REASON and _aware(started_at) >= occurrence - due.TICK_EARLY_TOLERANCE:
            return ATTEMPT_REASON[state]
    return default


# ------------------------------------------------------------------ loader


def project_task(session: Session, task: Task, now: Optional[datetime] = None) -> Freshness:
    """Read the schema and project. The only place that touches the database."""
    now = now or due._now()
    successes = session.exec(
        select(Run)
        .where(Run.task_id == task.id, Run.state == RunState.success)
        .order_by(Run.finished_at.desc())
        .limit(DURATION_SAMPLES)
    ).all()
    last_success = None
    durations = []
    for index, run in enumerate(successes):
        finished = _aware(run.finished_at or run.started_at)
        durations.append((finished - _aware(run.started_at)).total_seconds())
        if index == 0:
            last_success = SuccessInfo(run.id, _aware(run.started_at), finished)
    active = session.exec(
        select(Run.id).where(Run.task_id == task.id, Run.state.in_([RunState.pending, RunState.running]))
    ).first() is not None
    attempt = session.exec(
        select(Run)
        .where(Run.task_id == task.id, Run.state.in_([RunState.failed, RunState.cancelled]))
        .order_by(Run.started_at.desc())
    ).first()
    row = session.get(TaskDue, task.id)
    return project(
        cron=task.cron,
        enabled=task.enabled,
        now=now,
        epoch_at=_aware(task.schedule_epoch_at or task.created_at),
        last_success=last_success,
        success_durations=durations,
        due_status=None if row is None else row.status,
        active_run=active,
        latest_attempt=None if attempt is None else (attempt.state, _aware(attempt.started_at)),
    )
