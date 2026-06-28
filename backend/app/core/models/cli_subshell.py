"""Invoke the installed ``claude`` (or any compatible) CLI as the LLM.

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
import shutil
from pathlib import Path
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


class CliSubshellLLM(BaseLLM):
    provider = "cli_subshell"

    def __init__(
        self,
        model_id: str,
        *,
        cli: str = "claude",
        model: str | None = None,            # passed to `claude --model <name>`
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
        # Docker mode — when True, run inside an isolated container via docker_agent
        docker: bool = False,
        docker_creds: bool = True,
        docker_mcp_config_dir: str | None = None,
        # Session persistence: agent+target for cwd isolation; session_id for --resume
        agent_id: str | None = None,
        target_id: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        resume_run_id: str | None = None,
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
        self.docker = docker
        self.docker_creds = docker_creds
        self.docker_mcp_config_dir = docker_mcp_config_dir
        self.agent_id = agent_id
        self.target_id = target_id
        self.run_id = run_id
        self.session_id = session_id
        self.resume_run_id = resume_run_id
        if not docker and shutil.which(self.cli) is None:
            raise RuntimeError(f"CLI {cli!r} not found on PATH")

    def _build_argv(self, prompt: str) -> list[str]:
        if self.docker:
            return self._build_docker_argv(prompt)
        return self._build_direct_argv(prompt)

    def _build_docker_argv(self, prompt: str) -> list[str]:
        from pathlib import Path as _Path
        try:
            from src.tools.docker_agent import build_docker_argv, CLI_SPECS
        except ImportError:
            import sys
            sys.path.insert(0, "/opt/agentic-workspace")
            from src.tools.docker_agent import build_docker_argv, CLI_SPECS

        cli = self.cli if self.cli in CLI_SPECS else "claude"

        # Build mounts: cwd + all add_dirs
        mounts: list[str] = []
        if self.cwd:
            mounts.append(self.cwd)
        for d in self.add_dirs:
            # add_dirs are passed separately; don't double-mount as plain mounts
            pass

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
        )

    def _build_direct_argv(self, prompt: str) -> list[str]:
        argv: list[str] = [self.cli]
        if self.session_id:
            argv += ["--resume", self.session_id]
        argv += ["-p", prompt]
        if self.model:
            argv += ["--model", self.model]
        if self.dangerous_skip_permissions:
            argv.append("--dangerously-skip-permissions")
        if self.bare:
            argv.append("--bare")
        if self.append_system_prompt:
            argv += ["--append-system-prompt", self.append_system_prompt]
        for d in self.add_dirs:
            argv += ["--add-dir", d]
        if self.allowed_tools:
            argv += ["--allowed-tools", ",".join(self.allowed_tools)]
        if self.disallowed_tools:
            argv += ["--disallowed-tools", ",".join(self.disallowed_tools)]
        if self.stream_json:
            argv += ["--output-format", "stream-json", "--verbose"]
        argv += self.extra_args
        return argv

    async def astream(self, messages: list[dict], **params: Any) -> AsyncIterator[ChatChunk]:
        # Collapse messages → one prompt (the CLI runs the agent loop itself)
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

        cwd = self.cwd or os.getcwd()
        if self.cwd:
            Path(self.cwd).mkdir(parents=True, exist_ok=True)

        # ``limit`` controls the asyncio StreamReader buffer per line. claude CLI's
        # stream-json events can include tool_use blocks with large file payloads
        # and a system.init event listing every tool — both routinely > 64KB.
        # Bump to 10MB so we don't blow up with LimitOverrunError ("Separator …
        # chunk exceeds the limit").
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd, env={**os.environ},
            limit=10 * 1024 * 1024,
        )
        rid: str | None = current_run_id.get()
        if rid:
            await _register(rid, proc)

        tin = tout = 0
        cost = 0.0
        final_text = ""
        async def _read_line(stream: asyncio.StreamReader) -> bytes | None:
            """Read one newline-terminated line, surviving LimitOverrunError by
            draining until the next newline. Returns None at EOF."""
            try:
                return await stream.readline()
            except asyncio.LimitOverrunError as e:
                # consume up to `e.consumed` from the buffer and keep reading
                # the rest of the oversized line as raw bytes, then return it.
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
                        # not json — emit as plain text
                        yield ChatChunk(delta=line + "\n")
                        continue
                    et = evt.get("type")
                    if et == "system" and evt.get("subtype") == "init":
                        yield ChatChunk(delta="", finish=False, tokens_in=0, tokens_out=0,
                                        cost_usd=0.0)  # marker; metadata via .meta below
                        # Use a special chunk with a payload via the .meta hack:
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
    """ChatChunk variant: ``.delta`` empty, with metadata attached via attributes."""
    c = ChatChunk(delta="", finish=False)
    c.meta_kind = kind          # type: ignore[attr-defined]
    c.meta_payload = payload    # type: ignore[attr-defined]
    return c


def _redact(value: Any) -> Any:
    """Truncate big payloads in tool call inputs for log readability."""
    if isinstance(value, str):
        return value if len(value) < 800 else value[:800] + "…[trunc]"
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value[:20]]
    return value
