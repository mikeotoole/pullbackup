#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""Stand up a throwaway Pullbackup demo instance seeded with FABRICATED data.

WHAT THIS IS FOR
================

Screenshots. The README and any marketing page need pictures of a populated
Pullbackup, and the only populated instance a maintainer has is their own — full
of real hostnames, real dataset paths and a real private network layout. None of
that can go in a public repository.

So this script builds a *disposable* instance from the current checkout, fills it
with a small invented homelab, and prints the URL. Nothing here has ever existed:

  * Every host is a ``*.example.internal`` name. ``.internal`` is reserved by
    ICANN for private use and resolves nowhere on the public internet; these
    particular names resolve nowhere at all (see NEVER TOUCHES A REAL HOST).
  * Every path, byte count, file count, duration and log line below is written by
    hand in this file. No live database is read, copied or consulted.
  * The credentials are printed on stdout by design. They are not a secret and
    must never be reused anywhere.

Re-running is safe: the container is removed and the scratch directory is wiped
before anything is created, and every "random" choice is drawn from a seeded RNG,
so two runs produce byte-identical data.

NEVER TOUCHES A REAL HOST
=========================

The seeded tasks carry realistic cron schedules so the schedule column has
something to show, which means APScheduler *will* try to fire them. Four
independent layers stop that from becoming an outbound connection:

  1. DNS blackhole. The container runs with ``--dns 127.0.0.1``, so name
     resolution inside it goes to a resolver that does not exist. No hostname —
     invented or otherwise — resolves, so no socket can be opened to anything.
     ``--verify`` proves this by resolving from inside the running container.
  2. Non-existent hosts. Even with working DNS, ``fileserver.example.internal``
     and ``nas.example.internal`` are not registered anywhere.
  3. Admission blocking. Each seeded source owns one non-terminal run row (one
     ``running``, one ``pending``). ``runner.admit_run`` refuses a new run while
     any run on the same *source* is pending or running, so every scheduled fire
     is denied with ``source_active`` before a command is ever built.
     ``--verify`` proves this too, by asking the API to run a task now and
     requiring the 409.
  4. No key. ``ssh_key_path`` points at a file the container does not have, so
     even a command that somehow got built could not authenticate.

Layer 3 is the one that stops the attempt; layers 1, 2 and 4 are what make the
attempt harmless if a future change removes it.

REACHABLE FROM THIS MACHINE ONLY
================================

The credentials above are printed on stdout, so the instance must not be
reachable from anywhere but the host running it. The container publishes
``127.0.0.1:18080:8000`` explicitly: a spec without a host IP (``-p
18080:8000``) binds ``0.0.0.0`` *and* the IPv6 wildcard and would expose those
credentials to the whole LAN. ``--verify`` re-proves the binding against the
running container with ``docker port`` and ``docker inspect``, and
``tests/test_demo_publish_binding.py`` catches a regression before the
container is ever started.

USAGE
=====

    python3 scripts/seed-demo.py            # build, run, seed, verify
    python3 scripts/seed-demo.py --verify   # re-check an instance that is up
    python3 scripts/seed-demo.py --down     # remove the container and scratch dir
    python3 scripts/seed-demo.py --no-build # reuse the existing local image

Requires Docker. Nothing else — this is standard library only.
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# Demo instance parameters. All obviously disposable.
# --------------------------------------------------------------------------

IMAGE = "pullbackup-demo:local"
CONTAINER = "pullbackup-demo"
PORT = 18080
SCRATCH = Path("/tmp/pullbackup-demo")

# Printed on stdout on purpose. This is a throwaway instance with no real data;
# treat these strings as public. The password is long only because the app
# refuses to start with a Basic password under 32 characters.
DEMO_USERNAME = "demo"
DEMO_PASSWORD = "demo-instance-not-a-real-password-0000"

# Fixed so the fabricated history is identical on every run.
RNG_SEED = 20260903
# Anchors the fabricated two-week history. Seeded runs are placed relative to
# this, so "3 hours ago" stays 3 hours ago whenever the script is run.
NOW = datetime.now(timezone.utc).replace(microsecond=0)

REPO_ROOT = Path(__file__).resolve().parent.parent

GIB = 1024 ** 3
MIB = 1024 ** 2
KIB = 1024


# --------------------------------------------------------------------------
# The invented homelab.
# --------------------------------------------------------------------------

SOURCES = [
    {
        "name": "fileserver",
        "user": "backup",
        "host": "fileserver.example.internal",
        "port": 22,
        # Deliberately NOT /data/ssh/id_ed25519. The app generates that key at
        # startup, so naming it would give the demo a usable credential. This
        # path does not exist in the container and --verify proves it.
        "ssh_key_path": "/data/ssh/demo-no-such-key",
        "description": "General-purpose file host — documents, media, web assets",
    },
    {
        "name": "nas",
        "user": "replication",
        "host": "nas.example.internal",
        "port": 2222,
        "ssh_key_path": "/data/ssh/demo-no-such-key",
        "description": "ZFS storage array — dataset replication over syncoid",
    },
]

# `size` is the full size of the invented data set. It drives the first
# (seeding) run; later runs move a plausible incremental delta of it.
# `rate` is bytes/second for that task's transport, so durations and byte
# counts agree with each other instead of being drawn independently. It is per
# task because throughput is dominated by file size, not by the link: a maildir
# of a million 4 KB messages does not move at the same rate as a FLAC library
# over the same gigabit wire, and a screenshot where it does looks wrong to
# anyone who has actually watched an rsync.
TASKS = [
    # -- rsync, on `fileserver` ------------------------------------------------
    {
        "name": "documents",
        "source": "fileserver",
        "task_type": "rsync",
        "remote_path": "/srv/documents",
        "local_path": "/mnt/dest/backups/documents",
        "cron": "0 * * * *",
        # Hours between fires, matching `cron`. Drives how long ago the
        # most recent run was, so the "last run" column is not uniform.
        "cadence_h": 1,
        "enabled": True,
        "description": "Shared office documents. Hourly — small and changes constantly.",
        "size": 12 * GIB,
        "files": 48_312,
        "rate": 26 * MIB,
        "delete": True,
        "exclude_patterns": "*.tmp\n~$*\n.DS_Store",
    },
    {
        "name": "photos",
        "source": "fileserver",
        "task_type": "rsync",
        "remote_path": "/srv/photos",
        "local_path": "/mnt/dest/backups/photos",
        "cron": "30 2 * * *",
        "cadence_h": 24,
        "enabled": True,
        "description": "Photo library. Nightly, off-peak — large files, few of them change.",
        "size": 310 * GIB,
        "files": 91_204,
        "rate": 84 * MIB,
    },
    {
        "name": "music",
        "source": "fileserver",
        "task_type": "rsync",
        "remote_path": "/srv/music",
        "local_path": "/mnt/dest/backups/music",
        "cron": "0 3 * * 0",
        "cadence_h": 168,
        "enabled": True,
        "description": "FLAC library. Weekly — almost entirely static.",
        "size": 74 * GIB,
        "files": 12_880,
        "rate": 96 * MIB,
    },
    {
        "name": "web-assets",
        "source": "fileserver",
        "task_type": "rsync",
        "remote_path": "/var/www/html",
        "local_path": "/mnt/dest/backups/web-assets",
        "cron": "15 */6 * * *",
        "cadence_h": 6,
        "enabled": True,
        "description": "Static site build output.",
        "size": 2 * GIB,
        "files": 6_411,
        "rate": 34 * MIB,
        "delete": True,
    },
    {
        "name": "container-configs",
        "source": "fileserver",
        "task_type": "rsync",
        "remote_path": "/opt/stacks",
        "local_path": "/mnt/dest/backups/container-configs",
        "cron": "*/30 * * * *",
        "cadence_h": 0.5,
        "enabled": True,
        "description": "Compose files and per-stack config. Tiny, but the thing you actually need at 3am.",
        "size": 400 * MIB,
        "files": 1_902,
        "rate": 16 * MIB,
        "exclude_patterns": "*.sock\n*/cache/*",
    },
    {
        "name": "mail-archive",
        "source": "fileserver",
        "task_type": "rsync",
        "remote_path": "/var/mail/archive",
        "local_path": "/mnt/dest/backups/mail-archive",
        "cron": "45 1 * * *",
        "cadence_h": 24,
        "enabled": True,
        "description": "Maildir archive. Millions of very small files — slow per byte.",
        "size": 38 * GIB,
        "files": 1_204_776,
        "rate": 11 * MIB,
        "use_sudo": True,
    },
    {
        "name": "home-dirs",
        "source": "fileserver",
        "task_type": "rsync",
        "remote_path": "/home",
        "local_path": "/mnt/dest/backups/home-dirs",
        "cron": "0 4 * * *",
        "cadence_h": 24,
        "enabled": False,
        "description": "Paused while the new quota policy is worked out.",
        "size": 156 * GIB,
        "files": 402_118,
        "rate": 31 * MIB,
    },
    {
        "name": "legacy-wiki",
        "source": "fileserver",
        "task_type": "rsync",
        "remote_path": "/srv/legacy/wiki",
        "local_path": "/mnt/dest/backups/legacy-wiki",
        "cron": "0 5 1 * *",
        "cadence_h": 720,
        "enabled": False,
        "description": "Decommissioned. Kept for one more retention cycle, then deleted.",
        "size": 6 * GIB,
        "files": 21_450,
        "rate": 29 * MIB,
    },
    # -- syncoid (ZFS), on `nas` ----------------------------------------------
    {
        "name": "media",
        "source": "nas",
        "task_type": "syncoid",
        "remote_path": "tank/media",
        "local_path": "backup/media",
        "cron": "0 1 * * *",
        "cadence_h": 24,
        "enabled": True,
        "description": "Film and TV datasets. The big one — 900 GB, mostly cold.",
        "size": 900 * GIB,
        "files": None,
        "rate": 210 * MIB,
        "syncoid_compress": "zstd-fast",
        "prune_keep_hourly": 24,
    },
    {
        "name": "vm-images",
        "source": "nas",
        "task_type": "syncoid",
        "remote_path": "tank/vm-images",
        "local_path": "backup/vm-images",
        "cron": "0 */4 * * *",
        "cadence_h": 4,
        "enabled": True,
        "description": "zvols. Small deltas, but they change every four hours.",
        "size": 128 * GIB,
        "files": None,
        "rate": 240 * MIB,
        "syncoid_compress": "lz4",
    },
    {
        "name": "projects",
        "source": "nas",
        "task_type": "syncoid",
        "remote_path": "tank/projects",
        "local_path": "backup/projects",
        "cron": "20 0 * * *",
        "cadence_h": 24,
        "enabled": True,
        "description": "Working datasets. Nightly, recursive.",
        "size": 44 * GIB,
        "files": None,
        "rate": 195 * MIB,
    },
]

# The two tasks that carry a non-terminal run (see NEVER TOUCHES A REAL HOST,
# layer 3). One per source, so no scheduled run on either source is admissible.
NON_TERMINAL_TASKS = ("photos", "media")

# Fabricated failures. Keyed by task name -> (message, exit_code). Each reads
# like something that actually happens to a backup, not like lorem ipsum.
FAILURES = {
    "mail-archive": (
        "rsync exited 23",
        23,
        "rsync: [sender] send_files failed to open "
        '"/var/mail/archive/2019/cur/1567214883.M12P8842.mail:2,S": '
        "Permission denied (13)\n"
        "rsync error: some files/attrs were not transferred "
        "(see previous errors) (code 23) at main.c(1338) [sender=3.2.7]",
    ),
    "web-assets": (
        "rsync exited 12",
        12,
        "rsync: [Receiver] write error: No space left on device (28)\n"
        "rsync error: error in rsync protocol data stream (code 12) "
        "at io.c(1633) [receiver=3.2.7]",
    ),
    "vm-images": (
        "syncoid exited 1",
        1,
        "CRITICAL ERROR:  zfs send  failed: cannot send 'tank/vm-images@autosnap_2026-08-28_04:00:01': "
        "snapshot was destroyed mid-send\n"
        "Resuming interrupted zfs send/receive is not possible for this stream.",
    ),
}


# --------------------------------------------------------------------------
# Fabricated run logs.
# --------------------------------------------------------------------------

def rsync_log(task: dict, run_id: int, args: str, files: int, bytes_: int,
              seconds: int, rng: random.Random, failure: str | None) -> str:
    """A believable rsync log, in the exact shape the runner writes.

    The runner opens the log with a ``# pullback run N (type)`` header and an
    ``# args:`` line, then hands the file to rsync as stdout. Everything after
    the blank line is therefore literal rsync output — here, invented output
    with a real ``--info=stats2`` summary block.
    """
    total_size = task["size"]
    speed = bytes_ / max(seconds, 1)
    sample = _sample_paths(task, rng)
    lines = [
        f"# pullback run {run_id} (rsync)",
        f"# args: {args}",
        "",
        "receiving incremental file list",
    ]
    lines += sample
    lines.append("")
    if failure:
        lines.append(failure)
        lines.append("")
    lines += [
        "Number of files: {:,} (reg: {:,}, dir: {:,})".format(
            task["files"], task["files"] - task["files"] // 40, task["files"] // 40
        ),
        f"Number of created files: {max(0, files // 3):,}",
        "Number of deleted files: 0",
        f"Number of regular files transferred: {files:,}",
        f"Total file size: {total_size:,} bytes",
        f"Total transferred file size: {bytes_:,} bytes",
        f"Literal data: {bytes_:,} bytes",
        "Matched data: 0 bytes",
        f"File list size: {task['files'] * 47:,}",
        "File list generation time: 0.081 seconds",
        "File list transfer time: 0.000 seconds",
        f"Total bytes sent: {max(1024, files * 34):,}",
        f"Total bytes received: {bytes_ + task['files'] * 47:,}",
        "",
        "sent {:,} bytes  received {:,} bytes  {:,.2f} bytes/sec".format(
            max(1024, files * 34), bytes_ + task["files"] * 47, speed
        ),
        "total size is {:,}  speedup is {:.2f}".format(
            total_size, total_size / max(bytes_, 1)
        ),
    ]
    return "\n".join(lines) + "\n"


def syncoid_log(task: dict, run_id: int, args: str, bytes_: int, seconds: int,
                failure: str | None) -> str:
    """A believable syncoid log, in the shape the runner writes."""
    src, dst = task["remote_path"], task["local_path"]
    human = _human(bytes_)
    rate = _human(int(bytes_ / max(seconds, 1)))
    lines = [
        f"# pullback run {run_id} (syncoid)",
        f"# args: {args}",
        "",
        f"INFO: Sending incremental {src}@autosnap_2026-08-28_00:00:04 ... "
        f"{src}@autosnap_2026-08-29_00:00:02 (~ {human}):",
    ]
    if failure:
        lines += [failure, ""]
    else:
        lines += [
            f"{human} {human}    100%   {rate}/s    {_hms(seconds)}",
            f"INFO: Sending incremental {src}/movies@autosnap_2026-08-28_00:00:04 ... "
            "(~ 1.1 GB):",
            f"1.10GiB 1.10GiB   100%   {rate}/s    0:00:06",
            f"INFO: Sending incremental {src}/series@autosnap_2026-08-28_00:00:04 ... "
            "(~ 412 MB):",
            f"412MiB 412MiB   100%   {rate}/s    0:00:02",
        ]
        if task.get("prune_keep_hourly"):
            keep = task["prune_keep_hourly"]
            lines += [
                "",
                f"# prune: {keep + 3} hourly snaps on {dst}, keep {keep} -> destroy 3",
                f"destroy {dst}@zfs-auto-snap_hourly-2026-08-27-1700: rc=0 ",
                f"destroy {dst}@zfs-auto-snap_hourly-2026-08-27-1800: rc=0 ",
                f"destroy {dst}@zfs-auto-snap_hourly-2026-08-27-1900: rc=0 ",
            ]
    return "\n".join(lines) + "\n"


def _sample_paths(task: dict, rng: random.Random) -> list[str]:
    """A handful of invented relative paths, shaped like the task's content."""
    catalog = {
        "documents": ["finance/2026/q3-forecast.ods", "hr/handbook-v4.docx",
                      "contracts/renewal-northwind.pdf", "notes/standup-2026-08-29.md"],
        "photos": ["2026/08/DSC_4417.ARW", "2026/08/DSC_4418.ARW",
                   "2026/08/DSC_4419.ARW", "2026/07/scans/negatives-roll-12.tif"],
        "music": ["Flac/Talk Talk/Spirit of Eden/01 The Rainbow.flac",
                  "Flac/Portishead/Dummy/03 Strangers.flac"],
        "web-assets": ["assets/index-8f2a1c.js", "assets/index-4b91de.css",
                       "img/hero-2400.webp", "index.html"],
        "container-configs": ["media/compose.yml", "monitoring/compose.yml",
                              "monitoring/prometheus/prometheus.yml", "reverse-proxy/compose.yml"],
        "mail-archive": ["2026/cur/1756402011.M884210P4417.mail:2,S",
                         "2026/cur/1756402044.M112904P4417.mail:2,S",
                         "2026/new/1756402099.M551203P4418.mail:2,"],
        "home-dirs": ["ada/.config/nvim/init.lua", "ada/src/pathfinder/main.rs",
                      "grace/Documents/thesis-draft-9.tex"],
        "legacy-wiki": ["pages/Main_Page.wiki", "images/8/8c/network-2019.png"],
    }
    paths = catalog.get(task["name"], ["data/file-0001.bin", "data/file-0002.bin"])
    return list(paths) + [f"... {rng.randint(180, 4200):,} more files"]


def _human(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{int(value)}B" if unit == "B" else f"{value:.2f}{unit}"
        value /= 1024.0
    return f"{int(value)}B"


def _hms(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


# --------------------------------------------------------------------------
# Docker plumbing.
# --------------------------------------------------------------------------

def sh(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, check=check, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def down() -> None:
    """Remove the container and wipe the scratch directory. Idempotent."""
    sh("docker", "rm", "-f", CONTAINER, check=False, capture=True)
    # Guard: only ever delete the path this script owns. Compared after
    # resolving BOTH sides — on macOS /tmp is a symlink to /private/tmp, so
    # comparing a resolved path against the literal constant always mismatched
    # and refused to clean up.
    if SCRATCH.exists():
        expected = Path("/tmp/pullbackup-demo")
        if SCRATCH.resolve() != expected.resolve():
            raise SystemExit(f"refusing to delete unexpected scratch path {SCRATCH}")
        shutil.rmtree(SCRATCH)
    print(f"  removed container {CONTAINER} and {SCRATCH}")


def build() -> None:
    print(f"  building {IMAGE} from {REPO_ROOT}")
    # Plain `docker build`, not `buildx build`: with a container driver and no
    # --load the result stays in the builder cache and `docker run` would use a
    # stale image (or none). The inspect below is the proof that it landed in
    # the local image store either way.
    sh("docker", "build", "-t", IMAGE, str(REPO_ROOT))
    got = sh("docker", "image", "inspect", "--format", "{{.Id}}", IMAGE, capture=True)
    if got.returncode != 0:
        raise SystemExit(f"{IMAGE} is not in the local image store after build")
    print(f"  image {got.stdout.strip()[:19]}")


def up() -> None:
    data = SCRATCH / "data"
    dest = SCRATCH / "dest"
    (data / "logs").mkdir(parents=True, exist_ok=True)
    (data / "ssh").mkdir(parents=True, exist_ok=True)
    dest.mkdir(parents=True, exist_ok=True)
    print(f"  scratch dir {SCRATCH}")

    sh(
        "docker", "run", "-d",
        "--name", CONTAINER,
        # Layer 1 of the "never touches a real host" guarantee: resolution goes
        # to a resolver that is not there, so nothing resolves and no socket can
        # be opened. Port publishing is unaffected (unlike --network internal,
        # which also severs the host -> container path and makes the UI
        # unreachable).
        "--dns", "127.0.0.1",
        # Loopback ONLY. A publish spec without a host IP ("-p 18080:8000")
        # binds 0.0.0.0 *and* the IPv6 wildcard, which would put the
        # credentials printed at the end of this script on every interface the
        # host has. Naming 127.0.0.1 explicitly is what makes the "reachable
        # from this machine only" claim in docs/demo.md true.
        # Regression-tested by tests/test_demo_publish_binding.py and re-proved
        # against the live container by verify().
        "-p", f"127.0.0.1:{PORT}:8000",
        "-v", f"{data}:/data",
        "-v", f"{dest}:/mnt/dest/backups",
        "-e", "TZ=UTC",
        "-e", "PULLBACKUP_DEST_ROOTS=/mnt/dest/backups",
        "-e", "PULLBACKUP_ZFS_DEST_ROOTS=backup",
        "-e", f"PULLBACKUP_HTTP_BASIC_USERNAME={DEMO_USERNAME}",
        "-e", f"PULLBACKUP_HTTP_BASIC_PASSWORD={DEMO_PASSWORD}",
        IMAGE,
    )
    print(f"  container {CONTAINER} on 127.0.0.1:{PORT}")


def wait_healthy(timeout: int = 90) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/api/system/health", timeout=3
            ) as r:
                if r.status == 200:
                    print(f"  healthy: {json.loads(r.read())}")
                    return
        except Exception as e:  # noqa: BLE001 - any failure just means "not yet"
            last = f"{type(e).__name__}: {e}"
        time.sleep(1)
    sh("docker", "logs", "--tail", "40", CONTAINER, check=False)
    raise SystemExit(f"instance never became healthy ({last})")


# --------------------------------------------------------------------------
# Seeding.
# --------------------------------------------------------------------------

def _auth_header() -> str:
    raw = f"{DEMO_USERNAME}:{DEMO_PASSWORD}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def api(method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", data=data, method=method,
        headers={"Authorization": _auth_header(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            payload = r.read()
            return r.status, (json.loads(payload) if payload else None)
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload)
        except Exception:  # noqa: BLE001
            return e.code, {"detail": payload.decode(errors="replace")}


def seed_sources_and_tasks() -> tuple[dict[str, int], dict[str, int]]:
    """Create sources and tasks through the real HTTP API.

    Deliberately the API and not a direct database write: creating a task through
    ``POST /api/tasks`` is what calls ``scheduler.upsert_job``, and without a
    registered job the task list's "next run" column is empty for every row.
    """
    source_ids: dict[str, int] = {}
    for src in SOURCES:
        status, out = api("POST", "/api/sources", src)
        if status != 201 or not out:
            raise SystemExit(f"source {src['name']} rejected: {status} {out}")
        source_ids[src["name"]] = out["id"]
    print(f"  {len(source_ids)} sources")

    task_ids: dict[str, int] = {}
    for spec in TASKS:
        body = {
            "name": spec["name"],
            "source_id": source_ids[spec["source"]],
            "remote_path": spec["remote_path"],
            "local_path": spec["local_path"],
            "cron": spec["cron"],
            "enabled": spec["enabled"],
            "description": spec["description"],
            "task_type": spec["task_type"],
        }
        for optional in ("delete", "use_sudo", "exclude_patterns",
                         "syncoid_compress", "prune_keep_hourly"):
            if optional in spec:
                body[optional] = spec[optional]
        status, out = api("POST", "/api/tasks", body)
        if status != 201 or not out:
            raise SystemExit(f"task {spec['name']} rejected: {status} {out}")
        task_ids[spec["name"]] = out["id"]
    print(f"  {len(task_ids)} tasks")
    return source_ids, task_ids


def seed_runs(task_ids: dict[str, int]) -> int:
    """Write the fabricated run history straight into SQLite.

    Direct writes because runs are produced by the scheduler executing a real
    backup — there is no API that creates one without running something. For a
    screenshot fixture that is exactly what is wanted.
    """
    rng = random.Random(RNG_SEED)
    db = SCRATCH / "data" / "pullback.db"
    log_dir = SCRATCH / "data" / "logs"
    by_name = {t["name"]: t for t in TASKS}

    rows: list[tuple] = []
    logs: list[tuple[str, str]] = []
    run_id = 1

    for spec in TASKS:
        task_id = task_ids[spec["name"]]
        # A disabled task still has history — it ran until it was paused.
        history = 4 if spec["enabled"] else 2

        # When did this task last run? A believable answer is "less than one
        # cadence ago", so an hourly task shows minutes and a weekly one shows
        # days. Spreading every task evenly across the window instead puts all
        # eleven newest runs inside the same half hour, and the list then reads
        # as generated in one go - exactly what a screenshot must avoid.
        cadence = timedelta(hours=spec["cadence_h"])
        if not spec["enabled"]:
            # Paused, so it stopped running when it was switched off.
            newest = NOW - timedelta(days=rng.uniform(4, 9))
        elif spec["name"] in NON_TERMINAL_TASKS:
            # A run of this task is in flight right now, so its last COMPLETED
            # run must be at least a full cadence back or the two overlap.
            newest = NOW - cadence * rng.uniform(1.0, 1.2)
        else:
            newest = NOW - cadence * rng.uniform(0.15, 0.9)

        # Older rows step back at the task's OWN cadence, not across a fixed
        # window: the run-history page shows started-at timestamps, and an
        # hourly task whose four rows sit four days apart contradicts the
        # schedule column two clicks away. Capped so nothing predates the
        # fortnight the fixture claims to cover - a monthly task therefore
        # shows fewer, widely spaced rows, which is correct for a monthly task.
        window_start = NOW - timedelta(days=14)
        starts = [newest - cadence * n for n in range(history)]
        starts = [s for s in starts if s >= window_start] or [newest]
        starts.reverse()
        for index, started in enumerate(starts):
            first = index == 0

            if first:
                # Initial seeding run: the whole data set moves.
                bytes_ = spec["size"]
            else:
                # Incremental: a few per cent of the set, with jitter.
                fraction = rng.uniform(0.004, 0.06)
                bytes_ = int(spec["size"] * fraction)
            # Jitter the throughput per run. A task that moves a different
            # amount of data in exactly the same bytes/second every time is a
            # giveaway at screenshot resolution.
            seconds = max(8, int(bytes_ / (spec["rate"] * rng.uniform(0.55, 1.15))))
            files = None
            if spec["files"] is not None:
                files = spec["files"] if first else max(
                    1, int(spec["files"] * rng.uniform(0.002, 0.05))
                )

            # One fabricated failure per listed task, on its most recent
            # completed run, so the failure is visible on the task list.
            failure = None
            if spec["name"] in FAILURES and index == len(starts) - 1:
                failure = FAILURES[spec["name"]]

            state, exit_code, error = "success", 0, ""
            if failure:
                error, exit_code, failure_text = failure
                state = "failed"
                # A failed transfer stops partway through.
                bytes_ = int(bytes_ * rng.uniform(0.2, 0.7))
                seconds = max(8, int(bytes_ / (spec["rate"] * rng.uniform(0.55, 1.15))))
                if files is not None:
                    files = max(1, int(files * 0.6))
            else:
                failure_text = None

            finished = started + timedelta(seconds=seconds)
            log_name = f"task-{task_id}-{started.strftime('%Y%m%dT%H%M%SZ')}.log"
            args = _fake_args(spec)
            if spec["task_type"] == "syncoid":
                text = syncoid_log(spec, run_id, args, bytes_, seconds, failure_text)
            else:
                text = rsync_log(spec, run_id, args, files or 0, bytes_, seconds,
                                 rng, failure_text)
            logs.append((log_name, text))
            rows.append((run_id, task_id, state, _sql(started), _sql(finished),
                         exit_code, bytes_, files, error, log_name))
            run_id += 1

    # The two non-terminal rows. These are what block admission (see the module
    # docstring, layer 3) — one per source, so no scheduled run on either source
    # can ever be admitted. They are also what makes the running/pending pill
    # styling visible in a screenshot.
    running = by_name[NON_TERMINAL_TASKS[0]]
    started = NOW - timedelta(minutes=17)
    log_name = f"task-{task_ids[running['name']]}-{started.strftime('%Y%m%dT%H%M%SZ')}.log"
    partial = 4 * GIB
    logs.append((log_name, rsync_log(running, run_id, _fake_args(running), 611,
                                     partial, 47, rng, None).split("\nNumber of files:")[0]
                 + "\n"))
    rows.append((run_id, task_ids[running['name']], "running", _sql(started), None,
                 None, None, None, "", log_name))
    run_id += 1

    queued = by_name[NON_TERMINAL_TASKS[1]]
    started = NOW - timedelta(minutes=2)
    log_name = f"task-{task_ids[queued['name']]}-{started.strftime('%Y%m%dT%H%M%SZ')}.log"
    rows.append((run_id, task_ids[queued['name']], "pending", _sql(started), None,
                 None, None, None, "", log_name))
    run_id += 1

    log_dir.mkdir(parents=True, exist_ok=True)
    for name, text in logs:
        (log_dir / name).write_text(text)

    conn = sqlite3.connect(db)
    try:
        conn.executemany(
            "INSERT INTO run (id, task_id, state, started_at, finished_at, "
            "exit_code, bytes_transferred, files_transferred, error_message, "
            "log_filename) VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    print(f"  {len(rows)} runs, {len(logs)} log files")
    return len(rows)


def _fake_args(spec: dict) -> str:
    """The argv line the runner would have written into the log header."""
    src = SOURCES[0] if spec["source"] == "fileserver" else SOURCES[1]
    if spec["task_type"] == "syncoid":
        parts = ["syncoid", "--no-sync-snap", "--recursive"]
        if spec.get("syncoid_compress"):
            parts.append(f"--compress={spec['syncoid_compress']}")
        parts += [f"--sshkey={src['ssh_key_path']}"]
        if src["port"] != 22:
            parts.append(f"--sshport={src['port']}")
        parts += [f"{src['user']}@{src['host']}:{spec['remote_path']}", spec["local_path"]]
        return " ".join(parts)
    parts = ["rsync", "-a", "-z", "--info=stats2,progress2", "--delay-updates"]
    if spec.get("delete"):
        parts.insert(3, "--delete")
    for line in spec.get("exclude_patterns", "").splitlines():
        if line.strip():
            parts.append(f"--exclude={line.strip()}")
    parts += [
        "-e", f"'ssh -i {src['ssh_key_path']} -p {src['port']} -o StrictHostKeyChecking=accept-new "
              "-o BatchMode=yes -o ServerAliveInterval=30'",
        f"{src['user']}@{src['host']}:{spec['remote_path']}/",
        "/proc/self/fd/9",
    ]
    return " ".join(parts)


def _sql(dt: datetime) -> str:
    """Match how SQLAlchemy stores a DateTime in SQLite: naive UTC."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")


# --------------------------------------------------------------------------
# Verification. Proves the instance is inert rather than asserting it.
# --------------------------------------------------------------------------

def verify() -> bool:
    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))

    # 0. The instance is reachable from this machine only. The demo prints
    #    working credentials, so a wildcard bind would publish them to the LAN.
    #    Asked of the live container, not of this file: `docker port` reports
    #    what the daemon actually bound.
    ports = subprocess.run(
        ["docker", "port", CONTAINER, "8000/tcp"],
        capture_output=True, text=True,
    )
    bindings = [ln.strip() for ln in ports.stdout.splitlines() if ln.strip()]
    check(
        "container published on loopback only",
        ports.returncode == 0
        and bool(bindings)
        and all(b.rsplit(":", 1)[0] == "127.0.0.1" for b in bindings),
        ", ".join(bindings) or (ports.stderr.strip() or "no bindings"),
    )
    host_ips = subprocess.run(
        ["docker", "inspect", "--format",
         "{{range $p, $c := .NetworkSettings.Ports}}"
         "{{range $c}}{{$p}}={{.HostIp}} {{end}}{{end}}", CONTAINER],
        capture_output=True, text=True,
    )
    inspected = host_ips.stdout.split()
    check(
        "no wildcard host IP in the container's port map",
        host_ips.returncode == 0
        and bool(inspected)
        and all(e.split("=", 1)[1] == "127.0.0.1" for e in inspected),
        " ".join(inspected) or (host_ips.stderr.strip() or "empty port map"),
    )

    # 1. DNS inside the container resolves nothing.
    for host in ("fileserver.example.internal", "nas.example.internal", "example.com"):
        r = subprocess.run(
            ["docker", "exec", CONTAINER, "getent", "hosts", host],
            capture_output=True, text=True,
        )
        check(f"DNS blackholed for {host}", r.returncode != 0,
              (r.stdout or r.stderr).strip() or "no answer")

    # 2. Every scheduled fire is refused before a command is built.
    status_tasks, tasks = api("GET", "/api/tasks")
    if status_tasks != 200 or not tasks:
        check("task list readable", False, str(status_tasks))
        return False
    # Deliberately ask a task that does NOT itself hold a non-terminal run: a
    # 409 for such a task can only have come from the SOURCE-level check, which
    # is the one that covers every other task on that source too.
    by_source: dict[int, dict] = {}
    for t in tasks:
        if t["last_run_state"] in ("running", "pending"):
            continue
        by_source.setdefault(t["source_id"], t)
    check("a non-active task exists on every seeded source",
          len(by_source) == len(SOURCES), f"{len(by_source)}/{len(SOURCES)}")
    for source_id, task in by_source.items():
        status, out = api("POST", f"/api/tasks/{task['id']}/run", None)
        detail = str((out or {}).get("detail", ""))
        check(
            f"run-now refused at source scope for source {source_id} "
            f"(task {task['name']!r})",
            status == 409 and "source" in detail,
            f"HTTP {status} {detail}",
        )

    # 3. The SSH key the seeded sources name does not exist in the container.
    #    (The app generates its own key at /data/ssh/id_ed25519 on startup; the
    #    seeded sources deliberately point somewhere else.)
    for path in {s["ssh_key_path"] for s in SOURCES}:
        r = subprocess.run(
            ["docker", "exec", CONTAINER, "test", "-e", path],
            capture_output=True, text=True,
        )
        check(f"seeded ssh_key_path {path} absent in container", r.returncode != 0)

    # 4. No run has left the fabricated set (nothing actually executed).
    # A real execution attempt would leave a row the fixture never writes: the
    # runner's own failure text, or the restart-reconciliation message.
    status_runs, runs = api("GET", "/api/runs?limit=200")
    real = [r for r in (runs or [])
            if r["error_message"].startswith("run owner failed")
            or "interrupted by application restart" in r["error_message"]]
    check("no run rows produced by an actual execution attempt",
          status_runs == 200 and not real,
          f"{len(runs or [])} rows, {len(real)} suspicious")

    # 5. Browser-shaped request lands on the app's login page, not a native
    #    dialog: 401 with NO WWW-Authenticate (PR #34's behaviour).
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/api/tasks",
        headers={"Sec-Fetch-Mode": "cors", "X-Pullbackup-Client": "web"},
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        check("browser-shaped 401 carries no WWW-Authenticate", False, "request succeeded")
    except urllib.error.HTTPError as e:
        check("browser-shaped 401 carries no WWW-Authenticate",
              e.code == 401 and e.headers.get("WWW-Authenticate") is None,
              f"HTTP {e.code}, WWW-Authenticate={e.headers.get('WWW-Authenticate')!r}")

    return ok


# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description="Stand up a throwaway Pullbackup demo instance seeded with "
                    "fabricated data, for screenshots."
    )
    p.add_argument("--down", action="store_true",
                   help="remove the demo container and scratch directory, then exit")
    p.add_argument("--verify", action="store_true",
                   help="only re-run the verification checks against a running instance")
    p.add_argument("--no-build", action="store_true",
                   help="reuse the existing local image instead of rebuilding")
    args = p.parse_args()

    if shutil.which("docker") is None:
        print("docker is required", file=sys.stderr)
        return 2

    if args.down:
        print("tearing down")
        down()
        return 0

    if args.verify:
        print("verifying")
        return 0 if verify() else 1

    print("tearing down any previous demo instance")
    down()
    if not args.no_build:
        print("building the image")
        build()
    print("starting the container")
    up()
    print("waiting for the app")
    wait_healthy()
    print("seeding fabricated data")
    seed_sources_and_tasks_result = seed_sources_and_tasks()
    seed_runs(seed_sources_and_tasks_result[1])
    print("verifying the instance is inert")
    passed = verify()

    print()
    print("  " + "-" * 62)
    print(f"  Pullbackup demo:  http://127.0.0.1:{PORT}/")
    print(f"  username:         {DEMO_USERNAME}")
    print(f"  password:         {DEMO_PASSWORD}")
    print("  ")
    print("  Every source, task, run and log line is fabricated. This instance")
    print("  cannot reach any host: see the module docstring for the four")
    print("  independent layers, all re-checkable with --verify.")
    print(f"  Tear down with:   python3 {Path(__file__).name} --down")
    print("  " + "-" * 62)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

