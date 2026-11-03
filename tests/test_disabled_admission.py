# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""A disabled task, or a stale schedule generation, never admits new work.

Written RED against 45bc32b9dc7b (PR48 official review629). Disabling a task
must stop NEW scheduled/due admission while leaving a run that is already in
flight alone. Two holes:

  1. `dispatcher.dispatch_due` loaded the Task but never checked `enabled`, so
     any `TaskDue` row that outlived the disable (a stale tick committing after
     it) became a run.
  2. A callback from a replaced job (old cron, or a disable/enable cycle) ran
     `record_tick` against the NEW generation. The edit's fresh baseline makes
     the "tick is itself an occurrence" fallback fire, so the old generation
     resurrected a debt. `update_task` awaits between its commit and
     `upsert_job`, so the old job genuinely can run in that window.

Manual run policy (`admit_run`) is deliberately untouched.
"""
import asyncio

import pytest
from pullbackup.models import RunState, Task, TaskDue
from pullbackup.services import dispatcher, due, runner, scheduler
from sqlmodel import Session

from test_due_work import (  # noqa: F401  (fixtures)
    clock, debt, drain, make_source, make_task, pump_until, runs_for,
    sqlite_engine, transfer, until,
)


def _set_enabled(engine, task_id, enabled):
    """The same transaction shape as `PATCH /tasks/{id}` for an enable toggle."""
    with Session(engine) as s:
        t = s.get(Task, task_id)
        t.enabled = enabled
        s.add(t)
        s.flush()
        due.on_task_edited(s, t, schedule_changed=True)
        s.commit()


def _edit_cron(engine, task_id, cron):
    with Session(engine) as s:
        t = s.get(Task, task_id)
        t.cron = cron
        s.add(t)
        s.flush()
        due.on_task_edited(s, t, schedule_changed=True)
        s.commit()


def _stale_row(engine, task_id, status, now):
    """Debt left behind by a tick that committed after the disable."""
    with Session(engine) as s:
        t = s.get(Task, task_id)
        t.enabled = False
        s.add(t)
        s.add(TaskDue(
            task_id=task_id, due_at=now, status=status, updated_at=now,
            retry_at=now if status == due.RETRY else None,
        ))
        s.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [due.DUE, due.RETRY, due.EXHAUSTED, due.BLOCKED])
async def test_dispatch_never_admits_stale_debt_of_a_disabled_task(
    sqlite_engine, tmp_path, clock, transfer, status
):
    src = make_source(sqlite_engine)
    task = make_task(sqlite_engine, tmp_path, src, 0, clock.now)
    _stale_row(sqlite_engine, task, status, clock.now)

    assert await dispatcher.dispatch_due() == []
    await drain()

    assert runs_for(sqlite_engine, task) == []
    assert transfer.started() == 0
    # The stale debt is cleaned under the admission transaction, not left to
    # be re-examined (and to show up as owed work) on every later pass.
    assert debt(sqlite_engine, task) is None


@pytest.mark.asyncio
async def test_disabling_does_not_kill_or_orphan_the_run_already_in_flight(
    sqlite_engine, tmp_path, clock, transfer
):
    src = make_source(sqlite_engine)
    task = make_task(sqlite_engine, tmp_path, src, 0, clock.now)
    due.reconcile_all()
    await pump_until(lambda: transfer.started() == 1)
    [run] = runs_for(sqlite_engine, task)

    _set_enabled(sqlite_engine, task, False)
    assert await dispatcher.dispatch_due() == []

    # Still the same live run: not cancelled, not failed, no second one.
    assert runs_for(sqlite_engine, task)[0].state in (RunState.pending, RunState.running)
    transfer.release()
    await drain()
    [after] = runs_for(sqlite_engine, task)
    assert after.id == run.id and after.state == RunState.success
    assert debt(sqlite_engine, task) is None  # finishing did not resurrect debt
    assert await dispatcher.dispatch_due() == []


@pytest.mark.asyncio
async def test_re_enabling_rebaselines_instead_of_backfilling_the_disabled_interval(
    sqlite_engine, tmp_path, clock, transfer
):
    src = make_source(sqlite_engine)
    task = make_task(sqlite_engine, tmp_path, src, 0, clock.now)
    _set_enabled(sqlite_engine, task, False)
    clock.advance(hours=3)
    assert await dispatcher.dispatch_due() == []
    _set_enabled(sqlite_engine, task, True)

    due.reconcile_all()
    assert await dispatcher.dispatch_due() == []
    assert debt(sqlite_engine, task) is None
    assert runs_for(sqlite_engine, task) == []


@pytest.mark.asyncio
async def test_scheduled_tick_waits_for_a_lifecycle_mutation_in_progress(
    sqlite_engine, tmp_path, clock, transfer
):
    src = make_source(sqlite_engine)
    task = make_task(sqlite_engine, tmp_path, src, 0, clock.now)

    async with runner.lifecycle_mutation_lock():
        tick = asyncio.create_task(scheduler._execute(task))
        await asyncio.sleep(0.2)
        # An edit holds the lock: the tick must not write debt underneath it.
        assert debt(sqlite_engine, task) is None
        assert not tick.done()
        _set_enabled(sqlite_engine, task, False)
    await tick
    await drain()

    assert debt(sqlite_engine, task) is None
    assert runs_for(sqlite_engine, task) == []


@pytest.mark.asyncio
async def test_callback_of_a_replaced_job_after_a_cron_edit_creates_no_debt(
    sqlite_engine, tmp_path, clock, transfer
):
    src = make_source(sqlite_engine)
    task = make_task(sqlite_engine, tmp_path, src, 0, clock.now, cron="0 * * * *")
    with Session(sqlite_engine) as s:
        scheduler.upsert_job(s.get(Task, task))
    old = scheduler.get_scheduler().get_job(scheduler._job_id(task))

    # The edit (commit + upsert_job, as in `update_task`) completes; the old
    # job's callback, already past APScheduler's dispatch, runs afterwards.
    clock.advance(minutes=10)
    transfer.release()  # a failing assertion must not hang on a blocked child
    async with runner.lifecycle_mutation_lock():
        _edit_cron(sqlite_engine, task, "30 3 * * *")
        with Session(sqlite_engine) as s:
            scheduler.upsert_job(s.get(Task, task))
    await old.func(*old.args)
    await drain()

    assert debt(sqlite_engine, task) is None
    assert runs_for(sqlite_engine, task) == []


@pytest.mark.asyncio
async def test_callback_from_before_a_disable_enable_cycle_creates_no_debt(
    sqlite_engine, tmp_path, clock, transfer
):
    src = make_source(sqlite_engine)
    task = make_task(sqlite_engine, tmp_path, src, 0, clock.now, cron="0 * * * *")
    with Session(sqlite_engine) as s:
        scheduler.upsert_job(s.get(Task, task))
    old = scheduler.get_scheduler().get_job(scheduler._job_id(task))

    clock.advance(minutes=10)
    transfer.release()
    async with runner.lifecycle_mutation_lock():
        _set_enabled(sqlite_engine, task, False)
        with Session(sqlite_engine) as s:
            scheduler.upsert_job(s.get(Task, task))
    clock.advance(minutes=1)
    async with runner.lifecycle_mutation_lock():
        _set_enabled(sqlite_engine, task, True)
        with Session(sqlite_engine) as s:
            scheduler.upsert_job(s.get(Task, task))
    await old.func(*old.args)  # same task, same cron, but a dead generation
    await drain()

    assert debt(sqlite_engine, task) is None
    assert runs_for(sqlite_engine, task) == []


@pytest.mark.asyncio
async def test_the_current_jobs_callback_still_records_and_runs_its_tick(
    sqlite_engine, tmp_path, clock, transfer
):
    """The generation guard must not mute the live job."""
    src = make_source(sqlite_engine)
    task = make_task(sqlite_engine, tmp_path, src, 0, clock.now, cron="* * * * *")
    with Session(sqlite_engine) as s:
        scheduler.upsert_job(s.get(Task, task))
    job = scheduler.get_scheduler().get_job(scheduler._job_id(task))

    await job.func(*job.args)
    await until(lambda: transfer.started() == 1)
    transfer.release()
    await drain()
    assert len(runs_for(sqlite_engine, task)) == 1
