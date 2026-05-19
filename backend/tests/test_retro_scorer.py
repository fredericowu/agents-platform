"""Wave-6 retro scoring — auto-scorer tests (chunk A2).

All tests share a single in-memory SQLite DB (module-scoped fixture).
Unique target_slug values prevent cross-test baseline interference.
"""
from __future__ import annotations

from datetime import datetime, timedelta

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
# Helpers
# ---------------------------------------------------------------------------

def _make_success_run(db_session, slug, *, kind="agent", cost=0.05,
                      started_offset_s=-30, ended_offset_s=0):
    from backend.app.models import Run
    now = datetime.utcnow()
    r = Run(
        kind=kind,
        target_slug=slug,
        status="success",
        cost_usd=cost,
        started_at=now + timedelta(seconds=started_offset_s),
        ended_at=now + timedelta(seconds=ended_offset_s),
    )
    db_session.add(r)
    db_session.commit()
    db_session.refresh(r)
    return r


def _add_tool_event(db_session, run_id, tool, path=None, cmd=None, outcome=None, ts=None):
    from backend.app.models import RunEvent
    payload: dict = {"tool": tool}
    if path:
        payload["path"] = path
    if cmd:
        payload["cmd"] = cmd
    if outcome:
        payload["outcome"] = outcome
    e = RunEvent(
        run_id=run_id,
        kind="tool_call",
        payload=payload,
        ts=ts or datetime.utcnow(),
    )
    db_session.add(e)
    db_session.commit()
    return e


def _score_rows(db_session, run_id):
    from backend.app.models import RetroScore
    db_session.expire_all()
    return db_session.query(RetroScore).filter(
        RetroScore.run_id == run_id, RetroScore.source == "auto"
    ).all()


# ---------------------------------------------------------------------------
# Test 1 — non-existent run_id → no-op, no exception
# ---------------------------------------------------------------------------

def test_nonexistent_run_noop(db_engine):
    from backend.app.core.retro_scorer import score_run_terminal
    # Must not raise anything
    score_run_terminal("does-not-exist-xyz")


# ---------------------------------------------------------------------------
# Test 2 — no baseline → cost score=7 with rationale 'no baseline'
# ---------------------------------------------------------------------------

def test_no_baseline_cost(db_session, db_engine):
    from backend.app.core.retro_scorer import score_run_terminal

    run = _make_success_run(db_session, "no-baseline-agent-unique-a2")
    score_run_terminal(run.id)

    rows = _score_rows(db_session, run.id)
    cost_rows = [r for r in rows if r.dimension == "cost"]
    assert len(cost_rows) == 1
    assert cost_rows[0].score == 7
    assert "no baseline" in (cost_rows[0].rationale or "")


# ---------------------------------------------------------------------------
# Test 3 — agent run → plan_adherence=10 with N/A rationale
# ---------------------------------------------------------------------------

def test_agent_plan_adherence_na(db_session, db_engine):
    from backend.app.core.retro_scorer import score_run_terminal

    run = _make_success_run(db_session, "agent-plan-adherence-unique-a2", kind="agent")
    score_run_terminal(run.id)

    rows = _score_rows(db_session, run.id)
    pa_rows = [r for r in rows if r.dimension == "plan_adherence"]
    assert len(pa_rows) == 1
    assert pa_rows[0].score == 10
    assert "N/A" in (pa_rows[0].rationale or "")


# ---------------------------------------------------------------------------
# Test 4 — write to /tmp → scope_discipline=10
# ---------------------------------------------------------------------------

def test_scope_discipline_tmp_allowed(db_session, db_engine):
    from backend.app.core.retro_scorer import score_run_terminal

    run = _make_success_run(db_session, "scope-ok-agent-unique-a2")
    _add_tool_event(db_session, run.id, "write_file", path="/tmp/work/output.txt")
    score_run_terminal(run.id)

    rows = _score_rows(db_session, run.id)
    sd_rows = [r for r in rows if r.dimension == "scope_discipline"]
    assert len(sd_rows) == 1
    assert sd_rows[0].score == 10


# ---------------------------------------------------------------------------
# Test 5 — write to /etc/foo → scope_discipline ≤ 8
# ---------------------------------------------------------------------------

def test_scope_discipline_etc_offence(db_session, db_engine):
    from backend.app.core.retro_scorer import score_run_terminal

    run = _make_success_run(db_session, "scope-bad-agent-unique-a2")
    _add_tool_event(db_session, run.id, "write_file", path="/etc/passwd")
    score_run_terminal(run.id)

    rows = _score_rows(db_session, run.id)
    sd_rows = [r for r in rows if r.dimension == "scope_discipline"]
    assert len(sd_rows) == 1
    assert sd_rows[0].score <= 8
    evidence = sd_rows[0].evidence_json or {}
    assert evidence.get("total", 0) >= 1


# ---------------------------------------------------------------------------
# Test 6 — idempotency: call twice → exactly 7 rows (6 dims + overall)
# ---------------------------------------------------------------------------

def test_idempotency_seven_rows(db_session, db_engine):
    from backend.app.core.retro_scorer import score_run_terminal

    run = _make_success_run(db_session, "idempotent-agent-unique-a2")
    score_run_terminal(run.id)
    score_run_terminal(run.id)  # second call must replace, not duplicate

    rows = _score_rows(db_session, run.id)
    assert len(rows) == 7, f"expected 7 rows, got {len(rows)}: {[r.dimension for r in rows]}"
    dims = {r.dimension for r in rows}
    assert "overall" in dims
    assert "cost" in dims
    assert "wall" in dims
    assert "mistakes" in dims
    assert "lessons_applied" in dims
    assert "plan_adherence" in dims
    assert "scope_discipline" in dims


# ---------------------------------------------------------------------------
# Test 7 — Run.retro_score_summary populated after scoring
# ---------------------------------------------------------------------------

def test_retro_score_summary_populated(db_session, db_engine):
    from backend.app.core.retro_scorer import score_run_terminal
    from backend.app.models import Run

    run = _make_success_run(db_session, "summary-agent-unique-a2")
    assert run.retro_score_summary is None  # not yet scored

    score_run_terminal(run.id)

    db_session.expire_all()
    refreshed = db_session.get(Run, run.id)
    summary = refreshed.retro_score_summary
    assert summary is not None, "retro_score_summary should be set after scoring"
    assert "overall" in summary
    assert "dims" in summary
    assert "computed_at" in summary
    assert summary["n_scores"] == 7  # 6 dims + overall
    assert isinstance(summary["overall"], int)
    # Spot-check dims key set
    dims = set(summary["dims"].keys())
    assert dims == {"cost", "wall", "mistakes", "lessons_applied", "plan_adherence", "scope_discipline"}
    print("\nSample retro_score_summary:", summary)
