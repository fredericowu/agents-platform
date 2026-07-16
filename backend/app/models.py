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
    permissions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {"docker": true, "github": true, "share_network": false}  — share_network defaults True unless explicitly false
    inherit_from: Mapped[str | None] = mapped_column(String, nullable=True)  # slug of parent agent to inherit system_prompt from
    agent_config_slug: Mapped[str | None] = mapped_column(String, nullable=True)  # slug of an AgentConfig — when set, its permissions/extra_volumes/mcp_config win over the columns above
    group_slug: Mapped[str | None] = mapped_column(String, nullable=True)  # slug of an AgentGroup — when set, AgentGroup.instructions is prepended to this agent's system_prompt at run time
    kanban_target_status: Mapped[str | None] = mapped_column(String, nullable=True)  # logical Kanban status key (e.g. "ready_to_deploy") this agent moves its card to on completion — overrides AgentGroup.kanban_target_status when set
    capabilities: Mapped[str] = mapped_column(Text, default="")  # short (<=100 words) plain-English summary of what this agent can do — read by other agents deciding who to hand a task off to; overrides AgentGroup.capabilities when set
    hidden_from_flow: Mapped[bool] = mapped_column(Boolean, default=False)  # excluded from the Agents Flow connected-agents context list (direct or via group expansion) — NOT enforced, still callable by slug
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    icon: Mapped[str] = mapped_column(String, default="bot")
    color: Mapped[str] = mapped_column(String, default="#58a6ff")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class AgentConfig(Base):
    """Reusable bundle of Permissions + Extra volumes + MCP servers. Agents pick
    one via ``Agent.agent_config_slug`` instead of duplicating this config inline."""
    __tablename__ = "agent_configs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    mcp_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    extra_volumes: Mapped[list[str]] = mapped_column(JSON, default=list)
    permissions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Per-agent override of the global "Auto-compact threshold (tokens)" setting
    # (api/settings.py DEFAULT_AUTO_COMPACT_THRESHOLD_TOKENS). NULL inherits the
    # global value; 0 disables auto-compact for agents using this config.
    auto_compact_threshold_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class AgentGroup(Base):
    """A named cluster of agents (e.g. "Coders") sharing a common set of
    instructions. At run time, ``AgentGroup.instructions`` is prepended to
    the system_prompt of any Agent with a matching ``group_slug`` — lets
    several models (fable/haiku/opus/sonnet/...) share one prompt without
    duplicating it per agent."""
    __tablename__ = "agent_groups"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    instructions: Mapped[str] = mapped_column(Text, default="")
    kanban_target_status: Mapped[str | None] = mapped_column(String, nullable=True)  # default logical Kanban status key member agents move their card to on completion — Agent.kanban_target_status overrides this per-agent
    capabilities: Mapped[str] = mapped_column(Text, default="")  # short (<=100 words) plain-English summary of what member agents can do — Agent.capabilities overrides this per-agent
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


class AgentFlow(Base):
    """A capability/topology graph: which agents may hand off to which other
    agents to complete a task, starting from a "source" node (the inbound
    channel — watch, glasses, iOS app, Notion kanban, Telegram, ...).

    Unlike Workflow.graph (an *execution* DAG the executor runs), this graph
    is descriptive only — agents read it (via their instructions) to decide
    who to call next. Not tied to Notion; may be used alongside it.

    ``enabled`` gates runtime behavior: when True, any agent that appears as
    a node in this graph gets the aw-agents-flow skill auto-injected into
    its system prompt at dispatch time, followed by the list of agents
    directly connected to it in THIS graph (see
    core/executor.py::_agents_flow_context). Still not enforced — the agent
    can call anyone, the connected-agents list is just guidance."""
    __tablename__ = "agent_flows"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    graph: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)   # {nodes:[{id,type:"source"|"agent",agent_slug?,label,position}], edges:[{id,source,target}]}
    # Per-flow overrides for the Agents Flow safety net (core/executor.py::
    # _check_flow_completion). All nullable — null means "fall back to the
    # global agent_chain_max_hops setting" (max_hops) or "no cap" (the two
    # budgets). Checked against the rolled-up totals across every run
    # sharing a flow_run_id (Run.flow_run_id); hitting either escalates the
    # flow to Need Human instead of continuing, same destination as the
    # hop-count guard.
    max_hops: Mapped[int | None] = mapped_column(Integer, nullable=True)
    budget_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class AgentFlowRun(Base):
    """One row per hop (round) inside a live Agents Flow execution — not to
    be confused with AgentFlow (the topology graph definition). ``flow_run_id``
    is generated once, on the root hop (hop_index=0), and shared by every
    subsequent hop in the same chain (inherited via Run.parent_run_id ->
    Run.flow_run_id, see core/executor.py::_record_flow_hop). Denormalized
    onto Run.flow_run_id/Run.flow_slug too so the Runs list UI never needs to
    join here — this table exists for the full round-by-round history."""
    __tablename__ = "agent_flow_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    flow_run_id: Mapped[str] = mapped_column(String, index=True)
    flow_slug: Mapped[str] = mapped_column(String, index=True)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("runs.id"), index=True)
    agent_slug: Mapped[str] = mapped_column(String)
    hop_index: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class FlowWaiter(Base):
    """Flow-level 'call me back' — distinct from the per-hop
    Run.call_me_back/callback_origin_run_id (core.wakeups.register_agent_callback),
    which only resumes the ONE run that dispatched the ONE child it's watching.
    In a multi-hop chain (Telegram -> Architect -> Product Owner) that hop-level
    callback resolves as soon as Architect's own turn ends — long before
    mark_flow_done is eventually called, possibly by a session several hops
    deeper that Architect's original caller (Telegram) has no direct link to
    anymore. One row per flow instance (``flow_run_id``, shared by every hop —
    see Run.flow_run_id / core/executor.py::_record_flow_hop): first-writer-wins,
    set to whichever run first asked for a callback anywhere in the flow (see
    core.wakeups._register_flow_waiter). core.wakeups.mark_flow_done /
    mark_flow_planned resolve this to resume that run once, regardless of how
    many hops or intermediate agent-level callbacks happened in between."""
    __tablename__ = "flow_waiters"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    flow_run_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    origin_run_id: Mapped[str] = mapped_column(String, index=True)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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
    # Telegram "Processing…" progress-bubble message id (per-chat). Persisted so
    # restart recovery can flip its inline button [processing]→[done] after
    # re-attaching the run — a live dispatch flips it in its finally block, but a
    # restart kills that thread before it runs, leaving the bubble stuck.
    proc_msg_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Agent-to-agent "call me back" (core.wakeups.register_agent_callback):
    # set on THIS run (the dispatched child) when whoever called it via
    # run_agent_async/run_workflow_async asked to be notified on completion
    # (call_me_back, default true). callback_origin_run_id points back at the
    # caller's own run; callback_done flips true once the callback has been
    # attempted (success or failure) so a restart's rearm doesn't refire it.
    call_me_back: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    callback_done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    callback_origin_run_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    # Notion Kanban card this run was dispatched for (set at creation time so it
    # survives a restart-recovery reattach — _notify_kanban_run_done reads it
    # back from this row instead of relying on the in-flight kwarg, which dies
    # with the process that made the original dispatch call).
    notion_task_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    # Agent-to-agent chain depth (loop guard): 0 for a human/root-initiated run,
    # else caller's hop_count + 1. Set at dispatch time in api/agents.py and
    # api/workflows.py from the RunInput.caller_run_id the calling agent's own
    # AW_RUN_ID (auto-forwarded by mcp_server/agent_mcp.py). Checked against the
    # `agent_chain_max_hops` setting to abort runaway A→B→A call chains.
    hop_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Agents Flow safety net (core/executor.py::_took_flow_action /
    # _agents_flow_context). return_to_caller_done is set by
    # core.wakeups.return_to_caller on ITS OWN run's row when called (even a
    # no-op counts — it's still a deliberate decision). is_flow_reprompt marks
    # a run that was fired BY the "lost agent" safety net (not a normal
    # dispatch) — used to count how many times a session has been reprompted
    # before escalating to Need Human.
    return_to_caller_done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_flow_reprompt: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Denormalized from AgentFlowRun (see that model) so the Runs list UI can
    # show/group/color by flow without a join. Set once at Run-creation time
    # by core/executor.py::_record_flow_hop; null for runs outside any flow.
    flow_run_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    flow_slug: Mapped[str | None] = mapped_column(String, nullable=True)
    # Set on every run sharing this flow_run_id once _escalate_need_human
    # fires for the flow (hop-count exceeded or the "lost agent" reprompt
    # gave up) — a persistent "this flow needed a human at some point" mark,
    # not just the one hop that triggered it. Drives the yellow border on
    # the Flow chip in the Runs UI. Propagated onto new hops the same way
    # flow_run_id/flow_slug are (see core/executor.py::_record_flow_hop /
    # _inherit_flow_from_session) so it survives a human resuming the flow.
    flow_needs_human: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Set by core.wakeups.mark_flow_done when the agent calls the
    # mark_flow_done MCP tool — the explicit "I'm done" exit distinct from
    # handing off (①) or returning to caller (②). Also moves the Kanban card
    # to done when notion_task_id is set.
    marked_flow_done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Structured verdict fields on the two "free text" terminal actions — the
    # agent's actual DECISION lives in these enum-ish columns (validated by
    # the MCP tool's inputSchema, so a caller can check the *tool call*
    # rather than parse prose), while message/summary remains free text for
    # the human-readable detail. flow_outcome: "success" | "failed" |
    # "partial" (mark_flow_done). return_kind: "result" | "question" |
    # "blocker" (return_to_caller_agent). Both nullable — null for runs that
    # predate this column or never took that action.
    flow_outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    return_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    # mark_flow_done's QA accountability fields (core.wakeups.mark_flow_done) —
    # exactly one of the two must be set, enforced server-side: either
    # qa_run_id points at the Run.id of the QA agent run that reviewed this
    # work, or qa_not_needed=True is the agent's explicit declaration that no
    # QA pass applies here. Prevents a card sliding to done with neither a QA
    # trail nor an explicit "QA doesn't apply" call.
    qa_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    qa_not_needed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Effective system prompt actually sent to the LLM for this run (instructions
    # + skills + Agents Flow context, joined) — persisted at dispatch time so it's
    # inspectable afterwards (e.g. in the Run Detail UI), since it's otherwise
    # only held in-memory by the one-shot CLI process. None for runs predating
    # this column or where composition failed.
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)

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


class ScheduledWakeup(Base):
    """A ``ScheduleWakeup`` tool call captured from a CLI run's event stream.

    The claude-cli harness would hold the timer and re-invoke the model, but in
    AP each run is a one-shot ``claude -p`` process — the timer dies with it.
    We persist the request here and fire a follow-up run on the same session
    when it comes due (see ``core.wakeups``)."""
    __tablename__ = "scheduled_wakeups"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    origin_run_id: Mapped[str] = mapped_column(String, index=True)
    agent_slug: Mapped[str] = mapped_column(String)
    target_id: Mapped[str | None] = mapped_column(String, nullable=True)
    session_id: Mapped[str] = mapped_column(String, index=True)
    initiator_id: Mapped[str] = mapped_column(String)                # telegram: "{bot_id}:{chat_id}" · watch/meta: device session id
    channel: Mapped[str] = mapped_column(String, default="telegram") # telegram|watch — which delivery path ships the reply
    prompt: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    fire_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)  # pending|firing|fired|error
    fired_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


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
    is_sysadmin: Mapped[bool] = mapped_column(Boolean, default=False)      # receives system approval prompts
    agent_slug: Mapped[str | None] = mapped_column(String, nullable=True)  # AP agent to dispatch to
    admin_user_ids: Mapped[list[str]] = mapped_column(JSON, default=list)  # allowed Telegram user IDs
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    sessions: Mapped[list["TelegramSession"]] = relationship(
        back_populates="bot", cascade="all, delete-orphan"
    )


class TelegramInboundMessage(Base):
    """Durable record of an inbound Telegram message, written BEFORE the
    webhook handler acks Telegram with 200.

    The actual dispatch queue (telegram.py's per-chat ``_CHAT_QUEUES``) is a
    plain in-memory ``queue.Queue`` — it does not survive an agents-platform
    restart. Previously, a message that was already queued (Telegram got its
    200 OK) but not yet drained when the process restarted was lost forever:
    Telegram never retries a webhook call it believes succeeded. This table
    is the durability layer — on startup, any row still ``pending`` gets
    re-enqueued (see telegram.recover_pending_telegram_messages).
    """
    __tablename__ = "telegram_inbound_messages"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    bot_id: Mapped[str] = mapped_column(String, index=True)
    chat_id: Mapped[str] = mapped_column(String, index=True)
    user_id: Mapped[str] = mapped_column(String, default="")
    text: Mapped[str] = mapped_column(Text, default="")
    is_voice: Mapped[bool] = mapped_column(Boolean, default=False)
    inbound_lang: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="pending", index=True)  # pending | dispatched
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CliSession(Base):
    """Named CLI session — tracks a claude --resume session_id with a human-friendly name."""
    __tablename__ = "cli_sessions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String, unique=True, index=True)  # claude CLI session ID
    name: Mapped[str] = mapped_column(String, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class PendingSessionCommand(Base):
    """A /clear or /compact queued via the clear_session/compact_session MCP
    tools — applied to ``session_id`` right before its next resumed turn
    (see executor.run_agent), then deleted. One row per session at a time."""
    __tablename__ = "pending_session_commands"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    command: Mapped[str] = mapped_column(String)  # "clear" | "compact"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class TelegramSession(Base):
    """Tracks the last Claude session_id per (bot, chat) for conversation continuity."""
    __tablename__ = "telegram_sessions"
    __table_args__ = (UniqueConstraint("bot_id", "chat_id", name="uq_tg_session_bot_chat"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    bot_id: Mapped[str] = mapped_column(String, ForeignKey("telegram_bots.id"), index=True)
    chat_id: Mapped[str] = mapped_column(String, index=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)  # claude --resume id
    target_id: Mapped[str | None] = mapped_column(String, nullable=True)   # AP Target.id
    agent_slug_override: Mapped[str | None] = mapped_column(String, nullable=True)  # per-chat agent override
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    bot: Mapped["TelegramBot"] = relationship(back_populates="sessions")


class CrispalConversationSuggestion(Base):
    """A drafted reply for a Crispal social-media conversation, pending human
    approval via Telegram Action buttons (crispal_suggest:send/ignore/edit —
    see backend/app/api/telegram.py). Sending is a deterministic backend
    action triggered by the button tap, never an LLM decision — this row is
    the single source of truth for what was suggested vs. what actually went
    out, so it doubles as a traceability log for future prompt/skill tuning."""
    __tablename__ = "crispal_conversation_suggestions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    source: Mapped[str] = mapped_column(String)          # facebook | instagram | ...
    conversation_id: Mapped[str] = mapped_column(String, index=True)
    customer_id: Mapped[str] = mapped_column(String)     # recipient_id for social_send_message
    customer_name: Mapped[str] = mapped_column(String, default="")
    message_type: Mapped[str] = mapped_column(String, default="response")
    suggested_text: Mapped[str] = mapped_column(Text)
    final_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending|sent|ignored|edited|stale
    bot_id: Mapped[str] = mapped_column(String)
    chat_id: Mapped[str] = mapped_column(String)
    approval_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # message_ids of the context bubbles sent by _send_history (text + photos),
    # in send order — needed so a stale suggestion can be fully cleaned up from
    # Telegram (see crispal_watch_check.py's cleanup step), not just its
    # approval_message_id.
    history_message_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class HumanQuestion(Base):
    """A question an agent couldn't resolve on its own, sent to the human via
    the sysadmin Telegram bot as a clickable link (mini-app: question text +
    a free-text answer box), independent of whether the run carries a Kanban
    card. Answering resumes the SAME agent session with the answer as the
    next prompt (see api/telegram.py's question-answer route calling
    core.executor.run_agent with session_id=session_id) — this is the
    ask_human MCP tool's backing store."""
    __tablename__ = "human_questions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    token: Mapped[str] = mapped_column(String, unique=True, index=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String, index=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    agent_slug: Mapped[str] = mapped_column(String)
    target_id: Mapped[str | None] = mapped_column(String, nullable=True)
    notion_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending|answered
    bot_id: Mapped[str | None] = mapped_column(String, nullable=True)
    chat_id: Mapped[str | None] = mapped_column(String, nullable=True)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CrispalWhatsappMessage(Base):
    """One WhatsApp Cloud API message for the Crispal store, in or out.

    Unlike Facebook/Instagram (Graph API supports pulling full conversation
    history on demand), WhatsApp Cloud API only pushes messages via webhook —
    there is no "list conversations" endpoint. This table is therefore the
    only source of conversation history, populated by the webhook receiver
    (backend/app/api/whatsapp.py) for inbound messages and by the
    whatsapp_send_message MCP tool for outbound ones."""
    __tablename__ = "crispal_whatsapp_messages"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    wa_message_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    direction: Mapped[str] = mapped_column(String)          # in | out
    from_number: Mapped[str] = mapped_column(String, index=True)  # customer's E.164 number either way
    contact_name: Mapped[str] = mapped_column(String, default="")
    text: Mapped[str] = mapped_column(Text, default="")
    media_url: Mapped[str | None] = mapped_column(String, nullable=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class CrispalSuggestionFeedback(Base):
    """Human explanation of *why* a suggested reply was edited and what the
    correct behavior would have been — captured alongside the edit itself
    (edit mini-app's "Instrução de Comportamento" box), separate from
    CrispalConversationSuggestion so future prompt/skill tuning can query
    just the feedback corpus without scanning every suggestion row."""
    __tablename__ = "crispal_suggestion_feedback"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    suggestion_id: Mapped[str] = mapped_column(String, ForeignKey("crispal_conversation_suggestions.id"), index=True)
    instruction_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class GalleryToken(Base):
    """A share-link token minted by the `/images` Telegram command — lets an
    admin upload photos through the gallery webapp without those images ever
    entering the LLM's context window."""
    __tablename__ = "gallery_tokens"
    token: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    bot_slug: Mapped[str] = mapped_column(String, index=True)
    origin_chat_id: Mapped[str] = mapped_column(String)
    created_by: Mapped[str] = mapped_column(String)  # Telegram user id of the admin who minted it
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class GalleryBlock(Base):
    """One upload action (one HTTP multipart POST) = one block. Scoping unit
    for `list_gallery_images` — spans all tokens for a bot, not just the
    active one (rotating the link must not orphan earlier blocks)."""
    __tablename__ = "gallery_blocks"
    __table_args__ = (
        Index("ix_gallery_blocks_bot_slug", "bot_slug"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    # Nullable: blocks created by a tool (e.g. Arvin) rather than a share-link
    # upload have no token/origin_chat_id to point at — see `source` below.
    token: Mapped[str | None] = mapped_column(String, ForeignKey("gallery_tokens.token"), index=True, nullable=True)
    bot_slug: Mapped[str] = mapped_column(String)
    origin_chat_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # "upload" (default, via the /images share link) or "arvin" (auto-filed
    # after an Arvin generation job finishes) — lets the gallery UI show
    # separate folders/tabs without a second table.
    source: Mapped[str] = mapped_column(String, default="upload")
    image_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    images: Mapped[list["GalleryImage"]] = relationship(
        back_populates="block", cascade="all, delete-orphan"
    )


class GalleryImage(Base):
    """One uploaded file. `file_path` is the absolute, on-disk path — the
    same input `arvin`/`crispal_image_search` already accept, so intake
    stays a flat file-path handoff."""
    __tablename__ = "gallery_images"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    block_id: Mapped[str] = mapped_column(
        String, ForeignKey("gallery_blocks.id", ondelete="CASCADE"), index=True
    )
    file_path: Mapped[str] = mapped_column(Text)
    original_name: Mapped[str] = mapped_column(String)
    mime: Mapped[str] = mapped_column(String, default="")
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    block: Mapped["GalleryBlock"] = relationship(back_populates="images")
    image_tags: Mapped[list["GalleryImageTag"]] = relationship(
        back_populates="image", cascade="all, delete-orphan"
    )


def _normalize_tag(raw: str) -> str:
    """Case-insensitive, whitespace-collapsed tag vocabulary key — shared
    business rule (not UI convention) enforced by GalleryTag's unique
    constraint so "Inverno"/"inverno"/" INVERNO " collapse to one row."""
    return " ".join(raw.strip().casefold().split())


class GalleryTag(Base):
    """A tag in a bot's gallery vocabulary. First-class entity (not a loose
    string on GalleryImage) so autocomplete and the agent's free-text tag
    queries share one normalized vocabulary."""
    __tablename__ = "gallery_tags"
    __table_args__ = (
        UniqueConstraint("bot_slug", "normalized_name", name="uq_gallery_tag_bot_norm"),
        Index("ix_gallery_tags_bot_slug", "bot_slug"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    bot_slug: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)             # display form, first-typed casing wins
    normalized_name: Mapped[str] = mapped_column(String)  # _normalize_tag(name)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class GalleryImageTag(Base):
    """Join row linking one GalleryImage to one GalleryTag."""
    __tablename__ = "gallery_image_tags"
    __table_args__ = (
        UniqueConstraint("image_id", "tag_id", name="uq_gallery_image_tag"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    image_id: Mapped[str] = mapped_column(
        String, ForeignKey("gallery_images.id", ondelete="CASCADE"), index=True
    )
    tag_id: Mapped[str] = mapped_column(
        String, ForeignKey("gallery_tags.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    image: Mapped["GalleryImage"] = relationship(back_populates="image_tags")
