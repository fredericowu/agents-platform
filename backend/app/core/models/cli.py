"""Invoke a CLI agent inside an isolated Docker container.

All runs go through ``docker_agent.build_docker_argv`` — there is no direct
(non-Docker) execution path. This ensures consistent isolation, credential
mounting, and per-run cwd for session persistence regardless of which agent
or model is in use.

When ``stream_json=True`` (default for the "claude" CLI) we use
``--output-format stream-json`` to get *every* event emitted by the agentic
loop — thinking blocks, tool_use blocks, tool_result blocks, and final text.
Each event is mapped to a ``ChatChunk`` with an ``event`` payload, so the
platform's executor can publish them as first-class run_events for the UI.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any, AsyncIterator

log = logging.getLogger("ap.cli")

from . import BaseLLM, ChatChunk

# ContextVar set by the executor right before astream() — lets us register the
# subprocess under the correct run id so /runs/:id/cancel can kill it.
current_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_run_id", default=None)

# run_id → set of live subprocess.Process instances
_PROCS: dict[str, set[asyncio.subprocess.Process]] = {}
_PROCS_LOCK = asyncio.Lock()

# run_id -> the ACTUAL docker container name currently launched for it. This
# is the ground truth for "what container is running right now for this
# run_id" — populated the instant `_astream_once()` computes the name (before
# `docker run`), cleared when that call's `finally:` block tears down. kill_run()
# checks this FIRST, before falling back to any DB-derived guess: a run's own
# container name can't be reconstructed reliably from the DB while it's still
# in flight (`Run.session_id` isn't written until the run finishes — see
# 2026-07-23 fix, bot aw-17 — so a session-anchor lookup keyed off the row
# being cancelled is blind at the exact moment /abort needs it most).
_RUN_CONTAINER_NAMES: dict[str, str] = {}


async def _register(run_id: str, proc: asyncio.subprocess.Process) -> None:
    async with _PROCS_LOCK:
        _PROCS.setdefault(run_id, set()).add(proc)


async def _unregister(run_id: str, proc: asyncio.subprocess.Process) -> None:
    async with _PROCS_LOCK:
        _PROCS.get(run_id, set()).discard(proc)
        if run_id in _PROCS and not _PROCS[run_id]:
            del _PROCS[run_id]


# container_name -> lock serializing every astream() call that would launch
# `docker run --name <that name>`. The name is deterministic per session
# (aw-run-<resume_run_id or run_id>), so two turns of the SAME session firing
# concurrently (duplicate webhook delivery, a flow wakeup racing a live turn,
# etc.) used to both pass the `docker rm -f` guard and then race each other
# on `docker run` — the loser died instantly with "Conflict... already in
# use" and the run was marked status=error with zero output (see 2026-07-22
# incident). Holding this lock across the whole cleanup→run→wait→unregister
# span turns that race into a queue: the second turn simply waits for the
# first turn's container to be gone before it gets its shot at the name.
_CONTAINER_LOCKS: dict[str, asyncio.Lock] = {}
_CONTAINER_LOCKS_LOCK = asyncio.Lock()


async def _container_lock(name: str) -> asyncio.Lock:
    async with _CONTAINER_LOCKS_LOCK:
        lock = _CONTAINER_LOCKS.get(name)
        if lock is None:
            lock = asyncio.Lock()
            _CONTAINER_LOCKS[name] = lock
        return lock


async def kill_run(run_id: str) -> int:
    """Stop run_id's container immediately — `docker kill` with the default
    signal (SIGKILL), not SIGTERM. A graceful SIGTERM first was tried and
    dropped: if the claude-CLI process inside doesn't exit promptly (mid
    tool-call, ignoring the signal, etc.) the `--rm` container never gets
    torn down and keeps burning tokens/resources indefinitely even though the
    run is marked 'cancelled' in the DB (see 2026-07-09 incident: a cancelled
    run's container survived 16+ minutes until killed by hand). Cancel is a
    deliberate user action — there's nothing worth flushing gracefully, so go
    straight to SIGKILL. Always issues `docker kill` by container name, since
    a run recovered after an AP restart (Redis-stream survival) has no local
    subprocess handle but is still reachable by its deterministic name.
    Returns the count of processes/containers signalled.

    IMPORTANT — container-name resolution (fix 2026-07-23, bot aw-17): a
    RESUMED session's container is named after the SESSION'S ANCHOR run
    (`container_name_for_run(resume_run_id)`, resolved in executor.py as the
    *first* run to ever use that session_id — see the `resume_run_id`
    assignment in `run_agent()`), never the current turn's own `run_id`. Every
    caller here (this file's callers all pass a turn's own row id) used to
    call `docker kill aw-run-<that turn's id>` — a name that never existed
    for any turn after the session's first — so `/abort` silently no-opped
    against the REAL running container (nonzero exit from `docker kill`,
    swallowed, no error surfaced) while the DB row still flipped to
    'cancelled'/'success' as if the kill had worked.

    Resolution order:
    1. `_RUN_CONTAINER_NAMES[run_id]` — the container name `_astream_once()`
       ACTUALLY launched for this run_id, recorded live in this process. This
       is ground truth and is what /abort needs: the real, currently-running
       container, identified directly rather than reconstructed. Preferred
       because `Run.session_id` in the DB is only written once a run
       *finishes* (see executor.py's `Run(...)` insert vs. the later
       `_r.session_id = _sid` update) — so a DB-only lookup is blind for the
       exact case /abort exists for: a run still in flight right now.
    2. DB session-anchor lookup — best-effort fallback for a run that isn't
       tracked in this process (recovered after an AP restart). Won't help
       for an in-flight run for the reason above, but is harmless to try.

    Also force-removes the container right after killing it (`docker rm -f`)
    instead of leaving dockerd's async `--rm` cleanup to catch up on its own
    — abort must stop things NOW, not "eventually once dockerd gets to it"
    (that lag, up to several minutes under load, is what caused every
    following turn of the session to fail with a name conflict).
    """
    async with _PROCS_LOCK:
        procs = list(_PROCS.get(run_id, set()))
    n = 0
    for p in procs:
        try:
            p.kill()
            n += 1
        except ProcessLookupError:
            pass

    _container_name = _RUN_CONTAINER_NAMES.get(run_id)
    if _container_name:
        log.info("kill_run: run %s resolved to its actually-running container "
                  "%s (live registry)", run_id, _container_name)
    else:
        _container_run_id = run_id
        try:
            from ...db import session_scope
            from ...models import Run as _Run

            with session_scope() as _s:
                _row = _s.query(_Run).filter(_Run.id == run_id).first()
                if _row and _row.session_id:
                    _anchor = (
                        _s.query(_Run)
                        .filter(_Run.session_id == _row.session_id)
                        .order_by(_Run.started_at.asc())
                        .first()
                    )
                    if _anchor:
                        _container_run_id = _anchor.id
        except Exception:
            log.exception(
                "kill_run: anchor-run lookup failed for %s — falling back to "
                "its own id for the container name (may miss the real "
                "container on a resumed session)", run_id,
            )

        from ..tools.docker_agent import container_name_for_run
        _container_name = container_name_for_run(_container_run_id)
        if _container_run_id != run_id:
            log.info("kill_run: run %s not in the live registry — DB anchor "
                      "lookup resolved container %s", run_id, _container_name)
        else:
            log.warning("kill_run: run %s not in the live registry and no DB "
                        "session anchor found — targeting its own id as the "
                        "container name, may miss the real container: %s",
                        run_id, _container_name)

    proc = await asyncio.create_subprocess_exec(
        "docker", "kill", _container_name,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    if await proc.wait() == 0:
        n += 1

    _rm = await asyncio.create_subprocess_exec(
        "docker", "rm", "-f", _container_name,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await _rm.wait()
    return n


class CliLLM(BaseLLM):
    provider = "cli"

    def __init__(
        self,
        model_id: str,
        *,
        cli: str = "claude",
        model: str | None = None,
        cwd: str | None = None,
        add_dirs: list[str] | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        dangerous_skip_permissions: bool = True,
        stream_json: bool = True,
        bare: bool = False,
        append_system_prompt: str | None = None,
        timeout_s: int = 900,
        extra_args: list[str] | None = None,
        docker_creds: bool = True,
        docker_mcp_config_dir: str | None = None,
        agent_id: str | None = None,
        target_id: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        resume_run_id: str | None = None,
        notion_task_id: str | None = None,
        source_device: str | None = None,
        extra_volumes: list[str] | None = None,
        share_network: bool = False,
        mount_cwd: bool = True,
        **_: Any,
    ) -> None:
        self.model_id = model_id
        self.cli = cli
        self.model = model
        self.cwd = cwd
        self.mount_cwd = mount_cwd
        self.add_dirs = list(add_dirs or [])
        self.allowed_tools = list(allowed_tools or [])
        self.disallowed_tools = list(disallowed_tools or [])
        self.dangerous_skip_permissions = dangerous_skip_permissions
        self.stream_json = stream_json
        self.bare = bare
        self.append_system_prompt = append_system_prompt
        self.timeout_s = timeout_s
        self.extra_args = list(extra_args or [])
        self.docker_creds = docker_creds
        self.docker_mcp_config_dir = docker_mcp_config_dir
        self.agent_id = agent_id
        self.target_id = target_id
        self.run_id = run_id
        self.session_id = session_id
        self.resume_run_id = resume_run_id
        self.notion_task_id = notion_task_id
        self.source_device = source_device
        self.extra_volumes = list(extra_volumes or [])
        self.share_network = share_network
        # When set, astream() does NOT launch a container — it re-attaches to the
        # run's durable Redis Stream and replays it (platform-restart recovery).
        self.attach_run_id = _.get("attach_run_id")
        # When set, the prompt is the last user message verbatim — no
        # [SYSTEM]/[USER] framing. Required for CLI slash commands ("/compact"):
        # the claude CLI only recognises them at position 0 of the prompt.
        self.raw_prompt = bool(_.get("raw_prompt"))

    def _build_argv(self, prompt: str, ws_token: str | None = None,
                    redis_url: str | None = None) -> list[str]:
        from ..tools.docker_agent import build_docker_argv, CLI_SPECS

        cli = self.cli if self.cli in CLI_SPECS else "claude"

        # self.cwd is the CLI working directory (always passed as -w so the
        # session project dir stays constant). It is bind-mounted only when
        # mount_cwd is set ("workspace access on"); when off, docker_agent mounts
        # an empty writable tmpfs there instead, so the dir exists without exposing
        # the repo.
        mounts: list[str] = []
        if self.cwd and self.mount_cwd:
            mounts.append(self.cwd)

        extra: list[str] = []
        if self.allowed_tools:
            extra += ["--allowed-tools", ",".join(self.allowed_tools)]
        if self.disallowed_tools:
            extra += ["--disallowed-tools", ",".join(self.disallowed_tools)]
        if self.bare:
            extra.append("--bare")
        if self.append_system_prompt:
            extra += ["--append-system-prompt", self.append_system_prompt]
        extra += self.extra_args

        _extra_env: dict[str, str] = {}
        if self.notion_task_id:
            _extra_env["NOTION_TASK_ID"] = self.notion_task_id
        # AW_RUN_ID is the event-transport key (Redis Stream key / WS path). It
        # MUST be the CURRENT run's id — the consumer in astream() reads by
        # current_run_id. NOT resume_run_id: that only drives the isolated cwd
        # (passed as run_id= below) so the CLI can find the prior session file.
        # Conflating them sent a resume run's events to the OLD run's stream,
        # which the consumer never read → empty run → lost conversation memory.
        if ws_token:
            rid = self.run_id or current_run_id.get() or ""
            _extra_env["AW_RUN_ID"] = rid
            _extra_env["AW_AGENT_TOKEN"] = ws_token
            _ws_host = "127.0.0.1" if self.share_network else "host.docker.internal"
            _extra_env["AW_WS_URL"] = f"ws://{_ws_host}:9123/ws/agent"
        if redis_url:
            rid = self.run_id or current_run_id.get() or ""
            _extra_env["AW_RUN_ID"] = rid
            _extra_env["AW_REDIS_URL"] = redis_url
        # AW_SESSION_ID: always injected (unlike AW_RUN_ID above, not gated on
        # ws/redis streaming) so a resumed session can read its own claude CLI
        # session_id — e.g. `echo $AW_SESSION_ID` — to pass to the
        # clear_session/compact_session MCP tools without extra plumbing.
        if self.session_id:
            _extra_env["AW_SESSION_ID"] = self.session_id
        # AW_SOURCE_DEVICE: which physical/virtual channel originated this turn
        # (e.g. "watch", "iphone", "meta"/glasses, "telegram") — lets an agent
        # tailor its reply (see the aw-apple-watch skill) without parsing the
        # "/aw-apple-watch\nCONTEXT:\n- source: <device>" prompt header itself.
        if self.source_device:
            _extra_env["AW_SOURCE_DEVICE"] = self.source_device
        # Codex's aw-gateway MCP server (defined in the shared $CODEX_HOME/
        # config.toml, not the per-run mcp_codex.toml profile — codex's -p/
        # --profile flag does not layer the mcp_servers table, only the base
        # config.toml is honoured) authenticates via bearer_token_env_var =
        # "MCP_BEARER_AW_GATEWAY". Nothing else forwards that var into the
        # container, so codex agents silently got 0 MCP tools.
        if cli == "codex":
            try:
                _aw_json = Path(os.environ.get("AW_BASE_DIR", "/opt/agentic-workspace")) / "src" / "config" / "aw.json"
                _gw_token = json.loads(_aw_json.read_text()).get("mcp_gateway", {}).get("token") or ""
                if _gw_token:
                    _extra_env["MCP_BEARER_AW_GATEWAY"] = _gw_token
            except Exception:
                pass
        return build_docker_argv(
            cli=cli,
            prompt=prompt,
            mounts=mounts,
            skills=False,
            mcp=False,
            creds=self.docker_creds,
            add_dirs=self.add_dirs,
            env_file=None,
            forward_env=False,
            model=self.model,
            extra_args=extra,
            tag="latest",
            image_override=None,
            mcp_config_dir=self.docker_mcp_config_dir,
            agent_id=self.agent_id,
            target_id=self.target_id,
            run_id=self.resume_run_id or self.run_id,
            session_id=self.session_id,
            extra_docker_env=_extra_env or None,
            ws_mode=ws_token is not None,
            redis_mode=redis_url is not None,
            extra_volumes=self.extra_volumes or None,
            share_network=self.share_network,
            # cwd is always passed as -w. When mount_cwd is off we do NOT bind the
            # repo there; the dir still exists because it's baked into the agent
            # image (mkdir/chown ubuntu in the Dockerfile), so no tmpfs is needed.
            workdir=self.cwd,
        )

    async def astream(self, messages: list[dict], **params: Any) -> AsyncIterator[ChatChunk]:
        """Thin retry wrapper around `_astream_once`. Handles exactly one
        failure shape: the container-name collision described on the launch
        site in `_astream_once` (docker run --name aw-run-<id> raced the
        previous turn's async --rm removal). Nothing is yielded from
        `_astream_once` before that specific RuntimeError can occur (the raise
        happens after the whole read loop drains, gated on `received_events ==
        0`), so re-running it here from scratch is always safe — no partial
        output was ever handed to the caller. Any other exception, or a
        conflict on the retry attempt itself, propagates as-is: this is a
        single free retry for a known-transient race, not a general
        resilience mechanism.

        IMPORTANT: only force-removes the blocking container when `docker
        inspect` shows it's already dead (Exited/Dead/Created) — i.e. a true
        leftover from an async --rm that hadn't landed yet. If it's genuinely
        `running`, that's a DIFFERENT live turn of the same session (e.g. a
        Watch/Glasses-injected turn overlapping a phone-Telegram turn — see
        the 2026-07-22 incident where this forced-removed a still-executing
        sibling and destroyed its in-progress work). In that case we just
        raise: failing fast beats killing someone else's live run. The real
        fix for that overlap is serializing at the dispatch layer (the
        `_chat_lock` now held around `/inject` too), not here.
        """
        yielded = 0
        try:
            async for chunk in self._astream_once(messages, **params):
                yielded += 1
                yield chunk
            return
        except RuntimeError as e:
            _msg = str(e)
            if yielded or self.attach_run_id or "already in use" not in _msg:
                log.info(
                    "cli astream retry skipped (yielded=%d attach_run_id=%s "
                    "conflict_msg=%s) — reraising as-is: %s",
                    yielded, self.attach_run_id, "already in use" in _msg, _msg,
                )
                raise

        _resume_id = self.resume_run_id or self.run_id
        if not _resume_id:
            log.warning("cli astream: name-conflict retry has no resume_id/run_id "
                        "to derive a container name from — reraising as-is: %s", _msg)
            raise RuntimeError(_msg)
        from ..tools.docker_agent import container_name_for_run
        _container_name = container_name_for_run(_resume_id)

        # A single `docker inspect` snapshot here used to decide "live sibling,
        # fail fast" vs "dead, force-remove". That was wrong for the common
        # case: `_CONTAINER_LOCKS` (see `_container_lock()`) already serializes
        # every launch under this name WITHIN THIS PROCESS, so nothing else in
        # this process could have legitimately started a new `docker run
        # --name <this>` while we held that lock — meaning a still-"Running"
        # container we collide with immediately after releasing our own lock
        # can only be OUR OWN just-finished container, mid `--rm` teardown,
        # not a genuine sibling. dockerd's async removal is usually ~1-2s (see
        # the `_held_container_lock` release poll above) but was observed to
        # take 3+ MINUTES in a real incident (2026-07-23, bot aw-17) under
        # load — the finally-block's ~2s poll budget gave up long before that,
        # and this snapshot then misread the still-lingering container as a
        # live sibling, permanently failing every subsequent turn until
        # dockerd finally finished on its own. Poll for a much longer bounded
        # window here (worst-case adds latency to one Telegram reply, which
        # beats an error that keeps recurring turn after turn — "deveria ter
        # enfileirado ao invés de dar erro").
        #
        # Cross-process collisions (a container from BEFORE an AP restart, or
        # the attach_run_id path bypassing the lock — see the module docstring
        # above) are the only way this is a genuine live sibling; those are
        # rare and still correctly fail-fast once this window is exhausted.
        _CONFLICT_POLL_ATTEMPTS = 15
        _CONFLICT_POLL_INTERVAL_S = 2
        _still_running = True
        for _attempt in range(_CONFLICT_POLL_ATTEMPTS):
            _inspect = await asyncio.create_subprocess_exec(
                "docker", "inspect", "-f", "{{.State.Running}}", _container_name,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            _out, _ = await _inspect.communicate()
            if _inspect.returncode != 0 or _out.strip() != b"true":
                _still_running = False
                if _attempt:
                    log.info(
                        "cli container %s finished tearing down after ~%ds of "
                        "conflict-retry polling (was misread as live at first "
                        "snapshot) — removing and retrying",
                        _container_name, _attempt * _CONFLICT_POLL_INTERVAL_S,
                    )
                break
            await asyncio.sleep(_CONFLICT_POLL_INTERVAL_S)

        if _still_running:
            # Still running after the full window — genuinely a live sibling
            # (or dockerd is pathologically stuck). Do not kill it. Fail fast;
            # the caller (executor) surfaces this as a normal run error
            # instead of silently destroying real work.
            log.warning(
                "cli container name collision against a container (%s) still "
                "reporting Running=true after %ds of polling — treating as a "
                "LIVE sibling, not killing it, failing this turn: %s",
                _container_name, _CONFLICT_POLL_ATTEMPTS * _CONFLICT_POLL_INTERVAL_S, _msg,
            )
            raise RuntimeError(_msg)

        log.warning("cli container name collision against a dead/stale "
                    "container (%s) — removing and retrying once: %s",
                    _container_name, _msg)
        _rm = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", _container_name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await _rm.wait()
        async for chunk in self._astream_once(messages, **params):
            yield chunk

    async def _astream_once(self, messages: list[dict], **params: Any) -> AsyncIterator[ChatChunk]:
        parts: list[str] = []
        for m in messages:
            r = m.get("role", "user")
            c = m.get("content", "")
            if r == "system":
                parts.append(f"[SYSTEM]\n{c}\n")
            elif r == "assistant":
                parts.append(f"[ASSISTANT]\n{c}\n")
            else:
                parts.append(f"[USER]\n{c}\n")
        prompt = "\n".join(parts).strip()
        if self.raw_prompt:
            # Slash commands ("/compact") must be the whole prompt — any framing
            # makes the CLI treat them as ordinary text the model answers to.
            prompt = next((m.get("content", "") for m in reversed(messages)
                           if m.get("role", "user") not in ("system", "assistant")),
                          prompt)

        rid: str | None = current_run_id.get()

        # ── Streaming mode selection ──────────────────────────────────────────
        # Default: read the container's stdout DIRECTLY from the host-side docker
        # process. The CLI runs unwrapped inside docker (--output-format
        # stream-json on stdout); this process captures each line. No in-container
        # aw-connector, no WebSocket hop back through awserv:9123 — which is what
        # contended with the MCP streamable-http handshake on the same port and
        # left runs empty / the container apparently dead.
        #
        # Opt-in legacy WS mode (AP_CLI_WS_STREAM=1): the container is wrapped
        # with aw-connector, which streams stdout back over a WebSocket into a
        # registered queue. Kept for future remote-docker / reconnect scenarios.
        # Re-attach path: when attach_run_id is set the container is already
        # running (or finished) and has been publishing to its durable Redis
        # Stream. We replay that stream instead of launching a new container —
        # this is how a run survives a platform restart.
        attach_run_id = self.attach_run_id

        use_ws = os.environ.get("AP_CLI_WS_STREAM") == "1"
        # Redis is the default durable transport (the WS hop is being retired):
        # the container publishes straight to a Redis Stream that survives a
        # platform restart. Set AP_CLI_REDIS_STREAM=0 to fall back to reading the
        # container's stdout directly (no restart durability).
        use_redis = (not use_ws) and os.environ.get("AP_CLI_REDIS_STREAM", "1") != "0"

        q = None
        proc = None
        monitor_task = None
        register_run = unregister_run = None
        done_event = asyncio.Event()

        if attach_run_id:
            from ..redis_streams import replay_stream_into_queue
            q = asyncio.Queue()
            asyncio.create_task(
                replay_stream_into_queue(attach_run_id, q),
                name=f"redis-replay-{attach_run_id}",
            )
        elif use_ws:
            ws_token = secrets.token_hex(32)
            from ..ws_agent_registry import register_run, unregister_run
            q = register_run(rid or "unknown", ws_token)
            argv = self._build_argv(prompt, ws_token=ws_token)
            stdout_target = asyncio.subprocess.DEVNULL  # events arrive via WS
        elif use_redis:
            # Container publishes directly to Redis Stream via aw-connector-redis.
            # We consume via XREADGROUP into an asyncio.Queue (same interface as WS mode).
            _redis_url = os.environ.get("AP_REDIS_URL", "redis://127.0.0.1:6379/0")
            q = asyncio.Queue()
            # share_network containers join aw-sandbox's netns, so 127.0.0.1 already
            # reaches redis there — only the isolated-bridge containers need the alias.
            if self.share_network:
                argv = self._build_argv(prompt, redis_url=_redis_url)
            else:
                argv = self._build_argv(prompt, redis_url=_redis_url.replace(
                    "127.0.0.1", "host.docker.internal"))  # container sees host via this alias
            stdout_target = asyncio.subprocess.DEVNULL
        else:
            argv = self._build_argv(prompt, ws_token=None)  # no aw-connector wrapper
            stdout_target = asyncio.subprocess.PIPE  # read CLI stdout directly

        stderr_chunks: list[bytes] = []
        stderr_task = None
        _held_container_lock: asyncio.Lock | None = None
        _container_name: str | None = None
        if not attach_run_id:
            # The container name is deterministic (aw-run-<resume_run_id or run_id>)
            # so a RESUMED session reuses the SAME name across every turn (see
            # container_name_for_run) — that's what lets kill_run()/`/abort`
            # `docker kill` it by name with no live process handle, even after
            # an AP restart. It also means two launches under the same name can
            # collide ("Conflict... already in use"): dockerd removes the
            # previous turn's `--rm` container asynchronously, so a back-to-back
            # turn can start before that removal lands. Deliberately NOT
            # pre-checking (`docker rm -f` / `docker inspect`) here — that would
            # tax every single turn with real latency to defend against a
            # collision that's rare. Launch straight away; the retry wrapper
            # (astream(), below) reacts only if this specific launch actually
            # collides.
            _resume_id = self.resume_run_id or self.run_id
            try:
                if _resume_id:
                    from ..tools.docker_agent import container_name_for_run
                    _container_name = container_name_for_run(_resume_id)
                    # Serialize every astream() call targeting this container name.
                    # Without this, two turns of the SAME session firing concurrently
                    # (duplicate webhook delivery, a flow wakeup racing a live turn)
                    # could both race each other on `docker run --name` — the loser
                    # died instantly with "Conflict... already in use", surfacing as
                    # a status=error run with zero output. Holding the lock across
                    # run→wait below (released in the `finally:` block below) turns
                    # that race into a queue.
                    _held_container_lock = await _container_lock(_container_name)
                    await _held_container_lock.acquire()
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=stdout_target,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=os.getcwd(), env={**os.environ},
                    limit=10 * 1024 * 1024,  # raise StreamReader cap for large init lines
                )
            except BaseException:
                # Anything here blew up before the main try/finally below took
                # over lock ownership — release here or the next turn of this
                # session deadlocks forever waiting on a lock nobody will free.
                if _held_container_lock is not None:
                    _held_container_lock.release()
                raise
            # Timing breakdown for agent.docker_ready: this marks the moment
            # the host process for the container actually exists, separating
            # "container create/start" from the CLI's own boot time below.
            yield _meta_chunk("container.started", {})
            if rid:
                await _register(rid, proc)
                if _container_name:
                    # Ground truth for kill_run(): the container name THIS
                    # run_id actually launched, so /abort can target it
                    # directly instead of reconstructing it from the DB (see
                    # kill_run()'s docstring — Run.session_id isn't written
                    # until the run finishes, so a DB lookup is blind for an
                    # in-flight run, exactly when /abort needs it most).
                    _RUN_CONTAINER_NAMES[rid] = _container_name

            # Drain `docker run`'s stderr into a bounded buffer instead of
            # discarding it — needed below to explain a non-zero exit that
            # produced zero CLI events (container never started).
            async def _drain_stderr():
                assert proc.stderr is not None
                try:
                    while True:
                        chunk = await proc.stderr.read(4096)
                        if not chunk:
                            break
                        stderr_chunks.append(chunk)
                except Exception:
                    pass
            stderr_task = asyncio.create_task(_drain_stderr())

            # Redis mode: start background task consuming from the Stream into q
            if use_redis and q is not None:
                from ..redis_streams import consume_stream_into_queue
                asyncio.create_task(
                    consume_stream_into_queue(rid or "unknown", q),
                    name=f"redis-consumer-{rid}",
                )

            # Monitor process exit. In WS/Redis mode, inject the done sentinel if
            # docker dies before the connector sends "done" so astream() doesn't hang.
            async def _proc_monitor():
                rc = await proc.wait()
                if rc != 0:
                    log.warning("cli docker exited rc=%d run=%s", rc, rid)
                if not done_event.is_set() and q is not None:
                    await q.put(None)  # fallback sentinel on unexpected exit
            monitor_task = asyncio.create_task(_proc_monitor())

        # Unified line source: yields each raw CLI JSON line, or None when done.
        async def _next_line() -> str | None:
            if attach_run_id or use_ws or use_redis:
                return await asyncio.wait_for(q.get(), timeout=self.timeout_s)
            assert proc.stdout is not None
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=self.timeout_s)
            if not raw:
                return None  # EOF — container closed stdout
            return raw.decode("utf-8", errors="replace").rstrip("\n")

        tin = tout = 0
        cost = 0.0
        final_text = ""
        received_events = 0
        _first_byte_seen = False

        try:
            while True:
                try:
                    line = await _next_line()
                except asyncio.TimeoutError:
                    if proc is not None:
                        proc.kill()
                    yield _meta_chunk("cli.timeout", {"timeout_s": self.timeout_s})
                    break

                if line is None:  # done sentinel (WS) or stdout EOF (direct)
                    done_event.set()
                    break

                # Timing breakdown for agent.docker_ready: first raw line of
                # any kind (not yet parsed/typed) marks the CLI process as
                # alive — splits "container create/start" (above) from
                # "CLI boot to system.init" (below) instead of one opaque span.
                if not _first_byte_seen:
                    _first_byte_seen = True
                    yield _meta_chunk("cli.first_byte", {})

                # Process the raw CLI JSON line exactly like the old stdout path
                if not line.strip():
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    yield ChatChunk(delta=line + "\n")
                    continue

                received_events += 1
                et = evt.get("type")
                if et == "system" and evt.get("subtype") == "init":
                    yield ChatChunk(delta="", finish=False, tokens_in=0, tokens_out=0, cost_usd=0.0)
                    yield _meta_chunk("system.init", {
                        "session_id": evt.get("session_id"),
                        "model": evt.get("model"),
                        "cwd": evt.get("cwd"),
                        "tools": evt.get("tools", [])[:30],
                        "permission_mode": evt.get("permissionMode"),
                    })
                elif et == "assistant":
                    _acontent = evt.get("message", {}).get("content")
                    if isinstance(_acontent, str):
                        # Same string-content shape as compact user events.
                        _acontent = [{"type": "text", "text": _acontent}]
                    for block in (_acontent or []):
                        if not isinstance(block, dict):
                            continue
                        bt = block.get("type")
                        if bt == "thinking":
                            t = block.get("thinking", "")
                            if t:
                                yield _meta_chunk("thinking", {"text": t[:20000]})
                        elif bt == "tool_use":
                            yield _meta_chunk("tool_call", {
                                "id": block.get("id"),
                                "name": block.get("name"),
                                "input": _redact(block.get("input")),
                            })
                        elif bt == "text":
                            txt = block.get("text", "")
                            if txt:
                                # Each assistant text block is a distinct narration
                                # the CLI emitted between tool calls. Separate them
                                # with a blank line so they stay individual
                                # paragraphs instead of gluing into one blob
                                # ("...agora.Now remove..."). The Telegram
                                # dispatcher then delivers each as its own bubble.
                                sep = "\n\n" if (final_text and not final_text.endswith("\n")) else ""
                                final_text += sep + txt
                                yield ChatChunk(delta=sep + txt)
                    usage = evt.get("message", {}).get("usage") or {}
                    if usage:
                        tin = usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                        tout = usage.get("output_tokens", 0) or tout
                elif et == "user":
                    # message.content is usually a list of blocks, but compact
                    # turns emit user events with a plain-string content (the
                    # injected summary + "<local-command-stdout>Compacted</…>").
                    _ucontent = evt.get("message", {}).get("content")
                    if not isinstance(_ucontent, list):
                        _ucontent = []
                    for block in _ucontent:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_result":
                            content = block.get("content")
                            if isinstance(content, list):
                                text_blocks = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                                content_text = "\n".join(text_blocks)[:20000]
                            else:
                                content_text = str(content)[:20000]
                            yield _meta_chunk("tool_result", {
                                "tool_use_id": block.get("tool_use_id"),
                                "content": content_text,
                            })
                elif et == "result":
                    cost = evt.get("total_cost_usd", 0.0) or 0.0
                    usage = evt.get("usage") or {}
                    if usage:
                        tin = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
                        tout = usage.get("output_tokens", 0) or tout
                    if not final_text:
                        final_text = evt.get("result", "") or ""
                    if evt.get("subtype") != "success":
                        yield _meta_chunk("cli.error", {"subtype": evt.get("subtype"),
                                                        "is_error": evt.get("is_error")})
                elif et == "cli.stderr":
                    yield _meta_chunk("cli.error", {
                        "subtype": "stderr",
                        "message": str(evt.get("text") or "")[:20000],
                    })
                # ── codex --json event schema (thread.started/turn.started/
                # item.started/item.completed/turn.completed) — distinct "type"
                # values from claude's, so no collision with the branches above.
                elif et == "thread.started":
                    thread_id = evt.get("thread_id")
                    if thread_id:
                        yield _meta_chunk("system.init", {
                            "session_id": thread_id,
                            "model": self.model,
                            "cwd": self.cwd,
                            "tools": [],
                            "permission_mode": None,
                            "cli": "codex",
                        })
                elif et == "item.started":
                    item = evt.get("item") or {}
                    it = item.get("type")
                    if it == "mcp_tool_call":
                        yield _meta_chunk("tool_call", {
                            "id": item.get("id"),
                            "name": f"{item.get('server', '')}.{item.get('tool', '')}",
                            "input": _redact(item.get("arguments")),
                        })
                    elif it == "command_execution":
                        yield _meta_chunk("tool_call", {
                            "id": item.get("id"),
                            "name": "exec",
                            "input": _redact({"command": item.get("command")}),
                        })
                elif et == "item.completed":
                    item = evt.get("item") or {}
                    it = item.get("type")
                    if it == "agent_message":
                        txt = item.get("text", "")
                        if txt:
                            sep = "\n\n" if (final_text and not final_text.endswith("\n")) else ""
                            final_text += sep + txt
                            yield ChatChunk(delta=sep + txt)
                    elif it == "mcp_tool_call":
                        result = item.get("result") or {}
                        content = result.get("content")
                        if isinstance(content, list):
                            text_blocks = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                            content_text = "\n".join(text_blocks)[:20000]
                        else:
                            content_text = str(item.get("error") or result)[:20000]
                        yield _meta_chunk("tool_result", {
                            "tool_use_id": item.get("id"),
                            "content": content_text,
                        })
                    elif it == "command_execution":
                        yield _meta_chunk("tool_result", {
                            "tool_use_id": item.get("id"),
                            "content": str(item.get("aggregated_output", ""))[:20000],
                        })
                    elif it == "reasoning":
                        rtext = item.get("text", "")
                        if rtext:
                            yield _meta_chunk("thinking", {"text": rtext[:20000]})
                elif et == "turn.completed":
                    usage = evt.get("usage") or {}
                    if usage:
                        tin = usage.get("input_tokens", 0) + usage.get("cached_input_tokens", 0)
                        tout = usage.get("output_tokens", 0) or tout
        finally:
            done_event.set()
            if monitor_task is not None:
                monitor_task.cancel()
            # Give the docker process a moment to exit cleanly (attach mode has none)
            if proc is not None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()
                if rid:
                    await _unregister(rid, proc)
            if stderr_task is not None:
                try:
                    await asyncio.wait_for(stderr_task, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    stderr_task.cancel()
            if unregister_run is not None:
                unregister_run(rid or "unknown")
            if _held_container_lock is not None:
                # `docker run --rm` exiting (proc.wait() above) only means the
                # CLI process is dead — dockerd removes the container itself
                # asynchronously, a beat later. Releasing the lock right here
                # (as this used to do) let the very next queued turn of this
                # SAME session launch `docker run --name <same>` into that gap
                # and lose the race — surfacing as a user-visible "Conflict...
                # already in use" error instead of just quietly running next.
                # Poll briefly for the name to actually free up before handing
                # the lock to the next waiter; this only delays a turn that's
                # already queued behind another one, never the happy path of
                # an unrelated session's very first launch.
                if _container_name:
                    _waited = 0
                    for _waited in range(20):  # ~2s budget, 100ms steps
                        _chk = await asyncio.create_subprocess_exec(
                            "docker", "inspect", _container_name,
                            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                        )
                        if await _chk.wait() != 0:
                            break  # gone
                        await asyncio.sleep(0.1)
                    else:
                        log.warning(
                            "cli container %s still present ~2s after its run "
                            "exited — releasing the lock anyway, next queued "
                            "turn of this session may hit a name conflict",
                            _container_name,
                        )
                    if _waited:
                        log.info(
                            "cli container %s took ~%dms to actually disappear "
                            "after exit (held lock for the next queued turn)",
                            _container_name, _waited * 100,
                        )
                if rid:
                    _RUN_CONTAINER_NAMES.pop(rid, None)
                _held_container_lock.release()

        # `docker run` exited non-zero and the CLI never emitted a single
        # event (e.g. rc=125 — the container itself failed to start:
        # name conflict, bad mount, resource still held by a prior run of
        # the same session). Previously this fell through to the ordinary
        # EOF/done-sentinel path and finalized as an empty "success" — no
        # error, no tokens, no text. Raise instead so the executor's
        # exception handler marks the Run as status=error with a reason.
        rc = proc.returncode if proc is not None else 0
        if rc not in (None, 0) and received_events == 0:
            stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
            msg = f"docker run failed (rc={rc}) before the CLI produced any output"
            if stderr_text:
                msg += f": {stderr_text[-4000:]}"
            raise RuntimeError(msg)

        yield ChatChunk(delta="", finish=True, tokens_in=tin, tokens_out=tout, cost_usd=cost)


def _meta_chunk(kind: str, payload: dict) -> ChatChunk:
    c = ChatChunk(delta="", finish=False)
    c.meta_kind = kind          # type: ignore[attr-defined]
    c.meta_payload = payload    # type: ignore[attr-defined]
    return c


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) < 8000 else value[:8000] + "…[trunc]"
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value[:20]]
    return value
