"""SQLAlchemy engine + session factory."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, future=True, echo=False,
                       connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager for a unit-of-work scope."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def init_db() -> None:
    """Create all tables. Idempotent. Also runs trivial inline migrations:
    adds new columns to existing tables (Postgres) without dropping data."""
    from . import models  # noqa: F401 — ensure models register
    Base.metadata.create_all(engine)
    _apply_inline_migrations()
    with engine.begin() as conn:
        _backfill_lesson_evidence_runs(conn)
    _seed_retro_score_weights()


# NOTE: the old cancel_orphaned_runs() was removed. Cancelling every 'running'
# run on startup is wrong under the Redis Stream architecture — the docker agent
# keeps publishing while the platform is down, and its events arrive once the
# platform is back up. Startup recovery is now executor.recover_orphaned_runs(),
# which re-attaches in-flight runs via their durable stream instead of killing them.


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    """Add `column` to `table` if it does not already exist. Cross-database safe."""
    from sqlalchemy import inspect as sa_inspect, text
    insp = sa_inspect(conn)
    if column not in {c["name"] for c in insp.get_columns(table)}:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))


def _apply_inline_migrations() -> None:
    """Add missing columns to existing tables (Postgres in prod; DDL below must
    use Postgres-valid literals, e.g. BOOLEAN DEFAULT false/true, not 0/1)."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if "runs" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("runs")}
    additions = [
        ("parent_run_id", "VARCHAR"),
        ("initiator_kind", "VARCHAR DEFAULT 'agent_run'"),
        ("initiator_id", "VARCHAR"),
        ("node_id", "VARCHAR"),
        ("model_slug", "VARCHAR"),
        # First-class target FK — links a tree of runs to an overall delivery goal.
        ("target_id", "VARCHAR NOT NULL DEFAULT 'unlinked'"),
        # Which agent or workflow actually ran — separate from target_slug (the Target).
        ("source_slug", "VARCHAR"),
        # Telegram progress-bubble message id — lets restart recovery flip the
        # [processing] button to [done] after re-attaching an interrupted run.
        ("proc_msg_id", "VARCHAR"),
        # Agent-to-agent "call me back" persistence (core.wakeups) — survives
        # an AP restart mid-flight via rearm_pending_agent_callbacks().
        ("call_me_back", "BOOLEAN DEFAULT false"),
        ("callback_done", "BOOLEAN DEFAULT false"),
        ("callback_origin_run_id", "VARCHAR"),
        # Agent-to-agent chain depth loop guard (see Run.hop_count).
        ("hop_count", "INTEGER DEFAULT 0"),
    ]
    with engine.begin() as conn:
        for col, ddl in additions:
            if col not in existing:
                conn.execute(text(f"ALTER TABLE runs ADD COLUMN {col} {ddl}"))

    # mcp_config + inherit_from + permissions on agents
    if "agents" in insp.get_table_names():
        agent_cols = {c["name"] for c in insp.get_columns("agents")}
        if "mcp_config" not in agent_cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE agents ADD COLUMN mcp_config JSON DEFAULT '{}'"))
        if "inherit_from" not in agent_cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE agents ADD COLUMN inherit_from VARCHAR"))
        if "permissions" not in agent_cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE agents ADD COLUMN permissions JSON DEFAULT '{}'"))
        if "agent_config_slug" not in agent_cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE agents ADD COLUMN agent_config_slug VARCHAR"))

    # soft-delete columns on agents and workflows
    for tbl in ("agents", "workflows"):
        if tbl in insp.get_table_names():
            cols = {c["name"] for c in insp.get_columns(tbl)}
            if "deleted_at" not in cols:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN deleted_at DATETIME"))
            # use_cases JSON column — short list of example use cases so the
            # conductor / UI can pick the right agent or workflow without
            # reading the full system_prompt. Defaults to '[]'.
            if "use_cases" not in cols:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN use_cases JSON DEFAULT '[]'"))

    # Wave-2 additions on `targets` — only run if the table exists yet
    # (Base.metadata.create_all builds it; for an upgrade-in-place we add the
    # two new columns idempotently).
    if "targets" in insp.get_table_names():
        target_cols = {c["name"] for c in insp.get_columns("targets")}
        if "enforce_budget" not in target_cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE targets ADD COLUMN enforce_budget BOOLEAN DEFAULT false"))
        if "pr_urls" not in target_cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE targets ADD COLUMN pr_urls JSON DEFAULT '[]'"))

    # custom_skills.hidden
    if "custom_skills" in insp.get_table_names():
        skill_cols = {c["name"] for c in insp.get_columns("custom_skills")}
        if "hidden" not in skill_cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE custom_skills ADD COLUMN hidden BOOLEAN DEFAULT false"))

    # Stamp graph.concurrency on existing workflows where kind=='parallel' but
    # the graph doesn't already carry the field — keeps the new topology-from-
    # graph dispatcher correct for legacy rows.
    if "workflows" in insp.get_table_names():
        from .models import Workflow
        from sqlalchemy.orm import sessionmaker as _sm
        Sess = _sm(bind=engine, future=True, autocommit=False, autoflush=False)
        with Sess() as s:
            for w in s.query(Workflow).filter(Workflow.kind == "parallel").all():
                g = dict(w.graph or {})
                if "nodes" in g and g.get("concurrency") != "parallel":
                    g["concurrency"] = "parallel"
                    w.graph = g
            s.commit()

    # Wave-6: retro_score_summary on runs, status + created_in_run_id on target_lessons
    # Backfill source_slug for runs created before the column existed.
    # Heuristic: for runs where source_slug is NULL, try target_slug as agent slug
    # (works for runs where the executor fell back to agent_slug for target_slug).
    with engine.begin() as conn:
        try:
            conn.execute(text("""
                UPDATE runs
                SET source_slug = target_slug
                WHERE source_slug IS NULL
                  AND target_slug IN (SELECT slug FROM agents)
            """))
            conn.execute(text("""
                UPDATE runs
                SET source_slug = target_slug
                WHERE source_slug IS NULL
                  AND target_slug IN (SELECT slug FROM workflows)
            """))
        except Exception:
            pass  # non-fatal — new runs will have source_slug populated correctly
    with engine.begin() as conn:
        _ensure_column(conn, "runs", "retro_score_summary", "retro_score_summary JSON")
        if "target_lessons" in insp.get_table_names():
            _ensure_column(conn, "target_lessons", "status",
                           "status VARCHAR(32) NOT NULL DEFAULT 'active'")
            # Wave-6 L1: FK to the retro run that authored this lesson
            _ensure_column(conn, "target_lessons", "created_in_run_id",
                           "created_in_run_id VARCHAR")
        # GitHub Issues mirror columns
        _ensure_column(conn, "runs", "github_issue_number", "github_issue_number INTEGER")
        _ensure_column(conn, "runs", "github_issue_url", "github_issue_url TEXT")
        _ensure_column(conn, "runs", "session_id", "session_id VARCHAR")
        if "targets" in insp.get_table_names():
            _ensure_column(conn, "targets", "github_issue_number", "github_issue_number INTEGER")
            _ensure_column(conn, "targets", "github_issue_url", "github_issue_url TEXT")
        if "telegram_sessions" in insp.get_table_names():
            _ensure_column(conn, "telegram_sessions", "agent_slug_override",
                           "agent_slug_override VARCHAR")
    if "telegram_bots" in insp.get_table_names():
        with engine.begin() as conn:
            _ensure_column(conn, "telegram_bots", "is_sysadmin",
                           "is_sysadmin BOOLEAN NOT NULL DEFAULT FALSE")
    if "crispal_conversation_suggestions" in insp.get_table_names():
        with engine.begin() as conn:
            _ensure_column(conn, "crispal_conversation_suggestions", "history_message_ids",
                           "history_message_ids JSON DEFAULT '[]'")

    # Backfill cli_sessions from existing runs.session_id values
    if "cli_sessions" in insp.get_table_names() and "runs" in insp.get_table_names():
        with engine.begin() as conn:
            try:
                conn.execute(text("""
                    INSERT OR IGNORE INTO cli_sessions (id, session_id, name, description, created_at, updated_at)
                    SELECT
                        lower(hex(randomblob(16))),
                        session_id,
                        '',
                        '',
                        MIN(started_at),
                        MAX(started_at)
                    FROM runs
                    WHERE session_id IS NOT NULL AND session_id != ''
                    GROUP BY session_id
                """))
            except Exception:
                pass  # non-fatal — runs without session_id simply won't appear


def _backfill_lesson_evidence_runs(conn) -> None:
    """Idempotent backfill: for each TargetLesson that has evidence_run_ids JSON
    but zero entries in lesson_evidence_runs, insert a 'primary' row per run_id.
    Lessons that already have any join rows are skipped entirely (cheap re-run guard).
    Cross-database safe (SQLite + PostgreSQL)."""
    import json
    import uuid as _uuid_mod
    from datetime import datetime as _dt
    from sqlalchemy import text

    try:
        rows = conn.execute(text("""
            SELECT tl.id, tl.evidence_run_ids
            FROM target_lessons tl
            WHERE tl.evidence_run_ids IS NOT NULL
              AND tl.evidence_run_ids NOT IN ('[]', 'null', '')
              AND NOT EXISTS (
                  SELECT 1 FROM lesson_evidence_runs ler WHERE ler.lesson_id = tl.id
              )
        """)).fetchall()
    except Exception:
        # lesson_evidence_runs table may not exist in very old schemas — skip gracefully
        return

    dialect = conn.dialect.name
    now = _dt.utcnow().isoformat()
    for lesson_id, evidence_json in rows:
        try:
            run_ids = json.loads(evidence_json) if evidence_json else []
        except (json.JSONDecodeError, TypeError):
            continue
        for run_id in (run_ids or []):
            if not run_id:
                continue
            if dialect == "sqlite":
                insert_sql = text("""
                    INSERT OR IGNORE INTO lesson_evidence_runs
                    (id, lesson_id, run_id, role, created_at, updated_at)
                    VALUES (:id, :lesson_id, :run_id, 'primary', :ts, :ts2)
                """)
            else:
                insert_sql = text("""
                    INSERT INTO lesson_evidence_runs
                    (id, lesson_id, run_id, role, created_at, updated_at)
                    VALUES (:id, :lesson_id, :run_id, 'primary', :ts, :ts2)
                    ON CONFLICT DO NOTHING
                """)
            conn.execute(insert_sql, {
                "id": _uuid_mod.uuid4().hex,
                "lesson_id": lesson_id,
                "run_id": run_id,
                "ts": now,
                "ts2": now,
            })


def _seed_retro_score_weights() -> None:
    """Insert the default RetroScoreWeights singleton (id=1) if not present."""
    from .models import RetroScoreWeights
    _DEFAULT_WEIGHTS: dict[str, float] = {
        "accuracy": 0.25,
        "output_quality": 0.20,
        "lessons_applied": 0.15,
        "recovery": 0.10,
        "plan_adherence": 0.10,
        "cost": 0.05,
        "wall": 0.05,
        "mistakes": 0.05,
        "scope_discipline": 0.05,
    }
    assert abs(sum(_DEFAULT_WEIGHTS.values()) - 1.0) < 1e-9, \
        f"default weights must sum to 1.0, got {sum(_DEFAULT_WEIGHTS.values())}"
    s = SessionLocal()
    try:
        if not s.get(RetroScoreWeights, 1):
            s.add(RetroScoreWeights(id=1, weights_json=_DEFAULT_WEIGHTS))
            s.commit()
    finally:
        s.close()
