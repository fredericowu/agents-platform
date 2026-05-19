"""Target Lessons — the platform's growing knowledge store of what worked,
what wasted time, and what to do differently next time.

Two access paths:
  /api/targets/{slug}/lessons   — scoped CRUD on one Target's lessons
  /api/lessons/search           — cross-target search by tag/category/text

The retro agent uses both: per-target to read its own context, cross-target
to dedupe against existing lessons before writing new ones.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..core.rag import get_rag_provider, render_lesson_markdown, slugify_for_kb
from ..db import get_session
from ..models import LessonApplication, LessonEvidenceRun, Run, Target, TargetLesson
from ..schemas import (LessonApplicationIn, LessonApplicationOut, LessonEffectivenessOut,
                       LessonForecastIn, LessonForecastOut, LessonIn, LessonOut,
                       LessonSearchHit, LessonsMetrics, LessonUpdate)


def _sync_lesson_to_rag(s: Session, lesson: TargetLesson) -> dict | None:
    """Push a lesson into the configured RAG provider. Best-effort —
    table is source-of-truth; if the RAG write fails, we log and continue."""
    try:
        target = s.query(Target).filter(Target.id == lesson.target_id).first()
        target_dict = {"name": target.name, "slug": target.slug} if target else None
        slug = slugify_for_kb(lesson.title)
        path = f"{slug}.md"
        content = render_lesson_markdown({
            "id": lesson.id, "title": lesson.title, "category": lesson.category,
            "content": lesson.content, "confidence": lesson.confidence,
            "applicable_tags": lesson.applicable_tags or [],
            "evidence_run_ids": lesson.evidence_run_ids or [],
            "target_id": lesson.target_id,
        }, target_dict)
        provider = get_rag_provider()
        return provider.upsert(path, content)
    except Exception as e:
        return {"error": f"rag sync failed: {e}"}


def _delete_lesson_from_rag(lesson: TargetLesson) -> dict | None:
    try:
        slug = slugify_for_kb(lesson.title)
        path = f"{slug}.md"
        provider = get_rag_provider()
        return provider.delete(path)
    except Exception as e:
        return {"error": f"rag delete failed: {e}"}

def _sync_evidence_runs(s: Session, lesson: TargetLesson, new_run_ids: list[str]) -> None:
    """Diff the 'primary' join rows against new_run_ids — add missing, remove dropped.
    Does not touch rows with other roles (consolidated_from, evidence)."""
    existing = (s.query(LessonEvidenceRun)
                  .filter(LessonEvidenceRun.lesson_id == lesson.id,
                          LessonEvidenceRun.role == "primary")
                  .all())
    existing_by_run = {er.run_id: er for er in existing}
    new_set = set(new_run_ids)

    for run_id, er in existing_by_run.items():
        if run_id not in new_set:
            s.delete(er)

    for run_id in new_run_ids:
        if run_id not in existing_by_run:
            s.add(LessonEvidenceRun(lesson_id=lesson.id, run_id=run_id, role="primary"))


# Cross-target search lives at /api/lessons (not nested under a target).
search_router = APIRouter(prefix="/api/lessons", tags=["lessons"])

# Per-target CRUD lives at /api/targets/{slug}/lessons.
target_lessons_router = APIRouter(prefix="/api/targets/{slug}/lessons", tags=["lessons"])


# -----------------------------------------------------------------------------
# Cross-target search
# -----------------------------------------------------------------------------

@search_router.get("/search", response_model=list[LessonSearchHit])
def search_lessons(tags: str | None = Query(None,
                       description="Comma-separated tag list. Returns lessons whose applicable_tags overlap any of these."),
                   category: str | None = Query(None,
                       description="Filter by category (time-saver|pitfall|tooling-gap|...)."),
                   q: str | None = Query(None,
                       description="Full-text query. When set, hits from the configured RAG provider "
                                   "(semantic) are merged with the SQL filter results."),
                   include_superseded: bool = Query(False),
                   include_pending: bool = Query(False,
                       description="When False (default), exclude pending_review lessons. "
                                   "When True, return active+pending_review but exclude archived."),
                   limit: int = Query(20, ge=1, le=200),
                   use_rag: bool = Query(True,
                       description="If false, force SQL-only search even when `q` is set."),
                   s: Session = Depends(get_session)):
    """Search lessons across ALL Targets — SQL (tag/category) + RAG (semantic) merged.

    The retro agent calls this BEFORE creating a new lesson, to find existing
    ones to update instead. The conductor calls it at Phase 1.4 to surface
    prior lessons before dispatching project-manager.

    When ``q`` is set and a RAG provider is configured (see settings.rag_provider),
    semantic vector hits from the RAG are merged in with the SQL filter results,
    deduplicated by lesson_id (extracted from the markdown frontmatter)."""
    qry = (s.query(TargetLesson, Target)
            .join(Target, TargetLesson.target_id == Target.id)
            .filter(TargetLesson.deleted_at.is_(None)))
    if not include_superseded:
        qry = qry.filter(TargetLesson.superseded_by.is_(None))
    if not include_pending:
        qry = qry.filter(TargetLesson.status != 'pending_review')
    else:
        qry = qry.filter(TargetLesson.status != 'archived')
    if category:
        qry = qry.filter(TargetLesson.category == category)
    if q:
        like = f"%{q.lower()}%"
        from sqlalchemy import func, or_
        qry = qry.filter(or_(
            func.lower(TargetLesson.title).like(like),
            func.lower(TargetLesson.content).like(like),
        ))
    rows = qry.order_by(TargetLesson.updated_at.desc()).limit(limit * 4).all()

    wanted = {t.strip() for t in (tags or "").split(",") if t.strip()}
    seen_ids: set[str] = set()
    hits: list[LessonSearchHit] = []
    for lesson, target in rows:
        if wanted:
            tagset = set(lesson.applicable_tags or [])
            if not (wanted & tagset):
                continue
        hits.append(LessonSearchHit(
            lesson=LessonOut.model_validate(lesson),
            target_slug=target.slug,
            target_name=target.name,
            target_status=target.status,
        ))
        seen_ids.add(lesson.id)
        if len(hits) >= limit:
            break

    # ----- RAG semantic merge -----
    # When `q` is set and RAG is enabled, query the configured provider and
    # merge any lesson_id matches we haven't already returned via SQL.
    if q and use_rag and len(hits) < limit:
        try:
            provider = get_rag_provider()
            if provider.kind != "disabled":
                rag = provider.search(q, n_results=limit)
                for r in rag.get("results") or []:
                    # Each RAG result is expected to expose a `content` blob;
                    # we extract the lesson_id from the markdown frontmatter
                    # ("Lesson ID: `<id>`"). If not found, skip.
                    content = r.get("content") or ""
                    if not content:
                        continue
                    lesson_id = _extract_lesson_id(content)
                    if not lesson_id or lesson_id in seen_ids:
                        continue
                    lesson_row = (s.query(TargetLesson, Target)
                                    .join(Target, TargetLesson.target_id == Target.id)
                                    .filter(TargetLesson.id == lesson_id,
                                            TargetLesson.deleted_at.is_(None))
                                    .first())
                    if not lesson_row:
                        continue
                    lesson, target = lesson_row
                    if wanted:
                        tagset = set(lesson.applicable_tags or [])
                        if not (wanted & tagset):
                            continue
                    if category and lesson.category != category:
                        continue
                    hits.append(LessonSearchHit(
                        lesson=LessonOut.model_validate(lesson),
                        target_slug=target.slug,
                        target_name=target.name,
                        target_status=target.status,
                    ))
                    seen_ids.add(lesson.id)
                    if len(hits) >= limit:
                        break
        except Exception:
            # RAG failure must NOT break SQL search — log and continue
            pass

    return hits


def _extract_lesson_id(markdown: str) -> str | None:
    """Pull the lesson_id from a rendered KB markdown blob.

    The render_lesson_markdown helper emits a line like:
      ``> **Lesson ID:** `<uuid>``
    We grep for it. Returns None if not found.
    """
    import re
    m = re.search(r"\*\*Lesson ID:\*\*\s*`([a-f0-9]{20,})`", markdown)
    return m.group(1) if m else None


# -----------------------------------------------------------------------------
# Per-target CRUD
# -----------------------------------------------------------------------------

def _target_or_404(s: Session, slug: str) -> Target:
    t = s.query(Target).filter(Target.slug == slug).first()
    if not t:
        raise HTTPException(404, "target not found")
    return t


@target_lessons_router.get("", response_model=list[LessonOut])
def list_target_lessons(slug: str, include_superseded: bool = Query(False),
                        s: Session = Depends(get_session)):
    t = _target_or_404(s, slug)
    qry = (s.query(TargetLesson)
            .filter(TargetLesson.target_id == t.id,
                    TargetLesson.deleted_at.is_(None)))
    if not include_superseded:
        qry = qry.filter(TargetLesson.superseded_by.is_(None))
    return qry.order_by(TargetLesson.created_at.asc()).all()


@target_lessons_router.post("", response_model=LessonOut)
def create_target_lesson(slug: str, body: LessonIn,
                         s: Session = Depends(get_session)):
    t = _target_or_404(s, slug)
    lesson = TargetLesson(target_id=t.id, **body.model_dump())
    s.add(lesson)
    s.flush()  # get the id before writing join rows
    _sync_evidence_runs(s, lesson, body.evidence_run_ids or [])
    s.commit()
    s.refresh(lesson)
    # Dual-write to the configured RAG (best-effort — SQL is source of truth)
    _sync_lesson_to_rag(s, lesson)
    return lesson


@target_lessons_router.put("/{lesson_id}", response_model=LessonOut)
def update_target_lesson(slug: str, lesson_id: str, body: LessonUpdate,
                         s: Session = Depends(get_session)):
    t = _target_or_404(s, slug)
    lesson = (s.query(TargetLesson)
                .filter(TargetLesson.id == lesson_id, TargetLesson.target_id == t.id)
                .first())
    if not lesson:
        raise HTTPException(404, "lesson not found on this target")
    update_data = body.model_dump(exclude_unset=True)
    for k, v in update_data.items():
        setattr(lesson, k, v)
    if "evidence_run_ids" in update_data:
        _sync_evidence_runs(s, lesson, update_data["evidence_run_ids"] or [])
    s.commit()
    s.refresh(lesson)
    # Re-sync to RAG (overwrites the markdown with current content)
    _sync_lesson_to_rag(s, lesson)
    return lesson


@target_lessons_router.delete("/{lesson_id}")
def delete_target_lesson(slug: str, lesson_id: str, hard: bool = Query(False),
                         s: Session = Depends(get_session)):
    t = _target_or_404(s, slug)
    lesson = (s.query(TargetLesson)
                .filter(TargetLesson.id == lesson_id, TargetLesson.target_id == t.id)
                .first())
    if not lesson:
        raise HTTPException(404, "lesson not found on this target")
    if hard:
        _delete_lesson_from_rag(lesson)
        s.delete(lesson)
        s.commit()
        return {"deleted": lesson_id, "soft": False}
    if lesson.deleted_at is None:
        lesson.deleted_at = datetime.utcnow()
        # Soft-delete also removes from RAG so it's not returned by searches
        _delete_lesson_from_rag(lesson)
        s.commit()
    return {"deleted": lesson_id, "soft": True, "deleted_at": lesson.deleted_at}


# -----------------------------------------------------------------------------
# RAG provider — health check + manual resync
# -----------------------------------------------------------------------------

@search_router.get("/rag/health")
def rag_health():
    """Probe the configured RAG provider. Returns connectivity status,
    auth-header state, and the backend kind. Use this from the Settings UI
    after editing the rag_provider config."""
    return get_rag_provider().health()


@search_router.post("/rag/resync")
def rag_resync(s: Session = Depends(get_session)):
    """One-shot full re-sync of every non-deleted lesson into the configured RAG.
    Useful after switching providers (e.g. moving from aw-kb to a different
    vector store) or after a bulk import."""
    lessons = (s.query(TargetLesson)
                 .filter(TargetLesson.deleted_at.is_(None))
                 .all())
    results: dict[str, int] = {"synced": 0, "failed": 0}
    errors: list[dict] = []
    for lesson in lessons:
        out = _sync_lesson_to_rag(s, lesson)
        if out and out.get("error"):
            results["failed"] += 1
            errors.append({"lesson_id": lesson.id, "title": lesson.title, "error": out["error"]})
        else:
            results["synced"] += 1
    return {**results, "errors": errors[:10]}  # cap error list


@search_router.get("/rag/config")
def rag_config():
    """Return the currently-effective RAG provider config (read-only).
    Useful for the Settings UI to show what's active without re-deriving."""
    p = get_rag_provider()
    return {"kind": p.kind, "config": p.config}


# -----------------------------------------------------------------------------
# Lesson → Runs linkage
# -----------------------------------------------------------------------------

@search_router.get("/{lesson_id}/runs")
def get_lesson_runs(lesson_id: str,
                    limit: int = Query(50, ge=1, le=200),
                    s: Session = Depends(get_session)):
    """Return all runs linked to a lesson via lesson_evidence_runs, ordered by
    join-row created_at DESC. Used by the Lessons UI detail panel."""
    lesson = s.query(TargetLesson).filter(TargetLesson.id == lesson_id).first()
    if not lesson:
        raise HTTPException(404, "lesson not found")
    rows = (s.query(LessonEvidenceRun, Run)
              .join(Run, LessonEvidenceRun.run_id == Run.id)
              .filter(LessonEvidenceRun.lesson_id == lesson_id)
              .order_by(LessonEvidenceRun.created_at.desc())
              .limit(limit)
              .all())
    return [
        {
            "run_id": run.id,
            "role": er.role,
            "status": run.status,
            "kind": run.kind,
            "target_slug": run.target_slug,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "ended_at": run.ended_at.isoformat() if run.ended_at else None,
            "cost_usd": run.cost_usd,
            "tokens_in": run.tokens_in,
            "tokens_out": run.tokens_out,
            "linked_at": er.created_at.isoformat() if er.created_at else None,
        }
        for er, run in rows
    ]


# -----------------------------------------------------------------------------
# Lesson Applications — the tracking layer that makes "continuous improvement"
# measurable. Every lesson surfaced/applied/ignored gets recorded here.
# -----------------------------------------------------------------------------

@search_router.post("/applications", response_model=LessonApplicationOut)
def record_lesson_application(body: LessonApplicationIn,
                              s: Session = Depends(get_session)):
    """Record a single lesson↔target↔run application. Called by the conductor
    after Phase 1.5 (with outcome='shown_to_pm') and by the retro agent after
    walking the run history (with outcome='applied' | 'prevented' | 'ignored' | 'partial')."""
    lesson = s.query(TargetLesson).filter(TargetLesson.id == body.lesson_id).first()
    if not lesson:
        raise HTTPException(404, f"lesson {body.lesson_id} not found")
    target_id = body.target_id or lesson.target_id
    target = s.query(Target).filter(Target.id == target_id).first()
    if not target:
        raise HTTPException(404, f"target {target_id} not found")
    app = LessonApplication(
        lesson_id=body.lesson_id,
        target_id=target_id,
        applied_in_run_id=body.applied_in_run_id,
        outcome=body.outcome,
        notes=body.notes,
    )
    s.add(app)
    s.commit()
    s.refresh(app)
    return app


@search_router.get("/applications", response_model=list[LessonApplicationOut])
def list_lesson_applications(lesson_id: str | None = Query(None),
                             target_id: str | None = Query(None),
                             outcome: str | None = Query(None),
                             limit: int = Query(200, ge=1, le=2000),
                             s: Session = Depends(get_session)):
    qry = s.query(LessonApplication)
    if lesson_id:
        qry = qry.filter(LessonApplication.lesson_id == lesson_id)
    if target_id:
        qry = qry.filter(LessonApplication.target_id == target_id)
    if outcome:
        qry = qry.filter(LessonApplication.outcome == outcome)
    return qry.order_by(LessonApplication.created_at.desc()).limit(limit).all()


@search_router.get("/{lesson_id}/effectiveness", response_model=LessonEffectivenessOut)
def lesson_effectiveness(lesson_id: str, s: Session = Depends(get_session)):
    """Per-lesson stats — effectiveness rate, propagation gap rate."""
    lesson = s.query(TargetLesson).filter(TargetLesson.id == lesson_id).first()
    if not lesson:
        raise HTTPException(404, "lesson not found")
    apps = s.query(LessonApplication).filter(LessonApplication.lesson_id == lesson_id).all()
    by_outcome: dict[str, int] = {}
    for a in apps:
        by_outcome[a.outcome] = by_outcome.get(a.outcome, 0) + 1
    total = len(apps)
    helpful = by_outcome.get("applied", 0) + by_outcome.get("prevented", 0)
    effectiveness = (helpful / total) if total else None
    shown = by_outcome.get("shown_to_pm", 0) + by_outcome.get("applied", 0) + by_outcome.get("prevented", 0) + by_outcome.get("ignored", 0)
    ignored = by_outcome.get("ignored", 0)
    gap_rate = (ignored / shown) if shown else None
    return LessonEffectivenessOut(
        lesson_id=lesson.id,
        title=lesson.title,
        confidence=lesson.confidence,
        total_applications=total,
        by_outcome=by_outcome,
        effectiveness_rate=effectiveness,
        propagation_gap_rate=gap_rate,
    )


# -----------------------------------------------------------------------------
# Forecast — given a planned task's tags + category, predict cost/wall + advise
# -----------------------------------------------------------------------------

@search_router.post("/forecast", response_model=LessonForecastOut)
def lesson_forecast(body: LessonForecastIn,
                    include_pending: bool = Query(False,
                        description="When False (default), exclude pending_review lessons. "
                                    "When True, return active+pending_review but exclude archived."),
                    s: Session = Depends(get_session)):
    """Pre-flight forecast for an upcoming task. Returns:
      - matched_lessons: top-relevant lessons to apply (via tag overlap)
      - similar_targets: prior Targets that share tags — gives the cost/wall distribution
      - predicted_cost_usd / predicted_wall_seconds: p10/p50/p90 from similar Targets
      - advisories: high-confidence pitfalls to avoid, plus "first delivery" flag if no priors
    """
    # 1. Match lessons by tag overlap
    qry = (s.query(TargetLesson)
            .filter(TargetLesson.deleted_at.is_(None),
                    TargetLesson.superseded_by.is_(None)))
    if not include_pending:
        qry = qry.filter(TargetLesson.status != 'pending_review')
    else:
        qry = qry.filter(TargetLesson.status != 'archived')
    if body.category:
        # Don't strictly filter by category — match by tag if category appears as a tag
        pass
    all_lessons = qry.all()
    wanted = set(body.tags or [])
    if body.category:
        wanted.add(body.category)
    matched: list[tuple[TargetLesson, int]] = []
    for lesson in all_lessons:
        overlap = len(wanted & set(lesson.applicable_tags or []))
        if overlap:
            matched.append((lesson, overlap))
    matched.sort(key=lambda p: (
        -p[1],                       # tag overlap desc
        {"high": 0, "medium": 1, "low": 2}.get(p[0].confidence, 3),
    ))
    hits: list[LessonSearchHit] = []
    for lesson, _ovl in matched[:20]:
        target = s.query(Target).filter(Target.id == lesson.target_id).first()
        if not target:
            continue
        hits.append(LessonSearchHit(
            lesson=LessonOut.model_validate(lesson),
            target_slug=target.slug,
            target_name=target.name,
            target_status=target.status,
        ))

    # 2. Find similar Targets — share at least one tag
    similar: list[dict] = []
    cost_samples: list[float] = []
    wall_samples: list[float] = []
    for t in s.query(Target).filter(Target.deleted_at.is_(None)).all():
        ttags = set(t.tags or [])
        if not (wanted & ttags):
            continue
        # Compute leaf cost from runs (sum of leaves only, not parents — fixes double-count)
        leaves = (s.query(Run)
                    .filter(Run.target_id == t.id)
                    .all())
        leaf_ids = {r.id for r in leaves}
        parent_ids = {r.parent_run_id for r in leaves if r.parent_run_id in leaf_ids}
        cost_total = sum((r.cost_usd or 0.0) for r in leaves if r.id not in parent_ids)
        # wall = last_end - first_start
        starts = [r.started_at for r in leaves if r.started_at]
        ends = [r.ended_at for r in leaves if r.ended_at]
        wall = (max(ends) - min(starts)).total_seconds() if (starts and ends) else None
        if cost_total > 0:
            cost_samples.append(cost_total)
        if wall:
            wall_samples.append(wall)
        similar.append({
            "target_slug": t.slug,
            "target_name": t.name,
            "status": t.status,
            "cost_usd": round(cost_total, 4),
            "wall_seconds": wall,
        })

    # 3. p10/p50/p90 from samples
    def pct(arr: list[float], p: float) -> float | None:
        if not arr:
            return None
        arr = sorted(arr)
        i = max(0, min(len(arr) - 1, int(round(p * (len(arr) - 1)))))
        return round(arr[i], 2)

    predicted_cost = None
    predicted_wall = None
    if cost_samples:
        predicted_cost = {"p10": pct(cost_samples, 0.1), "p50": pct(cost_samples, 0.5), "p90": pct(cost_samples, 0.9)}
    if wall_samples:
        predicted_wall = {"p10": pct(wall_samples, 0.1), "p50": pct(wall_samples, 0.5), "p90": pct(wall_samples, 0.9)}

    # 4. Advisories
    advisories: list[str] = []
    if not similar:
        advisories.append(f"First delivery with tags {sorted(wanted)} — no priors to forecast from. Expect higher variance.")
    elif len(similar) < 3:
        advisories.append(f"Only {len(similar)} prior Targets share these tags — forecast is low-confidence.")
    # Surface top high-confidence pitfalls
    pitfalls = [h for h in hits if h.lesson.category in ("pitfall", "cost-trap") and h.lesson.confidence == "high"]
    for h in pitfalls[:3]:
        advisories.append(f"HIGH-CONFIDENCE pitfall: {h.lesson.title} (lesson_id={h.lesson.id})")
    patterns = [h for h in hits if h.lesson.category == "pattern-that-worked" and h.lesson.confidence in ("high", "medium")]
    for h in patterns[:3]:
        advisories.append(f"PROVEN PATTERN: {h.lesson.title} (lesson_id={h.lesson.id})")

    return LessonForecastOut(
        matched_lessons=hits,
        similar_targets=similar,
        predicted_cost_usd=predicted_cost,
        predicted_wall_seconds=predicted_wall,
        advisories=advisories,
    )


# -----------------------------------------------------------------------------
# Metrics — the "are we improving?" dashboard data
# -----------------------------------------------------------------------------

@search_router.get("/metrics", response_model=LessonsMetrics)
def lessons_metrics(s: Session = Depends(get_session)):
    """Cross-target continuous-improvement metrics."""
    all_lessons = (s.query(TargetLesson)
                    .filter(TargetLesson.deleted_at.is_(None),
                            TargetLesson.superseded_by.is_(None)).all())
    lessons_by_cat: dict[str, int] = {}
    lessons_by_conf: dict[str, int] = {}
    for l in all_lessons:
        lessons_by_cat[l.category] = lessons_by_cat.get(l.category, 0) + 1
        lessons_by_conf[l.confidence] = lessons_by_conf.get(l.confidence, 0) + 1

    apps = s.query(LessonApplication).all()
    apps_by_outcome: dict[str, int] = {}
    for a in apps:
        apps_by_outcome[a.outcome] = apps_by_outcome.get(a.outcome, 0) + 1

    # Effectiveness across all lessons
    eff_rates: list[float] = []
    gap_rates: list[float] = []
    for l in all_lessons:
        lapps = [a for a in apps if a.lesson_id == l.id]
        if not lapps:
            continue
        by_o: dict[str, int] = {}
        for a in lapps:
            by_o[a.outcome] = by_o.get(a.outcome, 0) + 1
        helpful = by_o.get("applied", 0) + by_o.get("prevented", 0)
        total = len(lapps)
        eff_rates.append(helpful / total)
        shown = by_o.get("shown_to_pm", 0) + by_o.get("applied", 0) + by_o.get("prevented", 0) + by_o.get("ignored", 0)
        if shown:
            gap_rates.append(by_o.get("ignored", 0) / shown)

    avg_eff = round(sum(eff_rates) / len(eff_rates), 3) if eff_rates else None
    avg_gap = round(sum(gap_rates) / len(gap_rates), 3) if gap_rates else None

    targets = s.query(Target).filter(Target.deleted_at.is_(None)).all()
    targets_by_status: dict[str, int] = {}
    cost_trend = []
    wall_trend = []
    for t in sorted(targets, key=lambda x: x.started_at):
        targets_by_status[t.status] = targets_by_status.get(t.status, 0) + 1
        if t.status not in ("completed", "active"):
            continue
        # leaf cost
        leaves = s.query(Run).filter(Run.target_id == t.id).all()
        leaf_ids = {r.id for r in leaves}
        parent_ids = {r.parent_run_id for r in leaves if r.parent_run_id in leaf_ids}
        cost_total = sum((r.cost_usd or 0.0) for r in leaves if r.id not in parent_ids)
        starts = [r.started_at for r in leaves if r.started_at]
        ends = [r.ended_at for r in leaves if r.ended_at]
        wall = (max(ends) - min(starts)).total_seconds() if (starts and ends) else 0
        cost_trend.append({"target_slug": t.slug, "cost": round(cost_total, 4),
                           "ended_at": (t.ended_at or (max(ends) if ends else t.started_at)).isoformat()})
        wall_trend.append({"target_slug": t.slug, "wall_seconds": round(wall, 1),
                           "ended_at": (t.ended_at or (max(ends) if ends else t.started_at)).isoformat()})

    # Top applied / top ignored
    apps_per_lesson: dict[str, dict[str, int]] = {}
    for a in apps:
        d = apps_per_lesson.setdefault(a.lesson_id, {})
        d[a.outcome] = d.get(a.outcome, 0) + 1
    top_applied = []
    top_ignored = []
    for lesson in all_lessons:
        d = apps_per_lesson.get(lesson.id, {})
        applied = d.get("applied", 0) + d.get("prevented", 0)
        ignored = d.get("ignored", 0)
        if applied:
            top_applied.append({"lesson_id": lesson.id, "title": lesson.title,
                                "category": lesson.category, "applied_count": applied})
        if ignored:
            top_ignored.append({"lesson_id": lesson.id, "title": lesson.title,
                                "category": lesson.category, "ignored_count": ignored})
    top_applied.sort(key=lambda x: -int(x["applied_count"]))
    top_ignored.sort(key=lambda x: -int(x["ignored_count"]))

    return LessonsMetrics(
        total_lessons=len(all_lessons),
        lessons_by_category=lessons_by_cat,
        lessons_by_confidence=lessons_by_conf,
        total_applications=len(apps),
        applications_by_outcome=apps_by_outcome,
        avg_effectiveness_rate=avg_eff,
        avg_propagation_gap_rate=avg_gap,
        total_targets=len(targets),
        completed_targets=targets_by_status.get("completed", 0),
        targets_by_status=targets_by_status,
        cost_trend=cost_trend,
        wall_trend=wall_trend,
        top_applied_lessons=top_applied[:10],
        top_ignored_lessons=top_ignored[:10],
    )


# -----------------------------------------------------------------------------
# D2 frontend endpoints — root listing, single fetch, restore, per-lesson apps
# -----------------------------------------------------------------------------

@search_router.get("/", response_model=list[LessonOut])
def list_lessons(
        q: str | None = Query(None),
        tags: str | None = Query(None),
        category: str | None = Query(None),
        status: str | None = Query(None),
        include_pending: bool = Query(False),
        include_superseded: bool = Query(False),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        s: Session = Depends(get_session)):
    """SQL-only listing for the UI table. Same filters as /search but returns LessonOut[]."""
    qry = s.query(TargetLesson).filter(TargetLesson.deleted_at.is_(None))
    if not include_superseded:
        qry = qry.filter(TargetLesson.superseded_by.is_(None))
    if status:
        qry = qry.filter(TargetLesson.status == status)
    elif not include_pending:
        qry = qry.filter(TargetLesson.status != 'pending_review')
    else:
        qry = qry.filter(TargetLesson.status != 'archived')
    if category:
        qry = qry.filter(TargetLesson.category == category)
    if q:
        from sqlalchemy import func, or_
        like = f"%{q.lower()}%"
        qry = qry.filter(or_(
            func.lower(TargetLesson.title).like(like),
            func.lower(TargetLesson.content).like(like),
        ))
    wanted = {t.strip() for t in (tags or "").split(",") if t.strip()}
    if wanted:
        rows = qry.order_by(TargetLesson.updated_at.desc()).all()
        matched = [r for r in rows if wanted & set(r.applicable_tags or [])]
        return matched[offset:offset + limit]
    return qry.order_by(TargetLesson.updated_at.desc()).offset(offset).limit(limit).all()


@search_router.post("/{lesson_id}/restore", response_model=LessonOut)
def restore_lesson(lesson_id: str, s: Session = Depends(get_session)):
    """Flip archived → active. Rejects (409) if superseded_by is set."""
    lesson = (s.query(TargetLesson)
               .filter(TargetLesson.id == lesson_id,
                       TargetLesson.deleted_at.is_(None))
               .first())
    if not lesson:
        raise HTTPException(404, "lesson not found")
    if lesson.superseded_by is not None:
        raise HTTPException(409, "lesson is superseded — un-supersede the consolidator first")
    if lesson.status != "archived":
        raise HTTPException(409, f"cannot restore a lesson with status '{lesson.status}' (only 'archived')")
    lesson.status = "active"
    s.commit()
    s.refresh(lesson)
    return lesson


@search_router.get("/{lesson_id}/applications", response_model=list[LessonApplicationOut])
def get_lesson_applications(lesson_id: str,
                            limit: int = Query(50, ge=1, le=200),
                            s: Session = Depends(get_session)):
    """Applications for a single lesson, capped at 50, ordered created_at DESC."""
    lesson = s.query(TargetLesson).filter(TargetLesson.id == lesson_id).first()
    if not lesson:
        raise HTTPException(404, "lesson not found")
    return (s.query(LessonApplication)
              .filter(LessonApplication.lesson_id == lesson_id)
              .order_by(LessonApplication.created_at.desc())
              .limit(limit)
              .all())


@search_router.get("/{lesson_id}", response_model=LessonOut)
def get_lesson(lesson_id: str, s: Session = Depends(get_session)):
    """Single lesson by ID. 404 if not found or soft-deleted."""
    lesson = (s.query(TargetLesson)
               .filter(TargetLesson.id == lesson_id,
                       TargetLesson.deleted_at.is_(None))
               .first())
    if not lesson:
        raise HTTPException(404, "lesson not found")
    return lesson
