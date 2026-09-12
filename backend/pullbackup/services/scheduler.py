# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
import asyncio
import logging
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlmodel import Session, select

from ..config import settings
from ..db import engine
from ..models import Task, utcnow

log = logging.getLogger(__name__)
_scheduler: Optional[AsyncIOScheduler] = None

# When the scheduler last proved it was executing work.
#
# INCIDENT 2026-09-06..10: the container healthcheck probed HTTP and stayed
# green for four days while nothing was being scheduled, because "the app
# answers" and "the scheduler executes" are different facts and only the first
# had a probe. This is the second one.
_last_heartbeat: Optional[datetime] = None

HEARTBEAT_JOB_ID = "scheduler-heartbeat"

# How often the heartbeat job stamps `_last_heartbeat`.
#
# A minute: far tighter than any task cadence in this system, and one indexed
# no-op per minute costs nothing. The point is resolution — a signal that
# refreshed hourly could not tell an external monitor anything useful inside
# the window it is meant to catch.
HEARTBEAT_INTERVAL_SECONDS = 60

# Missed heartbeats before the scheduler is reported dead.
#
# Three, so a single skipped stamp — a busy event loop, a slow sweep, a
# container under momentary load — is not an outage. Two would flap; ten would
# take the reporting delay past the point of being useful.
STALE_HEARTBEAT_MULTIPLIER = 3


def _job_id(task_id: int) -> str:
    return f"task-{task_id}"


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=settings.tzinfo)
    return _scheduler


async def _execute(task_id: int):
    from .runner import admit_run, start_admitted_run
    try:
        admission = await admit_run(task_id)
        if not admission.accepted:
            log.info(
                "scheduled run denied task_id=%s reason=%s",
                task_id,
                admission.reason,
                extra={"task_id": task_id, "reason": admission.reason},
            )
            return
        await start_admitted_run(admission.accepted_run_id())
    except Exception:
        log.exception("scheduled task %s failed", task_id)


def upsert_job(task: Task) -> None:
    sched = get_scheduler()
    job_id = _job_id(task.id)
    sched.remove_job(job_id) if sched.get_job(job_id) else None
    if not task.enabled:
        return
    try:
        trigger = CronTrigger.from_crontab(task.cron, timezone=settings.tzinfo)
    except Exception as e:
        log.warning("task %s has invalid cron %r: %s", task.id, task.cron, e)
        return
    sched.add_job(
        _execute,
        trigger=trigger,
        args=[task.id],
        id=job_id,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )


def remove_job(task_id: int) -> None:
    sched = get_scheduler()
    if sched.get_job(_job_id(task_id)):
        sched.remove_job(_job_id(task_id))


def start() -> None:
    sched = get_scheduler()
    with Session(engine) as session:
        tasks = session.exec(select(Task).where(Task.enabled == True)).all()  # noqa: E712
        for t in tasks:
            try:
                trigger = CronTrigger.from_crontab(t.cron, timezone=settings.tzinfo)
                sched.add_job(
                    _execute,
                    trigger=trigger,
                    args=[t.id],
                    id=_job_id(t.id),
                    replace_existing=True,
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=60,
                )
            except Exception as e:
                log.warning("skipping task %s: %s", t.id, e)
    # The liveness probe is a JOB, deliberately, rather than a flag set beside
    # the scheduler. `AsyncIOScheduler.running` is set by `start()` and stays
    # true while the executor is wedged or the event loop is blocked — it was
    # true throughout the four-day incident. Only work the scheduler actually
    # performs is evidence that it performs work.
    sched.add_job(
        _heartbeat,
        trigger=IntervalTrigger(seconds=HEARTBEAT_INTERVAL_SECONDS),
        id=HEARTBEAT_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    sched.start()
    # Starting counts as a beat. Without it every deploy reports a dead
    # scheduler for the first interval, which would teach an operator to
    # ignore the signal in exactly the window a bad deploy needs it.
    _beat()


def _beat() -> None:
    global _last_heartbeat
    _last_heartbeat = utcnow()


async def _heartbeat() -> None:
    """The scheduler executing something, once a minute, on the record."""
    _beat()


def liveness() -> dict:
    """Whether the scheduler is still executing jobs, and how sure we are.

    `running` and `alive` are reported separately on purpose: they disagreed
    for four days in September, and collapsing them would rebuild the exact
    blind spot. `running` is APScheduler's own flag — it says `start()` was
    called and `shutdown()` was not. `alive` is the heartbeat, which is the
    only one of the two that a wedged executor can falsify.
    """
    sched = _scheduler
    beat = _last_heartbeat
    age = None if beat is None else (utcnow() - beat).total_seconds()
    stale_after = HEARTBEAT_INTERVAL_SECONDS * STALE_HEARTBEAT_MULTIPLIER
    return {
        "running": bool(sched is not None and sched.running),
        "alive": age is not None and age < stale_after,
        "last_heartbeat": None if beat is None else beat.isoformat(),
        "heartbeat_age_seconds": age,
        "heartbeat_interval_seconds": HEARTBEAT_INTERVAL_SECONDS,
        "stale_after_seconds": stale_after,
    }


def shutdown() -> None:
    sched = get_scheduler()
    if sched.running:
        sched.shutdown(wait=False)


def next_run_at(task: Task) -> Optional[datetime]:
    """When this task is scheduled to fire next, or None if it is not.

    None is a real answer, not an absence: the job is not registered with the
    scheduler at all, or it is registered on a scheduler that never started.
    `cadence.schedule_is_missed` treats that as the strongest missed-schedule
    signal there is — an enabled task with no future.

    `next_run_time` is read through `getattr` because APScheduler only fills
    that slot once the scheduler is running; on an unstarted one it is unset
    and a direct attribute read raises AttributeError. The old code did read it
    directly, and the suite carried a fixture stubbing this function out to
    avoid the raise. That was survivable while the value was cosmetic. It is
    not survivable now: this function decides whether a row is flagged, and a
    missed-schedule check that raises on a scheduler that never started would
    fail hardest in exactly the case it exists to report.
    """
    sched = get_scheduler()
    job = sched.get_job(_job_id(task.id))
    if job is None:
        return None
    return getattr(job, "next_run_time", None) or None


def next_run_iso(task: Task) -> Optional[str]:
    at = next_run_at(task)
    return None if at is None else at.isoformat()
