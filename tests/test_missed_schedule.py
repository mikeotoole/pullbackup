# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""A schedule that stopped firing must be visible without reading a log.

Written RED against 79c0666f, where `TaskOut` reports `last_run_state` and
nothing else about scheduling health.

INCIDENT 2026-09-06..10. For four days the task list showed every task's
`last_run_state` as `success`. That was *true* — those were the last COMPLETED
runs — and it was also the opposite of what was happening: nothing had executed
since the 6th. Run state describes the last run; it says nothing about whether
the scheduler is still firing. The only thing that noticed was a set of Uptime
Kuma push monitors going down, and the container healthcheck stayed green the
whole time because it probes HTTP and HTTP was fine.

Two signals are pinned here, deliberately separate because they fail
independently and answer different questions:

  1. **Per task: a missed schedule.** An enabled task whose `next_run` is
     materially in the past, with nothing in flight for it, has silently
     skipped its window. "Materially" is proportional to the task's own
     cadence — five minutes late means nothing to a daily backup and means the
     world to one that runs every two minutes — and bounded so that a daily
     task is still flagged well inside one cadence cycle.

  2. **Process-wide: scheduler liveness.** A heartbeat the scheduler itself
     must keep stamping. `AsyncIOScheduler.running` is not evidence: it is set
     by `start()` and stays true while the executor is wedged or the event loop
     is blocked. Only work the scheduler actually performs can prove it is
     performing work, which is why the probe is a job registered *in* the
     scheduler rather than a flag read beside it.

The two are not interchangeable. A single wedged task is invisible to the
process-wide heartbeat, and a genuinely dead scheduler flags every task at
once — an operator needs to be able to tell those apart.
"""
import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from pullbackup import db, main
from pullbackup.api import system as system_api
from pullbackup.api import tasks as tasks_api
from pullbackup.models import Run, RunState, Task, utcnow
from pullbackup.services import cadence, kuma, scheduler
from sqlmodel import Session

# Shared engine fixture and row factories.
from test_run_admission import (  # noqa: F401
    TEST_HTTP_PASSWORD,
    TEST_HTTP_USERNAME,
    create_source_tasks,
    sqlite_engine,
)

HOURLY = "0 * * * *"
DAILY = "0 3 * * *"
EVERY_MINUTE = "* * * * *"


# --------------------------------------------------------------------------
# 1. cadence and the grace window
# --------------------------------------------------------------------------


def test_cadence_is_read_from_the_cron_expression():
    assert cadence.cadence_seconds(EVERY_MINUTE) == 60
    assert cadence.cadence_seconds(HOURLY) == 3600
    assert cadence.cadence_seconds(DAILY) == 86400


def test_a_malformed_cron_falls_back_instead_of_raising():
    """A row with an unschedulable cron must not take the task list down."""
    assert cadence.cadence_seconds("not a cron") == cadence.DEFAULT_CADENCE_SECONDS
    assert cadence.cadence_seconds("") == cadence.DEFAULT_CADENCE_SECONDS


def test_the_grace_window_is_proportional_to_the_cadence():
    """Half a cycle: late enough to be real, early enough to be one cycle."""
    assert cadence.schedule_grace_seconds(HOURLY) == 1800


def test_the_grace_window_has_a_floor_so_a_tight_cron_is_not_noise():
    # Half of a one-minute cadence is 30s, which is inside ordinary tick jitter
    # and clock skew. The floor buys slack rather than flapping.
    assert cadence.schedule_grace_seconds(EVERY_MINUTE) == cadence.MIN_SCHEDULE_GRACE_SECONDS
    assert cadence.MIN_SCHEDULE_GRACE_SECONDS >= 60


def test_the_grace_window_has_a_ceiling_so_a_daily_task_is_flagged_in_time():
    """Half of a day is twelve hours — most of a cadence cycle spent silent.

    The card's acceptance is that a wedged scheduler is visible within one
    cadence cycle. An uncapped proportional window would not meet it for any
    task slower than a couple of hours.
    """
    assert cadence.schedule_grace_seconds(DAILY) == cadence.MAX_SCHEDULE_GRACE_SECONDS
    assert cadence.MAX_SCHEDULE_GRACE_SECONDS < 86400 / 2


def test_kuma_intervals_come_from_the_same_cadence_helper():
    """One definition of "how often does this task run", not two.

    `kuma.cron_interval_seconds` sized push-monitor intervals long before this
    card existed. Two independent implementations of the same question drift,
    and the drift would be invisible: the monitor interval and the missed-
    schedule window would quietly disagree about the same cron.
    """
    for cron in (EVERY_MINUTE, HOURLY, DAILY, "bad cron"):
        assert kuma.cron_interval_seconds(cron) == cadence.cadence_seconds(cron)


# --------------------------------------------------------------------------
# 2. the per-task missed-schedule signal
# --------------------------------------------------------------------------


def stale(seconds: int):
    return utcnow() - timedelta(seconds=seconds)


def ahead(seconds: int):
    return utcnow() + timedelta(seconds=seconds)


def test_a_next_run_past_the_grace_window_is_missed():
    assert cadence.schedule_is_missed(
        cron=HOURLY, enabled=True, next_run=stale(1801), has_active_run=False
    )


# --------------------------------------------------------------------------
# 2b. the signal that actually catches the September incident
# --------------------------------------------------------------------------
#
# MEASURED against the real app and real APScheduler (2026-09-12), reproducing
# the incident's mechanism: run 4773 stuck `running`, admission refusing every
# subsequent fire for that source at `source_active`, four days elapsed.
#
#   task 'scrutiny' next_run_time 2026-09-12 01:15  stale? False
#   task 'sibling'  next_run_time 2026-09-12 01:45  stale? False
#
# `next_run_time` NEVER went stale. APScheduler advances it on every tick
# whether the job body does real work or returns immediately, and `_execute`
# returned immediately for four days because admission denied it. The same
# holds for a coroutine that never returns: `max_instances=1` SKIPS the fire
# and still advances the trigger.
#
# So a stale-next_run check alone would have shown a clean task list through
# the entire outage — the exact failure this card exists to end, rebuilt. What
# DID detect the incident was Kuma: nothing had *happened* for four days. That
# is a fact about elapsed activity, and it is pinned below.
#
# The stale-next_run check is kept as well. It is not redundant: it catches the
# cases elapsed-activity cannot see instantly — a job never registered, a
# scheduler that never started, a trigger that stopped advancing — and it fires
# without waiting a full cadence cycle.


def test_a_task_that_has_not_run_in_over_a_cycle_is_missed():
    """The signal that would actually have caught September.

    An hourly task whose last run started four days ago has missed roughly
    ninety-five windows, regardless of what any trigger claims about the next
    one.
    """
    assert cadence.schedule_is_missed(
        cron=HOURLY,
        enabled=True,
        next_run=ahead(600),
        has_active_run=False,
        last_activity_at=stale(4 * 86400),
    )


def test_a_task_that_ran_within_its_cycle_is_not_missed():
    assert not cadence.schedule_is_missed(
        cron=HOURLY,
        enabled=True,
        next_run=ahead(600),
        has_active_run=False,
        last_activity_at=stale(600),
    )


def test_one_slightly_late_cycle_is_not_a_missed_schedule():
    """A cadence plus its grace window, not a cadence exactly.

    A task fires AT its cadence, so elapsed time crosses one cadence every
    single cycle by definition. Flagging at exactly one would mean every task
    in the system is flagged for the instant before each run — an alarm that
    is always on is an alarm nobody reads.
    """
    just_over = cadence.cadence_seconds(HOURLY) + 60
    assert not cadence.schedule_is_missed(
        cron=HOURLY,
        enabled=True,
        next_run=ahead(600),
        has_active_run=False,
        last_activity_at=stale(just_over),
    )


def test_a_task_is_flagged_within_one_cycle_of_going_silent():
    """The card's acceptance, measured.

    A daily task must not need two days to be reported. Cadence plus the
    capped grace window is 25 hours — inside its own next cycle.
    """
    window = cadence.cadence_seconds(DAILY) + cadence.schedule_grace_seconds(DAILY)
    assert window < 2 * 86400
    assert cadence.schedule_is_missed(
        cron=DAILY,
        enabled=True,
        next_run=ahead(600),
        has_active_run=False,
        last_activity_at=stale(window + 60),
    )


def test_an_unknown_last_activity_does_not_flag_anything():
    """Absent evidence is not evidence. A caller with nothing to report gets
    the next_run check alone rather than a fabricated fault."""
    assert not cadence.schedule_is_missed(
        cron=HOURLY,
        enabled=True,
        next_run=ahead(600),
        has_active_run=False,
        last_activity_at=None,
    )


def test_a_naive_last_activity_is_compared_without_raising():
    """SQLite drops tzinfo on read; every `Run.started_at` arrives naive."""
    naive = (stale(4 * 86400)).replace(tzinfo=None)
    assert cadence.schedule_is_missed(
        cron=HOURLY,
        enabled=True,
        next_run=ahead(600),
        has_active_run=False,
        last_activity_at=naive,
    )


def test_a_next_run_inside_the_grace_window_is_not_missed():
    """Late is not missed. A tick lands when it lands."""
    assert not cadence.schedule_is_missed(
        cron=HOURLY, enabled=True, next_run=stale(1799), has_active_run=False
    )


def test_a_future_next_run_is_not_missed():
    assert not cadence.schedule_is_missed(
        cron=HOURLY, enabled=True, next_run=ahead(600), has_active_run=False
    )


def test_a_disabled_task_is_never_missed():
    """Its window is not a window. Not scheduling it is the intended state."""
    assert not cadence.schedule_is_missed(
        cron=HOURLY, enabled=False, next_run=stale(999999), has_active_run=False
    )


def test_a_task_with_work_in_flight_is_not_missed():
    """The scheduler fired; the run is simply still going.

    A long transfer holds its window open, and a run wedged in `running` is the
    *other* failure — bounded by the execution timeout and the watchdog, and
    already visible as a run that will not end. Flagging it here would put two
    different faults behind one badge.
    """
    assert not cadence.schedule_is_missed(
        cron=HOURLY, enabled=True, next_run=stale(999999), has_active_run=True
    )


def test_an_enabled_task_with_no_scheduled_run_at_all_is_missed():
    """No next_run is the deadest signal there is.

    The job is absent from the scheduler: it was never registered, the
    scheduler never started, or the cron is unschedulable. Whichever it is,
    this task has no future and the row must say so instead of rendering a
    tidy dash.
    """
    assert cadence.schedule_is_missed(
        cron=HOURLY, enabled=True, next_run=None, has_active_run=False
    )


def test_a_naive_next_run_is_compared_without_raising():
    """SQLite and some trigger paths hand back naive datetimes.

    Comparing naive to aware raises TypeError, which inside the task list
    would turn a wedged scheduler into a 500 — the diagnosis destroying the
    diagnostic.
    """
    naive = stale(999999).replace(tzinfo=None)
    assert cadence.schedule_is_missed(
        cron=HOURLY, enabled=True, next_run=naive, has_active_run=False
    )


# --------------------------------------------------------------------------
# 3. the signal on the API the task list actually reads
# --------------------------------------------------------------------------


def test_the_task_list_flags_a_wedged_schedule(sqlite_engine, monkeypatch):
    """The acceptance case, at the boundary the UI consumes.

    A fake stale `next_run` with nothing in flight: the row must carry a
    missed-schedule flag while its `last_run_state` still reads `success`,
    because both facts are true and only one of them was visible in September.
    """
    (task,) = create_source_tasks(sqlite_engine, count=1)
    with Session(sqlite_engine) as session:
        session.add(
            Run(
                task_id=task.id,
                state=RunState.success,
                started_at=stale(90000),
                finished_at=stale(89000),
            )
        )
        session.commit()
    monkeypatch.setattr(scheduler, "next_run_at", lambda t: stale(7200))

    with Session(sqlite_engine) as session:
        rows = tasks_api.list_tasks(session)

    (row,) = rows
    assert row.last_run_state == RunState.success
    assert row.missed_schedule is True


def test_the_task_list_flags_the_actual_september_incident(sqlite_engine, monkeypatch):
    """The incident, reproduced at the boundary the UI consumes.

    Its real mechanism, measured: one run wedged in `running` held the source,
    admission denied every later fire at `source_active`, and APScheduler kept
    advancing `next_run_time` on every tick because `_execute` returned
    immediately. So the sibling task has a HEALTHY-LOOKING future next_run, a
    `success` last-run state, and has not actually run in four days.

    That row is the September screen. It must be flagged.
    """
    (task,) = create_source_tasks(sqlite_engine, count=1)
    with Session(sqlite_engine) as session:
        session.add(
            Run(
                task_id=task.id,
                state=RunState.success,
                started_at=stale(4 * 86400),
                finished_at=stale(4 * 86400 - 300),
            )
        )
        session.commit()
    # Exactly what the live scheduler reported during the wedge.
    monkeypatch.setattr(scheduler, "next_run_at", lambda t: ahead(600))

    with Session(sqlite_engine) as session:
        (row,) = tasks_api.list_tasks(session)

    assert row.last_run_state == RunState.success
    assert row.next_run is not None
    assert row.missed_schedule is True


def test_a_task_that_has_simply_never_run_is_not_flagged_immediately(sqlite_engine, monkeypatch):
    """A task created a minute ago has no history and no fault.

    Elapsed activity falls back to the task's creation time so a brand-new
    task is not flagged before its first window has even arrived.
    """
    (task,) = create_source_tasks(sqlite_engine, count=1)
    monkeypatch.setattr(scheduler, "next_run_at", lambda t: ahead(600))

    with Session(sqlite_engine) as session:
        (row,) = tasks_api.list_tasks(session)

    assert row.missed_schedule is False


def test_a_task_created_long_ago_that_never_ran_is_flagged(sqlite_engine, monkeypatch):
    """Enabled, scheduled, days old, zero runs: it is not backing anything up."""
    (task,) = create_source_tasks(sqlite_engine, count=1)
    with Session(sqlite_engine) as session:
        stored = session.get(Task, task.id)
        stored.created_at = stale(4 * 86400)
        session.add(stored)
        session.commit()
    monkeypatch.setattr(scheduler, "next_run_at", lambda t: ahead(600))

    with Session(sqlite_engine) as session:
        (row,) = tasks_api.list_tasks(session)

    assert row.missed_schedule is True


def test_a_long_run_that_just_succeeded_is_not_a_missed_schedule(sqlite_engine, monkeypatch):
    """A backup that just finished is the opposite of a missed schedule.

    ai-review finding, PR #47, confirmed by measurement before fixing: an
    hourly task whose transfer legitimately takes two hours has a `started_at`
    older than cadence + grace the INSTANT it succeeds, so silence measured
    from the start flags it the moment it finishes backing up. This repo has
    real multi-hour transfers; that false positive would fire on every one of
    them, and a flag that cries wolf on healthy work is worse than no flag —
    the incident's entire cost was that nobody was looking at this screen.

    Silence is therefore measured from when work last STOPPED, not when it
    last started.
    """
    (task,) = create_source_tasks(sqlite_engine, count=1)
    with Session(sqlite_engine) as session:
        # HOURLY, deliberately: the shared fixture builds daily tasks, whose
        # 25-hour window swallows a two-hour run and would make this test pass
        # against the defect it is written for.
        stored = session.get(Task, task.id)
        stored.cron = HOURLY
        session.add(stored)
        session.add(
            Run(
                task_id=task.id,
                state=RunState.success,
                started_at=stale(2 * 3600),
                finished_at=stale(60),
            )
        )
        session.commit()
    assert cadence.cadence_seconds(HOURLY) + cadence.schedule_grace_seconds(HOURLY) < 2 * 3600
    monkeypatch.setattr(scheduler, "next_run_at", lambda t: ahead(3000))

    with Session(sqlite_engine) as session:
        (row,) = tasks_api.list_tasks(session)

    assert row.last_run_state == RunState.success
    assert row.missed_schedule is False


def test_a_long_run_that_finished_a_cycle_ago_is_still_flagged(sqlite_engine, monkeypatch):
    """The fix must not become a way for a task to stop being watched.

    Measuring from `finished_at` moves the clock forward once; it must not
    disable it. A task whose last run ended more than a cycle ago is silent
    regardless of how long that run took.
    """
    (task,) = create_source_tasks(sqlite_engine, count=1)
    with Session(sqlite_engine) as session:
        session.add(
            Run(
                task_id=task.id,
                state=RunState.success,
                started_at=stale(6 * 86400),
                finished_at=stale(4 * 86400),
            )
        )
        session.commit()
    monkeypatch.setattr(scheduler, "next_run_at", lambda t: ahead(600))

    with Session(sqlite_engine) as session:
        (row,) = tasks_api.list_tasks(session)

    assert row.missed_schedule is True


def test_a_terminal_run_with_no_finish_time_falls_back_to_its_start(sqlite_engine, monkeypatch):
    """Rows written before `finished_at` was reliably set must still be judged.

    Falling back to `started_at` is the conservative direction: it can only
    make a task look MORE silent than it was, never less, so a wedge cannot
    hide behind a missing timestamp.
    """
    (task,) = create_source_tasks(sqlite_engine, count=1)
    with Session(sqlite_engine) as session:
        session.add(
            Run(
                task_id=task.id,
                state=RunState.failed,
                started_at=stale(4 * 86400),
                finished_at=None,
            )
        )
        session.commit()
    monkeypatch.setattr(scheduler, "next_run_at", lambda t: ahead(600))

    with Session(sqlite_engine) as session:
        (row,) = tasks_api.list_tasks(session)

    assert row.missed_schedule is True


def test_the_task_list_does_not_flag_a_healthy_schedule(sqlite_engine, monkeypatch):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    monkeypatch.setattr(scheduler, "next_run_at", lambda t: ahead(600))

    with Session(sqlite_engine) as session:
        (row,) = tasks_api.list_tasks(session)

    assert row.missed_schedule is False


@pytest.mark.parametrize("state", [RunState.pending, RunState.running])
def test_the_task_list_does_not_flag_a_task_whose_run_is_in_flight(
    sqlite_engine, monkeypatch, state
):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    with Session(sqlite_engine) as session:
        session.add(Run(task_id=task.id, state=state, started_at=stale(600)))
        session.commit()
    monkeypatch.setattr(scheduler, "next_run_at", lambda t: stale(999999))

    with Session(sqlite_engine) as session:
        (row,) = tasks_api.list_tasks(session)

    assert row.missed_schedule is False


def test_the_task_detail_carries_the_same_flag(sqlite_engine, monkeypatch):
    """One derivation, both shapes. A flag on the list and not the detail
    would make the two views disagree about the same row."""
    (task,) = create_source_tasks(sqlite_engine, count=1)
    monkeypatch.setattr(scheduler, "next_run_at", lambda t: stale(999999))

    with Session(sqlite_engine) as session:
        row = tasks_api.get_task(task.id, session)

    assert row.missed_schedule is True


# --------------------------------------------------------------------------
# 4. scheduler liveness
# --------------------------------------------------------------------------


def test_liveness_reports_a_scheduler_that_never_started(monkeypatch):
    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setattr(scheduler, "_last_heartbeat", None)

    state = scheduler.liveness()

    assert state["running"] is False
    assert state["alive"] is False
    assert state["last_heartbeat"] is None


def test_a_fresh_heartbeat_is_alive(monkeypatch):
    monkeypatch.setattr(scheduler, "_last_heartbeat", utcnow())
    monkeypatch.setattr(scheduler, "_scheduler", _RunningScheduler())

    state = scheduler.liveness()

    assert state["running"] is True
    assert state["alive"] is True
    assert state["heartbeat_age_seconds"] < 5


def test_a_stale_heartbeat_is_not_alive_even_while_running(monkeypatch):
    """The exact shape of the September incident, one layer down.

    `running` stayed true for four days. If liveness trusted that flag the new
    endpoint would have reported "alive" throughout the outage and been worth
    precisely nothing.
    """
    age = scheduler.HEARTBEAT_INTERVAL_SECONDS * scheduler.STALE_HEARTBEAT_MULTIPLIER + 1
    monkeypatch.setattr(scheduler, "_last_heartbeat", stale(age))
    monkeypatch.setattr(scheduler, "_scheduler", _RunningScheduler())

    state = scheduler.liveness()

    assert state["running"] is True
    assert state["alive"] is False
    assert state["heartbeat_age_seconds"] >= age


@pytest.mark.asyncio
async def test_starting_the_scheduler_registers_a_heartbeat_job(sqlite_engine, monkeypatch):
    """The probe must be work the scheduler performs, not a flag beside it."""
    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setattr(scheduler, "_last_heartbeat", None)
    create_source_tasks(sqlite_engine, count=1)
    try:
        scheduler.start()
        sched = scheduler.get_scheduler()
        assert sched.get_job(scheduler.HEARTBEAT_JOB_ID) is not None
        # Started counts as a heartbeat: otherwise every deploy reports a dead
        # scheduler until the first interval elapses.
        assert scheduler.liveness()["alive"] is True
    finally:
        scheduler.shutdown()


@pytest.mark.asyncio
async def test_the_heartbeat_job_advances_the_stamp(monkeypatch):
    monkeypatch.setattr(scheduler, "_last_heartbeat", stale(999))

    await scheduler._heartbeat()

    assert scheduler.liveness()["heartbeat_age_seconds"] < 5


def test_an_unstarted_scheduler_reports_no_next_run_instead_of_raising(
    sqlite_engine, monkeypatch
):
    """The latent bug the flag would otherwise fail hardest on.

    APScheduler fills `Job.next_run_time` only once the scheduler is running;
    on an unstarted one the slot is unset and a direct read raises
    AttributeError. The suite carried a fixture stubbing `next_run_iso` out for
    exactly this, which was fine while the value was cosmetic. It is not fine
    now: this is the call that decides whether a row is flagged, so a raise
    here would turn "the scheduler never started" — the most total version of
    the fault this card reports — into a 500 on the task list.
    """
    monkeypatch.setattr(scheduler, "_scheduler", None)
    (task,) = create_source_tasks(sqlite_engine, count=1)
    scheduler.upsert_job(task)
    assert scheduler.get_scheduler().running is False
    assert scheduler.get_scheduler().get_job(f"task-{task.id}") is not None

    assert scheduler.next_run_at(task) is None
    assert scheduler.next_run_iso(task) is None


def test_an_unstarted_scheduler_flags_every_enabled_task(sqlite_engine, monkeypatch):
    """A scheduler that never started is a wedge with no exceptions.

    Every enabled task has no future firing, and the list must say so on each
    of them rather than rendering a tidy dash — the September screen.
    """
    monkeypatch.setattr(scheduler, "_scheduler", None)
    tasks = create_source_tasks(sqlite_engine, count=2)
    for task in tasks:
        scheduler.upsert_job(task)

    with Session(sqlite_engine) as session:
        rows = tasks_api.list_tasks(session)

    assert [row.missed_schedule for row in rows] == [True, True]


def test_a_stopped_scheduler_is_never_alive(monkeypatch):
    """ai-review finding, PR #47, confirmed by measurement before fixing.

    `shutdown()` does not clear the heartbeat, so a liveness answer derived
    from heartbeat age alone reported `running: false, alive: true` for up to
    three minutes after the scheduler stopped. That is a liveness endpoint
    vouching for a scheduler that is definitively not running — the window an
    operator restarting the service looks at, and an assertion that can only
    ever be wrong, since a stopped scheduler is the one case where doubt is
    unnecessary.

    Both signals are now required: recent work AND a scheduler to have done
    it. The heartbeat still carries the weight in the case that matters —
    `running` stayed true for four days in September — but it can no longer
    outvote a definite negative.
    """
    monkeypatch.setattr(scheduler, "_last_heartbeat", utcnow())
    monkeypatch.setattr(scheduler, "_scheduler", _StoppedScheduler())

    state = scheduler.liveness()

    assert state["running"] is False
    assert state["alive"] is False


@pytest.mark.asyncio
async def test_shutting_the_scheduler_down_stops_it_reporting_alive(sqlite_engine, monkeypatch):
    """End to end through the real scheduler, not a stand-in."""
    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setattr(scheduler, "_last_heartbeat", None)
    create_source_tasks(sqlite_engine, count=1)
    scheduler.start()
    assert scheduler.liveness()["alive"] is True

    scheduler.shutdown()
    # `shutdown(wait=False)` needs one turn of the loop before APScheduler
    # clears its own `running` flag — measured, and the reason the liveness
    # answer is read after yielding rather than synchronously.
    await asyncio.sleep(0.1)

    state = scheduler.liveness()
    assert state["running"] is False
    assert state["alive"] is False
    # The heartbeat itself is untouched and still fresh: this proves `alive`
    # actually consults `running`, rather than passing because the stamp
    # happened to age out.
    assert state["heartbeat_age_seconds"] < 5


def test_the_health_endpoint_reports_scheduler_liveness(monkeypatch):
    """Unauthenticated on purpose: the whole point is that something outside
    the app — a Kuma HTTP monitor, a compose healthcheck — can tell "the app
    answers" apart from "the scheduler is executing"."""
    monkeypatch.setattr(scheduler, "_last_heartbeat", utcnow())
    monkeypatch.setattr(scheduler, "_scheduler", _RunningScheduler())

    body = TestClient(main.app).get("/api/system/health").json()

    assert body["ok"] is True
    assert body["scheduler_alive"] is True


def test_the_health_endpoint_says_so_when_the_scheduler_is_wedged(monkeypatch):
    monkeypatch.setattr(scheduler, "_scheduler", _RunningScheduler())
    monkeypatch.setattr(scheduler, "_last_heartbeat", stale(86400))

    body = TestClient(main.app).get("/api/system/health").json()

    # `ok` deliberately stays true: this response is what the container
    # healthcheck reads, and flipping it would restart the container on a
    # signal this card only makes visible. The distinction lives in the field.
    assert body["ok"] is True
    assert body["scheduler_alive"] is False


def test_the_anonymous_health_response_carries_no_telemetry(monkeypatch):
    """The only unauthenticated endpoint answers one question and stops.

    Written RED against 83398f10, where `/health` returned the whole
    `liveness()` dict to anyone who asked: exact heartbeat timestamp, its age
    in seconds, the beat interval and the staleness threshold. None of that is
    needed to answer "is this service still backing things up", and all of it
    describes the internal timing of a service the caller has not
    authenticated to. The heartbeat timestamp in particular tells an anonymous
    caller exactly when the scheduler last did anything and exactly how long a
    gap has to open before anyone notices — the shape of a service's blind
    window, handed out for free.

    The boolean is the whole external contract: `false` means stop trusting
    that backups are running. Everything an operator needs to *diagnose* that
    lives one endpoint over, behind the credential.
    """
    monkeypatch.setattr(scheduler, "_scheduler", _RunningScheduler())
    monkeypatch.setattr(scheduler, "_last_heartbeat", utcnow())

    body = TestClient(main.app).get("/api/system/health").json()

    assert set(body) == {"ok", "version", "scheduler_alive"}
    assert isinstance(body["scheduler_alive"], bool)
    # Named individually as well as by the set comparison above: these are the
    # specific fields that leaked, and a future response gaining a nested
    # container would defeat a shape assertion alone.
    serialised = json.dumps(body)
    for leaked in (
        "last_heartbeat",
        "heartbeat_age_seconds",
        "heartbeat_interval_seconds",
        "stale_after_seconds",
        "missed_schedule_count",
        "missed_schedule_task_ids",
        "running",
    ):
        assert leaked not in serialised


@pytest.mark.asyncio
async def test_the_scheduler_endpoint_carries_the_detail_health_no_longer_does(
    sqlite_engine, monkeypatch
):
    """The split has to leave the detail somewhere, or removing it from
    `/health` would be a loss rather than a move. Same process state as the
    anonymous probe above; an authenticated caller gets the timings back."""
    tasks = create_source_tasks(sqlite_engine, count=1)
    monkeypatch.setattr(scheduler, "_scheduler", _RunningScheduler())
    monkeypatch.setattr(scheduler, "_last_heartbeat", utcnow())
    monkeypatch.setattr(scheduler, "next_run_at", lambda t: stale(999999))

    def override_session():
        with Session(sqlite_engine) as session:
            yield session

    previous = main.app.dependency_overrides.get(db.get_session)
    main.app.dependency_overrides[db.get_session] = override_session
    try:
        transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            auth=(TEST_HTTP_USERNAME, TEST_HTTP_PASSWORD),
        ) as client:
            response = await client.get("/api/system/scheduler")
    finally:
        if previous is None:
            main.app.dependency_overrides.pop(db.get_session, None)
        else:
            main.app.dependency_overrides[db.get_session] = previous

    assert response.status_code == 200
    body = response.json()
    assert body["scheduler"]["running"] is True
    assert body["scheduler"]["alive"] is True
    assert body["scheduler"]["last_heartbeat"] is not None
    assert body["scheduler"]["heartbeat_age_seconds"] is not None
    assert body["scheduler"]["stale_after_seconds"] > 0
    assert body["missed_schedule_task_ids"] == [tasks[0].id]


@pytest.mark.asyncio
async def test_the_scheduler_endpoint_names_the_tasks_that_missed(sqlite_engine, monkeypatch):
    tasks = create_source_tasks(sqlite_engine, count=2)
    monkeypatch.setattr(scheduler, "_scheduler", _RunningScheduler())
    monkeypatch.setattr(scheduler, "_last_heartbeat", utcnow())
    stale_for = {tasks[0].id}
    monkeypatch.setattr(
        scheduler,
        "next_run_at",
        lambda t: stale(999999) if t.id in stale_for else ahead(600),
    )

    with Session(sqlite_engine) as session:
        body = system_api.scheduler_health(session)

    assert body["scheduler"]["alive"] is True
    assert body["missed_schedule_count"] == 1
    assert body["missed_schedule_task_ids"] == [tasks[0].id]


@pytest.mark.asyncio
async def test_the_scheduler_endpoint_is_not_anonymous():
    """It reports which of the operator's tasks are failing to fire. The
    unauthenticated surface stays exactly one endpoint wide."""
    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        anonymous = await client.get("/api/system/scheduler")
    assert anonymous.status_code == 401


class _RunningScheduler:
    """The minimum an `AsyncIOScheduler` offers `liveness`."""

    running = True

    def get_job(self, job_id):  # pragma: no cover - shape only
        return None


class _StoppedScheduler(_RunningScheduler):
    """One that has been shut down, with its heartbeat left behind."""

    running = False
