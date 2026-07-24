"""Remote Agents router — ported from src/custom_apps/aw-remote-agent/src/api/main.py.

Exposes the same API paths as the standalone aw-remote-agent custom app so the
Windows agent exe and FUSE driver can connect without modification.

DB lives in the `remote_agents` / `remote_agents_config` tables of the main
agents-platform Postgres database, accessed via the SQLAlchemy ORM models in
app.core.remote_agents_db (no raw SQL).
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
import asyncio, base64, hashlib, json, logging, secrets as _sec, time, uuid as _uuid, os

from ..core.remote_agents_db import ConfigRow, RemoteAgentRow, init_db, now_epoch, session_scope

# Records forwarded here go through the root logger's OTel handler (see
# app.main._setup_otel_logs) straight into SigNoz, tagged with
# remote_agent=<client_id> for filtering — no separate instrumentation
# needed on the remote machine itself.
_remote_log = logging.getLogger("remote_agent")

# ── Paths ─────────────────────────────────────────────────────────────────────

UPDATE_EXE_PATH  = "/opt/agentic-workspace/.tmp/remote-agents/update/aw-remote-agent.exe"
UPDATE_JSON_PATH = "/opt/agentic-workspace/.tmp/remote-agents/update/version.json"

# Linux client auto-update (script instead of a compiled binary)
LINUX_UPDATE_SCRIPT_PATH = "/opt/agentic-workspace/.tmp/remote-agents/update/agent.py"
LINUX_UPDATE_JSON_PATH   = "/opt/agentic-workspace/.tmp/remote-agents/update/linux-version.json"

# Chunk size used for the streaming upload/download protocol (bytes, pre-base64).
TRANSFER_CHUNK_SIZE = 256 * 1024  # 256 KB

# ── In-memory state ───────────────────────────────────────────────────────────

# client_id -> { ws, info, connected_at }
connected_clients: dict = {}
# req_id -> asyncio.Future  (fs_request/response — single-shot, unchanged)
pending_requests: dict = {}
# req_id -> asyncio.Queue  (exec — streamed chunks + final {"done": True, "returncode": ...})
exec_queues: dict = {}
# req_id -> asyncio.Queue  (fs_read_chunk stream from client: {"data": b64, "eof": bool} | {"error": ...})
download_queues: dict = {}
# req_id -> asyncio.Future  (fs_write_chunk final ack: {"ok": True} | {"error": ...})
upload_futures: dict = {}
# UI WebSocket subscribers
ui_clients: set = set()

# ── Init on import ────────────────────────────────────────────────────────────

init_db()

# ── Router ────────────────────────────────────────────────────────────────────

router = APIRouter()


def _resolve_row_id(client_id: str) -> str:
    """Resolve `client_id` — which callers may pass as either a
    RemoteAgentRow's stable `id` (UUID) or its human-readable `name`
    (e.g. "macbook-fred") — to the row's canonical `id`.

    CANONICAL IDENTIFIER RULE: `connected_clients` is always keyed by the
    row's UUID `id`, never by name. `client_ws()` normalizes the WS
    connection key through this same function on connect, so no matter
    which string the client process was launched with (`--id <uuid>` or a
    stale `--id <name>`), it always lands in `connected_clients` under the
    same key. REST handlers below re-resolve on every call for the same
    reason, since name -> id is a DB lookup, not a fixed mapping.

    Falls back to the input unchanged when no row matches (e.g. an ad-hoc,
    unregistered client_id) so that behavior is unaffected.

    This exists because of a real incident (2026-07-12): a client connected
    for months as client_id="macbook-fred" (its original `--id` arg, a
    name). After a reinstall using the profile's DB UUID per (correct, but
    previously unenforced) docs, it connected as client_id="<uuid>"
    instead — a different dict key for the same physical machine.
    `/api/clients/macbook-fred/exec` then 404'd with "Client not
    connected" even though the machine was live the whole time, which
    looked exactly like the machine being offline and wasted real
    debugging time. See docs/knowledge_base/memory/remote-agents-external-install-page.md.
    """
    with session_scope() as s:
        row = s.get(RemoteAgentRow, client_id)
        if row:
            return row.id
        row = s.query(RemoteAgentRow).filter(RemoteAgentRow.name == client_id).first()
        if row:
            return row.id
    return client_id


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
    # Normalize whatever identifier the client actually connected with (its
    # registered name OR its DB UUID — clients SHOULD always use the UUID
    # going forward, but this resolves either way) to the RemoteAgentRow's
    # canonical id, so `connected_clients` is always keyed consistently
    # regardless of which one the client process was launched with. See
    # `_resolve_row_id` for why this matters.
    client_id = _resolve_row_id(client_id)

    # This endpoint is reachable anonymously (it's on the public path list in
    # Caddy — no forward_auth cookie gate), so id/name alone must not be
    # enough to register as a given profile. Require the per-profile secret
    # `?token=` query param, minted in RemoteAgentRow.token, and reject
    # before accept() so an attacker who only knows/guesses a profile's
    # id/name can't hijack its slot in `connected_clients` and receive the
    # exec/fs commands AW intends for the real machine.
    token = ws.query_params.get("token", "")
    with session_scope() as s:
        row = s.get(RemoteAgentRow, client_id)
        row_token = row.token if row else None
    token_ok = bool(row_token) and _sec.compare_digest(token, row_token)
    if not token_ok:
        # AW_REMOTE_AGENT_GRACE_AUTH=1 is a temporary rollout switch: existing
        # clients haven't been redeployed with a token yet, so rejecting them
        # outright would drop every connected machine the moment this ships.
        # With it set, a tokenless/mismatched connect is still let through
        # (loudly logged) while each machine is redeployed with its real
        # token one at a time; turn it back off once every profile has
        # reconnected using the token form (see `connected via` in the log
        # line below, or GET /api/remote-agents to check `token_verified`
        # per client — this flag is the ONLY thing standing between the
        # current state and the pre-fix no-auth behavior, so it must not be
        # left on).
        if os.environ.get("AW_REMOTE_AGENT_GRACE_AUTH") == "1":
            _remote_log.warning(
                "remote agent connect ACCEPTED WITHOUT VALID TOKEN (grace mode): client_id=%s", client_id)
        else:
            _remote_log.warning("remote agent connect rejected (bad/missing token): client_id=%s", client_id)
            await ws.close(code=4403)
            return

    await ws.accept()
    connected_clients[client_id] = {
        "ws": ws,
        "info": {},
        "connected_at": int(time.time()),
        "token_verified": token_ok,
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

        # Bring up any tunnels declared on this profile now that the agent
        # is reachable — no separate "sube a VPN aí" step needed.
        with session_scope() as s:
            row = s.get(RemoteAgentRow, client_id)
            profile_tunnels = json.loads(row.tunnels) if row and row.tunnels else []
        if profile_tunnels:
            await _apply_tunnels_for(client_id, profile_tunnels)

        # Consecutive 40s windows with zero inbound traffic (including no
        # pong reply to our own ping below). Two in a row (~80s of total
        # silence) means the client is unreachable even though the TCP
        # connection hasn't errored out yet (e.g. a silent network black
        # hole) — stop treating it as "connected" instead of lingering in
        # `connected_clients` forever as a phantom live entry.
        missed_pings = 0
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=40)
                msg = json.loads(raw)
                missed_pings = 0
            except asyncio.TimeoutError:
                missed_pings += 1
                if missed_pings >= 2:
                    try:
                        await ws.close(code=1000, reason="ping timeout")
                    except Exception:
                        pass
                    break
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
            elif kind == "fs_read_chunk":
                # Streaming download: client pushes {"data": b64, "eof": bool}
                # or {"error": "..."} chunks for one req_id until eof/error.
                req_id = msg.get("req_id")
                q = download_queues.get(req_id)
                if q is not None:
                    await q.put(msg)
            elif kind == "fs_write_chunk_ack":
                # Streaming upload: client acks the final chunk with
                # {"ok": True} or {"error": "..."} once the file is flushed.
                req_id = msg.get("req_id")
                fut = upload_futures.get(req_id)
                if fut and not fut.done():
                    fut.set_result(msg)
            elif kind == "pong":
                pass
            elif kind == "log":
                level = getattr(logging, str(msg.get("level", "INFO")).upper(), logging.INFO)
                _remote_log.log(level, "[%s] %s", client_id, msg.get("message", ""),
                                 extra={"remote_agent": client_id})

    except (WebSocketDisconnect, Exception):
        pass
    finally:
        connected_clients.pop(client_id, None)
        await _apply_tunnels_for(client_id, [])  # free the ports — agent is unreachable
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
    try:
        tunnels = json.loads(row.tunnels) if row.tunnels else []
    except (TypeError, ValueError):
        tunnels = []
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "created_at": row.created_at,
        "tunnels": tunnels,
        "auto_mount_fuse": row.auto_mount_fuse,
        "token": row.token,
        "connected": conn_data is not None,
        "info": conn_data["info"] if conn_data else None,
        "connected_at": conn_data["connected_at"] if conn_data else None,
        "token_verified": conn_data.get("token_verified") if conn_data else None,
    }


async def _apply_tunnels_for(client_id: str, tunnels: list) -> None:
    from .tunnels import apply_profile_tunnels
    await apply_profile_tunnels(client_id, tunnels)


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
    client_id = _resolve_row_id(client_id)
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
    client_id = _resolve_row_id(client_id)
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


# ── REST: Chunked file transfer (upload/download) ─────────────────────────────
#
# The legacy `/fs` op=read/write path base64-encodes a single chunk inside one
# JSON WS message and is used by the MCP read_file/write_file tools and the
# FUSE driver — fine for small reads/config edits, but impractical above a few
# hundred KB (one giant JSON message both ways) and hard-capped at 512KB by
# the MCP tool layer. These two endpoints stream real file sizes (tens of MB+)
# by pumping many small chunk messages over the same client WebSocket instead
# of one huge message, and streaming the HTTP body to/from the caller so the
# whole file never sits fully in backend memory at once.

@router.post("/api/clients/{client_id}/upload")
async def upload_to_client(client_id: str, request: Request, path: str):
    """Stream the HTTP request body to `path` on the remote client.

    Body = raw file bytes. Target path via the `?path=` query param. Chunks
    are relayed to the client as a series of `fs_write_chunk` WS messages
    (each independently base64-encoded, capped at TRANSFER_CHUNK_SIZE
    pre-encoding), terminated by one chunk carrying `"eof": true`. The client
    writes each chunk to disk, hashes it incrementally, and replies once (after
    the file is fully written) with `fs_write_chunk_ack` carrying its own
    sha256 of the complete file. The backend independently hashes the same
    bytes as it forwards them and cross-checks against the client's reported
    digest — a mismatch (bytes corrupted/reordered in transit) fails the
    request with 500 rather than silently returning a bad checksum. On
    success, the verified sha256 is included in the JSON response.
    """
    client_id = _resolve_row_id(client_id)
    client = connected_clients.get(client_id)
    if not client:
        raise HTTPException(404, "Client not connected")

    req_id = str(_uuid.uuid4())
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    upload_futures[req_id] = fut

    total = 0
    hasher = hashlib.sha256()
    try:
        buf = b""
        async for raw_chunk in request.stream():
            buf += raw_chunk
            while len(buf) >= TRANSFER_CHUNK_SIZE:
                piece, buf = buf[:TRANSFER_CHUNK_SIZE], buf[TRANSFER_CHUNK_SIZE:]
                total += len(piece)
                hasher.update(piece)
                await client["ws"].send_text(json.dumps({
                    "type": "fs_write_chunk", "req_id": req_id, "path": path,
                    "data": base64.b64encode(piece).decode(), "eof": False,
                }))
        # Final chunk (possibly empty for a zero-byte file) carries eof=true.
        total += len(buf)
        hasher.update(buf)
        await client["ws"].send_text(json.dumps({
            "type": "fs_write_chunk", "req_id": req_id, "path": path,
            "data": base64.b64encode(buf).decode(), "eof": True,
        }))

        result = await asyncio.wait_for(fut, timeout=120)
        if result.get("error"):
            raise HTTPException(500, result["error"])

        sha256 = hasher.hexdigest()
        client_sha256 = result.get("sha256")
        if client_sha256 and client_sha256 != sha256:
            raise HTTPException(
                500,
                f"sha256 mismatch after upload: backend computed {sha256}, "
                f"client reports {client_sha256}",
            )
        return {"ok": True, "path": path, "bytes": total, "sha256": sha256}
    except asyncio.TimeoutError:
        raise HTTPException(408, "Upload timed out waiting for client ack")
    finally:
        upload_futures.pop(req_id, None)


@router.get("/api/clients/{client_id}/download")
async def download_from_client(client_id: str, path: str):
    """Fetch `path` from the remote client and stream it back as the HTTP
    response body, with the verified sha256 exposed via the `X-Sha256` header.

    Sends one `fs_read_request` WS message; the client replies with a series
    of `fs_read_chunk` messages (base64 `data` + `eof` flag, or `error`).
    Because an HTTP response's headers must be sent before its body, and we
    want `X-Sha256` to reflect a value verified against the actual bytes (not
    just trusted blindly from the client), the full transfer is first spooled
    to a temp file under `.tmp/remote-agents/downloads/` while hashing
    incrementally — bounded disk use, not full in-memory buffering, and the
    hash is more useful this way than in the alternative (a hash the client
    computed but the backend never checked). The spooled file is then served
    via a background-cleanup generator and removed once fully sent.
    """
    client_id = _resolve_row_id(client_id)
    client = connected_clients.get(client_id)
    if not client:
        raise HTTPException(404, "Client not connected")

    req_id = str(_uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    download_queues[req_id] = queue

    await client["ws"].send_text(json.dumps({
        "type": "fs_read_request", "req_id": req_id, "path": path,
    }))

    # Peek the first chunk before spooling so a "file not found" on the
    # client surfaces as a proper HTTP error instead of a truncated 200.
    try:
        first = await asyncio.wait_for(queue.get(), timeout=30)
    except asyncio.TimeoutError:
        download_queues.pop(req_id, None)
        raise HTTPException(408, "Download timed out waiting for client")

    if first.get("error"):
        download_queues.pop(req_id, None)
        raise HTTPException(404, first["error"])

    spool_dir = "/opt/agentic-workspace/.tmp/remote-agents/downloads"
    os.makedirs(spool_dir, exist_ok=True)
    spool_path = os.path.join(spool_dir, f"{req_id}.part")

    hasher = hashlib.sha256()
    total = 0
    try:
        chunk = first
        with open(spool_path, "wb") as f:
            while True:
                data = chunk.get("data")
                if data:
                    raw = base64.b64decode(data)
                    f.write(raw)
                    hasher.update(raw)
                    total += len(raw)
                if chunk.get("eof") or chunk.get("error"):
                    if chunk.get("error"):
                        raise HTTPException(500, chunk["error"])
                    break
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=60)
                except asyncio.TimeoutError:
                    raise HTTPException(408, "Download timed out mid-transfer")
    finally:
        download_queues.pop(req_id, None)

    sha256 = hasher.hexdigest()

    def _stream_spool():
        try:
            with open(spool_path, "rb") as f:
                while True:
                    piece = f.read(1024 * 1024)
                    if not piece:
                        break
                    yield piece
        finally:
            try:
                os.remove(spool_path)
            except OSError:
                pass

    filename = os.path.basename(path.replace("\\", "/")) or "download"
    return StreamingResponse(
        _stream_spool(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(total),
            "X-Sha256": sha256,
        },
    )


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


@router.get("/api/update/linux-latest")
def update_linux_latest():
    """Version manifest for the Linux client's self-update check.

    Mirrors /api/update/latest (Windows) but points at the agent.py script
    instead of a compiled binary — see /api/update/linux-script.
    """
    if not os.path.exists(LINUX_UPDATE_JSON_PATH):
        raise HTTPException(404, "No update info available")
    with open(LINUX_UPDATE_JSON_PATH) as f:
        return json.load(f)


@router.get("/api/update/linux-script")
def update_linux_script():
    if not os.path.exists(LINUX_UPDATE_SCRIPT_PATH):
        raise HTTPException(404, "Script not found")
    return FileResponse(LINUX_UPDATE_SCRIPT_PATH, filename="agent.py",
                        media_type="text/x-python")


# ── REST: Remote Agents CRUD ──────────────────────────────────────────────────

class TunnelSpec(BaseModel):
    name: str = ""
    target_port: int
    public_port: int


class RemoteAgentBody(BaseModel):
    id: str | None = None  # present only when renaming the profile id
    name: str
    description: str = ""
    tunnels: list[TunnelSpec] = []
    auto_mount_fuse: bool = True


@router.get("/api/remote-agents")
def list_remote_agents():
    with session_scope() as s:
        rows = s.query(RemoteAgentRow).order_by(RemoteAgentRow.created_at.desc()).all()
        return [_agent_with_status(r) for r in rows]


@router.post("/api/remote-agents", status_code=201)
async def create_remote_agent(body: RemoteAgentBody):
    with session_scope() as s:
        row = RemoteAgentRow(id=str(_uuid.uuid4()), name=body.name,
                             description=body.description,
                             tunnels=json.dumps([t.model_dump() for t in body.tunnels]),
                             auto_mount_fuse=body.auto_mount_fuse,
                             token=_sec.token_urlsafe(32),
                             created_at=now_epoch())
        s.add(row)
        s.flush()
        result = _agent_with_status(row)
    # Immediate effect if this agent already happens to be connected under
    # this id (unusual for a fresh profile, but keeps create/update symmetric).
    await _apply_tunnels_for(result["id"], result["tunnels"])
    return result


@router.get("/api/remote-agents/{agent_id}")
def get_remote_agent(agent_id: str):
    with session_scope() as s:
        row = s.get(RemoteAgentRow, agent_id)
        if not row:
            raise HTTPException(404, "Agent not found")
        return _agent_with_status(row)


@router.put("/api/remote-agents/{agent_id}")
async def update_remote_agent(agent_id: str, body: RemoteAgentBody):
    new_id = (body.id or agent_id).strip()
    with session_scope() as s:
        row = s.get(RemoteAgentRow, agent_id)
        if not row:
            raise HTTPException(404, "Agent not found")
        if new_id != agent_id:
            # id is the primary key (and the value clients pass as --profile),
            # so a rename is a delete+recreate, not a column update. Any
            # machine already connected under the old id needs its launch
            # command updated to the new one before it can reconnect.
            if s.get(RemoteAgentRow, new_id):
                raise HTTPException(409, "That profile name is already in use")
            row = RemoteAgentRow(
                id=new_id, name=body.name, description=body.description,
                tunnels=json.dumps([t.model_dump() for t in body.tunnels]),
                auto_mount_fuse=body.auto_mount_fuse, created_at=row.created_at,
                token=row.token or _sec.token_urlsafe(32),
            )
            s.delete(s.get(RemoteAgentRow, agent_id))
            s.flush()
            s.add(row)
        else:
            row.name = body.name
            row.description = body.description
            row.tunnels = json.dumps([t.model_dump() for t in body.tunnels])
            row.auto_mount_fuse = body.auto_mount_fuse
        s.flush()
        result = _agent_with_status(row)
    # Takes effect immediately — no need for the agent to reconnect.
    await _apply_tunnels_for(new_id, result["tunnels"])
    return result


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
