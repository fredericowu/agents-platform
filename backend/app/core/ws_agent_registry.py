"""In-memory registry mapping run_id → (queue, token) for aw-connector WebSocket connections.

CliLLM.astream() calls register_run() before starting the docker container so the
WS endpoint has a queue ready when the connector connects back.
"""
from __future__ import annotations

import asyncio
from typing import Optional


# run_id → (queue, token)
_runs: dict[str, tuple[asyncio.Queue, str]] = {}


def register_run(run_id: str, token: str) -> asyncio.Queue:
    """Register a new run and return its event queue."""
    q: asyncio.Queue = asyncio.Queue()
    _runs[run_id] = (q, token)
    return q


def get_entry(run_id: str) -> Optional[tuple[asyncio.Queue, str]]:
    """Return (queue, token) for the run, or None if not registered."""
    return _runs.get(run_id)


def unregister_run(run_id: str) -> None:
    """Remove the run from the registry (called in finally block of astream)."""
    _runs.pop(run_id, None)
