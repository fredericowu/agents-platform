from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core.mcp_client import discover_tools, sync_mcp_servers_from_file
from ..db import get_session
from ..models import McpServer
from ..schemas import McpServerOut

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


class McpServerIn(BaseModel):
    name: str
    command: str
    args: list[str] = []
    env: dict[str, str] = {}
    enabled: bool = True


@router.get("/servers", response_model=list[McpServerOut])
def list_servers(s: Session = Depends(get_session)):
    return s.query(McpServer).order_by(McpServer.name).all()


@router.post("/servers", response_model=McpServerOut)
def add_server(body: McpServerIn, s: Session = Depends(get_session)):
    if s.query(McpServer).filter(McpServer.name == body.name).first():
        raise HTTPException(409, "server name already exists")
    srv = McpServer(**body.model_dump(), source="manual", discovered_tools=[])
    s.add(srv)
    s.commit()
    s.refresh(srv)
    return srv


@router.put("/servers/{name}", response_model=McpServerOut)
def update_server(name: str, body: McpServerIn, s: Session = Depends(get_session)):
    srv = s.query(McpServer).filter(McpServer.name == name).first()
    if not srv:
        raise HTTPException(404, "not found")
    srv.command = body.command
    srv.args = body.args
    srv.env = body.env
    srv.enabled = body.enabled
    s.commit()
    s.refresh(srv)
    return srv


@router.delete("/servers/{name}")
def delete_server(name: str, s: Session = Depends(get_session)):
    srv = s.query(McpServer).filter(McpServer.name == name).first()
    if not srv:
        raise HTTPException(404, "not found")
    s.delete(srv)
    s.commit()
    return {"deleted": name}


@router.post("/refresh", response_model=list[McpServerOut])
def refresh(s: Session = Depends(get_session)):
    sync_mcp_servers_from_file()
    return s.query(McpServer).order_by(McpServer.name).all()


@router.post("/servers/{name}/discover")
async def discover(name: str):
    tools = await discover_tools(name)
    return {"server": name, "tools": tools}


@router.get("/tools")
def list_all_tools(s: Session = Depends(get_session)):
    out = []
    for srv in s.query(McpServer).filter(McpServer.enabled == True).all():
        for t in (srv.discovered_tools or []):
            out.append({"server": srv.name, "name": t.get("name"),
                        "description": t.get("description", ""),
                        "input_schema": t.get("input_schema", {})})
    return out
