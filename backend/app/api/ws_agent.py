"""WebSocket endpoint for aw-connector agents to stream CLI events back to agents-platform.

aw-connector (running inside a docker container) connects here and sends each
stdout line from the CLI as {"type": "stdout", "line": "<raw json>"}.  When the
CLI process exits it sends {"type": "done", "returncode": N}.

The endpoint:
1. Puts raw line strings (or None as done sentinel) into the in-memory queue that
   CliLLM.astream() is reading from.
2. Also publishes each line to a Redis Stream (when Redis is available), providing
   a durable event log that survives platform restarts.

Reconnect after platform restart:
  If the in-memory entry is gone (platform restarted) but a Redis token exists for
  this run_id, get_entry_or_reconnect() recreates the queue so aw-connector (which
  buffered and will replay all lines) can resume seamlessly.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..core.ws_agent_registry import get_entry_or_reconnect

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/agent/{run_id}")
async def ws_agent_endpoint(ws: WebSocket, run_id: str, token: str = "") -> None:
    entry = await get_entry_or_reconnect(run_id)
    if entry is None:
        logger.warning("ws_agent: no queue for run %s — rejecting", run_id)
        await ws.close(code=4004)
        return

    q, expected_token = entry
    if token != expected_token:
        logger.warning("ws_agent: bad token for run %s — rejecting", run_id)
        await ws.close(code=4003)
        return

    await ws.accept()
    logger.info("aw-connector connected for run %s", run_id)

    # Import Redis helpers (graceful no-op when Redis is unavailable)
    try:
        from ..core.redis_streams import publish_line, publish_done
        _redis_ok = True
    except ImportError:
        _redis_ok = False

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            kind = msg.get("type")
            if kind == "stdout":
                line = msg.get("line", "")
                if line:
                    await q.put(line)  # in-memory queue for immediate consumption
                    if _redis_ok:
                        await publish_line(run_id, line)  # durable Redis copy
            elif kind == "done":
                returncode = msg.get("returncode", 0)
                await q.put(None)  # sentinel — tells astream() to stop
                if _redis_ok:
                    await publish_done(run_id, returncode)
                await ws.close(code=1000)
                break
    except WebSocketDisconnect:
        # aw-connector will reconnect; queue stays alive in registry
        logger.info("aw-connector disconnected for run %s (reconnect expected)", run_id)
    except Exception as e:
        logger.warning("ws_agent error for run %s: %s", run_id, e)
