import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from sqlmodel import Session
from ..config import settings
from ..db import engine
from ..models import Run, RunState, Task, Source, utcnow

# concurrency control
_global_sem = asyncio.Semaphore(settings.max_concurrent_runs)
_source_locks: dict[int, asyncio.Lock] = {}


def _source_lock(source_id: int) -> asyncio.Lock:
    lock = _source_locks.get(source_id)
    if lock is None:
        lock = asyncio.Lock()
        _source_locks[source_id] = lock
    return lock


def build_rsync_args(task: Task, source: Source) -> list[str]:
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
    args.extend([remote, task.local_path])
    return args


def build_syncoid_args(task: Task, source: Source) -> list[str]:
    """syncoid (ZFS replication). remote_path/local_path are ZFS dataset names."""
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
    src = f"{source.user}@{source.host}:{task.remote_path}"  # remote ZFS dataset
    args.extend([src, task.local_path])                      # local ZFS dataset
    return args


def build_command(task: Task, source: Source) -> list[str]:
    if task.task_type == "syncoid":
        return build_syncoid_args(task, source)
    return build_rsync_args(task, source)


async def _prune_zfs_hourly(dataset: str, keep: int, log_path: Path) -> None:
    """Keep the N newest zfs-auto-snap_hourly snapshots on `dataset`, destroy older ones.
    Mirrors the user's syncoid script: anchored to `<dataset>@zfs-auto-snap_hourly-`."""
    proc = await asyncio.create_subprocess_exec(
        "zfs", "list", "-H", "-t", "snapshot", "-o", "name", "-s", "creation", "-r", dataset,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    prefix = f"{dataset}@zfs-auto-snap_hourly-"
    snaps = [ln for ln in out.decode(errors="replace").splitlines() if ln.startswith(prefix)]
    excess = len(snaps) - keep
    with open(log_path, "ab") as logf:
        logf.write(f"\n# prune: {len(snaps)} hourly snaps on {dataset}, keep {keep} -> destroy {max(0, excess)}\n".encode())
        for s in snaps[: max(0, excess)]:
            p = await asyncio.create_subprocess_exec(
                "zfs", "destroy", s, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            o, _ = await p.communicate()
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


async def run_task(task_id: int) -> int:
    """Execute one rsync run for the task. Returns the Run id."""
    with Session(engine) as session:
        task = session.get(Task, task_id)
        if not task:
            raise ValueError(f"task {task_id} not found")
        source = session.get(Source, task.source_id)
        if not source:
            raise ValueError(f"source {task.source_id} not found")

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_filename = f"task-{task.id}-{ts}.log"
        run = Run(task_id=task.id, state=RunState.pending, log_filename=log_filename)
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id
        args = build_command(task, source)
        log_path = settings.log_dir / log_filename

    async with _global_sem, _source_lock(source.id):
        with Session(engine) as session:
            run = session.get(Run, run_id)
            run.state = RunState.running
            run.started_at = utcnow()
            session.add(run)
            session.commit()

        # rsync writes into a filesystem path (create it); syncoid's target is a ZFS
        # dataset that `zfs receive` creates itself — don't mkdir it.
        if task.task_type != "syncoid":
            Path(task.local_path).mkdir(parents=True, exist_ok=True)

        exit_code: Optional[int] = None
        try:
            with open(log_path, "wb") as logf:
                logf.write(f"# pullback run {run_id} ({task.task_type})\n# args: {' '.join(args)}\n\n".encode())
                logf.flush()
                proc = await asyncio.create_subprocess_exec(
                    *args, stdout=logf, stderr=asyncio.subprocess.STDOUT
                )
                exit_code = await proc.wait()
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
            if exit_code != 0:
                run.error_message = f"rsync exited {exit_code}"
            session.add(run)
            session.commit()

        # fire-and-forget notifications
        from . import notify
        asyncio.create_task(notify.dispatch(run_id))
        return run_id
