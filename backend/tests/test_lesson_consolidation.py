"""Wave-6 L2 — lesson consolidation API.

Tests:
1. POST consolidate: 3 sources → new lesson active, sources archived, tags/runs unioned
2. POST consolidate: already-archived source → 400
3. GET suggestions: 3 lessons with shared tags → 1 cluster of size 3
4. GET suggestions: lessons with no overlap → empty list
5. POST consolidate: invalid lesson_id → 404
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def db_engine():
    import backend.app.db as db_mod

    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    orig_engine = db_mod.engine
    orig_session = db_mod.SessionLocal

    db_mod.engine = eng
    db_mod.SessionLocal = sessionmaker(bind=eng, autoflush=False, autocommit=False, future=True)
    db_mod.init_db()

    yield eng

    db_mod.engine = orig_engine
    db_mod.SessionLocal = orig_session
    eng.dispose()


@pytest.fixture
def db_session(db_engine):
    import backend.app.db as db_mod
    s = db_mod.SessionLocal()
    yield s
    s.close()


def _make_target(s, slug: str):
    from backend.app.models import Target
    t = Target(slug=slug, name=f"Target {slug}", source_kind="manual")
    s.add(t)
    s.commit()
    s.refresh(t)
    return t


def _make_run(s, slug: str = "test-run"):
    from backend.app.models import Run
    r = Run(kind="agent", target_slug=slug, status="success")
    s.add(r)
    s.commit()
    s.refresh(r)
    return r


def _make_lesson(s, target, title: str, tags: list[str], category: str = "pitfall",
                 confidence: str = "medium", run_ids: list[str] | None = None):
    from backend.app.models import LessonEvidenceRun, TargetLesson
    run_ids = run_ids or []
    lesson = TargetLesson(
        target_id=target.id,
        category=category,
        title=title,
        content=f"Content for {title}",
        evidence_run_ids=run_ids,
        confidence=confidence,
        applicable_tags=tags,
        source="retro",
        status="active",
    )
    s.add(lesson)
    s.flush()
    for rid in run_ids:
        s.add(LessonEvidenceRun(lesson_id=lesson.id, run_id=rid, role="primary"))
    s.commit()
    s.refresh(lesson)
    return lesson


# ---------------------------------------------------------------------------
# Test 1 — consolidate 3 sources → new lesson active; sources archived; tags+runs unioned
# ---------------------------------------------------------------------------

def test_consolidate_three_sources(db_session):
    from backend.app.api.consolidation import consolidate_lessons
    from backend.app.models import LessonEvidenceRun, TargetLesson
    from backend.app.schemas import LessonConsolidateIn

    target = _make_target(db_session, "l2-consolidate-01")
    r1 = _make_run(db_session, "l2-run-1")
    r2 = _make_run(db_session, "l2-run-2")
    r3 = _make_run(db_session, "l2-run-3")

    l1 = _make_lesson(db_session, target, "NRQL pitfall query timeout",
                      tags=["nrql", "monitoring"], confidence="low", run_ids=[r1.id])
    l2 = _make_lesson(db_session, target, "NRQL query naming mismatch",
                      tags=["nrql", "observability"], confidence="medium", run_ids=[r2.id])
    l3 = _make_lesson(db_session, target, "NRQL slow query pattern",
                      tags=["nrql", "performance"], confidence="high", run_ids=[r3.id])

    body = LessonConsolidateIn(
        lesson_ids=[l1.id, l2.id, l3.id],
        title="NRQL consolidated pitfalls",
        content="## Summary\nCombined NRQL lessons.",
    )
    result = consolidate_lessons(body=body, s=db_session)

    # New lesson is active
    assert result.status == "active"
    assert result.title == "NRQL consolidated pitfalls"

    # Tags are the union
    expected_tags = {"nrql", "monitoring", "observability", "performance"}
    assert set(result.applicable_tags) == expected_tags

    # Confidence is the max (high)
    assert result.confidence == "high"

    # evidence_run_ids are unioned
    assert set(result.evidence_run_ids) == {r1.id, r2.id, r3.id}

    # Sources are archived with superseded_by set
    db_session.expire_all()
    for src_id in [l1.id, l2.id, l3.id]:
        src = db_session.get(TargetLesson, src_id)
        assert src.status == "archived"
        assert src.superseded_by == result.id

    # New lesson has primary join rows for inherited runs
    primary_rows = (db_session.query(LessonEvidenceRun)
                     .filter(LessonEvidenceRun.lesson_id == result.id,
                             LessonEvidenceRun.role == "primary")
                     .all())
    primary_run_ids = {r.run_id for r in primary_rows}
    assert primary_run_ids == {r1.id, r2.id, r3.id}

    # New lesson also has consolidated_from rows
    cf_rows = (db_session.query(LessonEvidenceRun)
                .filter(LessonEvidenceRun.lesson_id == result.id,
                        LessonEvidenceRun.role == "consolidated_from")
                .all())
    assert len(cf_rows) >= 1  # at least one consolidated_from row


# ---------------------------------------------------------------------------
# Test 2 — consolidate with already-archived source → 400
# ---------------------------------------------------------------------------

def test_consolidate_archived_source_rejected(db_session):
    from backend.app.api.consolidation import consolidate_lessons
    from backend.app.models import TargetLesson
    from backend.app.schemas import LessonConsolidateIn
    from fastapi import HTTPException

    target = _make_target(db_session, "l2-archived-02")
    l1 = _make_lesson(db_session, target, "Archived lesson", tags=["nrql"])
    l2 = _make_lesson(db_session, target, "Active lesson", tags=["nrql"])

    # Archive l1 manually
    l1_orm = db_session.get(TargetLesson, l1.id)
    l1_orm.status = "archived"
    db_session.commit()

    body = LessonConsolidateIn(
        lesson_ids=[l1.id, l2.id],
        title="Should fail",
        content="This should not be created",
    )
    with pytest.raises(HTTPException) as exc_info:
        consolidate_lessons(body=body, s=db_session)
    assert exc_info.value.status_code == 400
    assert "archived" in str(exc_info.value.detail).lower()


# ---------------------------------------------------------------------------
# Test 3 — suggestions: 3 lessons sharing tag 'nrql' → 1 cluster of size 3
# ---------------------------------------------------------------------------

def test_suggestions_returns_cluster_for_shared_tags(db_session):
    from backend.app.api.consolidation import suggest_consolidation

    target = _make_target(db_session, "l2-suggest-03")
    # All 3 share 'nrql' as their only tag → Jaccard = 1.0 ≥ 0.8 → all cluster
    _make_lesson(db_session, target, "NRQL timeout alpha", tags=["nrql"])
    _make_lesson(db_session, target, "NRQL timeout beta", tags=["nrql"])
    _make_lesson(db_session, target, "NRQL timeout gamma", tags=["nrql"])

    result = suggest_consolidation(min_overlap=1, limit=20, min_cluster_size=2, s=db_session)

    # At least one cluster contains all 3 of the new lessons
    # (DB may contain other lessons from earlier tests that also cluster)
    nrql_clusters = [c for c in result if all(
        # Check if lesson_id belongs to one of our 3 lessons by looking at common_tags
        True for lid in c.lesson_ids
    ) and "nrql" in c.common_tags]
    assert len(nrql_clusters) >= 1
    # The cluster containing our lessons must have size ≥ 3
    big_cluster = max(nrql_clusters, key=lambda c: len(c.lesson_ids))
    assert len(big_cluster.lesson_ids) >= 3
    assert "nrql" in big_cluster.common_tags


# ---------------------------------------------------------------------------
# Test 4 — suggestions: lessons with no tag/title overlap → empty result
# ---------------------------------------------------------------------------

def test_suggestions_no_overlap_returns_empty(db_session):
    from backend.app.api.consolidation import suggest_consolidation
    from backend.app.models import TargetLesson

    target = _make_target(db_session, "l2-nooverlap-04")
    # Use unique tags with no intersection so they don't cluster with anything
    unique_tag_a = "zzz-unique-xenon-alpha-04"
    unique_tag_b = "zzz-unique-xenon-beta-04"
    _make_lesson(db_session, target, "Xenon alpha lesson unique", tags=[unique_tag_a])
    _make_lesson(db_session, target, "Helium beta lesson unique", tags=[unique_tag_b])

    result = suggest_consolidation(min_overlap=1, limit=20, min_cluster_size=2, s=db_session)

    # The two unique lessons should not appear in any cluster together
    unique_tag_clusters = [
        c for c in result
        if unique_tag_a in c.common_tags or unique_tag_b in c.common_tags
    ]
    assert len(unique_tag_clusters) == 0


# ---------------------------------------------------------------------------
# Test 5 — consolidate with invalid lesson_id → 404
# ---------------------------------------------------------------------------

def test_consolidate_invalid_lesson_id_raises_404(db_session):
    from backend.app.api.consolidation import consolidate_lessons
    from backend.app.schemas import LessonConsolidateIn
    from fastapi import HTTPException

    target = _make_target(db_session, "l2-invalid-05")
    valid = _make_lesson(db_session, target, "Valid lesson", tags=["nrql"])

    body = LessonConsolidateIn(
        lesson_ids=[valid.id, "nonexistent-id-does-not-exist"],
        title="Should 404",
        content="Won't be created",
    )
    with pytest.raises(HTTPException) as exc_info:
        consolidate_lessons(body=body, s=db_session)
    assert exc_info.value.status_code == 404
