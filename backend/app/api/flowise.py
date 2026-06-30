"""Proxy endpoints for the local Flowise instance."""
from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException

from ..config import settings

router = APIRouter(prefix="/api/flowise", tags=["flowise"])

FLOWISE_BASE = getattr(settings, "flowise_url", "http://localhost:10006")


@router.get("/chatflows")
async def list_chatflows():
    """Return all chatflows from the local Flowise instance."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{FLOWISE_BASE}/api/v1/chatflows")
            r.raise_for_status()
            return r.json()
    except httpx.ConnectError:
        raise HTTPException(503, "Flowise is not running (localhost:10006)")
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, f"Flowise error: {e.response.text}")
    except Exception as e:
        raise HTTPException(503, f"Flowise unreachable: {e}")
