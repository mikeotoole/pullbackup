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
        args = build_rsync_args(task, source)
        log_path = settings.log_dir / log_filename

    async with _global_sem, _source_lock(source.id):
        with Session(engine) as session:
            run = session.get(Run, run_id)
            run.state = RunState.running
            run.started_at = utcnow()
            session.add(run)
            session.commit()

        Path(task.local_path).mkdir(parents=True, exist_ok=True)

        exit_code: Optional[int] = None
        try:
            with open(log_path, "wb") as logf:
                logf.write(f"# pullback run {run_id}\n# args: {' '.join(args)}\n\n".encode())
                logf.flush()
                proc = await asyncio.create_subprocess_exec(
                    *args, stdout=logf, stderr=asyncio.subprocess.STDOUT
                )
                exit_code = await proc.wait()
        except Exception as e:  # subprocess launch / FS error
            with open(log_path, "ab") as logf:
                logf.write(f"\n# runner error: {e}\n".encode())
            exit_code = -1

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
