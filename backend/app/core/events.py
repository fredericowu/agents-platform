"""In-process event bus for live observability. Used by orchestrators and surfaced via SSE."""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import datetime
from typing import Any, AsyncIterator

from ..db import session_scope
from ..models import RunEvent


class EventBus:
    """Pub/sub by run_id. Persists each event to the DB *and* fans out to subscribers."""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[dict]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def publish(
        self,
        run_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        node_id: str | None = None,
    ) -> None:
        evt = {
            "run_id": run_id,
            "kind": kind,
            "node_id": node_id,
            "payload": payload or {},
            "ts": datetime.utcnow().isoformat(),
        }
        # persist
        try:
            with session_scope() as s:
                s.add(RunEvent(run_id=run_id, kind=kind, node_id=node_id, payload=payload or {}))
        except Exception as e:
            # never let observability crash the run
            print(f"[eventbus] persist failed: {e}")
        # fan out
        async with self._lock:
            queues = list(self._subs.get(run_id, ()))
        for q in queues:
            await q.put(evt)

    async def subscribe(self, run_id: str) -> AsyncIterator[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=1024)
        async with self._lock:
            self._subs[run_id].add(q)
        try:
            while True:
                evt = await q.get()
                if evt.get("_close"):
                    return
                yield evt
        finally:
            async with self._lock:
                self._subs.get(run_id, set()).discard(q)

    async def close(self, run_id: str) -> None:
        async with self._lock:
            queues = list(self._subs.get(run_id, ()))
        for q in queues:
            await q.put({"_close": True})


bus = EventBus()


def sse_format(evt: dict) -> str:
    return f"data: {json.dumps(evt, default=str)}\n\n"
