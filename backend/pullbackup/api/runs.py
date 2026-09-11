# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
import asyncio
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse
from sqlmodel import Session, select
from ..db import get_session
from ..models import Run, RunState
from ..config import settings
from ..services.runner import CancelOutcome, cancel_run as cancel_owned_run

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("")
def list_runs(task_id: int | None = None, limit: int = 50, session: Session = Depends(get_session)):
    stmt = select(Run).order_by(Run.started_at.desc()).limit(limit)
    if task_id is not None:
        stmt = select(Run).where(Run.task_id == task_id).order_by(Run.started_at.desc()).limit(limit)
    return session.exec(stmt).all()


@router.get("/{run_id}")
def get_run(run_id: int, session: Session = Depends(get_session)):
    r = session.get(Run, run_id)
    if not r:
        raise HTTPException(404)
    return r


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: int, session: Session = Depends(get_session)):
    """Stop a run this application is currently executing.

    Authenticated like every other ``/api/`` route — the middleware allowlist in
    ``http_auth.py`` names what is public, and this is not on it.

    The four outcomes map to distinct HTTP answers rather than being collapsed
    into a cheerful 200, because an operator who clicked Stop needs to know
    whether the transfer actually stopped:

    ``404``
        No such run.
    ``409 ... not owned``
        The row is active but this process holds no execution for it — a pending
        run that has not started, or one left behind by a previous process.
        Nothing was signalled and nothing was written.
    ``409 ... already <state>``
        The run was already terminal, including the race where it completed
        while the request was in flight. The state named is the one it really
        reached, so a run that succeeded is never reported as cancelled.
    ``504 ... has not stopped``
        The process group was signalled but the run is still active. Nothing
        terminal happened, and saying otherwise would tell an operator the
        transfer stopped while it is still copying bytes.
    ``200``
        The process group was terminated and the run is ``cancelled``.
    """
    result = await cancel_owned_run(run_id)
    if result.outcome == CancelOutcome.not_found:
        raise HTTPException(404)
    if result.outcome == CancelOutcome.not_owned:
        raise HTTPException(
            409,
            f"run {run_id} is not owned by this process and cannot be cancelled",
        )
    if result.outcome == CancelOutcome.still_running:
        state = result.state.value if result.state else "unknown"
        raise HTTPException(
            504,
            f"stop requested, but run {run_id} has not stopped (still {state})",
        )
    if result.outcome == CancelOutcome.already_finished:
        state = result.state.value if result.state else "unknown"
        raise HTTPException(409, f"run {run_id} already finished ({state})")
    return {"cancelled": True, "state": result.state.value}


@router.get("/{run_id}/log")
def get_log(run_id: int, session: Session = Depends(get_session)):
    r = session.get(Run, run_id)
    if not r:
        raise HTTPException(404)
    p = settings.log_dir / r.log_filename
    if not p.exists():
        return {"content": ""}
    return {"content": p.read_text(errors="replace")}


@router.get("/{run_id}/log/stream")
async def stream_log(run_id: int, session: Session = Depends(get_session)):
    r = session.get(Run, run_id)
    if not r:
        raise HTTPException(404)
    log_path: Path = settings.log_dir / r.log_filename

    async def gen():
        # wait briefly for the log file to materialize
        for _ in range(20):
            if log_path.exists():
                break
            await asyncio.sleep(0.25)
        if not log_path.exists():
            yield {"event": "end", "data": ""}
            return
        with open(log_path, "r", errors="replace") as f:
            while True:
                line = f.readline()
                if line:
                    yield {"event": "log", "data": line.rstrip("\n")}
                    continue
                # check run state to decide whether to keep tailing
                with Session(session.bind) as s2:
                    cur = s2.get(Run, run_id)
                if cur and cur.state in (RunState.success, RunState.failed, RunState.cancelled):
                    yield {"event": "end", "data": cur.state.value}
                    return
                await asyncio.sleep(0.5)

    return EventSourceResponse(gen())
