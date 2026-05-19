from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..core.executor import start_agent_run_bg
from ..db import get_session
from ..models import Agent
from ..schemas import AgentIn, AgentOut, AgentUpdate, RunInput

router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.get("/_resettable")
def list_resettable_agents():
    """Slugs that have seed defaults — used by the UI to decide where to show
    the 'reset to default' button."""
    from ..seed import SEED_AGENTS
    return {a["slug"] for a in SEED_AGENTS}


@router.get("", response_model=list[AgentOut])
def list_agents(include_deleted: bool = Query(False),
                deleted_only: bool = Query(False),
                exclude_pattern: str | None = Query(None,
                    description="SQL LIKE pattern (use % wildcards) applied to slug; matching rows excluded. "
                                "E.g. 'agent-ui-%' hides the UI-test clutter."),
                s: Session = Depends(get_session)):
    """List agents. By default soft-deleted rows are excluded.

    Query params:
      include_deleted=true → return active **and** soft-deleted rows
      deleted_only=true    → return only soft-deleted rows (trash view)
      exclude_pattern      → SQL LIKE pattern to drop matching slugs (clutter filter)
    """
    q = s.query(Agent)
    if deleted_only:
        q = q.filter(Agent.deleted_at.is_not(None))
    elif not include_deleted:
        q = q.filter(Agent.deleted_at.is_(None))
    if exclude_pattern:
        q = q.filter(~Agent.slug.like(exclude_pattern))
    return q.order_by(Agent.name).all()


@router.get("/{slug}", response_model=AgentOut)
def get_agent(slug: str, include_deleted: bool = Query(False),
              s: Session = Depends(get_session)):
    a = s.query(Agent).filter(Agent.slug == slug).first()
    if not a:
        raise HTTPException(404, "not found")
    if a.deleted_at is not None and not include_deleted:
        raise HTTPException(404, "deleted (use include_deleted=true to view)")
    return a


@router.post("", response_model=AgentOut)
def create_agent(body: AgentIn, s: Session = Depends(get_session)):
    """Create a new agent. If a soft-deleted agent with the same slug exists,
    creation fails with 409 — restore it via POST /:slug/restore instead, or
    pick a different slug."""
    existing = s.query(Agent).filter(Agent.slug == body.slug).first()
    if existing is not None:
        if existing.deleted_at is not None:
            raise HTTPException(409,
                "slug exists but is soft-deleted — restore it or pick another slug")
        raise HTTPException(409, "slug already exists")
    a = Agent(**body.model_dump())
    s.add(a)
    s.commit()
    s.refresh(a)
    return a


@router.put("/{slug}", response_model=AgentOut)
def update_agent(slug: str, body: AgentUpdate, s: Session = Depends(get_session)):
    a = s.query(Agent).filter(Agent.slug == slug).first()
    if not a:
        raise HTTPException(404, "not found")
    if a.deleted_at is not None:
        raise HTTPException(409, "agent is soft-deleted — restore it first")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(a, k, v)
    s.commit()
    s.refresh(a)
    return a


@router.delete("/{slug}")
def delete_agent(slug: str, hard: bool = Query(False),
                 s: Session = Depends(get_session)):
    """Soft-delete an agent (sets ``deleted_at`` to now). The row stays in the
    DB and can be restored. Pass ``?hard=true`` to permanently delete (irreversible)."""
    a = s.query(Agent).filter(Agent.slug == slug).first()
    if not a:
        raise HTTPException(404, "not found")
    if hard:
        s.delete(a)
        s.commit()
        return {"deleted": slug, "soft": False}
    if a.deleted_at is None:
        a.deleted_at = datetime.utcnow()
        s.commit()
    return {"deleted": slug, "soft": True, "deleted_at": a.deleted_at}


@router.post("/{slug}/restore", response_model=AgentOut)
def restore_agent(slug: str, s: Session = Depends(get_session)):
    """Undo a soft-delete by clearing ``deleted_at``."""
    a = s.query(Agent).filter(Agent.slug == slug).first()
    if not a:
        raise HTTPException(404, "not found")
    if a.deleted_at is None:
        raise HTTPException(409, "not deleted")
    a.deleted_at = None
    s.commit()
    s.refresh(a)
    return a


@router.post("/{slug}/reset", response_model=AgentOut)
def reset_agent(slug: str, s: Session = Depends(get_session)):
    """Restore an agent to its seed-list defaults. Only works for slugs that
    exist in SEED_AGENTS (the platform's bundled list)."""
    from ..seed import SEED_AGENTS
    spec = next((a for a in SEED_AGENTS if a["slug"] == slug), None)
    if spec is None:
        raise HTTPException(400, "no seed defaults exist for this slug")
    a = s.query(Agent).filter(Agent.slug == slug).first()
    if a is None:
        a = Agent(**spec)
        s.add(a)
    else:
        for k, v in spec.items():
            setattr(a, k, v)
    s.commit(); s.refresh(a)
    return a


@router.get("/{slug}/export")
def export_agent(slug: str, s: Session = Depends(get_session)):
    a = s.query(Agent).filter(Agent.slug == slug).first()
    if not a:
        raise HTTPException(404, "not found")
    return {
        "_kind": "agent", "_version": 1,
        "slug": a.slug, "name": a.name, "description": a.description,
        "system_prompt": a.system_prompt, "model_slug": a.model_slug,
        "tool_specs": a.tool_specs, "skill_slugs": a.skill_slugs,
        "params": a.params, "icon": a.icon, "color": a.color,
    }


from pydantic import BaseModel as _BM
class _ImportAgent(_BM):
    slug: str | None = None
    name: str
    description: str = ""
    system_prompt: str = ""
    model_slug: str | None = None
    tool_specs: list = []
    skill_slugs: list = []
    params: dict = {}
    icon: str = "bot"
    color: str = "#58a6ff"


@router.post("/import", response_model=AgentOut)
def import_agent(body: _ImportAgent, s: Session = Depends(get_session)):
    """Import an agent. If slug exists, picks <slug>-imported[-N]."""
    base = body.slug or "imported-agent"
    new_slug = base
    i = 2
    while s.query(Agent).filter(Agent.slug == new_slug).first():
        new_slug = f"{base}-imported" if i == 2 else f"{base}-imported-{i}"
        i += 1
    a = Agent(slug=new_slug, name=body.name, description=body.description,
              system_prompt=body.system_prompt, model_slug=body.model_slug,
              tool_specs=body.tool_specs, skill_slugs=body.skill_slugs,
              params=body.params, icon=body.icon, color=body.color)
    s.add(a); s.commit(); s.refresh(a)
    return a


@router.post("/{slug}/clone", response_model=AgentOut)
def clone_agent(slug: str, s: Session = Depends(get_session)):
    src = s.query(Agent).filter(Agent.slug == slug).first()
    if not src:
        raise HTTPException(404, "not found")
    # find a unique new slug: <slug>-copy, -copy-2, etc.
    base = f"{slug}-copy"
    new_slug = base
    i = 2
    while s.query(Agent).filter(Agent.slug == new_slug).first():
        new_slug = f"{base}-{i}"
        i += 1
    clone = Agent(
        slug=new_slug,
        name=f"{src.name} (copy)",
        description=src.description,
        system_prompt=src.system_prompt,
        model_slug=src.model_slug,
        tool_specs=list(src.tool_specs or []),
        skill_slugs=list(src.skill_slugs or []),
        params=dict(src.params or {}),
        icon=src.icon, color=src.color,
    )
    s.add(clone); s.commit(); s.refresh(clone)
    return clone


@router.post("/{slug}/run")
async def run_agent_ep(slug: str, body: RunInput, s: Session = Depends(get_session)):
    a = s.query(Agent).filter(Agent.slug == slug).first()
    if not a:
        raise HTTPException(404, "not found")
    payload = body.input.get("input", "") if isinstance(body.input, dict) else str(body.input)
    # Prefer first-class body fields; fall back to legacy input.extra for compat.
    extra = body.input.get("extra", {}) if isinstance(body.input, dict) else {}
    target_id = body.target_id or (extra.get("target_id") if isinstance(extra, dict) else None)
    target_slug = body.target_slug or (extra.get("target_slug") if isinstance(extra, dict) else None)
    if target_id is None and target_slug:
        from ..models import Target
        t = s.query(Target).filter(Target.slug == target_slug).first()
        if t is None:
            raise HTTPException(404, f"target slug '{target_slug}' not found")
        target_id = t.id
    if target_id is None:
        raise HTTPException(400, "target_slug is required — pass a target_slug to link this run to a delivery Target")
    try:
        rid = start_agent_run_bg(slug, payload, target_id=target_id)
    except __import__("backend.app.core.executor", fromlist=["TargetBudgetExceeded"]).TargetBudgetExceeded as e:
        raise HTTPException(429, f"target budget exceeded: {e}")
    return {"run_id": rid, "target_id": target_id}
