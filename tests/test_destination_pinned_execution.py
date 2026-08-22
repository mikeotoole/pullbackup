"""The rsync execution path must use the pinned destination, not a pathname.

`build_rsync_args` and the pre-execution `mkdir` both took a plain pathname, so a
symlink swap landing between validation and the subprocess redirected the write.
These tests pin the descriptor-backed destination the runner now hands to rsync.

The `/proc`-gated tests below cannot run on macOS (`/dev/fd/N` is not traversable
there — verified: `mkdir /dev/fd/3/x` fails ENOTDIR and rsync reports
"mkpath: Not a directory"). Production runs in a Linux container, so those cases
are proven in CI, and the platform-invariant tests in this file guard the same
invariants everywhere so a skip can never be mistaken for a pass.
"""

import os
import shutil
import subprocess

import pytest
from pullback.models import Run, RunState, Source, Task
from pullback.services import fs, runner
from sqlmodel import Session, SQLModel, create_engine

HAVE_PROC_FD = os.path.isdir("/proc/self/fd")


@pytest.fixture
def dest_root(tmp_path, monkeypatch):
    root = tmp_path / "dest" / "backups"
    root.mkdir(parents=True)
    monkeypatch.setattr(fs.settings, "dest_roots", str(root))
    return root


def _task(local_path):
    source = Source(
        id=1,
        name="source",
        user="backup",
        host="source.example",
        ssh_key_path="/tmp/test-key",
    )
    task = Task(
        id=1,
        name="pinned",
        source_id=1,
        remote_path="/remote/data",
        local_path=local_path,
        cron="0 0 * * *",
    )
    return task, source


# --- Platform-invariant: these run on every OS ---------------------------------


def test_build_rsync_args_uses_the_descriptor_path_of_the_pinned_destination(
    dest_root,
):
    """The destination argument must be derived from the fd, not from local_path."""
    task, source = _task(str(dest_root / "task"))

    class _FakePinned:
        def fileno(self):
            return 4242

        @property
        def path(self):
            return dest_root / "task"

    args = runner.build_rsync_args(task, source, destination=_FakePinned())
    destination = args[-1]
    assert destination == f"{runner.FD_PATH_PREFIX}/4242/"
    # The mutable pathname must NOT be what rsync is pointed at.
    assert str(dest_root / "task") not in destination


def test_build_rsync_args_requires_a_pinned_destination(dest_root):
    """No caller may fall back to the raw pathname."""
    task, source = _task(str(dest_root / "task"))
    with pytest.raises(TypeError):
        runner.build_rsync_args(task, source)


def test_descriptor_destination_has_a_trailing_slash(dest_root):
    """rsync must treat the destination as a directory, never create a file by that name."""
    task, source = _task(str(dest_root / "task"))

    class _FakePinned:
        def fileno(self):
            return 7

        @property
        def path(self):
            return dest_root / "task"

    assert runner.build_rsync_args(task, source, destination=_FakePinned())[-1].endswith(
        "/"
    )


def test_syncoid_path_is_unchanged_and_takes_no_descriptor(dest_root, monkeypatch):
    """ZFS replication must keep its dataset destination and its own validation."""
    monkeypatch.setattr(runner.settings, "zfs_dest_roots", "cache/docker_remote")
    task, source = _task("cache/docker_remote/task")
    task.task_type = "syncoid"
    task.remote_path = "pool/source"

    args = runner.build_command(task, source)
    assert args[0] == "syncoid"
    assert args[-1] == "cache/docker_remote/task"


def test_build_command_rejects_an_out_of_root_rsync_destination(dest_root, tmp_path):
    """The pre-execution boundary check still fires for a persisted bad row."""
    task, source = _task(str(tmp_path / "outside" / "task"))
    with pytest.raises(fs.PathNotAllowed):
        runner.build_command(task, source)


# --- /proc-gated: real descriptor traversal ------------------------------------


@pytest.mark.skipif(not HAVE_PROC_FD, reason="requires /proc/self/fd (Linux)")
def test_rsync_writes_through_the_descriptor_after_a_destination_swap(
    dest_root, tmp_path
):
    """End-to-end: real rsync, real swap, and nothing lands outside the roots."""
    if not shutil.which("rsync"):
        pytest.skip("rsync is not installed")

    payload = tmp_path / "src"
    payload.mkdir()
    (payload / "data.bin").write_text("payload")
    outside = tmp_path / "outside"
    outside.mkdir()
    target = dest_root / "task"

    with fs.open_destination(str(target), create=True) as pinned:
        target.rename(dest_root / "task-moved")
        target.symlink_to(outside)

        result = subprocess.run(
            ["rsync", "-a", f"{payload}/", f"{runner.FD_PATH_PREFIX}/{pinned.fileno()}/"],
            pass_fds=(pinned.fileno(),),
            capture_output=True,
            text=True,
        )

    assert result.returncode == 0, result.stderr
    assert (dest_root / "task-moved" / "data.bin").is_file()
    assert not (outside / "data.bin").exists()


@pytest.mark.skipif(not HAVE_PROC_FD, reason="requires /proc/self/fd (Linux)")
@pytest.mark.asyncio
async def test_execute_run_passes_the_destination_descriptor_to_the_subprocess(
    dest_root, tmp_path, monkeypatch
):
    """The runner must inherit the fd into rsync; without it the argument dangles."""
    recorded = {}

    async def fake_exec(*args, **kwargs):
        recorded["args"] = args
        recorded["pass_fds"] = kwargs.get("pass_fds")

        class _Proc:
            returncode = 0
            pid = os.getpid()

            async def wait(self):
                return 0

        return _Proc()

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fake_exec)
    await _run_one_task(dest_root, tmp_path, monkeypatch)

    destination = recorded["args"][-1]
    assert destination.startswith(f"{runner.FD_PATH_PREFIX}/")
    fd = int(destination.rsplit("/", 2)[-2])
    assert recorded["pass_fds"] == (fd,)


@pytest.mark.skipif(not HAVE_PROC_FD, reason="requires /proc/self/fd (Linux)")
@pytest.mark.asyncio
async def test_validating_a_run_does_not_create_its_destination(
    dest_root, tmp_path, monkeypatch
):
    """Validation before admission must stay side-effect free.

    The destination is created when the run actually executes. A run that is
    validated and then rejected (or that never gets a concurrency slot) must not
    leave an empty directory behind.
    """
    target = dest_root / "queued" / "task"
    creates = []
    real_walk = fs.walk_destination

    def record(path, **kwargs):
        creates.append(kwargs.get("create", False))
        return real_walk(path, **kwargs)

    monkeypatch.setattr(fs, "walk_destination", record)

    async def stop_before_exec(*args, **kwargs):
        raise AssertionError("must not reach exec in this test")

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", stop_before_exec)
    await _run_one_task(dest_root, tmp_path, monkeypatch, local_path=str(target))

    assert creates, "the shared traversal was not used"
    assert creates[0] is False, "pre-admission validation must not create"


@pytest.mark.skipif(not HAVE_PROC_FD, reason="requires /proc/self/fd (Linux)")
@pytest.mark.asyncio
async def test_execute_run_validates_without_creating_before_admission(
    dest_root, tmp_path, monkeypatch
):
    """The pre-admission check must reject an unsafe row without writing."""
    outside = tmp_path / "outside"
    outside.mkdir()
    created = []

    real_walk = fs.walk_destination

    def record(path, **kwargs):
        created.append(kwargs.get("create", False))
        return real_walk(path, **kwargs)

    monkeypatch.setattr(fs, "walk_destination", record)

    async def unexpected(*args, **kwargs):
        pytest.fail("rsync started")

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", unexpected)
    await _run_one_task(
        dest_root, tmp_path, monkeypatch, local_path=str(outside / "task")
    )

    assert created, "the shared traversal was not used"
    assert created[0] is False, "pre-admission validation must not create"


@pytest.mark.skipif(not HAVE_PROC_FD, reason="requires /proc/self/fd (Linux)")
@pytest.mark.asyncio
async def test_execute_run_creates_the_destination_without_following_a_symlink(
    dest_root, tmp_path, monkeypatch
):
    """Directory creation is a write and must obey the same no-follow boundary."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (dest_root / "team").symlink_to(outside)

    async def unexpected(*args, **kwargs):
        pytest.fail("rsync started for an unsafe destination")

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", unexpected)
    run_id = await _run_one_task(
        dest_root, tmp_path, monkeypatch, local_path=str(dest_root / "team" / "task")
    )

    assert not (outside / "task").exists()
    assert run_id is not None


async def _run_one_task(dest_root, tmp_path, monkeypatch, local_path=None):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'run.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(runner, "engine", engine)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(type(runner.settings), "log_dir", property(lambda self: log_dir))

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
        task = Task(
            name="pinned",
            source_id=source.id,
            remote_path="/remote/data",
            local_path=local_path or str(dest_root / "task"),
            cron="0 0 * * *",
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        run = Run(task_id=task.id, state=RunState.pending, log_filename="run.log")
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id

    try:
        await runner.execute_run(run_id)
    except fs.PathNotAllowed:
        pass
    return run_id
