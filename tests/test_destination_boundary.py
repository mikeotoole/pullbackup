"""Destination write-boundary regressions for rsync tasks.

The rsync destination (`Task.local_path`) is attacker-influenced data that becomes a
filesystem write target. These tests pin the shared canonical resolver in
`pullback.services.fs`, the API acceptance boundary, and the pre-execution check.
"""

import pytest
from pullback.services import fs


@pytest.fixture
def dest_root(tmp_path, monkeypatch):
    root = tmp_path / "dest" / "backups"
    root.mkdir(parents=True)
    monkeypatch.setattr(fs.settings, "dest_roots", str(root))
    return root


def test_resolve_destination_accepts_a_descendant_of_a_configured_root(dest_root):
    assert fs.resolve_destination(str(dest_root / "task")) == (
        dest_root.resolve() / "task"
    )


def test_resolve_destination_accepts_a_deep_descendant(dest_root):
    target = dest_root / "team" / "nightly"
    assert fs.resolve_destination(str(target)) == target.resolve()


def test_resolve_destination_rejects_a_path_outside_every_root(dest_root, tmp_path):
    with pytest.raises(fs.PathNotAllowed):
        fs.resolve_destination(str(tmp_path / "elsewhere" / "task"))


def test_resolve_destination_rejects_the_configured_root_itself(dest_root):
    with pytest.raises(fs.PathNotAllowed):
        fs.resolve_destination(str(dest_root))


def test_resolve_destination_rejects_a_sibling_sharing_the_root_name_prefix(
    dest_root,
):
    sibling = dest_root.parent / f"{dest_root.name}_evil"
    with pytest.raises(fs.PathNotAllowed):
        fs.resolve_destination(str(sibling / "task"))


def test_resolve_destination_rejects_traversal_escaping_the_root(dest_root):
    with pytest.raises(fs.PathNotAllowed):
        fs.resolve_destination(str(dest_root / ".." / ".." / "escape"))


def test_resolve_destination_canonicalizes_traversal_that_stays_inside(dest_root):
    assert fs.resolve_destination(str(dest_root / "team" / ".." / "task")) == (
        dest_root.resolve() / "task"
    )


def test_resolve_destination_rejects_a_symlink_escaping_the_root(dest_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = dest_root / "link"
    link.symlink_to(outside)
    with pytest.raises(fs.PathNotAllowed):
        fs.resolve_destination(str(link / "task"))


def test_resolve_destination_rejects_a_relative_path(dest_root):
    with pytest.raises(fs.PathNotAllowed):
        fs.resolve_destination("relative/task")


def test_resolve_destination_rejects_an_empty_path(dest_root):
    with pytest.raises(fs.PathNotAllowed):
        fs.resolve_destination("")


@pytest.mark.parametrize("payload", ["\x00", "\n", "\r"])
def test_resolve_destination_rejects_control_bytes(dest_root, payload):
    with pytest.raises(fs.PathNotAllowed):
        fs.resolve_destination(f"{dest_root}/task{payload}other")


def test_resolve_destination_rejects_when_no_root_is_configured(monkeypatch):
    monkeypatch.setattr(fs.settings, "dest_roots", "")
    with pytest.raises(fs.PathNotAllowed):
        fs.resolve_destination("/anywhere/task")


def test_resolve_destination_rejects_a_relative_configured_root(monkeypatch):
    monkeypatch.setattr(fs.settings, "dest_roots", "relative/root")
    with pytest.raises(fs.PathNotAllowed):
        fs.resolve_destination("/relative/root/task")


def test_resolve_destination_accepts_a_descendant_of_any_configured_root(
    tmp_path, monkeypatch
):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(fs.settings, "dest_roots", f"{first},{second}")
    assert fs.resolve_destination(str(second / "task")) == second.resolve() / "task"


def test_browse_still_allows_the_configured_root_itself(dest_root):
    assert fs.resolve_allowed(str(dest_root)) == dest_root.resolve()


def test_resolve_destination_rejects_a_nested_root_shadowed_by_an_outer_root(
    tmp_path, monkeypatch
):
    """A configured root must stay unwritable even when nested inside another root.

    With roots `/backups,/backups/critical`, evaluating the exact-root rejection
    per-iteration skips `/backups/critical` for its own root but then accepts it as a
    descendant of `/backups`, exposing the whole `critical` root to `--delete`.
    """
    outer = tmp_path / "backups"
    nested = outer / "critical"
    nested.mkdir(parents=True)
    monkeypatch.setattr(fs.settings, "dest_roots", f"{outer},{nested}")

    with pytest.raises(fs.PathNotAllowed):
        fs.resolve_destination(str(nested))

    # Ordering must not matter: the same must hold with the roots declared in reverse.
    monkeypatch.setattr(fs.settings, "dest_roots", f"{nested},{outer}")
    with pytest.raises(fs.PathNotAllowed):
        fs.resolve_destination(str(nested))

    # A descendant of the nested root remains a legitimate write target.
    assert fs.resolve_destination(str(nested / "task")) == nested.resolve() / "task"


@pytest.mark.parametrize("outside", ["/etc", "relative/task", "", "/dest/../etc/task"])
def test_task_input_rejects_an_rsync_destination_outside_the_roots(
    dest_root, outside
):
    from pullback.api import tasks as tasks_api

    with pytest.raises(ValueError):
        tasks_api.TaskIn(
            name="escape",
            source_id=1,
            remote_path="/remote/data",
            local_path=outside,
            cron="0 0 * * *",
        )


def test_task_input_rejects_the_configured_root_itself(dest_root):
    from pullback.api import tasks as tasks_api

    with pytest.raises(ValueError):
        tasks_api.TaskIn(
            name="root-write",
            source_id=1,
            remote_path="/remote/data",
            local_path=str(dest_root),
            cron="0 0 * * *",
        )


def test_task_input_rejects_a_symlinked_rsync_destination(dest_root, tmp_path):
    from pullback.api import tasks as tasks_api

    outside = tmp_path / "outside"
    outside.mkdir()
    link = dest_root / "link"
    link.symlink_to(outside)
    with pytest.raises(ValueError):
        tasks_api.TaskIn(
            name="symlink",
            source_id=1,
            remote_path="/remote/data",
            local_path=str(link / "task"),
            cron="0 0 * * *",
        )


def test_task_input_accepts_a_valid_rsync_destination(dest_root):
    from pullback.api import tasks as tasks_api

    accepted = tasks_api.TaskIn(
        name="valid",
        source_id=1,
        remote_path="/remote/data",
        local_path=str(dest_root / "task"),
        cron="0 0 * * *",
    )
    assert accepted.local_path == str(dest_root / "task")


def test_task_input_does_not_apply_the_filesystem_root_check_to_syncoid(
    dest_root, monkeypatch
):
    from pullback.api import tasks as tasks_api
    from pullback.services import runner

    monkeypatch.setattr(runner.settings, "zfs_dest_roots", "cache/docker_remote")
    accepted = tasks_api.TaskIn(
        name="zfs",
        source_id=1,
        remote_path="pool/source",
        local_path="cache/docker_remote/task",
        cron="0 0 * * *",
        task_type="syncoid",
    )
    assert accepted.local_path == "cache/docker_remote/task"


def _stored_task(local_path):
    from pullback.models import Source, Task

    source = Source(
        id=1,
        name="source",
        user="backup",
        host="source.example",
        ssh_key_path="/tmp/test-key",
    )
    task = Task(
        id=1,
        name="persisted",
        source_id=1,
        remote_path="/remote/data",
        local_path=local_path,
        cron="0 0 * * *",
    )
    return task, source


def test_persisted_rsync_destination_is_revalidated_before_the_command(
    dest_root, tmp_path
):
    from pullback.services import runner

    task, source = _stored_task(str(tmp_path / "outside" / "task"))
    with pytest.raises(fs.PathNotAllowed):
        runner.build_command(task, source)


def test_valid_persisted_rsync_destination_still_builds_a_command(dest_root):
    from pullback.services import runner

    task, source = _stored_task(str(dest_root / "task"))
    args = runner.build_command(task, source)
    assert args[0] == "rsync"
    # The destination is now a descriptor path, not the mutable pathname: that is
    # the whole point of the pinning change. It must still resolve to the validated
    # directory.
    assert args[-1].startswith(f"{runner.FD_PATH_PREFIX}/")
    assert str(dest_root / "task") not in args[-1]


@pytest.mark.asyncio
async def test_unsafe_persisted_destination_starts_no_rsync_and_creates_no_directory(
    dest_root, tmp_path, monkeypatch
):
    import asyncio as _asyncio

    from pullback.services import runner

    outside = tmp_path / "outside" / "task"

    async def unexpected_process(*args, **kwargs):
        pytest.fail("unsafe destination reached rsync")

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", unexpected_process)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", unexpected_process)

    task, source = _stored_task(str(outside))
    with pytest.raises(fs.PathNotAllowed):
        runner.build_command(task, source)
    assert not outside.exists()
    assert not outside.parent.exists()


# --- Review round 2: legacy rows must stay readable so they can be remediated ---
#
# `TaskOut` inherits `TaskIn`, so the write-side destination validator also runs when
# an OUTPUT model is constructed. A row persisted before this validation existed would
# therefore make its own detail response and the whole task list 500, hiding exactly
# the task an operator needs to see in order to fix or delete it.


def _persist_legacy_task(engine, local_path):
    from pullback.models import Source, Task
    from sqlmodel import Session

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
            name="legacy",
            source_id=source.id,
            remote_path="/remote/data",
            local_path=local_path,
            cron="0 0 * * *",
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        return task.id


@pytest.fixture
def legacy_engine(tmp_path, monkeypatch, dest_root):
    from pullback import db
    from pullback.services import runner, scheduler
    from sqlmodel import SQLModel, create_engine

    engine = create_engine(
        f"sqlite:///{tmp_path / 'legacy.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(runner, "engine", engine)
    monkeypatch.setattr(scheduler, "engine", engine)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    return engine


def test_task_out_can_be_built_for_a_persisted_out_of_root_destination(
    legacy_engine, tmp_path
):
    """A legacy row must still serialize; enforcement belongs on the write path."""
    from pullback.api import tasks as tasks_api
    from pullback.models import Task
    from sqlmodel import Session

    unsafe = str(tmp_path / "outside" / "legacy")
    task_id = _persist_legacy_task(legacy_engine, unsafe)

    with Session(legacy_engine) as session:
        out = tasks_api._to_out(session.get(Task, task_id), session)

    assert out.id == task_id
    assert out.local_path == unsafe


def test_task_list_still_returns_a_persisted_out_of_root_destination(
    legacy_engine, tmp_path
):
    from pullback.api import tasks as tasks_api
    from sqlmodel import Session

    unsafe = str(tmp_path / "outside" / "legacy")
    _persist_legacy_task(legacy_engine, unsafe)

    with Session(legacy_engine) as session:
        listed = tasks_api.list_tasks(session)

    assert [t.local_path for t in listed] == [unsafe]


def test_task_input_still_rejects_that_same_out_of_root_destination(
    dest_root, tmp_path
):
    """Read tolerance must not weaken the write boundary."""
    from pullback.api import tasks as tasks_api

    with pytest.raises(ValueError):
        tasks_api.TaskIn(
            name="legacy",
            source_id=1,
            remote_path="/remote/data",
            local_path=str(tmp_path / "outside" / "legacy"),
            cron="0 0 * * *",
        )


# --- Review round 2: resolution failure must always become PathNotAllowed ---
#
# `TaskIn` only translates `PathNotAllowed`; anything else escapes as a 500 instead of
# a normal rejection. `Path.resolve()` raises `RuntimeError` (not `OSError`) for a
# symlink loop on some CPython versions, so the resolver must catch both. Note that on
# the pinned runtime (3.14) a non-strict `resolve()` returns the loop path rather than
# raising, so the version-independent contract is pinned by injection below and the
# real-loop test asserts fail-closed behaviour on whichever path this runtime takes.


def test_resolve_destination_handles_a_real_symlink_loop_without_escaping(dest_root):
    """Version-independent invariant: reject, or return a path still inside the root.

    Never raise a non-`PathNotAllowed` exception (which `TaskIn` cannot translate)
    and never hand back a path outside the boundary.
    """
    loop = dest_root / "loop"
    loop.symlink_to(loop)
    try:
        resolved = fs.resolve_destination(str(loop))
    except fs.PathNotAllowed:
        return
    assert resolved.is_relative_to(dest_root.resolve())


def test_resolve_destination_translates_a_resolution_runtime_error(
    dest_root, monkeypatch
):
    from pathlib import Path

    def exploding_resolve(self, *args, **kwargs):
        raise RuntimeError("Symlink loop from 'x'")

    monkeypatch.setattr(Path, "resolve", exploding_resolve)
    with pytest.raises(fs.PathNotAllowed):
        fs.resolve_destination(str(dest_root / "task"))


def test_task_input_rejects_a_runtime_error_destination_as_a_value_error(
    dest_root, monkeypatch
):
    """A resolution failure must be a normal 422 rejection, never an internal error."""
    from pathlib import Path

    from pullback.api import tasks as tasks_api

    def exploding_resolve(self, *args, **kwargs):
        raise RuntimeError("Symlink loop from 'x'")

    monkeypatch.setattr(Path, "resolve", exploding_resolve)
    with pytest.raises(ValueError):
        tasks_api.TaskIn(
            name="loop",
            source_id=1,
            remote_path="/remote/data",
            local_path=str(dest_root / "task"),
            cron="0 0 * * *",
        )


def test_canonical_roots_skips_a_root_that_raises_runtime_error(
    dest_root, monkeypatch
):
    """A malformed configured root must be dropped, not crash every resolution."""
    from pathlib import Path

    real_resolve = Path.resolve

    def selective_resolve(self, *args, **kwargs):
        if str(self) == str(dest_root):
            raise RuntimeError("Symlink loop from 'root'")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", selective_resolve)
    with pytest.raises(fs.PathNotAllowed):
        fs.resolve_destination(str(dest_root / "task"))
