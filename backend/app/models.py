"""SQLAlchemy models."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.utcnow()


class Model(Base):
    __tablename__ = "models"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String, unique=True, index=True)
    provider: Mapped[str] = mapped_column(String, index=True)  # anthropic|openai|bedrock|cli|echo
    model_id: Mapped[str] = mapped_column(String)              # e.g. claude-sonnet-4-5, gpt-4o
    display_name: Mapped[str] = mapped_column(String)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    use_cases: Mapped[list[str]] = mapped_column(JSON, default=list)       # ["Greenfield product research", "Domain investigation"] — when to pick this agent
    model_slug: Mapped[str | None] = mapped_column(String, ForeignKey("models.slug"), nullable=True)
    tool_specs: Mapped[list[Any]] = mapped_column(JSON, default=list)      # ["code.read_file", ...]
    skill_slugs: Mapped[list[str]] = mapped_column(JSON, default=list)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)     # {temperature, max_tokens, ...}
    mcp_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict) # {servers: {name: {type, url, headers}}}
    extra_volumes: Mapped[list[str]] = mapped_column(JSON, default=list)   # ["host:container", ...] extra -v flags for docker run
    inherit_from: Mapped[str | None] = mapped_column(String, nullable=True)  # slug of parent agent to inherit system_prompt from
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    icon: Mapped[str] = mapped_column(String, default="bot")
    color: Mapped[str] = mapped_column(String, default="#58a6ff")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Workflow(Base):
    __tablename__ = "workflows"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    use_cases: Mapped[list[str]] = mapped_column(JSON, default=list)    # ["Greenfield product exploration", "Bug investigation"] — when to pick this workflow
    kind: Mapped[str] = mapped_column(String)  # sequential|parallel|orchestrator_worker|pipeline|group_chat
    graph: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)   # nodes/edges in react-flow form
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String)            # agent|workflow|playground|eval
    target_slug: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending|running|success|error|cancelled
    input: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Lineage / provenance
    parent_run_id: Mapped[str | None] = mapped_column(String, ForeignKey("runs.id"), nullable=True, index=True)
    initiator_kind: Mapped[str] = mapped_column(String, default="agent_run")  # agent_run|workflow_run|chat|eval|mcp|cli
    initiator_id: Mapped[str | None] = mapped_column(String, nullable=True)   # e.g. chat session id, eval id
    node_id: Mapped[str | None] = mapped_column(String, nullable=True)        # which workflow node spawned us
    model_slug: Mapped[str | None] = mapped_column(String, nullable=True)     # model actually used at runtime
    source_slug: Mapped[str | None] = mapped_column(String, nullable=True, index=True)  # slug of the agent or workflow that ran

    # Target — first-class umbrella linking a tree of runs to an overall goal.
    target_id: Mapped[str] = mapped_column(String, ForeignKey("targets.id"), nullable=False, index=True)

    retro_score_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    github_issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    github_issue_url: Mapped[str | None] = mapped_column(String, nullable=True)
    # CLI session ID (captured from system.init event; used for --resume on next run)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)

    events: Mapped[list["RunEvent"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    children: Mapped[list["Run"]] = relationship("Run",
        primaryjoin="Run.id == foreign(Run.parent_run_id)",
        viewonly=True)
    artefacts: Mapped[list["RunArtefact"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class Target(Base):
    """A Target is the *why* of an orchestration — the overall goal a tree of
    runs is delivering against. Every Run can be FK-linked via ``runs.target_id``.

    Used for retros: from one Target you can see every Run, total cost, total
    tokens, total wall, status, and the canvases (plan + report) tied to it.
    """
    __tablename__ = "targets"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    source_kind: Mapped[str] = mapped_column(String, default="manual")  # manual|rally_story|incident|jira|github_issue|...
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)  # "US1924311" / PD-incident-id / URL
    plan_canvas_id: Mapped[str | None] = mapped_column(String, nullable=True)
    report_canvas_id: Mapped[str | None] = mapped_column(String, nullable=True)
    budget_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Soft budget = advisory only (default). Hard budget = cancel descendant runs
    # once total tokens or cost exceeds the cap.
    enforce_budget: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String, default="active")  # active|completed|cancelled|abandoned
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    # First-class PR linkage — each delivery may produce 1+ PRs (per-repo, stacked, retry).
    # Each entry: {url, title?, status?: open|merged|closed, ci_status?: passing|failing|pending}.
    pr_urls: Mapped[list[dict]] = mapped_column(JSON, default=list)
    github_issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    github_issue_url: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class TargetLesson(Base):
    """A lesson learned from a Target delivery. Produced by the `retro` agent
    (or any analyzer) by walking a Target's run tree + events + artefacts +
    cross-agent discussions, then deduping against prior lessons.

    Lessons are first-class so future deliveries can search them by tag
    (task_category, domain, tool) before any work begins — the platform gets
    smarter every Target.

    Categories (free-form, but conventions encouraged):
      time-saver         — pattern that would speed up similar work
      pitfall            — dead-end to avoid
      tooling-gap        — agent wanted to do X but couldn't
      pattern-that-worked — confirm-good practice
      prompt-fix         — initial prompt was unclear; refined version
      cost-trap          — overpaid by N (model swap / cancel-burn / re-dispatch)
      scope-creep        — task grew during execution; bake the gap into initial spec
    """
    __tablename__ = "target_lessons"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    target_id: Mapped[str] = mapped_column(String, ForeignKey("targets.id"), index=True)
    category: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text, default="")          # full markdown body
    evidence_run_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    confidence: Mapped[str] = mapped_column(String, default="medium")  # low|medium|high
    applicable_tags: Mapped[list[str]] = mapped_column(JSON, default=list)  # e.g. ["cat-2","acsb","cookiecutter","nrql"]
    source: Mapped[str] = mapped_column(String, default="retro")    # retro|manual|cross-agent
    superseded_by: Mapped[str | None] = mapped_column(String, nullable=True)  # id of newer lesson that replaces this
    status: Mapped[str] = mapped_column(String, default='active', index=True)  # pending_review|active|archived
    # FK to the retro run that authored this lesson (distinct from evidence runs it is *about*)
    created_in_run_id: Mapped[str | None] = mapped_column(String, ForeignKey("runs.id"), nullable=True, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    evidence_runs: Mapped[list["LessonEvidenceRun"]] = relationship(
        back_populates="lesson", cascade="all, delete-orphan",
    )

    @property
    def linked_runs(self) -> list[dict]:
        result = []
        for er in sorted(self.evidence_runs or [], key=lambda x: x.created_at, reverse=True):
            run = er.run
            result.append({
                "run_id": er.run_id,
                "role": er.role,
                "status": run.status if run else None,
                "kind": run.kind if run else None,
            })
        return result


class LessonApplication(Base):
    """A record that a lesson was surfaced to (or considered by) a specific
    delivery. Used to compute lesson effectiveness over time.

    Outcome semantics:
      retrieved   — lesson was returned by `search_lessons`; PM may or may not have read it
      shown_to_pm — lesson was explicitly passed to project-manager at Phase 1.5
      applied     — PM/agent baked the lesson into the decomposition / execution
      rejected    — PM acknowledged but explicitly rejected (with reason)
      prevented   — retro confirms the lesson prevented an issue it warned about
      ignored     — lesson was applicable but not applied; retro flags this as a propagation gap
      partial     — lesson applied imperfectly; retro suggests refinement
    """
    __tablename__ = "lesson_applications"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    lesson_id: Mapped[str] = mapped_column(String, ForeignKey("target_lessons.id"), index=True)
    target_id: Mapped[str] = mapped_column(String, ForeignKey("targets.id"), index=True)
    applied_in_run_id: Mapped[str | None] = mapped_column(String, ForeignKey("runs.id"),
                                                          nullable=True, index=True)
    outcome: Mapped[str] = mapped_column(String, default="retrieved")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class LessonEvidenceRun(Base):
    """Join table: which runs are linked to a lesson, and in what role.

    Roles:
      primary          — a run whose output directly motivated this lesson
      consolidated_from — a run folded into this lesson during de-dup (L2)
      evidence         — supporting run, not the primary motivator
    """
    __tablename__ = "lesson_evidence_runs"
    __table_args__ = (
        UniqueConstraint("lesson_id", "run_id", "role", name="uq_lesson_run_role"),
        Index("ix_lesson_evidence_runs_lesson_id", "lesson_id"),
        Index("ix_lesson_evidence_runs_run_id", "run_id"),
        Index("ix_lesson_evidence_runs_role", "role"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    lesson_id: Mapped[str] = mapped_column(
        String, ForeignKey("target_lessons.id", ondelete="CASCADE"), nullable=False,
    )
    run_id: Mapped[str] = mapped_column(String, ForeignKey("runs.id"), nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    lesson: Mapped["TargetLesson"] = relationship(back_populates="evidence_runs")
    run: Mapped["Run"] = relationship()


class RunArtefact(Base):
    """A structured artefact attached to a Run — e.g. NRQL output, terraform
    plan, JSON config, a diff. Lets agents emit named files instead of
    cramming everything into ``runs.output.final`` as one giant string.
    """
    __tablename__ = "run_artefacts"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("runs.id"), index=True)
    name: Mapped[str] = mapped_column(String)            # e.g. "nrql-baselines.md", "plan.txt"
    mime: Mapped[str] = mapped_column(String, default="text/plain")  # text/plain|text/markdown|application/json|image/png|...
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha: Mapped[str | None] = mapped_column(String, nullable=True)   # content sha256 (optional, integrity)
    content: Mapped[str] = mapped_column(Text, default="")           # stored inline; b64 for binary
    is_binary: Mapped[bool] = mapped_column(Boolean, default=False)  # content is b64-encoded bytes
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    run: Mapped["Run"] = relationship(back_populates="artefacts")


class RunEvent(Base):
    __tablename__ = "run_events"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("runs.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now)
    kind: Mapped[str] = mapped_column(String)   # node_start|node_end|llm_token|tool_call|tool_result|error|log
    node_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    run: Mapped["Run"] = relationship(back_populates="events")


class Eval(Base):
    __tablename__ = "evals"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    target_kind: Mapped[str] = mapped_column(String)   # agent|workflow
    target_slug: Mapped[str] = mapped_column(String)
    dataset: Mapped[list[Any]] = mapped_column(JSON, default=list)
    metric: Mapped[str] = mapped_column(String)         # judge_llm|assert_contains|cmd_returns_zero|tool_sequence_match
    metric_args: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class EvalRun(Base):
    __tablename__ = "eval_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    eval_slug: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    cases: Mapped[list[Any]] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class McpServer(Base):
    __tablename__ = "mcp_servers"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, unique=True, index=True)
    command: Mapped[str] = mapped_column(String)
    args: Mapped[list[str]] = mapped_column(JSON, default=list)
    env: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String, default="file")  # file|manual
    discovered_tools: Mapped[list[Any]] = mapped_column(JSON, default=list)
    last_refreshed: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CustomSkill(Base):
    """User-created or user-overridden skills stored in DB.

    Three roles a row can play:
      * source="custom"          — pure user skill (no file equivalent)
      * source="override"        — same slug as a file skill, but content overrides
      * hidden=True              — tombstone that hides a file skill from listings
    """
    __tablename__ = "custom_skills"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text, default="")    # the SKILL.md body
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class RetroScore(Base):
    """Per-dimension quality score for a single Run. Multiple scores may exist
    per (run_id, dimension) — e.g. auto then human override. ``superseded_by``
    chains the history so only the latest is active."""
    __tablename__ = "retro_scores"
    __table_args__ = (
        Index("ix_retro_scores_run_dim_src", "run_id", "dimension", "source"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("runs.id"), index=True)
    dimension: Mapped[str] = mapped_column(String, index=True)  # cost|wall|mistakes|lessons_applied|plan_adherence|scope_discipline|accuracy|output_quality|recovery|overall
    score: Mapped[int] = mapped_column(Integer)                  # 1..10
    source: Mapped[str] = mapped_column(String)                  # auto|retro_agent|human
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String, ForeignKey("retro_scores.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class RetroScoreWeights(Base):
    """Singleton (id=1) holding the dimension→weight map used by the scorer.
    Seeded by init_db; update via the settings API in chunk A2."""
    __tablename__ = "retro_score_weights"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    weights_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class TelegramBot(Base):
    """A Telegram bot whose inbound messages are dispatched to an AP agent."""
    __tablename__ = "telegram_bots"
    id: Mapped[str] = mapped_column(String, primary_key=True)           # e.g. "aw-17"
    name: Mapped[str] = mapped_column(String, default="")
    token: Mapped[str] = mapped_column(String)                          # Telegram Bot API token
    webhook_secret: Mapped[str] = mapped_column(String, default="")     # HMAC secret set on Telegram webhook
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    agent_slug: Mapped[str | None] = mapped_column(String, nullable=True)  # AP agent to dispatch to
    admin_user_ids: Mapped[list[str]] = mapped_column(JSON, default=list)  # allowed Telegram user IDs
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    sessions: Mapped[list["TelegramSession"]] = relationship(
        back_populates="bot", cascade="all, delete-orphan"
    )


class TelegramSession(Base):
    """Tracks the last Claude session_id per (bot, chat) for conversation continuity."""
    __tablename__ = "telegram_sessions"
    __table_args__ = (UniqueConstraint("bot_id", "chat_id", name="uq_tg_session_bot_chat"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    bot_id: Mapped[str] = mapped_column(String, ForeignKey("telegram_bots.id"), index=True)
    chat_id: Mapped[str] = mapped_column(String, index=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)  # claude --resume id
    target_id: Mapped[str | None] = mapped_column(String, nullable=True)   # AP Target.id
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    bot: Mapped["TelegramBot"] = relationship(back_populates="sessions")
