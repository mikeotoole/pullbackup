# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""One deterministic clock for every timestamp in the due-work tests.

Written RED against bb3395f94811 (PR48 review run170 finding 2).
`test_manual_success_pays_only_the_older_obligation...[retry-reconcile]` passed
or failed with the REAL second-of-minute: the injected `due._now` clock was
seeded from `datetime.now()` while `Run.started_at` came from the model's
default factory / the runner, i.e. the real clock, so a start near a minute
boundary (observed deterministically at second 59.7) reordered a run against an
occurrence. The reviewer's probes only reproduced it by sleeping to a chosen
second; these tests need no sleeping because the clock is pinned and the
boundary instant is injected directly.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pullbackup.models import Run, RunState, Task
from pullbackup.services import due, runner
from sqlmodel import Session, select

from test_due_work import (  # noqa: F401  (fixtures)
    clock, make_source, make_task, runs_for, sqlite_engine, transfer,
)

UTC = timezone.utc


def test_the_clock_fixture_is_pinned_and_independent_of_wall_time(clock):
    assert clock.now == datetime(2026, 11, 2, 12, 0, 30, tzinfo=UTC)


def test_run_rows_default_to_the_injected_clock_not_the_real_one(sqlite_engine, clock):
    clock.now = datetime(2026, 11, 2, 12, 0, 59, 700000, tzinfo=UTC)  # a minute boundary
    with Session(sqlite_engine) as s:
        s.add(Run(task_id=1))
        s.commit()
        run = s.exec(select(Run)).one()
    assert due.aware(run.started_at) == clock.now


@pytest.mark.asyncio
async def test_an_admitted_run_is_stamped_by_the_injected_clock(sqlite_engine, tmp_path, clock):
    clock.now = datetime(2026, 11, 2, 12, 0, 59, 700000, tzinfo=UTC)
    src = make_source(sqlite_engine)
    task = make_task(sqlite_engine, tmp_path, src, 0, clock.now)
    admission = await runner.admit_run(task)
    assert admission.accepted
    (run,) = runs_for(sqlite_engine, task)
    assert due.aware(run.started_at) == clock.now
