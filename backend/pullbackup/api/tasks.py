# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlmodel import Session, select

from ..config import settings
from ..db import get_session
from ..models import Run, RunState, Source, Task, utcnow
from ..services import fs, kuma, scheduler
from ..services.runner import (
    admit_run,
    lifecycle_mutation_lock,
    start_admitted_run,
    validate_zfs_destination,
    validate_zfs_source,
)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
log = logging.getLogger(__name__)


class TaskFields(BaseModel):
    """Shared task field set, with NO validation.

    Deliberately validator-free so it can be reused by the output model: destination
    enforcement is a *write* boundary. A row persisted before that boundary existed
    must still serialize, or the single task an operator needs to remediate would
    break its own detail response and the whole task list.
    """

    name: str
    source_id: int
    remote_path: str
    local_path: str
    cron: str
    enabled: bool = True
    description: str = ""
    task_type: str = "rsync"
    syncoid_recursive: bool = True
    syncoid_no_sync_snap: bool = True
    syncoid_compress: str = ""
    syncoid_extra_args: str = ""
    syncoid_force_full: bool = False
    prune_keep_hourly: Optional[int] = Field(default=None, ge=1)
    archive: bool = True
    recursive: bool = True
    times: bool = True
    compress: bool = True
    delete: bool = False
    quiet: bool = False
    preserve_permissions: bool = False
    preserve_xattrs: bool = False
    delay_updates: bool = True
    use_sudo: bool = False
    bwlimit_kbps: Optional[int] = None
    exclude_patterns: str = ""
    aux_args: str = ""
    notify_matrix: bool = False
    notify_matrix_on_success: bool = False
    kuma_enabled: bool = False


class TaskIn(TaskFields):
    """Write model: everything accepted from a client passes the boundary checks."""

    @model_validator(mode="after")
    def validate_destination(self):
        if self.task_type == "syncoid":
            validate_zfs_source(self.remote_path)
            validate_zfs_destination(self.local_path)
        else:
            # rsync writes into the filesystem: the destination must resolve inside a
            # configured root, and no EXISTING component may be a symlink. This is the
            # same no-follow traversal the runner uses before exec, so the two
            # boundaries cannot drift; acceptance passes `create=False` because
            # validating a task must never write to disk.
            try:
                pinned = fs.walk_destination(self.local_path, create=False)
            except fs.PathNotAllowed as error:
                raise ValueError(str(error)) from error
            except OSError as error:
                # A component that exists but cannot serve as a directory — a
                # regular file in the way, a permission denial — is a property
                # of the destination the client asked for, not an internal
                # fault. Only PathNotAllowed was converted before, so these
                # escaped as an unhandled OSError and turned an ordinary bad
                # request into a 500. The runner deliberately keeps the raw
                # OSError (it records it on the failed run row for operators);
                # the translation belongs here, at the client boundary.
                raise ValueError(
                    f"destination {self.local_path!r} is not usable: {error}"
                ) from error
            if pinned is not None:
                pinned.close()
        return self


class TaskOut(TaskFields):
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
        **{k: getattr(t, k) for k in TaskFields.model_fields.keys()},
        id=t.id,
        kuma_monitor_id=t.kuma_monitor_id,
        kuma_push_token=t.kuma_push_token,
        next_run=scheduler.next_run_iso(t),
        last_run_id=last.id if last else None,
        last_run_at=last.started_at if last else None,
        last_run_state=last.state if last else None,
    )


def _has_active_run(session: Session, task_id: int) -> bool:
    return session.exec(
        select(Run).where(
            Run.task_id == task_id,
            Run.state.in_([RunState.pending, RunState.running]),
        )
    ).first() is not None


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
    async with lifecycle_mutation_lock():
        t = session.get(Task, task_id)
        if not t:
            raise HTTPException(404)
        if _has_active_run(session, task_id):
            raise HTTPException(409, "task cannot be modified while a run is pending or running")
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
    async with lifecycle_mutation_lock():
        t = session.get(Task, task_id)
        if not t:
            raise HTTPException(404)
        if _has_active_run(session, task_id):
            raise HTTPException(409, "task cannot be deleted while a run is pending or running")
        if t.kuma_monitor_id and (k := kuma.client()):
            try:
                await k.delete(t.kuma_monitor_id)
            except Exception as e:
                log.warning("kuma delete failed: %s", e)
        scheduler.remove_job(task_id)
        # Delete child runs first: Run.task_id is NOT NULL with no ORM cascade, so deleting the
        # parent task alone would try to NULL the FK and raise IntegrityError (the silent-500 that
        # made the UI "do nothing"). Also clean up each run's on-disk log.
        runs = session.exec(select(Run).where(Run.task_id == task_id)).all()
        for r in runs:
            if r.log_filename:
                try:
                    (settings.log_dir / r.log_filename).unlink(missing_ok=True)
                except Exception as e:
                    log.warning("run log unlink failed for %s: %s", r.log_filename, e)
            session.delete(r)
        session.delete(t)
        session.commit()


@router.post("/{task_id}/run", status_code=202)
async def run_now(task_id: int, bg: BackgroundTasks, session: Session = Depends(get_session)):
    if not session.get(Task, task_id):
        raise HTTPException(404)
    admission = await admit_run(task_id)
    if not admission.accepted:
        scope = "task" if admission.reason == "task_active" else "source"
        raise HTTPException(409, f"a run is already pending or running for this {scope}")
    start_admitted_run(admission.accepted_run_id())
    return {"queued": True}


async def _sync_kuma(t: Task, session: Session, prev_kuma: bool = False, prev_cron: str = "", prev_enabled: bool = True):
    k = kuma.client()
    if not k:
        return
    interval = kuma.cron_interval_seconds(t.cron)
    try:
        if t.kuma_enabled and not t.kuma_monitor_id:
            # The "pullback: " prefix is the CURRENT product identifier and is
            # renamed in tranche 2 together with the TaskForm help text that
            # describes it. tests/test_naming_tranche1.py reads this exact
            # literal and requires the UI text to agree, so the two cannot
            # drift and the UI cannot misdescribe monitors that already exist
            # in a user's Uptime Kuma instance.
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
