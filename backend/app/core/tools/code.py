"""Code tools: file I/O, command execution, search. Used by agents that touch code."""
from __future__ import annotations

import asyncio
import contextvars
import fnmatch
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from ...config import settings
from .. import security

CWD_ROOT = settings.repo_root if hasattr(settings, "repo_root") else Path(os.getcwd())


# Context var: the currently-executing agent's ``params`` dict. Set by
# ``executor.run_agent`` right before invoking the LLM so that command-gate
# decisions in ``run_command`` know which agent is asking. Unset (None) means
# "ad-hoc call outside an agent context" → use the global security_mode.
current_agent_params: contextvars.ContextVar[dict | None] = \
    contextvars.ContextVar("current_agent_params", default=None)


def _resolve(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (CWD_ROOT / p).resolve()
    return p


def read_file(path: str, max_bytes: int = 200_000) -> dict[str, Any]:
    p = _resolve(path)
    if not p.exists() or not p.is_file():
        return {"error": f"not a file: {p}"}
    data = p.read_bytes()[:max_bytes]
    return {"path": str(p), "content": data.decode(errors="replace"), "bytes": len(data)}


def write_file(path: str, content: str) -> dict[str, Any]:
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return {"path": str(p), "bytes": len(content)}


def edit_file(path: str, find: str, replace: str, count: int = 1) -> dict[str, Any]:
    p = _resolve(path)
    if not p.exists():
        return {"error": f"not found: {p}"}
    text = p.read_text()
    if find not in text:
        return {"error": "find string not present", "path": str(p)}
    new = text.replace(find, replace, count if count > 0 else -1)
    p.write_text(new)
    return {"path": str(p), "replacements": text.count(find) if count <= 0 else min(count, text.count(find))}


async def run_command(cmd: str, cwd: str | None = None,
                      timeout_s: int | None = None) -> dict[str, Any]:
    """Run a shell command on the host.

    Security: gates the command through ``security.check_command`` using the
    effective mode (resolved from the current agent's params → global setting).
    Deny-list is **always** enforced. Allow-list applies in ``secure`` mode.

    Timeout: resolves from ``timeout_s`` arg → global setting → 300s default.
    """
    # Resolve effective security context for the current agent (if any).
    agent_params = current_agent_params.get()
    eff = security.effective_for_agent(agent_params)

    # Resolve timeout: explicit arg wins, else use settings default.
    if timeout_s is None or timeout_s <= 0:
        timeout_s = eff["timeout_s"]

    # Gate.
    try:
        security.check_command(cmd, mode=eff["mode"],
                               allowlist=eff["allowlist"], denylist=eff["denylist"])
    except security.CommandBlocked as e:
        return {
            "error": "blocked_by_policy",
            "reason": e.reason,
            "list": e.list_kind,                     # "deny" | "allow"
            "matched": e.entry,
            "security_mode": eff["mode"],
            "cmd": cmd,
        }

    cwd_p = _resolve(cwd) if cwd else CWD_ROOT
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=str(cwd_p), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env={**os.environ}
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        return {
            "exit_code": proc.returncode,
            "stdout": out.decode(errors="replace")[:50_000],
            "stderr": err.decode(errors="replace")[:50_000],
            "cmd": cmd,
            "cwd": str(cwd_p),
            "timeout_s": timeout_s,
            "security_mode": eff["mode"],
        }
    except asyncio.TimeoutError:
        return {"error": "timeout", "cmd": cmd, "timeout_s": timeout_s}


def glob_files(pattern: str, root: str | None = None, limit: int = 200) -> dict[str, Any]:
    root_p = _resolve(root) if root else CWD_ROOT
    matches: list[str] = []
    for dirpath, _, filenames in os.walk(root_p):
        # skip common heavy dirs
        if any(p in dirpath for p in (".git", "node_modules", ".venv", "__pycache__", "dist", "build")):
            continue
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root_p)
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(fn, pattern):
                matches.append(str(full))
                if len(matches) >= limit:
                    return {"matches": matches, "truncated": True}
    return {"matches": matches, "truncated": False}


def grep_files(pattern: str, root: str | None = None, file_pattern: str = "*", limit: int = 200) -> dict[str, Any]:
    root_p = _resolve(root) if root else CWD_ROOT
    rx = re.compile(pattern)
    hits: list[dict[str, Any]] = []
    for dirpath, _, filenames in os.walk(root_p):
        if any(p in dirpath for p in (".git", "node_modules", ".venv", "__pycache__")):
            continue
        for fn in filenames:
            if not fnmatch.fnmatch(fn, file_pattern):
                continue
            full = os.path.join(dirpath, fn)
            try:
                with open(full, "r", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        if rx.search(line):
                            hits.append({"path": full, "line": lineno, "text": line.rstrip()})
                            if len(hits) >= limit:
                                return {"hits": hits, "truncated": True}
            except OSError:
                continue
    return {"hits": hits, "truncated": False}


# Tool spec table — used by the registry to expose what's callable.
TOOL_SPECS = [
    {
        "id": "code.read_file",
        "kind": "builtin",
        "name": "read_file",
        "description": "Read a text file by path.",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string"},
        }, "required": ["path"]},
    },
    {
        "id": "code.write_file",
        "kind": "builtin",
        "name": "write_file",
        "description": "Write text content to a path (creates parents).",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"},
        }, "required": ["path", "content"]},
    },
    {
        "id": "code.edit_file",
        "kind": "builtin",
        "name": "edit_file",
        "description": "Find/replace within a file.",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string"}, "find": {"type": "string"}, "replace": {"type": "string"},
            "count": {"type": "integer"},
        }, "required": ["path", "find", "replace"]},
    },
    {
        "id": "code.run_command",
        "kind": "builtin",
        "name": "run_command",
        "description": "Run a shell command, return stdout/stderr/exit code.",
        "input_schema": {"type": "object", "properties": {
            "cmd": {"type": "string"}, "cwd": {"type": "string"},
            "timeout_s": {"type": "integer"},
        }, "required": ["cmd"]},
    },
    {
        "id": "code.glob",
        "kind": "builtin",
        "name": "glob",
        "description": "Find files matching a glob pattern.",
        "input_schema": {"type": "object", "properties": {
            "pattern": {"type": "string"}, "root": {"type": "string"},
        }, "required": ["pattern"]},
    },
    {
        "id": "code.grep",
        "kind": "builtin",
        "name": "grep",
        "description": "Search file contents by regex.",
        "input_schema": {"type": "object", "properties": {
            "pattern": {"type": "string"}, "root": {"type": "string"}, "file_pattern": {"type": "string"},
        }, "required": ["pattern"]},
    },
]


async def call_builtin(tool_id: str, args: dict[str, Any]) -> dict[str, Any]:
    fn_map = {
        "code.read_file": lambda: read_file(**args),
        "code.write_file": lambda: write_file(**args),
        "code.edit_file": lambda: edit_file(**args),
        "code.run_command": run_command(**args),  # coroutine
        "code.glob": lambda: glob_files(**args),
        "code.grep": lambda: grep_files(**args),
    }
    target = fn_map.get(tool_id)
    if target is None:
        return {"error": f"unknown tool {tool_id}"}
    if asyncio.iscoroutine(target):
        return await target
    return target()
