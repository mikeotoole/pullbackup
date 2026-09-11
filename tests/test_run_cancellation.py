"""Operator control over work that is already in flight.

Three actions used to be conflated, and only one of them existed:

1. **Cancel the current run.** There was no way to stop a transfer short of
   restarting the whole application, which terminates *every* run and loses the
   scheduler's state. Cancellation now goes through a run-id ownership lookup so
   a request can only ever signal the process group this application started for
   that exact run.
2. **Disable future runs.** Turning a task off is a *scheduling* change. It must
   not reach into a transfer that is already copying bytes.
3. **Edit next-run configuration.** A subset of fields cannot affect the command
   that is already executing, because the runner loaded its task/source snapshot
   before the subprocess was spawned. Those are editable while a run is active;
   everything that shapes the command stays locked.

The tests here pin all three, plus the races between them.
"""

import asyncio
import os
import pathlib
import subprocess

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pullbackup.api import runs as runs_api
from pullbackup.api import tasks as tasks_api
from pullbackup.models import Run, RunState, Task
from pullbackup.services import runner, scheduler
from sqlmodel import Session

# Shared engine fixture and row factories. Imported rather than duplicated so
# both suites exercise the same monkeypatched runner state.
from test_run_admission import (  # noqa: F401
    create_source_tasks,
    sqlite_engine,
    task_input,
)


@pytest.fixture
def blocking_binary(tmp_path, monkeypatch):
    """Install stub executables that block, and guarantee they are released.

    Returns ``install(program, name) -> (release_file, child_pid_file)``.

    The teardown touches every release file it handed out. Without that, an
    assertion firing before a test reaches its own release leaves the stub
    spinning, the event loop's child-watcher teardown never completes, and
    pytest hangs with the traceback *never printed* — an invisible failure,
    which is much worse than a visible one. It cost a 400s wall-clock timeout
    with no diagnostic before this fixture existed.
    """
    released = []

    def install(program, name):
        release_file = tmp_path / f"{name}.release"
        child_pid_file = tmp_path / f"{name}.child"
        bin_dir = tmp_path / name
        bin_dir.mkdir()
        stub = bin_dir / program
        # The child is the point of the stub. A cancellation that only signalled
        # the direct subprocess would leave it alive — for rsync that is the ssh
        # it spawned, for syncoid the zfs send/receive pipeline — so the tests
        # record its pid and assert it is gone afterwards. Both processes also
        # carry their own iteration bound as a second line of defence.
        # The grandchild also advances a heartbeat file on every iteration. Its
        # pid alone cannot prove it stopped: see `wait_until_dead` for why a
        # terminated process can stay signalable indefinitely. A heartbeat that
        # stops advancing is a direct measurement of "it is doing no more work",
        # which is the guarantee the cancellation actually owes.
        stub.write_text(
            "#!/bin/sh\n"
            "sh -c 'i=0; while [ $i -lt 6000 ]; do\n"
            '  echo $i > "$PULLBACK_TEST_CHILD_BEAT_FILE"\n'
            "  sleep 0.05\n"
            "  i=$((i+1))\n"
            "done' &\n"
            'echo $! > "$PULLBACK_TEST_CHILD_PID_FILE"\n'
            "i=0\n"
            'while [ ! -f "$PULLBACK_TEST_RELEASE_FILE" ] && [ $i -lt 6000 ]; do\n'
            "  sleep 0.01\n"
            "  i=$((i+1))\n"
            "done\n"
            "exit 0\n"
        )
        stub.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}:/bin:/usr/bin")
        monkeypatch.setenv("PULLBACK_TEST_RELEASE_FILE", str(release_file))
        monkeypatch.setenv("PULLBACK_TEST_CHILD_PID_FILE", str(child_pid_file))
        monkeypatch.setenv("PULLBACK_TEST_CHILD_BEAT_FILE", str(beat_file(child_pid_file)))
        released.append(release_file)
        return release_file, child_pid_file

    yield install
    for release_file in released:
        release_file.touch()


@pytest_asyncio.fixture(autouse=True)
async def drain_leftover_executions():
    """Cancel and await any execution a failing test left behind.

    A test that trips an assertion before its own ``drain`` leaves an execution
    task pending with a live subprocess transport attached. pytest-asyncio then
    closes the loop with that transport outstanding, and the session hangs
    **after printing the F but before printing the traceback** — so the real
    failure is never shown and the whole suite looks like a timeout. Draining
    here converts every such case back into an ordinary readable failure.

    This must be an *async* fixture so the teardown runs on the SAME loop the
    test used; ``asyncio.run`` would open a second loop that cannot await those
    tasks at all.
    """
    yield
    leftovers = tuple(runner._execution_tasks)
    for execution in leftovers:
        execution.cancel()
    if leftovers:
        await asyncio.gather(*leftovers, return_exceptions=True)


@pytest.fixture
def unstarted_scheduler_next_run(monkeypatch):
    """Stub ``next_run_iso`` for tests that leave the scheduler unstarted.

    APScheduler only fills in ``Job.next_run_time`` when the scheduler is
    running; on an unstarted one the attribute is an unset slot and
    ``next_run_iso`` raises ``AttributeError``. Production never sees this —
    ``scheduler.start()`` runs in the FastAPI lifespan before any request can
    arrive — but a unit test that calls ``update_task`` directly does. The
    existing suite stubs it for exactly this reason; reusing one fixture keeps
    the reason written down in one place instead of repeated at each call site.
    """
    monkeypatch.setattr(scheduler, "next_run_iso", lambda task: None)


def make_syncoid_task(engine, tmp_path):
    (task,) = create_source_tasks(engine, count=1)
    with Session(engine) as session:
        stored = session.get(Task, task.id)
        stored.task_type = "syncoid"
        stored.remote_path = "pool0/docker/eel"
        stored.local_path = "cache/docker_remote/eel"
        session.add(stored)
        session.commit()
        session.refresh(stored)
        return stored


def beat_file(child_pid_file):
    """Where the stub's grandchild records its liveness heartbeat."""
    return child_pid_file.with_suffix(".beat")


async def wait_for_child_pid(child_pid_file):
    for _ in range(500):
        if child_pid_file.exists():
            text = child_pid_file.read_text().strip()
            if text:
                return int(text)
        await asyncio.sleep(0.01)
    raise AssertionError("the stub never recorded a child pid")


async def wait_for_first_beat(child_pid_file):
    """Return once the grandchild has proven it is actually running.

    Without this the "it stopped beating" assertion could be satisfied
    vacuously by a grandchild that had not started beating yet.
    """
    path = beat_file(child_pid_file)
    for _ in range(500):
        if path.exists() and path.read_text().strip():
            return path
        await asyncio.sleep(0.01)
    raise AssertionError("the stub's grandchild never wrote a heartbeat")


def process_state(pid):
    """The kernel's state letter for `pid`, or None if no such process exists.

    Linux exposes /proc. macOS does not, but ships `ps`. The slim CI container
    has /proc but NOT `ps` (procps is not installed), so neither source alone
    covers both hosts this suite has to pass on.
    """
    try:
        raw = pathlib.Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        if pathlib.Path("/proc/self/stat").exists():
            return None  # /proc works here, so the pid genuinely does not exist
    except OSError:
        pass
    else:
        return raw.rsplit(")", 1)[1].split()[0]

    completed = subprocess.run(
        ["ps", "-o", "state=", "-p", str(pid)], capture_output=True, text=True
    )
    if completed.returncode != 0:
        return None
    state = completed.stdout.strip()
    return state[:1] if state else None


async def wait_until_dead(pid, timeout=10.0):
    """True once `pid` is no longer an *executing* process.

    Deliberately NOT ``os.kill(pid, 0)`` raising ProcessLookupError. A process
    that has been terminated but whose exit status nobody has collected stays a
    **zombie**: it has already released its memory, file descriptors and network
    connections — it is dead in every sense this cancellation guarantee cares
    about — yet it keeps a pid-table entry, so ``os.kill(pid, 0)`` on it
    SUCCEEDS, forever.

    Whether that zombie lingers is a property of the *host*, not of the
    cancellation. The stub's grandchild is orphaned the moment its parent dies,
    so it reparents to pid 1, and pid 1's reaping behaviour decides. On macOS
    launchd reaps in milliseconds and the old ``os.kill``-based probe passed. In
    the CI container pid 1 is a non-reaping placeholder — Gitea's act_runner
    holds the container open and ``docker exec``s each step — so the zombie is
    permanent and the identical probe reported a correctly-killed grandchild as
    "survived". Reproduced against this exact commit on both hosts; the
    production behaviour was never different between them.

    So: gone, or reaped-pending. Both mean terminated. Anything else is alive.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if process_state(pid) in (None, "Z"):
            return True
        await asyncio.sleep(0.02)
    return False


async def wait_until_stopped_beating(path, timeout=10.0):
    """True once the grandchild stops advancing its heartbeat file.

    The behavioural half of the guarantee, and the half that cannot be fooled:
    it is immune to zombie state and to pid reuse alike, because it observes
    work actually being done rather than a pid-table entry. A grandchild that
    survived a cancellation keeps counting, and this returns False.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        before = path.read_text() if path.exists() else None
        await asyncio.sleep(0.3)  # 6x the grandchild's 0.05s beat interval
        if path.exists() and path.read_text() == before:
            return True
    return False


async def start_blocking_run(engine, task_id, monkeypatch):
    """Admit and start a run, returning once its subprocess really exists."""
    spawned = asyncio.Event()
    captured = {}
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def observed_create_subprocess_exec(*args, **kwargs):
        process = await real_create_subprocess_exec(*args, **kwargs)
        captured["process"] = process
        spawned.set()
        return process

    monkeypatch.setattr(
        runner.asyncio, "create_subprocess_exec", observed_create_subprocess_exec
    )
    admission = await runner.admit_run(task_id)
    run_id = admission.accepted_run_id()
    execution = runner.start_admitted_run(run_id)
    await asyncio.wait_for(spawned.wait(), timeout=5)
    with Session(engine) as session:
        assert session.get(Run, run_id).state == RunState.running
    return run_id, execution, captured


async def drain(execution, release_file, process=None):
    release_file.touch()
    await asyncio.gather(execution, return_exceptions=True)
    if process is not None and process.returncode is None:
        process.kill()
        await process.wait()


# --------------------------------------------------------------------------
# 1. cancelling a run by id
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_run_terminates_the_rsync_group_and_marks_it_cancelled(
    sqlite_engine, tmp_path, monkeypatch, blocking_binary
):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    release_file, child_pid_file = blocking_binary("rsync", "cancel-rsync")
    run_id, execution, captured = await start_blocking_run(
        sqlite_engine, task.id, monkeypatch
    )
    child_pid = await wait_for_child_pid(child_pid_file)
    beat = await wait_for_first_beat(child_pid_file)

    try:
        result = await runner.cancel_run(run_id)
    finally:
        await drain(execution, release_file, captured.get("process"))

    assert result.outcome == runner.CancelOutcome.cancelled
    assert result.state == RunState.cancelled
    assert captured["process"].returncode is not None
    # Two independent probes of the same guarantee. The heartbeat is the one
    # that cannot be fooled: a grandchild that outlived the cancellation keeps
    # counting, whatever the pid table says.
    assert await wait_until_stopped_beating(beat), (
        "the child of the cancelled subprocess is still doing work; the whole "
        "process group must be terminated, not just the direct child"
    )
    assert await wait_until_dead(child_pid), (
        "the child of the cancelled subprocess survived; the whole process "
        "group must be terminated, not just the direct child"
    )
    with Session(sqlite_engine) as session:
        run = session.get(Run, run_id)
    assert run.state == RunState.cancelled
    assert run.finished_at is not None
    assert run.error_message == "run cancelled"
    log_path = runner.settings.log_dir / run.log_filename
    assert log_path.exists(), "a cancelled run must keep its log"
    assert "runner cancelled" in log_path.read_text()


@pytest.mark.asyncio
async def test_cancel_run_terminates_the_syncoid_group_and_marks_it_cancelled(
    sqlite_engine, tmp_path, monkeypatch, blocking_binary
):
    task = make_syncoid_task(sqlite_engine, tmp_path)
    release_file, child_pid_file = blocking_binary("syncoid", "cancel-syncoid")
    run_id, execution, captured = await start_blocking_run(
        sqlite_engine, task.id, monkeypatch
    )
    child_pid = await wait_for_child_pid(child_pid_file)
    beat = await wait_for_first_beat(child_pid_file)

    try:
        result = await runner.cancel_run(run_id)
    finally:
        await drain(execution, release_file, captured.get("process"))

    assert result.outcome == runner.CancelOutcome.cancelled
    assert captured["process"].returncode is not None
    assert await wait_until_stopped_beating(beat)
    assert await wait_until_dead(child_pid)
    with Session(sqlite_engine) as session:
        assert session.get(Run, run_id).state == RunState.cancelled


@pytest.mark.asyncio
async def test_cancelling_one_run_leaves_another_source_untouched(
    sqlite_engine, tmp_path, monkeypatch, blocking_binary
):
    """Admission only serialises per source, so two sources run concurrently.

    A cancellation that reached for a pid, or for "the running process", would
    take both down. Ownership is per run id precisely so it cannot.
    """
    monkeypatch.setattr(runner, "_global_sem", asyncio.Semaphore(2))
    first, second = create_source_tasks(sqlite_engine, count=2)
    with Session(sqlite_engine) as session:
        other_source = session.get(Task, second.id)
        replacement = runner.Source(
            name="second-source",
            user="backup",
            host="second.example",
            ssh_key_path="/tmp/test-key",
        )
        session.add(replacement)
        session.commit()
        session.refresh(replacement)
        other_source.source_id = replacement.id
        session.add(other_source)
        session.commit()

    release_file, _ = blocking_binary("rsync", "isolation-rsync")
    first_run, first_exec, first_captured = await start_blocking_run(
        sqlite_engine, first.id, monkeypatch
    )
    second_run, second_exec, second_captured = await start_blocking_run(
        sqlite_engine, second.id, monkeypatch
    )

    try:
        result = await runner.cancel_run(first_run)
        assert result.outcome == runner.CancelOutcome.cancelled
        assert second_captured["process"].returncode is None
        with Session(sqlite_engine) as session:
            assert session.get(Run, second_run).state == RunState.running
    finally:
        release_file.touch()
        await asyncio.gather(first_exec, second_exec, return_exceptions=True)
        for captured in (first_captured, second_captured):
            process = captured.get("process")
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()

    with Session(sqlite_engine) as session:
        assert session.get(Run, first_run).state == RunState.cancelled
        assert session.get(Run, second_run).state == RunState.success


@pytest.mark.asyncio
async def test_cancelling_a_run_that_finished_naturally_reports_its_real_outcome(
    sqlite_engine, tmp_path, monkeypatch, blocking_binary
):
    """The completion-vs-cancel race, resolved by reading the row afterwards.

    A cancel that arrives while the run is already terminalizing must not claim
    to have cancelled it. The honest answer is the state the run actually
    reached.
    """
    (task,) = create_source_tasks(sqlite_engine, count=1)
    release_file, _ = blocking_binary("rsync", "race-rsync")
    run_id, execution, captured = await start_blocking_run(
        sqlite_engine, task.id, monkeypatch
    )

    release_file.touch()
    await asyncio.gather(execution, return_exceptions=True)
    result = await runner.cancel_run(run_id)

    assert result.outcome == runner.CancelOutcome.already_finished
    assert result.state == RunState.success
    with Session(sqlite_engine) as session:
        assert session.get(Run, run_id).state == RunState.success


@pytest.mark.asyncio
async def test_cancelling_twice_reports_the_second_call_as_already_finished(
    sqlite_engine, tmp_path, monkeypatch, blocking_binary
):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    release_file, _ = blocking_binary("rsync", "twice-rsync")
    run_id, execution, captured = await start_blocking_run(
        sqlite_engine, task.id, monkeypatch
    )

    try:
        first = await runner.cancel_run(run_id)
        second = await runner.cancel_run(run_id)
    finally:
        await drain(execution, release_file, captured.get("process"))

    assert first.outcome == runner.CancelOutcome.cancelled
    assert second.outcome == runner.CancelOutcome.already_finished
    assert second.state == RunState.cancelled


@pytest.mark.asyncio
async def test_cancelling_an_unknown_run_is_not_found(sqlite_engine):
    result = await runner.cancel_run(4242)
    assert result.outcome == runner.CancelOutcome.not_found
    assert result.state is None


@pytest.mark.asyncio
async def test_cancelling_an_unowned_pending_run_refuses_to_terminalize_it(
    sqlite_engine,
):
    """A pending row this process never started executing is not ours to end.

    `admit_run` creates the row; `start_admitted_run` is what takes ownership.
    Between the two there is no process to signal, and inventing a terminal
    state for a row someone else may be about to execute is exactly the
    "terminalize something you do not own" failure the ownership map exists to
    prevent.
    """
    (task,) = create_source_tasks(sqlite_engine, count=1)
    admission = await runner.admit_run(task.id)

    result = await runner.cancel_run(admission.accepted_run_id())

    assert result.outcome == runner.CancelOutcome.not_owned
    assert result.state == RunState.pending
    with Session(sqlite_engine) as session:
        assert session.get(Run, admission.run_id).state == RunState.pending


@pytest.mark.asyncio
async def test_the_ownership_map_is_empty_once_a_run_settles(
    sqlite_engine, tmp_path, monkeypatch, blocking_binary
):
    """Both exits — natural completion and cancellation — must release the entry.

    The map is keyed by run id and lives for the life of the process. A leaked
    entry is an unbounded dict AND a stale handle that a later cancellation of a
    recycled id could act on.
    """
    first, second = create_source_tasks(sqlite_engine, count=2)
    release_file, _ = blocking_binary("rsync", "ownership-rsync")

    run_id, execution, captured = await start_blocking_run(
        sqlite_engine, first.id, monkeypatch
    )
    assert run_id in runner._run_owners
    await drain(execution, release_file, captured.get("process"))
    assert run_id not in runner._run_owners

    release_file.unlink()
    cancelled_id, cancelled_exec, cancelled_captured = await start_blocking_run(
        sqlite_engine, second.id, monkeypatch
    )
    try:
        await runner.cancel_run(cancelled_id)
    finally:
        await drain(cancelled_exec, release_file, cancelled_captured.get("process"))
    assert cancelled_id not in runner._run_owners
    assert runner._run_owners == {}


# --------------------------------------------------------------------------
# 2. the HTTP surface
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_cancel_endpoint_stops_a_running_run(
    sqlite_engine, tmp_path, monkeypatch, blocking_binary
):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    release_file, _ = blocking_binary("rsync", "endpoint-rsync")
    run_id, execution, captured = await start_blocking_run(
        sqlite_engine, task.id, monkeypatch
    )

    try:
        with Session(sqlite_engine) as session:
            body = await runs_api.cancel_run(run_id, session)
    finally:
        await drain(execution, release_file, captured.get("process"))

    assert body == {"cancelled": True, "state": "cancelled"}


@pytest.mark.asyncio
async def test_the_cancel_endpoint_404s_for_an_unknown_run(sqlite_engine):
    with Session(sqlite_engine) as session:
        with pytest.raises(HTTPException) as raised:
            await runs_api.cancel_run(4242, session)
    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_the_cancel_endpoint_409s_for_a_finished_run(
    sqlite_engine, tmp_path, monkeypatch, blocking_binary
):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    release_file, _ = blocking_binary("rsync", "finished-rsync")
    run_id, execution, captured = await start_blocking_run(
        sqlite_engine, task.id, monkeypatch
    )
    await drain(execution, release_file, captured.get("process"))

    with Session(sqlite_engine) as session:
        with pytest.raises(HTTPException) as raised:
            await runs_api.cancel_run(run_id, session)

    assert raised.value.status_code == 409
    assert "success" in raised.value.detail


@pytest.mark.asyncio
async def test_the_cancel_endpoint_409s_for_a_run_this_process_does_not_own(
    sqlite_engine,
):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    admission = await runner.admit_run(task.id)

    with Session(sqlite_engine) as session:
        with pytest.raises(HTTPException) as raised:
            await runs_api.cancel_run(admission.accepted_run_id(), session)

    assert raised.value.status_code == 409
    assert "not owned" in raised.value.detail
    with Session(sqlite_engine) as session:
        assert session.get(Run, admission.run_id).state == RunState.pending


@pytest.mark.asyncio
async def test_the_cancel_endpoint_requires_authentication():
    """Cancellation is a destructive control, so it lives behind the credential.

    Proven through the real middleware rather than by reading the router: the
    allowlist in http_auth.py is what decides, not the router's prefix.
    """
    from fastapi.testclient import TestClient

    from pullbackup.main import app

    response = TestClient(app).post("/api/runs/1/cancel")
    assert response.status_code == 401


# --------------------------------------------------------------------------
# 3. disabling a task while it runs
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabling_a_task_mid_run_stops_future_scheduling_without_signalling(
    sqlite_engine, tmp_path, monkeypatch, blocking_binary, unstarted_scheduler_next_run):
    """The whole point of separating the three actions.

    Disable is a scheduling change. The running transfer must be allowed to
    finish normally — so this asserts BOTH that the scheduler job is gone and
    that no signal was sent and no cancellation was requested.
    """
    (task,) = create_source_tasks(sqlite_engine, count=1)
    release_file, _ = blocking_binary("rsync", "disable-rsync")
    signalled = []
    monkeypatch.setattr(
        runner.os, "killpg", lambda pid, sig: signalled.append((pid, sig))
    )
    cancelled = []

    async def observed_cancel_run(run_id):
        cancelled.append(run_id)
        raise AssertionError("disabling a task must not cancel its running run")

    monkeypatch.setattr(runner, "cancel_run", observed_cancel_run)

    run_id, execution, captured = await start_blocking_run(
        sqlite_engine, task.id, monkeypatch
    )
    scheduler.upsert_job(task)
    assert scheduler.get_scheduler().get_job(f"task-{task.id}") is not None

    with Session(sqlite_engine) as session:
        updated = await tasks_api.update_task(
            task.id, task_input(task, enabled=False), session
        )

    assert updated.enabled is False
    assert scheduler.get_scheduler().get_job(f"task-{task.id}") is None
    assert signalled == []
    assert cancelled == []
    assert captured["process"].returncode is None, (
        "the running subprocess was killed by a scheduling change"
    )

    await drain(execution, release_file, captured.get("process"))

    with Session(sqlite_engine) as session:
        run = session.get(Run, run_id)
    assert run.state == RunState.success, (
        "a run disabled mid-flight must still be allowed to finish normally"
    )
    assert signalled == []


@pytest.mark.asyncio
async def test_re_enabling_a_task_mid_run_only_restores_future_scheduling(
    sqlite_engine, tmp_path, monkeypatch, blocking_binary, unstarted_scheduler_next_run):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    with Session(sqlite_engine) as session:
        stored = session.get(Task, task.id)
        stored.enabled = False
        session.add(stored)
        session.commit()
        session.refresh(stored)
        task = stored
    release_file, _ = blocking_binary("rsync", "reenable-rsync")
    signalled = []
    monkeypatch.setattr(
        runner.os, "killpg", lambda pid, sig: signalled.append((pid, sig))
    )

    run_id, execution, captured = await start_blocking_run(
        sqlite_engine, task.id, monkeypatch
    )
    assert scheduler.get_scheduler().get_job(f"task-{task.id}") is None

    with Session(sqlite_engine) as session:
        updated = await tasks_api.update_task(
            task.id, task_input(task, enabled=True), session
        )

    assert updated.enabled is True
    assert scheduler.get_scheduler().get_job(f"task-{task.id}") is not None
    assert signalled == []
    assert captured["process"].returncode is None

    await drain(execution, release_file, captured.get("process"))
    with Session(sqlite_engine) as session:
        assert session.get(Run, run_id).state == RunState.success


# --------------------------------------------------------------------------
# 4. which fields may be edited while a run is active
# --------------------------------------------------------------------------


SAFE_CHANGES = {
    "name": "renamed",
    "description": "a new description",
    "cron": "17 4 * * *",
    "enabled": False,
    "notify_matrix": True,
    "notify_matrix_on_success": True,
    "kuma_enabled": True,
}

# Every field that shapes the command the runner already built, or that decides
# whose data is being copied where. Changing any of these mid-run would make the
# stored row disagree with the transfer in flight.
LOCKED_CHANGES = {
    "source_id": None,  # filled in per-test with a real replacement source
    "remote_path": "/remote/changed",
    "local_path": None,  # filled in per-test: must stay inside a dest root
    "task_type": "syncoid",
    "syncoid_recursive": False,
    "syncoid_no_sync_snap": False,
    "syncoid_compress": "lz4",
    "syncoid_extra_args": "--no-privilege-elevation",
    "syncoid_force_full": True,
    "prune_keep_hourly": 24,
    "archive": False,
    "recursive": False,
    "times": False,
    "compress": False,
    "delete": True,
    "quiet": True,
    "preserve_permissions": True,
    "preserve_xattrs": True,
    "delay_updates": False,
    "use_sudo": True,
    "bwlimit_kbps": 4096,
    "exclude_patterns": "*.tmp",
    "aux_args": "--partial",
}


def test_the_safe_and_locked_sets_together_cover_every_task_field():
    """A field added later must land in exactly one of the two sets.

    Without this, a new command-shaping field defaults to whichever behaviour
    the implementation happens to give it, and nothing fails.
    """
    covered = set(SAFE_CHANGES) | set(LOCKED_CHANGES)
    assert covered == set(tasks_api.TaskFields.model_fields)
    assert set(SAFE_CHANGES) == set(tasks_api.ACTIVE_RUN_SAFE_FIELDS)
    assert not (set(SAFE_CHANGES) & set(LOCKED_CHANGES))


@pytest.mark.asyncio
@pytest.mark.parametrize("field", sorted(SAFE_CHANGES))
async def test_a_safe_field_may_be_edited_while_a_run_is_active(
    sqlite_engine, monkeypatch, field, unstarted_scheduler_next_run):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    admission = await runner.admit_run(task.id)

    with Session(sqlite_engine) as session:
        updated = await tasks_api.update_task(
            task.id, task_input(task, **{field: SAFE_CHANGES[field]}), session
        )

    assert getattr(updated, field) == SAFE_CHANGES[field]
    with Session(sqlite_engine) as session:
        assert getattr(session.get(Task, task.id), field) == SAFE_CHANGES[field]
        assert session.get(Run, admission.run_id).state == RunState.pending


@pytest.mark.asyncio
@pytest.mark.parametrize("field", sorted(LOCKED_CHANGES))
async def test_a_command_shaping_field_is_locked_while_a_run_is_active(
    sqlite_engine, tmp_path, monkeypatch, field, unstarted_scheduler_next_run):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    value = LOCKED_CHANGES[field]
    if field == "source_id":
        with Session(sqlite_engine) as session:
            replacement = runner.Source(
                name="replacement",
                user="backup",
                host="replacement.example",
                ssh_key_path="/tmp/test-key",
            )
            session.add(replacement)
            session.commit()
            session.refresh(replacement)
            value = replacement.id
    elif field == "local_path":
        value = str(tmp_path / "relocated")
    changes = {field: value}
    if field == "task_type":
        # A syncoid task's paths are dataset names, so the write model would
        # reject the rsync paths before the lock is ever consulted. Change them
        # together; the assertion below still names task_type.
        changes["remote_path"] = "pool0/docker/eel"
        changes["local_path"] = "cache/docker_remote/eel"
    admission = await runner.admit_run(task.id)

    with Session(sqlite_engine) as session:
        with pytest.raises(HTTPException) as raised:
            await tasks_api.update_task(task.id, task_input(task, **changes), session)

    assert raised.value.status_code == 409
    assert field in raised.value.detail, (
        f"the 409 detail must name the unsafe field; got {raised.value.detail!r}"
    )
    with Session(sqlite_engine) as session:
        unchanged = session.get(Task, task.id)
        assert getattr(unchanged, field) != value
        assert session.get(Run, admission.run_id).state == RunState.pending


@pytest.mark.asyncio
async def test_a_full_payload_repeating_unchanged_locked_fields_is_accepted(
    sqlite_engine, monkeypatch, unstarted_scheduler_next_run):
    """The form PATCHes the whole task, not a diff.

    A lock keyed on "the payload mentions a locked field" would reject every
    edit the UI can make. The lock is on a CHANGED value.
    """
    (task,) = create_source_tasks(sqlite_engine, count=1)
    admission = await runner.admit_run(task.id)

    with Session(sqlite_engine) as session:
        updated = await tasks_api.update_task(
            task.id, task_input(task, name="renamed while running"), session
        )

    assert updated.name == "renamed while running"
    assert updated.remote_path == task.remote_path
    assert updated.local_path == task.local_path
    with Session(sqlite_engine) as session:
        assert session.get(Run, admission.run_id).state == RunState.pending


@pytest.mark.asyncio
async def test_the_409_detail_names_every_unsafe_field_not_just_the_first(
    sqlite_engine, monkeypatch, unstarted_scheduler_next_run):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    await runner.admit_run(task.id)

    with Session(sqlite_engine) as session:
        with pytest.raises(HTTPException) as raised:
            await tasks_api.update_task(
                task.id,
                task_input(task, delete=True, use_sudo=True, bwlimit_kbps=99),
                session,
            )

    detail = raised.value.detail
    for field in ("bwlimit_kbps", "delete", "use_sudo"):
        assert field in detail, f"{field} missing from {detail!r}"


@pytest.mark.asyncio
async def test_every_field_stays_editable_when_no_run_is_active(
    sqlite_engine, tmp_path, monkeypatch, unstarted_scheduler_next_run):
    """The lock must not leak into the ordinary case."""
    (task,) = create_source_tasks(sqlite_engine, count=1)

    with Session(sqlite_engine) as session:
        updated = await tasks_api.update_task(
            task.id,
            task_input(
                task,
                delete=True,
                use_sudo=True,
                bwlimit_kbps=99,
                local_path=str(tmp_path / "relocated"),
            ),
            session,
        )

    assert updated.delete is True
    assert updated.use_sudo is True
    assert updated.bwlimit_kbps == 99


@pytest.mark.asyncio
async def test_deletion_stays_locked_for_the_whole_active_window(sqlite_engine):
    """Editing loosened; deleting did not. Deleting a task unlinks the log of a
    run that is still writing to it."""
    (task,) = create_source_tasks(sqlite_engine, count=1)
    await runner.admit_run(task.id)

    with Session(sqlite_engine) as session:
        with pytest.raises(HTTPException) as raised:
            await tasks_api.delete_task(task.id, session)

    assert raised.value.status_code == 409
    with Session(sqlite_engine) as session:
        assert session.get(Task, task.id) is not None


@pytest.mark.asyncio
async def test_a_mid_run_edit_does_not_change_the_command_already_executing(
    sqlite_engine, tmp_path, monkeypatch, blocking_binary, unstarted_scheduler_next_run):
    """The runner loaded its snapshot before the exec; the edit is next-run only.

    Proven from the run's own log, which records the argv the subprocess was
    actually given.
    """
    (task,) = create_source_tasks(sqlite_engine, count=1)
    release_file, _ = blocking_binary("rsync", "snapshot-rsync")
    run_id, execution, captured = await start_blocking_run(
        sqlite_engine, task.id, monkeypatch
    )

    with Session(sqlite_engine) as session:
        await tasks_api.update_task(
            task.id, task_input(task, name="renamed mid-run", cron="9 9 * * *"), session
        )

    await drain(execution, release_file, captured.get("process"))

    with Session(sqlite_engine) as session:
        run = session.get(Run, run_id)
        stored = session.get(Task, task.id)
    assert stored.name == "renamed mid-run"
    assert stored.cron == "9 9 * * *"
    log_text = (runner.settings.log_dir / run.log_filename).read_text()
    assert task.remote_path in log_text
    assert run.state == RunState.success


@pytest.mark.asyncio
async def test_cancelling_leaves_the_task_editable_again(
    sqlite_engine, tmp_path, monkeypatch, blocking_binary, unstarted_scheduler_next_run):
    """Cancellation has to actually clear the active window, or the lock it was
    meant to relieve outlives the run forever."""
    (task,) = create_source_tasks(sqlite_engine, count=1)
    release_file, _ = blocking_binary("rsync", "editable-rsync")
    run_id, execution, captured = await start_blocking_run(
        sqlite_engine, task.id, monkeypatch
    )

    try:
        await runner.cancel_run(run_id)
    finally:
        await drain(execution, release_file, captured.get("process"))

    with Session(sqlite_engine) as session:
        updated = await tasks_api.update_task(
            task.id, task_input(task, delete=True), session
        )
    assert updated.delete is True
