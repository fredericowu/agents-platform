"""TCP tunnel router — bridges a raw TCP port to a service listening on a
connected Remote Agent's loopback interface (e.g. macOS Screen Sharing on
127.0.0.1:5900), without a VPN.

Design: one dedicated WebSocket per TCP connection, not multiplexed in-band
on the agent's control channel. Each inbound TCP connection gets its own
tunnel_id; the agent dials 127.0.0.1:<target_port> and opens a fresh WS to
/ws/tunnel/{tunnel_id} to relay bytes. Simpler than in-band multiplexing and
avoids one slow/stalled tunnel head-of-line-blocking the control channel or
any other concurrent tunnel — see [[remote-agent-tunnel]] design discussion.

A profile can declare multiple tunnels (e.g. VNC + something else), each its
own public_port -> target_port mapping. `apply_profile_tunnels()` reconciles
the running listeners for a client against its declared config — called on
profile save (immediate effect) and on every agent connect/disconnect.
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel
import asyncio, json, logging, uuid as _uuid

from .remote_agents import connected_clients

log = logging.getLogger("tunnels")

router = APIRouter()

# tunnel_id -> asyncio.Future[WebSocket], resolved when the agent's dedicated
# tunnel websocket connects back and identifies itself.
_pending: dict = {}
# tunnel_id -> asyncio.Event, set once the bridge for that tunnel_id has
# finished relaying — /ws/tunnel/{tunnel_id} waits on this so Starlette
# doesn't tear the socket down before the bridge is done reading from it.
_done: dict = {}
# (client_id, public_port) -> running asyncio.Server
_listeners: dict = {}


class TunnelRequest(BaseModel):
    target_port: int
    public_port: int


async def _bridge(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                   client_id: str, target_port: int) -> None:
    tunnel_id = str(_uuid.uuid4())
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    _pending[tunnel_id] = fut
    _done[tunnel_id] = asyncio.Event()

    client = connected_clients.get(client_id)
    if not client:
        writer.close()
        _pending.pop(tunnel_id, None)
        _done.pop(tunnel_id, None)
        return

    await client["ws"].send_text(json.dumps({
        "type": "tunnel_request", "tunnel_id": tunnel_id, "target_port": target_port,
    }))

    try:
        tunnel_ws = await asyncio.wait_for(fut, timeout=15)
    except asyncio.TimeoutError:
        log.warning("tunnel %s: agent never connected back", tunnel_id)
        writer.close()
        _pending.pop(tunnel_id, None)
        _done.pop(tunnel_id, None)
        return

    async def tcp_to_ws():
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                await tunnel_ws.send_bytes(chunk)
        except Exception:
            pass

    async def ws_to_tcp():
        try:
            while True:
                chunk = await tunnel_ws.receive_bytes()
                writer.write(chunk)
                await writer.drain()
        except Exception:
            pass

    try:
        await asyncio.gather(tcp_to_ws(), ws_to_tcp(), return_exceptions=True)
    finally:
        writer.close()
        try:
            await tunnel_ws.close()
        except Exception:
            pass
        _done[tunnel_id].set()
        _done.pop(tunnel_id, None)


async def start_named_tunnel(client_id: str, target_port: int, public_port: int) -> None:
    key = (client_id, public_port)
    if key in _listeners:
        return

    async def handler(reader, writer):
        asyncio.create_task(_bridge(reader, writer, client_id, target_port))

    server = await asyncio.start_server(handler, "0.0.0.0", public_port)
    _listeners[key] = server
    log.info("tunnel started: client=%s public_port=%s -> 127.0.0.1:%s (on the agent)",
              client_id, public_port, target_port)


async def stop_named_tunnel(client_id: str, public_port: int) -> None:
    server = _listeners.pop((client_id, public_port), None)
    if not server:
        return
    server.close()
    await server.wait_closed()
    log.info("tunnel stopped: client=%s public_port=%s", client_id, public_port)


async def apply_profile_tunnels(client_id: str, tunnels: list) -> None:
    """Reconcile running listeners for `client_id` against the desired
    tunnels list [{name, target_port, public_port}, ...] — starts missing
    ones, stops ones no longer declared. Safe to call repeatedly/with an
    empty list (e.g. on disconnect, to tear everything down)."""
    desired = {
        int(t["public_port"]): int(t["target_port"])
        for t in tunnels if t.get("public_port") and t.get("target_port")
    }
    running_ports = {port for (cid, port) in _listeners if cid == client_id}
    for port in running_ports - desired.keys():
        await stop_named_tunnel(client_id, port)
    for port, target in desired.items():
        if port not in running_ports:
            try:
                await start_named_tunnel(client_id, target, port)
            except OSError as e:
                log.warning("tunnel client=%s public_port=%s failed to bind: %s", client_id, port, e)


@router.post("/api/clients/{client_id}/tunnels")
async def start_tunnel(client_id: str, req: TunnelRequest):
    if client_id not in connected_clients:
        raise HTTPException(404, "Agent not connected")
    if (client_id, req.public_port) in _listeners:
        raise HTTPException(409, "A tunnel is already running on this public port")
    try:
        await start_named_tunnel(client_id, req.target_port, req.public_port)
    except OSError as e:
        raise HTTPException(400, f"couldn't bind public_port {req.public_port}: {e}")
    return {"ok": True, "public_port": req.public_port, "target_port": req.target_port}


@router.delete("/api/clients/{client_id}/tunnels/{public_port}")
async def stop_tunnel(client_id: str, public_port: int):
    if (client_id, public_port) not in _listeners:
        raise HTTPException(404, "No tunnel running on this public port")
    await stop_named_tunnel(client_id, public_port)
    return {"ok": True}


@router.get("/api/clients/{client_id}/tunnels")
async def get_tunnel(client_id: str):
    ports = sorted(port for (cid, port) in _listeners if cid == client_id)
    return {"running": bool(ports), "public_ports": ports}


@router.websocket("/ws/tunnel/{tunnel_id}")
async def tunnel_ws(ws: WebSocket, tunnel_id: str):
    await ws.accept()
    fut = _pending.pop(tunnel_id, None)
    if not fut or fut.done():
        await ws.close()
        return
    fut.set_result(ws)
    event = _done.get(tunnel_id)
    try:
        if event:
            await event.wait()
    except WebSocketDisconnect:
        pass
