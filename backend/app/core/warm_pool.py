"""warm_pool.py — opt-in persistent "warm container" mode for the claude CLI.

Entirely gated behind ``AP_WARM_CONTAINER=1`` (default OFF — see `enabled()`).
When off, nothing in this module is ever invoked; `cli.py` keeps using the
existing per-turn `docker run --rm` path byte-for-byte. Only the "claude" CLI
is supported — see Target `agent-docker-coldstart-review` for the full design
(memory: project_persistent_claude_container_streamjson.md).

Shape: one persistent container per agent (`aw-warm-<agent_id>`), running a
single long-lived `claude --input-format stream-json --output-format
stream-json` process fed over a FIFO (`agent-images/shared/aw-warm-wrapper` +
`aw-warm-relay.py`). The container is labeled with a sha256 "epoch hash" of
everything frozen at spawn time (system prompt, model, tools, mounts,
resolved mcp.json, image tag, gateway-config hash). A dispatch whose epoch
hash no longer matches the running container's label triggers a fresh spawn
+ a background drain of the stale one — never a `docker kill`/`docker stop`
(that's `kill_run`'s job, cli.py:84, and must stay completely separate; see
`drain()`'s docstring).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import shlex
import time
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("ap.warm_pool")

WARM_LABEL = "aw.warm"
AGENT_ID_LABEL = "aw.agent_id"
EPOCH_LABEL = "aw.epoch"
TOKEN_LABEL = "aw.warm_token"

BASE_DIR = Path(os.environ.get("AW_BASE_DIR", "/opt/agentic-workspace"))
_SHARED_DIR = BASE_DIR / "repos" / "agents-platform" / "agent-images" / "shared"
WRAPPER_HOST_PATH = _SHARED_DIR / "aw-warm-wrapper"
RELAY_HOST_PATH = _SHARED_DIR / "aw-warm-relay.py"

# 6h TTL backstop is enforced INSIDE the container by the wrapper itself
# (self-tracked elapsed time, hard kill of its own child claude process) —
# this constant exists here only so callers/tests can reference the same
# number; agents-platform never polls or enforces it from the outside.
WARM_TTL_S = 21600


def enabled() -> bool:
    return os.environ.get("AP_WARM_CONTAINER") == "1"


def warm_container_name(agent_id: str) -> str:
    return f"aw-warm-{agent_id}"


def compute_epoch_hash(
    *,
    system_prompt: str | None,
    model: str | None,
    tools: list[str] | None,
    mounts: list[str] | None,
    mcp_config_hash: str | None,
    image: str | None,
    gateway_config_hash: str | None,
) -> str:
    """Everything frozen the instant a warm container is spawned, hashed
    together. A dispatch recomputes this from data it already has in hand
    (the Agent/AgentConfig row the executor already loaded, plus the
    resolved mcp config file it already wrote) — no extra I/O beyond what
    the ephemeral path already does today."""
    payload = {
        "system_prompt": system_prompt or "",
        "model": model or "",
        "tools": sorted(tools or []),
        "mounts": sorted(mounts or []),
        "mcp_config_hash": mcp_config_hash or "",
        "image": image or "",
        "gateway_config_hash": gateway_config_hash or "",
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def hash_file(path: str | os.PathLike | None) -> str:
    """sha256 of a file's contents, or "" if it doesn't exist — used to fold
    the resolved per-agent mcp.json (incl. gateway token) into the epoch
    hash without re-resolving MCP servers ourselves."""
    if not path:
        return ""
    p = Path(path)
    mcp_json = p / "mcp.json" if p.is_dir() else p
    try:
        return hashlib.sha256(mcp_json.read_bytes()).hexdigest()
    except OSError:
        return ""


async def _docker(*args: str, timeout: float = 20.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", "docker command timed out"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def inspect_labels(name: str) -> dict[str, str] | None:
    """Return the container's labels, or None if it doesn't exist."""
    rc, out, _ = await _docker("inspect", "-f", "{{json .Config.Labels}}", name)
    if rc != 0:
        return None
    try:
        return json.loads(out.strip()) or {}
    except json.JSONDecodeError:
        return {}


async def is_running(name: str) -> bool:
    rc, out, _ = await _docker("inspect", "-f", "{{.State.Running}}", name)
    return rc == 0 and out.strip() == "true"


async def drain(name: str) -> None:
    """Ask a warm container to exit on its own — after its current turn (if
    any) finishes (uncapped wait) or within ~15s if idle. This is a flag
    file, NOT a signal: `docker exec <name> touch /home/ubuntu/.aw-warm/drain`. The in-container
    wrapper (aw-warm-wrapper) polls for that file and exits 0 by itself.

    Deliberately does NOT call `docker kill`/`docker stop` — those belong
    exclusively to `kill_run` (cli.py:84), the hard-abort path, which must
    stay pure SIGKILL with zero graceful behavior (see its docstring: a
    graceful variant was tried and reverted after the 2026-07-09 incident
    where a "gracefully" cancelled container survived 16+ minutes). Mixing
    the two channels was explicitly rejected by product (Target
    agent-docker-coldstart-review, 2026-07-24 correction) — keep this
    function's implementation free of "kill"/"stop" verbs against docker,
    forever; a CI test (test_warm_drain_separation.py) greps for exactly
    that.
    """
    rc, _, err = await _docker("exec", name, "touch", "/home/ubuntu/.aw-warm/drain")
    if rc != 0:
        log.warning("warm_pool.drain: docker exec touch /home/ubuntu/.aw-warm/drain failed for %s: %s", name, err)


BuildArgv = Callable[[str, str, str], list[str]]


async def get_or_create(*, agent_id: str, epoch_hash: str, build_argv: BuildArgv) -> tuple[str, str]:
    """Return (container_name, warm_token) for a running warm container whose
    epoch label matches epoch_hash — reusing it if so, otherwise draining any
    stale one (mismatched epoch, or present-but-dead) and spawning a fresh
    one under the SAME stable name (`aw-warm-<agent_id>`).

    ``build_argv(name, epoch_hash, warm_token)`` must return the full
    ``["docker", "run", "-d", ...]`` argv for a fresh container.
    """
    name = warm_container_name(agent_id)
    labels = await inspect_labels(name)
    if labels is not None:
        if labels.get(EPOCH_LABEL) == epoch_hash and await is_running(name):
            return name, labels.get(TOKEN_LABEL, "")
        # Stale — free the stable name immediately so the fresh spawn below
        # can take it, then drain the old one in the background. Draining is
        # uncapped by design and must never block this dispatch.
        stale_name = f"{name}-draining-{int(time.time())}"
        rc, _, err = await _docker("rename", name, stale_name)
        if rc == 0:
            asyncio.create_task(drain(stale_name), name=f"warm-drain-{stale_name}")
        else:
            log.warning("warm_pool: rename of stale %s failed (%s) — force-removing instead",
                       name, err.strip())
            await _docker("rm", "-f", name)

    token = secrets.token_hex(16)
    argv = build_argv(name, epoch_hash, token)
    assert argv[:2] == ["docker", "run"], "build_argv must return a `docker run ...` argv"
    rc, out, err = await _docker(*argv[2:], timeout=60.0)
    if rc != 0:
        raise RuntimeError(f"warm_pool: failed to spawn warm container {name}: {err.strip()}")
    await _wait_ready(name)
    return name, token


async def _wait_ready(name: str, timeout_s: float = 10.0) -> None:
    """Bounded, coarse wait for the wrapper's `/home/ubuntu/.aw-warm/ready` marker right after
    spawning a brand-new warm container — the claude process needs a moment
    to boot the first time. One-time cost on creation only (same order of
    cost as today's per-turn cold start), NOT the per-request PID polling
    the design explicitly forbids — that rule is about polling
    agents-platform/mcp-gateway/awserv on every turn, not this one-off
    readiness check on the rare turn that actually spawns a container.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rc, _, _ = await _docker("exec", name, "test", "-f", "/home/ubuntu/.aw-warm/ready")
        if rc == 0:
            return
        await asyncio.sleep(0.3)
    log.warning("warm_pool: %s did not report /home/ubuntu/.aw-warm/ready within %.0fs — proceeding anyway",
               name, timeout_s)


def _sh_quote(s: str) -> str:
    return shlex.quote(s)


async def dispatch_turn(*, name: str, run_id: str, prompt: str) -> None:
    """Feed one turn's prompt into the warm container's FIFO.

    Writes /home/ubuntu/.aw-warm/current_run_id FIRST (so the relay tags the very next lines it
    reads with the right Redis stream key), then writes the stream-json
    payload into the FIFO. The relay (aw-warm-relay.py) publishes to
    `run:{run_id}:events` with the exact schema aw-connector-redis uses, so
    cli.py's existing Redis-consumption path (`consume_stream_into_queue`)
    needs no changes to read a warm turn's output.
    """
    rc, _, err = await _docker(
        "exec", "-i", name, "sh", "-c",
        f"printf '%s' {_sh_quote(run_id)} > /home/ubuntu/.aw-warm/current_run_id",
    )
    if rc != 0:
        raise RuntimeError(f"warm_pool.dispatch_turn: failed to set current run id on {name}: {err.strip()}")

    payload = json.dumps({"type": "user", "message": {"role": "user", "content": prompt}})
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", "-i", name, "sh", "-c", "cat > /home/ubuntu/.aw-warm/fifo_in",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err_b = await proc.communicate((payload + "\n").encode("utf-8"))
    if proc.returncode != 0:
        raise RuntimeError(
            f"warm_pool.dispatch_turn: failed to write turn into {name}'s fifo: "
            f"{err_b.decode(errors='replace').strip()}"
        )


async def current_epoch_for_agent(agent_id: str) -> str | None:
    """Recompute agent_id's CURRENT epoch hash straight from its live
    Agent/AgentConfig row — used only by `reconcile_on_boot` (a one-time
    startup sweep) to decide whether an already-running warm container is
    still valid to adopt. Mirrors the exact inputs
    `CliLLM._warm_get_or_create` hashes for a real dispatch, resolved via
    the same `_agent_to_runtime` the executor already uses for every run.
    Returns None if the agent no longer exists.
    """
    from ..db import session_scope
    from ..models import Agent
    from .executor import _agent_to_runtime
    from .tools.docker_agent import REGISTRY, IMAGE_PREFIX

    with session_scope() as s:
        agent = s.query(Agent).filter(Agent.id == agent_id, Agent.deleted_at.is_(None)).first()
        if agent is None:
            return None
        runtime = _agent_to_runtime(s, agent)
        params = runtime.get("params") or {}
        system_prompt = agent.system_prompt or ""
        mounts: list[str] = list(params.get("add_dirs") or [])
        cwd = params.get("cwd")
        if cwd and params.get("mount_cwd", True):
            mounts.append(cwd)
        mcp_config_hash = hash_file(params.get("docker_mcp_config_dir"))

    image = f"{REGISTRY}/{IMAGE_PREFIX}-claude:latest"
    allowed = params.get("allowed_tools") or []
    disallowed = params.get("disallowed_tools") or []
    return compute_epoch_hash(
        system_prompt=system_prompt,
        model=params.get("model"),
        tools=[*allowed, *(f"!{t}" for t in disallowed)],
        mounts=mounts,
        mcp_config_hash=mcp_config_hash,
        image=image,
        gateway_config_hash=None,
    )


LiveEpochLookup = Callable[[str], Awaitable[str | None]]


async def reconcile_on_boot(live_epoch_for_agent: LiveEpochLookup) -> None:
    """One-time agents-platform startup sweep (design constraint #7): adopt
    any warm container whose epoch label still matches its agent's CURRENT
    epoch hash (nothing to do — the next dispatch will find and reuse it via
    `get_or_create`); drain everything else (agent deleted/reconfigured while
    the platform was down, orphaned by a crash, ...).

    ``live_epoch_for_agent(agent_id)`` must return the agent's current epoch
    hash, or None if it can no longer be resolved (treated as stale).
    """
    rc, out, err = await _docker("ps", "--filter", f"label={WARM_LABEL}=1", "--format", "{{.Names}}")
    if rc != 0:
        log.warning("warm_pool.reconcile_on_boot: docker ps failed: %s", err.strip())
        return
    names = [n for n in out.splitlines() if n.strip()]
    if not names:
        log.info("warm_pool.reconcile_on_boot: no warm containers found")
        return
    for name in names:
        labels = await inspect_labels(name) or {}
        agent_id = labels.get(AGENT_ID_LABEL)
        epoch = labels.get(EPOCH_LABEL)
        current = await live_epoch_for_agent(agent_id) if agent_id else None
        if agent_id and current and current == epoch:
            log.info("warm_pool.reconcile_on_boot: adopting %s (epoch matches current agent config)", name)
            continue
        log.info("warm_pool.reconcile_on_boot: draining %s (agent_id=%s epoch=%s current=%s)",
                 name, agent_id, epoch, current)
        await drain(name)
