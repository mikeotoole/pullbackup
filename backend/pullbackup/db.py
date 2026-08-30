# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
from sqlmodel import SQLModel, Session, create_engine
from .config import settings

# check_same_thread=False because APScheduler and request handlers both touch the DB
engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
    echo=False,
)


def init_db() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    settings.ssh_dir.mkdir(parents=True, exist_ok=True)
    # importing models for side-effect (SQLModel.metadata registration)
    from . import models  # noqa: F401
    SQLModel.metadata.create_all(engine)
    # lightweight additive migrations (create_all won't ALTER existing tables)
    _ensure_columns()


def _ensure_columns() -> None:
    """Add new nullable/defaulted columns to existing SQLite tables."""
    wanted = {
        "task": [
            ("use_sudo", "BOOLEAN NOT NULL DEFAULT 0"),
            ("task_type", "VARCHAR NOT NULL DEFAULT 'rsync'"),
            ("syncoid_recursive", "BOOLEAN NOT NULL DEFAULT 1"),
            ("syncoid_no_sync_snap", "BOOLEAN NOT NULL DEFAULT 1"),
            ("syncoid_compress", "VARCHAR NOT NULL DEFAULT ''"),
            ("syncoid_extra_args", "VARCHAR NOT NULL DEFAULT ''"),
            ("syncoid_force_full", "BOOLEAN NOT NULL DEFAULT 0"),
            ("prune_keep_hourly", "INTEGER"),
        ],
    }
    with engine.connect() as conn:
        for table, cols in wanted.items():
            existing = {r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}
            for name, ddl in cols:
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        conn.commit()


def get_session():
    with Session(engine) as session:
        yield session
