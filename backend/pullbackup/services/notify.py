# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
import logging
import uuid
import httpx
from sqlmodel import Session
from ..config import settings
from ..db import engine
from ..models import Run, RunState, Task, Source

log = logging.getLogger(__name__)


async def _matrix_send(body: str) -> None:
    if not settings.matrix_enabled:
        return
    txn = uuid.uuid4().hex
    url = (
        f"{settings.matrix_homeserver}/_matrix/client/v3/rooms/"
        f"{settings.matrix_room_id}/send/m.room.message/{txn}"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.put(
                url,
                json={"msgtype": "m.notice", "body": body},
                headers={"Authorization": f"Bearer {settings.matrix_token}"},
            )
            r.raise_for_status()
        except Exception as e:
            log.warning("matrix notify failed: %s", e)


async def _kuma_push(token: str, status: str, msg: str = "") -> None:
    if not settings.kuma_enabled or not token:
        return
    url = f"{settings.uptime_kuma_url.rstrip('/')}/api/push/{token}"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(url, params={"status": status, "msg": msg, "ping": ""})
            r.raise_for_status()
        except Exception as e:
            log.warning("kuma push failed: %s", e)


async def dispatch(run_id: int) -> None:
    with Session(engine) as session:
        run = session.get(Run, run_id)
        if not run:
            return
        task = session.get(Task, run.task_id)
        source = session.get(Source, task.source_id) if task else None
    if not task or not source:
        return

    ok = run.state == RunState.success
    label = "succeeded" if ok else "FAILED"
    mb = run.bytes_transferred or 0
    body = (
        f"pullback: task '{task.name}' {label}\n"
        f"  source: {source.name} ({source.user}@{source.host}:{task.remote_path})\n"
        f"  dest:   {task.local_path}\n"
        f"  exit:   {run.exit_code}  files={run.files_transferred}  bytes={mb}"
    )

    if task.notify_matrix and (not ok or task.notify_matrix_on_success):
        await _matrix_send(body)

    if task.kuma_enabled and task.kuma_push_token:
        await _kuma_push(task.kuma_push_token, "up" if ok else "down", f"exit={run.exit_code}")
