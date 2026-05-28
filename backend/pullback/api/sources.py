from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select
from ..db import get_session
from ..models import Source, Task

router = APIRouter(prefix="/api/sources", tags=["sources"])


class SourceIn(BaseModel):
    name: str
    user: str
    host: str
    port: int = 22
    ssh_key_path: str
    description: str = ""


class SourceOut(SourceIn):
    id: int
    task_count: int


def _to_out(s: Source, task_count: int) -> SourceOut:
    return SourceOut(
        id=s.id, name=s.name, user=s.user, host=s.host, port=s.port,
        ssh_key_path=s.ssh_key_path, description=s.description, task_count=task_count,
    )


@router.get("", response_model=list[SourceOut])
def list_sources(session: Session = Depends(get_session)):
    rows = session.exec(select(Source)).all()
    counts: dict[int, int] = {}
    for src_id in session.exec(select(Task.source_id)).all():
        counts[src_id] = counts.get(src_id, 0) + 1
    return [_to_out(s, counts.get(s.id, 0)) for s in rows]


@router.post("", response_model=SourceOut, status_code=201)
def create_source(data: SourceIn, session: Session = Depends(get_session)):
    if session.exec(select(Source).where(Source.name == data.name)).first():
        raise HTTPException(409, "source name already exists")
    s = Source(**data.model_dump())
    session.add(s)
    session.commit()
    session.refresh(s)
    return _to_out(s, 0)


@router.get("/{source_id}", response_model=SourceOut)
def get_source(source_id: int, session: Session = Depends(get_session)):
    s = session.get(Source, source_id)
    if not s:
        raise HTTPException(404)
    n = session.exec(select(Task).where(Task.source_id == s.id)).all()
    return _to_out(s, len(n))


@router.patch("/{source_id}", response_model=SourceOut)
def update_source(source_id: int, data: SourceIn, session: Session = Depends(get_session)):
    s = session.get(Source, source_id)
    if not s:
        raise HTTPException(404)
    for k, v in data.model_dump().items():
        setattr(s, k, v)
    session.add(s)
    session.commit()
    session.refresh(s)
    n = session.exec(select(Task).where(Task.source_id == s.id)).all()
    return _to_out(s, len(n))


@router.delete("/{source_id}", status_code=204)
def delete_source(source_id: int, session: Session = Depends(get_session)):
    s = session.get(Source, source_id)
    if not s:
        raise HTTPException(404)
    if session.exec(select(Task).where(Task.source_id == s.id)).first():
        raise HTTPException(409, "source has tasks; delete them first")
    session.delete(s)
    session.commit()
