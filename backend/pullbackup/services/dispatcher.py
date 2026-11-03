# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""Turns retained due work into runs, oldest first, as capacity frees.

`due.py` decides WHAT is owed. This decides WHEN it can start. The split
matters: a cron tick used to ask "may I run right now?" and treat "no" as the
end of the story. Now the tick only records the debt, and this pump is the one
place that asks the admission questions, repeatedly, until the answer is yes.

Admission is unchanged in substance and is deliberately evaluated here from
the database, not from in-process locks, so a decision to START is made under
the same `_admission_lock` as manual admission and task edits:

  * one active run per task and per source (source serialization by default),
  * at most `max_concurrent_runs` active runs,
  * no overlapping rsync destination / syncoid dataset (`_destination_key`).

A pending row is only created for work that can start immediately, and its
execution is started in the same step, so every pending row has an owner in
`_run_owners` and the orphan watchdog can never reap queued work: queued work
is a `TaskDue` row, not a `Run` row.

Fairness: rows are walked oldest-due first, and scopes claimed by an older
row that could not start are held for the rest of the pass, so a younger
sibling cannot jump it — while an unrelated younger task still proceeds
(no head-of-line blocking).
"""
import asyncio
import logging
from datetime import timedelta
from typing import Optional

from sqlmodel import Session, select

from ..config import settings

from ..models import Run, RunState, Source, Task, TaskDue
from . import due, fs, runner

log = logging.getLogger(__name__)

# How often the loop re-examines the queue when nothing woke it, and how often
# it rebuilds missed occurrences from the schedule. Wake-ups (a tick, a run
# finishing, an edit) make the common case immediate; these bound the rest.
PUMP_INTERVAL_SECONDS = 15
RECONCILE_INTERVAL_SECONDS = 60

_dispatcher: Optional[asyncio.Task[None]] = None
_wake: Optional[asyncio.Event] = None
_wake_loop: Optional[asyncio.AbstractEventLoop] = None


def wake() -> None:
    """Ask the pump to run soon. Safe to call from any context, never raises."""
    event, loop = _wake, _wake_loop
    if event is None or loop is None or loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(event.set)
    except RuntimeError:
        pass


def _dest_key(task: Task):
    return runner._destination_key(task)


async def dispatch_due() -> list[int]:
    """Start every due obligation that can start now. Returns started run ids."""
    started: list[int] = []
    async with runner._admission_lock:
        now = due._now()
        with Session(due._engine()) as session:
            due.settle_all(session, now)

            active = session.exec(
                select(Run).where(Run.state.in_([RunState.pending, RunState.running]))
            ).all()
            active_tasks: set[int] = set()
            active_sources: set[int] = set()
            dests: list[tuple[str, ...]] = []
            for run in active:
                task = session.get(Task, run.task_id)
                if task is None:
                    continue
                active_tasks.add(task.id)
                active_sources.add(task.source_id)
                try:
                    dests.append(_dest_key(task))
                except Exception:
                    log.warning("active run %s has no destination key", run.id)
            count = len(active)
            limit = settings.max_concurrent_runs

            rows = session.exec(select(TaskDue).order_by(TaskDue.due_at)).all()
            claimed_sources: set[int] = set()
            claimed_dests: list[tuple[str, ...]] = []
            for row in rows:
                if row.status != due.DISPATCHED:
                    owner = session.get(Task, row.task_id)
                    if owner is not None and not owner.enabled:
                        # Disabling stops NEW admission. Debt that outlived the
                        # disable (a tick that committed after it) is stale:
                        # drop it here, under the admission lock. A DISPATCHED
                        # row is left to its in-flight run, which is never killed.
                        session.delete(row)
                        continue
                if row.status == due.DISPATCHED or row.status in (due.EXHAUSTED, due.BLOCKED):
                    continue
                retry_at = due.aware(row.retry_at)
                if row.status == due.RETRY and retry_at is not None and retry_at > now:
                    continue
                task = session.get(Task, row.task_id)
                source = session.get(Source, task.source_id) if task else None
                if task is None or source is None:
                    continue
                # An unsafe or unusable destination is NOT dropped here: the
                # run is still created and fails in `_execute_run` with the
                # real error on a visible history row, exactly as before. We
                # just cannot compute an exclusion key for it (it never
                # reaches a subprocess, so it cannot overlap anything).
                key = None
                try:
                    runner.validate_destination(task, source)
                    key = _dest_key(task)
                except Exception:
                    key = None

                reason = None
                if task.id in active_tasks:
                    reason = "task_active"
                elif task.source_id in active_sources or task.source_id in claimed_sources:
                    reason = "source_busy"
                elif key is not None and any(
                    runner._conflicts(key, held) for held in (*dests, *claimed_dests)
                ):
                    reason = "destination_busy"
                elif count >= limit:
                    reason = "capacity"

                if reason is not None:
                    # An older blocked row keeps its scopes against younger ones.
                    claimed_sources.add(task.source_id)
                    if key is not None:
                        claimed_dests.append(key)
                    if row.wait_reason != reason:
                        row.wait_reason = reason
                        row.updated_at = now
                        session.add(row)
                    continue

                run = runner._new_pending_run(session, task)
                row.status, row.run_id, row.wait_reason = due.DISPATCHED, run.id, None
                row.retry_at, row.updated_at = None, now
                session.add(row)
                active_tasks.add(task.id)
                active_sources.add(task.source_id)
                if key is not None:
                    dests.append(key)
                count += 1
                started.append(run.id)
            session.commit()
        # Ownership is taken in the same critical section as the pending row.
        for run_id in started:
            runner.start_admitted_run(run_id)
    return started


async def _loop() -> None:
    global _wake, _wake_loop
    _wake = asyncio.Event()
    _wake_loop = asyncio.get_running_loop()
    loop = asyncio.get_running_loop()
    next_reconcile = 0.0
    while True:
        try:
            if loop.time() >= next_reconcile:
                due.reconcile_all()
                next_reconcile = loop.time() + RECONCILE_INTERVAL_SECONDS
            await dispatch_due()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("due dispatch pass failed; continuing")
        try:
            await asyncio.wait_for(_wake.wait(), timeout=PUMP_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
        _wake.clear()


def start_dispatcher() -> asyncio.Task[None]:
    global _dispatcher
    if _dispatcher is not None and not _dispatcher.done():
        return _dispatcher
    _dispatcher = asyncio.create_task(_loop())
    return _dispatcher


async def stop_dispatcher() -> None:
    global _dispatcher, _wake, _wake_loop
    dispatcher, _dispatcher = _dispatcher, None
    _wake = _wake_loop = None
    if dispatcher is None:
        return
    dispatcher.cancel()
    try:
        await dispatcher
    except asyncio.CancelledError:
        pass
