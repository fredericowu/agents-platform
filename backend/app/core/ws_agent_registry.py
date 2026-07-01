"""Registry mapping run_id → (queue, token) for aw-connector WebSocket connections.

CliLLM.astream() calls register_run() before starting the docker container so the
WS endpoint has a queue ready when the connector connects back.

Token persistence: tokens are also stored in Redis (when available) so aw-connector
can reconnect after a platform restart — the in-memory entry is gone but the Redis
token survives and lets the endpoint accept the reconnection.
"""
from __future__ import annotations

import asyncio
from typing import Optional


# run_id → (queue, token)
_runs: dict[str, tuple[asyncio.Queue, str]] = {}


def register_run(run_id: str, token: str) -> asyncio.Queue:
    """Register a new run, return its event queue, and persist the token to Redis."""
    q: asyncio.Queue = asyncio.Queue()
    _runs[run_id] = (q, token)

    # Persist token to Redis asynchronously (best-effort; never blocks the caller)
    async def _persist():
        try:
            from .redis_streams import persist_token, ensure_group
            await persist_token(run_id, token)
            await ensure_group(run_id)
        except Exception:
            pass

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_persist())
    except RuntimeError:
        pass  # no event loop yet — skip persistence (tests / sync call path)

    return q


def get_entry(run_id: str) -> Optional[tuple[asyncio.Queue, str]]:
    """Return (queue, token) for the run, or None if not registered in memory."""
    return _runs.get(run_id)


async def get_entry_or_reconnect(run_id: str) -> Optional[tuple[asyncio.Queue, str]]:
    """Like get_entry() but falls back to Redis for reconnecting aw-connectors.

    If the platform restarted and the in-memory entry is gone, we look up the
    token from Redis.  On a match we create a fresh queue so the reconnecting
    aw-connector (which buffered all lines and will replay them) can continue.
    """
    entry = _runs.get(run_id)
    if entry is not None:
        return entry

    # Fallback: check Redis token store
    try:
        from .redis_streams import lookup_token
        stored_token = await lookup_token(run_id)
        if stored_token:
            # Re-create the in-memory entry so subsequent messages go to the queue
            q: asyncio.Queue = asyncio.Queue()
            _runs[run_id] = (q, stored_token)
            return (q, stored_token)
    except Exception:
        pass

    return None


def unregister_run(run_id: str) -> None:
    """Remove the run from the registry (called in finally block of astream)."""
    _runs.pop(run_id, None)
