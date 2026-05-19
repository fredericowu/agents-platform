from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..core.executor import start_workflow_run_bg
from ..core.orchestrators import derive_kind_from_graph
from ..db import get_session
from ..models import Workflow
from ..schemas import RunInput, WorkflowIn, WorkflowOut, WorkflowUpdate

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


@router.get("/_resettable")
def list_resettable_workflows():
    from ..seed import SEED_WORKFLOWS
    return {w["slug"] for w in SEED_WORKFLOWS}


@router.get("", response_model=list[WorkflowOut])
def list_workflows(include_deleted: bool = Query(False),
                   deleted_only: bool = Query(False),
                   exclude_pattern: str | None = Query(None,
                       description="SQL LIKE pattern (use % wildcards) applied to slug; matching rows excluded. "
                                   "E.g. 'wf-ui-%' hides the UI-test clutter."),
                   s: Session = Depends(get_session)):
    """List workflows. By default soft-deleted rows are excluded."""
    q = s.query(Workflow)
    if deleted_only:
        q = q.filter(Workflow.deleted_at.is_not(None))
    elif not include_deleted:
        q = q.filter(Workflow.deleted_at.is_(None))
    if exclude_pattern:
        q = q.filter(~Workflow.slug.like(exclude_pattern))
    return q.order_by(Workflow.name).all()


@router.get("/{slug}", response_model=WorkflowOut)
def get_workflow(slug: str, include_deleted: bool = Query(False),
                 s: Session = Depends(get_session)):
    w = s.query(Workflow).filter(Workflow.slug == slug).first()
    if not w:
        raise HTTPException(404, "not found")
    if w.deleted_at is not None and not include_deleted:
        raise HTTPException(404, "deleted (use include_deleted=true to view)")
    return w


def _normalize_kind(payload: dict) -> dict:
    """If the caller didn't set a kind, derive it from the graph. Always set
    kind to whatever the graph topology says — kind is now a *derived* field."""
    if "graph" in payload and payload["graph"] is not None:
        payload["kind"] = derive_kind_from_graph(payload["graph"],
                                                 fallback=payload.get("kind"))
    return payload


@router.post("", response_model=WorkflowOut)
def create_workflow(body: WorkflowIn, s: Session = Depends(get_session)):
    existing = s.query(Workflow).filter(Workflow.slug == body.slug).first()
    if existing is not None:
        if existing.deleted_at is not None:
            raise HTTPException(409,
                "slug exists but is soft-deleted — restore it or pick another slug")
        raise HTTPException(409, "slug already exists")
    data = _normalize_kind(body.model_dump())
    w = Workflow(**data)
    s.add(w)
    s.commit()
    s.refresh(w)
    return w


@router.put("/{slug}", response_model=WorkflowOut)
def update_workflow(slug: str, body: WorkflowUpdate, s: Session = Depends(get_session)):
    w = s.query(Workflow).filter(Workflow.slug == slug).first()
    if not w:
        raise HTTPException(404, "not found")
    if w.deleted_at is not None:
        raise HTTPException(409, "workflow is soft-deleted — restore it first")
    patch = _normalize_kind(body.model_dump(exclude_unset=True))
    for k, v in patch.items():
        setattr(w, k, v)
    s.commit()
    s.refresh(w)
    return w


@router.delete("/{slug}")
def delete_workflow(slug: str, hard: bool = Query(False),
                    s: Session = Depends(get_session)):
    """Soft-delete a workflow (sets ``deleted_at``). Pass ``?hard=true`` to
    permanently delete (irreversible)."""
    w = s.query(Workflow).filter(Workflow.slug == slug).first()
    if not w:
        raise HTTPException(404, "not found")
    if hard:
        s.delete(w)
        s.commit()
        return {"deleted": slug, "soft": False}
    if w.deleted_at is None:
        w.deleted_at = datetime.utcnow()
        s.commit()
    return {"deleted": slug, "soft": True, "deleted_at": w.deleted_at}


@router.post("/{slug}/restore", response_model=WorkflowOut)
def restore_workflow(slug: str, s: Session = Depends(get_session)):
    """Undo a soft-delete by clearing ``deleted_at``."""
    w = s.query(Workflow).filter(Workflow.slug == slug).first()
    if not w:
        raise HTTPException(404, "not found")
    if w.deleted_at is None:
        raise HTTPException(409, "not deleted")
    w.deleted_at = None
    s.commit()
    s.refresh(w)
    return w


@router.post("/{slug}/reset", response_model=WorkflowOut)
def reset_workflow(slug: str, s: Session = Depends(get_session)):
    """Restore a workflow to its seed-list defaults."""
    from ..seed import SEED_WORKFLOWS
    spec = next((w for w in SEED_WORKFLOWS if w["slug"] == slug), None)
    if spec is None:
        raise HTTPException(400, "no seed defaults exist for this slug")
    w = s.query(Workflow).filter(Workflow.slug == slug).first()
    if w is None:
        w = Workflow(**spec)
        s.add(w)
    else:
        for k, v in spec.items():
            setattr(w, k, v)
    s.commit(); s.refresh(w)
    return w


@router.get("/{slug}/export")
def export_workflow(slug: str, s: Session = Depends(get_session)):
    w = s.query(Workflow).filter(Workflow.slug == slug).first()
    if not w:
        raise HTTPException(404, "not found")
    return {"_kind": "workflow", "_version": 1,
            "slug": w.slug, "name": w.name, "description": w.description,
            "kind": w.kind, "graph": w.graph}


from pydantic import BaseModel as _BM
class _ImportWorkflow(_BM):
    slug: str | None = None
    name: str
    description: str = ""
    kind: str
    graph: dict


@router.post("/import", response_model=WorkflowOut)
def import_workflow(body: _ImportWorkflow, s: Session = Depends(get_session)):
    base = body.slug or "imported-workflow"
    new_slug = base
    i = 2
    while s.query(Workflow).filter(Workflow.slug == new_slug).first():
        new_slug = f"{base}-imported" if i == 2 else f"{base}-imported-{i}"
        i += 1
    w = Workflow(slug=new_slug, name=body.name, description=body.description,
                 kind=body.kind, graph=body.graph)
    s.add(w); s.commit(); s.refresh(w)
    return w


@router.post("/{slug}/clone", response_model=WorkflowOut)
def clone_workflow(slug: str, s: Session = Depends(get_session)):
    src = s.query(Workflow).filter(Workflow.slug == slug).first()
    if not src:
        raise HTTPException(404, "not found")
    base = f"{slug}-copy"
    new_slug = base
    i = 2
    while s.query(Workflow).filter(Workflow.slug == new_slug).first():
        new_slug = f"{base}-{i}"
        i += 1
    clone = Workflow(
        slug=new_slug,
        name=f"{src.name} (copy)",
        description=src.description,
        kind=src.kind,
        graph=dict(src.graph or {}),
    )
    s.add(clone); s.commit(); s.refresh(clone)
    return clone


@router.post("/{slug}/run")
async def run_workflow_ep(slug: str, body: RunInput, s: Session = Depends(get_session)):
    w = s.query(Workflow).filter(Workflow.slug == slug).first()
    if not w:
        raise HTTPException(404, "not found")
    payload = body.input.get("input") if isinstance(body.input, dict) and "input" in body.input else body.input
    extra = body.input.get("extra", {}) if isinstance(body.input, dict) else {}
    target_id = extra.get("target_id") if isinstance(extra, dict) else None
    target_slug = extra.get("target_slug") if isinstance(extra, dict) else None
    if target_id is None and target_slug:
        from ..models import Target
        t = s.query(Target).filter(Target.slug == target_slug).first()
        if t is not None:
            target_id = t.id
    try:
        rid = start_workflow_run_bg(slug, payload, target_id=target_id)
    except __import__("backend.app.core.executor", fromlist=["TargetBudgetExceeded"]).TargetBudgetExceeded as e:
        raise HTTPException(429, f"target budget exceeded: {e}")
    return {"run_id": rid, "target_id": target_id}
