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
    """Return the canonical write destination or fail closed.

    This is the single shared resolver for every rsync destination boundary: the API
    acceptance check and the pre-execution check both call it, so a persisted value
    written before this validation existed can never reach `mkdir`/`rsync`.

    Unlike `resolve_allowed` (a read-only browse helper) this REJECTS a configured root
    itself: writing directly into a bind-mounted root is out of contract, and accepting
    it would let `--delete` operate on the whole root.

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
        return resolved
    raise PathNotAllowed(f"{path} is not inside an allowed destination root")


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
