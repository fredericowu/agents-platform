"""Remote Agents router — ported from src/custom_apps/aw-remote-agent/src/api/main.py.

Exposes the same API paths as the standalone aw-remote-agent custom app so the
Windows agent exe and FUSE driver can connect without modification.

DB is stored at /opt/agentic-workspace/.tmp/remote-agents.db, accessed via the
SQLAlchemy ORM models in app.core.remote_agents_db (no raw sqlite3).
On first boot (empty table) it tries to migrate data from the old custom-app DB.
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
import asyncio, json, secrets as _sec, time, uuid as _uuid, os

from ..core.remote_agents_db import ConfigRow, RemoteAgentRow, init_db, now_epoch, session_scope

# ── Paths ─────────────────────────────────────────────────────────────────────

UPDATE_EXE_PATH  = "/opt/agentic-workspace/.tmp/remote-agents/update/aw-remote-agent.exe"
UPDATE_JSON_PATH = "/opt/agentic-workspace/.tmp/remote-agents/update/version.json"

# ── In-memory state ───────────────────────────────────────────────────────────

# client_id -> { ws, info, connected_at }
connected_clients: dict = {}
# req_id -> asyncio.Future  (fs_request/response — single-shot, unchanged)
pending_requests: dict = {}
# req_id -> asyncio.Queue  (exec — streamed chunks + final {"done": True, "returncode": ...})
exec_queues: dict = {}
# UI WebSocket subscribers
ui_clients: set = set()

# ── Init on import ────────────────────────────────────────────────────────────

init_db()

# ── Router ────────────────────────────────────────────────────────────────────

router = APIRouter()


async def _broadcast_ui(event: dict):
    """Send an event JSON to all connected UI WebSocket clients."""
    dead = set()
    msg = json.dumps(event)
    for ws in list(ui_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    for ws in dead:
        ui_clients.discard(ws)


# ── WebSocket: Windows agent ──────────────────────────────────────────────────

@router.websocket("/ws/client/{client_id}")
async def client_ws(ws: WebSocket, client_id: str):
    await ws.accept()
    connected_clients[client_id] = {
        "ws": ws,
        "info": {},
        "connected_at": int(time.time()),
    }
    try:
        # First message: handshake with system info
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
        msg = json.loads(raw)
        if msg.get("type") == "handshake":
            connected_clients[client_id]["info"] = msg.get("info", {})

        await _broadcast_ui({
            "type": "agent_connected",
            "agent_id": client_id,
            "info": connected_clients[client_id]["info"],
            "connected_at": connected_clients[client_id]["connected_at"],
        })

        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=40)
                msg = json.loads(raw)
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"type": "ping"}))
                continue

            kind = msg.get("type")
            if kind == "fs_response":
                req_id = msg.get("req_id")
                fut = pending_requests.get(req_id)
                if fut and not fut.done():
                    fut.set_result(msg)
            elif kind == "exec_response":
                # Legacy single-shot client (e.g. the precompiled Windows exe,
                # which we don't rebuild here): treat the one full response as
                # a chunk pair followed by done, so it fits the streaming API.
                req_id = msg.get("req_id")
                q = exec_queues.get(req_id)
                if q is not None:
                    if msg.get("stdout"):
                        await q.put({"stream": "stdout", "data": msg["stdout"]})
                    if msg.get("stderr"):
                        await q.put({"stream": "stderr", "data": msg["stderr"]})
                    await q.put({"done": True, "returncode": msg.get("returncode", 0)})
            elif kind == "exec_chunk":
                req_id = msg.get("req_id")
                q = exec_queues.get(req_id)
                if q is not None:
                    await q.put({"stream": msg.get("stream", "stdout"), "data": msg.get("data", "")})
            elif kind == "exec_done":
                req_id = msg.get("req_id")
                q = exec_queues.get(req_id)
                if q is not None:
                    await q.put({"done": True, "returncode": msg.get("returncode", 0)})
            elif kind == "pong":
                pass

    except (WebSocketDisconnect, Exception):
        pass
    finally:
        connected_clients.pop(client_id, None)
        await _broadcast_ui({"type": "agent_disconnected", "agent_id": client_id})


# ── WebSocket: UI ─────────────────────────────────────────────────────────────

@router.websocket("/ws/ui")
async def ui_ws(ws: WebSocket):
    """UI clients subscribe here to receive real-time agent events."""
    await ws.accept()
    ui_clients.add(ws)
    try:
        with session_scope() as s:
            rows = s.query(RemoteAgentRow).order_by(RemoteAgentRow.created_at.desc()).all()
            agents = [_agent_with_status(r) for r in rows]
        await ws.send_text(json.dumps({"type": "snapshot", "agents": agents}))

        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=60)
                msg = json.loads(raw)
                if msg.get("type") == "pong":
                    pass
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"type": "ping"}))
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        ui_clients.discard(ws)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _agent_with_status(row: RemoteAgentRow) -> dict:
    conn_data = connected_clients.get(row.id)
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "created_at": row.created_at,
        "connected": conn_data is not None,
        "info": conn_data["info"] if conn_data else None,
        "connected_at": conn_data["connected_at"] if conn_data else None,
    }


# ── REST: Clients ─────────────────────────────────────────────────────────────

@router.get("/api/clients")
def list_clients():
    return [
        {"id": cid, "info": c["info"], "connected_at": c["connected_at"]}
        for cid, c in connected_clients.items()
    ]


class ExecRequest(BaseModel):
    command: str
    timeout: int = 30


@router.post("/api/clients/{client_id}/exec")
async def exec_on_client(client_id: str, req: ExecRequest):
    """Run `req.command` on the client and stream the result back as NDJSON.

    Each line is either a chunk — {"stream": "stdout"|"stderr", "data": "..."}
    — emitted as the client produces output, or the final line
    {"done": true, "returncode": N}. Callers (the `aw` CLI's remote forward)
    read line-by-line and print as they arrive instead of waiting for the
    whole command to finish.
    """
    client = connected_clients.get(client_id)
    if not client:
        raise HTTPException(404, "Client not connected")

    req_id = str(_uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    exec_queues[req_id] = queue

    await client["ws"].send_text(json.dumps({
        "type": "exec",
        "req_id": req_id,
        "command": req.command,
        "timeout": req.timeout,
    }))

    async def _stream():
        loop = asyncio.get_event_loop()
        deadline = loop.time() + req.timeout + 15
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    yield json.dumps({"stream": "stderr", "data": "Command timed out\n"}) + "\n"
                    yield json.dumps({"done": True, "returncode": -1}) + "\n"
                    return
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    continue
                yield json.dumps(item) + "\n"
                if item.get("done"):
                    return
        finally:
            exec_queues.pop(req_id, None)

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


class FsRequest(BaseModel):
    op: str
    path: str
    data: str = ""
    offset: int = 0
    size: int = 65536
    dest: str = ""


@router.post("/api/clients/{client_id}/fs")
async def fs_op(client_id: str, req: FsRequest):
    client = connected_clients.get(client_id)
    if not client:
        raise HTTPException(404, "Client not connected")

    req_id = str(_uuid.uuid4())
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    pending_requests[req_id] = fut

    try:
        await client["ws"].send_text(json.dumps({
            "type": "fs_request",
            "req_id": req_id,
            "op": req.op,
            "path": req.path,
            "data": req.data,
            "offset": req.offset,
            "size": req.size,
            "dest": req.dest,
        }))
        result = await asyncio.wait_for(fut, timeout=30)
        return result
    except asyncio.TimeoutError:
        raise HTTPException(408, "FS op timed out")
    finally:
        pending_requests.pop(req_id, None)


# ── REST: Update endpoints ────────────────────────────────────────────────────

@router.get("/api/update/selfcheck")
def update_selfcheck():
    return {"ok": True, "server": "aw-remote-agent"}


@router.get("/api/update/latest")
def update_latest():
    if not os.path.exists(UPDATE_JSON_PATH):
        raise HTTPException(404, "No update info available")
    with open(UPDATE_JSON_PATH) as f:
        return json.load(f)


@router.get("/api/update/exe")
def update_exe():
    if not os.path.exists(UPDATE_EXE_PATH):
        raise HTTPException(404, "Exe not found")
    return FileResponse(UPDATE_EXE_PATH, filename="aw-remote-agent.exe",
                        media_type="application/octet-stream")


# ── REST: Remote Agents CRUD ──────────────────────────────────────────────────

class RemoteAgentBody(BaseModel):
    name: str
    description: str = ""


@router.get("/api/remote-agents")
def list_remote_agents():
    with session_scope() as s:
        rows = s.query(RemoteAgentRow).order_by(RemoteAgentRow.created_at.desc()).all()
        return [_agent_with_status(r) for r in rows]


@router.post("/api/remote-agents", status_code=201)
def create_remote_agent(body: RemoteAgentBody):
    with session_scope() as s:
        row = RemoteAgentRow(id=str(_uuid.uuid4()), name=body.name,
                             description=body.description, created_at=now_epoch())
        s.add(row)
        s.flush()
        return _agent_with_status(row)


@router.get("/api/remote-agents/{agent_id}")
def get_remote_agent(agent_id: str):
    with session_scope() as s:
        row = s.get(RemoteAgentRow, agent_id)
        if not row:
            raise HTTPException(404, "Agent not found")
        return _agent_with_status(row)


@router.put("/api/remote-agents/{agent_id}")
def update_remote_agent(agent_id: str, body: RemoteAgentBody):
    with session_scope() as s:
        row = s.get(RemoteAgentRow, agent_id)
        if not row:
            raise HTTPException(404, "Agent not found")
        row.name = body.name
        row.description = body.description
        s.flush()
        return _agent_with_status(row)


@router.delete("/api/remote-agents/{agent_id}")
def delete_remote_agent(agent_id: str):
    with session_scope() as s:
        row = s.get(RemoteAgentRow, agent_id)
        if row:
            s.delete(row)
    return {"ok": True}


# ── REST: Config ──────────────────────────────────────────────────────────────

@router.get("/api/config")
def get_config():
    with session_scope() as s:
        row = s.get(ConfigRow, "mcp_api_key")
        return {"mcp_api_key": row.value if row else ""}


@router.post("/api/config/regenerate")
def regenerate_api_key():
    new_key = _sec.token_urlsafe(32)
    with session_scope() as s:
        row = s.get(ConfigRow, "mcp_api_key")
        if row:
            row.value = new_key
        else:
            s.add(ConfigRow(key="mcp_api_key", value=new_key))
    return {"mcp_api_key": new_key}
