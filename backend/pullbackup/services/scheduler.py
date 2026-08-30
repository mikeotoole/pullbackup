# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
import asyncio
import logging
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlmodel import Session, select

from ..config import settings
from ..db import engine
from ..models import Task

log = logging.getLogger(__name__)
_scheduler: Optional[AsyncIOScheduler] = None


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
    sched.start()


def shutdown() -> None:
    sched = get_scheduler()
    if sched.running:
        sched.shutdown(wait=False)


def next_run_iso(task: Task) -> Optional[str]:
    sched = get_scheduler()
    job = sched.get_job(_job_id(task.id))
    if not job or not job.next_run_time:
        return None
    return job.next_run_time.isoformat()
