import asyncio
import base64
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException
from pullbackup import db, main
from pullbackup.api import sources as sources_api
from pullbackup.api import tasks as tasks_api
from pullbackup.config import Settings
from pullbackup.models import Run, RunState, Source, Task
from pullbackup.services import fs, runner, scheduler
from sqlmodel import Session, SQLModel, create_engine, select

TEST_HTTP_USERNAME = "pullback-test"
TEST_HTTP_PASSWORD = bytes(range(32)).hex()


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
    monkeypatch.setattr(runner, "_global_sem", asyncio.Semaphore(1))
    monkeypatch.setattr(runner, "_source_locks", {})
    monkeypatch.setattr(runner, "_admission_lock", asyncio.Lock())
    monkeypatch.setattr(runner, "_execution_tasks", set())
    # Per-run cancellation ownership. Reset with the rest of the runner's
    # process-global state so one test's execution can never be visible — or
    # cancellable — from another.
    monkeypatch.setattr(runner, "_run_owners", {})
    runner.settings.log_dir.mkdir()
    return engine


def create_source_tasks(engine, count=2):
    data_dir = Path(engine.url.database).parent
    with Session(engine) as session:
        source = Source(
            name="source",
            user="backup",
            host="source.example",
            ssh_key_path="/tmp/test-key",
        )
        session.add(source)
        session.commit()
        session.refresh(source)
        tasks = [
            Task(
                name=f"task-{index}",
                source_id=source.id,
                remote_path=f"/remote/{index}",
                local_path=str(data_dir / f"local-{index}"),
                cron="0 0 * * *",
            )
            for index in range(count)
        ]
        session.add_all(tasks)
        session.commit()
        for task in tasks:
            session.refresh(task)
        return tasks


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


def task_input(task, **changes):
    values = {
        name: getattr(task, name)
        for name in tasks_api.TaskIn.model_fields
    }
    values.update(changes)
    return tasks_api.TaskIn(**values)


def install_blocking_rsync(tmp_path, monkeypatch, name="blocking-rsync"):
    release_file = tmp_path / f"{name}.release"
    bin_dir = tmp_path / name
    bin_dir.mkdir()
    rsync = bin_dir / "rsync"
    rsync.write_text(
        "#!/bin/sh\n"
        'while [ ! -f "$PULLBACK_TEST_RELEASE_FILE" ]; do sleep 0.01; done\n'
        "exit 0\n"
    )
    rsync.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/bin:/usr/bin")
    monkeypatch.setenv("PULLBACK_TEST_RELEASE_FILE", str(release_file))
    return release_file


def release_blocking_rsync(release_file):
    release_file.touch()


@pytest.mark.asyncio
async def test_concurrent_admission_allows_only_one_task_from_a_source(sqlite_engine):
    first, second = create_source_tasks(sqlite_engine)

    admissions = await asyncio.gather(
        runner.admit_run(first.id),
        runner.admit_run(second.id),
    )

    accepted = [admission for admission in admissions if admission.accepted]
    denied = [admission for admission in admissions if not admission.accepted]
    assert len(accepted) == 1
    assert len(denied) == 1
    assert denied[0].reason == "source_active"
    with Session(sqlite_engine) as session:
        runs = session.exec(select(Run)).all()
    assert len(runs) == 1
    assert runs[0].id == accepted[0].run_id
    assert runs[0].state == RunState.pending


@pytest.mark.asyncio
async def test_delete_rejects_an_admitted_pending_run(sqlite_engine):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    admission = await runner.admit_run(task.id)

    with Session(sqlite_engine) as session:
        with pytest.raises(HTTPException) as raised:
            await tasks_api.delete_task(task.id, session)

    assert raised.value.status_code == 409
    with Session(sqlite_engine) as session:
        assert session.get(Task, task.id) is not None
        run = session.get(Run, admission.run_id)
    assert run is not None
    assert run.state == RunState.pending


@pytest.mark.asyncio
async def test_delete_rejects_while_execution_is_running(
    sqlite_engine, tmp_path, monkeypatch
):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    release_file = install_blocking_rsync(tmp_path, monkeypatch)
    spawned = asyncio.Event()
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def observed_create_subprocess_exec(*args, **kwargs):
        process = await real_create_subprocess_exec(*args, **kwargs)
        spawned.set()
        return process

    monkeypatch.setattr(
        runner.asyncio,
        "create_subprocess_exec",
        observed_create_subprocess_exec,
    )
    admission = await runner.admit_run(task.id)
    run_id = admission.accepted_run_id()
    execution = asyncio.create_task(runner.execute_run(run_id))
    execution_result = None
    try:
        await asyncio.wait_for(spawned.wait(), timeout=5)
        with Session(sqlite_engine) as session:
            assert session.get(Run, run_id).state == RunState.running
        with Session(sqlite_engine) as session:
            with pytest.raises(HTTPException) as raised:
                await tasks_api.delete_task(task.id, session)
        assert raised.value.status_code == 409
    finally:
        release_blocking_rsync(release_file)
        execution_result = (
            await asyncio.gather(
                asyncio.wait_for(execution, timeout=5),
                return_exceptions=True,
            )
        )[0]

    assert execution_result == run_id
    with Session(sqlite_engine) as session:
        assert session.get(Task, task.id) is not None
        run = session.get(Run, run_id)
    assert run is not None
    assert run.state == RunState.success


@pytest.mark.asyncio
async def test_cancelling_execution_terminates_child_and_terminalizes_run(
    sqlite_engine, tmp_path, monkeypatch
):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    release_file = install_blocking_rsync(tmp_path, monkeypatch, "cancel-rsync")
    spawned = asyncio.Event()
    child = None
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def observed_create_subprocess_exec(*args, **kwargs):
        nonlocal child
        child = await real_create_subprocess_exec(*args, **kwargs)
        spawned.set()
        return child

    monkeypatch.setattr(
        runner.asyncio,
        "create_subprocess_exec",
        observed_create_subprocess_exec,
    )
    admission = await runner.admit_run(task.id)
    run_id = admission.accepted_run_id()
    execution = asyncio.create_task(runner.execute_run(run_id))
    child_was_alive = None
    run_state = None
    exit_code = None
    error_message = None
    finished_at = None

    try:
        await asyncio.wait_for(spawned.wait(), timeout=5)
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
        child_was_alive = child.returncode is None
        with Session(sqlite_engine) as session:
            run = session.get(Run, run_id)
            run_state = run.state
            exit_code = run.exit_code
            error_message = run.error_message
            finished_at = run.finished_at
    finally:
        release_blocking_rsync(release_file)
        if child is not None and child.returncode is None:
            try:
                await asyncio.wait_for(child.wait(), timeout=5)
            except TimeoutError:
                child.kill()
                await child.wait()

    assert child_was_alive is False
    assert run_state == RunState.failed
    assert exit_code == -1
    assert error_message == "run cancelled"
    assert finished_at is not None


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_interrupt_process_cleanup(
    sqlite_engine, tmp_path, monkeypatch
):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    release_file = install_blocking_rsync(tmp_path, monkeypatch, "recancel-rsync")
    spawned = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    child = None
    real_create_subprocess_exec = asyncio.create_subprocess_exec
    real_terminate_process_group = runner._terminate_process_group

    async def observed_create_subprocess_exec(*args, **kwargs):
        nonlocal child
        child = await real_create_subprocess_exec(*args, **kwargs)
        spawned.set()
        return child

    async def gated_terminate_process_group(proc):
        cleanup_started.set()
        await cleanup_release.wait()
        await real_terminate_process_group(proc)

    monkeypatch.setattr(
        runner.asyncio,
        "create_subprocess_exec",
        observed_create_subprocess_exec,
    )
    monkeypatch.setattr(
        runner,
        "_terminate_process_group",
        gated_terminate_process_group,
    )
    admission = await runner.admit_run(task.id)
    run_id = admission.accepted_run_id()
    execution = asyncio.create_task(runner.execute_run(run_id))
    child_was_alive = None
    run_state = None

    try:
        await asyncio.wait_for(spawned.wait(), timeout=5)
        execution.cancel()
        await asyncio.wait_for(cleanup_started.wait(), timeout=5)
        execution.cancel()
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await execution
        child_was_alive = child.returncode is None
        with Session(sqlite_engine) as session:
            run = session.get(Run, run_id)
            run_state = run.state
    finally:
        cleanup_release.set()
        release_blocking_rsync(release_file)
        if child is not None and child.returncode is None:
            try:
                await asyncio.wait_for(child.wait(), timeout=5)
            except TimeoutError:
                child.kill()
                await child.wait()

    assert child_was_alive is False
    assert run_state == RunState.failed


@pytest.mark.asyncio
async def test_shutdown_cancels_owned_execution_and_terminalizes_run(
    sqlite_engine, tmp_path, monkeypatch
):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    release_file = install_blocking_rsync(tmp_path, monkeypatch, "shutdown-rsync")
    spawned = asyncio.Event()
    child = None
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def observed_create_subprocess_exec(*args, **kwargs):
        nonlocal child
        child = await real_create_subprocess_exec(*args, **kwargs)
        spawned.set()
        return child

    monkeypatch.setattr(
        runner.asyncio,
        "create_subprocess_exec",
        observed_create_subprocess_exec,
    )
    admission = await runner.admit_run(task.id)
    run_id = admission.accepted_run_id()
    runner.start_admitted_run(run_id)

    try:
        await asyncio.wait_for(spawned.wait(), timeout=5)
        await runner.shutdown_execution_tasks()
    finally:
        release_blocking_rsync(release_file)
        if child is not None and child.returncode is None:
            child.kill()
            await child.wait()

    assert child.returncode is not None
    assert runner._execution_tasks == set()
    with Session(sqlite_engine) as session:
        run = session.get(Run, run_id)
    assert run.state == RunState.failed
    assert run.finished_at is not None
    assert run.exit_code == -1
    assert run.error_message == "run cancelled"


@pytest.mark.asyncio
async def test_scheduler_execution_is_owned_by_shutdown_registry(
    sqlite_engine, tmp_path, monkeypatch
):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    release_file = install_blocking_rsync(tmp_path, monkeypatch, "scheduler-shutdown-rsync")
    spawned = asyncio.Event()
    child = None
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def observed_create_subprocess_exec(*args, **kwargs):
        nonlocal child
        child = await real_create_subprocess_exec(*args, **kwargs)
        spawned.set()
        return child

    monkeypatch.setattr(
        runner.asyncio,
        "create_subprocess_exec",
        observed_create_subprocess_exec,
    )
    scheduled = asyncio.create_task(scheduler._execute(task.id))

    try:
        await asyncio.wait_for(spawned.wait(), timeout=5)
        assert len(runner._execution_tasks) == 1
        await runner.shutdown_execution_tasks()
        await asyncio.gather(scheduled, return_exceptions=True)
    finally:
        release_blocking_rsync(release_file)
        if child is not None and child.returncode is None:
            child.kill()
            await child.wait()
        if not scheduled.done():
            scheduled.cancel()
            await asyncio.gather(scheduled, return_exceptions=True)

    assert child.returncode is not None
    assert runner._execution_tasks == set()
    with Session(sqlite_engine) as session:
        run = session.exec(select(Run)).one()
    assert run.state == RunState.failed
    assert run.finished_at is not None
    assert run.exit_code == -1
    assert run.error_message == "run cancelled"


@pytest.mark.asyncio
async def test_update_rejects_source_reassignment_while_run_is_active(
    sqlite_engine, monkeypatch
):
    monkeypatch.setattr(scheduler, "next_run_iso", lambda task: None)
    (task,) = create_source_tasks(sqlite_engine, count=1)
    replacement_source = create_source(sqlite_engine, "replacement")
    admission = await runner.admit_run(task.id)

    with Session(sqlite_engine) as session:
        with pytest.raises(HTTPException) as raised:
            await tasks_api.update_task(
                task.id,
                task_input(task, source_id=replacement_source.id),
                session,
            )

    assert raised.value.status_code == 409
    with Session(sqlite_engine) as session:
        unchanged_task = session.get(Task, task.id)
        run = session.get(Run, admission.run_id)
    assert unchanged_task.source_id != replacement_source.id
    assert run.state == RunState.pending


@pytest.mark.asyncio
async def test_source_update_rejects_while_run_is_active(sqlite_engine):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    admission = await runner.admit_run(task.id)
    with Session(sqlite_engine) as session:
        source = session.get(Source, task.source_id)
        assert source is not None
        assert source.id is not None
        source_id = source.id
        changed = sources_api.SourceIn(
            name=source.name,
            user=source.user,
            host="changed.example",
            port=source.port,
            ssh_key_path=source.ssh_key_path,
            description=source.description,
        )
        with pytest.raises(HTTPException) as raised:
            await sources_api.update_source(source_id, changed, session)

    assert raised.value.status_code == 409
    with Session(sqlite_engine) as session:
        unchanged_source = session.get(Source, task.source_id)
        run = session.get(Run, admission.run_id)
    assert unchanged_source.host == "source.example"
    assert run.state == RunState.pending


@pytest.mark.asyncio
async def test_mutation_first_serializes_before_later_admission(
    sqlite_engine, monkeypatch
):
    monkeypatch.setattr(scheduler, "next_run_iso", lambda task: None)
    (task,) = create_source_tasks(sqlite_engine, count=1)
    replacement_source = create_source(sqlite_engine, "serialized")
    mutation_started = asyncio.Event()
    admission_started = asyncio.Event()
    mutation = None
    admission = None

    async def mutate(session):
        mutation_started.set()
        return await tasks_api.update_task(
            task.id,
            task_input(task, source_id=replacement_source.id),
            session,
        )

    async def admit():
        admission_started.set()
        return await runner.admit_run(task.id)

    await runner._admission_lock.acquire()
    with Session(sqlite_engine) as mutation_session:
        try:
            mutation = asyncio.create_task(mutate(mutation_session))
            await mutation_started.wait()
            await asyncio.sleep(0)
            assert not mutation.done(), "task mutation did not wait for admission lock"

            admission = asyncio.create_task(admit())
            await admission_started.wait()
            await asyncio.sleep(0)
            assert not admission.done()
        finally:
            runner._admission_lock.release()
            await asyncio.gather(
                *(operation for operation in (mutation, admission) if operation),
                return_exceptions=True,
            )

    mutation_result = mutation.result()
    admission_result = admission.result()
    assert mutation_result.source_id == replacement_source.id
    assert admission_result.accepted is True
    with Session(sqlite_engine) as session:
        updated_task = session.get(Task, task.id)
        run = session.get(Run, admission_result.run_id)
    assert updated_task.source_id == replacement_source.id
    assert run.state == RunState.pending


@pytest.mark.asyncio
async def test_execution_updates_the_admitted_run_without_creating_another(
    sqlite_engine, tmp_path, monkeypatch
):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rsync = bin_dir / "rsync"
    rsync.write_text("#!/bin/sh\nexit 0\n")
    rsync.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{Path().resolve()}")
    admission = await runner.admit_run(task.id)
    run_id = admission.accepted_run_id()

    completed_id = await runner.execute_run(run_id)

    assert completed_id == run_id
    with Session(sqlite_engine) as session:
        runs = session.exec(select(Run)).all()
    assert len(runs) == 1
    assert runs[0].id == run_id
    assert runs[0].state == RunState.success


@pytest.mark.asyncio
async def test_execution_refuses_a_run_that_is_not_pending(sqlite_engine, monkeypatch):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    with Session(sqlite_engine) as session:
        active = Run(
            task_id=task.id,
            state=RunState.running,
            started_at=datetime.now(timezone.utc).replace(tzinfo=None),
            log_filename="nonpending.log",
        )
        session.add(active)
        session.commit()
        session.refresh(active)
        run_id = active.id

    async def unexpected_process(*args, **kwargs):
        pytest.fail("non-pending run started a subprocess")

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", unexpected_process)

    with pytest.raises(RuntimeError, match="is not pending"):
        await runner.execute_run(run_id)

    with Session(sqlite_engine) as session:
        active = session.get(Run, run_id)
    assert active.state == RunState.running


def test_reconcile_stale_runs_marks_only_active_rows_failed(sqlite_engine):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session(sqlite_engine) as session:
        session.add_all(
            [
                Run(task_id=task.id, state=RunState.pending),
                Run(task_id=task.id, state=RunState.running),
                Run(
                    task_id=task.id,
                    state=RunState.success,
                    finished_at=completed_at,
                    exit_code=0,
                ),
            ]
        )
        session.commit()

    reconciled = runner.reconcile_stale_runs()

    assert reconciled == 2
    with Session(sqlite_engine) as session:
        runs = session.exec(select(Run).order_by(Run.id)).all()  # type: ignore[arg-type]
    for run in runs[:2]:
        assert run.state == RunState.failed
        assert run.finished_at is not None
        assert run.exit_code == -1
        assert run.error_message == "interrupted by application restart"
    assert runs[2].state == RunState.success
    assert runs[2].finished_at == completed_at
    assert runs[2].exit_code == 0


@pytest.mark.asyncio
async def test_application_startup_reconciles_stale_runs(sqlite_engine):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    with Session(sqlite_engine) as session:
        stale = Run(task_id=task.id, state=RunState.pending)
        session.add(stale)
        session.commit()
        session.refresh(stale)
        stale_id = stale.id

    async with main.lifespan(main.app):
        with Session(sqlite_engine) as session:
            reconciled = session.get(Run, stale_id)
        assert reconciled.state == RunState.failed
        assert reconciled.finished_at is not None
        assert reconciled.exit_code == -1


@pytest.mark.parametrize("entry_path", ["run_task", "scheduler"])
@pytest.mark.asyncio
async def test_pre_spawn_owner_failure_terminalizes_running_run(
    sqlite_engine, tmp_path, entry_path
):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    blocker = tmp_path / f"{entry_path}-not-a-directory"
    blocker.write_text("file")
    with Session(sqlite_engine) as session:
        stored_task = session.get(Task, task.id)
        stored_task.local_path = str(blocker / "child")
        session.add(stored_task)
        session.commit()

    if entry_path == "run_task":
        with pytest.raises(NotADirectoryError):
            await runner.run_task(task.id)
    else:
        await scheduler._execute(task.id)

    with Session(sqlite_engine) as session:
        run = session.exec(select(Run).where(Run.task_id == task.id)).one()
    assert run.state == RunState.failed
    assert run.finished_at is not None
    assert run.exit_code == -1
    assert "NotADirectoryError" in run.error_message


@pytest.mark.asyncio
async def test_accepted_admission_without_run_id_fails_before_execution(monkeypatch):
    execution_ids = []

    async def accepted_without_id(task_id):
        return runner.RunAdmission(accepted=True)

    async def record_execution(run_id):
        execution_ids.append(run_id)
        return 0

    monkeypatch.setattr(runner, "admit_run", accepted_without_id)
    monkeypatch.setattr(runner, "execute_run", record_execution)

    with pytest.raises(RuntimeError, match="accepted run admission missing run id"):
        await runner.run_task(123)

    assert execution_ids == []


@pytest.mark.asyncio
async def test_denied_scheduled_run_logs_reason_without_creating_a_run(
    sqlite_engine, caplog
):
    caplog.set_level(logging.INFO, logger=scheduler.__name__)
    first, second = create_source_tasks(sqlite_engine)
    admitted = await runner.admit_run(first.id)

    await scheduler._execute(second.id)

    with Session(sqlite_engine) as session:
        runs = session.exec(select(Run)).all()
    assert [run.id for run in runs] == [admitted.run_id]
    denial_records = [
        record
        for record in caplog.records
        if record.getMessage()
        == f"scheduled run denied task_id={second.id} reason=source_active"
    ]
    assert len(denial_records) == 1
    assert denial_records[0].task_id == second.id
    assert denial_records[0].reason == "source_active"


def test_upserted_scheduler_job_has_bounded_overlap_and_misfires(sqlite_engine):
    (task,) = create_source_tasks(sqlite_engine, count=1)

    scheduler.upsert_job(task)

    job = scheduler.get_scheduler().get_job(f"task-{task.id}")
    assert job.max_instances == 1
    assert job.coalesce is True
    assert job.misfire_grace_time == 60


@pytest.mark.asyncio
async def test_startup_scheduler_job_has_bounded_overlap_and_misfires(sqlite_engine):
    (task,) = create_source_tasks(sqlite_engine, count=1)

    scheduler.start()
    try:
        job = scheduler.get_scheduler().get_job(f"task-{task.id}")
        assert job.max_instances == 1
        assert job.coalesce is True
        assert job.misfire_grace_time == 60
    finally:
        scheduler.shutdown()


@pytest.mark.asyncio
async def test_asgi_manual_run_returns_202_and_conflicts_while_background_runs(
    sqlite_engine, tmp_path, monkeypatch
):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    release_file = install_blocking_rsync(tmp_path, monkeypatch, "asgi-rsync")
    spawned = asyncio.Event()
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def observed_create_subprocess_exec(*args, **kwargs):
        process = await real_create_subprocess_exec(*args, **kwargs)
        spawned.set()
        return process

    def override_session():
        with Session(sqlite_engine) as session:
            yield session

    monkeypatch.setattr(
        runner.asyncio,
        "create_subprocess_exec",
        observed_create_subprocess_exec,
    )
    previous_override = main.app.dependency_overrides.get(db.get_session)
    main.app.dependency_overrides[db.get_session] = override_session
    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    first_request = None
    first_response = None

    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            auth=(TEST_HTTP_USERNAME, TEST_HTTP_PASSWORD),
        ) as client:
            first_request = asyncio.create_task(client.post(f"/api/tasks/{task.id}/run"))
            await asyncio.wait_for(spawned.wait(), timeout=5)
            duplicate_response = await client.post(f"/api/tasks/{task.id}/run")
            delete_response = await client.delete(f"/api/tasks/{task.id}")
    finally:
        release_blocking_rsync(release_file)
        if first_request is not None:
            first_response = (
                await asyncio.gather(
                    asyncio.wait_for(first_request, timeout=5),
                    return_exceptions=True,
                )
            )[0]
        for _ in range(100):
            with Session(sqlite_engine) as session:
                run = session.exec(select(Run)).one()
                if run.state not in (RunState.pending, RunState.running):
                    break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("application-owned run did not finish")
        if previous_override is None:
            main.app.dependency_overrides.pop(db.get_session, None)
        else:
            main.app.dependency_overrides[db.get_session] = previous_override

    assert isinstance(first_response, httpx.Response)
    assert first_response.status_code == 202
    assert first_response.json() == {"queued": True}
    assert duplicate_response.status_code == 409
    assert delete_response.status_code == 409
    with Session(sqlite_engine) as session:
        runs = session.exec(select(Run)).all()
    assert len(runs) == 1
    assert runs[0].state == RunState.success


@pytest.mark.asyncio
async def test_asgi_send_cancellation_does_not_orphan_admitted_run(
    sqlite_engine, tmp_path, monkeypatch
):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    release_file = install_blocking_rsync(tmp_path, monkeypatch, "asgi-owned-rsync")
    spawned = asyncio.Event()
    child = None
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def observed_create_subprocess_exec(*args, **kwargs):
        nonlocal child
        child = await real_create_subprocess_exec(*args, **kwargs)
        spawned.set()
        return child

    def override_session():
        with Session(sqlite_engine) as session:
            yield session

    monkeypatch.setattr(
        runner.asyncio,
        "create_subprocess_exec",
        observed_create_subprocess_exec,
    )
    previous_override = main.app.dependency_overrides.get(db.get_session)
    main.app.dependency_overrides[db.get_session] = override_session
    body_send_started = asyncio.Event()
    hold_send = asyncio.Event()
    app_task = None

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.body":
            body_send_started.set()
            await hold_send.wait()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"/api/tasks/{task.id}/run",
        "raw_path": f"/api/tasks/{task.id}/run".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (
                b"authorization",
                b"Basic "
                + base64.b64encode(
                    f"{TEST_HTTP_USERNAME}:{TEST_HTTP_PASSWORD}".encode()
                ),
            ),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }

    try:
        app_task = asyncio.create_task(main.app(scope, receive, send))
        await asyncio.wait_for(body_send_started.wait(), timeout=5)
        await asyncio.wait_for(spawned.wait(), timeout=1)
        app_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await app_task

        assert child.returncode is None
        release_blocking_rsync(release_file)
        for _ in range(100):
            with Session(sqlite_engine) as session:
                run = session.exec(select(Run)).one()
                if run.state not in (RunState.pending, RunState.running):
                    break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("application-owned run did not reach a terminal state")
    finally:
        hold_send.set()
        release_blocking_rsync(release_file)
        if app_task is not None and not app_task.done():
            app_task.cancel()
            await asyncio.gather(app_task, return_exceptions=True)
        if child is not None and child.returncode is None:
            child.kill()
            await child.wait()
        if previous_override is None:
            main.app.dependency_overrides.pop(db.get_session, None)
        else:
            main.app.dependency_overrides[db.get_session] = previous_override

    assert run.state == RunState.success
    assert run.finished_at is not None
    assert run.exit_code == 0


@pytest.mark.asyncio
async def test_asgi_admitted_run_waiting_for_global_slot_survives_request_completion(
    sqlite_engine, tmp_path, monkeypatch
):
    first_source = create_source(sqlite_engine, "slot-first")
    second_source = create_source(sqlite_engine, "slot-second")
    with Session(sqlite_engine) as session:
        first_task = Task(
            name="slot-first",
            source_id=first_source.id,
            remote_path="/remote/first",
            local_path=str(tmp_path / "slot-first"),
            cron="0 0 * * *",
        )
        second_task = Task(
            name="slot-second",
            source_id=second_source.id,
            remote_path="/remote/second",
            local_path=str(tmp_path / "slot-second"),
            cron="0 0 * * *",
        )
        session.add_all([first_task, second_task])
        session.commit()
        session.refresh(first_task)
        session.refresh(second_task)

    release_file = install_blocking_rsync(tmp_path, monkeypatch, "slot-cancel-rsync")
    spawned = asyncio.Event()
    spawn_count = 0
    processes = []
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def observed_create_subprocess_exec(*args, **kwargs):
        nonlocal spawn_count
        process = await real_create_subprocess_exec(*args, **kwargs)
        processes.append(process)
        spawn_count += 1
        spawned.set()
        return process

    def override_session():
        with Session(sqlite_engine) as session:
            yield session

    monkeypatch.setattr(
        runner.asyncio,
        "create_subprocess_exec",
        observed_create_subprocess_exec,
    )
    previous_override = main.app.dependency_overrides.get(db.get_session)
    main.app.dependency_overrides[db.get_session] = override_session
    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    first_request = None
    second_request = None

    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            auth=(TEST_HTTP_USERNAME, TEST_HTTP_PASSWORD),
        ) as client:
            first_request = asyncio.create_task(client.post(f"/api/tasks/{first_task.id}/run"))
            await asyncio.wait_for(spawned.wait(), timeout=5)
            second_request = asyncio.create_task(client.post(f"/api/tasks/{second_task.id}/run"))

            async def second_run_is_pending():
                with Session(sqlite_engine) as session:
                    run = session.exec(select(Run).where(Run.task_id == second_task.id)).first()
                    return run is not None and run.state == RunState.pending

            for _ in range(100):
                if await second_run_is_pending():
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("second run never reached pending state")

            first_response, second_response = await asyncio.gather(
                first_request,
                second_request,
            )
            assert first_response.status_code == 202
            assert second_response.status_code == 202
            assert spawn_count == 1

        release_blocking_rsync(release_file)
        await asyncio.wait_for(
            asyncio.gather(*list(runner._execution_tasks)),
            timeout=5,
        )

        with Session(sqlite_engine) as session:
            runs = session.exec(select(Run).order_by(Run.id)).all()  # type: ignore[arg-type]
        assert [run.state for run in runs] == [RunState.success, RunState.success]
        assert all(run.finished_at is not None for run in runs)
        assert [run.exit_code for run in runs] == [0, 0]
        assert spawn_count == 2
    finally:
        release_blocking_rsync(release_file)
        for request in (first_request, second_request):
            if request is not None and not request.done():
                request.cancel()
        await asyncio.gather(
            *(request for request in (first_request, second_request) if request is not None),
            return_exceptions=True,
        )
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if previous_override is None:
            main.app.dependency_overrides.pop(db.get_session, None)
        else:
            main.app.dependency_overrides[db.get_session] = previous_override


@pytest.mark.parametrize("cancel_command", ["list", "destroy"])
@pytest.mark.asyncio
async def test_cancellation_during_snapshot_pruning_reaps_child_and_terminalizes_run(
    sqlite_engine, tmp_path, monkeypatch, cancel_command
):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    with Session(sqlite_engine) as session:
        stored_task = session.get(Task, task.id)
        stored_task.task_type = "syncoid"
        stored_task.remote_path = f"pool/source/{cancel_command}"
        stored_task.local_path = f"cache/docker_remote/{cancel_command}"
        stored_task.prune_keep_hourly = 1
        session.add(stored_task)
        session.commit()

    bin_dir = tmp_path / "prune-cancel-bin"
    bin_dir.mkdir()
    syncoid = bin_dir / "syncoid"
    syncoid.write_text("#!/bin/sh\nexit 0\n")
    syncoid.chmod(0o755)
    zfs = bin_dir / "zfs"
    zfs.write_text(
        # Resolve the running interpreter rather than hardcoding /usr/bin/python3,
        # which does not exist in the CI container (python lives at
        # /usr/local/bin/python3). A missing interpreter makes the stub silently
        # unexecutable, so zfs_spawned never fires and the test times out.
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "if sys.argv[1] == os.environ['PULLBACK_CANCEL_ZFS_COMMAND']:\n"
        "    time.sleep(300)\n"
        "elif sys.argv[1] == 'list':\n"
        "    dataset = sys.argv[-1]\n"
        "    print(f'{dataset}@zfs-auto-snap_hourly-old')\n"
        "    print(f'{dataset}@zfs-auto-snap_hourly-new')\n"
    )
    zfs.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/bin:/usr/bin")
    monkeypatch.setenv("PULLBACK_CANCEL_ZFS_COMMAND", cancel_command)

    zfs_spawned = asyncio.Event()
    zfs_child = None
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def observed_create_subprocess_exec(*args, **kwargs):
        nonlocal zfs_child
        process = await real_create_subprocess_exec(*args, **kwargs)
        if Path(str(args[0])).name == "zfs" and args[1] == cancel_command:
            zfs_child = process
            zfs_spawned.set()
        return process

    monkeypatch.setattr(
        runner.asyncio,
        "create_subprocess_exec",
        observed_create_subprocess_exec,
    )
    admission = await runner.admit_run(task.id)
    run_id = admission.accepted_run_id()
    execution = asyncio.create_task(runner.execute_run(run_id))

    try:
        await asyncio.wait_for(zfs_spawned.wait(), timeout=5)
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution

        assert zfs_child.returncode is not None
        with Session(sqlite_engine) as session:
            run = session.get(Run, run_id)
            assert run.state == RunState.failed
            assert run.finished_at is not None
            assert run.exit_code == -1
            assert run.error_message == "run cancelled"
    finally:
        if zfs_child is not None and zfs_child.returncode is None:
            zfs_child.kill()
            await zfs_child.wait()
        if not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)


@pytest.mark.parametrize("keep", [-1, 0])
def test_task_input_rejects_nonpositive_prune_retention(keep):
    with pytest.raises(ValueError):
        tasks_api.TaskIn(
            name="unsafe-prune",
            source_id=1,
            remote_path="pool/source",
            local_path="cache/docker_remote/task",
            cron="0 0 * * *",
            task_type="syncoid",
            prune_keep_hourly=keep,
        )


@pytest.mark.parametrize("keep", [-1, 0])
@pytest.mark.asyncio
async def test_prune_rejects_nonpositive_retention_before_zfs(
    tmp_path, monkeypatch, keep
):
    async def unexpected_process(*args, **kwargs):
        pytest.fail("invalid retention reached zfs")

    monkeypatch.setattr(
        runner.asyncio,
        "create_subprocess_exec",
        unexpected_process,
    )

    with pytest.raises(ValueError, match="at least 1"):
        await runner._prune_zfs_hourly(
            "cache/docker_remote/task",
            keep,
            tmp_path / "prune.log",
        )


@pytest.mark.parametrize(
    ("allowed_roots", "dataset"),
    [
        ("", "cache/docker_remote/task"),
        ("cache/docker_remote", "cache/docker_remote"),
        ("cache/docker_remote", "cache/docker_remote_evil/task"),
        ("cache/docker_remote", "tank/other/task"),
        ("cache/docker_remote", "/cache/docker_remote/task"),
        ("cache/docker_remote", "cache/docker_remote/task@snapshot"),
        ("cache/docker_remote", "cache/docker_remote/../escape"),
        ("cache/docker_remote", "cache/docker_remote/task\nother/pool"),
    ],
)
@pytest.mark.asyncio
async def test_prune_rejects_untrusted_dataset_before_zfs(
    tmp_path, monkeypatch, allowed_roots, dataset
):
    async def unexpected_process(*args, **kwargs):
        pytest.fail("untrusted dataset reached zfs")

    monkeypatch.setattr(runner.settings, "zfs_dest_roots", allowed_roots)
    monkeypatch.setattr(
        runner.asyncio,
        "create_subprocess_exec",
        unexpected_process,
    )

    with pytest.raises(ValueError, match="ZFS destination"):
        await runner._prune_zfs_hourly(dataset, 1, tmp_path / "prune.log")


@pytest.mark.parametrize(
    "dataset",
    [
        "cache/docker_remote",
        "cache/docker_remote_evil/task",
        "tank/other/task",
        "cache/docker_remote/task@snapshot",
    ],
)
def test_task_input_rejects_untrusted_syncoid_destination(
    monkeypatch, dataset
):
    monkeypatch.setattr(
        runner.settings,
        "zfs_dest_roots",
        "cache/docker_remote",
    )

    with pytest.raises(ValueError, match="ZFS destination"):
        tasks_api.TaskIn(
            name="unsafe-target",
            source_id=1,
            remote_path="pool/source",
            local_path=dataset,
            cron="0 0 * * *",
            task_type="syncoid",
        )


def test_persisted_syncoid_destination_is_revalidated_before_command(
    monkeypatch
):
    monkeypatch.setattr(
        runner.settings,
        "zfs_dest_roots",
        "cache/docker_remote",
    )
    source = Source(
        id=1,
        name="source",
        user="backup",
        host="source.example",
        ssh_key_path="/tmp/test-key",
    )
    task = Task(
        id=1,
        name="persisted-unsafe-target",
        source_id=1,
        remote_path="pool/source",
        local_path="tank/other/task",
        cron="0 0 * * *",
        task_type="syncoid",
    )

    with pytest.raises(ValueError, match="ZFS destination"):
        runner.build_syncoid_args(task, source)


def test_persisted_syncoid_host_shell_injection_is_rejected_before_command(
    monkeypatch
):
    monkeypatch.setattr(
        runner.settings,
        "zfs_dest_roots",
        "cache/docker_remote",
    )
    source = Source(
        id=1,
        name="source",
        user="backup",
        host="host;touch /tmp/pullback-command-injection;#",
        ssh_key_path="/tmp/test-key",
    )
    task = Task(
        id=1,
        name="persisted-unsafe-source",
        source_id=1,
        remote_path="pool/source",
        local_path="cache/docker_remote/task",
        cron="0 0 * * *",
        task_type="syncoid",
    )

    with pytest.raises(ValueError, match="SSH host"):
        runner.build_syncoid_args(task, source)


def test_persisted_syncoid_user_shell_injection_is_rejected_before_command(
    monkeypatch
):
    monkeypatch.setattr(
        runner.settings,
        "zfs_dest_roots",
        "cache/docker_remote",
    )
    source = Source(
        id=1,
        name="source",
        user="backup;touch-command-injection;#",
        host="source.example",
        ssh_key_path="/tmp/test-key",
    )
    task = Task(
        id=1,
        name="persisted-unsafe-source",
        source_id=1,
        remote_path="pool/source",
        local_path="cache/docker_remote/task",
        cron="0 0 * * *",
        task_type="syncoid",
    )

    with pytest.raises(ValueError, match="SSH user"):
        runner.build_syncoid_args(task, source)


def test_persisted_syncoid_remote_dataset_shell_injection_is_rejected_before_command(
    monkeypatch
):
    monkeypatch.setattr(
        runner.settings,
        "zfs_dest_roots",
        "cache/docker_remote",
    )
    source = Source(
        id=1,
        name="source",
        user="backup",
        host="source.example",
        ssh_key_path="/tmp/test-key",
    )
    task = Task(
        id=1,
        name="persisted-unsafe-source",
        source_id=1,
        remote_path="pool/source;touch-command-injection;#",
        local_path="cache/docker_remote/task",
        cron="0 0 * * *",
        task_type="syncoid",
    )

    with pytest.raises(ValueError, match="ZFS source"):
        runner.build_syncoid_args(task, source)


def test_source_input_rejects_host_shell_injection():
    with pytest.raises(ValueError, match="SSH host"):
        sources_api.SourceIn(
            name="unsafe-source",
            user="backup",
            host="host;touch-command-injection;#",
            ssh_key_path="/tmp/test-key",
        )


def test_source_input_rejects_user_shell_injection():
    with pytest.raises(ValueError, match="SSH user"):
        sources_api.SourceIn(
            name="unsafe-source",
            user="backup;touch-command-injection;#",
            host="source.example",
            ssh_key_path="/tmp/test-key",
        )


def test_syncoid_task_input_rejects_remote_dataset_shell_injection(monkeypatch):
    monkeypatch.setattr(
        runner.settings,
        "zfs_dest_roots",
        "cache/docker_remote",
    )

    with pytest.raises(ValueError, match="ZFS source"):
        tasks_api.TaskIn(
            name="unsafe-source-dataset",
            source_id=1,
            remote_path="pool/source;touch-command-injection;#",
            local_path="cache/docker_remote/task",
            cron="0 0 * * *",
            task_type="syncoid",
        )


@pytest.mark.asyncio
async def test_concurrent_manual_requests_return_one_acceptance_and_one_conflict(
    sqlite_engine, tmp_path, monkeypatch
):
    (task,) = create_source_tasks(sqlite_engine, count=1)
    bin_dir = tmp_path / "manual-bin"
    bin_dir.mkdir()
    rsync = bin_dir / "rsync"
    rsync.write_text("#!/bin/sh\nsleep 0.1\nexit 0\n")
    rsync.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/bin:/usr/bin")

    first_background = BackgroundTasks()
    second_background = BackgroundTasks()
    with Session(sqlite_engine) as first_session, Session(sqlite_engine) as second_session:
        results = await asyncio.gather(
            tasks_api.run_now(task.id, first_background, first_session),
            tasks_api.run_now(task.id, second_background, second_session),
            return_exceptions=True,
        )

    accepted = [result for result in results if result == {"queued": True}]
    conflicts = [
        result
        for result in results
        if isinstance(result, HTTPException) and result.status_code == 409
    ]
    assert len(accepted) == 1
    assert len(conflicts) == 1
    assert len(first_background.tasks) + len(second_background.tasks) == 0
    assert len(runner._execution_tasks) == 1
    await asyncio.gather(*list(runner._execution_tasks))
    with Session(sqlite_engine) as session:
        runs = session.exec(select(Run)).all()
    assert len(runs) == 1
    assert runs[0].state == RunState.success


@pytest.mark.asyncio
async def test_execution_task_creation_failure_terminalizes_admitted_run(
    sqlite_engine, monkeypatch
):
    (task,) = create_source_tasks(sqlite_engine, count=1)

    def fail_create_task(coroutine):
        coroutine.close()
        raise RuntimeError("execution task creation failed")

    monkeypatch.setattr(runner.asyncio, "create_task", fail_create_task)
    with Session(sqlite_engine) as session:
        with pytest.raises(RuntimeError, match="task creation failed"):
            await tasks_api.run_now(task.id, BackgroundTasks(), session)

    with Session(sqlite_engine) as session:
        run = session.exec(select(Run)).one()
    assert run.state == RunState.failed
    assert run.finished_at is not None
    assert run.exit_code == -1
    assert run.error_message == "failed to start admitted run"


def test_default_global_concurrency_is_bounded_but_not_serialized(monkeypatch):
    """The default must be a real bound, not a system-wide serialization.

    Was 1, which made one long transfer stop every other backup. See
    tests/test_run_concurrency.py for the starvation this default caused and
    for the destination exclusion that makes a value above 1 safe.
    """
    from pullbackup.config import DEFAULT_MAX_CONCURRENT_RUNS

    monkeypatch.delenv("PULLBACK_MAX_CONCURRENT_RUNS", raising=False)
    default = Settings(_env_file=None).max_concurrent_runs  # type: ignore[call-arg]
    assert default == DEFAULT_MAX_CONCURRENT_RUNS
    assert default > 1


def test_zfs_destination_roots_are_explicit_and_empty_by_default(monkeypatch):
    monkeypatch.delenv("PULLBACK_ZFS_DEST_ROOTS", raising=False)
    defaults = Settings(_env_file=None)  # type: ignore[call-arg]
    configured = Settings(  # type: ignore[call-arg]
        _env_file=None,
        zfs_dest_roots=" cache/docker_remote, tank/backups/ ",
    )

    assert defaults.zfs_dest_roots_list == []
    assert configured.zfs_dest_roots_list == ["cache/docker_remote", "tank/backups"]


def test_bundled_compose_documents_the_default_concurrency():
    from pullbackup.config import DEFAULT_MAX_CONCURRENT_RUNS

    compose = (Path(__file__).parents[1] / "docker" / "compose.example.yaml").read_text()
    env_example = (Path(__file__).parents[1] / ".env.example").read_text()
    readme = (Path(__file__).parents[1] / "README.md").read_text()
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text()
    default = DEFAULT_MAX_CONCURRENT_RUNS
    assert f'PULLBACKUP_MAX_CONCURRENT_RUNS: "{default}"' in compose
    assert f"PULLBACKUP_MAX_CONCURRENT_RUNS={default}" in env_example
    assert "PULLBACKUP_ZFS_DEST_ROOTS=cache/docker_remote" in env_example
    assert "PULLBACKUP_HTTP_BASIC_USERNAME=" in env_example
    assert "PULLBACKUP_HTTP_BASIC_PASSWORD=" in env_example
    assert "PULLBACKUP_HTTP_BASIC_USERNAME" in readme
    assert "PULLBACKUP_HTTP_BASIC_PASSWORD" in readme
    assert "at least 32 characters" in readme
    assert "PULLBACKUP_ZFS_DEST_ROOTS" in readme
    assert "RUN npm ci" in dockerfile
    assert "RUN npm install" not in dockerfile
    assert "mem_limit: 512m" in compose
    assert "cpus: 1.0" in compose
    assert "pids_limit: 256" in compose


def test_bundled_compose_healthcheck_authenticates():
    compose = (Path(__file__).parents[1] / "docker" / "compose.example.yaml").read_text()
    healthcheck = compose.split("healthcheck:", maxsplit=1)[1]

    assert "Authorization" in healthcheck
    assert "Basic" in healthcheck
    assert "PULLBACKUP_HTTP_BASIC_USERNAME" in healthcheck
    assert "PULLBACKUP_HTTP_BASIC_PASSWORD" in healthcheck


@pytest.mark.parametrize("entry_path", ["run_task", "scheduler"])
@pytest.mark.asyncio
async def test_unsafe_persisted_rsync_destination_spawns_nothing(
    sqlite_engine, tmp_path, monkeypatch, entry_path
):
    """A destination persisted outside the configured roots must never run."""
    (task,) = create_source_tasks(sqlite_engine, count=1)
    outside = tmp_path.parent / f"outside-{entry_path}" / "task"
    with Session(sqlite_engine) as session:
        stored_task = session.get(Task, task.id)
        stored_task.local_path = str(outside)
        session.add(stored_task)
        session.commit()

    async def unexpected_process(*args, **kwargs):
        pytest.fail("unsafe destination reached a subprocess")

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", unexpected_process)

    if entry_path == "run_task":
        with pytest.raises(fs.PathNotAllowed):
            await runner.run_task(task.id)
    else:
        await scheduler._execute(task.id)

    assert not outside.exists()
    assert not outside.parent.exists()
    with Session(sqlite_engine) as session:
        run = session.exec(select(Run).where(Run.task_id == task.id)).one()
    assert run.state == RunState.failed
    assert run.exit_code == -1
    assert "PathNotAllowed" in run.error_message


@pytest.mark.asyncio
async def test_api_rejects_updating_a_task_to_an_out_of_root_destination(
    sqlite_engine, tmp_path, monkeypatch
):
    monkeypatch.setattr(scheduler, "next_run_iso", lambda task: None)
    (task,) = create_source_tasks(sqlite_engine, count=1)
    outside = tmp_path.parent / "update-outside" / "task"

    with pytest.raises(ValueError):
        task_input(task, local_path=str(outside))

    with Session(sqlite_engine) as session:
        unchanged = session.get(Task, task.id)
    assert unchanged.local_path != str(outside)
    assert not outside.exists()
