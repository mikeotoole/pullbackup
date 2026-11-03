# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""The occurrence walk must follow APScheduler's own sequence through a DST fold.

Written RED against bb3395f94811 (PR48 review run170 finding 1).
`due.next_occurrence` fed `CronTrigger.get_next_fire_time(after, after)` a UTC
`after`. During the repeated fall-back hour that returns the SAME instant
(hourly after 2026-11-01T05:00Z -> 05:00Z; daily 01:30 after 05:30Z -> 05:30Z),
so `_latest_occurrence` re-fed it MAX_OCCURRENCE_SCAN (20000) times, ~2 s per
lookup, on the synchronous scheduler path.

Contract: the walk is strictly increasing, equals the live trigger's own
chained sequence (both fold occurrences included, nonexistent spring-forward
times as APScheduler resolves them), and a non-advancing answer is detected in
O(1) trigger calls instead of being spun on. Cost is asserted as a trigger CALL
COUNT, never as wall time.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers.cron import CronTrigger
from pullbackup.models import TaskDue
from pullbackup.services import due, scheduler
from sqlmodel import Session

from test_due_work import (  # noqa: F401  (fixtures)
    clock, debt, make_source, make_task, runs_for, sqlite_engine, transfer,
)

UTC = timezone.utc
NY = "America/New_York"

WALKS = [
    pytest.param("0 * * * *", datetime(2026, 11, 1, 3, 0, tzinfo=UTC),
                 datetime(2026, 11, 1, 12, 0, tzinfo=UTC), id="fall-back-hourly"),
    pytest.param("30 1 * * *", datetime(2026, 10, 30, 12, 0, tzinfo=UTC),
                 datetime(2026, 11, 3, 12, 0, tzinfo=UTC), id="fall-back-0130"),
    pytest.param("*/20 * * * *", datetime(2026, 11, 1, 3, 0, tzinfo=UTC),
                 datetime(2026, 11, 1, 9, 0, tzinfo=UTC), id="fall-back-20min"),
    pytest.param("0 * * * *", datetime(2026, 3, 8, 3, 0, tzinfo=UTC),
                 datetime(2026, 3, 8, 12, 0, tzinfo=UTC), id="spring-forward-hourly"),
    pytest.param("30 2 * * *", datetime(2026, 3, 6, 12, 0, tzinfo=UTC),
                 datetime(2026, 3, 11, 12, 0, tzinfo=UTC), id="spring-forward-nonexistent-0230"),
]


def live_sequence(cron, start, end):
    """What APScheduler itself will emit: each fire chained from the previous."""
    trigger = CronTrigger.from_crontab(cron, timezone=ZoneInfo(NY))
    out, fire = [], trigger.get_next_fire_time(None, start)
    while fire is not None and fire.astimezone(UTC) <= end:
        out.append(fire.astimezone(UTC))
        fire = trigger.get_next_fire_time(fire, fire)
    return out


def walk(cron, start, end):
    out, cur = [], start
    for _ in range(200):
        nxt = due.next_occurrence(cron, cur)
        if nxt is None or nxt > end:
            break
        out.append(nxt)
        assert nxt > cur, f"next_occurrence did not advance: {cur} -> {nxt}"
        cur = nxt
    return out


@pytest.mark.parametrize("cron,start,end", WALKS)
def test_walk_is_strictly_increasing_and_equals_the_live_trigger_sequence(
    monkeypatch, cron, start, end
):
    monkeypatch.setenv("TZ", NY)
    expected = [f for f in live_sequence(cron, start, end) if f > start]
    assert expected, "probe window produced no live fires (vacuous)"
    assert walk(cron, start, end) == expected


def test_both_occurrences_of_a_repeated_hour_are_walked(monkeypatch):
    monkeypatch.setenv("TZ", NY)
    hourly = walk("0 * * * *", datetime(2026, 11, 1, 4, 0, tzinfo=UTC), datetime(2026, 11, 1, 7, 0, tzinfo=UTC))
    assert hourly == [datetime(2026, 11, 1, h, 0, tzinfo=UTC) for h in (5, 6, 7)]
    daily = walk("30 1 * * *", datetime(2026, 11, 1, 4, 0, tzinfo=UTC), datetime(2026, 11, 1, 8, 0, tzinfo=UTC))
    assert daily == [datetime(2026, 11, 1, 5, 30, tzinfo=UTC), datetime(2026, 11, 1, 6, 30, tzinfo=UTC)]


class CountingTrigger:
    """Wraps the real trigger, counting calls; optionally pins the answer."""

    def __init__(self, real, pinned=None):
        self.real, self.pinned, self.calls = real, pinned, 0

    def get_next_fire_time(self, previous, now):
        self.calls += 1
        return self.pinned if self.pinned is not None else self.real.get_next_fire_time(previous, now)


def install(monkeypatch, cron, pinned=None):
    counter = CountingTrigger(CronTrigger.from_crontab(cron, timezone=ZoneInfo(NY)), pinned)
    monkeypatch.setattr(due, "_trigger", lambda _cron: counter)
    return counter


def test_latest_occurrence_through_the_fold_is_bounded_by_calls_not_the_scan_cap(monkeypatch):
    monkeypatch.setenv("TZ", NY)
    counter = install(monkeypatch, "0 * * * *")
    first = datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
    now = datetime(2026, 11, 1, 9, 30, tzinfo=UTC)
    assert due._latest_occurrence("0 * * * *", first, now) == datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
    assert counter.calls <= 10, counter.calls


def test_a_non_advancing_trigger_answer_is_detected_not_spun_on(monkeypatch):
    monkeypatch.setenv("TZ", NY)
    stuck = datetime(2026, 11, 1, 5, 0, tzinfo=UTC)
    counter = install(monkeypatch, "0 * * * *", pinned=stuck)
    result = due._latest_occurrence("0 * * * *", stuck, datetime(2026, 11, 1, 9, 0, tzinfo=UTC))
    assert result == stuck, "watermark must stay at the last real occurrence, not jump to wallclock"
    assert counter.calls <= 3, counter.calls
    assert due.next_occurrence("0 * * * *", stuck) is None


def test_reconcile_through_the_fold_watermarks_the_actual_fire_not_now(
    sqlite_engine, tmp_path, clock, monkeypatch
):
    monkeypatch.setenv("TZ", NY)
    counter_calls = []
    real = CronTrigger.get_next_fire_time

    def counting(self, previous, now):
        counter_calls.append(1)
        return real(self, previous, now)

    monkeypatch.setattr(CronTrigger, "get_next_fire_time", counting)
    clock.now = datetime(2026, 11, 1, 5, 30, 20, tzinfo=UTC)  # inside the repeated 01:xx hour
    src = make_source(sqlite_engine)
    task = make_task(sqlite_engine, tmp_path, src, 0, clock.now, cron="0 * * * *",
                     schedule_baseline_at=datetime(2026, 11, 1, 3, 30, tzinfo=UTC))
    due.reconcile_all()
    with Session(sqlite_engine) as s:
        from pullbackup.models import Task
        assert due.aware(s.get(Task, task).schedule_baseline_at) == datetime(2026, 11, 1, 5, 0, tzinfo=UTC)
    assert len(counter_calls) <= 20, len(counter_calls)
    # Second 01:xx hour: startup folded 05:00Z; the live callback for 06:00Z follows.
    clock.now = datetime(2026, 11, 1, 6, 0, 0, 300000, tzinfo=UTC)
    due.record_tick(task)
    with Session(sqlite_engine) as s:
        assert due.aware(s.get(Task, task).schedule_baseline_at) == datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
    row = debt(sqlite_engine, task)
    assert row is not None and due.aware(row.due_at) == datetime(2026, 11, 1, 4, 0, tzinfo=UTC)  # earliest kept
    assert row.newer_due_at is None  # coalesced into the one obligation, no duplicate


def test_record_tick_in_the_fold_does_not_scan_on_every_callback(sqlite_engine, tmp_path, clock, monkeypatch):
    monkeypatch.setenv("TZ", NY)
    calls = []
    real = CronTrigger.get_next_fire_time
    monkeypatch.setattr(CronTrigger, "get_next_fire_time",
                        lambda self, p, n: (calls.append(1), real(self, p, n))[1])
    clock.now = datetime(2026, 11, 1, 5, 0, 0, 200000, tzinfo=UTC)
    src = make_source(sqlite_engine)
    task = make_task(sqlite_engine, tmp_path, src, 0, clock.now, cron="0 * * * *",
                     schedule_baseline_at=datetime(2026, 11, 1, 5, 0, tzinfo=UTC))
    due.record_tick(task)
    assert len(calls) <= 10, len(calls)


def test_long_offline_catch_up_stays_bounded_and_lands_on_the_last_occurrence(monkeypatch):
    monkeypatch.setenv("TZ", NY)
    counter = install(monkeypatch, "0 * * * *")
    first = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)
    now = datetime(2026, 11, 2, 12, 30, tzinfo=UTC)  # ~32 days of hourly ticks across the fall-back
    assert due._latest_occurrence("0 * * * *", first, now) == datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    assert counter.calls <= due.MAX_OCCURRENCE_SCAN
