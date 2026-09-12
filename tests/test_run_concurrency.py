# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""One long run must not stop every other backup.

Written RED against 1d329c061d4f, where `_execute_run` holds a process-global
`asyncio.Semaphore(settings.max_concurrent_runs)` — default 1 — for the entire
duration of a transfer (runner.py:23, runner.py:479). Observed on seal: a large
archive pull started at 18:41 UTC and no other run started for the next six
hours across all 19 enabled tasks, while their rows piled up at `pending`.

The two properties these tests pin are deliberately in tension, which is the
whole point:

  * an unrelated task must NOT wait behind a long one, and
  * two tasks writing the SAME destination must STILL wait for each other.

A fix that only raises the concurrency limit satisfies the first and breaks the
second — concurrent rsync into one directory, one side running `--delete`. So
the same-destination tests below deliberately run with a generous semaphore, so
that they fail on any build whose only exclusion is the global limit.
"""
import asyncio
from pathlib import Path

import pytest
from pullbackup import db
from pullbackup.config import ConfigurationError, Settings, load_settings
from pullbackup.models import Run, RunState, Source, Task
from pullbackup.services import runner, scheduler
from sqlmodel import Session, SQLModel, create_engine

TEST_HTTP_USERNAME = "pullback-test"
TEST_HTTP_PASSWORD = bytes(range(32)).hex()

# How long a run is given to reach `running` before we call it starved. The
# blocking rsync below spawns immediately, so this is a generous bound on
# "admitted, but never got a turn" rather than a measurement of real work.
START_TIMEOUT_SECONDS = 5
# How long we watch a run that must NOT start. Passing this is negative
# evidence, so it is kept short enough to stay cheap and long enough that the
# execution has demonstrably had its chance to run.
NO_START_WINDOW_SECONDS = 1.5


@pytest.fixture
def sqlite_engine(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pullbackup.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(runner, "engine", engine)
    monkeypatch.setattr(scheduler, "engine", engine)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setattr(runner.settings, "data_dir", tmp_path)
    monkeypatch.setattr(runner.settings, "dest_roots", str(tmp_path))
    monkeypatch.setattr(runner.settings, "zfs_dest_roots", "cache/docker_remote")
    monkeypatch.setattr(runner.settings, "http_basic_username", TEST_HTTP_USERNAME)
    monkeypatch.setattr(runner.settings, "http_basic_password", TEST_HTTP_PASSWORD)
    monkeypatch.setattr(runner, "_source_locks", {})
    monkeypatch.setattr(runner, "_active_destinations", {})
    monkeypatch.setattr(runner, "_destination_cv", None)
    monkeypatch.setattr(runner, "_destination_cv_loop", None)
    monkeypatch.setattr(runner, "_admission_lock", asyncio.Lock())
    monkeypatch.setattr(runner, "_execution_tasks", set())
    monkeypatch.setattr(runner, "_run_owners", {})
    runner.settings.log_dir.mkdir()
    return engine


def use_semaphore(monkeypatch, permits):
    monkeypatch.setattr(runner, "_global_sem", asyncio.Semaphore(permits))


def configured_permits() -> int:
    """The concurrency an operator gets with no configuration at all.

    Read from Settings rather than hard-coded, so this suite tracks the shipped
    default instead of a number copied out of it.
    """
    return Settings(_env_file=None).max_concurrent_runs  # type: ignore[call-arg]


def create_source(engine, name):
    with Session(engine) as session:
        source = Source(
            name=name,
            user="backup",
            host=f"{name}.example",
            ssh_key_path="/tmp/test-key",
        )
        session.add(source)
        session.commit()
        session.refresh(source)
        return source


def create_task(engine, source, name, local_path, remote_path=None, **fields):
    with Session(engine) as session:
        task = Task(
            name=name,
            source_id=source.id,
            remote_path=remote_path or f"/remote/{name}",
            local_path=str(local_path),
            cron="0 0 * * *",
            **fields,
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        return task


def install_blocking_transfer(tmp_path, monkeypatch):
    """Put a blocking `rsync` and `syncoid` on PATH and return their release file.

    Neither ever transfers anything: they spin until the release file appears.
    That is what lets a test hold one run "in progress" for as long as it needs
    without touching the network or a real destination.
    """
    release_file = tmp_path / "transfer.release"
    bin_dir = tmp_path / "blocking-bin"
    bin_dir.mkdir()
    for name in ("rsync", "syncoid"):
        executable = bin_dir / name
        executable.write_text(
            "#!/bin/sh\n"
            'while [ ! -f "$PULLBACK_TEST_RELEASE_FILE" ]; do sleep 0.01; done\n'
            "exit 0\n"
        )
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/bin:/usr/bin")
    monkeypatch.setenv("PULLBACK_TEST_RELEASE_FILE", str(release_file))
    return release_file


def run_state(engine, run_id):
    with Session(engine) as session:
        run = session.get(Run, run_id)
        return None if run is None else run.state


async def start_run(engine, task):
    """Admit and begin executing `task`, returning (run_id, execution)."""
    admission = await runner.admit_run(task.id)
    assert admission.accepted, f"admission denied: {admission.reason}"
    run_id = admission.accepted_run_id()
    return run_id, asyncio.create_task(runner.execute_run(run_id))


async def wait_for_running(engine, run_id, timeout=START_TIMEOUT_SECONDS):
    """True once `run_id` is executing, False if it never got a turn."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if run_state(engine, run_id) == RunState.running:
            return True
        await asyncio.sleep(0.02)
    return False


async def wait_for_start(engine, run_id, timeout=START_TIMEOUT_SECONDS):
    """True once `run_id` has left `pending` by any route.

    `wait_for_running` samples a transient state, so a run whose stub transfer
    exits immediately can pass through `running` between two polls and be
    reported as never started. Where the property under test is "it got its
    turn" rather than "it is currently executing", this is the honest check.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if run_state(engine, run_id) != RunState.pending:
            return True
        await asyncio.sleep(0.02)
    return False


async def settle(executions, release_file):
    release_file.touch()
    await asyncio.gather(
        *(asyncio.wait_for(execution, timeout=10) for execution in executions),
        return_exceptions=True,
    )


@pytest.mark.asyncio
async def test_a_long_run_does_not_starve_an_unrelated_task(
    sqlite_engine, tmp_path, monkeypatch
):
    """The reported defect, reproduced without transferring a byte.

    Two different sources, two different destinations, nothing shared. Under the
    default configuration the second task must still get to run while the first
    is mid-transfer. At 1d329c061d4f it does not: it is admitted, its row is
    written `pending`, and it waits on the global semaphore until the first run
    finishes — which for the archive task on seal had not happened in six hours.
    """
    use_semaphore(monkeypatch, configured_permits())
    release_file = install_blocking_transfer(tmp_path, monkeypatch)

    archive = create_task(
        sqlite_engine, create_source(sqlite_engine, "archive-src"),
        "archive", tmp_path / "dest-archive",
    )
    hourly = create_task(
        sqlite_engine, create_source(sqlite_engine, "hourly-src"),
        "hourly", tmp_path / "dest-hourly",
    )

    archive_id, archive_execution = await start_run(sqlite_engine, archive)
    hourly_id = None
    hourly_execution = None
    try:
        assert await wait_for_running(sqlite_engine, archive_id), (
            "the long run never started, so this test proves nothing"
        )
        hourly_id, hourly_execution = await start_run(sqlite_engine, hourly)
        assert await wait_for_running(sqlite_engine, hourly_id), (
            "an unrelated task never started while a long run was in progress: "
            f"its state is {run_state(sqlite_engine, hourly_id)}. This is the "
            "starvation reported on seal — every other backup silently stops "
            "for the duration of one long transfer."
        )
    finally:
        executions = [archive_execution]
        if hourly_execution is not None:
            executions.append(hourly_execution)
        await settle(executions, release_file)


@pytest.mark.asyncio
async def test_two_tasks_sharing_a_destination_still_serialize(
    sqlite_engine, tmp_path, monkeypatch
):
    """Raising the limit must not let two runs write one directory at once.

    Deliberately run with four permits, so the global limit cannot be what
    provides the exclusion. At 1d329c061d4f nothing else does: exclusion is
    keyed on `source.id` alone (runner.py:191, runner.py:479), so two tasks on
    different sources pointing at the same `local_path` overlap — one of them
    possibly running `--delete` against files the other is still writing.
    """
    use_semaphore(monkeypatch, 4)
    release_file = install_blocking_transfer(tmp_path, monkeypatch)

    shared = tmp_path / "dest-shared"
    first = create_task(
        sqlite_engine, create_source(sqlite_engine, "first-src"), "first", shared
    )
    second = create_task(
        sqlite_engine, create_source(sqlite_engine, "second-src"), "second", shared
    )

    first_id, first_execution = await start_run(sqlite_engine, first)
    second_id = None
    second_execution = None
    try:
        assert await wait_for_running(sqlite_engine, first_id), (
            "the first run never started, so this test proves nothing"
        )
        second_id, second_execution = await start_run(sqlite_engine, second)
        # See the note above: leaving `pending` at all is the overlap.
        started = await wait_for_start(
            sqlite_engine, second_id, timeout=NO_START_WINDOW_SECONDS
        )
        assert not started, (
            "two runs are writing the same destination concurrently; with "
            "--delete on either side that is data loss, not a speed-up"
        )
    finally:
        executions = [first_execution]
        if second_execution is not None:
            executions.append(second_execution)
        await settle(executions, release_file)


@pytest.mark.asyncio
async def test_a_nested_destination_still_serializes(
    sqlite_engine, tmp_path, monkeypatch
):
    """Containment, not string equality, is what makes two destinations conflict.

    `/dest/photos` and `/dest/photos/raw` are different paths that are the same
    bytes on disk. An exclusion keyed on the exact path would let a `--delete`
    sync of the parent run against a write into the child.
    """
    use_semaphore(monkeypatch, 4)
    release_file = install_blocking_transfer(tmp_path, monkeypatch)

    parent_path = tmp_path / "dest-photos"
    parent = create_task(
        sqlite_engine, create_source(sqlite_engine, "parent-src"), "parent", parent_path
    )
    child = create_task(
        sqlite_engine, create_source(sqlite_engine, "child-src"),
        "child", parent_path / "raw",
    )

    parent_id, parent_execution = await start_run(sqlite_engine, parent)
    child_id = None
    child_execution = None
    try:
        assert await wait_for_running(sqlite_engine, parent_id), (
            "the parent run never started, so this test proves nothing"
        )
        child_id, child_execution = await start_run(sqlite_engine, child)
        # `wait_for_start`, not `wait_for_running`: a stub transfer can begin and
        # finish between two polls, and a check that only samples `running`
        # would score that overlap as a pass.
        started = await wait_for_start(
            sqlite_engine, child_id, timeout=NO_START_WINDOW_SECONDS
        )
        assert not started, (
            "a run into a subdirectory started while its parent directory was "
            "being synced"
        )
    finally:
        executions = [parent_execution]
        if child_execution is not None:
            executions.append(child_execution)
        await settle(executions, release_file)


@pytest.mark.asyncio
async def test_zfs_replication_into_one_dataset_still_serializes(
    sqlite_engine, tmp_path, monkeypatch
):
    """syncoid into the same dataset must stay serialized, per the card.

    A recursive replication of `cache/docker_remote/app` covers
    `cache/docker_remote/app/db`, so these two overlap for the same reason the
    nested filesystem paths above do.
    """
    use_semaphore(monkeypatch, 4)
    release_file = install_blocking_transfer(tmp_path, monkeypatch)

    zfs = {"task_type": "syncoid"}
    parent = create_task(
        sqlite_engine, create_source(sqlite_engine, "zfs-parent-src"),
        "zfs-parent", "cache/docker_remote/app",
        remote_path="tank/app", **zfs,
    )
    child = create_task(
        sqlite_engine, create_source(sqlite_engine, "zfs-child-src"),
        "zfs-child", "cache/docker_remote/app/db",
        remote_path="tank/app/db", **zfs,
    )

    parent_id, parent_execution = await start_run(sqlite_engine, parent)
    child_id = None
    child_execution = None
    try:
        assert await wait_for_running(sqlite_engine, parent_id), (
            "the first syncoid run never started, so this test proves nothing"
        )
        child_id, child_execution = await start_run(sqlite_engine, child)
        # `wait_for_start`, not `wait_for_running`: a stub transfer can begin and
        # finish between two polls, and a check that only samples `running`
        # would score that overlap as a pass.
        started = await wait_for_start(
            sqlite_engine, child_id, timeout=NO_START_WINDOW_SECONDS
        )
        assert not started, (
            "two syncoid replications overlapped on the same ZFS dataset"
        )
    finally:
        executions = [parent_execution]
        if child_execution is not None:
            executions.append(child_execution)
        await settle(executions, release_file)


@pytest.mark.asyncio
async def test_a_released_destination_lets_the_waiting_run_proceed(
    sqlite_engine, tmp_path, monkeypatch
):
    """Exclusion must be a queue, not a drop.

    The waiting run stays admitted and starts as soon as the destination frees
    up. If it were dropped instead, a task sharing a destination would silently
    skip its schedule — a quieter version of the same defect.
    """
    use_semaphore(monkeypatch, 4)
    release_file = install_blocking_transfer(tmp_path, monkeypatch)

    shared = tmp_path / "dest-handoff"
    first = create_task(
        sqlite_engine, create_source(sqlite_engine, "handoff-a"), "handoff-a", shared
    )
    second = create_task(
        sqlite_engine, create_source(sqlite_engine, "handoff-b"), "handoff-b", shared
    )

    first_id, first_execution = await start_run(sqlite_engine, first)
    second_id, second_execution = await start_run(sqlite_engine, second)
    try:
        assert await wait_for_running(sqlite_engine, first_id), (
            "the first run never started, so this test proves nothing"
        )
        release_file.touch()
        await asyncio.wait_for(first_execution, timeout=10)
        # Waits for the run to LEAVE pending rather than to be observed
        # `running`: once the destination frees, the stub transfer exits
        # immediately, so the run can reach `success` between two polls. Either
        # way it got its turn, which is the property under test.
        assert await wait_for_start(sqlite_engine, second_id), (
            "the waiting run never started after the destination was released"
        )
    finally:
        await settle([first_execution, second_execution], release_file)

    assert run_state(sqlite_engine, second_id) == RunState.success


@pytest.mark.asyncio
async def test_a_sibling_destination_is_not_falsely_serialized(
    sqlite_engine, tmp_path, monkeypatch
):
    """A shared name PREFIX is not containment.

    `/dest/photos` and `/dest/photos-old` are unrelated directories whose joined
    paths share a prefix. An exclusion built on `startswith` would serialize
    them, which is the reported defect reintroduced under a new name: two
    backups that could safely run together, not running together.
    """
    use_semaphore(monkeypatch, 4)
    release_file = install_blocking_transfer(tmp_path, monkeypatch)

    photos = create_task(
        sqlite_engine, create_source(sqlite_engine, "photos-src"),
        "photos", tmp_path / "photos",
    )
    photos_old = create_task(
        sqlite_engine, create_source(sqlite_engine, "photos-old-src"),
        "photos-old", tmp_path / "photos-old",
    )

    photos_id, photos_execution = await start_run(sqlite_engine, photos)
    old_id = None
    old_execution = None
    try:
        assert await wait_for_running(sqlite_engine, photos_id), (
            "the first run never started, so this test proves nothing"
        )
        old_id, old_execution = await start_run(sqlite_engine, photos_old)
        assert await wait_for_running(sqlite_engine, old_id), (
            "a sibling directory was serialized behind an unrelated backup "
            "merely because their paths share a prefix"
        )
    finally:
        executions = [photos_execution]
        if old_execution is not None:
            executions.append(old_execution)
        await settle(executions, release_file)


@pytest.mark.asyncio
async def test_the_same_directory_spelled_differently_still_serializes(
    sqlite_engine, tmp_path, monkeypatch
):
    """Exclusion must survive a cosmetic rewrite of the stored path.

    `<tmp>/shared` and `<tmp>/./shared/` are one directory written two ways. If
    the key were the raw `local_path` string, editing a task's path into an
    equivalent spelling would silently disable the exclusion protecting it —
    without moving a byte on disk.
    """
    use_semaphore(monkeypatch, 4)
    release_file = install_blocking_transfer(tmp_path, monkeypatch)

    canonical = create_task(
        sqlite_engine, create_source(sqlite_engine, "canonical-src"),
        "canonical", tmp_path / "spelled",
    )
    equivalent = create_task(
        sqlite_engine, create_source(sqlite_engine, "equivalent-src"),
        "equivalent", f"{tmp_path}/./spelled/",
    )

    canonical_id, canonical_execution = await start_run(sqlite_engine, canonical)
    equivalent_id = None
    equivalent_execution = None
    try:
        assert await wait_for_running(sqlite_engine, canonical_id), (
            "the first run never started, so this test proves nothing"
        )
        equivalent_id, equivalent_execution = await start_run(
            sqlite_engine, equivalent
        )
        started = await wait_for_start(
            sqlite_engine, equivalent_id, timeout=NO_START_WINDOW_SECONDS
        )
        assert not started, (
            "the same directory spelled differently escaped the exclusion"
        )
    finally:
        executions = [canonical_execution]
        if equivalent_execution is not None:
            executions.append(equivalent_execution)
        await settle(executions, release_file)


def test_a_held_destination_is_never_forgotten_on_a_loop_change(monkeypatch):
    """Rebinding must refuse while a destination is held, not clear it.

    Review finding on PR #45: the first version rebound on loop identity alone
    and cleared `_active_destinations` with it, reasoning that entries under a
    previous loop must belong to finished runs. That is an assumption, not a
    fact. With two loops live in one process it forgets a RUNNING holder, and
    the next run acquires the overlapping destination unopposed — the exact
    concurrent-writer case this exclusion exists to prevent.

    Refusing costs one run and says why. Guessing costs a destination.
    """
    monkeypatch.setattr(runner, "_destination_cv", None)
    monkeypatch.setattr(runner, "_destination_cv_loop", None)
    monkeypatch.setattr(runner, "_active_destinations", {})

    async def bind():
        runner._destination_condition()

    asyncio.run(bind())
    bound_to = runner._destination_cv

    # A run on the now-finished loop is still recorded as holding a
    # destination. A second loop must not be allowed to ignore it.
    runner._active_destinations[1] = ("fs", "/", "dest", "shared")

    async def rebind():
        runner._destination_condition()

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(rebind())

    assert "different event loop" in str(raised.value)
    assert runner._active_destinations == {1: ("fs", "/", "dest", "shared")}, (
        "the held destination was dropped despite the refusal"
    )
    assert runner._destination_cv is bound_to, (
        "the condition was rebound despite the refusal"
    )


def test_a_fresh_loop_binds_cleanly_when_nothing_is_held(monkeypatch):
    """The refusal must not break the ordinary case it guards.

    With no destination held there is no holder to forget, so a new loop binds
    without complaint. Paired with the test above so a uniformly-raising
    implementation cannot satisfy the guard vacuously.
    """
    monkeypatch.setattr(runner, "_destination_cv", None)
    monkeypatch.setattr(runner, "_destination_cv_loop", None)
    monkeypatch.setattr(runner, "_active_destinations", {})

    async def bind():
        return runner._destination_condition()

    first = asyncio.run(bind())
    second = asyncio.run(bind())
    assert first is not second, "a stale condition was reused on a new loop"


def test_the_default_concurrency_no_longer_serializes_everything():
    """The shipped default must let more than one backup run at a time.

    Pinned as a property rather than an exact number: the point is that an
    operator who configures nothing does not get a system where one long task
    stops all the others.
    """
    assert configured_permits() > 1, (
        "the default concurrency is 1, so a single long run blocks every other "
        "backup exactly as it did on seal"
    )


def test_the_loader_refuses_a_concurrency_that_cannot_run_anything():
    """Zero or negative permits is a system that never runs a backup.

    Refused at startup rather than clamped, mirroring
    `_validate_auth_store_max_entries`: a silently corrected value is how an
    operator ends up believing their configuration took effect while the
    process runs on a number they never chose.
    """
    for value in ("0", "-1", "-5"):
        with pytest.raises(ConfigurationError) as raised:
            load_settings(
                {
                    "PULLBACKUP_HTTP_BASIC_USERNAME": TEST_HTTP_USERNAME,
                    "PULLBACKUP_HTTP_BASIC_PASSWORD": TEST_HTTP_PASSWORD,
                    "PULLBACKUP_MAX_CONCURRENT_RUNS": value,
                },
                env_file=None,
            )
        message = str(raised.value)
        assert "PULLBACKUP_MAX_CONCURRENT_RUNS" in message, (
            f"the error does not name the variable to fix: {message!r}"
        )


def test_a_deliberate_full_serialization_is_still_allowed():
    """1 must remain configurable: some deployments really do want one at a time."""
    loaded = load_settings(
        {
            "PULLBACKUP_HTTP_BASIC_USERNAME": TEST_HTTP_USERNAME,
            "PULLBACKUP_HTTP_BASIC_PASSWORD": TEST_HTTP_PASSWORD,
            "PULLBACKUP_MAX_CONCURRENT_RUNS": "1",
        },
        env_file=None,
    )
    assert loaded.max_concurrent_runs == 1


def test_the_bundled_configuration_documents_the_new_default():
    """compose/.env.example/README must not keep advertising the old value."""
    root = Path(__file__).parents[1]
    compose = (root / "docker" / "compose.example.yaml").read_text()
    env_example = (root / ".env.example").read_text()
    readme = (root / "README.md").read_text()
    default = configured_permits()

    assert f'PULLBACKUP_MAX_CONCURRENT_RUNS: "{default}"' in compose
    assert f"PULLBACKUP_MAX_CONCURRENT_RUNS={default}" in env_example
    assert "PULLBACKUP_MAX_CONCURRENT_RUNS" in readme, (
        "the setting that decides whether one long backup stops all the others "
        "is not mentioned in the README"
    )
