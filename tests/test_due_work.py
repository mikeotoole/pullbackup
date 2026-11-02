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
