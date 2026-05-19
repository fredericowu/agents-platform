from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..core.skills import list_skills
from ..core.tools.code import TOOL_SPECS
from ..db import get_session
from ..models import McpServer

router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.get("")
def list_all_tools(s: Session = Depends(get_session)):
    out = []
    # builtin
    for spec in TOOL_SPECS:
        out.append({
            "id": spec["id"], "kind": "builtin", "name": spec["name"],
            "description": spec["description"], "server": None,
            "input_schema": spec["input_schema"],
        })
    # mcp
    for srv in s.query(McpServer).filter(McpServer.enabled == True).all():
        for t in (srv.discovered_tools or []):
            out.append({
                "id": f"mcp.{srv.name}.{t.get('name')}",
                "kind": "mcp", "name": t.get("name"),
                "description": t.get("description", ""), "server": srv.name,
                "input_schema": t.get("input_schema", {}),
            })
    # skills as "tools"
    for sk in list_skills():
        out.append({
            "id": f"skill.{sk['slug']}",
            "kind": "skill", "name": sk["slug"],
            "description": sk["description"], "server": None,
            "input_schema": {"type": "object", "properties": {"args": {"type": "string"}}},
        })
    return out
