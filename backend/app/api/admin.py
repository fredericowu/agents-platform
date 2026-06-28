"""Admin utilities — slug generation, backfill, etc."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Agent, Run, Workflow
from ..slug_utils import generate_unique_slug

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/slugs/generate")
def generate_slug(kind: str = Query(..., pattern="^(agent|workflow)$"),
                  name: str = Query(""),
                  s: Session = Depends(get_session)):
    """Generate a unique slug for the given kind and optional name."""
    slug = generate_unique_slug(kind, s, name or None)
    return {"slug": slug}


@router.post("/slugs/backfill")
def backfill_slugs(dry_run: bool = Query(True), s: Session = Depends(get_session)):
    """Re-slug agents and workflows that don't follow the agent-xxx / workflow-xxx
    convention.  Also updates Run.target_slug so run history stays consistent.

    Pass ?dry_run=false to apply changes.
    """
    renames: list[dict] = []

    def needs_rename(slug: str, prefix: str) -> bool:
        return not slug.startswith(f"{prefix}-")

    # Collect agents to rename
    agents = s.query(Agent).filter(Agent.deleted_at.is_(None)).all()
    agent_renames: dict[str, str] = {}
    for a in agents:
        if needs_rename(a.slug, "agent"):
            new_slug = generate_unique_slug("agent", s, a.name)
            agent_renames[a.slug] = new_slug
            renames.append({"kind": "agent", "old": a.slug, "new": new_slug})

    # Collect workflows to rename
    workflows = s.query(Workflow).filter(Workflow.deleted_at.is_(None)).all()
    wf_renames: dict[str, str] = {}
    for w in workflows:
        if needs_rename(w.slug, "workflow"):
            new_slug = generate_unique_slug("workflow", s, w.name)
            wf_renames[w.slug] = new_slug
            renames.append({"kind": "workflow", "old": w.slug, "new": new_slug})

    if dry_run or not renames:
        return {"dry_run": True, "renames": renames}

    # Apply renames — agents first, then workflows, then runs
    for a in agents:
        if a.slug in agent_renames:
            new_slug = agent_renames[a.slug]
            s.query(Run).filter(Run.target_slug == a.slug).update(
                {"target_slug": new_slug}, synchronize_session=False)
            a.slug = new_slug

    for w in workflows:
        if w.slug in wf_renames:
            new_slug = wf_renames[w.slug]
            s.query(Run).filter(Run.target_slug == w.slug).update(
                {"target_slug": new_slug}, synchronize_session=False)
            w.slug = new_slug

    s.commit()
    return {"dry_run": False, "renames": renames}
