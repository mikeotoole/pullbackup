# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""Pullbackup — pull-only rsync task manager.

The version is *derived*, never written here. It used to be a literal, and it
drifted: this file said 0.10.0 while ``backend/pyproject.toml`` said 0.5.1 and
the deployed image was tagged 0.11.0. ``/api/system/health`` reports this value,
so a correctly deployed 0.11.0 image announced itself as 0.10.0 and briefly
looked like a failed rollout.

``backend/pyproject.toml`` is now the single source of truth. Reading it back
through the installed distribution metadata means the number the app reports is
the number that was actually packaged, not a string someone remembered to bump.
"""

from importlib.metadata import PackageNotFoundError, version as _distribution_version

#: Returned when the package is imported from a source checkout that was never
#: installed (``python -m`` from ``backend/`` with no ``pip install``). It is
#: deliberately not a version number: no comparison, sort, or equality check
#: against a real version will accidentally succeed, and it is obvious in an API
#: response that the reading is not authoritative.
UNKNOWN_VERSION = "0+unknown.not-installed"

try:
    __version__ = _distribution_version("pullbackup")
except PackageNotFoundError:  # pragma: no cover - requires an uninstalled checkout
    __version__ = UNKNOWN_VERSION
