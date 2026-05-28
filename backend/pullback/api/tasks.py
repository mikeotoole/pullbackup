import asyncio
import logging
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlmodel import Session, select
from ..db import get_session
from ..models import Task, Run, RunState, Source, utcnow
from ..services import scheduler, kuma
from ..services.runner import run_task

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
log = logging.getLogger(__name__)


class TaskIn(BaseModel):
    name: str
    source_id: int
    remote_path: str
    local_path: str
    cron: str
    enabled: bool = True
    description: str = ""
    archive: bool = True
    recursive: bool = True
    times: bool = True
    compress: bool = True
    delete: bool = False
    quiet: bool = False
    preserve_permissions: bool = False
    preserve_xattrs: bool = False
    delay_updates: bool = True
    bwlimit_kbps: Optional[int] = None
    exclude_patterns: str = ""
    aux_args: str = ""
    notify_matrix: bool = False
    notify_matrix_on_success: bool = False
    kuma_enabled: bool = False


class TaskOut(TaskIn):
    id: int
    kuma_monitor_id: Optional[int] = None
    kuma_push_token: Optional[str] = None
    next_run: Optional[str] = None
    last_run_id: Optional[int] = None
    last_run_at: Optional[datetime] = None
    last_run_state: Optional[RunState] = None


def _to_out(t: Task, session: Session) -> TaskOut:
    last = session.exec(
        select(Run).where(Run.task_id == t.id).order_by(Run.started_at.desc())
    ).first()
    return TaskOut(
        **{k: getattr(t, k) for k in TaskIn.model_fields.keys()},
        id=t.id,
        kuma_monitor_id=t.kuma_monitor_id,
        kuma_push_token=t.kuma_push_token,
        next_run=scheduler.next_run_iso(t),
        last_run_id=last.id if last else None,
        last_run_at=last.started_at if last else None,
        last_run_state=last.state if last else None,
    )


@router.get("", response_model=list[TaskOut])
def list_tasks(session: Session = Depends(get_session)):
    return [_to_out(t, session) for t in session.exec(select(Task)).all()]


@router.post("", response_model=TaskOut, status_code=201)
async def create_task(data: TaskIn, session: Session = Depends(get_session)):
    if not session.get(Source, data.source_id):
        raise HTTPException(400, "source not found")
    t = Task(**data.model_dump())
    session.add(t)
    session.commit()
    session.refresh(t)
    await _sync_kuma(t, session)
    scheduler.upsert_job(t)
    return _to_out(t, session)


@router.get("/{task_id}", response_model=TaskOut)
def get_task(task_id: int, session: Session = Depends(get_session)):
    t = session.get(Task, task_id)
    if not t:
        raise HTTPException(404)
    return _to_out(t, session)


@router.patch("/{task_id}", response_model=TaskOut)
async def update_task(task_id: int, data: TaskIn, session: Session = Depends(get_session)):
    t = session.get(Task, task_id)
    if not t:
        raise HTTPException(404)
    prev_cron = t.cron
    prev_enabled = t.enabled
    prev_kuma = t.kuma_enabled
    for k, v in data.model_dump().items():
        setattr(t, k, v)
    t.updated_at = utcnow()
    session.add(t)
    session.commit()
    session.refresh(t)
    await _sync_kuma(t, session, prev_kuma=prev_kuma, prev_cron=prev_cron, prev_enabled=prev_enabled)
    scheduler.upsert_job(t)
    return _to_out(t, session)


@router.delete("/{task_id}", status_code=204)
async def delete_task(task_id: int, session: Session = Depends(get_session)):
    t = session.get(Task, task_id)
    if not t:
        raise HTTPException(404)
    if t.kuma_monitor_id and (k := kuma.client()):
        try:
            await k.delete(t.kuma_monitor_id)
        except Exception as e:
            log.warning("kuma delete failed: %s", e)
    scheduler.remove_job(task_id)
    session.delete(t)
    session.commit()


@router.post("/{task_id}/run", status_code=202)
async def run_now(task_id: int, bg: BackgroundTasks, session: Session = Depends(get_session)):
    if not session.get(Task, task_id):
        raise HTTPException(404)
    asyncio.create_task(run_task(task_id))
    return {"queued": True}


async def _sync_kuma(t: Task, session: Session, prev_kuma: bool = False, prev_cron: str = "", prev_enabled: bool = True):
    k = kuma.client()
    if not k:
        return
    interval = kuma.cron_interval_seconds(t.cron)
    try:
        if t.kuma_enabled and not t.kuma_monitor_id:
            mon_id, token = await k.create_push_monitor(f"pullback: {t.name}", interval)
            t.kuma_monitor_id = mon_id
            t.kuma_push_token = token
            session.add(t)
            session.commit()
        elif t.kuma_enabled and t.kuma_monitor_id and (t.cron != prev_cron or t.enabled != prev_enabled):
            await k.update_interval(t.kuma_monitor_id, interval)
            await k.set_active(t.kuma_monitor_id, t.enabled)
        elif (not t.kuma_enabled) and t.kuma_monitor_id:
            await k.delete(t.kuma_monitor_id)
            t.kuma_monitor_id = None
            t.kuma_push_token = None
            session.add(t)
            session.commit()
    except Exception as e:
        log.warning("kuma sync for task %s failed: %s", t.id, e)
