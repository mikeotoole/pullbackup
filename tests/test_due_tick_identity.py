# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""A cron tick carries an occurrence IDENTITY, not just a wallclock.

Written RED against 9c4850195b00 (PR48 review run166 finding 1). Periodic
reconcile / startup folds the 12:00 occurrence at 12:00:00.2, the dispatcher
starts the run, and APScheduler's own (slightly late) callback for that same
12:00 occurrence arrives at 12:00:00.4. `record_tick` used to treat the
callback's wallclock as a NEW occurrence and promised a follow-up, so one cron
time produced two runs.

Contract: one folded occurrence -> one run. A genuinely NEWER occurrence that
arrives while work is active -> exactly one promised follow-up, no matter how
many callbacks, reconciles, settles or restarts see it.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pullbackup.models import Run, TaskDue
from pullbackup.services import dispatcher, due, scheduler
from sqlmodel import Session

from test_due_work import (  # noqa: F401  (fixtures)
    clock, debt, drain, make_source, make_task, pump_until, runs_for,
    sqlite_engine, transfer, until,
)

UTC = timezone.utc
HOURLY = "0 * * * *"

# (zone, the folded occurrence as a UTC instant). The New York pair are the two
# DST boundaries of 2026: spring-forward 02:00 EST -> 03:00 EDT at 07:00Z, and
# fall-back 02:00 EDT -> 01:00 EST at 06:00Z (the 01:00 hour happens twice).
OCCURRENCES = [
    pytest.param("UTC", datetime(2026, 11, 2, 12, 0, tzinfo=UTC), id="utc"),
    pytest.param("America/New_York", datetime(2026, 3, 8, 7, 0, tzinfo=UTC), id="dst-forward"),
    pytest.param("America/New_York", datetime(2026, 11, 1, 7, 0, tzinfo=UTC), id="dst-back-after-repeat"),
]


def ms(base: datetime, millis: int) -> datetime:
    return base + timedelta(milliseconds=millis)


async def _fold_and_dispatch(engine, tmp_path, clock, transfer, monkeypatch, zone, occ, cron=HOURLY):
    """Reconcile folds `occ` at occ+0.2s and the dispatcher starts its run."""
    monkeypatch.setenv("TZ", zone)
    clock.now = ms(occ, 200)
    src = make_source(engine)
    task = make_task(engine, tmp_path, src, 0, clock.now, cron=cron,
                     schedule_baseline_at=occ - timedelta(minutes=30))
    due.reconcile_all()
    assert due.aware(task_baseline(engine, task)) == occ
    await dispatcher.dispatch_due()
    await until(lambda: transfer.started() == 1)
    assert debt(engine, task).status == due.DISPATCHED
    # Run rows are stamped by the real clock; pin the run to the injected one.
    with Session(engine) as s:
        for run in runs_for(engine, task):
            run = s.get(Run, run.id)
            run.started_at = ms(occ, 300)
            s.add(run)
        s.commit()
    return task


def task_baseline(engine, task_id):
    from pullbackup.models import Task
    with Session(engine) as s:
        return s.get(Task, task_id).schedule_baseline_at


@pytest.mark.asyncio
@pytest.mark.parametrize("zone,occ", OCCURRENCES)
async def test_late_callback_for_a_folded_occurrence_on_dispatched_work_makes_no_follow_up(
    sqlite_engine, tmp_path, clock, transfer, monkeypatch, zone, occ
):
    task = await _fold_and_dispatch(sqlite_engine, tmp_path, clock, transfer, monkeypatch, zone, occ)
    for millis in (400, 900, 1500):  # the callback, and repeats of it
        clock.now = ms(occ, millis)
        await scheduler._execute(task)
    assert debt(sqlite_engine, task).newer_due_at is None
    transfer.release()
    await drain()
    await dispatcher.dispatch_due()
    await drain()
    assert len(runs_for(sqlite_engine, task)) == 1, "one cron occurrence ran twice"
    assert debt(sqlite_engine, task) is None


@pytest.mark.asyncio
async def test_late_callback_for_a_folded_occurrence_does_not_survive_reconcile_or_restart(
    sqlite_engine, tmp_path, clock, transfer, monkeypatch
):
    occ = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    task = await _fold_and_dispatch(sqlite_engine, tmp_path, clock, transfer, monkeypatch, "UTC", occ)
    clock.now = ms(occ, 400)
    await scheduler._execute(task)
    for millis in (500, 5000, 60_000):  # periodic reconcile, then a restart's reconcile
        clock.now = ms(occ, millis)
        due.reconcile_all()
    assert debt(sqlite_engine, task).newer_due_at is None


@pytest.mark.asyncio
async def test_late_callback_for_a_folded_occurrence_on_queued_debt_stays_one_obligation(
    sqlite_engine, tmp_path, clock, transfer, monkeypatch
):
    monkeypatch.setenv("TZ", "UTC")
    occ = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    clock.now = ms(occ, 200)
    src = make_source(sqlite_engine)
    task = make_task(sqlite_engine, tmp_path, src, 0, clock.now, cron=HOURLY,
                     schedule_baseline_at=occ - timedelta(hours=1))
    due.reconcile_all()  # folded, not yet dispatched
    clock.now = ms(occ, 400)
    due.record_tick(task)
    row = debt(sqlite_engine, task)
    assert row.status == due.DUE and row.newer_due_at is None
    assert due.aware(row.due_at) == occ
    await dispatcher.dispatch_due()
    await until(lambda: transfer.started() == 1)
    transfer.release()
    await drain()
    await dispatcher.dispatch_due()
    await drain()
    assert len(runs_for(sqlite_engine, task)) == 1


@pytest.mark.asyncio
async def test_late_callback_for_a_folded_occurrence_during_a_manual_run_makes_no_follow_up(
    sqlite_engine, tmp_path, clock, transfer, monkeypatch
):
    from pullbackup.services import runner
    import asyncio

    monkeypatch.setenv("TZ", "UTC")
    occ = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    clock.now = ms(occ, 200)
    src = make_source(sqlite_engine)
    task = make_task(sqlite_engine, tmp_path, src, 0, clock.now, cron=HOURLY,
                     schedule_baseline_at=occ - timedelta(hours=1))
    due.reconcile_all()  # 12:00 folded into DUE debt
    assert debt(sqlite_engine, task).status == due.DUE
    transfer.hold()
    admission = await runner.admit_run(task)
    assert admission.accepted
    manual = asyncio.create_task(runner.execute_run(admission.run_id))
    await until(lambda: transfer.started() == 1)
    with Session(sqlite_engine) as s:  # run rows are stamped by the real clock
        run = s.get(Run, admission.run_id)
        run.started_at = ms(occ, 300)
        s.add(run)
        s.commit()
    clock.now = ms(occ, 400)
    await scheduler._execute(task)  # the callback for the SAME 12:00
    assert debt(sqlite_engine, task).newer_due_at is None
    transfer.release()
    await manual
    await drain()
    with Session(sqlite_engine) as s:
        due.settle_all(s)
    assert debt(sqlite_engine, task) is None, "the manual run paid 12:00; nothing newer exists"


@pytest.mark.asyncio
@pytest.mark.parametrize("zone,occ", OCCURRENCES)
async def test_a_genuinely_newer_occurrence_during_dispatched_work_gets_exactly_one_follow_up(
    sqlite_engine, tmp_path, clock, transfer, monkeypatch, zone, occ
):
    task = await _fold_and_dispatch(sqlite_engine, tmp_path, clock, transfer, monkeypatch, zone, occ)
    newer = due.next_occurrence(HOURLY, occ)
    assert newer > occ
    # The echo of the first occurrence, then the newer occurrence arrives by
    # tick, by repeated tick, by reconcile and by a restart's reconcile.
    clock.now = ms(occ, 400)
    await scheduler._execute(task)
    assert debt(sqlite_engine, task).newer_due_at is None
    clock.now = ms(newer, 300)
    await scheduler._execute(task)
    first = due.aware(debt(sqlite_engine, task).newer_due_at)
    assert first is not None and newer <= first <= ms(newer, 300)
    for millis in (500, 900):
        clock.now = ms(newer, millis)
        await scheduler._execute(task)
        due.reconcile_all()
    assert due.aware(debt(sqlite_engine, task).newer_due_at) == first
    transfer.release()
    await pump_until(lambda: len(runs_for(sqlite_engine, task)) == 2, timeout=10)
    await drain()
    await dispatcher.dispatch_due()
    await drain()
    assert len(runs_for(sqlite_engine, task)) == 2, "exactly one follow-up run"
    assert debt(sqlite_engine, task) is None


@pytest.mark.asyncio
async def test_a_tick_slightly_before_the_trigger_instant_is_the_next_occurrence_and_is_promised_once(
    sqlite_engine, tmp_path, clock, transfer, monkeypatch
):
    occ = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    task = await _fold_and_dispatch(sqlite_engine, tmp_path, clock, transfer, monkeypatch, "UTC", occ)
    newer = due.next_occurrence(HOURLY, occ)
    clock.now = newer - timedelta(milliseconds=100)  # jitter: a hair EARLY
    await scheduler._execute(task)
    first = debt(sqlite_engine, task).newer_due_at
    assert first is not None, "the early tick IS the 13:00 occurrence and must be promised"
    clock.now = ms(newer, 200)  # reconcile later sees 13:00 and must not count it again
    due.reconcile_all()
    clock.now = ms(newer, 400)
    await scheduler._execute(task)  # and the echo of that same occurrence
    assert debt(sqlite_engine, task).newer_due_at == first
    transfer.release()
    await pump_until(lambda: len(runs_for(sqlite_engine, task)) == 2, timeout=10)
    await drain()
    await dispatcher.dispatch_due()
    await drain()
    assert len(runs_for(sqlite_engine, task)) == 2
    assert debt(sqlite_engine, task) is None
