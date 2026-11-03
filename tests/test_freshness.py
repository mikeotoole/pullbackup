# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""The successful-backup freshness projection (services/freshness.py).

Written RED first: every test here failed with ModuleNotFoundError before the
module existed. The contract under test is docs/freshness.md.

Every timestamp comes from an injected clock or a literal; nothing reads the
wall clock, so DST folds and second-of-minute boundaries are deterministic.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pullbackup.models import Run, RunState, Task, TaskDue
from pullbackup.services import due, freshness, scheduler
from sqlmodel import Session

from test_due_work import (  # noqa: F401  (fixtures)
    clock, make_source, make_task, sqlite_engine,
)

UTC = timezone.utc
H = 3600
D = 86400


def at(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def ok(started: datetime, seconds: int = 600, run_id: int = 1) -> freshness.SuccessInfo:
    return freshness.SuccessInfo(
        run_id=run_id, started_at=started, finished_at=started + timedelta(seconds=seconds)
    )


def project(cron, now, *, success=None, epoch=None, durations=(), **kw):
    return freshness.project(
        cron=cron,
        enabled=kw.pop("enabled", True),
        now=now,
        epoch_at=epoch or at(2026, 1, 1),
        last_success=success,
        success_durations=durations,
        **kw,
    )


@pytest.fixture(autouse=True)
def denver(clock, monkeypatch):
    """The scheduler zone is $TZ; DST tests need a zone that has DST.

    Depends on `clock` so it runs AFTER it: the clock fixture pins TZ=UTC.
    """
    monkeypatch.setenv("TZ", "America/Denver")


# ----------------------------------------------------------- grace contract


@pytest.mark.parametrize(
    "gap,durations,expected",
    [
        # slack = clamp(10% of gap, 10 min, 2 h); allowance = min(1.5 x longest, gap/2)
        (60, (), 600 + 30),                    # per-minute: slack floor, allowance cap gap/2
        (H, (), 600 + 1800),                   # hourly, no history: slack floor + half cycle
        (H, (600,), 600 + 900),                # hourly, 10 min runs: 1.5 x 10 min
        (D, (H,), 7200 + 5400),                # daily: slack cap + 1.5 h
        (D, (40 * H,), 7200 + 12 * H),         # absurd duration: capped at half a cycle
        (7 * D, (H,), 7200 + 5400),            # weekly: slack still capped at 2 h
        (D, (), 7200 + 12 * H),                # first ever run: benefit of the doubt = half a cycle
    ],
)
def test_grace_is_slack_plus_measured_duration_allowance_both_bounded(gap, durations, expected):
    assert freshness.grace_seconds(gap, durations) == expected


def test_grace_never_reaches_a_whole_cycle_for_cycles_above_the_slack_floor():
    """The bound that keeps a missed daily/weekly obligation visible."""
    for gap in (H, 6 * H, D, 7 * D, 30 * D):
        for durations in ((), (60,), (H,), (10 * D,)):
            assert freshness.grace_seconds(gap, durations) < gap


def test_only_the_longest_of_the_recent_samples_matters_and_garbage_is_ignored():
    assert freshness.grace_seconds(D, (H, 2 * H, -5, 0)) == freshness.grace_seconds(D, (2 * H,))


# ---------------------------------------------------- the lifecycle branches


def test_disabled_is_its_own_state_and_never_pushes():
    f = project("0 3 * * *", at(2027, 1, 1), enabled=False, success=ok(at(2026, 6, 1)))
    assert (f.state, f.reason, f.kuma_status) == ("disabled", "disabled", None)


def test_invalid_cron_is_stale_not_silently_fine():
    f = project("not a cron", at(2026, 6, 2), success=ok(at(2026, 6, 1)))
    assert (f.state, f.reason, f.kuma_status) == ("stale", "invalid_schedule", "down")


def test_a_disabled_task_with_an_invalid_cron_is_disabled():
    assert project("bogus", at(2026, 6, 2), enabled=False).state == "disabled"


def test_no_history_before_the_first_occurrence_is_not_yet_due():
    epoch = at(2026, 6, 1, 12)  # created 06:00 Denver
    f = project("0 3 * * *", at(2026, 6, 1, 13), epoch=epoch)
    assert (f.state, f.reason, f.kuma_status) == ("not_yet_due", "no_history", None)
    assert f.occurrence_at is None and f.last_success_at is None


def test_first_due_then_grace_then_stale_with_never_succeeded():
    epoch = at(2026, 6, 1, 12)
    first = at(2026, 6, 2, 9)  # 03:00 Denver (MDT, UTC-6)
    grace = freshness.grace_seconds(D, ())
    inside = project("0 3 * * *", first + timedelta(seconds=grace), epoch=epoch)
    assert (inside.state, inside.reason, inside.occurrence_at) == ("delayed", "awaiting_start", first)
    assert inside.deadline_at == first + timedelta(seconds=grace)
    after = project("0 3 * * *", first + timedelta(seconds=grace + 1), epoch=epoch)
    assert (after.state, after.reason, after.kuma_status) == ("stale", "never_succeeded", "down")


def test_a_success_older_than_the_current_schedule_does_not_make_a_new_schedule_fresh():
    """Re-enabled / re-scheduled: nothing owed yet, but nothing succeeded under THIS schedule."""
    f = project("0 3 * * sun", at(2026, 6, 3), epoch=at(2026, 6, 2), success=ok(at(2026, 5, 1)))
    assert (f.state, f.reason) == ("not_yet_due", "schedule_changed")
    assert f.last_success_at == at(2026, 5, 1) + timedelta(seconds=600)  # identity preserved


def test_fresh_between_occurrences_keeps_the_success_identity():
    s = ok(at(2026, 6, 2, 9, 0, 30), seconds=1800, run_id=77)
    f = project("0 3 * * *", at(2026, 6, 2, 20), success=s)
    assert (f.state, f.reason, f.kuma_status) == ("fresh", "covered", "up")
    assert (f.last_success_run_id, f.last_success_started_at, f.last_success_at) == (
        77, s.started_at, s.finished_at,
    )
    assert f.occurrence_at == at(2026, 6, 3, 9)  # the NEXT obligation, for the UI


def test_a_success_started_hair_early_still_covers_its_occurrence():
    s = ok(at(2026, 6, 2, 8, 59, 59, 500000))
    assert project("0 3 * * *", at(2026, 6, 2, 12), success=s).state == "fresh"
    # ...but not one second earlier than the tolerance: that was the PREVIOUS occurrence's run
    early = ok(at(2026, 6, 2, 8, 59, 58))
    assert project("0 3 * * *", at(2026, 6, 2, 12), success=early).state == "delayed"


def test_a_success_that_started_before_an_occurrence_does_not_pay_it():
    """A run spanning an occurrence: started 02:00, finished 05:00 Denver, daily 03:00."""
    s = ok(at(2026, 6, 2, 8), seconds=3 * H)
    f = project("0 3 * * *", at(2026, 6, 2, 11, 30), success=s)
    assert f.state == "delayed" and f.occurrence_at == at(2026, 6, 2, 9)


def test_missed_weekly_goes_stale_and_a_fresh_week_does_not():
    cron = "0 3 * * sun"  # Sundays 03:00 Denver (APScheduler numbers 0 as Monday)
    sunday = at(2026, 6, 7, 9)
    s = ok(sunday + timedelta(seconds=30), seconds=H)
    assert project(cron, sunday + timedelta(days=6), success=s).state == "fresh"
    grace = freshness.grace_seconds(7 * D, (H,))
    next_due = sunday + timedelta(days=7)
    assert project(cron, next_due + timedelta(seconds=grace), success=s, durations=(H,)).state == "delayed"
    stale = project(cron, next_due + timedelta(seconds=grace + 1), success=s, durations=(H,))
    assert (stale.state, stale.reason, stale.kuma_status) == ("stale", "missed", "down")


def test_missed_daily_is_stale_within_a_day_not_a_week():
    s = ok(at(2026, 6, 2, 9, 0, 30), seconds=H)
    f = project("0 3 * * *", at(2026, 6, 3, 9) + timedelta(seconds=7200 + 5400 + 1), success=s, durations=(H,))
    assert f.state == "stale"


# ----------------------------------------- variable runtimes, both DST folds


def _walk(cron, start, days, durations, probe_minutes=10):
    """Simulate `days` of back-to-back runs; return every state probed.

    Each run starts 30 s after its occurrence (the dispatcher tick). History
    handed to the projection is exactly what the loader would read: the last
    ten successful durations before the probe.
    """
    seen = []
    history: list[int] = [H]  # seed: one prior hour-long success
    success = ok(start, seconds=H, run_id=0)
    occurrence = due.next_occurrence(cron, success.started_at + timedelta(seconds=1))
    for k in range(days):
        run_start = occurrence + timedelta(seconds=30)
        duration = durations[k % len(durations)]
        finish = run_start + timedelta(seconds=duration)
        t = success.finished_at
        while t <= finish:
            f = freshness.project(
                cron=cron, enabled=True, now=t, epoch_at=start - timedelta(days=1),
                last_success=success, success_durations=tuple(history[-10:]),
                active_run=t >= run_start,
            )
            seen.append((t, f))
            t += timedelta(minutes=probe_minutes)
        history.append(duration)
        success = ok(run_start, seconds=duration, run_id=k + 1)
        occurrence = due.next_occurrence(cron, success.started_at + timedelta(seconds=1))
    return seen


@pytest.mark.parametrize(
    "label,cron,start",
    [
        ("plain", "0 3 * * *", at(2026, 6, 1, 9)),
        ("spring-forward-2026-03-08", "30 2 * * *", at(2026, 3, 1, 9)),
        ("fall-back-2026-11-01", "30 1 * * *", at(2026, 10, 25, 7)),
        ("fall-back-hourly-repeats-the-hour", "30 * * * *", at(2026, 10, 31, 0, 30)),
    ],
)
def test_normal_variable_runtimes_and_dst_never_go_stale(label, cron, start):
    hourly = cron.startswith("30 *")
    # durations vary, but each stays within 1.5 x the longest recent one
    durations = [600, 1100, 400, 1300, 900, 1500] if hourly else [H, 4500, 1800, 5400, 3000, 5000]
    days = 80 if hourly else 30
    seen = _walk(cron, start, days, durations)
    assert len(seen) > 100
    stale = [(t, f.reason) for t, f in seen if f.state == "stale"]
    assert stale == [], f"{label}: flapped to stale at {stale[:3]}"
    assert {f.state for _, f in seen} <= {"fresh", "delayed"}


def test_the_walk_actually_crosses_both_dst_transitions():
    """Guards the guard: the walks above must include the spring/fall days."""
    spring = _walk("30 2 * * *", at(2026, 3, 1, 9), 30, [H])
    days = {t.astimezone(freshness_zone()).date().isoformat() for t, _ in spring}
    assert "2026-03-08" in days
    fall = _walk("30 1 * * *", at(2026, 10, 25, 7), 30, [H])
    days = {t.astimezone(freshness_zone()).date().isoformat() for t, _ in fall}
    assert "2026-11-01" in days


def freshness_zone():
    from pullbackup.config import settings

    return settings.tzinfo


def test_a_run_longer_than_its_promised_grace_does_go_stale_and_names_running():
    """The bound is real: it is not 'running means fine'."""
    s = ok(at(2026, 6, 1, 9, 0, 30), seconds=H, run_id=1)
    o = at(2026, 6, 2, 9)
    grace = freshness.grace_seconds(D, (H,))
    f = project("0 3 * * *", o + timedelta(seconds=grace + 1), success=s, durations=(H,), active_run=True)
    assert (f.state, f.reason) == ("stale", "running")


def test_spring_forward_nonexistent_wall_time_uses_the_live_triggers_answer():
    """02:30 does not exist on 2026-03-08: the occurrence is whatever APScheduler emits."""
    s = ok(at(2026, 3, 7, 9, 30, 30))
    f = project("30 2 * * *", at(2026, 3, 8, 12), success=s)
    assert f.occurrence_at == due.next_occurrence("30 2 * * *", s.started_at + timedelta(seconds=1))
    assert f.occurrence_at is not None and at(2026, 3, 8, 8) < f.occurrence_at < at(2026, 3, 8, 11)


def test_fall_back_repeated_hour_yields_both_occurrences_in_order():
    cron = "30 1 * * *"
    s = ok(at(2026, 10, 31, 7, 30, 30))
    first = project(cron, at(2026, 11, 1, 7, 40), success=s).occurrence_at
    assert first == at(2026, 11, 1, 7, 30)  # 01:30 MDT, the first of the repeated hour
    after_first = ok(first + timedelta(seconds=30))
    second = project(cron, at(2026, 11, 1, 7, 40), success=after_first).occurrence_at
    assert second is None or second > first  # never a non-advancing instant


# ----------------------------------- attempts, scheduler, queue never renew


STALE_NOW = at(2026, 6, 3, 9) + timedelta(seconds=freshness.grace_seconds(D, (H,)) + 60)
LAST = ok(at(2026, 6, 1, 9, 0, 30), seconds=H, run_id=5)  # missed 06-02 03:00 Denver


def test_the_unchanged_stale_baseline_for_the_matrices_below():
    f = project("0 3 * * *", STALE_NOW, success=LAST, durations=(H,))
    assert f.state == "stale" and f.last_success_run_id == 5


@pytest.mark.parametrize("status", [None, due.DUE, due.DISPATCHED, due.RETRY, due.EXHAUSTED, due.BLOCKED])
@pytest.mark.parametrize("latest", [None, *RunState])
@pytest.mark.parametrize("active", [False, True])
def test_no_combination_of_attempt_queue_or_retry_state_can_mask_a_stale_backup(status, latest, active):
    """The whole production lifecycle vocabulary x every RunState x active/idle."""
    attempt = None if latest is None else (latest, at(2026, 6, 3, 9, 5))
    f = project(
        "0 3 * * *", STALE_NOW, success=LAST, durations=(H,),
        due_status=status, active_run=active, latest_attempt=attempt,
    )
    assert f.state == "stale", (status, latest, active)
    assert f.kuma_status == "down"
    # the last success is the unchanged identity: no attempt advanced it
    assert (f.last_success_run_id, f.last_success_started_at) == (5, LAST.started_at)


@pytest.mark.parametrize("status", [None, due.DUE, due.DISPATCHED, due.RETRY, due.EXHAUSTED, due.BLOCKED])
@pytest.mark.parametrize("latest", [None, *RunState])
def test_inside_the_grace_every_owed_combination_is_delayed_never_fresh_never_stale(status, latest):
    now = at(2026, 6, 2, 9, 30)
    attempt = None if latest is None else (latest, at(2026, 6, 2, 9, 5))
    f = project("0 3 * * *", now, success=LAST, durations=(H,), due_status=status, latest_attempt=attempt)
    assert f.state == "delayed" and f.kuma_status is None


def test_every_production_due_status_has_a_named_evidence_reason():
    """Derived from due.py's own constants, so a status added there fails here."""
    production = {v for k, v in vars(due).items() if k.isupper() and isinstance(v, str)}
    assert production == {"due", "dispatched", "retry", "exhausted", "blocked"}
    assert set(freshness.DUE_STATUS_REASON) == production


@pytest.mark.parametrize("bogus", ["", "invented", "DUE", "retry ", None])
def test_an_unknown_due_status_is_never_ignored_into_fresh(bogus):
    f = project("0 3 * * *", STALE_NOW, success=LAST, durations=(H,), due_status=bogus)
    assert f.state == "stale"


@pytest.mark.parametrize(
    "status,active,latest,reason",
    [
        (due.BLOCKED, False, None, "blocked"),
        (due.EXHAUSTED, False, None, "retries_exhausted"),
        (due.DISPATCHED, True, None, "running"),
        (None, True, None, "running"),
        (due.RETRY, False, RunState.failed, "retrying"),
        (due.DUE, False, None, "queued"),
        (None, False, RunState.failed, "last_attempt_failed"),
        (None, False, RunState.cancelled, "last_attempt_cancelled"),
        (None, False, None, "missed"),
    ],
)
def test_the_reason_names_why_it_is_owed(status, active, latest, reason):
    attempt = None if latest is None else (latest, at(2026, 6, 3, 9, 5))
    f = project("0 3 * * *", STALE_NOW, success=LAST, durations=(H,),
                due_status=status, active_run=active, latest_attempt=attempt)
    assert f.reason == reason


def test_a_failed_attempt_from_before_the_owed_occurrence_is_not_evidence():
    f = project("0 3 * * *", STALE_NOW, success=LAST, durations=(H,),
                latest_attempt=(RunState.failed, at(2026, 6, 1, 12)))
    assert f.reason == "missed"


def test_run_state_vocabulary_is_fully_classified():
    """A new RunState must be given a reason, not fall through to 'missed'."""
    assert set(freshness.ATTEMPT_REASON) == {RunState.failed, RunState.cancelled}
    non_terminal = {RunState.pending, RunState.running}
    assert set(RunState) == set(freshness.ATTEMPT_REASON) | non_terminal | {RunState.success}


def test_freshness_does_not_depend_on_the_scheduler_heartbeat(monkeypatch):
    before = project("0 3 * * *", STALE_NOW, success=LAST, durations=(H,))
    monkeypatch.setattr(scheduler, "_last_heartbeat", STALE_NOW)
    scheduler._beat()
    assert scheduler._last_heartbeat is not None
    assert project("0 3 * * *", STALE_NOW, success=LAST, durations=(H,)) == before


# ------------------------------------------------- loader against the schema


def _add_run(engine, task_id, state, started, seconds=600):
    with Session(engine) as s:
        run = Run(
            task_id=task_id, state=state, started_at=started,
            finished_at=None if state in (RunState.pending, RunState.running) else started + timedelta(seconds=seconds),
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        return run.id


def _task(engine, tmp_path, now, cron="0 3 * * *", **kw):
    src = make_source(engine)
    return make_task(engine, tmp_path, src, 0, now, cron=cron, **kw)


def _project(engine, task_id, now):
    with Session(engine) as s:
        return freshness.project_task(s, s.get(Task, task_id), now=now)


def test_loader_reads_last_success_ignoring_newer_failures_and_cancellations(sqlite_engine, tmp_path, clock):
    clock.now = at(2026, 6, 3, 9, 30)
    tid = _task(sqlite_engine, tmp_path, clock.now, schedule_baseline_at=at(2026, 6, 1))
    with Session(sqlite_engine) as s:
        t = s.get(Task, tid)
        t.created_at = at(2026, 5, 1)
        s.add(t)
        s.commit()
    good = _add_run(sqlite_engine, tid, RunState.success, at(2026, 6, 1, 9, 0, 30))
    for i in range(5):
        _add_run(sqlite_engine, tid, RunState.failed, at(2026, 6, 2, 9, 1 + i))
    _add_run(sqlite_engine, tid, RunState.cancelled, at(2026, 6, 2, 10))
    f = _project(sqlite_engine, tid, STALE_NOW)
    assert f.state == "stale" and f.last_success_run_id == good
    assert f.reason == "last_attempt_cancelled"


def test_loader_uses_the_clock_default_for_run_started_at(sqlite_engine, tmp_path, clock):
    """Model default timestamps come from the injected clock (no wall time)."""
    clock.now = at(2026, 6, 2, 9, 0, 40)
    tid = _task(sqlite_engine, tmp_path, clock.now)
    with Session(sqlite_engine) as s:
        s.add(Run(task_id=tid, state=RunState.success, finished_at=clock.now + timedelta(seconds=60)))
        s.commit()
    clock.now = at(2026, 6, 2, 20)
    with Session(sqlite_engine) as s:
        t = s.get(Task, tid)
        t.created_at = at(2026, 5, 1)
        s.add(t)
        s.commit()
    f = _project(sqlite_engine, tid, clock.now)
    assert f.state == "fresh" and f.last_success_started_at == at(2026, 6, 2, 9, 0, 40)


def test_loader_reads_due_row_and_active_run(sqlite_engine, tmp_path, clock):
    clock.now = STALE_NOW
    tid = _task(sqlite_engine, tmp_path, clock.now)
    with Session(sqlite_engine) as s:
        t = s.get(Task, tid)
        t.created_at = at(2026, 5, 1)
        s.add(t)
        s.add(TaskDue(task_id=tid, due_at=at(2026, 6, 2, 9), status=due.EXHAUSTED))
        s.commit()
    _add_run(sqlite_engine, tid, RunState.success, LAST.started_at)
    assert _project(sqlite_engine, tid, STALE_NOW).reason == "retries_exhausted"
    with Session(sqlite_engine) as s:
        s.delete(s.get(TaskDue, tid))
        s.commit()
    _add_run(sqlite_engine, tid, RunState.running, at(2026, 6, 3, 9, 1))
    assert _project(sqlite_engine, tid, STALE_NOW).reason == "running"


def test_schedule_edit_moves_the_epoch_so_old_occurrences_are_not_owed(sqlite_engine, tmp_path, clock):
    clock.now = at(2026, 6, 2, 16)  # 10:00 Denver
    tid = _task(sqlite_engine, tmp_path, clock.now, cron="0 3 * * *")
    with Session(sqlite_engine) as s:
        t = s.get(Task, tid)
        t.created_at = at(2026, 5, 1)
        s.add(t)
        s.commit()
        t.cron = "0 9 * * *"  # 09:00 Denver, already past today
        due.on_task_edited(s, t, schedule_changed=True, now=clock.now)
        s.commit()
        assert due.aware(t.schedule_epoch_at) == clock.now
    f = _project(sqlite_engine, tid, clock.now + timedelta(minutes=30))
    assert f.state == "not_yet_due" and f.occurrence_at is None


def test_enable_edit_moves_the_epoch_too(sqlite_engine, tmp_path, clock):
    clock.now = at(2026, 6, 2, 16)
    tid = _task(sqlite_engine, tmp_path, clock.now, enabled=False)
    with Session(sqlite_engine) as s:
        t = s.get(Task, tid)
        t.enabled = True
        due.on_task_edited(s, t, schedule_changed=True, now=clock.now)
        s.commit()
        assert due.aware(t.schedule_epoch_at) == clock.now


def test_kuma_expiry_is_cadence_plus_the_largest_grace_plus_the_dst_margin():
    # daily: 24h + 2h slack + 12h allowance cap + 1h DST margin
    assert freshness.kuma_expiry_seconds("0 3 * * *") == D + 7200 + 12 * H + H
    assert freshness.kuma_expiry_seconds("garbage") == freshness.kuma_expiry_seconds("0 * * * *")
