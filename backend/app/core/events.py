"""In-process event bus for live observability. Used by orchestrators and surfaced via SSE/WS."""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import datetime
from typing import Any, AsyncIterator

from ..db import session_scope
from ..models import RunEvent

# ---------------------------------------------------------------------------
# Global WebSocket broadcast hub
# ---------------------------------------------------------------------------
# Stores WebSocket.send_json callables registered by api/ws.py.
# Accessed by executor.py and targets.py to push live updates.
_ws_clients: set[Any] = set()
_ws_lock = asyncio.Lock()


async def ws_broadcast(kind: str, data: dict[str, Any]) -> None:
    """Broadcast a typed JSON message to every connected WebSocket client."""
    message = {"kind": kind, "data": data}
    async with _ws_lock:
        clients = list(_ws_clients)
    dead: list[Any] = []
    for send_fn in clients:
        try:
            await send_fn(message)
        except Exception:
            dead.append(send_fn)
    if dead:
        async with _ws_lock:
            for fn in dead:
                _ws_clients.discard(fn)


def _run_to_ws_dict(r: Any) -> dict[str, Any]:
    """Serialize a Run SQLAlchemy row to a WS-safe dict (call inside session scope)."""
    return {
        "id": r.id,
        "kind": r.kind,
        "target_slug": r.target_slug,
        "target_id": r.target_id,
        "status": r.status,
        "input": r.input,
        "output": r.output,
        "error": r.error,
        "tokens_in": r.tokens_in or 0,
        "tokens_out": r.tokens_out or 0,
        "cost_usd": r.cost_usd or 0.0,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        "parent_run_id": r.parent_run_id,
        "initiator_kind": r.initiator_kind,
        "initiator_id": r.initiator_id,
        "node_id": r.node_id,
        "model_slug": r.model_slug,
        "github_issue_number": getattr(r, "github_issue_number", None),
        "github_issue_url": getattr(r, "github_issue_url", None),
    }


def _target_to_ws_dict(t: Any) -> dict[str, Any]:
    """Serialize a Target SQLAlchemy row to a WS-safe dict (call inside session scope)."""
    return {
        "id": t.id,
        "slug": t.slug,
        "name": t.name,
        "description": t.description,
        "source_kind": t.source_kind,
        "source_ref": t.source_ref,
        "plan_canvas_id": t.plan_canvas_id,
        "report_canvas_id": t.report_canvas_id,
        "budget_tokens": t.budget_tokens,
        "budget_usd": t.budget_usd,
        "enforce_budget": t.enforce_budget,
        "status": t.status,
        "tags": t.tags or [],
        "notes": t.notes or "",
        "pr_urls": t.pr_urls or [],
        "created_by": t.created_by,
        "deleted_at": t.deleted_at.isoformat() if t.deleted_at else None,
        "started_at": t.started_at.isoformat() if t.started_at else None,
        "ended_at": t.ended_at.isoformat() if t.ended_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "github_issue_number": getattr(t, "github_issue_number", None),
        "github_issue_url": getattr(t, "github_issue_url", None),
    }


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
        # Persist off the event loop. This is a synchronous SQLAlchemy write
        # (INSERT + commit) and publish() is on the hottest path in the whole
        # system — every llm_token/tool_call/tool_result for every run, and
        # ALL dispatches (every bot, every chat) share this one event loop
        # (see telegram.py's _MAIN_LOOP). A single heavy run can emit
        # thousands of these; before this fix each one blocked the entire
        # loop for its DB round-trip, and the cumulative stall across one
        # big run (observed: a ~24M-token run) was long enough that
        # Telegram's webhook delivery timed out and silently dropped
        # messages from OTHER chats sent during that window — not a per-chat
        # queueing bug, a total event-loop-starvation bug.
        def _persist() -> None:
            try:
                with session_scope() as s:
                    s.add(RunEvent(run_id=run_id, kind=kind, node_id=node_id, payload=payload or {}))
            except Exception as e:
                # never let observability crash the run
                print(f"[eventbus] persist failed: {e}")
        await asyncio.to_thread(_persist)
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
