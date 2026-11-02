# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""Durable due-work: a cron occurrence is a debt that survives a busy source.

INCIDENT 2026-11-02. Three scheduled backups fired on time, were refused by
`admit_run` because a sibling on the same source was running, and the
scheduler returned. The occurrence existed nowhere afterwards, so the backup
waited a whole cadence for the next tick. This module is where an occurrence
is written down BEFORE capacity or source admission is consulted, and where
it stays until a run satisfies it.

Model, in one paragraph. Each task has at most one `TaskDue` row. Overdue
occurrences COALESCE into it (the earliest is kept, because fairness is by
age); an occurrence arriving while a run is active becomes one follow-up
(`newer_due_at`), not a replay. A per-task watermark
(`Task.schedule_baseline_at`) records which occurrences are already accounted
for, so startup and the periodic reconciliation can rebuild what was missed
without replaying history and without inventing debt for a period in which the
task did not exist or was disabled.

Occurrences are computed with APScheduler's own `CronTrigger`, not croniter.
The live job is an APScheduler trigger and the two libraries number
`day_of_week` differently (0=Mon vs 0=Sun); catch-up that disagreed with the
live schedule about which weekday a task runs on would be a worse bug than
the one being fixed. DST follows for free.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.triggers.cron import CronTrigger
from sqlmodel import Session, select

from ..config import settings

from ..models import Run, RunState, Task, TaskDue, utcnow

log = logging.getLogger(__name__)

BACKOFF_INITIAL_SECONDS = 60
BACKOFF_CEILING_SECONDS = 3600
# Automatic retries after a failed run. The failure after the fifth retry
# leaves the debt `exhausted` until the next normal tick or an operator run.
MAX_AUTOMATIC_RETRIES = 5

DUE = "due"
DISPATCHED = "dispatched"
RETRY = "retry"
EXHAUSTED = "exhausted"
BLOCKED = "blocked"

# Failures that say nothing about the backup itself: the process died or the
# watchdog reclaimed the row, or the application was shutting down. They are
# requeued without spending a retry, because nothing was tried.
_INFRASTRUCTURE_FAILURES = (
    "interrupted by application restart",
    "run reclaimed by the watchdog",
    "run cancelled",  # state=failed only: shutdown, not an operator cancel
)

# Failures that retrying cannot fix: the stored configuration itself is
# invalid (destination outside the allowed roots, malformed dataset or host).
# These park the debt as `blocked` until the task is edited.
_PERMANENT_FAILURES = (
    "run owner failed: PathNotAllowed",
    "run owner failed: ValueError",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _engine():
    """The live engine, looked up per call so a swapped `db.engine` is honoured."""
    from .. import db

    return db.engine


def aware(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite drops tzinfo on read; the stored contract is UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def backoff_seconds(retry_number: int) -> int:
    """1m, 2m, 4m, ... capped at one hour. `retry_number` starts at 1."""
    return min(BACKOFF_INITIAL_SECONDS * 2 ** max(retry_number - 1, 0), BACKOFF_CEILING_SECONDS)


def _trigger(cron: str) -> Optional[CronTrigger]:
    try:
        return CronTrigger.from_crontab(cron, timezone=settings.tzinfo)
    except Exception:
        return None


def next_occurrence(cron: str, after: datetime) -> Optional[datetime]:
    """First occurrence strictly after `after`, by the live job's own trigger."""
    trigger = _trigger(cron)
    if trigger is None:
        return None
    after = aware(after)
    fire = trigger.get_next_fire_time(after, after)
    # Stored datetimes are naive UTC (SQLite drops tzinfo). The trigger answers
    # in the scheduler's zone; persisting that wall time unconverted would
    # shift every due time by the zone offset.
    return None if fire is None else fire.astimezone(timezone.utc)


MAX_OCCURRENCE_SCAN = 20000


def _latest_occurrence(cron: str, first: datetime, now: datetime) -> datetime:
    """Latest occurrence <= now, starting from `first` (itself <= now).

    Bounded: a per-minute task offline for a year is ~525k ticks, and walking
    them all would be the unbounded replay this module exists to avoid. Past
    the bound the watermark simply jumps to `now`; the debt is one coalesced
    obligation either way.
    """
    latest = first
    for _ in range(MAX_OCCURRENCE_SCAN):
        following = next_occurrence(cron, latest)
        if following is None or following > now:
            return latest
        latest = following
    return now


def derive_baseline(session: Session, task: Task) -> datetime:
    """Watermark for a row that predates the column.

    The end of the newest run: whatever it consumed is accounted for, and a
    cron occurrence after it that never produced a run was missed. FINISH time
    rather than start, so a legitimately long run does not turn the
    occurrences that fell inside it into debt the moment it ends.
    """
    last = session.exec(
        select(Run).where(Run.task_id == task.id).order_by(Run.started_at.desc())
    ).first()
    if last is not None:
        return aware(last.finished_at or last.started_at)
    return aware(task.created_at)


def reconcile_task(session: Session, task: Task, now: Optional[datetime] = None) -> Optional[TaskDue]:
    """Fold every occurrence in (baseline, now] into the task's one obligation."""
    now = now or _now()
    if not task.enabled:
        return None
    if _trigger(task.cron) is None:
        # A persisted cron that cannot be parsed would otherwise produce no
        # occurrences and no debt: a silent never-runs. Make it visible. The
        # write boundary does not repair rows that already exist.
        row = session.get(TaskDue, task.id)
        if row is None:
            row = TaskDue(task_id=int(task.id), due_at=now)
        if row.status != BLOCKED or "cron" not in (row.last_error or ""):
            row.status, row.wait_reason, row.retry_at = BLOCKED, None, None
            row.last_error = f"invalid cron expression {task.cron!r}: edit the task to fix its schedule"[:500]
            row.updated_at = now
            session.add(row)
            session.commit()
        return row
    baseline = aware(task.schedule_baseline_at)
    if baseline is None:
        baseline = derive_baseline(session, task)
    first = next_occurrence(task.cron, baseline)
    if first is None or first > now:
        # Nothing owed; keep the watermark honest so the next scan is cheap.
        if task.schedule_baseline_at is None or aware(task.schedule_baseline_at) < baseline:
            task.schedule_baseline_at = baseline
            session.add(task)
        return session.get(TaskDue, task.id)

    task.schedule_baseline_at = _latest_occurrence(task.cron, first, now)
    session.add(task)
    row = session.get(TaskDue, task.id)
    if row is None:
        row = TaskDue(task_id=task.id, due_at=first, status=DUE, updated_at=now)
    elif row.status == DISPATCHED:
        # Active work: one explicit follow-up, never a pile.
        if row.newer_due_at is None:
            row.newer_due_at = first
    elif row.status == EXHAUSTED:
        # "until the next normal schedule": a fresh occurrence re-arms it.
        row.status, row.attempts, row.retry_at, row.last_error = DUE, 0, None, ""
    # due / retry / blocked: already owed (or deliberately held); coalesce.
    row.updated_at = now
    session.add(row)
    session.commit()
    return row


def reconcile_all(now: Optional[datetime] = None) -> int:
    """Startup and periodic reconstruction. Returns how many tasks owe work."""
    owed = 0
    with Session(_engine()) as session:
        for task in session.exec(select(Task).where(Task.enabled == True)).all():  # noqa: E712
            try:
                row = reconcile_task(session, task, now)
            except Exception:
                session.rollback()
                log.exception("due reconcile failed for task %s", task.id)
                continue
            session.commit()
            owed += 1 if row is not None else 0
    return owed


def record_tick(task_id: int) -> None:
    """A cron tick. Records the debt before anything can refuse it.

    The tick IS an occurrence. If the schedule scan finds none owed (clock
    jitter: APScheduler fired a hair before the trigger's own instant, or the
    watermark already covers it) the tick still creates the obligation, unless
    one is already outstanding, in which case it coalesces into it.
    """
    now = _now()
    with Session(_engine()) as session:
        task = session.get(Task, task_id)
        if task is None or not task.enabled:
            return
        row = reconcile_task(session, task, now)
        if row is None:
            row = TaskDue(task_id=task.id, due_at=now, status=DUE, updated_at=now)
            task.schedule_baseline_at = max(aware(task.schedule_baseline_at) or now, now)
            session.add(task)
            session.add(row)
            session.commit()
        elif row.status == DISPATCHED and row.newer_due_at is None:
            row.newer_due_at = now
            session.add(row)
            session.commit()


# ------------------------------------------------------------------ outcomes


def apply_run_outcome(session: Session, row: TaskDue, run: Run, now: Optional[datetime] = None) -> None:
    """Settle one dispatched obligation from its run's terminal state.

    Called inside the transaction that terminalizes the run, so the debt is
    cleared atomically with the success that satisfied it, and again from the
    periodic settle for runs terminalized elsewhere (restart reconcile, the
    watchdog, an owner that raised).
    """
    now = now or _now()
    if run.state in (RunState.pending, RunState.running):
        return
    follow_up = row.newer_due_at
    if run.state == RunState.success:
        _finish(session, row, follow_up, now)
    elif run.state == RunState.cancelled:
        # A deliberate stop is not a failure to retry. Dropping the follow-up
        # as well keeps "cancel" from turning into an instant re-run.
        session.delete(row)
    else:  # failed
        message = run.error_message or ""
        if any(message.startswith(prefix) for prefix in _INFRASTRUCTURE_FAILURES):
            row.status, row.run_id, row.wait_reason = DUE, None, None
            row.retry_at = None
        elif any(marker in message for marker in _PERMANENT_FAILURES):
            # Invalid configuration: retrying cannot help. Visibly blocked
            # until the task is edited; the failed run row carries the detail.
            row.status, row.run_id, row.wait_reason = BLOCKED, None, None
            row.retry_at, row.last_error = None, message[:500]
        else:
            row.attempts += 1
            row.last_error = message[:500]
            row.run_id = None
            row.wait_reason = None
            if row.attempts > MAX_AUTOMATIC_RETRIES:
                row.status, row.retry_at = EXHAUSTED, None
            else:
                row.status = RETRY
                row.retry_at = now + timedelta(seconds=backoff_seconds(row.attempts))
        row.updated_at = now
        session.add(row)


def _finish(session: Session, row: TaskDue, follow_up: Optional[datetime], now: datetime) -> None:
    if follow_up is None:
        session.delete(row)
        return
    row.due_at, row.newer_due_at = follow_up, None
    row.status, row.attempts, row.retry_at = DUE, 0, None
    row.run_id, row.wait_reason, row.last_error, row.updated_at = None, None, "", now
    session.add(row)


def settle_all(session: Session, now: Optional[datetime] = None) -> None:
    """Reconcile every obligation against the run table."""
    now = now or _now()
    for row in session.exec(select(TaskDue)).all():
        if session.get(Task, row.task_id) is None:
            session.delete(row)
            continue
        if row.status == DISPATCHED:
            run = session.get(Run, row.run_id) if row.run_id is not None else None
            if run is None:
                row.status, row.run_id = DUE, None
                session.add(row)
            else:
                apply_run_outcome(session, row, run, now)
            continue
        if row.status in (DUE, RETRY, EXHAUSTED):
            # A run that succeeded after the occurrence (a manual one) pays the debt.
            paid = session.exec(
                select(Run).where(
                    Run.task_id == row.task_id,
                    Run.state == RunState.success,
                    Run.started_at >= row.due_at,
                )
            ).first()
            if paid is not None:
                _finish(session, row, row.newer_due_at, now)
    session.commit()


# ------------------------------------------------------------------ edits


def on_task_edited(session: Session, task: Task, *, schedule_changed: bool, now: Optional[datetime] = None) -> None:
    """An edit establishes an explicit baseline; it never backfills.

    Flushes only: the caller commits the task write and this invalidation in
    ONE transaction so a crash exposes the whole old or the whole new state."""
    now = now or _now()
    row = session.get(TaskDue, task.id)
    if schedule_changed or not task.enabled:
        task.schedule_baseline_at = now
        session.add(task)
        # Drop the old generation's obligation, DISPATCHED included. The
        # in-flight run is left alone (never killed), but with the row gone its
        # result has nothing to settle: it cannot retry, re-run, or satisfy debt
        # owed under a different schedule. Fresh debt starts a new row.
        if row is not None:
            session.delete(row)
    elif row is not None and row.status in (BLOCKED, EXHAUSTED):
        # Any other edit may have fixed what blocked it.
        row.status, row.attempts, row.retry_at, row.last_error = DUE, 0, None, ""
        row.updated_at = now
        session.add(row)
    session.flush()


def forget_task(session: Session, task_id: int) -> None:
    row = session.get(TaskDue, task_id)
    if row is not None:
        session.delete(row)


# ------------------------------------------------------------------ reads


def last_success_at(session: Session, task_id: int) -> Optional[datetime]:
    run = session.exec(
        select(Run)
        .where(Run.task_id == task_id, Run.state == RunState.success)
        .order_by(Run.finished_at.desc())
    ).first()
    return None if run is None else (run.finished_at or run.started_at)
