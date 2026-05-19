"""Wave-6 retro scoring — DB schema smoke tests (chunk A1).

All tests share a single in-memory SQLite DB created by the `db_engine`
module-scoped fixture. init_db() runs once, seeding tables + migrations.
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
    """Patch the module-level engine in backend.app.db to point at an
    in-memory SQLite DB, call init_db(), then restore on teardown."""
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


# ---------------------------------------------------------------------------
# Test 1 — RetroScore insert + query roundtrip
# ---------------------------------------------------------------------------

def test_retro_score_roundtrip(db_session):
    from backend.app.models import RetroScore, Run

    run = Run(kind="agent", target_slug="retro-test-run", status="success")
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    rs = RetroScore(
        run_id=run.id,
        dimension="accuracy",
        score=8,
        source="auto",
        rationale="solid output",
        evidence_json={"tokens_checked": 42},
    )
    db_session.add(rs)
    db_session.commit()
    db_session.refresh(rs)

    fetched = db_session.get(RetroScore, rs.id)
    assert fetched is not None
    assert fetched.score == 8
    assert fetched.dimension == "accuracy"
    assert fetched.source == "auto"
    assert fetched.evidence_json == {"tokens_checked": 42}
    assert fetched.superseded_by is None
    assert fetched.created_at is not None


# ---------------------------------------------------------------------------
# Test 2 — RetroScoreWeights seed present and sums to 1.0
# ---------------------------------------------------------------------------

def test_retro_score_weights_seed(db_session):
    from backend.app.models import RetroScoreWeights

    weights = db_session.get(RetroScoreWeights, 1)
    assert weights is not None, "RetroScoreWeights singleton (id=1) not seeded"
    total = sum(weights.weights_json.values())
    assert abs(total - 1.0) < 1e-9, f"weights sum to {total}, expected 1.0"


# ---------------------------------------------------------------------------
# Test 3 — PRAGMA confirms retro_score_summary column on runs
# ---------------------------------------------------------------------------

def test_pragma_retro_score_summary(db_engine):
    with db_engine.connect() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(runs)").fetchall()
    col_names = {r[1] for r in rows}
    assert "retro_score_summary" in col_names, \
        f"retro_score_summary missing from runs; columns: {col_names}"


# ---------------------------------------------------------------------------
# Test 4 — PRAGMA confirms status column on target_lessons
# ---------------------------------------------------------------------------

def test_pragma_target_lessons_status(db_engine):
    with db_engine.connect() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(target_lessons)").fetchall()
    col_names = {r[1] for r in rows}
    assert "status" in col_names, \
        f"status missing from target_lessons; columns: {col_names}"


# ---------------------------------------------------------------------------
# Test 5 — search_lessons respects include_pending filter
# ---------------------------------------------------------------------------

def test_search_lessons_pending_filter(db_session):
    from backend.app.models import Target, TargetLesson
    from backend.app.api.lessons import search_lessons

    target = Target(
        slug="retro-test-target-a1",
        name="Retro Test Target A1",
        source_kind="manual",
    )
    db_session.add(target)
    db_session.commit()
    db_session.refresh(target)

    lesson = TargetLesson(
        target_id=target.id,
        category="pitfall",
        title="Pending-review lesson for A1 test",
        status="pending_review",
    )
    db_session.add(lesson)
    db_session.commit()
    db_session.refresh(lesson)

    # Default: include_pending=False → pending_review excluded
    hits_default = search_lessons(
        tags=None, category=None, q=None,
        include_superseded=False, include_pending=False,
        limit=200, use_rag=False, s=db_session,
    )
    default_ids = {h.lesson.id for h in hits_default}
    assert lesson.id not in default_ids, \
        "pending_review lesson should be excluded when include_pending=False"

    # include_pending=True → pending_review included
    hits_with = search_lessons(
        tags=None, category=None, q=None,
        include_superseded=False, include_pending=True,
        limit=200, use_rag=False, s=db_session,
    )
    with_ids = {h.lesson.id for h in hits_with}
    assert lesson.id in with_ids, \
        "pending_review lesson should be included when include_pending=True"
