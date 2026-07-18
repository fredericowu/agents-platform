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

from sqlalchemy import Boolean, Integer, String, Text, inspect as sa_inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from ..db import engine

# aw.tekflox.com bare-metal — the workspace's own host. Auto-mount is opt-in
# only for this profile (see `init_db`'s one-time migration default below and
# `src/services/remote_agent_fs_watcher.py`).
_BARE_METAL_ID = "7bd79fb5-08c8-4dd5-af10-4749e048375b"


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
    # Whether the FUSE watcher (src/services/remote_agent_fs_watcher.py)
    # should auto-mount this profile's filesystem while it's connected.
    # Point-and-click toggle in the Remote Agents UI — source of truth for
    # the watcher, overridden only by AW_REMOTE_FS_EXCLUDE when that env var
    # is explicitly set.
    auto_mount_fuse: Mapped[bool] = mapped_column(Boolean, default=True)


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
    # existing ones — `tunnels` and `auto_mount_fuse` were added after
    # remote_agents already existed in production, so add them explicitly,
    # idempotently. SQLite (used by the test suite) has no "IF NOT EXISTS"
    # clause for ADD COLUMN, so check existing columns via the inspector
    # first (works the same way against Postgres).
    with engine.begin() as conn:
        existing_cols = {c["name"] for c in sa_inspect(conn).get_columns("remote_agents")}
        if "tunnels" not in existing_cols:
            conn.execute(text("ALTER TABLE remote_agents ADD COLUMN tunnels TEXT DEFAULT '[]'"))
        if "auto_mount_fuse" not in existing_cols:
            default_literal = "1" if engine.dialect.name == "sqlite" else "true"
            conn.execute(text(
                f"ALTER TABLE remote_agents ADD COLUMN auto_mount_fuse BOOLEAN DEFAULT {default_literal}"
            ))
            # Data migration (one-time, only on the pass that adds the
            # column): the bare-metal profile keeps its historical
            # opt-out-by-default behavior, everyone else preserves the prior
            # "always auto-mount" behavior via the column default above.
            conn.execute(
                text("UPDATE remote_agents SET auto_mount_fuse = false WHERE id = :bare_metal_id"),
                {"bare_metal_id": _BARE_METAL_ID},
            )
    with session_scope() as s:
        if not s.get(ConfigRow, "mcp_api_key"):
            s.add(ConfigRow(key="mcp_api_key", value=secrets.token_urlsafe(32)))
        if not s.get(ConfigRow, "openai_compat_api_key"):
            s.add(ConfigRow(key="openai_compat_api_key", value=secrets.token_urlsafe(32)))


def now_epoch() -> int:
    return int(time.time())
