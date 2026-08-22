import os
import stat
from pathlib import Path

from ..config import settings


class PathNotAllowed(Exception):
    pass


# Bytes that can split or escape a canonical path/argument representation.
_FORBIDDEN_PATH_CHARACTERS = frozenset("\x00\n\r")


def _canonical_roots() -> list[Path]:
    """Return canonical absolute destination roots, dropping invalid configuration."""
    roots: list[Path] = []
    for root in settings.dest_roots_list:
        if not root.is_absolute():
            continue
        if _FORBIDDEN_PATH_CHARACTERS.intersection(str(root)):
            continue
        try:
            roots.append(root.resolve())
        except (OSError, RuntimeError):
            # RuntimeError is CPython's symlink-loop signal on some versions; a root we
            # cannot canonicalize is dropped rather than allowed to poison every check.
            continue
    return roots


def resolve_destination(path: str) -> Path:
    """Return the canonical write destination or fail closed."""
    resolved, _root = _resolve_with_root(path)
    return resolved


def _resolve_with_root(path: str) -> tuple[Path, Path]:
    """Return the canonical destination and the configured root containing it.

    This is the single shared resolver for every rsync destination boundary: the API
    acceptance check, the pre-execution check, and the descriptor-pinning traversal
    all reach it, so a persisted value written before this validation existed can
    never reach `mkdir`/`rsync`.

    Unlike `resolve_allowed` (a read-only browse helper) this REJECTS a configured
    root itself: writing directly into a bind-mounted root is out of contract, and
    accepting it would let `--delete` operate on the whole root.

    The exact-root rejection is evaluated against EVERY configured root before any
    descendant match is considered. Checking it per-iteration is unsafe when roots
    overlap: with `/backups,/backups/critical`, the destination `/backups/critical` is
    skipped as an exact match for the second root but would then be accepted as a
    descendant of the first, exposing an entire configured root to `--delete`.
    """
    if not isinstance(path, str) or not path:
        raise PathNotAllowed("destination path is empty")
    if _FORBIDDEN_PATH_CHARACTERS.intersection(path):
        raise PathNotAllowed("destination path contains control characters")
    candidate = Path(path)
    if not candidate.is_absolute():
        raise PathNotAllowed("destination path must be absolute")
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as error:
        # `RuntimeError` is CPython's symlink-loop signal on some versions. Both must
        # become `PathNotAllowed`: the API layer translates only that type, so any other
        # exception escapes as an internal error instead of a normal rejection.
        raise PathNotAllowed(f"destination path cannot be resolved: {error}") from error
    roots = _canonical_roots()
    if any(resolved == root for root in roots):
        raise PathNotAllowed(f"{path} is a configured destination root")
    for root in roots:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return resolved, root
    raise PathNotAllowed(f"{path} is not inside an allowed destination root")


class PinnedDestination:
    """An open directory descriptor for a validated destination.

    `resolve_destination` validates a *pathname*, which is only true at the instant
    it is checked. This holds the checked directory open, so every subsequent use
    reaches that exact inode no matter how the names above it are rewritten.
    """

    def __init__(self, fd: int, path: Path):
        self._fd = fd
        self._path = path

    def fileno(self) -> int:
        return self._fd

    @property
    def path(self) -> Path:
        """The canonical pathname that was validated. Informational only."""
        return self._path

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> "PinnedDestination":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _open_child(parent_fd: int, name: str, create: bool) -> int:
    """Open `name` under `parent_fd`, refusing to follow a symlink."""
    if create:
        try:
            os.mkdir(name, dir_fd=parent_fd)
        except FileExistsError:
            pass
    try:
        return os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except OSError as error:
        # The open has ALREADY failed closed at this point; nothing was written and
        # no descriptor escaped. The lstat below only classifies the failure for
        # reporting, so that an operational problem (a regular file in the way)
        # keeps surfacing as its own OSError, while a symlink — the race this
        # traversal exists to defeat — is reported as a boundary rejection. A
        # concurrent swap can only mislabel an error that already aborted the write.
        #
        # The raw OSError is deliberately preserved here rather than converted to
        # PathNotAllowed: the runner records it verbatim on the failed run row so
        # an operator can tell "a file is in the way" apart from "this path is
        # outside the boundary". The API validator translates it separately, at
        # its own boundary, where the distinction the *caller* needs is different.
        if _is_symlink(parent_fd, name):
            raise PathNotAllowed(
                f"destination component {name!r} is a symlink: {error}"
            ) from error
        raise


def _is_symlink(parent_fd: int, name: str) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(name, dir_fd=parent_fd).st_mode)
    except OSError:
        return False


def walk_destination(path: str, create: bool = False) -> PinnedDestination | None:
    """Validate `path` by walking it root-relative with no-follow semantics.

    This is the SINGLE traversal both destination boundaries use, so the API
    acceptance check and the pre-execution check cannot drift apart.

    Traversal starts at a configured root and opens one component at a time with
    `O_NOFOLLOW`, so no existing component may be a symlink, and the descriptor it
    returns is bound to an inode rather than to a name that can still be rewritten.

    With `create=False` (acceptance) validation is side-effect free: a destination
    that does not exist yet is legitimate — the runner creates it later — so the
    walk stops at the first missing component and returns `None`. Components that
    do not exist cannot be a symlink, and every component that DOES exist has been
    checked. With `create=True` (execution) missing components are created through
    the same descriptors and a pinned handle is always returned.
    """
    resolved, root = _resolve_with_root(path)
    relative_parts = resolved.relative_to(root).parts

    try:
        current = os.open(
            str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
    except OSError as error:
        # O_NOFOLLOW closes the last hole in the traversal: every child component
        # is opened no-follow, but the configured root itself was previously
        # opened by mutable pathname. If the root were renamed and replaced with
        # a symlink between _resolve_with_root() returning and this open, the
        # descriptor — and therefore every mkdirat and the pinned rsync target
        # below it — would land outside the boundary.
        raise PathNotAllowed(
            f"destination root {root} cannot be opened: {error}"
        ) from error

    try:
        for name in relative_parts:
            if not create and not _exists_at(current, name):
                # Acceptance stops here: the rest of the path does not exist yet, so
                # there is nothing further to check. Close explicitly — this is a
                # normal return, not the exception path, so nothing else frees it.
                os.close(current)
                return None
            nxt = _open_child(current, name, create)
            os.close(current)
            current = nxt
    except BaseException:
        os.close(current)
        raise
    return PinnedDestination(current, resolved)


def _exists_at(parent_fd: int, name: str) -> bool:
    """Report whether `name` exists under `parent_fd`.

    Only `FileNotFoundError` means "not there yet". Every other `OSError` — a
    permission denial on an unsearchable parent, an over-long component, an I/O
    error — means the component could not be *checked*, which is not the same
    thing. Treating those as absent made acceptance stop the walk and return
    None, so the API accepted a destination it had never validated and the
    failure only surfaced later, mid-run, when create=True tried to open it.

    Re-raising keeps each boundary reporting what its caller needs: `TaskIn`
    converts the OSError into a client-visible invalid destination, and the
    runner records the operational error verbatim on the failed run row.
    """
    try:
        os.lstat(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return False
    return True


def open_destination(path: str, create: bool = False) -> PinnedDestination:
    """Return an open descriptor for a validated destination directory.

    Unlike `walk_destination` this always yields a handle: a destination that does
    not exist is an error unless `create=True`.
    """
    pinned = walk_destination(path, create=create)
    if pinned is None:
        raise PathNotAllowed(f"{path} does not exist")
    return pinned


def resolve_allowed(path: str) -> Path:
    """Resolve `path` and reject anything outside the configured dest_roots.

    Read-only browse helper: the roots themselves are listable, so unlike
    `resolve_destination` an exact root match is accepted here.
    """
    p = Path(path).resolve()
    for root in settings.dest_roots_list:
        try:
            p.relative_to(root.resolve())
            return p
        except ValueError:
            continue
    raise PathNotAllowed(f"{path} is not inside an allowed destination root")


def browse(path: str | None = None) -> dict:
    """Return directory listing under an allowed root.

    If `path` is None, returns the list of roots (top-level browse entry point).
    """
    if not path:
        return {
            "path": None,
            "is_root": True,
            "entries": [
                {"name": str(r), "path": str(r), "is_dir": True}
                for r in settings.dest_roots_list
                if r.exists()
            ],
        }
    p = resolve_allowed(path)
    if not p.exists() or not p.is_dir():
        return {"path": str(p), "is_root": False, "entries": []}
    entries = []
    for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
        if child.name.startswith("."):
            continue
        entries.append({"name": child.name, "path": str(child), "is_dir": child.is_dir()})
    return {"path": str(p), "is_root": False, "entries": entries}
