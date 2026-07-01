from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Agent, AgentConfig
from ..schemas import AgentConfigIn, AgentConfigOut, AgentConfigUpdate

router = APIRouter(prefix="/api/agent-configs", tags=["agent-configs"])


def _slug_taken(slug: str, s: Session) -> bool:
    return s.query(AgentConfig).filter(AgentConfig.slug == slug).first() is not None


def _generate_slug(s: Session, name: str) -> str:
    import random
    import re
    import string
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "config"
    candidate = f"agent-config-{base}"
    if not _slug_taken(candidate, s):
        return candidate
    chars = string.ascii_lowercase + string.digits
    for _ in range(20):
        suffix = "".join(random.choices(chars, k=4))
        candidate = f"agent-config-{base}-{suffix}"
        if not _slug_taken(candidate, s):
            return candidate
    raise RuntimeError("could not generate a unique agent-config slug")


@router.get("", response_model=list[AgentConfigOut])
def list_agent_configs(include_deleted: bool = Query(False),
                        s: Session = Depends(get_session)):
    q = s.query(AgentConfig)
    if not include_deleted:
        q = q.filter(AgentConfig.deleted_at.is_(None))
    return q.order_by(AgentConfig.name).all()


@router.get("/{slug}", response_model=AgentConfigOut)
def get_agent_config(slug: str, s: Session = Depends(get_session)):
    c = s.query(AgentConfig).filter(AgentConfig.slug == slug).first()
    if not c:
        raise HTTPException(404, "not found")
    return c


@router.post("", response_model=AgentConfigOut)
def create_agent_config(body: AgentConfigIn, s: Session = Depends(get_session)):
    slug = (body.slug or "").strip() or _generate_slug(s, body.name)
    if _slug_taken(slug, s):
        raise HTTPException(409, "slug already exists")
    c = AgentConfig(slug=slug, name=body.name, description=body.description,
                     mcp_config=body.mcp_config, extra_volumes=body.extra_volumes,
                     permissions=body.permissions)
    s.add(c); s.commit(); s.refresh(c)
    return c


@router.put("/{slug}", response_model=AgentConfigOut)
def update_agent_config(slug: str, body: AgentConfigUpdate, s: Session = Depends(get_session)):
    c = s.query(AgentConfig).filter(AgentConfig.slug == slug).first()
    if not c:
        raise HTTPException(404, "not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(c, k, v)
    s.commit(); s.refresh(c)
    return c


@router.delete("/{slug}")
def delete_agent_config(slug: str, hard: bool = Query(False),
                        s: Session = Depends(get_session)):
    c = s.query(AgentConfig).filter(AgentConfig.slug == slug).first()
    if not c:
        raise HTTPException(404, "not found")
    in_use = s.query(Agent).filter(Agent.agent_config_slug == slug,
                                    Agent.deleted_at.is_(None)).count()
    if in_use and hard:
        raise HTTPException(409, f"in use by {in_use} agent(s) — unlink them first")
    if hard:
        s.delete(c); s.commit()
        return {"deleted": slug, "soft": False}
    if c.deleted_at is None:
        c.deleted_at = datetime.utcnow()
        s.commit()
    return {"deleted": slug, "soft": True, "deleted_at": c.deleted_at, "in_use_by": in_use}
