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
from typing import Any

_exec_log = logging.getLogger("ap.executor")

from sqlalchemy.orm import Session

from ..db import session_scope
from ..models import Agent, Run, Workflow
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
    # Defaults ON (unlike other opt-in perms) per current rollout — flip to False
    # per-agent via the "Share network" permission checkbox to opt a given agent out.
    params["share_network"] = bool(permissions.get("share_network", True))
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
    params["cwd"] = _aw_base
    params["mount_cwd"] = bool(permissions.get("workspace_access", True))
    # /opt is now the cwd, so drop any redundant --add-dir for it (keep e.g. /tmp).
    params["add_dirs"] = [d for d in (params.get("add_dirs") or []) if d != _aw_base]
    return {"provider": provider, "model_id": model_id, "model_slug": model_slug,
            "params": params,
            "system_prompt": system_prompt,
            "tool_specs": list(agent.tool_specs or []),
            "skill_slugs": list(agent.skill_slugs or []),
            "verbose_replies": bool(permissions.get("verbose_replies", False))}


async def _notify_kanban_run_done(*, run_id: str, agent_slug: str,
                                   notion_task_id: str, status: str, text: str) -> None:
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
                         },
                         headers=headers)
    except Exception:
        pass


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
    attach: bool = False,
    skip_auto_compact: bool = False,
) -> dict[str, Any]:
    """Run an agent and return ``{run_id, text, status, error, tokens_in, tokens_out}``.

    ``run_id``      attach to existing row (no parent set)
    ``event_run_id`` publish events on this id too (so workflows roll up nicely)
    ``attach``      re-attach to an already-running container via its Redis Stream
                    instead of launching a new one (platform-restart recovery).
    ``skip_auto_compact`` internal — set on the nested "/compact" call itself
                    so it doesn't try to trigger another compaction of itself.
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
                    source_slug=agent_slug)
            s.add(r); s.flush()
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
                        source_slug=agent_slug)
                s.add(r)

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

    # Auto-compact: a resumed session that's grown past the configured
    # threshold gets a "/compact" turn first — same session_id, its own Run
    # row (initiator_kind="auto_compact") — before this turn's real message
    # is processed. Settings → General → "Auto-compact threshold (tokens)";
    # 0 disables. Guarded by skip_auto_compact so the compact call itself
    # doesn't try to compact itself.
    auto_compacted = False
    if session_id and not skip_auto_compact and user_input.strip() != "/compact":
        from .security import get_setting
        try:
            threshold = int(get_setting("auto_compact_threshold_tokens", 500_000) or 0)
        except (TypeError, ValueError):
            threshold = 500_000
        if threshold > 0:
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
                )
                auto_compacted = True

    text = ""
    # The concluding answer — the assistant text emitted AFTER the last tool
    # call. Progress narration between tool calls ("Let me search…", "Perfect!
    # Now…") is process, not answer, and would flood a Telegram chat if each
    # were delivered as its own bubble. We reset this on every tool_call so only
    # the trailing message survives; `text` keeps the full transcript for the
    # run history / View Progress.
    reply_text = ""
    tin = tout = 0
    cost = 0.0
    err: str | None = None
    _t_run_start = _time.perf_counter()
    _t_llm_invoke: float | None = None
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
        messages: list[dict] = []
        if sys_blocks:
            messages.append({"role": "system", "content": "\n\n".join(sys_blocks)})
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
                            if runtime.get("verbose_replies"):
                                if reply_text.strip() and not reply_text.endswith("\n\n"):
                                    reply_text += "\n\n"
                            else:
                                reply_text = ""
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
            from .events import _run_to_ws_dict
            _run_ws_data = _run_to_ws_dict(r)
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
            if output_text:
                from ..api.telegram import deliver_recovered_run
                await deliver_recovered_run(run_id, output_text)
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
             r.target_id, r.session_id, r.kind)
            for r in s.query(Run).filter(Run.status == "running").all()
        ]
    if not orphans:
        return

    reattaching = cancelled = 0
    for run_id, slug, user_input, target_id, session_id, kind in orphans:
        if slug and kind == "agent":
            # Never cancel here — hand off to a task that waits for the stream.
            reattaching += 1
            asyncio.create_task(
                _reattach_or_wait(run_id, slug, user_input, target_id, session_id),
                name=f"recover-{run_id}",
            )
        else:
            with session_scope() as s:
                r = s.query(Run).filter(Run.id == run_id).first()
                if r and r.status == "running":
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
                       node_id: str | None = None,
                       target_id: str | None = None,
                       session_id: str | None = None,
                       notion_task_id: str | None = None) -> str:
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
        r = Run(kind="agent", target_slug=_run_target_slug, status="running",
                input=_run_input,
                initiator_kind=initiator_kind,
                initiator_id=initiator_id,
                parent_run_id=parent_run_id,
                node_id=node_id,
                target_id=target_id,
                model_slug=model_slug,
                source_slug=agent_slug)
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
            await run_agent(agent_slug, user_input, run_id=rid,
                            initiator_kind=initiator_kind,
                            initiator_id=initiator_id,
                            session_id=session_id,
                            target_id=target_id,
                            notion_task_id=notion_task_id)
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
