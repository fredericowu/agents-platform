"""Run an agent or a workflow. Emits events to the bus and updates Run rows.

Lineage:
  * Every Run row carries ``parent_run_id`` (NULL = root) and ``initiator_kind``
    (``agent_run``, ``workflow_run``, ``chat``, ``eval``, ``mcp``, ``cli``).
  * Workflow children are real Run rows whose parent is the workflow's run.
  * Events on a child node are also published on the *parent*'s event channel
    so a single SSE subscription gives the UI the whole tree.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time as _time
from datetime import datetime, timedelta
from typing import Any, Callable

_exec_log = logging.getLogger("ap.executor")

from sqlalchemy.orm import Session

from ..db import session_scope
from ..models import Agent, AgentGroup, Run, Workflow
from .retro_scorer import score_run_terminal
from .agent_loop import _provider_supports_langchain, run_langchain_agent
from .cancel import Cancelled, is_cancelled
from .events import bus
from . import hops
from .models import make_llm
from .orchestrators import dispatch as dispatch_workflow
from .tools.langchain_tools import tools_for_agent

# Every docker agent runs with cwd pinned to /opt/agentic-workspace (see
# _agent_to_runtime's params["cwd"] = _aw_base below), so the claude CLI's
# --resume session store is always this one shared project dir.
_CLAUDE_PROJECTS_DIR = os.path.expanduser(
    "~/.claude/projects/-opt-agentic-workspace"
)
# Published Anthropic per-million-token rates for the Sonnet tier used here
# (claude-cli-sonnet / claude-sonnet-5) — $/MTok.
_SONNET_PRICE_INPUT = 3.00
_SONNET_PRICE_OUTPUT = 15.00
_SONNET_PRICE_CACHE_WRITE = 3.75
_SONNET_PRICE_CACHE_READ = 0.30

# Sessions whose auto-compact ran but did NOT bring the token total back under
# the threshold — mapped to a monotonic deadline before which we won't try
# again. Prevents a compact storm (one full-context turn billed per message)
# when compaction is broken or ineffective for a session.
_AUTO_COMPACT_COOLDOWN: dict[str, float] = {}
_AUTO_COMPACT_COOLDOWN_S = 1800

# ---------------- per-session serialization ----------------
# Two runs resuming the same CLI session id concurrently corrupt/fork the
# session transcript. The Telegram dispatcher already serializes per
# (bot, chat), but other entry points resume the same session ids without
# any coordination: /run_sync (Meta Glasses / Watch), the internal
# telegram inject endpoint (telegram_system), restart-recovery re-attach,
# and openai-compat. Every one of them funnels through run_agent, so the
# rule lives here: same session_id → strictly one run at a time, FIFO in
# arrival order (asyncio.Lock wakes waiters in acquire order).
#
# Re-entrant per asyncio task: the nested auto-compact "/compact" run is
# awaited inside the parent run with the same session_id and must not
# deadlock against its own parent's lock.
_SESSION_LOCKS: dict[str, asyncio.Lock] = {}
_SESSION_LOCK_OWNER: dict[str, "asyncio.Task"] = {}
_SESSION_LOCK_REFS: dict[str, int] = {}
# Safety valve: if a run hangs holding the lock, don't wedge the session's
# queue forever — after this long, proceed unserialized with a loud warning.
_SESSION_LOCK_MAX_WAIT_S = 1800


def _recover_cost_from_transcript(
    session_id: str, window_start: datetime, window_end: datetime
) -> float | None:
    """Fallback for a run whose cost_usd stayed 0.0 because claude-cli's own
    "result" event (which carries total_cost_usd) never arrived — the usual
    cause is the docker process getting killed mid-turn by an awserv/
    agents-platform restart, before the CLI printed its final summary. The
    session's transcript file on disk still has the real per-message usage
    breakdown regardless (the CLI persists it incrementally, independent of
    whatever our own stream capture managed to see), so recompute cost from
    the last usage block in the run's time window instead of leaving it at 0.

    Returns None (never raises) if the transcript is missing or has no usage
    data in range — the caller just keeps whatever cost it already had.
    """
    path = os.path.join(_CLAUDE_PROJECTS_DIR, f"{session_id}.jsonl")
    if not os.path.exists(path):
        return None
    # A little slack for clock skew between our own started_at/ended_at
    # bookkeeping and the CLI process's own event timestamps.
    lo = window_start - timedelta(seconds=5)
    hi = window_end + timedelta(seconds=15)
    last_usage: dict | None = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_raw = obj.get("timestamp")
                if not ts_raw:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).replace(tzinfo=None)
                except ValueError:
                    continue
                if not (lo <= ts <= hi):
                    continue
                usage = (obj.get("message") or {}).get("usage")
                if usage:
                    last_usage = usage
    except OSError:
        return None
    if not last_usage:
        return None
    cost = (
        last_usage.get("input_tokens", 0) * _SONNET_PRICE_INPUT
        + last_usage.get("output_tokens", 0) * _SONNET_PRICE_OUTPUT
        + last_usage.get("cache_creation_input_tokens", 0) * _SONNET_PRICE_CACHE_WRITE
        + last_usage.get("cache_read_input_tokens", 0) * _SONNET_PRICE_CACHE_READ
    ) / 1_000_000
    return cost


def _current_session_token_total(session_id: str) -> int | None:
    """Context size (input + cache_read + cache_creation) as of the LAST
    recorded turn in this session's transcript — used by the auto-compact
    check to decide whether the session has grown past the configured
    threshold before processing the next real turn. Returns None (never
    raises) if the transcript is missing or has no usage data yet."""
    path = os.path.join(_CLAUDE_PROJECTS_DIR, f"{session_id}.jsonl")
    if not os.path.exists(path):
        return None
    last_usage: dict | None = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # A compact_boundary invalidates every usage entry before it —
                # the context the NEXT turn sees is the compacted one, whose
                # size only shows up in the next real turn's usage. Without
                # this reset the last pre-compact usage (still > threshold)
                # would re-trigger auto-compact on every message.
                if obj.get("subtype") == "compact_boundary":
                    last_usage = None
                    continue
                usage = (obj.get("message") or {}).get("usage")
                if usage:
                    last_usage = usage
    except OSError:
        return None
    if not last_usage:
        return None
    return (
        last_usage.get("input_tokens", 0)
        + last_usage.get("cache_read_input_tokens", 0)
        + last_usage.get("cache_creation_input_tokens", 0)
    )


def _resolve_agent_config(s: Session, agent: Agent) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Return (permissions, extra_volumes, mcp_config) for *agent*. When
    ``agent.agent_config_slug`` points at an AgentConfig, that record wins —
    otherwise fall back to the legacy inline columns on Agent itself (agents
    created before Agents Config existed)."""
    if agent.agent_config_slug:
        from ..models import AgentConfig
        cfg = s.query(AgentConfig).filter(AgentConfig.slug == agent.agent_config_slug,
                                          AgentConfig.deleted_at.is_(None)).first()
        if cfg:
            return dict(cfg.permissions or {}), list(cfg.extra_volumes or []), dict(cfg.mcp_config or {})
    return dict(agent.permissions or {}), list(agent.extra_volumes or []), dict(agent.mcp_config or {})


def _resolve_auto_compact_threshold(s: Session, agent: Agent) -> int | None:
    """Return *agent*'s own auto-compact threshold override (via its
    AgentConfig), or None to inherit the platform-wide setting."""
    if agent.agent_config_slug:
        from ..models import AgentConfig
        cfg = s.query(AgentConfig).filter(AgentConfig.slug == agent.agent_config_slug,
                                          AgentConfig.deleted_at.is_(None)).first()
        if cfg is not None:
            return cfg.auto_compact_threshold_tokens
    return None


def _agent_to_runtime(s: Session, agent: Agent) -> dict[str, Any]:
    from ..models import Model
    provider = "echo"
    model_id = "echo"
    model_slug = None
    params: dict[str, Any] = {}
    if agent.model_slug:
        m = s.query(Model).filter(Model.slug == agent.model_slug).first()
        if m:
            provider = m.provider
            model_id = m.model_id
            params = dict(m.params or {})
            model_slug = m.slug
    params.update(agent.params or {})
    permissions, extra_volumes, mcp_config = _resolve_agent_config(s, agent)
    # If the agent has an MCP config, inject the config dir for Docker mode
    if mcp_config.get("servers"):
        import os as _os
        base = _os.environ.get("AW_BASE_DIR", "/opt/agentic-workspace")
        mcp_dir = f"{base}/data/agents-platform/{agent.id}"
        params.setdefault("docker_mcp_config_dir", mcp_dir)
    # Always inject agent_id so docker cwd isolation can use it
    params["agent_id"] = agent.id
    # Resolve inherited system_prompt and extra_volumes (one level deep — no recursive loops)
    system_prompt = agent.system_prompt or ""
    if agent.inherit_from:
        parent = s.query(Agent).filter(Agent.slug == agent.inherit_from,
                                       Agent.deleted_at.is_(None)).first()
        if parent:
            if not system_prompt:
                system_prompt = parent.system_prompt or ""
            # Prepend parent volumes; child volumes take precedence (dedup by container path)
            _, parent_vols, _ = _resolve_agent_config(s, parent)
            seen_container = {v.split(":", 1)[-1] for v in extra_volumes}
            for pv in parent_vols:
                container = pv.split(":", 1)[-1]
                if container not in seen_container:
                    extra_volumes.insert(0, pv)
    # If the agent belongs to an AgentGroup, prepend the group's shared
    # instructions to this agent's own system_prompt (append semantics:
    # group instructions first, then whatever is agent-specific).
    if agent.group_slug:
        from ..models import AgentGroup
        group = s.query(AgentGroup).filter(AgentGroup.slug == agent.group_slug,
                                           AgentGroup.deleted_at.is_(None)).first()
        if group and group.instructions:
            system_prompt = f"{group.instructions}\n\n{system_prompt}" if system_prompt else group.instructions
    # Resolve agent permissions into additional volume mounts.
    # Values are either a single "host:container[:opts]" string or a list of them.
    import os as _os
    _aw_base = _os.environ.get("AW_BASE_DIR", "/opt/agentic-workspace")
    _data_home = _os.path.join(_aw_base, "data", "home")
    _perm_volumes: dict[str, str | list[str]] = {
        "docker": "/var/run/docker.sock:/var/run/docker.sock",
        # Naive "/tmp:/tmp" would be resolved by the BARE-METAL host's dockerd
        # (docker.sock is a sibling-container passthrough, same gotcha as the
        # .tmp case below) — that's the physical host's /tmp, not the
        # sandbox's own. Use the real host path the sandbox's own /tmp is
        # bind-mounted from (see docker-compose.yml aw-sandbox volumes) so
        # agent containers actually share the sandbox's /tmp.
        "tmp_access": f"{_aw_base}/data/sandbox-tmp:/tmp",
        "github": [
            v for v in [
                f"{_data_home}/.gitconfig:/home/ubuntu/.gitconfig:ro"
                if _os.path.exists(f"{_data_home}/.gitconfig") else None,
                f"{_data_home}/.config/gh:/home/ubuntu/.config/gh:ro"
                if _os.path.exists(f"{_data_home}/.config/gh") else None,
            ] if v
        ],
    }
    seen_container = {v.split(":", 1)[-1] for v in extra_volumes}
    for perm, vol_or_list in _perm_volumes.items():
        if permissions.get(perm):
            vols = [vol_or_list] if isinstance(vol_or_list, str) else vol_or_list
            for vol in vols:
                container = vol.split(":", 1)[-1].split(":")[0]
                if container not in seen_container:
                    extra_volumes.append(vol)
                    seen_container.add(container)
    # `.tmp` under the agent's cwd must resolve to the SAME durable host dir
    # awserv itself reads/writes (`data/tmp`), regardless of workspace_access:
    # OFF means docker_agent mounts an empty tmpfs at cwd (so `.tmp` would be a
    # fresh, non-shared dir that vanishes on container exit); ON means the real
    # repo is bind-mounted (so `.tmp` would just be that repo's checked-in,
    # stale subdir — not the live data/tmp content). Either way, an explicit,
    # more-specific bind mount is needed to shadow whatever `.tmp` would
    # otherwise resolve to. Always add it.
    _tmp_container = f"{_aw_base}/.tmp"
    if _tmp_container not in seen_container:
        # The docker socket we spawn sibling containers through belongs to
        # the BARE-METAL host's daemon, not to aw-sandbox's own filesystem
        # view — so a bind source of "{_aw_base}/.tmp" resolves against
        # the host's literal /opt/agentic-workspace/.tmp (empty/stale),
        # not against what aw-sandbox itself sees at that path (which is
        # its own bind mount of the host's data/tmp). Use the real host
        # source so sibling containers see the same durable .tmp state.
        extra_volumes.append(f"{_aw_base}/data/tmp:{_tmp_container}")
        seen_container.add(_tmp_container)
    if extra_volumes:
        params["extra_volumes"] = extra_volumes
    # share_network: join aw-sandbox's docker netns instead of the default bridge,
    # so 127.0.0.1 reaches awserv/redis/postgres/the agents-platform backend itself.
    # Secure by default: an agent with no AgentConfig/permissions gets NO access —
    # opt in per-agent via the "Share network" permission checkbox.
    params["share_network"] = bool(permissions.get("share_network", False))
    # "Agentic Workspace Folder Access" — controls only whether the REAL repo is
    # bind-mounted into the container; it must NOT change the CLI's working dir.
    # The claude CLI keys conversation memory (session files under
    # ~/.claude/projects/<encoded-cwd>/) by cwd, and ~/.claude is a shared mount,
    # so pinning cwd to /opt/agentic-workspace for every agent means:
    #   • all docker agents share one session store (same cwd → same project dir),
    #   • those sessions are the SAME ones host/external CLIs see (they also run
    #     from /opt/agentic-workspace), so a session_id resumes identically
    #     inside and outside the container, and
    #   • toggling the mount never moves the session dir → memory is preserved.
    # When access is ON the repo is bind-mounted at that path; when OFF docker_agent
    # mounts an empty writable tmpfs there instead (the dir still exists as cwd,
    # just without the repo). See CliLLM.mount_cwd / build_docker_argv.workdir.
    #
    # "isolated_identity" is a SEPARATE, stronger opt-out from "workspace_access":
    # turning workspace_access off still keeps cwd pinned to _aw_base, so the CLI's
    # ~/.claude/projects/<encoded-cwd>/ session dir (and this project's auto-memory
    # MEMORY.md under it) is the SAME shared one every other AW agent uses --
    # ~/.claude itself is always mounted (creds), independent of the repo bind, so
    # an agent can still pick up AW's memory/session history by cwd alone even with
    # the repo unmounted. Public-facing personas (e.g. the aw-roblox Genie NPC) need
    # zero awareness of AW at all, not just no filesystem access -- so this flag
    # points cwd at a completely different, never-before-used path instead, which
    # gets its own empty project dir with no CLAUDE.md/AGENTS.md and no memory bleed.
    if permissions.get("isolated_identity"):
        params["cwd"] = "/home/ubuntu"
        params["mount_cwd"] = False
    else:
        params["cwd"] = _aw_base
        params["mount_cwd"] = bool(permissions.get("workspace_access", False))
    # /opt is now the cwd, so drop any redundant --add-dir for it (keep e.g. /tmp).
    params["add_dirs"] = [d for d in (params.get("add_dirs") or []) if d != _aw_base]
    return {"provider": provider, "model_id": model_id, "model_slug": model_slug,
            "params": params,
            "system_prompt": system_prompt,
            "tool_specs": list(agent.tool_specs or []),
            "skill_slugs": list(agent.skill_slugs or []),
            "verbose_replies": bool(permissions.get("verbose_replies", False))}


async def _notify_kanban_run_done(*, run_id: str, agent_slug: str,
                                   notion_task_id: str, status: str, text: str,
                                   started_at: datetime | None = None,
                                   ended_at: datetime | None = None,
                                   hop_count: int = 0,
                                   tokens_total: int = 0) -> None:
    """Fire-and-forget: tell awserv that a Notion-linked run finished."""
    import os as _os
    try:
        import httpx as _httpx
        awserv = _os.environ.get("AWSERV_BASE", "http://127.0.0.1:9123")
        # Read awserv API key for internal call auth
        api_key = ""
        try:
            key_path = _os.path.join(_os.environ.get("AW_BASE_DIR", "/opt/agentic-workspace"), ".tmp", "awserv_api_key")
            with open(key_path) as _f:
                api_key = _f.read().strip()
        except Exception:
            pass
        headers = {"X-Api-Key": api_key} if api_key else {}
        async with _httpx.AsyncClient(timeout=10.0) as c:
            await c.post(f"{awserv}/api/workspace-agent/notify",
                         json={
                             "run_id": run_id,
                             "agent_slug": agent_slug,
                             "notion_task_id": notion_task_id,
                             "status": status,
                             "summary": text,
                             "started_at": started_at.isoformat() if started_at else None,
                             "ended_at": ended_at.isoformat() if ended_at else None,
                             "run_url": f"https://agents-platform.app.aw.tekflox.com/runs/{run_id}",
                             "hop_count": hop_count,
                             "tokens_total": tokens_total,
                         },
                         headers=headers)
    except Exception:
        pass


def _resolve_kanban_target_status(s: Session, agent: Agent) -> str | None:
    """Effective Kanban status this agent should set its card to on dispatch —
    ``Agent.kanban_target_status`` wins; falls back to the agent's
    ``AgentGroup.kanban_target_status`` when the agent itself has none set."""
    if agent.kanban_target_status:
        return agent.kanban_target_status
    if agent.group_slug:
        group = s.query(AgentGroup).filter(AgentGroup.slug == agent.group_slug,
                                           AgentGroup.deleted_at.is_(None)).first()
        if group and group.kanban_target_status:
            return group.kanban_target_status
    return None


def _kanban_awserv_headers() -> tuple[str, dict[str, str]]:
    """(awserv base URL, X-Api-Key header) shared by the fire-and-forget
    auto-set-on-Kanban-card helpers below."""
    import os as _os
    awserv = _os.environ.get("AWSERV_BASE", "http://127.0.0.1:9123")
    api_key = ""
    try:
        key_path = _os.path.join(_os.environ.get("AW_BASE_DIR", "/opt/agentic-workspace"), ".tmp", "awserv_api_key")
        with open(key_path) as _f:
            api_key = _f.read().strip()
    except Exception:
        pass
    return awserv, ({"X-Api-Key": api_key} if api_key else {})


async def _auto_set_kanban_status(*, notion_task_id: str, status_key: str, run_id: str) -> None:
    """Fire-and-forget: move a Kanban card's Status before its agent's run
    actually starts executing, driven by Agent/AgentGroup.kanban_target_status.
    Best-effort — a rejected move (e.g. the done/ready_to_deploy/need_human
    hard-lock during an active QA cycle) must never block the run itself."""
    try:
        import httpx as _httpx
        awserv, headers = _kanban_awserv_headers()
        async with _httpx.AsyncClient(timeout=10.0) as c:
            await c.post(f"{awserv}/api/notion/kanban/move",
                         json={"page_id": notion_task_id, "status": status_key, "run_id": run_id},
                         headers=headers)
    except Exception:
        _exec_log.warning("auto-set-kanban-status failed: page=%s status=%s run=%s",
                          notion_task_id, status_key, run_id, exc_info=True)


async def _auto_set_kanban_agent_slug(*, notion_task_id: str, agent_slug: str, run_id: str) -> None:
    """Fire-and-forget: stamp the card's `AgentSlug` property with whichever
    agent is dispatched against it now, on every run tied to a card — not
    just the ones with a configured kanban_target_status.

    Without this, `AgentSlug` is only ever written once, at card creation
    (see `create-task` in notion_kanban.py), and silently goes stale the
    moment a different agent picks the card up (e.g. a handoff via
    `run_agent_async`, or the QA/next-hop agent in a flow) — nothing forces
    the dispatching agent to call `set_kanban_property` itself, so the card
    would misreport who's actually working it unless the backend does this
    systemically instead of relying on the LLM to remember."""
    try:
        import httpx as _httpx
        awserv, headers = _kanban_awserv_headers()
        async with _httpx.AsyncClient(timeout=10.0) as c:
            await c.post(f"{awserv}/api/notion/kanban/set-property",
                         json={"page_id": notion_task_id, "property": "AgentSlug", "value": agent_slug},
                         headers=headers)
    except Exception:
        _exec_log.warning("auto-set-kanban-agent-slug failed: page=%s agent=%s run=%s",
                          notion_task_id, agent_slug, run_id, exc_info=True)


def _agents_flow_context(s: Session, agent_slug: str, own_call_me_back: bool,
                         own_parent_run_id: str | None = None) -> str | None:
    """If ``agent_slug`` is a node in any ENABLED AgentFlow — either directly
    (an ``agent`` node) or as a member of a ``group`` node — return a text
    block (agents directly connected to it in that flow, union across every
    matching flow, plus whether this run's own call_me_back is set — and if
    so, WHICH agent called it, so it has context on the call) to inject
    right after the aw-agents-flow skill. Returns None if the agent isn't
    reachable via any enabled flow — such an agent gets no flow-mode context
    at all, same as before this feature existed.

    This is the *only* thing that decides injection — evaluated fresh at
    invocation time for every run, independent of Kanban/notion_task_id or
    anything else about how the run was triggered.

    Loose by design (see skills/aw-agents-flow/SKILL.md): this list is
    guidance, never enforced — nothing stops the agent calling someone not
    on it.
    """
    from ..models import AgentFlow, AgentGroup

    me = s.query(Agent).filter(Agent.slug == agent_slug).first()
    my_group_slug = me.group_slug if me else None

    flows = (s.query(AgentFlow)
             .filter(AgentFlow.enabled.is_(True), AgentFlow.deleted_at.is_(None)).all())
    connected: dict[str, str] = {}  # agent_slug -> how it's connected (informational)
    matched_any = False
    for flow in flows:
        graph = flow.graph or {}
        nodes = {n.get("id"): n for n in (graph.get("nodes") or [])}
        my_node_ids = {nid for nid, n in nodes.items()
                       if n.get("type") == "agent" and n.get("agent_slug") == agent_slug}
        if my_group_slug:
            my_node_ids |= {nid for nid, n in nodes.items()
                            if n.get("type") == "group" and n.get("group_slug") == my_group_slug}
        if not my_node_ids:
            continue
        matched_any = True
        for edge in (graph.get("edges") or []):
            src, tgt = edge.get("source"), edge.get("target")
            if src in my_node_ids:
                other = nodes.get(tgt)
            elif tgt in my_node_ids:
                other = nodes.get(src)
            else:
                continue
            if not other:
                continue
            if other.get("type") == "agent" and other.get("agent_slug"):
                connected.setdefault(other["agent_slug"], "directly connected")
            elif other.get("type") == "group" and other.get("group_slug"):
                group = s.query(AgentGroup).filter(AgentGroup.slug == other["group_slug"]).first()
                members = s.query(Agent).filter(Agent.group_slug == other["group_slug"],
                                                Agent.deleted_at.is_(None)).all()
                for m in members:
                    if m.slug != agent_slug:
                        connected.setdefault(m.slug, f"via group '{group.name if group else other['group_slug']}'")
            # a "source" neighbor is the origin channel — nothing to call, skip

    if not matched_any:
        return None

    # Drop agents flagged hidden_from_flow — never suggested here, but still
    # fully callable by slug (not enforced, see skills/aw-agents-flow).
    agent_rows = s.query(Agent).filter(Agent.slug.in_(connected.keys()),
                                       Agent.deleted_at.is_(None),
                                       Agent.hidden_from_flow.is_(False)).all()
    by_slug = {a.slug: a for a in agent_rows}
    connected = {k: v for k, v in connected.items() if k in by_slug}

    lines = ["## Your Agents Flow context (this run only)"]
    if connected:
        lines.append("Agents directly connected to you in this flow — a starting point, "
                     "not a restriction (use list_agents for anyone else):")
        for aslug in sorted(connected.keys()):
            a = by_slug[aslug]
            cap = (a.capabilities or "").strip()
            lines.append(f"- `{aslug}` ({a.name}){': ' + cap if cap else ''}")
    else:
        lines.append("No other agents are directly connected to you in this flow yet — "
                     "use list_agents to find who to call.")

    if own_call_me_back:
        caller_desc = "the agent that called you"
        if own_parent_run_id:
            parent = s.query(Run).filter(Run.id == own_parent_run_id).first()
            if parent and parent.source_slug:
                caller_agent = s.query(Agent).filter(Agent.slug == parent.source_slug).first()
                caller_desc = f"`{parent.source_slug}`" + (f" ({caller_agent.name})" if caller_agent else "")
        lines.append(f"\n{caller_desc} called you and is waiting for your result — this run was "
                     "dispatched with call_me_back=true, so that agent's session will be resumed "
                     "automatically with your output when you finish. You don't need to call "
                     "return_to_caller_agent yourself (safe no-op if you do anyway).")
    else:
        lines.append("\nNo one is automatically waiting for your result. To report back to "
                     "whoever called you, call return_to_caller_agent explicitly.")
    return "\n".join(lines)


def _matched_enabled_flow_slug(s: Session, agent_slug: str) -> str | None:
    """First ENABLED AgentFlow (by created_at) where ``agent_slug`` appears as
    an agent node, or a member of a group node. Used only to pick which flow
    a run STARTS — inheritance from a parent run's own flow_run_id (see
    _record_flow_hop) always takes precedence, so a downstream agent doesn't
    need to be a node in the same graph to stay counted as part of the flow.
    """
    from ..models import AgentFlow

    me = s.query(Agent).filter(Agent.slug == agent_slug).first()
    my_group_slug = me.group_slug if me else None
    flows = (s.query(AgentFlow)
             .filter(AgentFlow.enabled.is_(True), AgentFlow.deleted_at.is_(None))
             .order_by(AgentFlow.created_at.asc()).all())
    for flow in flows:
        nodes = (flow.graph or {}).get("nodes") or []
        for n in nodes:
            if n.get("type") == "agent" and n.get("agent_slug") == agent_slug:
                return flow.slug
            if my_group_slug and n.get("type") == "group" and n.get("group_slug") == my_group_slug:
                return flow.slug
    return None


def _record_flow_hop(s: Session, run: "Run", agent_slug: str, parent_run_id: str | None,
                     session_id: str | None = None) -> None:
    """Assign ``run.flow_run_id``/``run.flow_slug`` and append a row to
    ``agent_flow_runs`` (the round-by-round history) — either inheriting the
    parent run's live flow (any child of a flow run counts as part of that
    flow by default, regardless of whether it's itself a node in the graph),
    or, for a root run with no flow-carrying parent, checking for a live flow
    on the same Kanban card or the same CLI session (see the fallbacks
    below), or finally starting a brand new flow if this agent is a node in
    an ENABLED AgentFlow. No-ops (leaves both fields null) for a plain run
    outside any flow.

    MUST be called before the run's own turn starts executing (i.e. before
    any tool call it might make, like ``run_agent_async``) — every call site
    in this module satisfies that (see core/executor.py::_run_agent_impl).
    2026-07-15: idempotent/never-overwrite by design (see the guard below) —
    a run's flow_run_id, once resolved, is locked in for good. This matters
    because a run that already dispatched a child using its (correct) flow
    assignment must never have that assignment silently changed afterward —
    the child already inherited the old value and can't be un-forked. Before
    this guard, ``_inherit_flow_from_session``'s post-hoc correction (fired
    after a reprompted/resumed turn finished executing) could overwrite a
    flow_run_id that a mid-turn ``run_agent_async`` call had already used to
    fork a child, leaving parent and child on two different flow instances
    for the same logical conversation. Resolving eagerly here — including
    the session_id fallback that used to live only in the post-hoc path —
    closes that race: every code path that creates OR re-enters a Run row
    before executing it calls this first, so by the time any tool call can
    fire, the flow assignment is already final."""
    if run.flow_run_id is not None:
        return  # already resolved — never re-mint/overwrite (see docstring)
    from ..models import AgentFlowRun

    flow_run_id: str | None = None
    flow_slug: str | None = None
    flow_needs_human = False
    hop_index = 0
    if parent_run_id:
        parent = s.query(Run).filter(Run.id == parent_run_id).first()
        if parent and parent.flow_run_id:
            flow_run_id = parent.flow_run_id
            flow_slug = parent.flow_slug
            flow_needs_human = parent.flow_needs_human
    if flow_run_id is None and run.notion_task_id:
        # No flow-carrying parent (e.g. dispatched via the Kanban webhook/poll,
        # invoke_kanban_agent, or create_kanban_task(start_now=true) — none of
        # those thread a parent_run_id). Rather than mint a brand-new
        # flow_run_id and silently fork the card's flow history in two, reuse
        # the most recent OTHER run against the same card that's still
        # carrying a live flow. 2026-07-15: fixes flow_run_id fragmenting
        # across dispatch mechanisms for the same card (see
        # docs: UX-Proto investigation, same-day).
        sibling = (s.query(Run)
                   .filter(Run.notion_task_id == run.notion_task_id,
                           Run.flow_run_id.is_not(None),
                           Run.id != run.id)
                   .order_by(Run.started_at.desc()).first())
        if sibling:
            flow_run_id = sibling.flow_run_id
            flow_slug = sibling.flow_slug
            flow_needs_human = sibling.flow_needs_human
    if flow_run_id is None and session_id:
        # Session-resume dispatch (timer wakeup, agent-callback auto-resume,
        # the "lost agent" reprompt) has no parent_run_id either — it's just
        # the same CLI session continuing. Reuse whatever flow the session's
        # own prior turn belonged to. This used to only run post-hoc via
        # _inherit_flow_from_session (after the turn already executed); doing
        # it here means it's resolved before this run's own turn — and any
        # child it dispatches — can even start.
        sibling = (s.query(Run)
                   .filter(Run.session_id == session_id, Run.flow_run_id.is_not(None),
                           Run.id != run.id)
                   .order_by(Run.started_at.desc()).first())
        if sibling:
            flow_run_id = sibling.flow_run_id
            flow_slug = sibling.flow_slug
            flow_needs_human = sibling.flow_needs_human
    if flow_run_id:
        last_hop = (s.query(AgentFlowRun)
                    .filter(AgentFlowRun.flow_run_id == flow_run_id)
                    .order_by(AgentFlowRun.hop_index.desc()).first())
        hop_index = (last_hop.hop_index + 1) if last_hop else 1
    if flow_run_id is None:
        flow_slug = _matched_enabled_flow_slug(s, agent_slug)
        if flow_slug is None:
            return
        import uuid as _uuid_mod
        flow_run_id = _uuid_mod.uuid4().hex

    s.add(AgentFlowRun(flow_run_id=flow_run_id, flow_slug=flow_slug, run_id=run.id,
                       agent_slug=agent_slug, hop_index=hop_index))
    run.flow_run_id = flow_run_id
    run.flow_slug = flow_slug
    run.flow_needs_human = flow_needs_human


def _inherit_flow_from_session(run_id: str, session_id: str | None) -> None:
    """Post-hoc: tag ``run_id`` with the same flow_run_id/flow_slug as the
    most recent OTHER run on ``session_id``, if that run belongs to a flow.

    Wakeup-triggered resumes (core.wakeups — timer wakeups, agent-callback
    auto-resume, return_to_caller_agent, and the "lost agent" reprompt) all
    fire via ``run_agent(agent_slug, prompt, session_id=...)`` with no
    ``parent_run_id`` — there's no dispatch "caller" in that sense, just a
    session being resumed. _record_flow_hop's parent_run_id-based
    inheritance therefore never fires for them, and the row would only get
    tagged if the agent itself happens to be a fresh flow root. This fills
    that gap: whatever flow the session's own prior turn belonged to, this
    new turn belongs to too. No-ops if there's no flow to inherit, or the
    run already carries the right tag.

    2026-07-15: superseded as the primary mechanism by the session_id
    fallback now built into `_record_flow_hop` itself (resolved BEFORE a
    run's turn executes, not after — see its docstring). Kept as a harmless
    defensive fallback for any path not yet covered; **never overwrites an
    already-resolved flow_run_id** — doing so used to be the actual bug
    (a mid-turn `run_agent_async` child could already have forked off the
    pre-correction value, orphaning it once this function rewrote the
    parent's assignment afterward)."""
    if not session_id:
        return
    from ..models import AgentFlowRun
    with session_scope() as s:
        run = s.query(Run).filter(Run.id == run_id).first()
        if run is None or run.flow_run_id is not None:
            return  # already resolved elsewhere — do not overwrite
        prev = (s.query(Run)
                .filter(Run.session_id == session_id, Run.id != run_id,
                       Run.flow_run_id.isnot(None))
                .order_by(Run.started_at.desc()).first())
        if prev is None:
            return
        last_hop = (s.query(AgentFlowRun)
                    .filter(AgentFlowRun.flow_run_id == prev.flow_run_id)
                    .order_by(AgentFlowRun.hop_index.desc()).first())
        hop_index = (last_hop.hop_index + 1) if last_hop else 1
        s.add(AgentFlowRun(flow_run_id=prev.flow_run_id, flow_slug=prev.flow_slug,
                           run_id=run.id, agent_slug=run.source_slug or "", hop_index=hop_index))
        run.flow_run_id = prev.flow_run_id
        run.flow_slug = prev.flow_slug
        run.flow_needs_human = prev.flow_needs_human


_MAX_FLOW_REPROMPTS = 1


def _took_flow_action(s: Session, run_id: str) -> bool:
    """Did this run take one of the 3 Agents Flow terminal actions during its
    turn — (a) dispatched a child agent/workflow run (handoff), (b) called
    return_to_caller_agent, or (c) called mark_flow_done? Used by the "lost
    agent" safety net (_check_flow_completion): if none of these happened,
    the run gets reprompted once on the same session."""
    if s.query(Run.id).filter(Run.parent_run_id == run_id).first():
        return True
    own = s.query(Run).filter(Run.id == run_id).first()
    if own is None:
        return False
    return bool(own.return_to_caller_done or own.marked_flow_done)


def _notify_need_human_sysadmins(*, run_id: str, agent_slug: str, reason: str,
                                 extra: str = "") -> None:
    try:
        from ..api.telegram import notify_sysadmins
        notify_sysadmins(f"🆘 Agents Flow needs a human — run {run_id} (agent {agent_slug}).\n"
                         f"{reason}{extra}\n"
                         f"https://agents-platform.app.aw.tekflox.com/runs/{run_id}")
        _exec_log.info("agents-flow need-human sysadmin notify sent: run=%s", run_id)
    except Exception:
        _exec_log.warning("escalate-need-human sysadmin notify failed run=%s", run_id, exc_info=True)


async def _escalate_need_human(*, run_id: str, agent_slug: str,
                               notion_task_id: str | None, reason: str) -> None:
    """Force this run's flow chain to a human. With a Kanban card, move it to
    need_human (comment required — AW's hard rule). Without one, there's no
    card to point at: ping sysadmins on Telegram with the run id and reason
    instead (Frederico's "mande a mensagem pelo agente principal, com o
    run_id e detalhes da intervenção" — same notify path as the existing
    hop-count-loop-guard alert).

    If a card IS linked but the move is rejected (e.g. the hard lock in
    /api/notion/kanban/move that blocks done/ready_to_deploy/need_human while
    QAStatus=In Progress — see notion_kanban.py), the escalation must not be
    silently dropped: fall back to the same sysadmin Telegram alert as the
    no-card case, so a human is notified either way."""
    import os as _os
    _exec_log.info("agents-flow escalating to Need Human: run=%s agent=%s notion_task_id=%s reason=%s",
                   run_id, agent_slug, notion_task_id, reason)
    # Mark every run sharing this flow instance (not just this one hop) —
    # drives the yellow border on the Flow chip in the Runs UI (Frederico,
    # 2026-07-15): "at a glance, did this flow ever need a human?" New hops
    # appended later (see _record_flow_hop / _inherit_flow_from_session)
    # inherit the flag too, so a human resuming the flow still sees it.
    with session_scope() as _nh_s:
        own = _nh_s.query(Run).filter(Run.id == run_id).first()
        if own and own.flow_run_id:
            _nh_s.query(Run).filter(Run.flow_run_id == own.flow_run_id).update(
                {"flow_needs_human": True})
    if notion_task_id:
        try:
            import httpx as _httpx
            awserv = _os.environ.get("AWSERV_BASE", "http://127.0.0.1:9123")
            api_key = ""
            try:
                key_path = _os.path.join(_os.environ.get("AW_BASE_DIR", "/opt/agentic-workspace"), ".tmp", "awserv_api_key")
                with open(key_path) as _f:
                    api_key = _f.read().strip()
            except Exception:
                pass
            headers = {"X-Api-Key": api_key} if api_key else {}
            async with _httpx.AsyncClient(timeout=10.0) as c:
                resp = await c.post(f"{awserv}/api/notion/kanban/move",
                                    json={"page_id": notion_task_id, "status": "need_human",
                                          "comment": f"🆘 Agents Flow escalation — run {run_id} "
                                                    f"(agent {agent_slug}): {reason}",
                                          "run_id": run_id},
                                    headers=headers)
            _exec_log.info("agents-flow need-human kanban move: run=%s status_code=%s body=%s",
                          run_id, resp.status_code, resp.text[:300])
            if resp.status_code != 200:
                _notify_need_human_sysadmins(
                    run_id=run_id, agent_slug=agent_slug, reason=reason,
                    extra=f"\n(Kanban card {notion_task_id} move to need_human was rejected — "
                          f"{resp.status_code}: {resp.text[:200]} — likely mid-QA-cycle hard lock. "
                          f"Card status was NOT updated, check it manually.)")
        except Exception:
            _exec_log.warning("escalate-need-human kanban move failed run=%s", run_id, exc_info=True)
            _notify_need_human_sysadmins(
                run_id=run_id, agent_slug=agent_slug, reason=reason,
                extra=f"\n(Kanban card {notion_task_id} move to need_human failed — see logs. "
                      f"Card status was NOT updated, check it manually.)")
    else:
        _notify_need_human_sysadmins(run_id=run_id, agent_slug=agent_slug, reason=reason,
                                     extra="\n(no Kanban card)")


async def _fire_flow_reprompt(*, run_id: str, agent_slug: str, session_id: str,
                              target_id: str | None) -> None:
    """Resume the SAME session that just finished without taking a terminal
    Agents Flow action, nudging it to decide.

    Pre-creates the reprompted run's row with is_flow_reprompt=True set
    BEFORE it executes (rather than patching the flag after run_agent()
    returns) — the reprompted run's own turn triggers its own
    _check_flow_completion as a fire-and-forget task the instant it
    finishes, which can start before a post-hoc patch would've committed;
    pre-creating avoids that race so the reprompt count is never
    undercounted (confirmed live: without this, 3 reprompts fired before
    _MAX_FLOW_REPROMPTS=2 kicked in instead of 2 — 2026-07-14)."""
    prompt = ("You finished this turn without calling another agent, using "
             "return_to_caller_agent, or concluding the task (e.g. moving the "
             "Kanban card). This is an Agents Flow — pick one now: call "
             "another agent, return to your caller, or conclude the task.")
    import uuid as _uuid_mod
    new_run_id = _uuid_mod.uuid4().hex
    with session_scope() as s:
        _target_slug = agent_slug
        if target_id:
            from ..models import Target as _Target
            _t = s.query(_Target).filter(_Target.id == target_id).first()
            if _t:
                _target_slug = _t.slug
        s.add(Run(id=new_run_id, kind="agent", target_slug=_target_slug, status="pending",
                  input={"input": prompt}, target_id=target_id, source_slug=agent_slug,
                  initiator_kind="wakeup", is_flow_reprompt=True))
    try:
        await run_agent(agent_slug, prompt, run_id=new_run_id, session_id=session_id,
                        target_id=target_id, initiator_kind="wakeup")
        _inherit_flow_from_session(new_run_id, session_id)
    except Exception:
        _exec_log.warning("flow-reprompt failed to fire for run=%s", run_id, exc_info=True)


async def _check_flow_completion(*, run_id: str, agent_slug: str, session_id: str | None,
                                 target_id: str | None, notion_task_id: str | None,
                                 hop_count: int, own_call_me_back: bool) -> None:
    """Agents Flow safety net (plan steps 4+5) — called after a flow-mode run
    finishes successfully. Order matters: hop-count is checked FIRST and
    unconditionally (already at the limit → straight to Need Human, even if
    call_me_back is set — that's a global loop guard, not a per-hop nudge).
    Then: if call_me_back is true, skip the "did it act" check entirely —
    the caller already asked to be woken up automatically, so the flow has a
    defined continuation regardless of what this agent did; nudging it would
    be redundant. Otherwise, reprompt ONCE if no terminal action was taken,
    then escalate."""
    from .security import get_setting
    from ..models import AgentFlow

    with session_scope() as s:
        own = s.query(Run).filter(Run.id == run_id).first()
        flow_run_id = own.flow_run_id if own else None
        flow_slug = own.flow_slug if own else None
        flow = (s.query(AgentFlow)
                .filter(AgentFlow.slug == flow_slug, AgentFlow.deleted_at.is_(None)).first()
                if flow_slug else None)
        flow_max_hops = flow.max_hops if flow else None
        flow_budget_tokens = flow.budget_tokens if flow else None
        flow_budget_usd = flow.budget_usd if flow else None

    max_hops = flow_max_hops if flow_max_hops is not None else get_setting("agent_chain_max_hops", 8)
    if hop_count >= max_hops:
        await _escalate_need_human(
            run_id=run_id, agent_slug=agent_slug, notion_task_id=notion_task_id,
            reason=f"hop count {hop_count} reached the "
                  f"{'flow' if flow_max_hops is not None else 'agent_chain_max_hops'} "
                  f"limit ({max_hops}) without the task concluding.")
        return

    if flow_run_id and (flow_budget_tokens is not None or flow_budget_usd is not None):
        with session_scope() as s:
            runs = s.query(Run.tokens_in, Run.tokens_out, Run.cost_usd).filter(
                Run.flow_run_id == flow_run_id).all()
            tot_tok = sum((r.tokens_in or 0) + (r.tokens_out or 0) for r in runs)
            tot_usd = sum(r.cost_usd or 0.0 for r in runs)
        if flow_budget_tokens is not None and tot_tok >= flow_budget_tokens:
            await _escalate_need_human(
                run_id=run_id, agent_slug=agent_slug, notion_task_id=notion_task_id,
                reason=f"flow '{flow_slug}' token budget reached: {tot_tok:,} >= "
                      f"cap {flow_budget_tokens:,}.")
            return
        if flow_budget_usd is not None and tot_usd >= flow_budget_usd:
            await _escalate_need_human(
                run_id=run_id, agent_slug=agent_slug, notion_task_id=notion_task_id,
                reason=f"flow '{flow_slug}' cost budget reached: ${tot_usd:.2f} >= "
                      f"cap ${flow_budget_usd:.2f}.")
            return

    if own_call_me_back:
        _exec_log.info("agents-flow: run=%s has call_me_back=true — flow continuation is "
                       "already defined, skipping the reprompt check", run_id)
        return

    if not session_id:
        return  # can't reprompt without a resumable session — nothing more to do

    with session_scope() as s:
        if _took_flow_action(s, run_id):
            _exec_log.info("agents-flow: run=%s took a flow action — no reprompt needed", run_id)
            return
        reprompt_count = (s.query(Run)
                          .filter(Run.session_id == session_id, Run.is_flow_reprompt.is_(True))
                          .count())

    if reprompt_count >= _MAX_FLOW_REPROMPTS:
        await _escalate_need_human(
            run_id=run_id, agent_slug=agent_slug, notion_task_id=notion_task_id,
            reason=f"reprompted {reprompt_count} time(s) without deciding to call another "
                  "agent, return to caller, or conclude the task.")
        return

    _exec_log.info("agents-flow: run=%s took no action, reprompt_count=%d — firing reprompt",
                   run_id, reprompt_count)
    await _fire_flow_reprompt(run_id=run_id, agent_slug=agent_slug,
                              session_id=session_id, target_id=target_id)


async def _acquire_session_lock(session_id: str) -> bool:
    """Wait for exclusive rights to resume ``session_id``. Returns True when
    the lock was actually acquired (caller must release), False when this task
    already owns it (re-entrant) or the safety-valve timeout expired."""
    task = asyncio.current_task()
    if task is not None and _SESSION_LOCK_OWNER.get(session_id) is task:
        return False  # nested call (auto-compact) inside the owning run
    lock = _SESSION_LOCKS.setdefault(session_id, asyncio.Lock())
    _SESSION_LOCK_REFS[session_id] = _SESSION_LOCK_REFS.get(session_id, 0) + 1
    if lock.locked():
        _exec_log.info("session %s busy — queueing run behind the one in flight",
                       session_id)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=_SESSION_LOCK_MAX_WAIT_S)
    except asyncio.TimeoutError:
        _exec_log.warning(
            "session %s: lock wait exceeded %ss — proceeding unserialized "
            "(previous run likely hung)", session_id, _SESSION_LOCK_MAX_WAIT_S)
        _release_session_ref(session_id)
        return False
    except RuntimeError as e:
        # Self-heal a stale lock bound to a dead event loop. This happens when a
        # lock was first created under a throwaway asyncio.run() loop (e.g. the
        # telegram dispatcher's fallback when _MAIN_LOOP wasn't captured yet):
        # the loop dies but the Lock stays cached in _SESSION_LOCKS, so every
        # later acquire on the real loop raises "bound to a different event
        # loop" and wedges the session forever. Replace it with a fresh lock on
        # the current loop and acquire that.
        if "different event loop" not in str(e):
            raise
        _exec_log.warning("session %s: stale cross-loop lock — recreating", session_id)
        lock = asyncio.Lock()
        _SESSION_LOCKS[session_id] = lock
        _SESSION_LOCK_OWNER.pop(session_id, None)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=_SESSION_LOCK_MAX_WAIT_S)
        except asyncio.TimeoutError:
            _release_session_ref(session_id)
            return False
    _SESSION_LOCK_OWNER[session_id] = task
    _exec_log.info("session %s: lock acquired by task %s", session_id, id(task))
    return True


def _release_session_ref(session_id: str) -> None:
    """Drop one waiter/holder reference; garbage-collect the lock at zero."""
    n = _SESSION_LOCK_REFS.get(session_id, 1) - 1
    if n <= 0:
        _SESSION_LOCK_REFS.pop(session_id, None)
        lock = _SESSION_LOCKS.get(session_id)
        if lock is not None and not lock.locked():
            _SESSION_LOCKS.pop(session_id, None)
    else:
        _SESSION_LOCK_REFS[session_id] = n


def _release_session_lock(session_id: str) -> None:
    _SESSION_LOCK_OWNER.pop(session_id, None)
    lock = _SESSION_LOCKS.get(session_id)
    if lock is not None and lock.locked():
        lock.release()
    _release_session_ref(session_id)


async def run_agent(
    agent_slug: str,
    user_input: str,
    *,
    run_id: str | None = None,
    event_run_id: str | None = None,
    parent_run_id: str | None = None,
    initiator_kind: str = "agent_run",
    initiator_id: str | None = None,
    node_id: str | None = None,
    target_id: str | None = None,
    extra_messages: list[dict] | None = None,
    session_id: str | None = None,
    notion_task_id: str | None = None,
    source_device: str | None = None,
    attach: bool = False,
    skip_auto_compact: bool = False,
    raw_cli_prompt: bool = False,
    on_state: "Callable[[str], None] | None" = None,
    proc_msg_id: str | None = None,
) -> dict[str, Any]:
    """Serializing wrapper: runs resuming the same ``session_id`` execute
    strictly one at a time, in arrival order (see _SESSION_LOCKS above).
    Runs without a session_id (fresh sessions) are unaffected.

    ``on_state`` — optional lifecycle callback for UI surfaces (Telegram
    progress button): called with "waiting" when this run queues behind the
    session lock and "processing" when it actually starts. Must be cheap /
    non-blocking; exceptions are swallowed."""
    def _signal(state: str) -> None:
        if on_state is not None:
            try:
                on_state(state)
            except Exception:
                pass

    kwargs = dict(
        run_id=run_id, event_run_id=event_run_id, parent_run_id=parent_run_id,
        initiator_kind=initiator_kind, initiator_id=initiator_id,
        node_id=node_id, target_id=target_id, extra_messages=extra_messages,
        session_id=session_id, notion_task_id=notion_task_id,
        source_device=source_device, attach=attach,
        skip_auto_compact=skip_auto_compact, raw_cli_prompt=raw_cli_prompt,
        proc_msg_id=proc_msg_id,
    )
    if not session_id:
        _signal("processing")
        return await _run_agent_impl(agent_slug, user_input, **kwargs)
    _lk = _SESSION_LOCKS.get(session_id)
    if (_lk is not None and _lk.locked()
            and _SESSION_LOCK_OWNER.get(session_id) is not asyncio.current_task()):
        _signal("waiting")
    acquired = await _acquire_session_lock(session_id)
    _signal("processing")
    try:
        return await _run_agent_impl(agent_slug, user_input, **kwargs)
    finally:
        if acquired:
            _release_session_lock(session_id)


async def _run_agent_impl(
    agent_slug: str,
    user_input: str,
    *,
    run_id: str | None = None,
    event_run_id: str | None = None,
    parent_run_id: str | None = None,
    initiator_kind: str = "agent_run",
    initiator_id: str | None = None,
    node_id: str | None = None,
    target_id: str | None = None,
    extra_messages: list[dict] | None = None,
    session_id: str | None = None,
    notion_task_id: str | None = None,
    source_device: str | None = None,
    attach: bool = False,
    skip_auto_compact: bool = False,
    raw_cli_prompt: bool = False,
    proc_msg_id: str | None = None,
) -> dict[str, Any]:
    """Run an agent and return ``{run_id, text, status, error, tokens_in, tokens_out}``.

    ``run_id``      attach to existing row (no parent set)
    ``event_run_id`` publish events on this id too (so workflows roll up nicely)
    ``attach``      re-attach to an already-running container via its Redis Stream
                    instead of launching a new one (platform-restart recovery).
    ``skip_auto_compact`` internal — set on the nested "/compact" call itself
                    so it doesn't try to trigger another compaction of itself.
    ``source_device`` originating device/channel (e.g. "watch", "iphone",
                    "meta"/glasses, "telegram") — injected into the docker CLI
                    container as the ``AW_SOURCE_DEVICE`` env var so the agent
                    can read it directly instead of parsing the prompt header.
    ``raw_cli_prompt`` pass ``user_input`` to the CLI verbatim — no system
                    prompt, no [SYSTEM]/[USER] framing. Required for CLI slash
                    commands ("/compact"), which the claude CLI only recognises
                    at position 0 of the prompt.
    """
    # If the workflow that spawned us was already cancelled (or our own row was
    # marked while pending), bail out before spinning up an LLM subprocess.
    if is_cancelled(parent_run_id) or is_cancelled(event_run_id):
        return {"run_id": None, "text": "", "status": "cancelled",
                "error": "parent workflow cancelled", "tokens_in": 0, "tokens_out": 0}

    own_row = run_id is None
    with session_scope() as s:
        agent = s.query(Agent).filter(Agent.slug == agent_slug).first()
        if not agent:
            raise ValueError(f"agent not found: {agent_slug}")
        if agent.deleted_at is not None:
            raise ValueError(f"agent soft-deleted: {agent_slug} — restore it first")
        runtime = _agent_to_runtime(s, agent)
        agent_name = agent.name
        _kanban_target_status = _resolve_kanban_target_status(s, agent) if notion_task_id else None
        # Resolve target_slug: prefer the actual Target's slug; fall back to
        # agent slug only if no target_id is available (legacy / child run paths).
        _run_target_slug = agent_slug
        if target_id is None and parent_run_id:
            # Inherit target from the parent run so child rows are always linked.
            parent_row = s.query(Run).filter(Run.id == parent_run_id).first()
            if parent_row and parent_row.target_id:
                target_id = parent_row.target_id
        if target_id:
            from ..models import Target as _Target
            _t = s.query(_Target).filter(_Target.id == target_id).first()
            if _t:
                _run_target_slug = _t.slug
        if own_row:
            r = Run(kind="agent", target_slug=_run_target_slug, status="running",
                    input={"input": user_input},
                    parent_run_id=parent_run_id,
                    initiator_kind=initiator_kind,
                    initiator_id=initiator_id,
                    node_id=node_id,
                    target_id=target_id,
                    model_slug=runtime["model_slug"],
                    source_slug=agent_slug,
                    proc_msg_id=proc_msg_id,
                    notion_task_id=notion_task_id)
            s.add(r); s.flush()
            _record_flow_hop(s, r, agent_slug, parent_run_id, session_id=session_id)
            run_id = r.id
        else:
            r = s.query(Run).filter(Run.id == run_id).first()
            if r is None:
                r = Run(id=run_id, kind="agent", target_slug=_run_target_slug, status="running",
                        input={"input": user_input},
                        parent_run_id=parent_run_id,
                        initiator_kind=initiator_kind,
                        initiator_id=initiator_id,
                        node_id=node_id,
                        target_id=target_id,
                        model_slug=runtime["model_slug"],
                        source_slug=agent_slug,
                        proc_msg_id=proc_msg_id,
                        notion_task_id=notion_task_id)
                s.add(r); s.flush()
                _record_flow_hop(s, r, agent_slug, parent_run_id, session_id=session_id)
            else:
                if proc_msg_id and not r.proc_msg_id:
                    r.proc_msg_id = proc_msg_id
                if notion_task_id and not r.notion_task_id:
                    r.notion_task_id = notion_task_id
                # Pre-created row (Agents Flow reprompt, pending-wakeup-run
                # executor, ask_human resume) that never went through
                # _record_flow_hop at creation — resolve it now, still
                # before this run's own turn (and any tool call it might
                # make) starts executing. See _record_flow_hop's docstring
                # for why this must happen before, not after.
                if r.flow_run_id is None:
                    _record_flow_hop(s, r, agent_slug, parent_run_id, session_id=session_id)

    if notion_task_id and _kanban_target_status:
        asyncio.create_task(_auto_set_kanban_status(
            notion_task_id=notion_task_id, status_key=_kanban_target_status, run_id=run_id))
    if notion_task_id:
        asyncio.create_task(_auto_set_kanban_agent_slug(
            notion_task_id=notion_task_id, agent_slug=agent_slug, run_id=run_id))

    # Per-run MCP config: same servers as the agent's static config, plus an
    # X-Aw-Caller-Run-Id header the gateway reads to identify this run to
    # agents-platform's own tools (run_agent_async's caller_run_id,
    # return_to_caller_agent) — see api/agents.py::write_run_mcp_config and
    # core/wakeups.py. Written per-run (not the shared per-agent dir) so
    # concurrent runs of the same agent never race on the header value.
    try:
        from ..api.agents import write_run_mcp_config
        with session_scope() as _mcp_s:
            # Re-query by slug (a plain str param, not an ORM attribute) — the
            # `agent` object is detached from the outer `with session_scope()
            # as s:` block above (session already closed), and its attributes
            # are expired-by-default on commit, so even `agent.id` would try
            # (and fail) to lazy-load against a closed session.
            _fresh_agent = _mcp_s.query(Agent).filter(Agent.slug == agent_slug).first()
            _run_context = {"NOTION_TASK_ID": notion_task_id} if notion_task_id else None
            _run_mcp_dir = (write_run_mcp_config(_fresh_agent, run_id, _mcp_s, extra_context=_run_context)
                             if _fresh_agent else None)
        if _run_mcp_dir:
            runtime["params"]["docker_mcp_config_dir"] = _run_mcp_dir
    except Exception:
        _exec_log.warning("per-run mcp config write failed for run %s — falling back "
                          "to the static per-agent config (no caller-run-id header)",
                          run_id, exc_info=True)

    ev_ids = {run_id}
    if event_run_id and event_run_id != run_id:
        ev_ids.add(event_run_id)

    async def emit(kind: str, payload: dict | None = None, node: str | None = None):
        for eid in ev_ids:
            await bus.publish(eid, kind, payload or {}, node_id=node or agent_slug)

    await emit("node_start", {"label": agent_name, "agent": agent_slug,
                              "provider": runtime["provider"],
                              "model": runtime["model_id"],
                              "model_slug": runtime["model_slug"],
                              "run_id": run_id,
                              "parent_run_id": parent_run_id}, node=node_id or agent_slug)

    # Pending session command: clear_session/compact_session (MCP tools) queue
    # a row in pending_session_commands instead of acting mid-conversation —
    # neither slash command can run inside an already-open turn. It's applied
    # here, before THIS turn's real prompt, then deleted (once per queue call).
    # "compact" mirrors auto-compact below: a nested "/compact" child run on
    # the same session_id. "clear" can't be sent to the CLI headless (verified
    # no-op — see memory/claude-cli-headless-clear-noop.md); the only real
    # clear is a fresh session, so we just drop session_id for this call —
    # no --resume, a brand-new session_id gets minted and captured onto this
    # Run row below exactly like any other fresh run, so whatever caller reads
    # run.session_id to persist "next session to resume" picks it up naturally.
    if session_id and not skip_auto_compact and user_input.strip() not in ("/compact", "/clear"):
        from ..models import PendingSessionCommand
        with session_scope() as _pcs:
            _pending = (_pcs.query(PendingSessionCommand)
                        .filter(PendingSessionCommand.session_id == session_id)
                        .first())
            _pending_cmd = _pending.command if _pending else None
            if _pending:
                _pcs.delete(_pending)
        if _pending_cmd == "compact":
            _exec_log.info(
                "pending compact: session %s before run %s", session_id, run_id)
            await run_agent(
                agent_slug, "/compact",
                parent_run_id=run_id, initiator_kind="pending_compact",
                target_id=target_id, session_id=session_id,
                skip_auto_compact=True, raw_cli_prompt=True,
            )
        elif _pending_cmd == "clear":
            _exec_log.info(
                "pending clear: session %s before run %s — starting a fresh "
                "session instead", session_id, run_id)
            session_id = None

    # Auto-compact: a resumed session that's grown past the configured
    # threshold gets a "/compact" turn first — same session_id, its own Run
    # row (initiator_kind="auto_compact") — before this turn's real message
    # is processed. Per-agent override lives on the agent's AgentConfig
    # (auto_compact_threshold_tokens); falls back to Settings → General →
    # "Auto-compact threshold (tokens)" when the agent has no override.
    # 0 disables. Guarded by skip_auto_compact so the compact call itself
    # doesn't try to compact itself.
    auto_compacted = False
    if session_id and not skip_auto_compact and user_input.strip() != "/compact":
        from .security import get_setting
        threshold_override = None
        with session_scope() as _cfg_s:
            _agent_row = _cfg_s.query(Agent).filter(Agent.slug == agent_slug).first()
            if _agent_row is not None:
                threshold_override = _resolve_auto_compact_threshold(_cfg_s, _agent_row)
        try:
            threshold = int(threshold_override if threshold_override is not None
                             else get_setting("auto_compact_threshold_tokens", 500_000) or 0)
        except (TypeError, ValueError):
            threshold = 500_000
        cooldown_until = _AUTO_COMPACT_COOLDOWN.get(session_id, 0.0)
        if threshold > 0 and _time.monotonic() >= cooldown_until:
            current_tokens = _current_session_token_total(session_id)
            if current_tokens is not None and current_tokens > threshold:
                _exec_log.info(
                    "auto-compact: session %s at %d tokens (> %d threshold) — "
                    "compacting before run %s", session_id, current_tokens, threshold, run_id,
                )
                await run_agent(
                    agent_slug, "/compact",
                    parent_run_id=run_id, initiator_kind="auto_compact",
                    target_id=target_id, session_id=session_id,
                    skip_auto_compact=True,
                    raw_cli_prompt=True,
                )
                # Verify it worked: a real compact writes a compact_boundary,
                # which makes the token total drop (or read as None until the
                # next real turn). If it's still above threshold, something is
                # off — back off for 30 min instead of re-compacting (and
                # re-billing a full-context turn) on every single message.
                after_tokens = _current_session_token_total(session_id)
                if after_tokens is not None and after_tokens > threshold:
                    _exec_log.warning(
                        "auto-compact: session %s still at %d tokens after compact "
                        "(threshold %d) — cooling down for %ds",
                        session_id, after_tokens, threshold, _AUTO_COMPACT_COOLDOWN_S,
                    )
                    _AUTO_COMPACT_COOLDOWN[session_id] = (
                        _time.monotonic() + _AUTO_COMPACT_COOLDOWN_S
                    )
                else:
                    _AUTO_COMPACT_COOLDOWN.pop(session_id, None)
                    auto_compacted = True

    text = ""
    # The concluding answer — the assistant text emitted AFTER the last tool
    # call. Progress narration between tool calls ("Let me search…", "Perfect!
    # Now…") is process, not answer, and would flood a Telegram chat if each
    # were delivered as its own bubble. We reset this on every tool_call so only
    # the trailing message survives; `text` keeps the full transcript for the
    # run history / View Progress.
    reply_text = ""
    # Set below when composing sys_blocks — True iff this agent is a node in
    # an ENABLED AgentFlow, i.e. it got the aw-agents-flow skill + context
    # injected. Read again at finalization to decide whether the "lost
    # agent" safety net (_check_flow_completion) applies to this run.
    _is_flow_agent = False
    # Last ScheduleWakeup tool_call seen in the stream — the one-shot CLI
    # process can't hold the timer, so we honour it ourselves (core.wakeups).
    _wakeup_req: dict | None = None
    # run_agent_async / run_workflow_async tool_call ids seen with
    # call_me_back not explicitly false (the default is to call back) —
    # matched against their tool_result to learn the dispatched child's
    # run_id, so we can arm an agent-callback once THIS run ends.
    # tool_use_id -> (agent_slug, call_me_back_on) — call_me_back_on is the
    # optional session_id override (redirect the callback to a different
    # session than this one, see core.wakeups.register_agent_callback).
    _pending_callback_calls: dict[str, tuple[str, str | None]] = {}
    _callback_watch_run_ids: list[tuple[str, str | None]] = []
    tin = tout = 0
    cost = 0.0
    err: str | None = None
    _t_run_start = _time.perf_counter()
    _t_llm_invoke: float | None = None
    _t_container_started: float | None = None
    _t_cli_first_byte: float | None = None
    # aw-connector-redis's own self-timed sub-phases of container_wait
    # (decompose it into "dockerd overhead" vs. "connector's own work").
    _connector_connect_spawn_s: float | None = None
    _connector_cli_boot_s: float | None = None
    _t_system_init: float | None = None
    _t_first_token: float | None = None
    _t_finalizing: float | None = None
    try:
        from .skills import load_skill
        sys_blocks = [runtime["system_prompt"]] if runtime["system_prompt"] else []
        for sslug in runtime["skill_slugs"]:
            content = load_skill(sslug)
            if content:
                sys_blocks.append(f"[skill:{sslug}]\n{content}")
        # Agents Flow: if this agent is a node in an ENABLED flow, inject the
        # aw-agents-flow skill (right after the instructions/other skills)
        # followed immediately by this run's specific context — connected
        # agents in that flow, and whether this run's own call_me_back means
        # someone is already waiting for the result. See
        # skills/aw-agents-flow/SKILL.md and _agents_flow_context above.
        try:
            with session_scope() as _flow_s:
                _own_row = _flow_s.query(Run).filter(Run.id == run_id).first()
                _own_cmb = bool(_own_row.call_me_back) if _own_row else False
                _own_parent = _own_row.parent_run_id if _own_row else None
                _flow_ctx = _agents_flow_context(_flow_s, agent_slug, _own_cmb, _own_parent)
            if _flow_ctx is not None:
                _is_flow_agent = True
                if "aw-agents-flow" not in runtime["skill_slugs"]:
                    _flow_skill = load_skill("aw-agents-flow")
                    if _flow_skill:
                        sys_blocks.append(f"[skill:aw-agents-flow]\n{_flow_skill}")
                sys_blocks.append(_flow_ctx)
        except Exception:
            _exec_log.warning("agents-flow context injection failed for run %s", run_id, exc_info=True)
        if raw_cli_prompt:
            # CLI slash-command turn ("/compact"): the prompt must reach the CLI
            # verbatim — no system prompt, no skills, no framing, and no
            # --append-system-prompt from model/agent params either.
            sys_blocks = []
            extra_messages = None
            runtime["params"]["raw_prompt"] = True
            runtime["params"]["append_system_prompt"] = None
        messages: list[dict] = []
        if sys_blocks:
            _effective_system_prompt = "\n\n".join(sys_blocks)
            messages.append({"role": "system", "content": _effective_system_prompt})
            try:
                with session_scope() as _sp_s:
                    _sp_row = _sp_s.query(Run).filter(Run.id == run_id).first()
                    if _sp_row:
                        _sp_row.system_prompt = _effective_system_prompt
            except Exception:
                _exec_log.warning("failed to persist system_prompt for run %s", run_id, exc_info=True)
        if extra_messages:
            messages.extend(extra_messages)
        messages.append({"role": "user", "content": user_input})

        # Inject runtime context for session isolation and resumption.
        runtime["params"]["target_id"] = target_id
        runtime["params"]["run_id"] = run_id
        if attach:
            # Re-attach to the run's durable Redis Stream (CliLLM replays it
            # instead of launching a new container).
            runtime["params"]["attach_run_id"] = run_id
        if notion_task_id:
            runtime["params"]["notion_task_id"] = notion_task_id
        if source_device:
            runtime["params"]["source_device"] = source_device
        if session_id:
            runtime["params"]["session_id"] = session_id
            # Reuse the original run's isolated cwd so --resume can find the session file.
            # The session file lives in ~/.claude/projects/{encoded_original_cwd}/,
            # so the resumed run must use that same cwd (not a new isolated one).
            with session_scope() as _ss:
                # Order by created_at ASC to get the ORIGINAL run that created
                # the session. Multiple runs can share the same session_id when
                # subsequent runs resume the same conversation; the session file
                # lives in the project dir of the FIRST run.
                _orig = (_ss.query(Run)
                         .filter(Run.session_id == session_id)
                         .order_by(Run.started_at.asc())
                         .first())
                if _orig:
                    runtime["params"]["resume_run_id"] = _orig.id

        from .models.cli import current_run_id
        from .tools.code import current_agent_params
        token = current_run_id.set(run_id)
        # Make the agent's params visible to the run_command gate so per-agent
        # security_mode / command_allowlist overrides are honored.
        params_token = current_agent_params.set(runtime["params"])
        try:
            if _provider_supports_langchain(runtime["provider"]):
                # ───── API-direct providers via LangGraph ReAct loop ─────
                tools = await tools_for_agent(runtime["tool_specs"])
                async def _emit(kind: str, payload: dict, nid: str | None):
                    await emit(kind, payload, node=nid or node_id or agent_slug)
                res = await run_langchain_agent(
                    provider=runtime["provider"],
                    model_id=runtime["model_id"],
                    params=runtime["params"],
                    system_prompt="\n\n".join(sys_blocks) if sys_blocks else "",
                    extra_messages=extra_messages or [],
                    user_message=user_input,
                    tools=tools,
                    emit=_emit,
                    node_id=node_id or agent_slug,
                    cancel_check=lambda: is_cancelled(run_id) or is_cancelled(event_run_id) or is_cancelled(parent_run_id),
                )
                text = res.text
                reply_text = res.text
                tin = max(tin, res.tokens_in)
                tout = max(tout, res.tokens_out)
                cost = max(cost, res.cost_usd)
            else:
                # ───── CLI subshell + echo path (text-only stream) ─────
                _t_llm_invoke = _time.perf_counter()
                llm = make_llm(runtime["provider"], runtime["model_id"], **runtime["params"])
                async for chunk in llm.astream(messages):
                    meta_kind = getattr(chunk, "meta_kind", None)
                    meta_payload = getattr(chunk, "meta_payload", None)
                    if meta_kind:
                        await emit(meta_kind, meta_payload or {}, node=node_id or agent_slug)
                        # A new tool call means everything narrated so far was
                        # progress, not the final answer — drop it so only the
                        # post-last-tool message reaches Telegram. Agents with
                        # the "Verbose replies" permission opt out: they want
                        # the full narration delivered (still as one message,
                        # just untruncated) instead of losing everything before
                        # the last tool call.
                        if meta_kind == "tool_call":
                            if (meta_payload or {}).get("name") == "ScheduleWakeup":
                                _wakeup_req = dict((meta_payload or {}).get("input") or {})
                            _tc_name = (meta_payload or {}).get("name") or ""
                            _tc_short = _tc_name.rsplit("__", 1)[-1]
                            if _tc_short in ("run_agent_async", "run_workflow_async"):
                                _tc_input = (meta_payload or {}).get("input") or {}
                                _tc_id = (meta_payload or {}).get("id")
                                # call_me_back defaults to true — an agent must opt OUT
                                # (call_me_back:false) to get the old pure fire-and-forget.
                                if _tc_id and _tc_input.get("call_me_back") is not False:
                                    _cmb_on = _tc_input.get("call_me_back_on") or None
                                    _pending_callback_calls[_tc_id] = (agent_slug, _cmb_on)
                            if runtime.get("verbose_replies"):
                                if reply_text.strip() and not reply_text.endswith("\n\n"):
                                    reply_text += "\n\n"
                            else:
                                reply_text = ""
                        elif meta_kind == "tool_result" and _pending_callback_calls:
                            _tr_id = (meta_payload or {}).get("tool_use_id")
                            _pending = _pending_callback_calls.pop(_tr_id, None)
                            if _pending:
                                _origin_agent_slug, _cmb_on = _pending
                                import re as _re
                                _m = _re.search(r'"run_id"\s*:\s*"([^"]+)"',
                                               str((meta_payload or {}).get("content") or ""))
                                if _m:
                                    _callback_watch_run_ids.append((_m.group(1), _cmb_on))
                                else:
                                    _exec_log.warning("agent-callback: no run_id found in tool_result "
                                                      "for %s (tool_use_id=%s)", _origin_agent_slug, _tr_id)
                        # Timing breakdown for agent.docker_ready — see cli.py's
                        # "container.started"/"cli.first_byte" meta chunks.
                        if meta_kind == "container.started" and _t_container_started is None:
                            _t_container_started = _time.perf_counter()
                        elif meta_kind == "cli.first_byte" and _t_cli_first_byte is None:
                            _t_cli_first_byte = _time.perf_counter()
                        elif meta_kind == "connector.timing" and meta_payload:
                            _phase = meta_payload.get("phase")
                            _dur = meta_payload.get("duration_s")
                            if _phase == "connect_and_spawn" and _connector_connect_spawn_s is None:
                                _connector_connect_spawn_s = _dur
                            elif _phase == "spawn_to_first_line" and _connector_cli_boot_s is None:
                                _connector_cli_boot_s = _dur
                        # Persist session_id from system.init so callers can resume later.
                        if meta_kind == "system.init" and meta_payload and run_id:
                            if _t_system_init is None:
                                _t_system_init = _time.perf_counter()
                            _sid = meta_payload.get("session_id")
                            if _sid:
                                with session_scope() as _ss:
                                    _r = _ss.query(Run).filter(Run.id == run_id).first()
                                    if _r:
                                        _r.session_id = _sid
                                from ..models import CliSession as _CliSession
                                from ..db import session_scope as _scope
                                with _scope() as _cs:
                                    if not _cs.query(_CliSession).filter(_CliSession.session_id == _sid).first():
                                        _cs.add(_CliSession(session_id=_sid, name="", description=""))
                                # Push the newly-known session_id live — otherwise a Runs
                                # screen already open when the run started never sees it
                                # (the only other run_update broadcasts are at start, before
                                # session_id exists, and at terminal state).
                                with session_scope() as _ss2:
                                    _r2 = _ss2.query(Run).filter(Run.id == run_id).first()
                                    if _r2:
                                        from .events import _run_to_ws_dict, ws_broadcast
                                        asyncio.create_task(ws_broadcast("run_update", _run_to_ws_dict(_r2)))
                    if chunk.delta:
                        if _t_first_token is None:
                            _t_first_token = _time.perf_counter()
                        text += chunk.delta
                        reply_text += chunk.delta
                        await emit("llm_token", {"delta": chunk.delta}, node=node_id or agent_slug)
                    if chunk.tokens_in:
                        tin = max(tin, chunk.tokens_in)
                    if chunk.tokens_out:
                        tout = max(tout, chunk.tokens_out)
                    if chunk.cost_usd:
                        cost = max(cost, chunk.cost_usd)
        finally:
            try: current_run_id.reset(token)
            except Exception: pass
            try: current_agent_params.reset(params_token)
            except Exception: pass
    except Exception as e:
        err = str(e)
        await emit("error", {"error": err}, node=node_id or agent_slug)

    # Marker: streaming done, status flip imminent. Lets clients stop watching
    # for new llm_token events and start expecting a terminal status.
    _t_finalizing = _time.perf_counter()
    await emit("finalizing", {"tokens_in": tin, "tokens_out": tout, "cost_usd": cost},
               node=node_id or agent_slug)

    # Cancel-grace: if the work actually completed (output present, no error)
    # but a cancel signal arrived during finalisation, preserve the output.
    # Status becomes 'success' with a note instead of overwriting work.
    cancelled = (is_cancelled(run_id) or is_cancelled(event_run_id)
                 or is_cancelled(parent_run_id))
    work_completed = bool(text) and not err
    if cancelled and work_completed:
        # Treat as graceful late-cancel — preserve the output.
        status = "success"
        err = "cancel received after completion — output preserved"
    elif cancelled:
        status = "cancelled"
        if not err:
            err = "cancelled by user"
    else:
        status = "error" if err else "success"
    _gh_issue_number = None
    _run_ws_data: dict | None = None
    _wu_ctx: tuple | None = None
    _started_at: datetime | None = None
    _hop_count = 0
    _own_call_me_back = False
    _ended_at = datetime.utcnow()
    with session_scope() as s:
        r = s.query(Run).filter(Run.id == run_id).first()
        if r:
            # claude-cli's own "result" event never arrived (process killed
            # mid-turn, usually by a backend restart) — the CLI's session
            # transcript on disk still has the real usage, so recompute cost
            # from that instead of persisting a misleading 0.0.
            if cost == 0.0 and tin > 0 and r.session_id:
                recovered = _recover_cost_from_transcript(r.session_id, r.started_at, _ended_at)
                if recovered:
                    _exec_log.info(
                        "recovered cost_usd=%.6f for run %s from transcript (was 0.0, tokens_in=%d)",
                        recovered, run_id, tin,
                    )
                    cost = recovered
            r.status = status
            r.output = {"text": text}
            r.error = err
            r.tokens_in = tin
            r.tokens_out = tout
            r.cost_usd = cost
            r.ended_at = _ended_at
            _gh_issue_number = getattr(r, "github_issue_number", None)
            _wu_ctx = (r.session_id, r.target_id, r.initiator_kind, r.initiator_id)
            _started_at = r.started_at
            # Fall back to the persisted column when the in-flight kwarg is
            # empty — happens after a restart-recovery reattach, since
            # _reattach_run doesn't (and can't) thread the original caller's
            # notion_task_id kwarg back through. The DB row set it at creation
            # time, so this survives the process restart that killed the kwarg.
            notion_task_id = notion_task_id or getattr(r, "notion_task_id", None)
            _hop_count = getattr(r, "hop_count", 0) or 0
            _own_call_me_back = bool(getattr(r, "call_me_back", False))
            from .events import _run_to_ws_dict
            _run_ws_data = _run_to_ws_dict(r)
    # Wake up any call_me_back watcher (wakeups._watch_and_callback) blocked on
    # THIS run's completion — event-driven nudge, DB row above is still the
    # source of truth if the publish is missed (see redis_streams.notify_run_finished).
    try:
        from .redis_streams import notify_run_finished
        await notify_run_finished(run_id)
    except Exception:
        _exec_log.warning("notify_run_finished failed run=%s", run_id, exc_info=True)
    # Honour a ScheduleWakeup the model issued during this run: persist + arm a
    # follow-up run on the same session (see core.wakeups for why AP must do
    # this instead of the CLI harness).
    try:
        if status == "success" and _wakeup_req and _wu_ctx:
            from .wakeups import schedule_wakeup
            schedule_wakeup(origin_run_id=run_id, agent_slug=agent_slug,
                            target_id=_wu_ctx[1], session_id=_wu_ctx[0],
                            initiator_kind=_wu_ctx[2], initiator_id=_wu_ctx[3],
                            req=_wakeup_req)
    except Exception as _we:
        _exec_log.warning("wakeup scheduling failed run=%s: %s", run_id, _we)
    # Agent-to-agent "call me back": for every run_agent_async/run_workflow_async
    # dispatch this run made with call_me_back not explicitly false, persist +
    # arm an event-driven callback that re-invokes THIS session when the
    # dispatched child run finishes (see core.wakeups.register_agent_callback).
    # Persisted on the CHILD run's own row — no in-memory context needed here
    # beyond the two ids, so this survives an AP restart mid-flight.
    if status == "success" and _callback_watch_run_ids:
        from .wakeups import register_agent_callback
        for _watch_id, _cmb_on in _callback_watch_run_ids:
            try:
                register_agent_callback(watch_run_id=_watch_id, origin_run_id=run_id,
                                        target_session_id=_cmb_on)
            except Exception as _ce:
                _exec_log.warning("agent-callback registration failed run=%s watch=%s: %s",
                                  run_id, _watch_id, _ce)
    # Agents Flow safety net (plan steps 4+5) — only for agents dispatched as
    # a node in an ENABLED flow (see _is_flow_agent, set when sys_blocks was
    # composed). Fire-and-forget: never block finalisation on this.
    if status == "success" and _is_flow_agent and _wu_ctx:
        try:
            asyncio.create_task(_check_flow_completion(
                run_id=run_id, agent_slug=agent_slug, session_id=_wu_ctx[0],
                target_id=_wu_ctx[1], notion_task_id=notion_task_id, hop_count=_hop_count,
                own_call_me_back=_own_call_me_back,
            ))
        except Exception:
            _exec_log.warning("agents-flow completion check failed to start run=%s", run_id, exc_info=True)
    score_run_terminal(run_id)
    # WS broadcast — push terminal state to all connected clients
    try:
        if _run_ws_data:
            from .events import ws_broadcast
            asyncio.create_task(ws_broadcast("run_update", _run_ws_data))
    except Exception:
        pass
    # Notion Kanban post-run notification: if this run was triggered from a Notion card,
    # notify awserv so it can update the card status and send Telegram confirmation.
    try:
        if notion_task_id and run_id:
            asyncio.create_task(_notify_kanban_run_done(
                run_id=run_id,
                agent_slug=agent_slug,
                notion_task_id=notion_task_id,
                status=status,
                text=text[:10000] if text else "",
                started_at=_started_at,
                ended_at=_ended_at,
                hop_count=_hop_count,
                tokens_total=tin + tout,
            ))
    except Exception:
        pass
    try:
        if _gh_issue_number:
            from .github_sync import update_run_issue
            asyncio.create_task(update_run_issue(
                issue_number=_gh_issue_number,
                status=status,
                tokens_in=tin,
                tokens_out=tout,
                cost_usd=cost,
                error=err,
                run_id=run_id,
            ))
    except Exception:
        pass
    await emit("node_end", {"text": text[:10000], "tokens_in": tin, "tokens_out": tout,
                            "cost_usd": cost, "run_id": run_id},
               node=node_id or agent_slug)

    # Build timing dict for callers that want observability (e.g. telegram dispatcher).
    _t_end = _time.perf_counter()
    _timing: dict[str, float | None] = {}
    if _t_llm_invoke is not None:
        _timing["llm_total_s"] = (_t_finalizing or _t_end) - _t_llm_invoke
    if _t_llm_invoke is not None and _t_system_init is not None:
        _timing["docker_ready_s"] = _t_system_init - _t_llm_invoke
    # Sub-spans of docker_ready_s: container create/start vs. CLI process boot.
    # Absent (None) for the attach/reattach-after-restart path or any run that
    # never got that far (e.g. it errored before the container came up).
    if _t_llm_invoke is not None and _t_container_started is not None:
        _timing["container_create_s"] = _t_container_started - _t_llm_invoke
    if _t_container_started is not None and _t_cli_first_byte is not None:
        _timing["container_wait_s"] = _t_cli_first_byte - _t_container_started
    # Sub-spans of container_wait_s, self-reported by aw-connector-redis's own
    # clock (not derived from our perf_counter timestamps, so no host/container
    # clock-skew assumption needed — see connector.timing meta chunks above).
    if _connector_connect_spawn_s is not None:
        _timing["connector_connect_spawn_s"] = _connector_connect_spawn_s
    if _connector_cli_boot_s is not None:
        _timing["connector_cli_boot_s"] = _connector_cli_boot_s
    if _t_cli_first_byte is not None and _t_system_init is not None:
        _timing["cli_boot_s"] = _t_system_init - _t_cli_first_byte
    if _t_llm_invoke is not None and _t_first_token is not None:
        _timing["first_token_s"] = _t_first_token - _t_llm_invoke
    if _t_system_init is not None and _t_first_token is not None:
        _timing["docker_to_first_token_s"] = _t_first_token - _t_system_init
    _timing["run_total_s"] = _t_end - _t_run_start

    if _t_llm_invoke is not None:
        _exec_log.info(
            "[TIMING] run=%s agent=%s | llm_invoke→system_init=%s "
            "system_init→first_token=%s llm_total=%.2fs run_total=%.2fs",
            (run_id or "?")[:8], agent_slug,
            f"{_timing['docker_ready_s']:.2f}s" if "docker_ready_s" in _timing else "n/a",
            f"{_timing['docker_to_first_token_s']:.2f}s" if "docker_to_first_token_s" in _timing else "n/a",
            _timing.get("llm_total_s") or 0,
            _timing["run_total_s"],
        )

    # `reply` = concluding answer only (for chat delivery); `text` = full
    # transcript (for storage / progress). Fall back to full text if the run
    # ended on a tool call with no trailing message.
    return {"run_id": run_id, "text": text, "reply": (reply_text.strip() or text),
            "status": status, "error": err, "auto_compacted": auto_compacted,
            "tokens_in": tin, "tokens_out": tout, "cost_usd": cost, "timing": _timing}


# ---------------- restart recovery ----------------

async def _reattach_run(run_id: str, agent_slug: str, user_input: str,
                        target_id: str | None, session_id: str | None) -> None:
    """Re-run finalisation for an interrupted agent run by replaying its Redis Stream."""
    try:
        result = await run_agent(agent_slug, user_input, run_id=run_id, target_id=target_id,
                                 session_id=session_id, attach=True)
        _exec_log.info("re-attached run %s finalised", run_id)
        # Close the loop with the user: deliver the recovered reply to its
        # originating chat (the webhook coroutine that would have done this died
        # with the restart). Idempotent — guarded by a Redis dedup claim.
        try:
            output_text = (result or {}).get("reply") or (result or {}).get("text", "")
            from ..api.telegram import deliver_recovered_run, finalize_progress_bubble
            if output_text:
                await deliver_recovered_run(run_id, output_text)
            # Settle the "Processing…" bubble the killed dispatch thread never
            # got to flip — [processing]/[waiting] → [done]/[error]/[cancelled].
            await asyncio.to_thread(finalize_progress_bubble, run_id,
                                    (result or {}).get("status"))
        except Exception as de:
            _exec_log.warning("recovery delivery failed run=%s: %s", run_id, de)
    except Exception as e:
        _exec_log.warning("re-attach failed run=%s: %s", run_id, e)
        # Last resort: don't leave it stuck in 'running'.
        try:
            with session_scope() as s:
                r = s.query(Run).filter(Run.id == run_id).first()
                if r and r.status == "running":
                    r.status = "cancelled"
                    r.error = f"re-attach failed: {e}"
                    r.ended_at = datetime.utcnow()
            from ..api.telegram import finalize_progress_bubble
            await asyncio.to_thread(finalize_progress_bubble, run_id, "cancelled")
        except Exception:
            pass


# How long to wait for a still-booting container to create its Redis Stream
# before concluding the run is genuinely dead. The CLI emits its first stream
# entry (system/init) within a few seconds of `docker run`; 30s is generous
# even under API rate-limiting, while bounding hung rows for truly-dead runs.
_RECOVER_STREAM_GRACE_S = 30


async def _reattach_or_wait(run_id: str, agent_slug: str, user_input: str,
                            target_id: str | None, session_id: str | None) -> None:
    """Re-attach a running agent run, NEVER cancelling one that may still stream.

    A container launched just before the restart keeps running and keeps
    publishing to its durable Redis Stream; its events simply arrive once the
    platform is back up. So we re-attach as soon as the stream has any data,
    waiting up to a grace window for a still-booting container. Only if no
    stream ever appears (the container is genuinely gone) do we finalise the
    row — otherwise it would be stuck 'running' forever.
    """
    from .redis_streams import stream_has_data

    waited = 0.0
    while waited < _RECOVER_STREAM_GRACE_S:
        try:
            if await stream_has_data(run_id):
                await _reattach_run(run_id, agent_slug, user_input, target_id, session_id)
                return
        except Exception:
            pass
        await asyncio.sleep(1.0)
        waited += 1.0

    # No stream after the grace window — the container is gone, nothing will
    # stream. Finalise so the row isn't stuck 'running' indefinitely.
    with session_scope() as s:
        r = s.query(Run).filter(Run.id == run_id).first()
        if r and r.status == "running":
            r.status = "cancelled"
            r.error = "server restarted — no live run stream to resume"
            r.ended_at = datetime.utcnow()
    try:
        from ..api.telegram import finalize_progress_bubble
        await asyncio.to_thread(finalize_progress_bubble, run_id, "cancelled")
    except Exception:
        pass
    _exec_log.info("recovery: run %s had no stream after %ss — marked cancelled",
                   run_id, int(_RECOVER_STREAM_GRACE_S))


async def recover_orphaned_runs() -> None:
    """On startup, re-attach interrupted agent runs via their durable Redis Stream.

    A docker CLI agent keeps running (and keeps publishing to its Redis Stream)
    even while the platform process is down — so on boot we re-attach instead of
    cancelling. Each agent run is handled in its own background task that waits
    for the stream (the container may still be booting) and replays it; a run is
    only cancelled if no stream ever appears (container genuinely gone). Workflow
    rows (no replayable CLI stream) are finalised immediately; their child agent
    rows recover independently.
    """
    with session_scope() as s:
        orphans = [
            (r.id, r.source_slug, (r.input or {}).get("input", ""),
             r.target_id, r.session_id, r.kind, r.status)
            for r in s.query(Run).filter(Run.status.in_(("running", "queued"))).all()
        ]
    if not orphans:
        return

    reattaching = cancelled = 0
    for run_id, slug, user_input, target_id, session_id, kind, status in orphans:
        # "queued" rows were still waiting on the session lock and never got
        # far enough to launch a docker CLI container — nothing to reattach
        # to, just cancel like any other never-started run.
        if slug and kind == "agent" and status == "running":
            # Never cancel here — hand off to a task that waits for the stream.
            reattaching += 1
            asyncio.create_task(
                _reattach_or_wait(run_id, slug, user_input, target_id, session_id),
                name=f"recover-{run_id}",
            )
        else:
            with session_scope() as s:
                r = s.query(Run).filter(Run.id == run_id).first()
                if r and r.status in ("running", "queued"):
                    r.status = "cancelled"
                    r.error = "server restarted — run interrupted"
                    r.ended_at = datetime.utcnow()
            cancelled += 1

    print(f"[startup] run recovery: {reattaching} agent run(s) re-attaching, "
          f"{cancelled} non-agent cancelled")


# ---------------- workflow execution ----------------

def _depth_of(run_id: str | None) -> int:
    """How many workflow ancestors does this run have? Used to cap recursion."""
    if not run_id:
        return 0
    depth = 0
    cur = run_id
    seen: set[str] = set()
    with session_scope() as s:
        while cur and cur not in seen and depth < 20:
            seen.add(cur)
            row = s.query(Run).filter(Run.id == cur).first()
            if row is None or row.parent_run_id is None:
                break
            cur = row.parent_run_id
            depth += 1
    return depth


def _root_run_id(run_id: str | None) -> str | None:
    """Walk up parent_run_id until NULL → returns the root workflow's run_id."""
    if not run_id:
        return None
    cur = run_id
    seen: set[str] = set()
    with session_scope() as s:
        while cur and cur not in seen:
            seen.add(cur)
            row = s.query(Run).filter(Run.id == cur).first()
            if row is None or row.parent_run_id is None:
                return cur
            cur = row.parent_run_id
    return run_id

async def run_workflow(
    workflow_slug: str,
    user_input: Any,
    *,
    run_id: str | None = None,
    parent_run_id: str | None = None,
    initiator_kind: str = "workflow_run",
    initiator_id: str | None = None,
    target_id: str | None = None,
) -> dict[str, Any]:
    with session_scope() as s:
        wf = s.query(Workflow).filter(Workflow.slug == workflow_slug).first()
        if not wf:
            raise ValueError(f"workflow not found: {workflow_slug}")
        if wf.deleted_at is not None:
            raise ValueError(f"workflow soft-deleted: {workflow_slug} — restore it first")
        kind = wf.kind
        graph = dict(wf.graph or {})
        name = wf.name
        if run_id is None:
            # Inherit target from parent run if not supplied directly.
            if target_id is None and parent_run_id:
                parent_row = s.query(Run).filter(Run.id == parent_run_id).first()
                if parent_row and parent_row.target_id:
                    target_id = parent_row.target_id
            # Resolve target_slug from the actual Target row.
            _wf_target_slug = workflow_slug
            if target_id:
                from ..models import Target as _Target
                _t = s.query(_Target).filter(_Target.id == target_id).first()
                if _t:
                    _wf_target_slug = _t.slug
            r = Run(kind="workflow", target_slug=_wf_target_slug, status="running",
                    input={"input": user_input},
                    parent_run_id=parent_run_id,
                    initiator_kind=initiator_kind,
                    initiator_id=initiator_id or workflow_slug,
                    target_id=target_id,
                    source_slug=workflow_slug)
            s.add(r); s.flush()
            run_id = r.id

    # Budget setup: the root workflow owns a shared budget the whole tree
    # charges against (both hops + tokens). Sub-workflows reuse it
    # (no re-init / no double clear).
    root_id = _root_run_id(run_id) or run_id
    own_counter = (parent_run_id is None) and not hops.has(root_id)
    if own_counter:
        max_hops = int(graph.get("max_hops") or hops.DEFAULT_MAX_HOPS)
        max_tokens = int(graph.get("max_tokens") or hops.DEFAULT_MAX_TOKENS)
        hops.init(root_id, max_hops=max_hops, max_tokens=max_tokens)
        budget_msg = f"hop limit {max_hops}"
        if max_tokens:
            budget_msg += f", token limit {max_tokens}"
        else:
            budget_msg += ", token limit unlimited"
        await bus.publish(run_id, "log", {"msg": f"budget: {budget_msg}"})

    await bus.publish(run_id, "log", {"msg": f"start workflow {name} ({kind})"})

    # Children: own row, parent set, events also bubble to workflow's run_id.
    # If a node's "agent" slug starts with ``workflow:`` we spawn a SUB-WORKFLOW
    # instead of an agent run. Lineage threads through the parent_run_id.
    async def child_agent(slug: str, payload: str, **kwargs):
        node_id = kwargs.get("node_id")
        if isinstance(slug, str) and slug.startswith("workflow:"):
            sub_slug = slug[len("workflow:"):]
            # cycle / runaway-recursion guard: max 5 levels of sub-workflow
            depth = _depth_of(run_id) + 1
            if depth > 5:
                return {"run_id": None, "text": "", "status": "error",
                        "error": f"sub-workflow depth limit exceeded at {sub_slug}",
                        "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
            res = await run_workflow(
                sub_slug, payload,
                parent_run_id=run_id,
                initiator_kind="workflow_run",
                initiator_id=workflow_slug,
                target_id=target_id,
            )
            import json as _json
            txt = (_json.dumps(res.get("output"), default=str)
                   if res.get("output") is not None else "")
            return {
                "run_id": res.get("run_id"),
                "text": txt,
                "status": res.get("status"),
                "error": res.get("error"),
                "tokens_in": res.get("tokens_in", 0),
                "tokens_out": res.get("tokens_out", 0),
                "cost_usd": res.get("cost_usd", 0.0),
            }
        return await run_agent(
            slug, payload,
            run_id=None,
            event_run_id=run_id,
            parent_run_id=run_id,
            initiator_kind="workflow_run",
            initiator_id=workflow_slug,
            node_id=node_id,
            target_id=target_id,
        )

    err: str | None = None
    final: Any = None
    cancelled = False
    limit_reached: str | None = None    # "hops" | "tokens" | None
    try:
        final = await dispatch_workflow(kind, graph, user_input, run_id,
                                        child_agent, root_run_id=root_id)
    except Cancelled as ce:
        cancelled = True
        await bus.publish(run_id, "log", {"msg": str(ce)})
    except hops.BudgetExceeded as be:
        # Safety net — orchestrators *should* catch this internally and return
        # a partial result with `limit_reached` set. If a future orchestrator
        # forgets, we still stop gracefully here (no error status).
        limit_reached = be.reason
        final = {"limit_reached": be.reason, f"{be.reason}_limit_reached": True,
                 "message": str(be)}
        await bus.publish(run_id, "log",
                          {"msg": f"budget reached ({be.reason}): {be}",
                           "budget": hops.get(root_id)})
    except Exception as e:
        err = str(e)
        await bus.publish(run_id, "error", {"error": err})

    # If the orchestrator returned a dict with `limit_reached`, surface it on
    # the workflow's run too, so the parent (and the UI) can see the cap.
    if isinstance(final, dict) and final.get("limit_reached") and not limit_reached:
        limit_reached = str(final.get("limit_reached") or "budget")

    if cancelled or is_cancelled(run_id):
        status = "cancelled"
    elif err:
        status = "error"
    else:
        # NOTE: budget breach is a *graceful* stop — status is success and
        # the output dict carries the `limit_reached` flag.
        status = "success"
    # roll-up totals from children
    _wf_gh_issue_number = None
    _wf_run_ws_data: dict | None = None
    with session_scope() as s:
        children = s.query(Run).filter(Run.parent_run_id == run_id).all()
        rollup_in = sum(c.tokens_in for c in children)
        rollup_out = sum(c.tokens_out for c in children)
        rollup_cost = sum(c.cost_usd for c in children)
        r = s.query(Run).filter(Run.id == run_id).first()
        if r:
            r.status = status
            r.output = final if isinstance(final, dict) else {"output": final}
            r.error = err
            r.tokens_in = rollup_in
            r.tokens_out = rollup_out
            r.cost_usd = rollup_cost
            r.ended_at = datetime.utcnow()
            _wf_gh_issue_number = getattr(r, "github_issue_number", None)
            from .events import _run_to_ws_dict
            _wf_run_ws_data = _run_to_ws_dict(r)
    score_run_terminal(run_id)
    # WS broadcast — push terminal state to all connected clients
    try:
        if _wf_run_ws_data:
            from .events import ws_broadcast
            asyncio.create_task(ws_broadcast("run_update", _wf_run_ws_data))
    except Exception:
        pass
    try:
        if _wf_gh_issue_number:
            from .github_sync import update_run_issue
            asyncio.create_task(update_run_issue(
                issue_number=_wf_gh_issue_number,
                status=status,
                tokens_in=rollup_in,
                tokens_out=rollup_out,
                cost_usd=rollup_cost,
                error=err,
                run_id=run_id,
            ))
    except Exception:
        pass
    budget_snap = hops.get(root_id)
    await bus.publish(run_id, "node_end", {"final": "<wf done>",
                                           "tokens_in": rollup_in,
                                           "tokens_out": rollup_out,
                                           "cost_usd": rollup_cost,
                                           "budget": budget_snap},
                      node_id="__workflow__")
    await bus.publish(run_id, "done",
                      {"status": status, "budget": budget_snap,
                       "limit_reached": limit_reached})
    await bus.close(run_id)
    # Only the root workflow that owns the counter clears it.
    if own_counter:
        hops.clear(root_id)
    return {"run_id": run_id, "status": status, "output": final, "error": err,
            "tokens_in": rollup_in, "tokens_out": rollup_out, "cost_usd": rollup_cost,
            "budget": budget_snap, "limit_reached": limit_reached}


# ---------------- entry-point helpers ----------------

class TargetBudgetExceeded(Exception):
    """Raised when a new run dispatch would violate a Target's hard budget."""


class AgentChainLoopError(Exception):
    """Raised when an agent-to-agent dispatch chain would exceed agent_chain_max_hops."""


def _resolve_hop_count(caller_run_id: str | None) -> int:
    """Compute a new run's hop_count from its caller's, enforcing the hop cap.

    A run started with no ``caller_run_id`` (human/UI/task-initiated) is hop 0.
    A run dispatched via ``run_agent_async``/``run_workflow_async`` inherits
    caller.hop_count + 1. Raises AgentChainLoopError once the chain would
    exceed the configured cap — this is the loop guard for runaway A→B→A
    "call me back" cycles. Callers should surface the error to whoever
    dispatched the run (HTTPException) and alert the sysadmin bot.

    If the caller run belongs to an Agents Flow (``Run.flow_slug``) that has
    its own ``AgentFlow.max_hops`` set, that overrides the global
    ``agent_chain_max_hops`` setting (default 8) — same precedence as
    ``_check_flow_completion``, so a flow-scoped limit raised above the
    global default doesn't get pre-empted by this dispatch-time guard.
    """
    if not caller_run_id:
        return 0
    from .security import get_setting
    from ..models import Run, AgentFlow
    with session_scope() as s:
        caller = s.query(Run).filter(Run.id == caller_run_id).first()
        caller_hops = caller.hop_count if caller else 0
        flow_max_hops = None
        if caller and caller.flow_slug:
            flow = (s.query(AgentFlow)
                    .filter(AgentFlow.slug == caller.flow_slug, AgentFlow.deleted_at.is_(None))
                    .first())
            flow_max_hops = flow.max_hops if flow else None
    max_hops = flow_max_hops if flow_max_hops is not None else get_setting("agent_chain_max_hops", 8)
    new_hops = caller_hops + 1
    if new_hops > max_hops:
        raise AgentChainLoopError(
            f"agent chain depth {new_hops} would exceed "
            f"{'flow' if flow_max_hops is not None else 'agent_chain_max_hops'} limit "
            f"({max_hops}) (caller_run_id={caller_run_id}) — this looks like a runaway "
            f"agent-to-agent call loop rather than legitimate depth; raise the limit "
            f"in the flow's settings or in global Settings if not."
        )
    return new_hops


def _check_target_budget(target_id: str | None) -> None:
    """If target_id refers to a Target with enforce_budget=true and the rolled-up
    spend already exceeds its caps, raise TargetBudgetExceeded.

    Called at dispatch time only — once a run is in flight the existing
    hops/wait_run mechanisms take over."""
    if not target_id:
        return
    with session_scope() as s:
        from ..models import Run, Target
        t = s.query(Target).filter(Target.id == target_id,
                                   Target.deleted_at.is_(None)).first()
        if t is None or not t.enforce_budget:
            return
        if t.status != "active":
            raise TargetBudgetExceeded(
                f"target '{t.slug}' is {t.status} — no new runs accepted")
        if t.budget_tokens is None and t.budget_usd is None:
            return
        runs = s.query(Run).filter(Run.target_id == target_id).all()
        tot_tok = sum((r.tokens_in or 0) + (r.tokens_out or 0) for r in runs)
        tot_usd = sum(r.cost_usd or 0.0 for r in runs)
        if t.budget_tokens is not None and tot_tok >= t.budget_tokens:
            raise TargetBudgetExceeded(
                f"target '{t.slug}' tokens {tot_tok:,} >= cap {t.budget_tokens:,}")
        if t.budget_usd is not None and tot_usd >= t.budget_usd:
            raise TargetBudgetExceeded(
                f"target '{t.slug}' cost ${tot_usd:.2f} >= cap ${t.budget_usd:.2f}")


def start_agent_run_bg(agent_slug: str, user_input: str, *,
                       initiator_kind: str = "agent_run",
                       initiator_id: str | None = None,
                       parent_run_id: str | None = None,
                       hop_count: int = 0,
                       node_id: str | None = None,
                       target_id: str | None = None,
                       session_id: str | None = None,
                       notion_task_id: str | None = None,
                       raw_cli_prompt: bool = False) -> str:
    """Schedule an agent run in the background; return its run_id."""
    if target_id is None and parent_run_id is None:
        raise ValueError("target_id is required for top-level agent runs")
    _check_target_budget(target_id)
    with session_scope() as s:
        from ..models import Agent, Model, Target as _Target
        agent = s.query(Agent).filter(Agent.slug == agent_slug).first()
        model_slug = None
        if agent and agent.model_slug:
            m = s.query(Model).filter(Model.slug == agent.model_slug).first()
            if m:
                model_slug = m.slug
        # Resolve target_slug from the actual Target row (not the agent slug).
        _run_target_slug = agent_slug  # fallback
        if target_id:
            _t = s.query(_Target).filter(_Target.id == target_id).first()
            if _t:
                _run_target_slug = _t.slug
        _run_input = {"input": user_input}
        if notion_task_id:
            _run_input["notion_task_id"] = notion_task_id
        # A run resuming a session_id may have to sit behind the per-session
        # lock (_acquire_session_lock) before it actually starts — surface
        # that as "queued" rather than lying with "running" the instant it's
        # dispatched. Runs with no session_id never contend for that lock, so
        # they go straight to "running". _on_state_change (below) flips
        # queued -> running the moment the lock is actually acquired.
        _initial_status = "queued" if session_id else "running"
        r = Run(kind="agent", target_slug=_run_target_slug, status=_initial_status,
                input=_run_input,
                initiator_kind=initiator_kind,
                initiator_id=initiator_id,
                parent_run_id=parent_run_id,
                hop_count=hop_count,
                node_id=node_id,
                target_id=target_id,
                model_slug=model_slug,
                source_slug=agent_slug,
                session_id=session_id,
                notion_task_id=notion_task_id)
        s.add(r); s.flush()
        _record_flow_hop(s, r, agent_slug, parent_run_id, session_id=session_id)
        rid = r.id
        from .events import _run_to_ws_dict
        _start_ws_data = _run_to_ws_dict(r)

    # WS broadcast — push the initial state ("queued" or "running") immediately
    try:
        from .events import ws_broadcast
        loop = asyncio.get_running_loop()
        loop.create_task(ws_broadcast("run_update", _start_ws_data))
    except Exception:
        pass

    def _on_state_change(state: str) -> None:
        """run_agent's on_state hook (see its docstring): "waiting" means we're
        still parked behind the session lock — the row is already "queued", so
        nothing to do. "processing" means the lock was just acquired and the
        CLI turn is actually starting — flip the DB row + broadcast so the UI
        stops showing this run as merely queued."""
        if state != "processing":
            return
        try:
            with session_scope() as fs:
                row = fs.query(Run).filter(Run.id == rid).first()
                if row is None or row.status != "queued":
                    return
                row.status = "running"
                fs.flush()
                from .events import _run_to_ws_dict as _to_ws
                data = _to_ws(row)
        except Exception:
            _exec_log.warning("on_state queued->running flip failed for run %s",
                              rid, exc_info=True)
            return
        try:
            from .events import ws_broadcast as _wsb
            asyncio.get_running_loop().create_task(_wsb("run_update", data))
        except Exception:
            pass

    async def _go():
        try:
            await run_agent(agent_slug, user_input, run_id=rid,
                            initiator_kind=initiator_kind,
                            initiator_id=initiator_id,
                            session_id=session_id,
                            target_id=target_id,
                            notion_task_id=notion_task_id,
                            raw_cli_prompt=raw_cli_prompt,
                            on_state=_on_state_change if session_id else None)
        finally:
            await bus.publish(rid, "done", {})
            await bus.close(rid)

    # GitHub sync: create issue for this run (fire-and-forget)
    try:
        from .github_sync import create_run_issue
        _gh_rid = rid
        _gh_target_id = target_id
        _gh_agent_slug = agent_slug
        _gh_model_slug = model_slug
        _gh_input = str(user_input)[:200]

        async def _create_agent_run_issue():
            _t_issue_num = None
            try:
                with session_scope() as ss:
                    from ..models import Target as _GT
                    gt = ss.query(_GT).filter(_GT.id == _gh_target_id).first()
                    if gt:
                        _t_issue_num = getattr(gt, "github_issue_number", None)
            except Exception:
                pass
            issue_num = await create_run_issue(
                run_id=_gh_rid,
                agent_slug=_gh_agent_slug,
                model_slug=_gh_model_slug,
                target_issue_number=_t_issue_num,
                target_name="",
                input_summary=_gh_input,
            )
            if issue_num:
                from .security import get_setting
                repo = get_setting("github_repo", "") or ""
                with session_scope() as ss:
                    from ..models import Run as _RM
                    from sqlalchemy import update as _upd
                    ss.execute(_upd(_RM).where(_RM.id == _gh_rid).values(
                        github_issue_number=issue_num,
                        github_issue_url=f"https://github.com/{repo}/issues/{issue_num}",
                    ))

        asyncio.create_task(_create_agent_run_issue())
    except Exception:
        pass

    asyncio.create_task(_go())
    return rid


def start_workflow_run_bg(workflow_slug: str, user_input: Any, *,
                          initiator_kind: str = "workflow_run",
                          initiator_id: str | None = None,
                          parent_run_id: str | None = None,
                          hop_count: int = 0,
                          target_id: str | None = None) -> str:
    if target_id is None:
        raise ValueError("target_id is required for workflow runs")
    _check_target_budget(target_id)
    with session_scope() as s:
        from ..models import Target as _Target
        # Resolve target_slug from the actual Target row.
        _wf_target_slug = workflow_slug  # fallback
        _t = s.query(_Target).filter(_Target.id == target_id).first()
        if _t:
            _wf_target_slug = _t.slug
        r = Run(kind="workflow", target_slug=_wf_target_slug, status="running",
                input={"input": user_input},
                initiator_kind=initiator_kind,
                initiator_id=initiator_id or workflow_slug,
                parent_run_id=parent_run_id,
                hop_count=hop_count,
                target_id=target_id,
                source_slug=workflow_slug)
        s.add(r); s.flush()
        rid = r.id
        from .events import _run_to_ws_dict
        _start_ws_data = _run_to_ws_dict(r)

    # WS broadcast — push "running" state immediately
    try:
        from .events import ws_broadcast
        loop = asyncio.get_running_loop()
        loop.create_task(ws_broadcast("run_update", _start_ws_data))
    except Exception:
        pass

    async def _go():
        try:
            await run_workflow(workflow_slug, user_input,
                               run_id=rid,
                               initiator_kind=initiator_kind,
                               initiator_id=initiator_id or workflow_slug,
                               target_id=target_id)
        finally:
            await bus.close(rid)

    # GitHub sync: create issue for this workflow run (fire-and-forget)
    try:
        from .github_sync import create_run_issue
        _gh_wrid = rid
        _gh_wtarget_id = target_id
        _gh_wf_slug = workflow_slug
        _gh_winput = str(user_input)[:200]

        async def _create_wf_run_issue():
            _t_issue_num = None
            try:
                with session_scope() as ss:
                    from ..models import Target as _GT
                    gt = ss.query(_GT).filter(_GT.id == _gh_wtarget_id).first()
                    if gt:
                        _t_issue_num = getattr(gt, "github_issue_number", None)
            except Exception:
                pass
            issue_num = await create_run_issue(
                run_id=_gh_wrid,
                agent_slug=_gh_wf_slug,
                model_slug=None,
                target_issue_number=_t_issue_num,
                target_name="",
                input_summary=_gh_winput,
            )
            if issue_num:
                from .security import get_setting
                repo = get_setting("github_repo", "") or ""
                with session_scope() as ss:
                    from ..models import Run as _RM
                    from sqlalchemy import update as _upd
                    ss.execute(_upd(_RM).where(_RM.id == _gh_wrid).values(
                        github_issue_number=issue_num,
                        github_issue_url=f"https://github.com/{repo}/issues/{issue_num}",
                    ))

        asyncio.create_task(_create_wf_run_issue())
    except Exception:
        pass

    asyncio.create_task(_go())
    return rid
