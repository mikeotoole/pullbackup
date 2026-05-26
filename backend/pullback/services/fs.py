from pathlib import Path
from ..config import settings


class PathNotAllowed(Exception):
    pass


def resolve_allowed(path: str) -> Path:
    """Resolve `path` and reject anything outside the configured dest_roots."""
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
