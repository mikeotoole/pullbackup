import asyncio
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse
from sqlmodel import Session, select
from ..db import get_session
from ..models import Run, RunState
from ..config import settings

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
