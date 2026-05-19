"""Wave-6 Chunk A3 — retro-score REST endpoints.

  GET  /api/runs/{run_id}/retro-scores
  PATCH /api/runs/{run_id}/retro-scores/{dimension}
  POST  /api/runs/{run_id}/retro-scores/recompute
  GET  /api/retro-score-weights
  PUT  /api/retro-score-weights
  GET  /api/lessons/pending
  POST /api/lessons/{lesson_id}/approve
  POST /api/lessons/{lesson_id}/archive
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import RetroScore, RetroScoreWeights, Run, TargetLesson
from ..schemas import LessonOut

router = APIRouter(tags=["retro-scores"])

_VALID_DIMS = frozenset({
    "accuracy", "output_quality", "lessons_applied", "recovery",
    "plan_adherence", "cost", "wall", "mistakes", "scope_discipline",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _recompute_summary(s: Session, run: Run) -> None:
    """Recompute Run.retro_score_summary and supersede the old 'overall' row."""
    from ..core.retro_scorer import _weighted_mean

    active = (s.query(RetroScore)
               .filter(RetroScore.run_id == run.id,
                       RetroScore.dimension != "overall",
                       RetroScore.superseded_by.is_(None))
               .all())
    if not active:
        return

    scores_map = {
        rs.dimension: (rs.score, rs.rationale or "", rs.evidence_json or {})
        for rs in active
    }

    weights_row = s.get(RetroScoreWeights, 1)
    weights = weights_row.weights_json if weights_row else {}
    overall = _weighted_mean(scores_map, weights)

    old_overall = (s.query(RetroScore)
                   .filter(RetroScore.run_id == run.id,
                           RetroScore.dimension == "overall",
                           RetroScore.superseded_by.is_(None))
                   .order_by(RetroScore.created_at.desc())
                   .first())
    new_overall = RetroScore(run_id=run.id, dimension="overall", score=overall, source="auto")
    s.add(new_overall)
    s.flush()
    if old_overall:
        old_overall.superseded_by = new_overall.id

    run.retro_score_summary = {
        "overall": overall,
        "dims": {rs.dimension: rs.score for rs in active},
        "computed_at": datetime.utcnow().isoformat(),
        "n_scores": len(active) + 1,
    }


# ---------------------------------------------------------------------------
# Pydantic shapes
# ---------------------------------------------------------------------------

class RetroScoreOverrideIn(BaseModel):
    score: int
    rationale: str | None = None
    evidence_json: dict[str, Any] | None = None


class RetroWeightsIn(BaseModel):
    weights: dict[str, float]


# ---------------------------------------------------------------------------
# GET /api/runs/{run_id}/retro-scores
# ---------------------------------------------------------------------------

@router.get("/api/runs/{run_id}/retro-scores")
def get_retro_scores(run_id: str,
                     include_superseded: bool = Query(False),
                     s: Session = Depends(get_session)):
    run = s.get(Run, run_id)
    if not run:
        raise HTTPException(404, "run not found")

    qry = s.query(RetroScore).filter(RetroScore.run_id == run_id)
    if not include_superseded:
        qry = qry.filter(RetroScore.superseded_by.is_(None))
    scores = qry.order_by(RetroScore.dimension.asc()).all()

    return {
        "run_id": run_id,
        "summary": run.retro_score_summary,
        "scores": [
            {
                "dimension": rs.dimension,
                "score": rs.score,
                "source": rs.source,
                "rationale": rs.rationale,
                "evidence_json": rs.evidence_json,
                "created_at": rs.created_at,
                "superseded_by": rs.superseded_by,
            }
            for rs in scores
        ],
    }


# ---------------------------------------------------------------------------
# PATCH /api/runs/{run_id}/retro-scores/{dimension}
# ---------------------------------------------------------------------------

@router.patch("/api/runs/{run_id}/retro-scores/{dimension}")
def override_retro_score(run_id: str, dimension: str,
                         body: RetroScoreOverrideIn,
                         s: Session = Depends(get_session)):
    if not (1 <= body.score <= 10):
        raise HTTPException(400, "score must be between 1 and 10")

    run = s.get(Run, run_id)
    if not run:
        raise HTTPException(404, "run not found")

    old_row = (s.query(RetroScore)
               .filter(RetroScore.run_id == run_id,
                       RetroScore.dimension == dimension,
                       RetroScore.superseded_by.is_(None))
               .order_by(RetroScore.created_at.desc())
               .first())

    new_row = RetroScore(
        run_id=run_id,
        dimension=dimension,
        score=body.score,
        source="human",
        rationale=body.rationale,
        evidence_json=body.evidence_json,
    )
    s.add(new_row)
    s.flush()  # populate new_row.id

    if old_row:
        old_row.superseded_by = new_row.id

    _recompute_summary(s, run)
    s.commit()
    s.refresh(new_row)
    return {
        "id": new_row.id,
        "run_id": new_row.run_id,
        "dimension": new_row.dimension,
        "score": new_row.score,
        "source": new_row.source,
        "rationale": new_row.rationale,
        "evidence_json": new_row.evidence_json,
        "created_at": new_row.created_at,
        "superseded_by": new_row.superseded_by,
    }


# ---------------------------------------------------------------------------
# POST /api/runs/{run_id}/retro-scores/recompute
# ---------------------------------------------------------------------------

@router.post("/api/runs/{run_id}/retro-scores/recompute")
def recompute_retro_scores(run_id: str, s: Session = Depends(get_session)):
    run = s.get(Run, run_id)
    if not run:
        raise HTTPException(404, "run not found")

    from ..core.retro_scorer import score_run_terminal
    # score_run_terminal opens its own session_scope; idempotent
    score_run_terminal(run_id)

    # Reload run to pick up summary written by score_run_terminal
    s.expire(run)
    s.refresh(run)

    return {"computed": True, "summary": run.retro_score_summary}


# ---------------------------------------------------------------------------
# GET /api/retro-score-weights
# ---------------------------------------------------------------------------

@router.get("/api/retro-score-weights")
def get_retro_score_weights(s: Session = Depends(get_session)):
    row = s.get(RetroScoreWeights, 1)
    if not row:
        raise HTTPException(404, "RetroScoreWeights singleton not seeded")
    return {"weights": row.weights_json, "updated_at": row.updated_at}


# ---------------------------------------------------------------------------
# PUT /api/retro-score-weights
# ---------------------------------------------------------------------------

@router.put("/api/retro-score-weights")
def set_retro_score_weights(body: RetroWeightsIn, s: Session = Depends(get_session)):
    if "overall" in body.weights:
        raise HTTPException(400, "'overall' is computed and cannot be set directly")

    invalid = set(body.weights) - _VALID_DIMS
    if invalid:
        raise HTTPException(400, f"invalid dimension(s): {sorted(invalid)}")

    for k, v in body.weights.items():
        if not (0.0 <= v <= 1.0):
            raise HTTPException(400, f"weight for '{k}' must be 0-1, got {v}")

    full_weights = {dim: body.weights.get(dim, 0.0) for dim in _VALID_DIMS}
    total = sum(full_weights.values())
    if not (0.99 <= total <= 1.01):
        raise HTTPException(400, f"weights must sum to ~1.0 (got {total:.4f})")

    row = s.get(RetroScoreWeights, 1)
    if not row:
        row = RetroScoreWeights(id=1, weights_json=full_weights)
        s.add(row)
    else:
        row.weights_json = full_weights
        row.updated_at = datetime.utcnow()
    s.commit()
    s.refresh(row)
    return {"weights": row.weights_json, "updated_at": row.updated_at}


# ---------------------------------------------------------------------------
# GET /api/lessons/pending
# ---------------------------------------------------------------------------

@router.get("/api/lessons/pending", response_model=list[LessonOut])
def list_pending_lessons(limit: int = Query(50, ge=1, le=200),
                         offset: int = Query(0, ge=0),
                         s: Session = Depends(get_session)):
    return (s.query(TargetLesson)
              .filter(TargetLesson.status == "pending_review",
                      TargetLesson.deleted_at.is_(None))
              .order_by(TargetLesson.created_at.desc())
              .offset(offset)
              .limit(limit)
              .all())


# ---------------------------------------------------------------------------
# POST /api/lessons/{lesson_id}/approve
# ---------------------------------------------------------------------------

@router.post("/api/lessons/{lesson_id}/approve", response_model=LessonOut)
def approve_lesson(lesson_id: str, s: Session = Depends(get_session)):
    lesson = (s.query(TargetLesson)
               .filter(TargetLesson.id == lesson_id,
                       TargetLesson.deleted_at.is_(None))
               .first())
    if not lesson:
        raise HTTPException(404, "lesson not found")
    lesson.status = "active"
    s.commit()
    s.refresh(lesson)
    return lesson


# ---------------------------------------------------------------------------
# POST /api/lessons/{lesson_id}/archive
# ---------------------------------------------------------------------------

@router.post("/api/lessons/{lesson_id}/archive", response_model=LessonOut)
def archive_lesson(lesson_id: str, s: Session = Depends(get_session)):
    lesson = (s.query(TargetLesson)
               .filter(TargetLesson.id == lesson_id,
                       TargetLesson.deleted_at.is_(None))
               .first())
    if not lesson:
        raise HTTPException(404, "lesson not found")
    lesson.status = "archived"
    s.commit()
    s.refresh(lesson)
    return lesson
