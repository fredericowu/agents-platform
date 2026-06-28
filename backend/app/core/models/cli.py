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
import os
from typing import Any, AsyncIterator

from . import BaseLLM, ChatChunk

# ContextVar set by the executor right before astream() — lets us register the
# subprocess under the correct run id so /runs/:id/cancel can kill it.
current_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_run_id", default=None)

# run_id → set of live subprocess.Process instances
_PROCS: dict[str, set[asyncio.subprocess.Process]] = {}
_PROCS_LOCK = asyncio.Lock()


async def _register(run_id: str, proc: asyncio.subprocess.Process) -> None:
    async with _PROCS_LOCK:
        _PROCS.setdefault(run_id, set()).add(proc)


async def _unregister(run_id: str, proc: asyncio.subprocess.Process) -> None:
    async with _PROCS_LOCK:
        _PROCS.get(run_id, set()).discard(proc)
        if run_id in _PROCS and not _PROCS[run_id]:
            del _PROCS[run_id]


async def kill_run(run_id: str) -> int:
    """Send SIGTERM to every live subprocess registered for run_id. Returns
    the count of processes signalled."""
    async with _PROCS_LOCK:
        procs = list(_PROCS.get(run_id, set()))
    n = 0
    for p in procs:
        try:
            p.terminate()
            n += 1
        except ProcessLookupError:
            pass
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
        **_: Any,
    ) -> None:
        self.model_id = model_id
        self.cli = cli
        self.model = model
        self.cwd = cwd
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

    def _build_argv(self, prompt: str) -> list[str]:
        from ..tools.docker_agent import build_docker_argv, CLI_SPECS

        cli = self.cli if self.cli in CLI_SPECS else "claude"

        mounts: list[str] = []
        if self.cwd:
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
        )

    async def astream(self, messages: list[dict], **params: Any) -> AsyncIterator[ChatChunk]:
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
        argv = self._build_argv(prompt)

        # Bump to 10MB — claude CLI stream-json events (system.init, tool payloads) exceed 64KB default
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.getcwd(), env={**os.environ},
            limit=10 * 1024 * 1024,
        )
        rid: str | None = current_run_id.get()
        if rid:
            await _register(rid, proc)

        tin = tout = 0
        cost = 0.0
        final_text = ""

        async def _read_line(stream: asyncio.StreamReader) -> bytes | None:
            try:
                return await stream.readline()
            except asyncio.LimitOverrunError as e:
                head = await stream.readexactly(e.consumed)
                rest = b""
                while True:
                    try:
                        chunk = await stream.readuntil(b"\n")
                        return head + rest + chunk
                    except asyncio.LimitOverrunError as e2:
                        rest += await stream.readexactly(e2.consumed)
                    except asyncio.IncompleteReadError as ie:
                        return head + rest + ie.partial

        try:
            if self.stream_json:
                assert proc.stdout
                while True:
                    raw = await _read_line(proc.stdout)
                    if not raw:
                        break
                    line = raw.decode(errors="replace").strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        yield ChatChunk(delta=line + "\n")
                        continue
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
                        for block in (evt.get("message", {}).get("content") or []):
                            bt = block.get("type")
                            if bt == "thinking":
                                t = block.get("thinking", "")
                                if t:
                                    yield _meta_chunk("thinking", {"text": t[:1500]})
                            elif bt == "tool_use":
                                yield _meta_chunk("tool_call", {
                                    "id": block.get("id"),
                                    "name": block.get("name"),
                                    "input": _redact(block.get("input")),
                                })
                            elif bt == "text":
                                txt = block.get("text", "")
                                if txt:
                                    final_text += txt
                                    yield ChatChunk(delta=txt)
                        usage = evt.get("message", {}).get("usage") or {}
                        if usage:
                            tin = usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                            tout = usage.get("output_tokens", 0) or tout
                    elif et == "user":
                        for block in (evt.get("message", {}).get("content") or []):
                            if block.get("type") == "tool_result":
                                content = block.get("content")
                                if isinstance(content, list):
                                    text_blocks = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                                    content_text = "\n".join(text_blocks)[:1500]
                                else:
                                    content_text = str(content)[:1500]
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
            else:
                assert proc.stdout
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace")
                    final_text += line
                    yield ChatChunk(delta=line)
        finally:
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.timeout_s)
            except asyncio.TimeoutError:
                proc.kill()
                yield _meta_chunk("cli.timeout", {"timeout_s": self.timeout_s})
            if rid:
                await _unregister(rid, proc)

        yield ChatChunk(delta="", finish=True, tokens_in=tin, tokens_out=tout, cost_usd=cost)


def _meta_chunk(kind: str, payload: dict) -> ChatChunk:
    c = ChatChunk(delta="", finish=False)
    c.meta_kind = kind          # type: ignore[attr-defined]
    c.meta_payload = payload    # type: ignore[attr-defined]
    return c


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) < 800 else value[:800] + "…[trunc]"
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value[:20]]
    return value
