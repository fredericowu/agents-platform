"""Remote Agents router — ported from src/custom_apps/aw-remote-agent/src/api/main.py.

Exposes the same API paths as the standalone aw-remote-agent custom app so the
Windows agent exe and FUSE driver can connect without modification.

DB is stored at /opt/agentic-workspace/.tmp/remote-agents.db.
On first boot (empty table) it tries to migrate data from the old custom-app DB.
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import asyncio, json, sqlite3, time, uuid as _uuid, os, secrets as _sec

# ── Paths ─────────────────────────────────────────────────────────────────────

DB_PATH          = "/opt/agentic-workspace/.tmp/remote-agents.db"
OLD_DB_PATH      = "/opt/agentic-workspace/src/custom_apps/aw-remote-agent/data/app.db"
UPDATE_EXE_PATH  = "/opt/agentic-workspace/.tmp/remote-agents/update/aw-remote-agent.exe"
UPDATE_JSON_PATH = "/opt/agentic-workspace/.tmp/remote-agents/update/version.json"

# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_remote_agents_db():
    """Create tables and seed the MCP API key if needed."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    existing = conn.execute("SELECT value FROM config WHERE key='mcp_api_key'").fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO config (key, value) VALUES ('mcp_api_key', ?)",
            (_sec.token_urlsafe(32),),
        )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS remote_agents (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            description TEXT DEFAULT '',
            created_at  INTEGER DEFAULT (strftime('%s','now'))
        )
    """)
    conn.commit()

    # Migration: if table is empty, try to copy rows from the old custom-app DB
    count = conn.execute("SELECT COUNT(*) FROM remote_agents").fetchone()[0]
    if count == 0 and os.path.exists(OLD_DB_PATH):
        try:
            old = sqlite3.connect(OLD_DB_PATH)
            old.row_factory = sqlite3.Row
            rows = old.execute("SELECT * FROM remote_agents").fetchall()
            for r in rows:
                conn.execute(
                    "INSERT OR IGNORE INTO remote_agents (id, name, description, created_at) VALUES (?,?,?,?)",
                    (r["id"], r["name"], r["description"], r["created_at"]),
                )
            # Also migrate the MCP API key if present
            old_key = old.execute("SELECT value FROM config WHERE key='mcp_api_key'").fetchone()
            if old_key:
                conn.execute(
                    "INSERT OR REPLACE INTO config (key, value) VALUES ('mcp_api_key', ?)",
                    (old_key["value"],),
                )
            old.close()
            conn.commit()
        except Exception:
            pass  # Migration is best-effort

    conn.close()


# ── In-memory state ───────────────────────────────────────────────────────────

# client_id -> { ws, info, connected_at }
connected_clients: dict = {}
# req_id -> asyncio.Future
pending_requests: dict = {}
# UI WebSocket subscribers
ui_clients: set = set()

# ── Init on import ────────────────────────────────────────────────────────────

_init_remote_agents_db()

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

            if msg.get("type") in ("exec_response", "fs_response"):
                req_id = msg.get("req_id")
                fut = pending_requests.get(req_id)
                if fut and not fut.done():
                    fut.set_result(msg)
            elif msg.get("type") == "pong":
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
        conn = get_db()
        rows = conn.execute("SELECT * FROM remote_agents ORDER BY created_at DESC").fetchall()
        conn.close()
        agents = [_agent_with_status(dict(r)) for r in rows]
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

def _agent_with_status(row: dict) -> dict:
    agent_id = row["id"]
    conn_data = connected_clients.get(agent_id)
    return {
        "id": agent_id,
        "name": row["name"],
        "description": row["description"],
        "created_at": row["created_at"],
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
    client = connected_clients.get(client_id)
    if not client:
        raise HTTPException(404, "Client not connected")

    req_id = str(_uuid.uuid4())
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    pending_requests[req_id] = fut

    try:
        await client["ws"].send_text(json.dumps({
            "type": "exec",
            "req_id": req_id,
            "command": req.command,
        }))
        result = await asyncio.wait_for(fut, timeout=req.timeout)
        return result
    except asyncio.TimeoutError:
        raise HTTPException(408, "Command timed out")
    finally:
        pending_requests.pop(req_id, None)


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
    conn = get_db()
    rows = conn.execute("SELECT * FROM remote_agents ORDER BY created_at DESC").fetchall()
    conn.close()
    return [_agent_with_status(dict(r)) for r in rows]


@router.post("/api/remote-agents", status_code=201)
def create_remote_agent(body: RemoteAgentBody):
    agent_id = str(_uuid.uuid4())
    conn = get_db()
    conn.execute(
        "INSERT INTO remote_agents (id, name, description) VALUES (?,?,?)",
        (agent_id, body.name, body.description),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM remote_agents WHERE id=?", (agent_id,)).fetchone()
    conn.close()
    return _agent_with_status(dict(row))


@router.get("/api/remote-agents/{agent_id}")
def get_remote_agent(agent_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM remote_agents WHERE id=?", (agent_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Agent not found")
    return _agent_with_status(dict(row))


@router.put("/api/remote-agents/{agent_id}")
def update_remote_agent(agent_id: str, body: RemoteAgentBody):
    conn = get_db()
    conn.execute(
        "UPDATE remote_agents SET name=?, description=? WHERE id=?",
        (body.name, body.description, agent_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM remote_agents WHERE id=?", (agent_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Agent not found")
    return _agent_with_status(dict(row))


@router.delete("/api/remote-agents/{agent_id}")
def delete_remote_agent(agent_id: str):
    conn = get_db()
    conn.execute("DELETE FROM remote_agents WHERE id=?", (agent_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ── REST: Config ──────────────────────────────────────────────────────────────

@router.get("/api/config")
def get_config():
    conn = get_db()
    row = conn.execute("SELECT value FROM config WHERE key='mcp_api_key'").fetchone()
    conn.close()
    key = row["value"] if row else ""
    return {"mcp_api_key": key}


@router.post("/api/config/regenerate")
def regenerate_api_key():
    new_key = _sec.token_urlsafe(32)
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES ('mcp_api_key', ?)",
        (new_key,),
    )
    conn.commit()
    conn.close()
    return {"mcp_api_key": new_key}
