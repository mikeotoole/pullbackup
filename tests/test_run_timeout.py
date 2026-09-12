# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""A run must not be able to hold the executor forever.

Written RED against 241dfe6d (PR #45 merged), where `_execute_run` does a bare
`await proc.wait()` with no bound at all, and nothing anywhere reconciles an
active row whose execution this process is not actually running.

INCIDENT 2026-09-06..10. Run 4773 (task 11, syncoid) hung on a ZFS divergence —
"cannot receive incremental stream: most recent snapshot of
tank/docker_remote/reef/scrutiny does not match incremental source". The
subprocess never returned. The run row stayed `running` for four days, and
because admission refuses a new run while a non-terminal row exists for the
task's source, every subsequent fire for that scope was denied. No backup ran
from 2026-09-06 07:15 UTC until a human ran RestartStack on 2026-09-10 19:49
UTC. Uptime Kuma push monitors were the only thing that noticed.

Two distinct gaps, pinned separately below because they fail independently:

  1. **No execution timeout.** A subprocess that never exits — a dead network
     path, a wedged `zfs receive` — is waited on forever. There is no ceiling
     anywhere: not in the runner, not in APScheduler (`max_instances` only
     SKIPS the next fire, it does not reclaim the stuck one), not in the
     container healthcheck, which probes HTTP and stays green throughout.

  2. **No liveness reconciliation while running.** `reconcile_stale_runs()` is
     correct and was what finally cleared this, but it only runs at startup. A
     row left `running` with no execution behind it wedges admission for its
     whole source until somebody restarts the application. Startup recovery is
     a recovery path, not a control loop.

The ownership map (`_run_owners`, PR #41) is the sound liveness signal and the
one these tests use. A pid is not: pids are recycled, so "the pid is gone" and
"a different process now holds that pid" are indistinguishable, and a run's pid
is not knowable until after the exec anyway. An entry in `_run_owners` is
written by `execute_run` itself and removed in its `finally`, so its absence
means this process is provably not executing that row.
"""
import asyncio
from datetime import timedelta

import pytest
from pullbackup.config import ConfigurationError, Settings, load_settings
from pullbackup.models import Run, RunState, Task, utcnow
from pullbackup.services import runner
from sqlmodel import Session

# Shared engine fixture and row factories.
from test_run_admission import (  # noqa: F401
    create_source_tasks,
    sqlite_engine,
)

# The stub subprocess machinery, the process-group liveness probes, and the
# autouse execution drain. Imported rather than duplicated so both suites make
# the same guarantee with the same measurement.
from test_run_cancellation import (  # noqa: F401
    blocking_binary,
    drain,
    drain_leftover_executions,
    make_syncoid_task,
    start_blocking_run,
    wait_for_child_pid,
    wait_for_first_beat,
    wait_until_dead,
    wait_until_stopped_beating,
)

# Short enough to keep the suite fast, long enough that a run which is supposed
# to finish normally is not racing the bound.
TEST_TIMEOUT_SECONDS = 1
# How long a timed-out run is given to terminalize. Generous: the kill path is
# TERM -> 5s -> KILL, so a stub that ignored TERM would need the full
# escalation. The stubs here do not, but the bound must not assume that.
SETTLE_TIMEOUT_SECONDS = 20


def use_timeout(monkeypatch, seconds):
    monkeypatch.setattr(runner.settings, "run_timeout_seconds", seconds)


async def wait_for_state(engine, run_id, states, timeout=SETTLE_TIMEOUT_SECONDS):
    """Return the run once it reaches one of `states`, or raise.

    Polls rather than awaiting the execution task, because the guarantee under
    test is that the row becomes terminal *on its own* — without anybody
    awaiting, cancelling, or restarting anything.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last = None
    while loop.time() < deadline:
        with Session(engine) as session:
            run = session.get(Run, run_id)
            last = run.state
            if run.state in states:
                session.expunge(run)
                return run
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"run {run_id} never reached {[s.value for s in states]}; it is {last}"
    )


def age_run(engine, run_id, seconds):
    """Backdate a run's start so it is older than the watchdog's grace window."""
    with Session(engine) as session:
        run = session.get(Run, run_id)
        run.started_at = utcnow() - timedelta(seconds=seconds)
        session.add(run)
        session.commit()


# --------------------------------------------------------------------------
# 1. the execution timeout
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_hung_run_is_failed_within_the_timeout(
    sqlite_engine, monkeypatch, blocking_binary
):
    """The incident, reduced: a subprocess that never returns on its own.

    The stub is never released, so on an unbounded build `proc.wait()` never
    completes and this test hangs until the suite-level drain tears it down —
    which is exactly the production behaviour, four days of it.
    """
    use_timeout(monkeypatch, TEST_TIMEOUT_SECONDS)
    (task,) = create_source_tasks(sqlite_engine, count=1)
    release_file, child_pid_file = blocking_binary("rsync", "timeout-rsync")
    run_id, execution, captured = await start_blocking_run(
        sqlite_engine, task.id, monkeypatch
    )

    try:
        run = await wait_for_state(
            sqlite_engine, run_id, (RunState.failed, RunState.cancelled)
        )
    finally:
        await drain(execution, release_file, captured.get("process"))

    assert run.state == RunState.failed, (
        "a run killed by the execution timeout is a FAILURE, not an operator "
        "cancellation; the history has to tell those apart"
    )
    assert run.finished_at is not None
    assert run.exit_code == -1
    assert "timeout" in run.error_message.lower(), (
        f"the row must say why it was killed, got {run.error_message!r}"
    )
    assert str(TEST_TIMEOUT_SECONDS) in run.error_message, (
        "an operator reading the row needs the bound that was exceeded, so "
        "they can tell a genuine long transfer from a wedge"
    )
    log_path = runner.settings.log_dir / run.log_filename
    assert log_path.exists(), "a timed-out run must keep its log"
    assert "timeout" in log_path.read_text().lower()


@pytest.mark.asyncio
async def test_the_timeout_kills_the_whole_process_group(
    sqlite_engine, monkeypatch, blocking_binary
):
    """Killing only the direct child leaves the real work running.

    For rsync the direct child spawns ssh; for syncoid it is a
    `zfs send | zfs receive` pipeline. The wedged process in the incident was
    downstream of the process the runner spawned, so a timeout that signals
    only its own child would have terminalized the row while leaving the thing
    that was actually stuck holding the dataset.
    """
    use_timeout(monkeypatch, TEST_TIMEOUT_SECONDS)
    (task,) = create_source_tasks(sqlite_engine, count=1)
    release_file, child_pid_file = blocking_binary("rsync", "timeout-group-rsync")
    run_id, execution, captured = await start_blocking_run(
        sqlite_engine, task.id, monkeypatch
    )
    child_pid = await wait_for_child_pid(child_pid_file)
    beat = await wait_for_first_beat(child_pid_file)

    try:
        await wait_for_state(sqlite_engine, run_id, (RunState.failed,))
        assert captured["process"].returncode is not None
        assert await wait_until_stopped_beating(beat), (
            "the child of the timed-out subprocess is still doing work; the "
            "whole process group must be terminated"
        )
        assert await wait_until_dead(child_pid), (
            "the child of the timed-out subprocess survived the timeout"
        )
    finally:
        await drain(execution, release_file, captured.get("process"))


@pytest.mark.asyncio
async def test_a_timed_out_syncoid_run_is_failed_and_its_group_terminated(
    sqlite_engine, tmp_path, monkeypatch, blocking_binary
):
    """Syncoid is the engine that actually hung, so it gets its own pin."""
    use_timeout(monkeypatch, TEST_TIMEOUT_SECONDS)
    task = make_syncoid_task(sqlite_engine, tmp_path)
    release_file, child_pid_file = blocking_binary("syncoid", "timeout-syncoid")
    run_id, execution, captured = await start_blocking_run(
        sqlite_engine, task.id, monkeypatch
    )
    child_pid = await wait_for_child_pid(child_pid_file)
    beat = await wait_for_first_beat(child_pid_file)

    try:
        run = await wait_for_state(sqlite_engine, run_id, (RunState.failed,))
        assert "timeout" in run.error_message.lower()
        assert await wait_until_stopped_beating(beat)
        assert await wait_until_dead(child_pid)
    finally:
        await drain(execution, release_file, captured.get("process"))


@pytest.mark.asyncio
async def test_the_timeout_unwedges_the_next_run_of_the_same_task(
    sqlite_engine, monkeypatch, blocking_binary
):
    """The acceptance criterion, stated as admission rather than as a row.

    A terminal row that nobody can start a successor for is not a fix. What the
    incident actually cost was four days of denied admissions: every fire for
    task 11's source was refused while the wedged row stayed non-terminal. So
    the test that matters is that the NEXT run is admitted, not merely that the
    hung one stopped.
    """
    use_timeout(monkeypatch, TEST_TIMEOUT_SECONDS)
    (task,) = create_source_tasks(sqlite_engine, count=1)
    release_file, _ = blocking_binary("rsync", "unwedge-rsync")
    run_id, execution, captured = await start_blocking_run(
        sqlite_engine, task.id, monkeypatch
    )

    # Before the timeout fires the refusal is correct: the run really is active.
    blocked = await runner.admit_run(task.id)
    assert not blocked.accepted
    assert blocked.reason == "task_active"

    try:
        await wait_for_state(sqlite_engine, run_id, (RunState.failed,))
        admitted = await runner.admit_run(task.id)
        assert admitted.accepted, (
            "after the hung run was terminated the task must be schedulable "
            "again; this is the four days of missed backups"
        )
        assert admitted.run_id != run_id
    finally:
        await drain(execution, release_file, captured.get("process"))


@pytest.mark.asyncio
async def test_a_run_that_finishes_inside_the_timeout_is_untouched(
    sqlite_engine, monkeypatch, blocking_binary
):
    """No false positives. A bound that fails healthy runs is worse than none."""
    use_timeout(monkeypatch, 600)
    (task,) = create_source_tasks(sqlite_engine, count=1)
    release_file, _ = blocking_binary("rsync", "inside-rsync")
    run_id, execution, captured = await start_blocking_run(
        sqlite_engine, task.id, monkeypatch
    )

    release_file.touch()
    await asyncio.gather(execution, return_exceptions=True)

    with Session(sqlite_engine) as session:
        run = session.get(Run, run_id)
        assert run.state == RunState.success
        assert run.error_message == ""


@pytest.mark.asyncio
async def test_a_zero_timeout_restores_the_unbounded_wait(
    sqlite_engine, monkeypatch, blocking_binary
):
    """The documented escape hatch has to actually disable the bound.

    A multi-day initial replication is a real thing an operator has, and their
    way out must not be to run an unmaintained fork. Zero means no ceiling —
    and this test proves it means that, rather than quietly behaving like some
    other number.
    """
    use_timeout(monkeypatch, 0)
    (task,) = create_source_tasks(sqlite_engine, count=1)
    release_file, _ = blocking_binary("rsync", "unbounded-rsync")
    run_id, execution, captured = await start_blocking_run(
        sqlite_engine, task.id, monkeypatch
    )

    # Well past any plausible internal default, including a "0 means use the
    # default" misreading.
    await asyncio.sleep(2.0)
    with Session(sqlite_engine) as session:
        assert session.get(Run, run_id).state == RunState.running

    await drain(execution, release_file, captured.get("process"))
    with Session(sqlite_engine) as session:
        assert session.get(Run, run_id).state == RunState.success


# --------------------------------------------------------------------------
# 2. configuration
# --------------------------------------------------------------------------


def test_the_default_timeout_is_finite():
    """A fresh install must not ship the configuration that caused the incident.

    Defaulting to "no bound" would mean every deployment reproduces four days
    of silence out of the box and only the operators who read the source are
    protected.
    """
    assert Settings().run_timeout_seconds > 0


def test_a_negative_timeout_is_refused_at_startup():
    """Rejected rather than clamped, matching the rest of config.py.

    A negative value reaches `asyncio.wait_for` as an already-expired deadline,
    so every run would be killed the instant it spawned — a backup system that
    never completes a backup. Running on a number the operator never chose is
    the silence this file exists to remove, relocated.
    """
    with pytest.raises(ConfigurationError) as error:
        load_settings({"PULLBACKUP_RUN_TIMEOUT_SECONDS": "-1"}, env_file=None)
    assert "RUN_TIMEOUT_SECONDS" in str(error.value)


def test_a_non_positive_watchdog_interval_is_refused_at_startup():
    """A zero or negative sweep interval is a busy loop, not a disabled one."""
    with pytest.raises(ConfigurationError) as error:
        load_settings({"PULLBACKUP_WATCHDOG_INTERVAL_SECONDS": "0"}, env_file=None)
    assert "WATCHDOG_INTERVAL_SECONDS" in str(error.value)


# --------------------------------------------------------------------------
# 3. the dead-execution watchdog
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_watchdog_fails_a_running_row_with_no_execution_owner(
    sqlite_engine,
):
    """The row that wedged admission, reproduced without a subprocess.

    A `running` row that this process holds no execution for is provably not
    executing: `_run_owners` is written by `execute_run` and released in its
    `finally`, so there is no state in which a live execution is missing from
    it. Leaving such a row alone means waiting for a restart — which is the
    four-day gap.
    """
    (task,) = create_source_tasks(sqlite_engine, count=1)
    admission = await runner.admit_run(task.id)
    run_id = admission.accepted_run_id()
    with Session(sqlite_engine) as session:
        run = session.get(Run, run_id)
        run.state = RunState.running
        session.add(run)
        session.commit()
    age_run(sqlite_engine, run_id, runner.ORPHAN_GRACE_SECONDS + 60)

    reclaimed = await runner.sweep_orphaned_runs()

    assert reclaimed == [run_id]
    with Session(sqlite_engine) as session:
        run = session.get(Run, run_id)
    assert run.state == RunState.failed
    assert run.finished_at is not None
    assert run.exit_code == -1
    assert run.error_message, "a reclaimed row must say why it was reclaimed"


@pytest.mark.asyncio
async def test_the_watchdog_unwedges_admission_for_the_orphaned_source(
    sqlite_engine,
):
    """Same acceptance shape as the timeout: the next run must be admissible."""
    (task,) = create_source_tasks(sqlite_engine, count=1)
    admission = await runner.admit_run(task.id)
    run_id = admission.accepted_run_id()
    with Session(sqlite_engine) as session:
        run = session.get(Run, run_id)
        run.state = RunState.running
        session.add(run)
        session.commit()
    age_run(sqlite_engine, run_id, runner.ORPHAN_GRACE_SECONDS + 60)

    assert not (await runner.admit_run(task.id)).accepted
    await runner.sweep_orphaned_runs()
    assert (await runner.admit_run(task.id)).accepted


@pytest.mark.asyncio
async def test_the_watchdog_leaves_a_genuinely_executing_run_alone(
    sqlite_engine, monkeypatch, blocking_binary
):
    """The false positive that would be worse than the bug.

    A long transfer is indistinguishable from a wedge by duration alone — the
    production instance has legitimate six-hour pulls. Only the ownership map
    separates them, so the sweep must consult it and nothing else.
    """
    use_timeout(monkeypatch, 0)
    (task,) = create_source_tasks(sqlite_engine, count=1)
    release_file, _ = blocking_binary("rsync", "watchdog-live-rsync")
    run_id, execution, captured = await start_blocking_run(
        sqlite_engine, task.id, monkeypatch
    )
    age_run(sqlite_engine, run_id, runner.ORPHAN_GRACE_SECONDS + 3600)

    try:
        reclaimed = await runner.sweep_orphaned_runs()
        assert reclaimed == [], "the watchdog killed a run that was executing"
        with Session(sqlite_engine) as session:
            assert session.get(Run, run_id).state == RunState.running
        assert captured["process"].returncode is None
    finally:
        await drain(execution, release_file, captured.get("process"))

    with Session(sqlite_engine) as session:
        assert session.get(Run, run_id).state == RunState.success


@pytest.mark.asyncio
async def test_the_watchdog_leaves_a_freshly_admitted_pending_run_alone(
    sqlite_engine,
):
    """`admit_run` and `execute_run` are two steps, and there is a gap.

    `start_admitted_run` schedules the execution; ownership is registered when
    that task first runs, on a later loop iteration. A sweep landing inside
    that window sees an active row with no owner — which is exactly the
    orphan signature. Killing it would make the watchdog a race against every
    single run the system starts.
    """
    (task,) = create_source_tasks(sqlite_engine, count=1)
    admission = await runner.admit_run(task.id)

    reclaimed = await runner.sweep_orphaned_runs()

    assert reclaimed == []
    with Session(sqlite_engine) as session:
        assert session.get(Run, admission.run_id).state == RunState.pending


@pytest.mark.asyncio
async def test_the_watchdog_leaves_terminal_rows_alone(sqlite_engine):
    """Reclaiming is for active rows only; history is not rewritten."""
    (task,) = create_source_tasks(sqlite_engine, count=1)
    admission = await runner.admit_run(task.id)
    run_id = admission.accepted_run_id()
    with Session(sqlite_engine) as session:
        run = session.get(Run, run_id)
        run.state = RunState.success
        run.finished_at = utcnow()
        run.exit_code = 0
        session.add(run)
        session.commit()
    age_run(sqlite_engine, run_id, runner.ORPHAN_GRACE_SECONDS + 60)

    assert await runner.sweep_orphaned_runs() == []
    with Session(sqlite_engine) as session:
        run = session.get(Run, run_id)
    assert run.state == RunState.success
    assert run.exit_code == 0
    assert run.error_message == ""


@pytest.mark.asyncio
async def test_the_watchdog_loop_sweeps_repeatedly_and_stops_cleanly(
    sqlite_engine, monkeypatch
):
    """The sweep has to be a control loop, not another startup-only path.

    `reconcile_stale_runs` already covers restart. The whole point of this gap
    is that a wedge must clear WITHOUT one, so a sweep that only ever runs once
    fixes nothing.
    """
    monkeypatch.setattr(runner.settings, "watchdog_interval_seconds", 1)
    sweeps = asyncio.Event()
    calls = []
    real_sweep = runner.sweep_orphaned_runs

    async def counted_sweep():
        calls.append(1)
        if len(calls) >= 2:
            sweeps.set()
        return await real_sweep()

    monkeypatch.setattr(runner, "sweep_orphaned_runs", counted_sweep)

    watchdog = runner.start_watchdog()
    try:
        await asyncio.wait_for(sweeps.wait(), timeout=10)
    finally:
        await runner.stop_watchdog()

    assert len(calls) >= 2
    assert watchdog.done()
    assert runner._watchdog is None


@pytest.mark.asyncio
async def test_the_watchdog_loop_survives_a_failing_sweep(
    sqlite_engine, monkeypatch
):
    """One bad sweep must not silently switch the watchdog off.

    A loop that dies on its first exception leaves the system in exactly the
    state this card is about, with no log line saying the guard is gone.
    """
    monkeypatch.setattr(runner.settings, "watchdog_interval_seconds", 1)
    recovered = asyncio.Event()
    calls = []

    async def exploding_sweep():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        recovered.set()
        return []

    monkeypatch.setattr(runner, "sweep_orphaned_runs", exploding_sweep)

    runner.start_watchdog()
    try:
        await asyncio.wait_for(recovered.wait(), timeout=10)
    finally:
        await runner.stop_watchdog()

    assert len(calls) >= 2
