from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import AgentFlow
from ..schemas import AgentFlowIn, AgentFlowOut, AgentFlowUpdate

router = APIRouter(prefix="/api/agent-flows", tags=["agent-flows"])


def _slug_taken(slug: str, s: Session, exclude_id: str | None = None) -> bool:
    q = s.query(AgentFlow).filter(AgentFlow.slug == slug)
    if exclude_id:
        q = q.filter(AgentFlow.id != exclude_id)
    return q.first() is not None


def _generate_slug(s: Session, name: str) -> str:
    import re
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "agent-flow"
    candidate = base
    i = 2
    while _slug_taken(candidate, s):
        candidate = f"{base}-{i}"
        i += 1
    return candidate


@router.get("", response_model=list[AgentFlowOut])
def list_agent_flows(include_deleted: bool = Query(False), s: Session = Depends(get_session)):
    q = s.query(AgentFlow)
    if not include_deleted:
        q = q.filter(AgentFlow.deleted_at.is_(None))
    return q.order_by(AgentFlow.name).all()


@router.get("/{slug}", response_model=AgentFlowOut)
def get_agent_flow(slug: str, s: Session = Depends(get_session)):
    f = s.query(AgentFlow).filter(AgentFlow.slug == slug).first()
    if not f:
        raise HTTPException(404, "not found")
    return f


@router.post("", response_model=AgentFlowOut)
def create_agent_flow(body: AgentFlowIn, s: Session = Depends(get_session)):
    slug = (body.slug or "").strip() or _generate_slug(s, body.name)
    if _slug_taken(slug, s):
        raise HTTPException(409, "slug already exists")
    f = AgentFlow(slug=slug, name=body.name, description=body.description, graph=body.graph)
    s.add(f)
    s.commit()
    s.refresh(f)
    return f


@router.put("/{slug}", response_model=AgentFlowOut)
def update_agent_flow(slug: str, body: AgentFlowUpdate, s: Session = Depends(get_session)):
    f = s.query(AgentFlow).filter(AgentFlow.slug == slug).first()
    if not f:
        raise HTTPException(404, "not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(f, k, v)
    s.commit()
    s.refresh(f)
    return f


from pydantic import BaseModel as _BM
class _RenameAgentFlow(_BM):
    new_slug: str


@router.post("/{slug}/rename", response_model=AgentFlowOut)
def rename_agent_flow(slug: str, body: _RenameAgentFlow, s: Session = Depends(get_session)):
    f = s.query(AgentFlow).filter(AgentFlow.slug == slug).first()
    if not f:
        raise HTTPException(404, "not found")
    new_slug = body.new_slug.strip()
    if not new_slug:
        raise HTTPException(400, "new_slug is required")
    if new_slug == slug:
        return f
    if _slug_taken(new_slug, s):
        raise HTTPException(409, "slug already exists")
    f.slug = new_slug
    s.commit(); s.refresh(f)
    return f


@router.delete("/{slug}")
def delete_agent_flow(slug: str, hard: bool = Query(False), s: Session = Depends(get_session)):
    f = s.query(AgentFlow).filter(AgentFlow.slug == slug).first()
    if not f:
        raise HTTPException(404, "not found")
    if hard:
        s.delete(f)
        s.commit()
        return {"deleted": slug, "soft": False}
    f.deleted_at = datetime.utcnow()
    s.commit()
    return {"deleted": slug, "soft": True}


@router.post("/{slug}/clone", response_model=AgentFlowOut)
def clone_agent_flow(slug: str, s: Session = Depends(get_session)):
    src = s.query(AgentFlow).filter(AgentFlow.slug == slug).first()
    if not src:
        raise HTTPException(404, "not found")
    new_slug = _generate_slug(s, f"{src.name} copy")
    clone = AgentFlow(slug=new_slug, name=f"{src.name} (copy)",
                      description=src.description, graph=dict(src.graph or {}))
    s.add(clone)
    s.commit()
    s.refresh(clone)
    return clone
