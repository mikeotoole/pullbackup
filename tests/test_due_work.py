# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""A scheduled occurrence is a debt, and a debt is never silently dropped.

Written RED against 368365fe940b. Observed on the live instance on 2026-11-02:
task 8 (01:00), task 2 (04:30) and task 6 (05:00) each fired on time, were
denied at `source_active` by `admit_run`, and `scheduler._execute` returned —
so the occurrence existed nowhere afterwards and the backup simply did not
happen until the NEXT cron tick, a day later.

These tests drive the real SQLite schema, the real APScheduler cron trigger
and a real (fake-binary) subprocess. Time is injected through `due._now`
because the behaviour under test is "what is owed at instant T".
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers.cron import CronTrigger
from pullbackup import db
from pullbackup.models import Run, RunState, Source, Task, TaskDue
from pullbackup.services import due, dispatcher, runner, scheduler
from sqlmodel import Session, SQLModel, create_engine, select

TEST_HTTP_USERNAME = "pullback-test"
TEST_HTTP_PASSWORD = bytes(range(32)).hex()
WAIT = 5.0


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now = self.now + timedelta(**kw)


@pytest.fixture
def clock(monkeypatch):
    # The scheduler zone comes from $TZ; pin it so the suite does not depend on
    # the machine it runs on (the DST/zone tests set their own).
    monkeypatch.setenv("TZ", "UTC")
    c = Clock(datetime(2026, 11, 2, 12, 0, 30, tzinfo=timezone.utc))
    monkeypatch.setattr(due, "_now", c)
    return c


@pytest.fixture
def sqlite_engine(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pullbackup.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    for module in (db, runner, scheduler):
        monkeypatch.setattr(module, "engine", engine)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setattr(runner.settings, "data_dir", tmp_path)
    monkeypatch.setattr(runner.settings, "dest_roots", str(tmp_path))
    monkeypatch.setattr(runner.settings, "zfs_dest_roots", "cache/docker_remote")
    monkeypatch.setattr(runner.settings, "http_basic_username", TEST_HTTP_USERNAME)
    monkeypatch.setattr(runner.settings, "http_basic_password", TEST_HTTP_PASSWORD)
    monkeypatch.setattr(runner.settings, "max_concurrent_runs", 2)
    monkeypatch.setattr(runner, "_global_sem", asyncio.Semaphore(2))
    monkeypatch.setattr(runner, "_source_locks", {})
    monkeypatch.setattr(runner, "_active_destinations", {})
    monkeypatch.setattr(runner, "_destination_cv", None)
    monkeypatch.setattr(runner, "_destination_cv_loop", None)
    monkeypatch.setattr(runner, "_admission_lock", asyncio.Lock())
    monkeypatch.setattr(runner, "_execution_tasks", set())
    monkeypatch.setattr(runner, "_run_owners", {})
    monkeypatch.setattr(runner, "_watchdog", None)
    monkeypatch.setattr(dispatcher, "_dispatcher", None)
    runner.settings.log_dir.mkdir()
    return engine


@pytest.fixture
def transfer(tmp_path, monkeypatch):
    """A fake rsync that logs its start and blocks until released.

    Every released file is touched at teardown so a failing assertion cannot
    leave a spinning child behind (it would hang the loop teardown and hide
    the traceback).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    started = tmp_path / "started.log"
    release = tmp_path / "release"
    exit_code = tmp_path / "exit_code"
    script = bin_dir / "rsync"
    script.write_text(
        "#!/bin/sh\n"
        'echo "$$" >> "$PULLBACK_TEST_STARTED"\n'
        "i=0\n"
        'while [ ! -f "$PULLBACK_TEST_RELEASE" ] && [ $i -lt 3000 ]; do sleep 0.01; i=$((i+1)); done\n'
        'if [ -f "$PULLBACK_TEST_EXIT" ]; then exit "$(cat "$PULLBACK_TEST_EXIT")"; fi\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/bin:/usr/bin")
    monkeypatch.setenv("PULLBACK_TEST_STARTED", str(started))
    monkeypatch.setenv("PULLBACK_TEST_RELEASE", str(release))
    monkeypatch.setenv("PULLBACK_TEST_EXIT", str(exit_code))

    class T:
        def started(self) -> int:
            return len(started.read_text().split()) if started.exists() else 0

        def release(self):
            release.touch()

        def hold(self):
            release.unlink(missing_ok=True)

        def fail_with(self, code: int):
            exit_code.write_text(str(code))

    t = T()
    yield t
    release.touch()


def make_source(engine, name="source"):
    with Session(engine) as s:
        src = Source(name=name, user="b", host=f"{name}.example", ssh_key_path="/tmp/k")
        s.add(src)
        s.commit()
        s.refresh(src)
        return src.id


def make_task(engine, tmp_path, source_id, index, clock_now, cron="* * * * *", **fields):
    fields.setdefault("schedule_baseline_at", clock_now - timedelta(minutes=5))
    with Session(engine) as s:
        t = Task(
            name=f"task-{index}",
            source_id=source_id,
            remote_path=f"/remote/{index}",
            local_path=str(tmp_path / f"local-{index}"),
            cron=cron,
            **fields,
        )
        s.add(t)
        s.commit()
        s.refresh(t)
        return t.id


def runs_for(engine, task_id):
    with Session(engine) as s:
        return s.exec(select(Run).where(Run.task_id == task_id).order_by(Run.id)).all()


def debt(engine, task_id):
    with Session(engine) as s:
        return s.get(TaskDue, task_id)


async def until(predicate, timeout=WAIT):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached in time")


async def pump_until(predicate, timeout=WAIT):
    """Stand in for the dispatcher loop, which these tests do not start."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        await dispatcher.dispatch_due()
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached in time")


async def drain():
    await asyncio.gather(*list(runner._execution_tasks), return_exceptions=True)


# ---------------------------------------------------------------- slice A


@pytest.mark.asyncio
async def test_source_busy_cron_tick_runs_after_release_without_another_tick(
    sqlite_engine, tmp_path, clock, transfer
):
    src = make_source(sqlite_engine)
    first = make_task(sqlite_engine, tmp_path, src, 0, clock.now)
    second = make_task(sqlite_engine, tmp_path, src, 1, clock.now)

    await scheduler._execute(first)
    await until(lambda: transfer.started() == 1)
    await scheduler._execute(second)

    # Denied by the busy source, but the occurrence is RETAINED and says why.
    assert runs_for(sqlite_engine, second) == []
    owed = debt(sqlite_engine, second)
    assert owed is not None
    assert owed.status == "due"
    assert owed.wait_reason == "source_busy"

    transfer.release()
    await until(lambda: runs_for(sqlite_engine, first)[0].state == RunState.success)
    await dispatcher.dispatch_due()
    await until(
        lambda: runs_for(sqlite_engine, second)
        and runs_for(sqlite_engine, second)[0].state == RunState.success
    )
    await drain()
    assert debt(sqlite_engine, second) is None


@pytest.mark.asyncio
async def test_duplicate_ticks_and_racing_dispatchers_start_exactly_one_run(
    sqlite_engine, tmp_path, clock, transfer
):
    src = make_source(sqlite_engine)
    task = make_task(sqlite_engine, tmp_path, src, 0, clock.now)

    await asyncio.gather(
        scheduler._execute(task),
        scheduler._execute(task),
        dispatcher.dispatch_due(),
        dispatcher.dispatch_due(),
    )
    await until(lambda: transfer.started() >= 1)
    transfer.release()
    await drain()
    await dispatcher.dispatch_due()
    await drain()

    assert len(runs_for(sqlite_engine, task)) == 1
    assert transfer.started() == 1
    assert debt(sqlite_engine, task) is None


# ---------------------------------------------------------------- slice B


def set_state(engine, task_id, **fields):
    with Session(engine) as s:
        t = s.get(Task, task_id)
        for k, v in fields.items():
            setattr(t, k, v)
        s.add(t)
        s.commit()


def add_run(engine, task_id, state, finished, error=""):
    with Session(engine) as s:
        r = Run(
            task_id=task_id,
            state=state,
            started_at=finished - timedelta(minutes=1),
            finished_at=finished,
            error_message=error,
            log_filename=f"r{task_id}-{finished.timestamp()}.log",
        )
        s.add(r)
        s.commit()


@pytest.mark.asyncio
async def test_missed_occurrence_is_caught_up_once_at_startup(
    sqlite_engine, tmp_path, clock
):
    src = make_source(sqlite_engine)
    # Hourly task; last baseline three hours ago: three occurrences missed.
    task = make_task(
        sqlite_engine, tmp_path, src, 0, clock.now, cron="0 * * * *",
        schedule_baseline_at=clock.now - timedelta(hours=3),
    )
    assert due.reconcile_all() == 1
    owed = debt(sqlite_engine, task)
    assert owed is not None
    # Coalesced: the EARLIEST missed occurrence is kept, only one row exists.
    assert due.aware(owed.due_at) == datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    with Session(sqlite_engine) as s:
        assert len(s.exec(select(TaskDue)).all()) == 1
    # Reconciling again must not create more debt.
    due.reconcile_all()
    assert due.aware(debt(sqlite_engine, task).due_at) == datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_queued_work_survives_a_restart_and_then_runs(
    sqlite_engine, tmp_path, clock, transfer, monkeypatch
):
    src = make_source(sqlite_engine)
    first = make_task(sqlite_engine, tmp_path, src, 0, clock.now)
    second = make_task(sqlite_engine, tmp_path, src, 1, clock.now)
    await scheduler._execute(first)
    await until(lambda: transfer.started() == 1)
    await scheduler._execute(second)
    assert debt(sqlite_engine, second) is not None

    # "Restart": the process dies. Startup reconciliation fails the orphan.
    await runner.shutdown_execution_tasks()
    runner.reconcile_stale_runs()
    monkeypatch.setattr(runner, "_run_owners", {})
    transfer.release()

    assert debt(sqlite_engine, second) is not None  # the queue is on disk
    await pump_until(lambda: transfer.started() >= 2)
    await drain()
    assert any(r.state == RunState.success for r in runs_for(sqlite_engine, second))
    assert debt(sqlite_engine, second) is None


@pytest.mark.asyncio
async def test_oldest_debt_goes_first_and_an_unrelated_source_is_not_blocked(
    sqlite_engine, tmp_path, clock, transfer
):
    busy = make_source(sqlite_engine, "busy")
    other = make_source(sqlite_engine, "other")
    holder = make_task(sqlite_engine, tmp_path, busy, 0, clock.now)
    old = make_task(sqlite_engine, tmp_path, busy, 1, clock.now)
    young = make_task(
        sqlite_engine, tmp_path, busy, 2, clock.now,
        schedule_baseline_at=clock.now - timedelta(minutes=2),
    )
    free = make_task(sqlite_engine, tmp_path, other, 3, clock.now)

    await scheduler._execute(holder)
    await until(lambda: transfer.started() == 1)
    await scheduler._execute(old)
    clock.advance(seconds=1)
    await scheduler._execute(young)
    await scheduler._execute(free)

    # The unrelated source progressed while the busy one queued behind its holder.
    await until(lambda: len(runs_for(sqlite_engine, free)) == 1)
    assert runs_for(sqlite_engine, old) == []
    assert runs_for(sqlite_engine, young) == []
    assert debt(sqlite_engine, old).due_at < debt(sqlite_engine, young).due_at

    transfer.release()
    await pump_until(lambda: len(runs_for(sqlite_engine, old)) == 1)
    # `young` cannot overtake `old` on the same source.
    first_old = runs_for(sqlite_engine, old)[0].id
    assert not runs_for(sqlite_engine, young) or runs_for(sqlite_engine, young)[0].id > first_old
    await pump_until(lambda: len(runs_for(sqlite_engine, young)) == 1, timeout=10)
    await drain()


@pytest.mark.asyncio
async def test_global_maximum_of_two_is_respected_and_waiters_say_capacity(
    sqlite_engine, tmp_path, clock, transfer
):
    ids = []
    for i in range(4):
        src = make_source(sqlite_engine, f"s{i}")
        ids.append(make_task(sqlite_engine, tmp_path, src, i, clock.now))
    for task in ids:
        await scheduler._execute(task)
    await until(lambda: transfer.started() == 2)
    await asyncio.sleep(0.3)
    assert transfer.started() == 2
    waiting = [t for t in ids if not runs_for(sqlite_engine, t)]
    assert len(waiting) == 2
    assert {debt(sqlite_engine, t).wait_reason for t in waiting} == {"capacity"}
    transfer.release()
    await pump_until(lambda: transfer.started() == 4, timeout=10)
    await drain()


@pytest.mark.asyncio
async def test_overlapping_rsync_destinations_never_run_together(
    sqlite_engine, tmp_path, clock, transfer
):
    s1 = make_source(sqlite_engine, "a")
    s2 = make_source(sqlite_engine, "b")
    parent = make_task(sqlite_engine, tmp_path, s1, 0, clock.now)
    child = make_task(sqlite_engine, tmp_path, s2, 1, clock.now)
    set_state(sqlite_engine, child, local_path=str(tmp_path / "local-0" / "nested"))
    await scheduler._execute(parent)
    await until(lambda: transfer.started() == 1)
    await scheduler._execute(child)
    await asyncio.sleep(0.3)
    assert transfer.started() == 1
    assert debt(sqlite_engine, child).wait_reason == "destination_busy"
    transfer.release()
    await pump_until(lambda: transfer.started() == 2, timeout=10)
    await drain()


@pytest.mark.asyncio
async def test_overlapping_syncoid_datasets_never_run_together(
    sqlite_engine, tmp_path, clock, transfer, monkeypatch
):
    bin_dir = tmp_path / "bin"
    syncoid = bin_dir / "syncoid"
    syncoid.write_text((bin_dir / "rsync").read_text())
    syncoid.chmod(0o755)
    s1 = make_source(sqlite_engine, "a")
    s2 = make_source(sqlite_engine, "b")
    common = dict(task_type="syncoid")
    parent = make_task(sqlite_engine, tmp_path, s1, 0, clock.now, **common)
    child = make_task(sqlite_engine, tmp_path, s2, 1, clock.now, **common)
    set_state(sqlite_engine, parent, remote_path="pool/app", local_path="cache/docker_remote/app")
    set_state(sqlite_engine, child, remote_path="pool/db", local_path="cache/docker_remote/app/db")
    await scheduler._execute(parent)
    await until(lambda: transfer.started() == 1)
    await scheduler._execute(child)
    await asyncio.sleep(0.3)
    assert transfer.started() == 1
    assert debt(sqlite_engine, child).wait_reason == "destination_busy"
    transfer.release()
    await pump_until(lambda: transfer.started() == 2, timeout=10)
    await drain()


@pytest.mark.asyncio
async def test_queued_work_is_not_reaped_by_the_orphan_watchdog(
    sqlite_engine, tmp_path, clock, transfer, monkeypatch
):
    monkeypatch.setattr(runner, "ORPHAN_GRACE_SECONDS", 0)
    src = make_source(sqlite_engine)
    first = make_task(sqlite_engine, tmp_path, src, 0, clock.now)
    second = make_task(sqlite_engine, tmp_path, src, 1, clock.now)
    await scheduler._execute(first)
    await until(lambda: transfer.started() == 1)
    await scheduler._execute(second)
    await asyncio.sleep(0.05)
    assert await runner.sweep_orphaned_runs() == []
    assert debt(sqlite_engine, second).status == "due"
    transfer.release()
    await drain()


@pytest.mark.asyncio
async def test_failure_is_retried_with_capped_exponential_backoff_then_exhausted(
    sqlite_engine, tmp_path, clock, transfer
):
    assert [due.backoff_seconds(n) for n in range(1, 9)] == [60, 120, 240, 480, 960, 1920, 3600, 3600]
    src = make_source(sqlite_engine)
    task = make_task(sqlite_engine, tmp_path, src, 0, clock.now)
    transfer.fail_with(23)
    transfer.release()
    await scheduler._execute(task)
    await drain()
    owed = debt(sqlite_engine, task)
    assert owed.status == "retry"
    assert owed.attempts == 1
    assert due.aware(owed.retry_at) == clock.now + timedelta(seconds=60)
    assert "rsync exited 23" in owed.last_error

    # Not retried before retry_at, however many times the pump runs.
    await dispatcher.dispatch_due()
    await dispatcher.dispatch_due()
    assert len(runs_for(sqlite_engine, task)) == 1

    for attempt in range(2, 7):
        clock.advance(seconds=3601)
        await dispatcher.dispatch_due()
        await drain()
        assert len(runs_for(sqlite_engine, task)) == attempt
    owed = debt(sqlite_engine, task)
    assert owed.status == "exhausted"
    assert owed.retry_at is None
    clock.advance(days=1)
    await dispatcher.dispatch_due()
    await drain()
    assert len(runs_for(sqlite_engine, task)) == 6  # exhausted: no automatic run


@pytest.mark.asyncio
async def test_a_deliberate_cancel_is_not_retried(
    sqlite_engine, tmp_path, clock, transfer
):
    src = make_source(sqlite_engine)
    task = make_task(sqlite_engine, tmp_path, src, 0, clock.now)
    await scheduler._execute(task)
    await until(lambda: transfer.started() == 1)
    (run,) = runs_for(sqlite_engine, task)
    result = await runner.cancel_run(run.id)
    assert result.outcome == runner.CancelOutcome.cancelled
    await dispatcher.dispatch_due()
    await asyncio.sleep(0.2)
    assert len(runs_for(sqlite_engine, task)) == 1
    assert debt(sqlite_engine, task) is None


@pytest.mark.asyncio
async def test_a_tick_during_active_work_coalesces_into_one_follow_up(
    sqlite_engine, tmp_path, clock, transfer
):
    # Run rows are stamped with the REAL clock, so the injected one must start
    # at real "now" or the first run would look newer than the follow-up tick.
    clock.now = datetime.now(timezone.utc)
    src = make_source(sqlite_engine)
    task = make_task(sqlite_engine, tmp_path, src, 0, clock.now)
    await scheduler._execute(task)
    await until(lambda: transfer.started() == 1)
    clock.advance(minutes=1)
    await scheduler._execute(task)
    clock.advance(minutes=1)
    await scheduler._execute(task)
    assert debt(sqlite_engine, task).newer_due_at is not None
    transfer.release()
    await pump_until(lambda: len(runs_for(sqlite_engine, task)) == 2, timeout=10)
    await drain()
    await dispatcher.dispatch_due()
    await drain()
    assert len(runs_for(sqlite_engine, task)) == 2  # one follow-up, not two
    assert debt(sqlite_engine, task) is None


def test_new_task_waits_for_its_first_due_time(sqlite_engine, tmp_path, clock):
    src = make_source(sqlite_engine)
    with Session(sqlite_engine) as s:
        t = Task(name="new", source_id=src, remote_path="/r", local_path=str(tmp_path / "n"),
                 cron="0 3 * * *", created_at=clock.now)
        s.add(t)
        s.commit()
        s.refresh(t)
        tid = t.id
    due.reconcile_all()
    assert debt(sqlite_engine, tid) is None  # nothing owed before the first 03:00
    clock.advance(days=1)  # now Nov 3 12:00, past 03:00
    due.reconcile_all()
    assert debt(sqlite_engine, tid) is not None


def test_existing_task_without_baseline_catches_up_only_a_really_missed_cron(
    sqlite_engine, tmp_path, clock
):
    src = make_source(sqlite_engine)
    # Daily 03:00. Last run FINISHED 02 Nov 03:30 after a very long transfer:
    # finish age (8.5h) is not a missed cron. No debt.
    ok = make_task(sqlite_engine, tmp_path, src, 0, clock.now, cron="0 3 * * *", schedule_baseline_at=None)
    add_run(sqlite_engine, ok, RunState.success, datetime(2026, 11, 2, 3, 30, tzinfo=timezone.utc))
    # Daily 03:00 but last finished 31 Oct: 02 Nov 03:00 really was missed.
    missed = make_task(sqlite_engine, tmp_path, src, 1, clock.now, cron="0 3 * * *", schedule_baseline_at=None)
    add_run(sqlite_engine, missed, RunState.success, datetime(2026, 10, 31, 3, 30, tzinfo=timezone.utc))
    due.reconcile_all()
    assert debt(sqlite_engine, ok) is None
    assert debt(sqlite_engine, missed) is not None


def test_disabled_and_edited_schedules_do_not_backfill(sqlite_engine, tmp_path, clock):
    src = make_source(sqlite_engine)
    task = make_task(sqlite_engine, tmp_path, src, 0, clock.now, cron="0 * * * *",
                     schedule_baseline_at=clock.now - timedelta(hours=5))
    set_state(sqlite_engine, task, enabled=False)
    due.reconcile_all()
    assert debt(sqlite_engine, task) is None  # disabled: nothing accrues

    # Re-enable: explicit baseline now, no backfill of the disabled period.
    with Session(sqlite_engine) as s:
        t = s.get(Task, task)
        t.enabled = True
        s.add(t)
        s.commit()
        due.on_task_edited(s, t, schedule_changed=True)
    due.reconcile_all()
    assert debt(sqlite_engine, task) is None
    clock.advance(hours=1)
    due.reconcile_all()
    assert debt(sqlite_engine, task) is not None

    # Cron edit drops stale debt and re-baselines.
    with Session(sqlite_engine) as s:
        t = s.get(Task, task)
        t.cron = "30 4 * * *"
        s.add(t)
        s.commit()
        due.on_task_edited(s, t, schedule_changed=True)
    assert debt(sqlite_engine, task) is None


def test_weekday_numbering_matches_the_live_apscheduler_job(sqlite_engine, tmp_path, clock):
    """Catch-up must agree with the live job about which day `1` means.

    APScheduler's day_of_week 0 is Monday; croniter's 0 is Sunday. Occurrences
    are computed from the same CronTrigger the scheduler registers.
    """
    src = make_source(sqlite_engine)
    # Whatever weekday APScheduler's numbering gives `1` (it is NOT cron's),
    # the debt must be for exactly the instant the live job would fire.
    task = make_task(sqlite_engine, tmp_path, src, 0, clock.now, cron="0 3 * * 1",
                     schedule_baseline_at=datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc))
    due.reconcile_all()
    live = CronTrigger.from_crontab("0 3 * * 1", timezone=runner.settings.tzinfo)
    base = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    expected = live.get_next_fire_time(None, base)
    assert due.aware(debt(sqlite_engine, task).due_at) == expected.astimezone(timezone.utc)


def test_dst_transition_yields_one_occurrence_per_wall_clock_day(monkeypatch):
    tz = ZoneInfo("America/Denver")
    monkeypatch.setenv("TZ", "America/Denver")
    # 2026-11-01 02:00 MDT->MST fallback. A 02:30 daily task fires once that day.
    start = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
    seen = []
    cursor = start
    for _ in range(3):
        cursor = due.next_occurrence("30 2 * * *", cursor)
        seen.append(cursor.astimezone(tz).date())
    assert len(set(seen)) == 3


@pytest.mark.asyncio
async def test_a_failed_run_does_not_advance_last_success(sqlite_engine, tmp_path, clock, transfer):
    src = make_source(sqlite_engine)
    task = make_task(sqlite_engine, tmp_path, src, 0, clock.now)
    ok_at = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)
    add_run(sqlite_engine, task, RunState.success, ok_at)
    transfer.fail_with(1)
    transfer.release()
    await scheduler._execute(task)
    await drain()
    with Session(sqlite_engine) as s:
        assert due.aware(due.last_success_at(s, task)) == ok_at


def test_init_db_adds_the_new_column_and_table_to_an_existing_database(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    old = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    with old.connect() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE task (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, source_id INTEGER NOT NULL, "
            "remote_path VARCHAR, local_path VARCHAR, cron VARCHAR, enabled BOOLEAN, description VARCHAR, "
            "task_type VARCHAR NOT NULL DEFAULT 'rsync', created_at DATETIME, updated_at DATETIME)"
        )
        conn.commit()
    monkeypatch.setattr(db, "engine", old)
    monkeypatch.setattr(runner.settings, "data_dir", tmp_path)
    db.init_db()
    with old.connect() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(task)").fetchall()}
        tables = {r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "schedule_baseline_at" in cols
    assert "taskdue" in tables
