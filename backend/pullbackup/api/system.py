# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
from fastapi import APIRouter, Depends
from sqlmodel import Session

from ..config import settings
from ..db import get_session
from ..services import fs, scheduler, ssh
from . import tasks as tasks_api
from .. import __version__

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/health")
def health():
    """Liveness for anything outside the process.

    INCIDENT 2026-09-06..10: the compose healthcheck probed this endpoint every
    thirty seconds for four days and stayed green while the scheduler executed
    nothing, because it only ever proved the HTTP server was answering. The
    `scheduler` block is the missing fact — an external monitor can now tell
    "the app is up" from "the app is backing things up".

    `ok` deliberately stays true when the scheduler is dead. This response is
    what the container healthcheck reads, and a container that restarts itself
    on a wedged scheduler would destroy the evidence and mask the wedge as a
    crash loop. Report the distinction; let a human or a monitor decide.
    """
    return {"ok": True, "version": __version__, "scheduler": scheduler.liveness()}


@router.get("/scheduler")
def scheduler_health(session: Session = Depends(get_session)):
    """Scheduler liveness plus every task whose schedule has stopped firing.

    Authenticated, unlike `/health`: this names which of the operator's tasks
    are failing, and the unauthenticated surface stays exactly one endpoint
    wide. `/health` carries the process-wide signal an anonymous monitor needs;
    this carries the per-task detail that only the operator should see.

    The two answer different questions and both are needed. A single wedged
    task is invisible in the heartbeat — the scheduler is fine, that one job is
    not — and a genuinely dead scheduler flags every task at once. Told apart,
    they point at different repairs.

    Reads the flag off the task list's own projection rather than deriving it
    again here. Two derivations of "is this task's schedule missed" would drift
    silently, and the drift would show up as this endpoint and the UI
    disagreeing about the same row — which is a rebuild of the exact confusion
    this card exists to remove.
    """
    missed = [
        row.id for row in tasks_api.list_tasks(session) if row.missed_schedule
    ]
    return {
        "scheduler": scheduler.liveness(),
        "missed_schedule_count": len(missed),
        "missed_schedule_task_ids": sorted(missed),
    }


@router.get("/info")
def info():
    return {
        "version": __version__,
        "dest_roots": [str(p) for p in settings.dest_roots_list],
        "tz": settings.tz_name,
        "matrix_enabled": settings.matrix_enabled,
        "kuma_enabled": settings.kuma_enabled,
        "kuma_url": settings.uptime_kuma_url,
    }


@router.get("/browse")
def browse(path: str | None = None):
    try:
        return fs.browse(path)
    except fs.PathNotAllowed as e:
        return {"error": str(e), "entries": []}


@router.get("/ssh-pubkey")
def ssh_pubkey():
    priv = ssh.ensure_default_key()
    return {"private_path": str(priv), "public_key": ssh.read_pubkey(priv)}
