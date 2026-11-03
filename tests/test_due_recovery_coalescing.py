# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""Recovery debt is settled BEFORE a new occurrence is folded behind it (review630).

Written RED against 8959b9e. `reconcile_task` handed a DISPATCHED row to
`_note_occurrence_during_run` without asking whether its run was still alive.
After a restart/watchdog the run is already terminal, so a cron occurrence
folded into `newer_due_at` on a DEAD run; the retry then succeeded and `_finish`
resurrected that marker as a phantom follow-up run.

The lifecycle state is settled first; a follow-up is only ever derived from
work that is genuinely active.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pullbackup.models import Run, RunState, TaskDue
from pullbackup.services import dispatcher, due, runner
from sqlmodel import Session, select
from test_due_work import (  # noqa: F401  (fixtures + helpers)
    clock,
    debt,
    drain,
    make_source,
    make_task,
    runs_for,
    sqlite_engine,
    transfer,
    until,
)


def _dispatched(engine, task, *, state, error=None, started_at=None, newer_due_at=None, run=True):
    """Persist the state a process death leaves behind: a run + DISPATCHED debt."""
    with Session(engine) as s:
        run_id = None
        if run:
            r = Run(task_id=task, state=state, error_message=error)
            if started_at is not None:
                r.started_at = started_at
            if state not in (RunState.pending, RunState.running):
                r.finished_at = due._now()
            s.add(r)
            s.flush()
            run_id = r.id
        else:
            run_id = 987654  # points at a run that does not exist
        s.add(
            TaskDue(
                task_id=task,
                due_at=due._now(),
                status=due.DISPATCHED,
                run_id=run_id,
                newer_due_at=newer_due_at,
            )
        )
        s.commit()
        return run_id


def _restart(engine):
    assert runner.reconcile_stale_runs() == 1


async def _watchdog(engine):
    assert len(await runner.sweep_orphaned_runs()) == 1


def _setup(engine, tmp_path, clock):
    src = make_source(engine)
    return make_task(engine, tmp_path, src, 0, clock.now, schedule_baseline_at=clock.now)


async def _retry_to_success(engine, task, transfer, expected_runs):
    transfer.release()
    await dispatcher.dispatch_due()
    await drain()
    assert len(runs_for(engine, task)) == expected_runs
    assert runs_for(engine, task)[-1].state == RunState.success


@pytest.mark.asyncio
async def test_startup_recovered_run_occurrence_does_not_leave_phantom_followup(
    sqlite_engine, tmp_path, clock, transfer
):
    task = _setup(sqlite_engine, tmp_path, clock)
    _dispatched(sqlite_engine, task, state=RunState.running)
    _restart(sqlite_engine)  # the real startup reconciliation
    clock.advance(minutes=2)
    due.reconcile_all()

    row = debt(sqlite_engine, task)
    assert row.newer_due_at is None, "occurrence folded behind a run that is already dead"
    assert row.status == due.DUE

    await _retry_to_success(sqlite_engine, task, transfer, 2)
    assert debt(sqlite_engine, task) is None


@pytest.mark.asyncio
async def test_watchdog_reclaimed_run_occurrence_does_not_leave_phantom_followup(
    sqlite_engine, tmp_path, clock, transfer
):
    task = _setup(sqlite_engine, tmp_path, clock)
    _dispatched(
        sqlite_engine, task, state=RunState.running, started_at=due._now() - timedelta(hours=2)
    )
    await _watchdog(sqlite_engine)  # the real watchdog sweep
    clock.advance(minutes=2)
    due.reconcile_all()

    row = debt(sqlite_engine, task)
    assert row.newer_due_at is None
    assert row.status == due.DUE

    await _retry_to_success(sqlite_engine, task, transfer, 2)
    assert debt(sqlite_engine, task) is None


@pytest.mark.asyncio
async def test_normal_failure_backoff_occurrence_coalesces_into_the_retry(
    sqlite_engine, tmp_path, clock, transfer
):
    task = _setup(sqlite_engine, tmp_path, clock)
    _dispatched(sqlite_engine, task, state=RunState.failed, error="rsync exited 23")
    clock.advance(minutes=2)
    due.reconcile_all()

    row = debt(sqlite_engine, task)
    assert row.status == due.RETRY and row.attempts == 1
    assert row.newer_due_at is None
    assert row.retry_at is not None and due.aware(row.retry_at) > due._now() - timedelta(minutes=2)

    clock.advance(minutes=5)  # past the backoff
    await _retry_to_success(sqlite_engine, task, transfer, 2)
    assert debt(sqlite_engine, task) is None


@pytest.mark.asyncio
async def test_success_terminal_row_keeps_new_occurrence_as_exactly_one_debt(
    sqlite_engine, tmp_path, clock, transfer
):
    task = _setup(sqlite_engine, tmp_path, clock)
    _dispatched(sqlite_engine, task, state=RunState.success)
    clock.advance(minutes=2)
    due.reconcile_all()
    due.reconcile_all()  # idempotent

    row = debt(sqlite_engine, task)
    assert row is not None, "the paid run must not swallow the later occurrence"
    assert row.status == due.DUE and row.run_id is None
    assert row.newer_due_at is None  # it is the debt itself, not a follow-up

    await _retry_to_success(sqlite_engine, task, transfer, 2)
    assert debt(sqlite_engine, task) is None


@pytest.mark.asyncio
async def test_missing_run_row_is_requeued_without_a_followup(
    sqlite_engine, tmp_path, clock, transfer
):
    task = _setup(sqlite_engine, tmp_path, clock)
    _dispatched(sqlite_engine, task, state=None, run=False)
    clock.advance(minutes=2)
    due.reconcile_all()

    row = debt(sqlite_engine, task)
    assert row.status == due.DUE and row.run_id is None
    assert row.newer_due_at is None

    await _retry_to_success(sqlite_engine, task, transfer, 1)
    assert debt(sqlite_engine, task) is None


@pytest.mark.asyncio
async def test_cancelled_run_then_new_occurrence_is_one_fresh_debt(
    sqlite_engine, tmp_path, clock, transfer
):
    task = _setup(sqlite_engine, tmp_path, clock)
    _dispatched(sqlite_engine, task, state=RunState.cancelled, error="run cancelled by user")
    clock.advance(minutes=2)
    due.reconcile_all()

    row = debt(sqlite_engine, task)
    assert row is not None and row.status == due.DUE and row.newer_due_at is None

    await _retry_to_success(sqlite_engine, task, transfer, 2)
    assert debt(sqlite_engine, task) is None


@pytest.mark.asyncio
async def test_real_followup_recorded_before_death_does_not_replay_after_recovery(
    sqlite_engine, tmp_path, clock, transfer
):
    task = _setup(sqlite_engine, tmp_path, clock)
    _dispatched(
        sqlite_engine,
        task,
        state=RunState.running,
        newer_due_at=due._now() + timedelta(seconds=30),
    )
    _restart(sqlite_engine)
    clock.advance(minutes=2)
    due.reconcile_all()

    # One recovery run covers the original AND the earlier follow-up.
    await _retry_to_success(sqlite_engine, task, transfer, 2)
    assert debt(sqlite_engine, task) is None


@pytest.mark.asyncio
async def test_true_occurrence_during_the_retry_is_retained_exactly_once(
    sqlite_engine, tmp_path, clock, transfer
):
    task = _setup(sqlite_engine, tmp_path, clock)
    _dispatched(sqlite_engine, task, state=RunState.running)
    _restart(sqlite_engine)
    clock.advance(minutes=1)
    due.reconcile_all()
    transfer.hold()
    await dispatcher.dispatch_due()
    await until(lambda: transfer.started() == 1)  # the retry is genuinely active

    clock.advance(minutes=2)  # a real occurrence arrives behind live work
    due.reconcile_all()
    first = debt(sqlite_engine, task).newer_due_at
    assert first is not None
    for _ in range(3):  # repeated reconcile/tick/settle: no duplicate, no loss
        due.reconcile_all()
        due.record_tick(task)
        with Session(sqlite_engine) as s:
            due.settle_all(s, due._now())
    row = debt(sqlite_engine, task)
    assert row.status == due.DISPATCHED and row.newer_due_at == first

    transfer.release()
    await until(lambda: runs_for(sqlite_engine, task)[-1].state == RunState.success)
    await drain()
    await dispatcher.dispatch_due()  # settle: the follow-up is promoted, then runs
    await drain()
    runs = runs_for(sqlite_engine, task)
    assert len(runs) == 3, "the genuine occurrence must run exactly once"
    assert debt(sqlite_engine, task) is None


@pytest.mark.asyncio
async def test_record_tick_on_terminal_dispatched_row_does_not_fold_behind_dead_run(
    sqlite_engine, tmp_path, clock, transfer
):
    task = _setup(sqlite_engine, tmp_path, clock)
    _dispatched(sqlite_engine, task, state=RunState.running)
    _restart(sqlite_engine)
    due.record_tick(task)  # the other caller of _note_occurrence_during_run
    clock.advance(minutes=2)
    due.record_tick(task)

    row = debt(sqlite_engine, task)
    assert row is not None and row.newer_due_at is None
    with Session(sqlite_engine) as s:
        assert len(s.exec(select(TaskDue)).all()) == 1
