"""Wave-6 L1 — LessonEvidenceRun join table + created_in_run_id FK.

Tests verify:
  1. create_target_lesson writes join rows + created_in_run_id
  2. update (drop run) removes the primary join row
  3. update (add run) inserts a new primary join row
  4. LessonOut.linked_runs is populated with run status/kind
  5. Backfill (_backfill_lesson_evidence_runs) is idempotent
  6. GET /api/lessons/{id}/runs returns expected shape
  7. pending_review lessons still get join rows correctly
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


def _make_target(s, slug):
    from backend.app.models import Target
    t = Target(slug=slug, name=f"Target {slug}", source_kind="manual")
    s.add(t)
    s.commit()
    s.refresh(t)
    return t


def _make_run(s, slug="test-run"):
    from backend.app.models import Run
    r = Run(kind="agent", target_slug=slug, status="success")
    s.add(r)
    s.commit()
    s.refresh(r)
    return r


# ---------------------------------------------------------------------------
# Test 1 — create lesson writes join rows + created_in_run_id
# ---------------------------------------------------------------------------

def test_create_writes_join_rows_and_created_in_run_id(db_session):
    from backend.app.api.lessons import create_target_lesson
    from backend.app.models import LessonEvidenceRun
    from backend.app.schemas import LessonIn

    target = _make_target(db_session, "l1-test-create-01")
    r1 = _make_run(db_session, "l1-run-01")
    r2 = _make_run(db_session, "l1-run-02")
    r3 = _make_run(db_session, "l1-run-03")  # created_in_run_id

    body = LessonIn(
        category="pitfall",
        title="Test create join rows",
        evidence_run_ids=[r1.id, r2.id],
        created_in_run_id=r3.id,
    )
    lesson_out = create_target_lesson(slug=target.slug, body=body, s=db_session)

    # Join rows
    rows = (db_session.query(LessonEvidenceRun)
              .filter(LessonEvidenceRun.lesson_id == lesson_out.id,
                      LessonEvidenceRun.role == "primary")
              .all())
    assert len(rows) == 2
    linked_run_ids = {r.run_id for r in rows}
    assert r1.id in linked_run_ids
    assert r2.id in linked_run_ids

    # created_in_run_id persisted
    from backend.app.models import TargetLesson
    lesson_orm = db_session.get(TargetLesson, lesson_out.id)
    assert lesson_orm.created_in_run_id == r3.id


# ---------------------------------------------------------------------------
# Test 2 — update drops a run → join row removed
# ---------------------------------------------------------------------------

def test_update_drop_run_removes_join_row(db_session):
    from backend.app.api.lessons import create_target_lesson, update_target_lesson
    from backend.app.models import LessonEvidenceRun
    from backend.app.schemas import LessonIn, LessonUpdate

    target = _make_target(db_session, "l1-test-drop-02")
    r1 = _make_run(db_session, "l1-drop-r1")
    r2 = _make_run(db_session, "l1-drop-r2")

    body = LessonIn(
        category="time-saver",
        title="Test drop run",
        evidence_run_ids=[r1.id, r2.id],
    )
    lesson_out = create_target_lesson(slug=target.slug, body=body, s=db_session)

    # Now drop r1
    update = LessonUpdate(evidence_run_ids=[r2.id])
    update_target_lesson(slug=target.slug, lesson_id=lesson_out.id, body=update, s=db_session)

    rows = (db_session.query(LessonEvidenceRun)
              .filter(LessonEvidenceRun.lesson_id == lesson_out.id,
                      LessonEvidenceRun.role == "primary")
              .all())
    assert len(rows) == 1
    assert rows[0].run_id == r2.id


# ---------------------------------------------------------------------------
# Test 3 — update adds a run → join row inserted
# ---------------------------------------------------------------------------

def test_update_add_run_inserts_join_row(db_session):
    from backend.app.api.lessons import create_target_lesson, update_target_lesson
    from backend.app.models import LessonEvidenceRun
    from backend.app.schemas import LessonIn, LessonUpdate

    target = _make_target(db_session, "l1-test-add-03")
    r1 = _make_run(db_session, "l1-add-r1")
    r4 = _make_run(db_session, "l1-add-r4")

    body = LessonIn(
        category="tooling-gap",
        title="Test add run",
        evidence_run_ids=[r1.id],
    )
    lesson_out = create_target_lesson(slug=target.slug, body=body, s=db_session)

    update = LessonUpdate(evidence_run_ids=[r1.id, r4.id])
    update_target_lesson(slug=target.slug, lesson_id=lesson_out.id, body=update, s=db_session)

    rows = (db_session.query(LessonEvidenceRun)
              .filter(LessonEvidenceRun.lesson_id == lesson_out.id,
                      LessonEvidenceRun.role == "primary")
              .all())
    assert len(rows) == 2
    ids = {r.run_id for r in rows}
    assert r1.id in ids
    assert r4.id in ids


# ---------------------------------------------------------------------------
# Test 4 — LessonOut.linked_runs populated with run status/kind
# ---------------------------------------------------------------------------

def test_lesson_out_linked_runs_populated(db_session):
    from backend.app.api.lessons import create_target_lesson
    from backend.app.models import TargetLesson
    from backend.app.schemas import LessonIn, LessonOut

    target = _make_target(db_session, "l1-test-linked-04")
    run = _make_run(db_session, "l1-linked-run")  # status='success', kind='agent'

    body = LessonIn(
        category="pattern-that-worked",
        title="Linked runs test",
        evidence_run_ids=[run.id],
    )
    lesson_out = create_target_lesson(slug=target.slug, body=body, s=db_session)

    # Re-read via ORM to exercise the linked_runs property
    lesson_orm = db_session.get(TargetLesson, lesson_out.id)
    db_session.refresh(lesson_orm)  # expire + reload

    serialized = LessonOut.model_validate(lesson_orm)
    assert len(serialized.linked_runs) == 1
    lr = serialized.linked_runs[0]
    assert lr["run_id"] == run.id
    assert lr["role"] == "primary"
    assert lr["status"] == "success"
    assert lr["kind"] == "agent"


# ---------------------------------------------------------------------------
# Test 5 — backfill is idempotent
# ---------------------------------------------------------------------------

def test_backfill_idempotent(db_engine, db_session):
    """Create a lesson with JSON evidence_run_ids but NO join rows (raw insert),
    then call the backfill. Verify rows are created. Call again → no duplicates."""
    from backend.app.models import LessonEvidenceRun, Target, TargetLesson, Run
    import backend.app.db as db_mod

    target = _make_target(db_session, "l1-test-backfill-05")
    run = _make_run(db_session, "l1-backfill-run")

    # Insert lesson with JSON evidence but no join rows (simulates pre-L1 data)
    lesson = TargetLesson(
        target_id=target.id,
        category="cost-trap",
        title="Backfill test lesson",
        evidence_run_ids=[run.id],
    )
    db_session.add(lesson)
    db_session.commit()

    # Confirm no join rows yet
    before = (db_session.query(LessonEvidenceRun)
                .filter(LessonEvidenceRun.lesson_id == lesson.id)
                .count())
    assert before == 0

    # Run backfill
    with db_engine.begin() as conn:
        db_mod._backfill_lesson_evidence_runs(conn)

    db_session.expire_all()
    after = (db_session.query(LessonEvidenceRun)
               .filter(LessonEvidenceRun.lesson_id == lesson.id,
                       LessonEvidenceRun.role == "primary")
               .all())
    assert len(after) == 1
    assert after[0].run_id == run.id

    # Run backfill again — idempotent
    with db_engine.begin() as conn:
        db_mod._backfill_lesson_evidence_runs(conn)

    db_session.expire_all()
    after2 = (db_session.query(LessonEvidenceRun)
                .filter(LessonEvidenceRun.lesson_id == lesson.id)
                .count())
    assert after2 == 1  # still exactly one


# ---------------------------------------------------------------------------
# Test 6 — GET /api/lessons/{lesson_id}/runs returns expected shape
# ---------------------------------------------------------------------------

def test_get_lesson_runs_endpoint(db_session):
    from backend.app.api.lessons import create_target_lesson, get_lesson_runs
    from backend.app.schemas import LessonIn

    target = _make_target(db_session, "l1-test-endpoint-06")
    r1 = _make_run(db_session, "l1-ep-r1")
    r2 = _make_run(db_session, "l1-ep-r2")

    body = LessonIn(
        category="scope-creep",
        title="Endpoint test lesson",
        evidence_run_ids=[r1.id, r2.id],
    )
    lesson_out = create_target_lesson(slug=target.slug, body=body, s=db_session)

    result = get_lesson_runs(lesson_id=lesson_out.id, limit=50, s=db_session)

    assert len(result) == 2
    # Ordered by created_at DESC of join row — both were inserted at roughly the same time;
    # just check that both run_ids are present with the right fields.
    run_ids = {r["run_id"] for r in result}
    assert r1.id in run_ids
    assert r2.id in run_ids
    for row in result:
        assert "run_id" in row
        assert "role" in row
        assert "status" in row
        assert "kind" in row
        assert "target_slug" in row
        assert row["role"] == "primary"


# ---------------------------------------------------------------------------
# Test 7 — pending_review lessons still get join rows correctly
# ---------------------------------------------------------------------------

def test_pending_review_lesson_gets_join_rows(db_session):
    from backend.app.api.lessons import _sync_evidence_runs
    from backend.app.models import LessonEvidenceRun, Target, TargetLesson

    target = _make_target(db_session, "l1-test-pending-07")
    run = _make_run(db_session, "l1-pending-run")

    lesson = TargetLesson(
        target_id=target.id,
        category="pitfall",
        title="Pending review lesson",
        status="pending_review",
        evidence_run_ids=[run.id],
    )
    db_session.add(lesson)
    db_session.flush()

    _sync_evidence_runs(db_session, lesson, [run.id])
    db_session.commit()
    db_session.refresh(lesson)

    rows = (db_session.query(LessonEvidenceRun)
              .filter(LessonEvidenceRun.lesson_id == lesson.id,
                      LessonEvidenceRun.role == "primary")
              .all())
    assert len(rows) == 1
    assert rows[0].run_id == run.id
    assert lesson.status == "pending_review"
