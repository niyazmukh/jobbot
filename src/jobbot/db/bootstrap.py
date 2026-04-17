"""Database bootstrap helpers."""

from __future__ import annotations

from jobbot.db.base import Base
from jobbot.db.session import create_engine_from_settings
from jobbot.db import models  # noqa: F401


def create_all_tables() -> None:
    """Create all tables from ORM metadata.

    This is a Phase 0 convenience path for local development before a fully
    migration-driven setup is wired into the CLI/runtime.
    """

    engine = create_engine_from_settings()
    Base.metadata.create_all(bind=engine)
