from collections.abc import Generator
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from bot_app.models import WorkflowDefaults


def ensure_database_parent_directory(database_url: str) -> None:
    url = make_url(database_url)
    if url.drivername.startswith("sqlite") and url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)


def create_engine_for_url(database_url: str) -> Engine:
    ensure_database_parent_directory(database_url)
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args, future=True)


def create_session_factory(database_url: str) -> sessionmaker[Session]:
    engine = create_engine_for_url(database_url)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def _alembic_config(database_url: str) -> Config:
    config_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    config = Config(str(config_path))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def run_migrations(database_url: str) -> None:
    ensure_database_parent_directory(database_url)
    command.upgrade(_alembic_config(database_url), "head")


def ensure_workflow_defaults(session: Session) -> WorkflowDefaults:
    defaults = session.scalars(select(WorkflowDefaults).where(WorkflowDefaults.id == 1)).first()
    if defaults is None:
        defaults = WorkflowDefaults(id=1)
        session.add(defaults)
        session.commit()
        session.refresh(defaults)
    return defaults


def initialize_database(database_url: str) -> None:
    run_migrations(database_url)
    session_factory = create_session_factory(database_url)
    with session_factory() as session:
        ensure_workflow_defaults(session)


def get_session(database_url: str) -> Generator[Session, None, None]:
    session_factory = create_session_factory(database_url)
    with session_factory() as session:
        yield session
