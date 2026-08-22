"""TOCTOU regressions for the rsync destination write boundary.

`resolve_destination` validates a *pathname*. Between that check and the moment
`mkdir`/rsync dereferences it, a principal able to write inside a configured root
can replace the accepted destination — or any parent component of it — with a
symlink pointing outside the roots. These tests pin the descriptor-based resolver
that closes that window.
"""

import os

import pytest
from pydantic import ValidationError
from pullback.services import fs


@pytest.fixture
def dest_root(tmp_path, monkeypatch):
    root = tmp_path / "dest" / "backups"
    root.mkdir(parents=True)
    monkeypatch.setattr(fs.settings, "dest_roots", str(root))
    return root


def test_pinned_destination_write_survives_a_final_component_symlink_swap(
    dest_root, tmp_path
):
    """A swap after the check must not redirect writes made through the handle."""
    outside = tmp_path / "outside"
    outside.mkdir()
    target = dest_root / "task"

    with fs.open_destination(str(target), create=True) as pinned:
        # Attacker wins the race: the validated name now points outside the roots.
        target.rename(dest_root / "task-moved")
        target.symlink_to(outside)

        os.mkdir("written", dir_fd=pinned.fileno())

    assert (dest_root / "task-moved" / "written").is_dir()
    assert not (outside / "written").exists()


def test_pinned_destination_write_survives_a_parent_component_symlink_swap(
    dest_root, tmp_path
):
    """Swapping an intermediate directory must not redirect the pinned handle."""
    outside = tmp_path / "outside"
    (outside / "nightly").mkdir(parents=True)
    parent = dest_root / "team"
    target = parent / "nightly"
    target.mkdir(parents=True)

    with fs.open_destination(str(target)) as pinned:
        # Swap the PARENT, not the leaf: the leaf name now resolves outside.
        parent.rename(dest_root / "team-moved")
        parent.symlink_to(outside)
        assert target.resolve() == (outside / "nightly").resolve()

        os.mkdir("written", dir_fd=pinned.fileno())

    assert (dest_root / "team-moved" / "nightly" / "written").is_dir()
    assert not (outside / "nightly" / "written").exists()


def test_open_destination_rejects_a_leaf_that_became_a_symlink_before_the_open(
    dest_root, tmp_path
):
    """A component that is a symlink at traversal time fails closed."""
    outside = tmp_path / "outside"
    outside.mkdir()
    link = dest_root / "task"
    link.symlink_to(outside)

    with pytest.raises(fs.PathNotAllowed):
        fs.open_destination(str(dest_root / "task"))
    assert not (outside / "anything").exists()


def test_open_destination_rejects_a_parent_that_is_a_symlink(dest_root, tmp_path):
    outside = tmp_path / "outside"
    (outside / "nightly").mkdir(parents=True)
    (dest_root / "team").symlink_to(outside)

    with pytest.raises(fs.PathNotAllowed):
        fs.open_destination(str(dest_root / "team" / "nightly"), create=True)


def test_open_destination_creates_missing_components_when_asked(dest_root):
    target = dest_root / "team" / "nightly"

    with fs.open_destination(str(target), create=True) as pinned:
        os.mkdir("marker", dir_fd=pinned.fileno())

    assert (target / "marker").is_dir()


def test_open_destination_rejects_a_path_outside_every_root(dest_root, tmp_path):
    with pytest.raises(fs.PathNotAllowed):
        fs.open_destination(str(tmp_path / "elsewhere" / "task"), create=True)


def test_open_destination_rejects_the_configured_root_itself(dest_root):
    with pytest.raises(fs.PathNotAllowed):
        fs.open_destination(str(dest_root))


def test_open_destination_rejects_a_component_swapped_during_traversal(
    dest_root, tmp_path, monkeypatch
):
    """The race window between validation and traversal must fail closed.

    `resolve_destination` returns a fully-resolved path, so under quiescent
    conditions no component is a symlink and `O_NOFOLLOW` never fires. The window
    this closes is the attacker winning the race *after* validation returns: here
    the swap is injected at exactly that instant.
    """
    outside = tmp_path / "outside"
    (outside / "nightly").mkdir(parents=True)
    parent = dest_root / "team"
    target = parent / "nightly"
    target.mkdir(parents=True)

    real_resolve = fs._resolve_with_root

    def resolve_then_lose_the_race(path):
        result = real_resolve(path)
        parent.rename(dest_root / "team-moved")
        parent.symlink_to(outside)
        return result

    monkeypatch.setattr(fs, "_resolve_with_root", resolve_then_lose_the_race)

    with pytest.raises(fs.PathNotAllowed):
        fs.open_destination(str(target), create=True)
    assert not (outside / "nightly" / "written").exists()
    # No write of any kind may land outside the roots.
    assert sorted(p.name for p in (outside / "nightly").iterdir()) == []


def test_open_destination_pins_the_leaf_against_a_swap_after_its_parent_is_opened(
    dest_root, tmp_path, monkeypatch
):
    """The final open must be dir_fd-relative and no-follow, not name-based.

    Once the parent is pinned, an attacker's remaining move is to swap the LEAF
    name. A name-based re-open of the resolved path would follow it outside; an
    `openat(parent_fd, name, O_NOFOLLOW)` cannot.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = dest_root / "team"
    target = parent / "nightly"
    target.mkdir(parents=True)

    real_open_child = fs._open_child
    swapped = []

    def swap_before_opening_the_leaf(parent_fd, name, create):
        if name == "nightly" and not swapped:
            swapped.append(name)
            target.rename(parent / "nightly-moved")
            target.symlink_to(outside)
        return real_open_child(parent_fd, name, create)

    monkeypatch.setattr(fs, "_open_child", swap_before_opening_the_leaf)

    try:
        with fs.open_destination(str(target)) as pinned:
            os.mkdir("written", dir_fd=pinned.fileno())
    except fs.PathNotAllowed:
        pass  # fail-closed is equally acceptable; escaping is not
    assert swapped, "the race injection did not fire"
    assert not (outside / "written").exists()


def test_open_destination_leaks_no_descriptor_when_traversal_fails(
    dest_root, tmp_path, monkeypatch
):
    """A rejected traversal must close every descriptor it opened.

    The failure has to happen *inside* the traversal loop: a path that is already
    unsafe at validation time is rejected before any descriptor exists, so it
    proves nothing about descriptor ownership.
    """
    outside = tmp_path / "outside"
    (outside / "nightly").mkdir(parents=True)
    parent = dest_root / "team"
    target = parent / "nightly"
    target.mkdir(parents=True)

    real_resolve = fs._resolve_with_root
    fired = []

    def resolve_then_lose_the_race(path):
        # Restore the safe tree so validation always succeeds, then swap the
        # parent immediately after it returns — the exact race window.
        link = dest_root / "team"
        if link.is_symlink():
            link.unlink()
            (dest_root / "team-moved").rename(parent)
        result = real_resolve(path)
        parent.rename(dest_root / "team-moved")
        parent.symlink_to(outside)
        fired.append(path)
        return result

    monkeypatch.setattr(fs, "_resolve_with_root", resolve_then_lose_the_race)

    before = _open_fd_count()
    for _ in range(20):
        with pytest.raises(fs.PathNotAllowed):
            fs.open_destination(str(target))
    assert len(fired) == 20
    assert _open_fd_count() == before


def _open_fd_count() -> int:
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


def test_configured_root_symlink_swap_is_refused(dest_root, tmp_path, monkeypatch):
    """Review 299, finding 1: the root open must be O_NOFOLLOW.

    Every *child* component was already opened no-follow, but the configured
    root itself was opened by mutable pathname. An attacker who renames the root
    and drops a symlink in its place between `_resolve_with_root()` returning
    and the root `os.open()` would otherwise hand the traversal a descriptor
    pointing outside the boundary — and every mkdirat and the pinned rsync
    target below it would follow.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    target = dest_root / "task"
    target.mkdir()

    real_resolve = fs._resolve_with_root
    fired = []

    def resolve_then_swap_the_root(path):
        result = real_resolve(path)
        # Attacker wins the race on the ROOT, after validation resolved it.
        dest_root.rename(dest_root.parent / "backups-moved")
        dest_root.symlink_to(outside)
        fired.append(path)
        return result

    monkeypatch.setattr(fs, "_resolve_with_root", resolve_then_swap_the_root)

    with pytest.raises(fs.PathNotAllowed):
        fs.open_destination(str(target), create=True)

    assert fired, "the race injection did not fire"
    assert not any(outside.iterdir()), "a write escaped through the swapped root"


def test_regular_file_in_the_destination_path_surfaces_the_os_error(dest_root):
    """Review 299, finding 2 — the resolver half of the contract.

    `_open_child` deliberately re-raises the raw `OSError` rather than
    converting it: `tests/test_run_admission.py` requires the runner to record
    `NotADirectoryError` verbatim on the failed run row, so an operator can tell
    "a file is in the way" apart from "this path is outside the boundary".
    The client-facing translation happens at the API validator instead — see
    the test below.
    """
    blocker = dest_root / "team"
    blocker.write_text("not a directory")

    with pytest.raises(NotADirectoryError):
        fs.open_destination(str(dest_root / "team" / "nightly"), create=True)


def test_unusable_component_is_rejected_by_the_api_validator(dest_root):
    """Review 299, finding 2 — the boundary the user actually hits.

    `TaskIn` previously converted only `PathNotAllowed`, so a `NotADirectoryError`
    escaped validation and turned an ordinary bad destination into an internal
    server error instead of a client rejection.

    Asserting only `ValueError` would prove nothing here: Pydantic wraps *any*
    exception raised inside a validator — including a raw `NotADirectoryError` —
    in a `ValidationError`, which subclasses `ValueError`. Assert on the message
    and the cause chain so the test can actually fail.
    """
    from pullback.api.tasks import TaskIn

    blocker = dest_root / "team"
    blocker.write_text("not a directory")

    with pytest.raises(ValidationError) as caught:
        TaskIn(
            name="t",
            source_id=1,
            remote_path="/srv/data",
            local_path=str(dest_root / "team" / "nightly"),
            cron="0 3 * * *",
        )

    assert "is not usable" in str(caught.value)


def test_unsearchable_component_is_not_treated_as_missing(dest_root):
    """Review 317: `_exists_at` swallowed every OSError, not just "absent".

    An existing but unsearchable component raises EACCES from lstat. Reporting
    that as "does not exist yet" made acceptance stop the walk and return None,
    so the API accepted a destination it had never validated and the failure
    only appeared later, mid-run, when create=True tried to open it.
    """
    if os.geteuid() == 0:
        pytest.skip("root bypasses search permission checks")

    locked = dest_root / "locked"
    (locked / "child").mkdir(parents=True)
    os.chmod(locked, 0o600)  # readable, not searchable
    try:
        with pytest.raises(PermissionError):
            fs.walk_destination(str(locked / "child"), create=False)
    finally:
        os.chmod(locked, 0o700)


def test_uncheckable_component_is_not_treated_as_missing(dest_root):
    """The same defect without depending on uid: an over-long component name
    raises ENAMETOOLONG rather than ENOENT, so it too was silently accepted."""
    with pytest.raises(OSError) as caught:
        fs.walk_destination(str(dest_root / ("x" * 5000)), create=False)
    assert not isinstance(caught.value, FileNotFoundError)


def test_missing_component_still_stops_acceptance_cleanly(dest_root):
    """The legitimate case must keep working: a destination that simply does
    not exist yet is valid at acceptance — the runner creates it later."""
    assert fs.walk_destination(str(dest_root / "not-yet" / "deeper"),
                               create=False) is None


def test_unsearchable_component_is_rejected_by_the_api_validator(dest_root):
    """And it must reach the client as a validation error, not a 500."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses search permission checks")
    from pullback.api.tasks import TaskIn

    locked = dest_root / "locked-api"
    (locked / "child").mkdir(parents=True)
    os.chmod(locked, 0o600)
    try:
        with pytest.raises(ValidationError) as caught:
            TaskIn(
                name="t",
                source_id=1,
                remote_path="/srv/data",
                local_path=str(locked / "child"),
                cron="0 3 * * *",
            )
        assert "is not usable" in str(caught.value)
    finally:
        os.chmod(locked, 0o700)
