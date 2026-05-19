"""LangChain Tool wrappers for the platform's builtin code tools and discovered
MCP tools. Used by ``agent_loop.run_langchain_agent`` to give API-direct
agents (Anthropic / OpenAI / Bedrock) real tool execution.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

from langchain_core.tools import StructuredTool, Tool

from ..mcp_client import call_tool as mcp_call_tool
from .code import (
    edit_file as _edit_file,
    glob_files as _glob_files,
    grep_files as _grep_files,
    read_file as _read_file,
    run_command as _run_command,
    write_file as _write_file,
)


# ----------------- platform code tools -----------------

def _serialize(v: Any) -> str:
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, default=str)
    except Exception:
        return str(v)


async def _read_file_tool(path: str) -> str:
    return _serialize(_read_file(path))


async def _write_file_tool(path: str, content: str) -> str:
    return _serialize(_write_file(path, content))


async def _edit_file_tool(path: str, find: str, replace: str, count: int = 1) -> str:
    return _serialize(_edit_file(path, find, replace, count))


async def _run_command_tool(cmd: str, cwd: str | None = None,
                            timeout_s: int | None = None) -> str:
    """timeout_s defaults to the platform setting (300s). Pass an explicit
    value to override on a per-call basis (e.g. quick `node --version` checks)."""
    return _serialize(await _run_command(cmd, cwd=cwd, timeout_s=timeout_s))


async def _glob_tool(pattern: str, root: str | None = None, limit: int = 200) -> str:
    return _serialize(_glob_files(pattern, root=root, limit=limit))


async def _grep_tool(pattern: str, root: str | None = None, file_pattern: str = "*", limit: int = 200) -> str:
    return _serialize(_grep_files(pattern, root=root, file_pattern=file_pattern, limit=limit))


# Tools keyed by the same id we use in agent.tool_specs.
def builtin_tools() -> dict[str, StructuredTool]:
    return {
        "code.read_file": StructuredTool.from_function(
            name="read_file",
            description="Read a UTF-8 text file by absolute or workspace-relative path. Returns JSON: {path, content, bytes} or {error}.",
            coroutine=_read_file_tool,
        ),
        "code.write_file": StructuredTool.from_function(
            name="write_file",
            description="Write text content to a file (creates parent directories). Returns JSON: {path, bytes}.",
            coroutine=_write_file_tool,
        ),
        "code.edit_file": StructuredTool.from_function(
            name="edit_file",
            description="Find/replace within a file. Returns JSON: {path, replacements}.",
            coroutine=_edit_file_tool,
        ),
        "code.run_command": StructuredTool.from_function(
            name="run_command",
            description=("Run a shell command and return {exit_code, stdout, stderr}. "
                         "Gated by the platform security policy (deny-list always "
                         "enforced; allow-list enforced in secure mode). Blocked "
                         "commands return {error:'blocked_by_policy', reason, list}. "
                         "cwd optional; timeout_s defaults to the platform setting (5min)."),
            coroutine=_run_command_tool,
        ),
        "code.glob": StructuredTool.from_function(
            name="glob",
            description="Find files by glob pattern under root (defaults to workspace). Returns {matches, truncated}.",
            coroutine=_glob_tool,
        ),
        "code.grep": StructuredTool.from_function(
            name="grep",
            description="Search file contents by regex. Returns {hits:[{path,line,text}], truncated}.",
            coroutine=_grep_tool,
        ),
    }


# ----------------- MCP tools -----------------

def _make_mcp_tool(server: str, tool_name: str, description: str) -> Tool:
    """Wrap a discovered MCP tool as a generic LangChain Tool. The model passes
    a single JSON string argument; we parse it and call the MCP tool."""
    async def _run(arg_json: str) -> str:
        try:
            args = json.loads(arg_json) if arg_json else {}
            if not isinstance(args, dict):
                args = {"input": args}
        except json.JSONDecodeError:
            args = {"input": arg_json}
        res = await mcp_call_tool(server, tool_name, args)
        return _serialize(res)
    return Tool(
        name=f"mcp_{server}_{tool_name}".replace("-", "_").replace(".", "_"),
        description=(description or f"MCP tool {server}.{tool_name}") +
                    "  Pass arguments as a single JSON-string.",
        coroutine=_run,
    )


async def mcp_tools_for_server(server_name: str) -> list[Tool]:
    """Return wrappers for every tool already discovered on a server."""
    from sqlalchemy.orm import Session
    from ...db import session_scope
    from ...models import McpServer
    out: list[Tool] = []
    with session_scope() as s:
        srv = s.query(McpServer).filter(McpServer.name == server_name).first()
        if srv is None:
            return out
        tools = list(srv.discovered_tools or [])
    for t in tools:
        out.append(_make_mcp_tool(server_name, t.get("name", ""), t.get("description", "")))
    return out


# ----------------- selection per agent -----------------

async def tools_for_agent(tool_specs: list[str]) -> list:
    """Translate the agent's ``tool_specs`` list into concrete LangChain tools.

    Spec ids:
      - ``code.<name>``       — platform builtin
      - ``mcp.<server>.<name>`` — discovered MCP tool
      - ``skill.<slug>``      — ignored here (skills are injected as system prompt by the executor)
    Empty list means *all* builtin tools are bound (sensible default for a new agent).
    """
    builtins = builtin_tools()
    out: list = []

    if not tool_specs:
        return list(builtins.values())

    mcp_by_server: dict[str, set[str]] = {}
    for spec in tool_specs:
        if not isinstance(spec, str):
            continue
        if spec in builtins:
            out.append(builtins[spec])
        elif spec.startswith("mcp."):
            parts = spec.split(".", 2)
            if len(parts) == 3:
                server, tool = parts[1], parts[2]
                mcp_by_server.setdefault(server, set()).add(tool)
        # skill.* tools are silently ignored — handled at prompt time.

    # discover MCP servers and pick the right ones
    for server, want in mcp_by_server.items():
        for t in await mcp_tools_for_server(server):
            short = t.name.split("_", 2)[-1]
            if short in {n.replace("-", "_").replace(".", "_") for n in want}:
                out.append(t)
    return out
