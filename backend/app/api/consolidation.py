"""Lesson consolidation — Wave-6 L2.

POST /api/lessons/consolidate               — merge N lessons into one
GET  /api/lessons/consolidate/suggestions   — find candidate clusters (deterministic, no LLM)
POST /api/lessons/consolidate/draft         — kick off a planner run to draft merged content
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import LessonEvidenceRun, TargetLesson
from ..schemas import ConsolidateDraftIn, ConsolidateSuggestion, LessonConsolidateIn, LessonOut

router = APIRouter(prefix="/api/lessons/consolidate", tags=["lessons"])

_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "it", "this", "that", "be", "as",
    "are", "was", "were", "has", "have", "had", "not", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "can",
})


# ---------------------------------------------------------------------------
# Pure helpers — no I/O
# ---------------------------------------------------------------------------

def _tokenize(title: str) -> set[str]:
    tokens = re.split(r"[^a-z0-9]+", title.lower())
    return {t for t in tokens if t and t not in _STOPWORDS and len(t) > 1}


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    u = a | b
    return len(a & b) / len(u) if u else 0.0


def _title_overlap(ta: set[str], tb: set[str]) -> float:
    if not ta and not tb:
        return 1.0
    inter = ta & tb
    denom = min(len(ta), len(tb))
    return len(inter) / denom if denom else 0.0


def _pair_scores(la: TargetLesson, lb: TargetLesson) -> tuple[float, float]:
    """Returns (jaccard_tags, title_overlap_ratio)."""
    jt = _jaccard(set(la.applicable_tags or []), set(lb.applicable_tags or []))
    to_ = _title_overlap(_tokenize(la.title), _tokenize(lb.title))
    return jt, to_


def _meets_threshold(jt: float, to_: float) -> bool:
    return (jt >= 0.5 and to_ >= 0.3) or jt >= 0.8


# ---------------------------------------------------------------------------
# POST /api/lessons/consolidate
# ---------------------------------------------------------------------------

@router.post("", response_model=LessonOut)
def consolidate_lessons(body: LessonConsolidateIn, s: Session = Depends(get_session)):
    """Merge N existing lessons into one consolidated lesson.

    Sources are archived (status='archived', superseded_by=new.id).
    The new lesson inherits the union of evidence_run_ids and applicable_tags.
    """
    from ..api.lessons import _delete_lesson_from_rag, _sync_lesson_to_rag

    if len(body.lesson_ids) < 2:
        raise HTTPException(400, "lesson_ids must contain at least 2 IDs")

    # 1. Load and validate sources
    sources: list[TargetLesson] = []
    for lid in body.lesson_ids:
        lesson = s.query(TargetLesson).filter(TargetLesson.id == lid).first()
        if not lesson:
            raise HTTPException(404, f"lesson {lid} not found")
        if lesson.status == "archived":
            raise HTTPException(400, f"lesson {lid} is already archived — cannot consolidate archived lessons")
        sources.append(lesson)

    # 2. Compute defaults from sources
    target_id = body.target_id or sources[0].target_id

    if body.applicable_tags is not None:
        applicable_tags = list(body.applicable_tags)
    else:
        union_tags: set[str] = set()
        for src in sources:
            union_tags.update(src.applicable_tags or [])
        applicable_tags = sorted(union_tags)

    if body.confidence is not None:
        confidence = body.confidence
    else:
        confidence = min(
            sources, key=lambda s: _CONFIDENCE_ORDER.get(s.confidence, 99)
        ).confidence

    if body.category is not None:
        category = body.category
    else:
        cats = [src.category for src in sources]
        category = Counter(cats).most_common(1)[0][0]

    # Union of evidence_run_ids JSON fields
    union_run_ids: set[str] = set()
    for src in sources:
        union_run_ids.update(src.evidence_run_ids or [])
    evidence_run_ids = sorted(union_run_ids)

    # 3. Insert the new lesson
    new_lesson = TargetLesson(
        target_id=target_id,
        category=category,
        title=body.title,
        content=body.content,
        evidence_run_ids=evidence_run_ids,
        confidence=confidence,
        applicable_tags=applicable_tags,
        source="consolidated",
        status="active",
    )
    s.add(new_lesson)
    s.flush()  # get new_lesson.id

    # 5 + 6. Build evidence run join rows for the new lesson.
    # Primary: all runs that were 'primary' in any source → inherit for navigation.
    # consolidated_from: all runs from all source evidence_run rows (any role).
    primary_run_ids: set[str] = set()
    all_source_run_ids: set[str] = set()

    for src in sources:
        for er in src.evidence_runs:
            all_source_run_ids.add(er.run_id)
            if er.role == "primary":
                primary_run_ids.add(er.run_id)
        # Also include from JSON field in case join table not fully synced
        for rid in src.evidence_run_ids or []:
            all_source_run_ids.add(rid)

    seen_role_pairs: set[tuple[str, str]] = set()
    for run_id in primary_run_ids:
        if (run_id, "primary") not in seen_role_pairs:
            seen_role_pairs.add((run_id, "primary"))
            s.add(LessonEvidenceRun(lesson_id=new_lesson.id, run_id=run_id, role="primary"))
    for run_id in all_source_run_ids:
        if (run_id, "consolidated_from") not in seen_role_pairs:
            seen_role_pairs.add((run_id, "consolidated_from"))
            s.add(LessonEvidenceRun(lesson_id=new_lesson.id, run_id=run_id, role="consolidated_from"))

    # 4. Archive source lessons
    for src in sources:
        src.status = "archived"
        src.superseded_by = new_lesson.id

    s.commit()
    s.refresh(new_lesson)

    # 8. Push new lesson to RAG (best-effort)
    _sync_lesson_to_rag(s, new_lesson)

    # 9. Remove archived sources from RAG
    for src in sources:
        _delete_lesson_from_rag(src)

    return new_lesson


# ---------------------------------------------------------------------------
# GET /api/lessons/consolidate/suggestions
# ---------------------------------------------------------------------------

@router.get("/suggestions", response_model=list[ConsolidateSuggestion])
def suggest_consolidation(
    min_overlap: int = Query(1, ge=1, description="Min shared tags for a cluster to be reported."),
    limit: int = Query(20, ge=1, le=200),
    min_cluster_size: int = Query(2, ge=2),
    s: Session = Depends(get_session),
) -> list[ConsolidateSuggestion]:
    """Propose consolidation clusters via deterministic tag+title overlap. No LLM."""
    lessons = (
        s.query(TargetLesson)
        .filter(TargetLesson.status == "active", TargetLesson.deleted_at.is_(None))
        .all()
    )
    if len(lessons) < 2:
        return []

    # Union-find clustering
    parent: dict[str, str] = {l.id: l.id for l in lessons}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        parent[find(x)] = find(y)

    # Precompute pair scores and apply clustering
    n = len(lessons)
    for i in range(n):
        for j in range(i + 1, n):
            jt, to_ = _pair_scores(lessons[i], lessons[j])
            if _meets_threshold(jt, to_):
                union(lessons[i].id, lessons[j].id)

    # Group by root
    clusters: dict[str, list[TargetLesson]] = defaultdict(list)
    for l in lessons:
        clusters[find(l.id)].append(l)

    results: list[ConsolidateSuggestion] = []
    for root, members in clusters.items():
        if len(members) < min_cluster_size:
            continue

        # Common tags = intersection of all members' tags
        common_tags: set[str] = set(members[0].applicable_tags or [])
        for m in members[1:]:
            common_tags &= set(m.applicable_tags or [])

        # Filter by min_overlap on common tag count
        if len(common_tags) < min_overlap:
            continue

        # Majority category
        cat_counts: Counter[str] = Counter(m.category for m in members)
        common_category = cat_counts.most_common(1)[0][0]

        # Confidence = average pairwise jaccard_tags
        pair_jaccards: list[float] = []
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                jt, _ = _pair_scores(members[i], members[j])
                pair_jaccards.append(jt)
        confidence = round(sum(pair_jaccards) / len(pair_jaccards), 3) if pair_jaccards else 0.0

        # Reason string
        common_title_tokens: set[str] = _tokenize(members[0].title)
        for m in members[1:]:
            common_title_tokens &= _tokenize(m.title)
        reason_parts: list[str] = []
        if common_tags:
            reason_parts.append(f"shared tags {{{', '.join(sorted(common_tags))}}}")
        if common_title_tokens:
            reason_parts.append(
                "titles overlap " + ", ".join(f"'{t}'" for t in sorted(common_title_tokens))
            )
        reason = "; ".join(reason_parts) if reason_parts else "similar content"

        results.append(ConsolidateSuggestion(
            lesson_ids=[m.id for m in members],
            reason=reason,
            confidence=confidence,
            common_tags=sorted(common_tags),
            common_category=common_category,
        ))

    results.sort(key=lambda x: -x.confidence)
    return results[:limit]


# ---------------------------------------------------------------------------
# POST /api/lessons/consolidate/draft
# ---------------------------------------------------------------------------

@router.post("/draft")
def draft_consolidation(body: ConsolidateDraftIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    """Kick off a planner agent run to draft merged title + content.

    Returns {run_id, status: 'running'}. UI polls run_status. When done,
    the caller parses the planner output (TITLE / CATEGORY / TAGS / BODY sections)
    and offers for approval before calling POST /api/lessons/consolidate.

    This is the ONLY LLM call in the consolidation flow — opt-in, on demand.
    """
    from ..core.executor import start_agent_run_bg

    if len(body.lesson_ids) < 2:
        raise HTTPException(400, "lesson_ids must contain at least 2 IDs")

    lessons: list[TargetLesson] = []
    for lid in body.lesson_ids:
        lesson = s.query(TargetLesson).filter(TargetLesson.id == lid).first()
        if not lesson:
            raise HTTPException(404, f"lesson {lid} not found")
        lessons.append(lesson)

    # Build the structured prompt for the planner
    lesson_blocks = []
    for i, l in enumerate(lessons, 1):
        lesson_blocks.append(
            f"--- Lesson {i} ---\n"
            f"Title: {l.title}\n"
            f"Category: {l.category}\n"
            f"Tags: {', '.join(l.applicable_tags or [])}\n"
            f"Content:\n{l.content}"
        )
    lessons_text = "\n\n".join(lesson_blocks)

    prompt = (
        f"Consolidate these {len(lessons)} lessons into ONE. Return STRICTLY:\n"
        "TITLE: <one line>\n"
        "CATEGORY: <one of: pitfall|time-saver|tooling-gap|pattern-that-worked|prompt-fix|cost-trap|scope-creep>\n"
        "TAGS: tag1, tag2, tag3\n"
        "BODY:\n"
        "<markdown body — merged, deduplicated, with evidence preserved>\n\n"
        f"LESSONS TO MERGE:\n\n{lessons_text}"
    )

    run_id = start_agent_run_bg("planner", prompt)
    return {"run_id": run_id, "status": "running"}
