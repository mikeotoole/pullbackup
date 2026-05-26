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


def get_session():
    with Session(engine) as session:
        yield session
