"""The API acceptance path must share the execution path's no-follow traversal.

`TaskIn` validation happens at accept time, before any directory exists, so it
cannot demand a materialized destination — but it must not disagree with what the
runner will later do. These tests pin that both paths reach the same traversal and
that acceptance stays side-effect free.
"""

import pytest
from pullback.services import fs


@pytest.fixture
def dest_root(tmp_path, monkeypatch):
    root = tmp_path / "dest" / "backups"
    root.mkdir(parents=True)
    monkeypatch.setattr(fs.settings, "dest_roots", str(root))
    return root


def _task_in(local_path, **overrides):
    from pullback.api import tasks as tasks_api

    fields = dict(
        name="task",
        source_id=1,
        remote_path="/remote/data",
        local_path=local_path,
        cron="0 0 * * *",
    )
    fields.update(overrides)
    return tasks_api.TaskIn(**fields)


def test_accepting_a_task_creates_no_directory(dest_root):
    """Validation is not a write: accepting must not materialize the destination."""
    target = dest_root / "team" / "nightly"

    accepted = _task_in(str(target))

    assert accepted.local_path == str(target)
    assert not target.exists()
    assert not target.parent.exists()


def test_accepting_a_not_yet_created_destination_leaks_no_descriptor(dest_root):
    """The early return for a missing component must still close its descriptor.

    Acceptance stops walking at the first component that does not exist. That exit
    path is not an exception, so it does not go through the error cleanup — an
    unclosed descriptor there would leak once per accepted task and eventually
    exhaust the process's file descriptors.
    """
    target = dest_root / "team" / "nightly"

    before = _open_fd_count()
    for _ in range(50):
        _task_in(str(target))
    assert _open_fd_count() == before


def _open_fd_count() -> int:
    import os
    import resource

    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    limit = min(soft, 4096)
    count = 0
    for fd in range(limit):
        try:
            os.fstat(fd)
        except OSError:
            continue
        count += 1
    return count


def test_api_rejects_a_destination_whose_existing_parent_is_a_symlink(
    dest_root, tmp_path
):
    """An existing component that is a symlink is refused at accept time."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (dest_root / "team").symlink_to(outside)

    with pytest.raises(ValueError):
        _task_in(str(dest_root / "team" / "nightly"))


def test_api_and_runner_share_one_traversal(dest_root, monkeypatch):
    """Both boundaries must reach the same helper, so they cannot drift apart."""
    seen = []
    real = fs.walk_destination

    def record(path, **kwargs):
        seen.append(("walk", path, kwargs.get("create", False)))
        return real(path, **kwargs)

    monkeypatch.setattr(fs, "walk_destination", record)

    _task_in(str(dest_root / "task"))
    assert seen and seen[-1][0] == "walk"
    assert seen[-1][2] is False, "acceptance must not create"

    seen.clear()
    with fs.open_destination(str(dest_root / "task"), create=True):
        pass
    assert seen and seen[-1][2] is True, "execution creates through the same walk"


def test_api_still_accepts_a_valid_new_destination(dest_root):
    accepted = _task_in(str(dest_root / "task"))
    assert accepted.local_path == str(dest_root / "task")


def test_api_still_rejects_an_out_of_root_destination(dest_root, tmp_path):
    with pytest.raises(ValueError):
        _task_in(str(tmp_path / "outside" / "task"))


def test_api_still_rejects_the_configured_root_itself(dest_root):
    with pytest.raises(ValueError):
        _task_in(str(dest_root))


def test_api_leaves_syncoid_validation_alone(dest_root, monkeypatch):
    from pullback.services import runner

    monkeypatch.setattr(runner.settings, "zfs_dest_roots", "cache/docker_remote")
    accepted = _task_in(
        "cache/docker_remote/task",
        remote_path="pool/source",
        task_type="syncoid",
    )
    assert accepted.local_path == "cache/docker_remote/task"
