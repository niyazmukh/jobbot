"""Engine and session helpers."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from jobbot.config import get_settings


def create_engine_from_settings() -> Engine:
    """Create the SQLAlchemy engine from app settings."""

    settings = get_settings()
    engine = create_engine(settings.resolved_database_url, future=True)
    if settings.resolved_database_url.startswith("sqlite"):
        _configure_sqlite(engine)
    return engine


def _configure_sqlite(engine: Engine) -> None:
    """Enable SQLite WAL-friendly pragmas on connect."""

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()


def get_session_factory() -> sessionmaker[Session]:
    """Return a configured sessionmaker."""

    return sessionmaker(bind=create_engine_from_settings(), autoflush=False, autocommit=False)


SessionLocal = get_session_factory()
