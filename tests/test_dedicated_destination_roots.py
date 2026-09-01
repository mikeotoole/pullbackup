# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""A dedicated configured root is a legitimate rsync destination.

Production regression on 0.12.0: tasks whose `local_path` is exactly one
configured destination root (a dedicated, least-privilege bind mount per task)
failed before rsync with `PathNotAllowed: <path> is a configured destination
root`.

The prohibition was written to stop `--delete` from operating on "the whole
root", but that reasoning does not survive contact with the deployment: the
operator chooses what a root *is*. Mounting `/mnt/user/gitea` at
`/mnt/dest/gitea` and pointing one task at it exposes strictly less of the host
than mounting the shared parent `/mnt/user` merely to manufacture a descendant
path. Forcing the latter is the weaker boundary, so the rule inverted its own
intent.

Containment is unchanged: a destination must still canonicalize to a path that
is a configured root or lies beneath one, with every other control intact.
"""

import pytest

from pullbackup.services import fs


@pytest.fixture
def dest_root(tmp_path, monkeypatch):
    root = tmp_path / "dest" / "gitea"
    root.mkdir(parents=True)
    monkeypatch.setattr(fs.settings, "dest_roots", str(root))
    return root


def test_resolve_destination_accepts_an_exact_configured_root(dest_root):
    """The live failure: task 7's destination IS its dedicated mount."""
    assert fs.resolve_destination(str(dest_root)) == dest_root.resolve()


@pytest.mark.parametrize("reversed_order", [False, True])
def test_a_nested_exact_root_binds_to_itself_not_a_broader_ancestor(
    tmp_path, monkeypatch, reversed_order
):
    """With `/backups,/backups/critical`, `/backups/critical` selects itself.

    Both roots would *accept* the path, so a test asserting only acceptance
    passes either way and proves nothing. The observable difference is which
    root the resolver bound to, which is exactly what the descriptor-pinned
    traversal starts from: bound to itself the relative walk is empty, bound to
    the ancestor it is `('critical',)`. Asserting the returned root pins that,
    and the parametrize pins ordering independence.
    """
    outer = tmp_path / "backups"
    nested = outer / "critical"
    nested.mkdir(parents=True)
    order = f"{nested},{outer}" if reversed_order else f"{outer},{nested}"
    monkeypatch.setattr(fs.settings, "dest_roots", order)

    resolved, root = fs._resolve_with_root(str(nested))

    assert resolved == nested.resolve()
    assert root == nested.resolve()
    assert resolved.relative_to(root).parts == ()


@pytest.mark.parametrize("reversed_order", [False, True])
def test_a_broader_root_still_binds_a_descendant_that_is_no_root(
    tmp_path, monkeypatch, reversed_order
):
    """Control: exact-first must not steal ordinary descendant resolution.

    Also order-independent — a descendant of the nested root must bind to the
    nested root, not to whichever overlapping root is declared first.
    """
    outer = tmp_path / "backups"
    nested = outer / "critical"
    nested.mkdir(parents=True)
    order = f"{nested},{outer}" if reversed_order else f"{outer},{nested}"
    monkeypatch.setattr(fs.settings, "dest_roots", order)

    resolved, root = fs._resolve_with_root(str(nested / "task"))

    assert root == nested.resolve()
    assert resolved.relative_to(root).parts == ("task",)


def test_a_sibling_sharing_a_root_name_prefix_is_still_outside(tmp_path, monkeypatch):
    """`/dest/backups_evil` must not be admitted by the root `/dest/backups`.

    Containment is by path component, never by string prefix. Pinned here
    because this rewrite changed how containment is computed.
    """
    root = tmp_path / "dest" / "backups"
    root.mkdir(parents=True)
    (tmp_path / "dest" / "backups_evil").mkdir()
    monkeypatch.setattr(fs.settings, "dest_roots", str(root))

    with pytest.raises(fs.PathNotAllowed):
        fs.resolve_destination(str(tmp_path / "dest" / "backups_evil"))
    with pytest.raises(fs.PathNotAllowed):
        fs.resolve_destination(str(tmp_path / "dest" / "backups_evil" / "task"))
