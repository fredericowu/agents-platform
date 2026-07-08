"""SQLAlchemy engine + ORM models for the Remote Agents feature.

Lives in its own tables (``remote_agents`` / ``remote_agents_config``) in the
main agents-platform Postgres database, via their own ``Base``/engine so a
bug in the main app's migrations (``db.py``) can't cross-touch this feature's
schema and vice versa. All access goes through the ORM below — no raw SQL.
"""
from __future__ import annotations

import secrets
import time
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from ..config import settings


class Base(DeclarativeBase):
    pass


class RemoteAgentRow(Base):
    __tablename__ = "remote_agents"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[int] = mapped_column(Integer)


class ConfigRow(Base):
    __tablename__ = "remote_agents_config"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


engine = create_engine(settings.database_url, future=True)
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


def init_db() -> None:
    Base.metadata.create_all(engine)
    with session_scope() as s:
        if not s.get(ConfigRow, "mcp_api_key"):
            s.add(ConfigRow(key="mcp_api_key", value=secrets.token_urlsafe(32)))


def now_epoch() -> int:
    return int(time.time())
