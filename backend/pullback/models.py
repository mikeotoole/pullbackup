from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from sqlmodel import SQLModel, Field, Relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RunState(str, Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"
    cancelled = "cancelled"


class Source(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    user: str
    host: str
    port: int = 22
    ssh_key_path: str  # absolute path inside the container
    description: str = ""
    created_at: datetime = Field(default_factory=utcnow)

    tasks: list["Task"] = Relationship(back_populates="source")


class Task(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    source_id: int = Field(foreign_key="source.id")
    remote_path: str
    local_path: str
    cron: str  # "M H DOM MON DOW" — apscheduler's CronTrigger.from_crontab format
    enabled: bool = True
    description: str = ""

    # rsync flags — names mirror the TrueNAS form
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
    exclude_patterns: str = ""  # newline-delimited
    aux_args: str = ""  # raw extra args, space-split

    # notifications
    notify_matrix: bool = False
    notify_matrix_on_success: bool = False
    kuma_enabled: bool = False
    kuma_monitor_id: Optional[int] = None
    kuma_push_token: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    source: Optional[Source] = Relationship(back_populates="tasks")
    runs: list["Run"] = Relationship(back_populates="task")


class Run(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="task.id", index=True)
    state: RunState = RunState.pending
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    exit_code: Optional[int] = None
    bytes_transferred: Optional[int] = None
    files_transferred: Optional[int] = None
    error_message: str = ""
    log_filename: str = ""  # relative to settings.log_dir

    task: Optional[Task] = Relationship(back_populates="runs")
