"""Global WebSocket endpoint — single connection per browser tab, broadcasts all live updates.

Connect: ws(s)://<host>/api/ws
Messages from server: {"kind": "run_update"|"target_update", "data": {...}}
Messages from client: ignored (connection kept alive by client sending pings/text)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..core.events import _ws_clients, _ws_lock

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/api/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    send_fn = websocket.send_json
    async with _ws_lock:
        _ws_clients.add(send_fn)
    logger.debug("WS client connected — total: %d", len(_ws_clients))
    try:
        # Keep the connection alive; any message from the client (e.g. ping) is ignored.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        async with _ws_lock:
            _ws_clients.discard(send_fn)
        logger.debug("WS client disconnected — total: %d", len(_ws_clients))
