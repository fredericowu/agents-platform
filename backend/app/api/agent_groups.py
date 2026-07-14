from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Agent, AgentGroup
from ..schemas import AgentGroupIn, AgentGroupOut, AgentGroupUpdate

router = APIRouter(prefix="/api/agent-groups", tags=["agent-groups"])


def _slug_taken(slug: str, s: Session, exclude_id: str | None = None) -> bool:
    q = s.query(AgentGroup).filter(AgentGroup.slug == slug)
    if exclude_id:
        q = q.filter(AgentGroup.id != exclude_id)
    return q.first() is not None


def _generate_slug(s: Session, name: str) -> str:
    import re
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "agent-group"
    candidate = base
    i = 2
    while _slug_taken(candidate, s):
        candidate = f"{base}-{i}"
        i += 1
    return candidate


@router.get("", response_model=list[AgentGroupOut])
def list_agent_groups(include_deleted: bool = Query(False), s: Session = Depends(get_session)):
    q = s.query(AgentGroup)
    if not include_deleted:
        q = q.filter(AgentGroup.deleted_at.is_(None))
    return q.order_by(AgentGroup.name).all()


@router.get("/{slug}", response_model=AgentGroupOut)
def get_agent_group(slug: str, s: Session = Depends(get_session)):
    g = s.query(AgentGroup).filter(AgentGroup.slug == slug).first()
    if not g:
        raise HTTPException(404, "not found")
    return g


@router.get("/{slug}/members")
def list_agent_group_members(slug: str, s: Session = Depends(get_session)):
    members = s.query(Agent).filter(Agent.group_slug == slug, Agent.deleted_at.is_(None)).order_by(Agent.name).all()
    return [{"slug": a.slug, "name": a.name, "model_slug": a.model_slug} for a in members]


@router.post("", response_model=AgentGroupOut)
def create_agent_group(body: AgentGroupIn, s: Session = Depends(get_session)):
    slug = (body.slug or "").strip() or _generate_slug(s, body.name)
    if _slug_taken(slug, s):
        raise HTTPException(409, "slug already exists")
    g = AgentGroup(slug=slug, name=body.name, description=body.description, instructions=body.instructions)
    s.add(g)
    s.commit()
    s.refresh(g)
    return g


@router.put("/{slug}", response_model=AgentGroupOut)
def update_agent_group(slug: str, body: AgentGroupUpdate, s: Session = Depends(get_session)):
    g = s.query(AgentGroup).filter(AgentGroup.slug == slug).first()
    if not g:
        raise HTTPException(404, "not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(g, k, v)
    s.commit()
    s.refresh(g)
    return g


from pydantic import BaseModel as _BM
class _RenameAgentGroup(_BM):
    new_slug: str


@router.post("/{slug}/rename", response_model=AgentGroupOut)
def rename_agent_group(slug: str, body: _RenameAgentGroup, s: Session = Depends(get_session)):
    """Rename a group's slug. Also repoints any member Agent.group_slug."""
    g = s.query(AgentGroup).filter(AgentGroup.slug == slug).first()
    if not g:
        raise HTTPException(404, "not found")
    new_slug = body.new_slug.strip()
    if not new_slug:
        raise HTTPException(400, "new_slug is required")
    if new_slug == slug:
        return g
    if _slug_taken(new_slug, s):
        raise HTTPException(409, "slug already exists")
    s.query(Agent).filter(Agent.group_slug == slug).update(
        {"group_slug": new_slug}, synchronize_session=False)
    g.slug = new_slug
    s.commit(); s.refresh(g)
    return g


@router.post("/{slug}/members/{agent_slug}")
def add_agent_group_member(slug: str, agent_slug: str, s: Session = Depends(get_session)):
    g = s.query(AgentGroup).filter(AgentGroup.slug == slug).first()
    if not g:
        raise HTTPException(404, "group not found")
    a = s.query(Agent).filter(Agent.slug == agent_slug).first()
    if not a:
        raise HTTPException(404, "agent not found")
    a.group_slug = slug
    s.commit()
    return {"agent": agent_slug, "group": slug}


@router.delete("/{slug}/members/{agent_slug}")
def remove_agent_group_member(slug: str, agent_slug: str, s: Session = Depends(get_session)):
    a = s.query(Agent).filter(Agent.slug == agent_slug, Agent.group_slug == slug).first()
    if not a:
        raise HTTPException(404, "agent not in this group")
    a.group_slug = None
    s.commit()
    return {"agent": agent_slug, "group": None}


@router.delete("/{slug}")
def delete_agent_group(slug: str, hard: bool = Query(False), s: Session = Depends(get_session)):
    g = s.query(AgentGroup).filter(AgentGroup.slug == slug).first()
    if not g:
        raise HTTPException(404, "not found")
    if hard:
        s.delete(g)
        s.commit()
        return {"deleted": slug, "soft": False}
    g.deleted_at = datetime.utcnow()
    s.commit()
    return {"deleted": slug, "soft": True}
