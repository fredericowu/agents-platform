"""Targets — first-class umbrella linking a tree of runs to an overall delivery goal.

A Target is created at the start of an orchestration (e.g. "deliver US1924311").
Every Run can carry ``runs.target_id`` linking it back. The summary endpoint
rolls up runs/tokens/cost over the whole tree for retros + dashboards.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import get_session, session_scope
from ..models import Run, Target
from ..schemas import AttachPrIn, LinkRunIn, TargetIn, TargetOut, TargetSummary, TargetUpdate

router = APIRouter(prefix="/api/targets", tags=["targets"])


def _walk_descendants(s, parent_id: str) -> list[str]:
    """Return every run id descended from `parent_id` (DFS)."""
    out: list[str] = []
    stack = [parent_id]
    seen = {parent_id}
    while stack:
        pid = stack.pop()
        kids = [r.id for r in s.query(Run).filter(Run.parent_run_id == pid).all()]
        for k in kids:
            if k in seen:
                continue
            seen.add(k)
            out.append(k)
            stack.append(k)
    return out


@router.get("", response_model=list[TargetOut])
def list_targets(include_deleted: bool = Query(False),
                 deleted_only: bool = Query(False),
                 status: str | None = Query(None),
                 q: str | None = Query(None),
                 limit: int = Query(100, ge=1, le=500),
                 s: Session = Depends(get_session)):
    """List Targets. Soft-deleted excluded by default."""
    qry = s.query(Target)
    if deleted_only:
        qry = qry.filter(Target.deleted_at.is_not(None))
    elif not include_deleted:
        qry = qry.filter(Target.deleted_at.is_(None))
    if status:
        qry = qry.filter(Target.status == status)
    if q:
        like = f"%{q.lower()}%"
        from sqlalchemy import func, or_
        qry = qry.filter(or_(
            func.lower(Target.slug).like(like),
            func.lower(Target.name).like(like),
            func.lower(Target.description).like(like),
            func.lower(Target.source_ref).like(like),
        ))
    return qry.order_by(Target.started_at.desc()).limit(limit).all()


@router.get("/{slug}", response_model=TargetOut)
def get_target(slug: str, include_deleted: bool = Query(False),
               s: Session = Depends(get_session)):
    t = s.query(Target).filter(Target.slug == slug).first()
    if not t:
        raise HTTPException(404, "not found")
    if t.deleted_at is not None and not include_deleted:
        raise HTTPException(404, "deleted (use include_deleted=true to view)")
    return t


@router.post("", response_model=TargetOut)
async def create_target(body: TargetIn, s: Session = Depends(get_session)):
    existing = s.query(Target).filter(Target.slug == body.slug).first()
    if existing is not None:
        if existing.deleted_at is not None:
            raise HTTPException(409,
                "slug exists but is soft-deleted — restore it or pick another slug")
        raise HTTPException(409, "slug already exists")
    t = Target(**body.model_dump())
    s.add(t)
    s.commit()
    s.refresh(t)
    # GitHub sync — fire-and-forget
    try:
        from ..core.github_sync import create_target_issue
        from ..core import security as _sec
        _t_slug = t.slug
        _t_name = t.name
        _t_desc = t.description
        _t_tags = list(t.tags or [])

        async def _sync_new_target():
            issue_number = await create_target_issue(
                target_slug=_t_slug,
                target_name=_t_name,
                description=_t_desc,
                tags=_t_tags,
            )
            if issue_number:
                repo = _sec.get_setting("github_repo", "") or ""
                with session_scope() as sync_s:
                    from sqlalchemy import update as _upd
                    sync_s.execute(
                        _upd(Target).where(Target.slug == _t_slug).values(
                            github_issue_number=issue_number,
                            github_issue_url=f"https://github.com/{repo}/issues/{issue_number}",
                        )
                    )

        asyncio.create_task(_sync_new_target())
    except Exception:
        pass
    return t


@router.put("/{slug}", response_model=TargetOut)
async def update_target(slug: str, body: TargetUpdate, s: Session = Depends(get_session)):
    t = s.query(Target).filter(Target.slug == slug).first()
    if not t:
        raise HTTPException(404, "not found")
    if t.deleted_at is not None:
        raise HTTPException(409, "target is soft-deleted — restore it first")
    patch = body.model_dump(exclude_unset=True)
    new_status = patch.get("status")
    # If status flips to a terminal value and ended_at not set, auto-stamp it.
    if new_status in ("completed", "cancelled", "abandoned") and t.ended_at is None:
        patch.setdefault("ended_at", datetime.utcnow())
    for k, v in patch.items():
        setattr(t, k, v)
    s.commit()
    s.refresh(t)
    # GitHub sync — fire-and-forget when status changes
    try:
        _gh_issue_num = getattr(t, "github_issue_number", None)
        if new_status and _gh_issue_num:
            from ..core.github_sync import update_target_issue
            asyncio.create_task(update_target_issue(_gh_issue_num, new_status))
    except Exception:
        pass
    return t


@router.delete("/{slug}")
def delete_target(slug: str, hard: bool = Query(False),
                  s: Session = Depends(get_session)):
    """Soft-delete by default (sets deleted_at). Hard-delete is irreversible
    and ALSO unlinks every run.target_id pointing at this target (sets NULL)."""
    t = s.query(Target).filter(Target.slug == slug).first()
    if not t:
        raise HTTPException(404, "not found")
    if hard:
        # null out FKs first
        s.query(Run).filter(Run.target_id == t.id).update({"target_id": None})
        s.delete(t)
        s.commit()
        return {"deleted": slug, "soft": False, "runs_unlinked": True}
    if t.deleted_at is None:
        t.deleted_at = datetime.utcnow()
        s.commit()
    return {"deleted": slug, "soft": True, "deleted_at": t.deleted_at}


@router.post("/{slug}/restore", response_model=TargetOut)
def restore_target(slug: str, s: Session = Depends(get_session)):
    t = s.query(Target).filter(Target.slug == slug).first()
    if not t:
        raise HTTPException(404, "not found")
    if t.deleted_at is None:
        raise HTTPException(409, "not deleted")
    t.deleted_at = None
    s.commit()
    s.refresh(t)
    return t


@router.get("/{slug}/runs")
def list_target_runs(slug: str, limit: int = Query(500, ge=1, le=2000),
                     s: Session = Depends(get_session)):
    """Every Run linked to this Target, ordered chronologically. Lightweight
    response (no nested events). Use /runs/{id}/tree for full lineage on
    any specific run."""
    t = s.query(Target).filter(Target.slug == slug).first()
    if not t:
        raise HTTPException(404, "not found")
    rows = (s.query(Run)
              .filter(Run.target_id == t.id)
              .order_by(Run.started_at.asc())
              .limit(limit).all())
    out = []
    for r in rows:
        out.append({
            "id": r.id, "kind": r.kind, "target_slug": r.target_slug,
            "status": r.status, "model_slug": r.model_slug,
            "tokens_in": r.tokens_in, "tokens_out": r.tokens_out,
            "cost_usd": r.cost_usd,
            "started_at": r.started_at.isoformat(),
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            "parent_run_id": r.parent_run_id,
            "node_id": r.node_id,
        })
    return {"target": {"id": t.id, "slug": t.slug, "name": t.name},
            "count": len(out), "runs": out}


@router.get("/{slug}/summary", response_model=TargetSummary)
def target_summary(slug: str, s: Session = Depends(get_session)):
    """Rolled-up retro stats over every Run linked to this Target."""
    t = s.query(Target).filter(Target.slug == slug).first()
    if not t:
        raise HTTPException(404, "not found")
    runs = s.query(Run).filter(Run.target_id == t.id).all()

    by_status: dict[str, int] = {}
    by_agent: dict[str, int] = {}
    by_model: dict[str, int] = {}
    tokens_in = 0
    tokens_out = 0
    cost_usd = 0.0
    first_start: datetime | None = None
    last_end: datetime | None = None
    for r in runs:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        by_agent[r.target_slug] = by_agent.get(r.target_slug, 0) + 1
        if r.model_slug:
            by_model[r.model_slug] = by_model.get(r.model_slug, 0) + 1
        tokens_in += r.tokens_in or 0
        tokens_out += r.tokens_out or 0
        cost_usd += r.cost_usd or 0.0
        if r.started_at and (first_start is None or r.started_at < first_start):
            first_start = r.started_at
        if r.ended_at is not None and (last_end is None or r.ended_at > last_end):
            last_end = r.ended_at

    pct_tok = None
    if t.budget_tokens:
        pct_tok = round(((tokens_in + tokens_out) / t.budget_tokens) * 100, 2)
    pct_usd = None
    if t.budget_usd and t.budget_usd > 0:
        pct_usd = round((cost_usd / t.budget_usd) * 100, 2)

    # Wall = span across actual runs when present; otherwise the Target's own span.
    # Target.started_at can be later than its runs (retroactive linkage), so use
    # the earliest run start as the floor.
    start_for_wall = first_start if (first_start and first_start < t.started_at) else t.started_at
    end_for_wall = t.ended_at or last_end
    wall = (end_for_wall - start_for_wall).total_seconds() if end_for_wall else None

    return TargetSummary(
        target_id=t.id, target_slug=t.slug, target_name=t.name,
        status=t.status,
        runs_count=len(runs),
        runs_by_status=by_status,
        tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=round(cost_usd, 6),
        budget_tokens=t.budget_tokens, budget_usd=t.budget_usd,
        pct_of_token_budget=pct_tok, pct_of_usd_budget=pct_usd,
        agents_used=by_agent, models_used=by_model,
        started_at=t.started_at, ended_at=t.ended_at,
        wall_seconds=wall,
    )


# -----------------------------------------------------------------------------
# Run linkage — link/unlink existing runs to a Target.
# Used for retro-backfill, late linkage, or to fix mis-tagged runs.
# -----------------------------------------------------------------------------

@router.post("/{slug}/link_run")
def link_run(slug: str, body: LinkRunIn, s: Session = Depends(get_session)):
    """Link an existing run (and optionally all its descendants) to this Target.
    Idempotent: if the run is already linked, no-op. Returns the count linked."""
    t = s.query(Target).filter(Target.slug == slug, Target.deleted_at.is_(None)).first()
    if not t:
        raise HTTPException(404, "target not found")
    r = s.query(Run).filter(Run.id == body.run_id).first()
    if not r:
        raise HTTPException(404, "run not found")

    ids = [body.run_id]
    if body.include_descendants:
        ids.extend(_walk_descendants(s, body.run_id))
    linked = (s.query(Run)
                .filter(Run.id.in_(ids))
                .update({"target_id": t.id}, synchronize_session=False))
    s.commit()
    return {"target_id": t.id, "target_slug": t.slug, "run_ids": ids, "linked": linked}


@router.delete("/{slug}/link_run/{run_id}")
def unlink_run(slug: str, run_id: str, include_descendants: bool = False,
               s: Session = Depends(get_session)):
    """Unlink a run (set target_id NULL). If include_descendants=true, also
    unlinks every descendant."""
    t = s.query(Target).filter(Target.slug == slug).first()
    if not t:
        raise HTTPException(404, "target not found")
    ids = [run_id]
    if include_descendants:
        ids.extend(_walk_descendants(s, run_id))
    unlinked = (s.query(Run)
                  .filter(Run.id.in_(ids), Run.target_id == t.id)
                  .update({"target_id": None}, synchronize_session=False))
    s.commit()
    return {"target_id": t.id, "run_ids": ids, "unlinked": unlinked}


# -----------------------------------------------------------------------------
# PR attachment — first-class linkage so the retro view shows the delivery PRs.
# -----------------------------------------------------------------------------

@router.post("/{slug}/pr", response_model=TargetOut)
def attach_pr(slug: str, body: AttachPrIn, s: Session = Depends(get_session)):
    """Attach a PR (URL + optional metadata) to this Target. Idempotent on URL —
    if a row with the same url already exists, it's updated in place."""
    t = s.query(Target).filter(Target.slug == slug, Target.deleted_at.is_(None)).first()
    if not t:
        raise HTTPException(404, "target not found")
    prs = list(t.pr_urls or [])
    entry = {k: v for k, v in body.model_dump().items() if v is not None}
    found = False
    for i, ex in enumerate(prs):
        if isinstance(ex, dict) and ex.get("url") == body.url:
            prs[i] = {**ex, **entry}
            found = True
            break
    if not found:
        prs.append(entry)
    t.pr_urls = prs
    s.commit(); s.refresh(t)
    return t


@router.delete("/{slug}/pr", response_model=TargetOut)
def detach_pr(slug: str, url: str, s: Session = Depends(get_session)):
    """Remove a PR from this Target's linkage (by URL match)."""
    t = s.query(Target).filter(Target.slug == slug).first()
    if not t:
        raise HTTPException(404, "target not found")
    prs = [p for p in (t.pr_urls or []) if not (isinstance(p, dict) and p.get("url") == url)]
    t.pr_urls = prs
    s.commit(); s.refresh(t)
    return t
