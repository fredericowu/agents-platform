"""Smoke-test the agent-mcp stdio server by acting as an MCP client."""
import asyncio
import os
import sys
from pathlib import Path


async def main() -> int:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    repo = Path(__file__).resolve().parents[1]
    py = repo / ".venv" / "bin" / "python"
    params = StdioServerParameters(
        command=str(py),
        args=["-m", "mcp_server.agent_mcp"],
        env={**os.environ},
        cwd=str(repo),
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as sess:
            await sess.initialize()
            tools = await sess.list_tools()
            tool_names = [t.name for t in tools.tools]
            print(f"discovered {len(tool_names)} tools")
            assert "list_agents" in tool_names
            assert any(n.startswith("agent_") for n in tool_names)
            assert any(n.startswith("workflow_") for n in tool_names)
            print("OK: introspection + agent_ + workflow_ tools present")

            result = await sess.call_tool("list_agents", arguments={})
            text = result.content[0].text if result.content else ""
            assert "coder" in text, f"expected coder in result: {text[:200]}"
            print("OK: list_agents returned coder")

            result = await sess.call_tool("agent_coder", arguments={"input": "mcp ping"})
            text = result.content[0].text if result.content else ""
            assert "mcp ping" in text, f"expected echo: {text[:200]}"
            print("OK: agent_coder returned echo")

            result = await sess.call_tool("workflow_orchestrator_worker",
                                          arguments={"input": "via mcp"})
            text = result.content[0].text if result.content else ""
            assert "synthesis" in text, f"expected synthesis: {text[:200]}"
            print("OK: workflow_orchestrator_worker returned synthesis")
            return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
