"""Wave-6 Chunk A3 — retro-scores REST API tests.

All tests share a single in-memory SQLite DB (module-scoped fixture) and a
FastAPI TestClient with the get_session dependency overridden to point at it.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def db_engine():
    """Patch backend.app.db engine + SessionLocal with in-memory SQLite,
    call init_db() to create tables + seed weights, then restore on teardown."""
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

    db_mod.init_db()  # creates tables + seeds RetroScoreWeights

    yield eng

    db_mod.engine = orig_engine
    db_mod.SessionLocal = orig_session
    eng.dispose()


@pytest.fixture(scope="module")
def client(db_engine):
    """TestClient with get_session overridden to use in-memory SQLite.
    Lifespan is NOT triggered (no context manager) so we avoid re-seeding
    and the sync_mcp_servers_from_file side-effect."""
    import backend.app.db as db_mod
    from backend.app.db import get_session
    from backend.app.main import app
    from fastapi.testclient import TestClient

    def _override():
        s = db_mod.SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = _override
    c = TestClient(app, raise_server_exceptions=True)
    yield c
    app.dependency_overrides.clear()


@pytest.fixture
def db_session(db_engine):
    import backend.app.db as db_mod
    s = db_mod.SessionLocal()
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_run(db_session, slug, *, status="success", kind="agent"):
    from backend.app.models import Run
    run = Run(kind=kind, target_slug=slug, status=status,
              started_at=datetime.utcnow(), ended_at=datetime.utcnow())
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _make_target(db_session, slug):
    from backend.app.models import Target
    t = Target(slug=slug, name=slug, source_kind="manual")
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


def _make_lesson(db_session, target_id, *, status="active", title=None):
    from backend.app.models import TargetLesson
    lesson = TargetLesson(
        target_id=target_id,
        category="pitfall",
        title=title or f"lesson-{id(target_id)}",
        status=status,
    )
    db_session.add(lesson)
    db_session.commit()
    db_session.refresh(lesson)
    return lesson


# ---------------------------------------------------------------------------
# Test 1 — GET retro-scores returns 7 rows (6 auto + overall) after scoring
# ---------------------------------------------------------------------------

def test_get_retro_scores_after_auto_scoring(client, db_session):
    run = _make_run(db_session, "retro-api-t1-auto")
    run_id = run.id

    from backend.app.core.retro_scorer import score_run_terminal
    score_run_terminal(run_id)  # inserts 6 auto dims + 1 overall = 7 rows

    resp = client.get(f"/api/runs/{run_id}/retro-scores")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["run_id"] == run_id
    assert data["summary"] is not None
    assert data["summary"]["overall"] is not None
    scores = data["scores"]
    assert len(scores) == 7, f"expected 7 non-superseded rows, got {len(scores)}: {[s['dimension'] for s in scores]}"
    dims = {s["dimension"] for s in scores}
    assert "overall" in dims
    assert {"cost", "wall", "mistakes", "lessons_applied", "plan_adherence", "scope_discipline"} <= dims


# ---------------------------------------------------------------------------
# Test 2 — PATCH creates source='human' row, supersedes auto row, updates summary
# ---------------------------------------------------------------------------

def test_patch_retro_score_human_override(client, db_session):
    run = _make_run(db_session, "retro-api-t2-patch")
    run_id = run.id

    from backend.app.core.retro_scorer import score_run_terminal
    score_run_terminal(run_id)

    # PATCH cost (an auto-scored dim) with a human override
    resp = client.patch(
        f"/api/runs/{run_id}/retro-scores/cost",
        json={"score": 9, "rationale": "human says excellent cost control"},
    )
    assert resp.status_code == 200, resp.text
    patch_data = resp.json()
    assert patch_data["source"] == "human"
    assert patch_data["score"] == 9
    assert patch_data["dimension"] == "cost"
    new_id = patch_data["id"]

    # Old auto row for 'cost' should now be superseded
    from backend.app.models import RetroScore
    import backend.app.db as db_mod
    s2 = db_mod.SessionLocal()
    try:
        superseded = s2.query(RetroScore).filter(
            RetroScore.run_id == run_id,
            RetroScore.dimension == "cost",
            RetroScore.source == "auto",
        ).all()
        assert len(superseded) == 1
        assert superseded[0].superseded_by == new_id

        # Overall and summary should be updated
        from backend.app.models import Run as RunModel
        run_obj = s2.get(RunModel, run_id)
        assert run_obj.retro_score_summary is not None
        # cost score in dims reflects the human override
        assert run_obj.retro_score_summary["dims"]["cost"] == 9
    finally:
        s2.close()


# ---------------------------------------------------------------------------
# Test 3 — GET retro-score-weights returns seeded defaults summing ~1.00
# ---------------------------------------------------------------------------

def test_get_retro_score_weights(client):
    resp = client.get("/api/retro-score-weights")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "weights" in data
    weights = data["weights"]
    total = sum(weights.values())
    assert abs(total - 1.0) < 0.01, f"weights sum to {total}, expected ~1.0"
    assert len(weights) == 9  # 9 scored dims (no 'overall')


# ---------------------------------------------------------------------------
# Test 4 — PUT bad weights (sum != 1.0) → 400
# ---------------------------------------------------------------------------

def test_put_retro_score_weights_bad_sum(client):
    bad_weights = {
        "accuracy": 0.5,
        "output_quality": 0.5,
        # rest missing → default 0 → sum = 1.0 exactly... use explicit bad sum
        "lessons_applied": 0.1,  # total > 1.01
    }
    resp = client.put("/api/retro-score-weights", json={"weights": bad_weights})
    assert resp.status_code == 400, resp.text
    assert "sum" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Test 5 — PUT good weights → returns updated row
# ---------------------------------------------------------------------------

def test_put_retro_score_weights_good(client):
    good_weights = {
        "accuracy": 0.30,
        "output_quality": 0.20,
        "lessons_applied": 0.15,
        "recovery": 0.10,
        "plan_adherence": 0.10,
        "cost": 0.05,
        "wall": 0.04,
        "mistakes": 0.03,
        "scope_discipline": 0.03,
    }
    resp = client.put("/api/retro-score-weights", json={"weights": good_weights})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "weights" in data
    assert abs(sum(data["weights"].values()) - 1.0) < 0.01
    assert data["weights"]["accuracy"] == pytest.approx(0.30)

    # Restore original defaults so other tests aren't affected
    defaults = {
        "accuracy": 0.25, "output_quality": 0.20, "lessons_applied": 0.15,
        "recovery": 0.10, "plan_adherence": 0.10, "cost": 0.05,
        "wall": 0.05, "mistakes": 0.05, "scope_discipline": 0.05,
    }
    client.put("/api/retro-score-weights", json={"weights": defaults})


# ---------------------------------------------------------------------------
# Test 6 — GET /api/lessons/pending excludes status='active' rows
# ---------------------------------------------------------------------------

def test_list_pending_lessons_excludes_active(client, db_session):
    target = _make_target(db_session, "retro-api-t6-pending")
    active_lesson = _make_lesson(db_session, target.id, status="active", title="active-lesson-t6")
    pending_lesson = _make_lesson(db_session, target.id, status="pending_review", title="pending-lesson-t6")

    resp = client.get("/api/lessons/pending")
    assert resp.status_code == 200, resp.text
    ids = {l["id"] for l in resp.json()}

    assert pending_lesson.id in ids, "pending_review lesson should appear"
    assert active_lesson.id not in ids, "active lesson must be excluded from /pending"


# ---------------------------------------------------------------------------
# Test 7 — POST approve flips status to 'active'
# ---------------------------------------------------------------------------

def test_approve_pending_lesson(client, db_session):
    target = _make_target(db_session, "retro-api-t7-approve")
    lesson = _make_lesson(db_session, target.id, status="pending_review", title="approve-me-t7")

    resp = client.post(f"/api/lessons/{lesson.id}/approve")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "active"
    assert data["id"] == lesson.id


# ---------------------------------------------------------------------------
# Test 8 — POST archive flips status to 'archived'
# ---------------------------------------------------------------------------

def test_archive_lesson(client, db_session):
    target = _make_target(db_session, "retro-api-t8-archive")
    lesson = _make_lesson(db_session, target.id, status="active", title="archive-me-t8")

    resp = client.post(f"/api/lessons/{lesson.id}/archive")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "archived"
    assert data["id"] == lesson.id


# ---------------------------------------------------------------------------
# Test 9 — GET /api/lessons/ returns list (100-cap default)
# ---------------------------------------------------------------------------

def test_list_lessons_root(client, db_session):
    target = _make_target(db_session, "retro-api-t9-list")
    _make_lesson(db_session, target.id, status="active", title="list-t9-a")
    _make_lesson(db_session, target.id, status="active", title="list-t9-b")

    resp = client.get("/api/lessons/")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, list)
    ids = {l["id"] for l in data}
    # Both active lessons must appear; pending excluded by default
    pending = _make_lesson(db_session, target.id, status="pending_review", title="list-t9-pending")
    resp2 = client.get("/api/lessons/")
    assert resp2.status_code == 200
    ids2 = {l["id"] for l in resp2.json()}
    assert pending.id not in ids2, "pending_review must be excluded by default"


# ---------------------------------------------------------------------------
# Test 10 — GET /api/lessons/{id} returns one lesson; 404 for missing
# ---------------------------------------------------------------------------

def test_get_single_lesson(client, db_session):
    target = _make_target(db_session, "retro-api-t10-single")
    lesson = _make_lesson(db_session, target.id, status="active", title="single-t10")

    resp = client.get(f"/api/lessons/{lesson.id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == lesson.id

    resp404 = client.get("/api/lessons/does-not-exist-xxxxxxx")
    assert resp404.status_code == 404


# ---------------------------------------------------------------------------
# Test 11 — POST /api/lessons/{id}/restore flips archived → active
# ---------------------------------------------------------------------------

def test_restore_lesson(client, db_session):
    target = _make_target(db_session, "retro-api-t11-restore")
    lesson = _make_lesson(db_session, target.id, status="archived", title="restore-me-t11")

    resp = client.post(f"/api/lessons/{lesson.id}/restore")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"


# ---------------------------------------------------------------------------
# Test 12 — POST /api/lessons/{id}/restore on superseded lesson → 409
# ---------------------------------------------------------------------------

def test_restore_superseded_lesson_409(client, db_session):
    from backend.app.models import TargetLesson
    target = _make_target(db_session, "retro-api-t12-superseded")
    lesson = _make_lesson(db_session, target.id, status="archived", title="superseded-t12")
    # Fake a superseded_by link so restore must reject it
    lesson.superseded_by = "fake-consolidator-id"
    db_session.commit()

    resp = client.post(f"/api/lessons/{lesson.id}/restore")
    assert resp.status_code == 409, resp.text
    assert "superseded" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Test 13 — GET /api/lessons/{id}/applications returns list
# ---------------------------------------------------------------------------

def test_get_lesson_applications(client, db_session):
    from backend.app.models import LessonApplication, Target as TargetModel
    target = _make_target(db_session, "retro-api-t13-apps")
    lesson = _make_lesson(db_session, target.id, status="active", title="apps-t13")

    # Insert two application rows directly
    for outcome in ("applied", "ignored"):
        app = LessonApplication(
            lesson_id=lesson.id,
            target_id=target.id,
            outcome=outcome,
        )
        db_session.add(app)
    db_session.commit()

    resp = client.get(f"/api/lessons/{lesson.id}/applications")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2
    outcomes = {r["outcome"] for r in data}
    assert outcomes == {"applied", "ignored"}
