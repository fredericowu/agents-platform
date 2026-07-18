"""Generic external-caller identities + message history.

Populated by ``POST /v1/chat/completions`` whenever a request carries the
``X-Caller-Meta-Id`` / ``X-Caller-Meta-Info`` / ``X-Caller-Meta-Source``
headers (see openai_compat.py) — e.g. a Roblox player talking to an in-game
NPC agent. Read-only here; writes happen inline in the chat-completions path.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..db import session_scope
from ..models import CallerIdentity, CallerMessage

router = APIRouter(prefix="/api/callers", tags=["callers"])


def _identity_out(row: CallerIdentity) -> dict:
    return {
        "id": row.id,
        "source": row.source,
        "external_id": row.external_id,
        "meta_info": row.meta_info,
        "first_seen": row.first_seen.isoformat(),
        "last_seen": row.last_seen.isoformat(),
    }


@router.get("")
def list_callers(source: str | None = Query(None)) -> list[dict]:
    with session_scope() as s:
        q = s.query(CallerIdentity)
        if source:
            q = q.filter(CallerIdentity.source == source)
        rows = q.order_by(CallerIdentity.last_seen.desc()).all()
        return [_identity_out(r) for r in rows]


@router.get("/messages")
def list_caller_messages(source: str = Query(...), external_id: str = Query(...),
                         limit: int = Query(50, le=500)) -> dict:
    with session_scope() as s:
        identity = (s.query(CallerIdentity)
                    .filter(CallerIdentity.source == source,
                            CallerIdentity.external_id == external_id)
                    .first())
        if identity is None:
            raise HTTPException(404, f"no caller {source}/{external_id}")
        msgs = (s.query(CallerMessage)
                .filter(CallerMessage.caller_identity_id == identity.id)
                .order_by(CallerMessage.created_at.desc())
                .limit(limit).all())
        msgs.reverse()  # oldest first
        return {
            "identity": _identity_out(identity),
            "messages": [
                {"role": m.role, "content": m.content, "created_at": m.created_at.isoformat()}
                for m in msgs
            ],
        }
