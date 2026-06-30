"""WebSocket endpoint for aw-connector agents to stream CLI events back to agents-platform.

aw-connector (running inside a docker container) connects here and sends each
stdout line from the CLI as {"type": "stdout", "line": "<raw json>"}.  When the
CLI process exits it sends {"type": "done", "returncode": N}.

The endpoint puts raw line strings (or None as done sentinel) into the queue that
CliLLM.astream() is reading from — no parsing here, same logic as the stdout path.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..core.ws_agent_registry import get_entry

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/agent/{run_id}")
async def ws_agent_endpoint(ws: WebSocket, run_id: str, token: str = "") -> None:
    entry = get_entry(run_id)
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
                    await q.put(line)  # raw CLI JSON line string
            elif kind == "done":
                await q.put(None)  # sentinel — tells astream() to stop
                await ws.close(code=1000)  # normal closure — aw-connector sees clean close and exits
                break
    except WebSocketDisconnect:
        # aw-connector will reconnect; queue stays alive in registry
        logger.info("aw-connector disconnected for run %s (reconnect expected)", run_id)
    except Exception as e:
        logger.warning("ws_agent error for run %s: %s", run_id, e)
