"""CRUD for ApiKey — the bearer keys gating the OpenAI-compat surface
(``openai_compat.py``'s ``/v1/*`` routes). Each key is optionally scoped to a
list of agent/workflow slugs; empty ``agent_slugs`` means unrestricted.

The raw token is only ever returned once, at creation time — afterwards only
a masked preview (last 4 chars) is exposed, same convention as GitHub/Stripe
style API keys.
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import ApiKey

router = APIRouter(prefix="/api/api-keys", tags=["api-keys"])


class ApiKeyIn(BaseModel):
    name: str
    agent_slugs: list[str] = []


def _preview(token: str) -> str:
    return f"...{token[-4:]}" if len(token) >= 4 else "...****"


def _out(row: ApiKey, *, reveal: str | None = None) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "agent_slugs": row.agent_slugs or [],
        "token_preview": _preview(row.token),
        "token": reveal,  # only set right after creation
        "created_at": row.created_at,
        "last_used_at": row.last_used_at,
        "revoked_at": row.revoked_at,
    }


@router.get("")
def list_api_keys(s: Session = Depends(get_session)):
    rows = s.query(ApiKey).order_by(ApiKey.created_at.desc()).all()
    return [_out(r) for r in rows]


@router.post("")
def create_api_key(body: ApiKeyIn, s: Session = Depends(get_session)):
    token = secrets.token_urlsafe(32)
    row = ApiKey(name=body.name, token=token, agent_slugs=body.agent_slugs)
    s.add(row)
    s.commit()
    return _out(row, reveal=token)


@router.delete("/{key_id}")
def revoke_api_key(key_id: str, s: Session = Depends(get_session)):
    row = s.get(ApiKey, key_id)
    if not row:
        raise HTTPException(404, "api key not found")
    s.delete(row)
    s.commit()
    return {"ok": True}
