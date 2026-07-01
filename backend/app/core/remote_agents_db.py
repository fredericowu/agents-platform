"""SQLAlchemy engine + ORM models for the Remote Agents feature.

This lives on its own SQLite file (separate from the main agents-platform DB)
so the Windows agent exe / FUSE driver's on-disk state is untouched by
agents-platform schema migrations. All access goes through the ORM below —
no raw sqlite3 connections.
"""
from __future__ import annotations

import os
import secrets
import time
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Integer, MetaData, String, Table, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

DB_PATH = "/opt/agentic-workspace/.tmp/remote-agents.db"
OLD_DB_PATH = "/opt/agentic-workspace/src/custom_apps/aw-remote-agent/data/app.db"


class Base(DeclarativeBase):
    pass


class RemoteAgentRow(Base):
    __tablename__ = "remote_agents"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[int] = mapped_column(Integer)


class ConfigRow(Base):
    __tablename__ = "config"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
engine = create_engine(f"sqlite:///{DB_PATH}", future=True,
                       connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def _migrate_from_old_db(s: Session) -> None:
    """Best-effort one-time import from the old standalone aw-remote-agent
    custom app's SQLite file, via SQLAlchemy reflection (no raw SQL)."""
    if not os.path.exists(OLD_DB_PATH):
        return
    try:
        old_engine = create_engine(f"sqlite:///{OLD_DB_PATH}", future=True)
        meta = MetaData()
        old_agents = Table("remote_agents", meta, autoload_with=old_engine)
        old_config = Table("config", meta, autoload_with=old_engine)
        with old_engine.connect() as oc:
            for row in oc.execute(select(old_agents)).mappings():
                if not s.get(RemoteAgentRow, row["id"]):
                    s.add(RemoteAgentRow(id=row["id"], name=row["name"],
                                         description=row["description"] or "",
                                         created_at=row["created_at"]))
            old_key = oc.execute(
                select(old_config).where(old_config.c.key == "mcp_api_key")
            ).mappings().first()
            if old_key:
                existing = s.get(ConfigRow, "mcp_api_key")
                if existing:
                    existing.value = old_key["value"]
                else:
                    s.add(ConfigRow(key="mcp_api_key", value=old_key["value"]))
        old_engine.dispose()
    except Exception:
        pass  # migration is best-effort


def init_db() -> None:
    Base.metadata.create_all(engine)
    with session_scope() as s:
        if not s.get(ConfigRow, "mcp_api_key"):
            s.add(ConfigRow(key="mcp_api_key", value=secrets.token_urlsafe(32)))
    with session_scope() as s:
        count = s.query(RemoteAgentRow).count()
        if count == 0:
            _migrate_from_old_db(s)


def now_epoch() -> int:
    return int(time.time())
