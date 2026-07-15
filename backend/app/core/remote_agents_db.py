"""SQLAlchemy engine + ORM models for the Remote Agents feature.

Lives in its own tables (``remote_agents`` / ``remote_agents_config``) in the
main agents-platform Postgres database, via their own ``Base`` so a bug in
the main app's migrations (``db.py``) can't cross-touch this feature's schema
and vice versa. All access goes through the ORM below — no raw SQL.

Shares ``db.py``'s engine/connection pool (not a separate ``create_engine``)
— two independent pools against the same Postgres instance just halved the
headroom available under ``max_connections`` for no isolation benefit; the
schema/migration isolation above doesn't require a separate pool too.
"""
from __future__ import annotations

import secrets
import time
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Integer, String, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from ..db import engine


class Base(DeclarativeBase):
    pass


class RemoteAgentRow(Base):
    __tablename__ = "remote_agents"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[int] = mapped_column(Integer)
    # JSON-encoded list of {name, target_port, public_port} — ports this
    # profile exposes via the WS tunnel (see api/tunnels.py). Applied
    # immediately on save and on every agent (re)connect.
    tunnels: Mapped[str] = mapped_column(Text, default="[]")


class ConfigRow(Base):
    __tablename__ = "remote_agents_config"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


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
    # create_all() only creates missing tables, not missing columns on
    # existing ones — `tunnels` was added after remote_agents already
    # existed in production, so add it explicitly, idempotently. SQLite (used
    # by the test suite) has no "IF NOT EXISTS" clause for ADD COLUMN, so
    # check via PRAGMA instead; production Postgres keeps the single
    # idempotent statement.
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            cols = [row[1] for row in conn.execute(text("PRAGMA table_info(remote_agents)")).fetchall()]
            if "tunnels" not in cols:
                conn.execute(text("ALTER TABLE remote_agents ADD COLUMN tunnels TEXT DEFAULT '[]'"))
        else:
            conn.execute(text(
                "ALTER TABLE remote_agents ADD COLUMN IF NOT EXISTS tunnels TEXT DEFAULT '[]'"
            ))
    with session_scope() as s:
        if not s.get(ConfigRow, "mcp_api_key"):
            s.add(ConfigRow(key="mcp_api_key", value=secrets.token_urlsafe(32)))


def now_epoch() -> int:
    return int(time.time())
