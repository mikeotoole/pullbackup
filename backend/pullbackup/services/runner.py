# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
import asyncio
import logging
import os
import re
import signal
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from sqlmodel import Session, select

from ..config import settings
from ..db import engine
from ..models import Run, RunState, Source, Task, utcnow
from . import fs

# concurrency control
_global_sem = asyncio.Semaphore(settings.max_concurrent_runs)
_source_locks: dict[int, asyncio.Lock] = {}
# Destinations currently being written, as containment keys (see
# `_destination_key`). Two runs may never hold overlapping keys at the same
# time: rsync with `--delete` racing another writer in the same tree, or two
# syncoid replications into one dataset, is data loss rather than throughput.
#
# This is a SEPARATE exclusion from the source lock and it is the thing that
# makes raising `max_concurrent_runs` safe. Before it existed the global limit
# of 1 was the only reason two tasks pointing at one directory could not
# overlap — which meant the price of not corrupting a destination was that one
# long transfer stopped every other backup in the system.
_active_destinations: dict[int, tuple[str, ...]] = {}
# Bound lazily, per running loop, by `_destination_condition`. A module-level
# `asyncio.Condition()` would attach to whichever loop first awaited it and
# raise "bound to a different event loop" for every loop after that — harmless
# in the single-loop deployment, but it makes this exclusion untestable and
# would turn any future second loop into failed runs rather than a clear error.
_destination_cv: Optional[asyncio.Condition] = None
_destination_cv_loop: Optional[asyncio.AbstractEventLoop] = None

# This process-local lock assumes the supported single-Uvicorn-process deployment.
_admission_lock = asyncio.Lock()
_execution_tasks: set[asyncio.Task[int]] = set()
# run id -> the execution this process owns for it.
#
# This is the ONLY thing a cancellation request is allowed to consult. Looking a
# run up by pid would be unsound in both directions: pids are recycled, and a
# run's pid is not knowable until after the exec. Keying on the run id means a
# request can signal exactly the execution this application started for that
# exact row, and nothing else — including nothing at all when the row is pending,
# terminal, or belongs to a previous process.
#
# Entries are added when execution takes ownership of a pending row and removed
# in a `finally`, so both exits (natural completion and cancellation) release
# them. A leaked entry would be an unbounded dict AND a stale handle a later
# cancellation could act on.
_run_owners: dict[int, "RunOwner"] = {}
log = logging.getLogger(__name__)
_ZFS_DATASET = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.:%-]*(?:/[A-Za-z0-9][A-Za-z0-9_.:%-]*)*"
)
_SSH_HOST = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)
_SSH_USER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
# The kernel resolves this to the inode the descriptor holds, which is why it is
# immune to a rename/symlink swap of the pathname that was validated.
FD_PATH_PREFIX = "/proc/self/fd"


@dataclass(frozen=True)
class RunAdmission:
    accepted: bool
    run_id: Optional[int] = None
    reason: Optional[str] = None

    def accepted_run_id(self) -> int:
        if not self.accepted or self.run_id is None:
            raise RuntimeError("accepted run admission missing run id")
        return self.run_id


class RunNotOwnedError(RuntimeError):
    """Execution was invoked for a row that it did not transition from pending."""


class CancelOutcome(str, Enum):
    """What a cancellation request actually achieved. Every value is honest.

    ``cancelled``
        This process owned the run, signalled its process group, and the row
        reached a terminal state as a result.
    ``already_finished``
        The run was terminal by the time the request was serviced — either it
        never was active, or it completed while the request was in flight. The
        accompanying ``state`` is the state it actually reached, which may be
        ``success``: a run that finished normally must never be reported as
        cancelled.
    ``not_owned``
        The row exists and is active, but this process holds no execution for
        it. That is a pending row not yet started, or a row left by a previous
        process. Terminalizing it would be claiming an effect on something we
        cannot signal.
    ``still_running``
        This process owned the run and signalled it, but the execution had not
        unwound by the time the request gave up waiting, so the row is STILL
        active. A stop that has not stopped anything is not a terminal outcome
        and must never be dressed up as one.
    ``not_found``
        No such run.
    """

    cancelled = "cancelled"
    already_finished = "already_finished"
    not_owned = "not_owned"
    still_running = "still_running"
    not_found = "not_found"


@dataclass(frozen=True)
class CancelResult:
    outcome: CancelOutcome
    state: Optional[RunState] = None


@dataclass
class RunOwner:
    """This process's handle on one executing run."""

    execution: asyncio.Task[int]
    # Set once the execution has finished its own terminalization, so a caller
    # can wait for the row to settle rather than polling it.
    settled: asyncio.Event
    # True once a cancellation has been requested for this run, so the execution
    # records `cancelled` rather than `failed` when it unwinds.
    cancel_requested: bool = False


@asynccontextmanager
async def lifecycle_mutation_lock():
    """Serialize task/source mutation with run admission in this process."""
    async with _admission_lock:
        yield


def reconcile_stale_runs() -> int:
    """Fail active rows that cannot belong to this application process."""
    with Session(engine) as session:
        stale_runs = session.exec(
            select(Run).where(Run.state.in_([RunState.pending, RunState.running]))
        ).all()
        finished_at = utcnow()
        for run in stale_runs:
            run.state = RunState.failed
            run.finished_at = finished_at
            run.exit_code = -1
            run.error_message = "interrupted by application restart"
            session.add(run)
        session.commit()
        return len(stale_runs)


async def admit_run(task_id: int) -> RunAdmission:
    """Atomically create a pending run when the task's source is idle."""
    async with _admission_lock:
        with Session(engine) as session:
            task = session.get(Task, task_id)
            if not task:
                raise ValueError(f"task {task_id} not found")

            active_states = [RunState.pending, RunState.running]
            active_task = session.exec(
                select(Run).where(
                    Run.task_id == task.id,
                    Run.state.in_(active_states),
                )
            ).first()
            if active_task:
                return RunAdmission(accepted=False, reason="task_active")

            active_source = session.exec(
                select(Run)
                .join(Task, Run.task_id == Task.id)
                .where(
                    Task.source_id == task.source_id,
                    Run.state.in_(active_states),
                )
            ).first()
            if active_source:
                return RunAdmission(accepted=False, reason="source_active")

            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            run = Run(
                task_id=task.id,
                state=RunState.pending,
                log_filename=f"task-{task.id}-{ts}.log",
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            if run.id is None:
                raise RuntimeError("accepted run admission missing run id")
            return RunAdmission(accepted=True, run_id=run.id)


def _source_lock(source_id: int) -> asyncio.Lock:
    lock = _source_locks.get(source_id)
    if lock is None:
        lock = asyncio.Lock()
        _source_locks[source_id] = lock
    return lock


def _destination_key(task: Task) -> tuple[str, ...]:
    """Return the destination this task writes, as path components.

    A tuple of components rather than a string, so containment is a prefix
    comparison on whole names. `("a", "bc")` and `("a", "bcd")` do not conflict,
    while a plain `startswith` on the joined strings would say they do; the
    reverse trap (`/dest/photos` vs `/dest/photos-old`) is the one that matters
    in practice, because falsely serializing two unrelated backups is exactly
    the defect being fixed.

    rsync destinations are canonicalized through the same resolver the
    destination boundary uses, so two rows spelling one directory differently —
    a trailing slash, a `..`, a symlinked parent — produce the SAME key. Keying
    on the raw `local_path` string would let a rename of the row defeat the
    exclusion without moving a single byte on disk.

    syncoid destinations are ZFS dataset names, already validated and already
    canonical, and are split on `/` for the same ancestor/descendant
    containment: a recursive replication of `tank/app` covers `tank/app/db`.

    The two namespaces are kept apart by a leading discriminator so a dataset
    named like a path can never collide with a real directory.
    """
    if task.task_type == "syncoid":
        dataset = validate_zfs_destination(task.local_path)
        return ("zfs", *dataset.split("/"))
    return ("fs", *fs.resolve_destination(task.local_path).parts)


def _conflicts(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """True when one destination contains the other, in either direction.

    Equality, ancestor, and descendant all conflict. Only a genuine divergence
    at some component is safe to run concurrently.
    """
    shared = min(len(left), len(right))
    return left[:shared] == right[:shared]


def _destination_condition() -> asyncio.Condition:
    """Return the destination condition for the running loop, rebinding if needed.

    Rebinding drops `_active_destinations` with it: entries recorded under a
    dead loop describe runs that cannot still be executing, and carrying them
    forward would block their destinations permanently.
    """
    global _destination_cv, _destination_cv_loop
    loop = asyncio.get_running_loop()
    if _destination_cv is None or _destination_cv_loop is not loop:
        _destination_cv = asyncio.Condition()
        _destination_cv_loop = loop
        _active_destinations.clear()
    return _destination_cv


@asynccontextmanager
async def _destination_exclusion(run_id: int, key: tuple[str, ...]):
    """Hold `key` for the duration of a run, waiting out any overlapping run.

    A condition variable rather than a per-destination lock, because the unit of
    exclusion is CONTAINMENT, not identity: `tank/app` must block `tank/app/db`,
    and those are two different keys that no single lock object can cover.

    Waiting — never dropping. A run that finds its destination busy stays
    admitted and starts when the destination frees, so a task that shares a
    destination with a long one is delayed rather than silently skipped. A drop
    would be a quieter version of the very defect this fixes.
    """
    condition = _destination_condition()
    async with condition:
        await condition.wait_for(
            lambda: not any(
                _conflicts(key, held)
                for holder, held in _active_destinations.items()
                if holder != run_id
            )
        )
        _active_destinations[run_id] = key
    try:
        yield
    finally:
        # Released under the condition so a waiter cannot miss the wakeup, and
        # unconditionally, so a cancelled or failed run never leaves its
        # destination permanently claimed — a leak here would wedge every task
        # sharing that path until the process restarts.
        async with condition:
            _active_destinations.pop(run_id, None)
            condition.notify_all()



def build_rsync_args(task: Task, source: Source, *, destination: fs.PinnedDestination) -> list[str]:
    # `destination` is an OPEN DESCRIPTOR for the already-validated directory, not a
    # pathname. rsync is pointed at /proc/self/fd/N, which the kernel resolves to that
    # exact inode, so a symlink swap landing after validation cannot redirect the
    # transfer. `destination` is keyword-only and required: there is no pathname
    # fallback for a caller to reach for.
    args = ["rsync"]
    if task.archive:
        args.append("-a")
    else:
        if task.recursive:
            args.append("-r")
        if task.times:
            args.append("-t")
        if task.preserve_permissions:
            args.append("-p")
    if task.compress:
        args.append("-z")
    if task.preserve_xattrs:
        args.append("-X")
    if task.delete:
        args.append("--delete")
    if task.quiet:
        args.append("-q")
    else:
        args.append("--info=stats2,progress2")
    if task.delay_updates:
        args.append("--delay-updates")
    if task.bwlimit_kbps:
        args.append(f"--bwlimit={task.bwlimit_kbps}")
    for line in task.exclude_patterns.splitlines():
        line = line.strip()
        if line:
            args.append(f"--exclude={line}")
    if task.use_sudo:
        # run the remote rsync as root (reads root/other-owned files). Single argv element — the
        # space inside the value is fine since we exec directly (no shell). Requires the remote user
        # to have NOPASSWD sudo for /usr/bin/rsync.
        args.append("--rsync-path=sudo /usr/bin/rsync")
    if task.aux_args.strip():
        args.extend(task.aux_args.split())

    ssh_cmd = (
        f"ssh -i {source.ssh_key_path} -p {source.port} "
        "-o StrictHostKeyChecking=accept-new "
        "-o BatchMode=yes "
        "-o ServerAliveInterval=30"
    )
    args.extend(["-e", ssh_cmd])

    remote = f"{source.user}@{source.host}:{task.remote_path}"
    args.extend([remote, f"{FD_PATH_PREFIX}/{destination.fileno()}/"])
    return args


def build_syncoid_args(task: Task, source: Source) -> list[str]:
    """syncoid (ZFS replication). remote_path/local_path are ZFS dataset names."""
    user = validate_ssh_user(source.user)
    host = validate_ssh_host(source.host)
    remote_dataset = validate_zfs_source(task.remote_path)
    destination = validate_zfs_destination(task.local_path)
    args = ["syncoid"]
    if task.syncoid_recursive:
        args.append("--recursive")
    if task.syncoid_no_sync_snap:
        args.append("--no-sync-snap")
    if task.syncoid_compress and task.syncoid_compress != "none":
        args.append(f"--compress={task.syncoid_compress}")
    if task.syncoid_force_full:
        # Destroy a diverged/pre-existing target and do a full initial send.
        # Recovers a replica that shares no snapshots with the source (syncoid
        # otherwise "cowardly refuses to destroy your existing target").
        args.append("--force-delete")
    args.append(f"--sshkey={source.ssh_key_path}")
    if source.port and source.port != 22:
        args.append(f"--sshport={source.port}")
    # Match the rsync path's SSH behaviour. syncoid's ssh otherwise strict-checks
    # against the ephemeral /root/.ssh/known_hosts, so a container recreate breaks
    # every task with "Host key verification failed". accept-new + a known_hosts
    # in the persistent /data volume learns the host key once and keeps it.
    args.append("--sshoption=StrictHostKeyChecking=accept-new")
    args.append(f"--sshoption=UserKnownHostsFile={settings.data_dir / 'known_hosts'}")
    args.append("--sshoption=BatchMode=yes")
    args.append("--sshoption=ServerAliveInterval=30")
    if task.syncoid_extra_args.strip():
        args.extend(task.syncoid_extra_args.split())
    src = f"{user}@{host}:{remote_dataset}"  # remote ZFS dataset
    args.extend([src, destination])                          # local ZFS dataset
    return args


def validate_destination(task: Task, source: Source) -> None:
    """Reject an unsafe destination without writing anything.

    Runs before the concurrency semaphore so a bad row fails fast, and stays
    side-effect free: a run that is validated but never executed must not leave an
    empty directory behind. Uses the same no-follow traversal as execution.
    """
    if task.task_type == "syncoid":
        build_syncoid_args(task, source)
        return
    pinned = fs.walk_destination(task.local_path, create=False)
    if pinned is not None:
        pinned.close()


def build_command(task: Task, source: Source) -> list[str]:
    """Build a command for `task`, validating and materializing its destination.

    For rsync the destination is opened root-relative with no-follow semantics
    (creating missing components), then closed again. Use this for inspecting the
    argv; callers that actually execute must use `pinned_command`, which keeps the
    descriptor open across the exec so the argument cannot dangle.
    """
    if task.task_type == "syncoid":
        return build_syncoid_args(task, source)
    with fs.open_destination(task.local_path, create=True) as destination:
        return build_rsync_args(task, source, destination=destination)


@contextmanager
def pinned_command(task: Task, source: Source):
    """Yield `(args, pass_fds)` with the destination descriptor held open.

    The descriptor must outlive `create_subprocess_exec`: rsync resolves
    `/proc/self/fd/N` in its own process, so the fd has to be inherited and still
    open at exec time. Closing it earlier would leave a dangling argument.
    """
    if task.task_type == "syncoid":
        # ZFS receive creates its own dataset; there is no filesystem path to pin.
        yield build_syncoid_args(task, source), ()
        return
    # `create=True`: making the directory is itself a write, so it happens through
    # the same root-relative, no-follow traversal as the transfer.
    with fs.open_destination(task.local_path, create=True) as destination:
        yield (
            build_rsync_args(task, source, destination=destination),
            (destination.fileno(),),
        )


def validate_zfs_destination(dataset: str) -> str:
    """Return a safe configured ZFS destination descendant or fail closed."""
    if not _ZFS_DATASET.fullmatch(dataset):
        raise ValueError("ZFS destination has an invalid dataset name")
    allowed_roots = settings.zfs_dest_roots_list
    if not allowed_roots:
        raise ValueError("ZFS destination roots are not configured")
    for root in allowed_roots:
        if not _ZFS_DATASET.fullmatch(root):
            raise ValueError("ZFS destination root configuration is invalid")
        if dataset.startswith(f"{root}/"):
            return dataset
    raise ValueError("ZFS destination is outside configured roots")


def validate_zfs_source(dataset: str) -> str:
    """Return a source ZFS dataset name or reject executable syntax."""
    if not _ZFS_DATASET.fullmatch(dataset):
        raise ValueError("ZFS source has an invalid dataset name")
    return dataset


def validate_ssh_host(host: str) -> str:
    """Return a DNS-name/IPv4-shaped SSH host or reject executable syntax."""
    if len(host) > 253 or not _SSH_HOST.fullmatch(host):
        raise ValueError("SSH host has an invalid name")
    return host


def validate_ssh_user(user: str) -> str:
    """Return a portable SSH username or reject executable syntax."""
    if len(user) > 255 or not _SSH_USER.fullmatch(user):
        raise ValueError("SSH user has an invalid name")
    return user


async def _prune_zfs_hourly(dataset: str, keep: int, log_path: Path) -> None:
    """Keep the N newest zfs-auto-snap_hourly snapshots on `dataset`, destroy older ones.
    Mirrors the user's syncoid script: anchored to `<dataset>@zfs-auto-snap_hourly-`."""
    if keep < 1:
        raise ValueError("prune retention must be at least 1")
    dataset = validate_zfs_destination(dataset)
    proc = await asyncio.create_subprocess_exec(
        "zfs", "list", "-H", "-t", "snapshot", "-o", "name", "-s", "creation", "-r", dataset,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        out, _ = await proc.communicate()
    except asyncio.CancelledError:
        await _shield_process_cleanup(proc)
        raise
    prefix = f"{dataset}@zfs-auto-snap_hourly-"
    snaps = [ln for ln in out.decode(errors="replace").splitlines() if ln.startswith(prefix)]
    excess = len(snaps) - keep
    with open(log_path, "ab") as logf:
        logf.write(f"\n# prune: {len(snaps)} hourly snaps on {dataset}, keep {keep} -> destroy {max(0, excess)}\n".encode())
        for s in snaps[: max(0, excess)]:
            p = await asyncio.create_subprocess_exec(
                "zfs", "destroy", s,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                o, _ = await p.communicate()
            except asyncio.CancelledError:
                await _shield_process_cleanup(p)
                raise
            logf.write(f"destroy {s}: rc={p.returncode} {o.decode(errors='replace').strip()}\n".encode())


_SYNCOID_BYTES = re.compile(r"(\d[\d.]*)\s*([KMGT]?)i?B(?:ytes)?\s+(?:sent|received|transferred)", re.I)
_STATS_FILES = re.compile(r"Number of regular files transferred:\s+([\d,]+)")
_STATS_BYTES = re.compile(r"Total transferred file size:\s+([\d,]+)\s+bytes")


def _parse_stats(log_text: str) -> tuple[Optional[int], Optional[int]]:
    files = bytes_ = None
    m = _STATS_FILES.search(log_text)
    if m:
        files = int(m.group(1).replace(",", ""))
    m = _STATS_BYTES.search(log_text)
    if m:
        bytes_ = int(m.group(1).replace(",", ""))
    return files, bytes_


async def _terminate_process_group(proc: asyncio.subprocess.Process) -> None:
    """Terminate and reap the isolated process group for a cancelled run."""
    if proc.returncode is not None:
        await proc.wait()
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()


async def _shield_process_cleanup(proc: asyncio.subprocess.Process) -> None:
    """Finish process cleanup even if the execution task is cancelled again."""
    cleanup = asyncio.create_task(_terminate_process_group(proc))
    while True:
        try:
            await asyncio.shield(cleanup)
            return
        except asyncio.CancelledError:
            if cleanup.done():
                cleanup.result()
                return


async def _execute_run(run_id: int) -> int:
    """Execute a run that was already admitted and return its id."""
    with Session(engine) as session:
        run = session.get(Run, run_id)
        if not run:
            raise ValueError(f"run {run_id} not found")
        task = session.get(Task, run.task_id)
        if not task:
            raise ValueError(f"task {run.task_id} not found")
        source = session.get(Source, task.source_id)
        if not source:
            raise ValueError(f"source {task.source_id} not found")
        # Validate the destination before admitting the run to the semaphore, so an
        # unsafe row fails fast exactly as it did before. This does NOT create the
        # directory: the handle used for the exec is opened later, in
        # `pinned_command`, so a run that never executes writes nothing.
        validate_destination(task, source)
        # Computed here, inside the validated block, so the key is derived from
        # the same canonicalization the boundary just accepted.
        destination_key = _destination_key(task)
        log_path = settings.log_dir / run.log_filename

    # Acquisition order is fixed everywhere: destination, then the global limit,
    # then the source. A single order is what makes this deadlock-free.
    #
    # The destination is taken OUTSIDE the semaphore deliberately. A run waiting
    # for a busy destination holds no concurrency permit, so it cannot occupy a
    # slot that an unrelated backup could be using — which is the whole point of
    # the change.
    async with (
        _destination_exclusion(run_id, destination_key),
        _global_sem,
        _source_lock(source.id),
    ):
        with Session(engine) as session:
            run = session.get(Run, run_id)
            if not run:
                raise ValueError(f"run {run_id} not found")
            if run.state != RunState.pending:
                raise RunNotOwnedError(f"run {run_id} is not pending")
            run.state = RunState.running
            run.started_at = utcnow()
            session.add(run)
            session.commit()

        # The rsync destination directory is created inside `pinned_command` via the
        # same root-relative no-follow traversal that produces the handed-to-rsync
        # descriptor; syncoid's target is a ZFS dataset `zfs receive` creates itself.

        exit_code: Optional[int] = None
        proc: Optional[asyncio.subprocess.Process] = None
        cancellation: Optional[asyncio.CancelledError] = None
        try:
            # The destination descriptor is opened here and stays open across
            # `create_subprocess_exec`, because rsync dereferences /proc/self/fd/N
            # in its own process. For rsync this also performs the no-follow
            # directory creation that used to be a separate pathname `mkdir`.
            with pinned_command(task, source) as (args, pass_fds):
                with open(log_path, "wb") as logf:
                    logf.write(f"# pullbackup run {run_id} ({task.task_type})\n# args: {' '.join(args)}\n\n".encode())
                    logf.flush()
                    proc = await asyncio.create_subprocess_exec(
                        *args,
                        stdout=logf,
                        stderr=asyncio.subprocess.STDOUT,
                        start_new_session=True,
                        pass_fds=pass_fds,
                    )
                    exit_code = await proc.wait()
        except asyncio.CancelledError as exc:
            cancellation = exc
            if proc is not None:
                await _shield_process_cleanup(proc)
            with open(log_path, "ab") as logf:
                logf.write(b"\n# runner cancelled; subprocess group terminated\n")
            exit_code = -1
        except Exception as e:  # subprocess launch / FS error
            with open(log_path, "ab") as logf:
                logf.write(f"\n# runner error: {e}\n".encode())
            exit_code = -1

        # syncoid: optional snapshot prune on the destination dataset after a clean run
        if task.task_type == "syncoid" and exit_code == 0 and task.prune_keep_hourly:
            try:
                await _prune_zfs_hourly(task.local_path, task.prune_keep_hourly, log_path)
            except Exception as e:
                with open(log_path, "ab") as logf:
                    logf.write(f"\n# prune error: {e}\n".encode())

        log_text = log_path.read_text(errors="replace") if log_path.exists() else ""
        files, bytes_ = _parse_stats(log_text)

        with Session(engine) as session:
            run = session.get(Run, run_id)
            run.finished_at = utcnow()
            run.exit_code = exit_code
            run.bytes_transferred = bytes_
            run.files_transferred = files
            run.state = RunState.success if exit_code == 0 else RunState.failed
            if cancellation is not None:
                # An OPERATOR cancellation gets its own terminal state, because
                # `failed` would misreport a deliberate stop as a broken
                # transfer. A cancellation from anywhere else — application
                # shutdown, in particular — keeps `failed`: there the run really
                # was interrupted rather than stopped on purpose, and an
                # operator reading the history needs to tell the two apart.
                if _cancel_was_requested(run_id):
                    run.state = RunState.cancelled
                run.error_message = "run cancelled"
            elif exit_code != 0:
                run.error_message = f"rsync exited {exit_code}"
            session.add(run)
            session.commit()

        # fire-and-forget notifications
        from . import notify
        asyncio.create_task(notify.dispatch(run_id))
        if cancellation is not None:
            raise cancellation
        return run_id


def _cancel_was_requested(run_id: int) -> bool:
    """True when an operator asked for this specific run to stop.

    The distinction matters at terminalization: an operator cancellation is
    ``cancelled``, while any other interruption (application shutdown, a crashed
    owner) stays ``failed``. Both write ``error_message = "run cancelled"``, so
    the state is the only thing that tells them apart in the history.
    """
    owner = _run_owners.get(run_id)
    return owner is not None and owner.cancel_requested


def _terminalize_owned_run(run_id: int, error_message: str) -> bool:
    """Fail an active run when its execution owner exits before terminalization."""
    with Session(engine) as session:
        run = session.get(Run, run_id)
        if not run or run.state not in (RunState.pending, RunState.running):
            return False
        run.state = (
            RunState.cancelled
            if error_message == "run cancelled" and _cancel_was_requested(run_id)
            else RunState.failed
        )
        run.finished_at = utcnow()
        run.exit_code = -1
        run.error_message = error_message
        session.add(run)
        session.commit()
        return True


def _terminalize_cancelled_run(run_id: int) -> bool:
    return _terminalize_owned_run(run_id, "run cancelled")


async def execute_run(run_id: int) -> int:
    """Execute an admitted run and terminalize it if its owner exits early.

    Ownership is registered here rather than in ``start_admitted_run`` so that
    every path into execution — the HTTP "run now", the scheduler, and the
    ``run_task`` convenience wrapper — is cancellable by id. The ``finally``
    releases the entry on BOTH exits, so the map holds exactly the runs this
    process can actually signal.
    """
    owner = RunOwner(
        execution=asyncio.current_task(),  # type: ignore[arg-type]
        settled=asyncio.Event(),
    )
    _run_owners[run_id] = owner
    try:
        return await _execute_run(run_id)
    except asyncio.CancelledError:
        _terminalize_cancelled_run(run_id)
        raise
    except RunNotOwnedError:
        raise
    except Exception as error:
        _terminalize_owned_run(
            run_id,
            f"run owner failed: {type(error).__name__}: {error}",
        )
        raise
    finally:
        # Pop before signalling, so a waiter that wakes on `settled` can never
        # observe a stale owner for a run that has already finished.
        if _run_owners.get(run_id) is owner:
            del _run_owners[run_id]
        owner.settled.set()


# How long a cancellation waits for the execution to unwind. `_terminate_process_group`
# already bounds its own TERM->KILL escalation at 5s; this is the outer bound that
# covers the log write and the terminal database commit that follow it.
CANCEL_SETTLE_TIMEOUT_SECONDS = 30


async def cancel_run(run_id: int) -> CancelResult:
    """Stop the run this process owns for ``run_id``, or explain why it cannot.

    Deliberately never terminalizes a row it cannot prove it owns: the only
    thing that makes a run cancellable is an entry in ``_run_owners``, which is
    written by ``execute_run`` itself. A pending row, a row left behind by a
    previous process, and an unknown id all get an honest non-cancelled outcome
    with the row untouched.

    The completion-vs-cancel race is resolved by reading the row AFTER the
    execution has settled rather than by asserting what the request intended. A
    run that finished normally while the request was in flight reports
    ``already_finished`` with ``success`` — never ``cancelled``. A run that is
    still active after the wait reports ``still_running``: the request achieved
    nothing terminal and says so.
    """

    def _state() -> Optional[RunState]:
        with Session(engine) as session:
            run = session.get(Run, run_id)
            return None if run is None else run.state

    state = _state()
    if state is None:
        return CancelResult(outcome=CancelOutcome.not_found)
    if state not in (RunState.pending, RunState.running):
        return CancelResult(outcome=CancelOutcome.already_finished, state=state)

    owner = _run_owners.get(run_id)
    if owner is None:
        return CancelResult(outcome=CancelOutcome.not_owned, state=state)

    owner.cancel_requested = True
    owner.execution.cancel()
    try:
        await asyncio.wait_for(
            owner.settled.wait(), timeout=CANCEL_SETTLE_TIMEOUT_SECONDS
        )
    except TimeoutError:
        log.error("run %s did not settle within the cancellation timeout", run_id)

    settled_state = _state()
    if settled_state == RunState.cancelled:
        return CancelResult(outcome=CancelOutcome.cancelled, state=settled_state)
    if settled_state in (RunState.pending, RunState.running):
        # The signal was sent but the execution has not unwound, so the row is
        # still active. Reporting this as `already_finished` would produce the
        # self-contradictory "already finished (running)" and tell an operator
        # the transfer stopped while it is still copying bytes.
        return CancelResult(outcome=CancelOutcome.still_running, state=settled_state)
    return CancelResult(outcome=CancelOutcome.already_finished, state=settled_state)


def start_admitted_run(run_id: int) -> asyncio.Task[int]:
    """Start and retain application ownership of an already-admitted run."""
    execution_coro = execute_run(run_id)
    try:
        execution = asyncio.create_task(execution_coro)
    except BaseException:
        execution_coro.close()
        _terminalize_owned_run(run_id, "failed to start admitted run")
        raise
    _execution_tasks.add(execution)

    def execution_done(completed: asyncio.Task[int]) -> None:
        _execution_tasks.discard(completed)
        if completed.cancelled():
            return
        if error := completed.exception():
            log.error(
                "admitted run %s failed",
                run_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    execution.add_done_callback(execution_done)
    return execution


async def shutdown_execution_tasks() -> None:
    """Cancel and await all application-owned executions during shutdown."""
    executions = tuple(_execution_tasks)
    for execution in executions:
        execution.cancel()
    if executions:
        await asyncio.gather(*executions, return_exceptions=True)


async def run_task(task_id: int) -> int:
    """Admit and execute one task run. Returns the Run id."""
    admission = await admit_run(task_id)
    if not admission.accepted:
        raise RuntimeError(f"run admission denied: {admission.reason}")
    return await execute_run(admission.accepted_run_id())
