"""MCP discovery + invocation. Reads .mcp.json, lists tools on demand, calls them."""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import settings
from ..db import session_scope
from ..models import McpServer


def load_mcp_json(path: Path | None = None) -> dict[str, Any]:
    p = path or settings.mcp_json_path
    if not p.exists():
        return {"mcpServers": {}}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as e:
        print(f"[mcp] bad json in {p}: {e}")
        return {"mcpServers": {}}


def sync_mcp_servers_from_file() -> list[McpServer]:
    """Reads ./.mcp.json and upserts McpServer rows."""
    data = load_mcp_json()
    servers: list[McpServer] = []
    with session_scope() as s:
        existing = {row.name: row for row in s.query(McpServer).filter(McpServer.source == "file").all()}
        seen = set()
        for name, spec in (data.get("mcpServers") or {}).items():
            row = existing.get(name)
            if row is None:
                row = McpServer(name=name, source="file")
                s.add(row)
            row.command = spec.get("command", "")
            row.args = spec.get("args", [])
            row.env = spec.get("env", {})
            row.enabled = True
            seen.add(name)
            servers.append(row)
        # Mark any removed ones as disabled
        for name, row in existing.items():
            if name not in seen:
                row.enabled = False
        s.flush()
    return servers


@asynccontextmanager
async def mcp_session(server_row: McpServer):
    """Open a stdio MCP session to the given server row."""
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    env = {**os.environ, **(server_row.env or {})}
    params = StdioServerParameters(command=server_row.command, args=list(server_row.args or []), env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as sess:
            await sess.initialize()
            yield sess


async def discover_tools(server_name: str, *, timeout: float = 20.0) -> list[dict[str, Any]]:
    with session_scope() as s:
        row = s.query(McpServer).filter(McpServer.name == server_name).first()
        if row is None or not row.enabled:
            return []
        # detach a snapshot
        snap = McpServer(name=row.name, command=row.command, args=list(row.args), env=dict(row.env))
    try:
        async with asyncio.timeout(timeout):
            async with mcp_session(snap) as sess:
                tools = await sess.list_tools()
                out = []
                for t in tools.tools:
                    schema = getattr(t, "inputSchema", None) or {}
                    out.append({
                        "name": t.name,
                        "description": getattr(t, "description", "") or "",
                        "input_schema": schema if isinstance(schema, dict) else {},
                    })
        # persist
        with session_scope() as s:
            row = s.query(McpServer).filter(McpServer.name == server_name).first()
            if row:
                row.discovered_tools = out
                row.last_refreshed = datetime.utcnow()
        return out
    except Exception as e:
        return [{"name": "_error", "description": str(e), "input_schema": {}}]


async def call_tool(server_name: str, tool_name: str, args: dict[str, Any], *, timeout: float = 60.0) -> dict[str, Any]:
    with session_scope() as s:
        row = s.query(McpServer).filter(McpServer.name == server_name).first()
        if row is None or not row.enabled:
            return {"error": f"server {server_name} not enabled"}
        snap = McpServer(name=row.name, command=row.command, args=list(row.args), env=dict(row.env))
    try:
        async with asyncio.timeout(timeout):
            async with mcp_session(snap) as sess:
                result = await sess.call_tool(tool_name, arguments=args)
                # Normalize content blocks
                blocks = []
                for c in (result.content or []):
                    if getattr(c, "type", None) == "text":
                        blocks.append({"type": "text", "text": c.text})
                    else:
                        blocks.append({"type": str(getattr(c, "type", "unknown")), "data": getattr(c, "data", None)})
                return {"content": blocks, "isError": bool(getattr(result, "isError", False))}
    except Exception as e:
        return {"error": str(e)}
