"""Wave-6 Chunk A2 — auto-scorer for 6 retro dimensions.

Called by executor when a run reaches terminal status (success/error/cancelled).
All scoring errors are swallowed so a bug here can never break a run.
"""
from __future__ import annotations

import json
import logging
import re
import statistics
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ..db import session_scope
from ..models import (
    LessonApplication, Model, RetroScore, RetroScoreWeights,
    Run, RunEvent, Workflow,
)

logger = logging.getLogger(__name__)

_TERMINAL = frozenset({"success", "error", "cancelled"})


# ---------------------------------------------------------------------------
# Ratio buckets (shared by cost + wall dims)
# ---------------------------------------------------------------------------

def _score_ratio(ratio: float) -> int:
    if ratio <= 0.5:  return 10
    if ratio <= 0.75: return 9
    if ratio <= 1.0:  return 8
    if ratio <= 1.5:  return 6
    if ratio <= 2.0:  return 4
    if ratio <= 3.0:  return 2
    return 1


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------

def _score_cost(s: Session, run: Run) -> tuple[int, str, dict]:
    actual = run.cost_usd or 0.0
    cutoff = datetime.utcnow() - timedelta(days=30)
    rows = (s.query(Run)
            .filter(
                Run.target_slug == run.target_slug,
                Run.kind == run.kind,
                Run.status == "success",
                Run.id != run.id,
                Run.started_at >= cutoff,
            )
            .all())
    values = [r.cost_usd for r in rows if r.cost_usd is not None]
    if not values:
        return (7, "no baseline",
                {"baseline_usd": None, "actual_usd": actual, "ratio": None, "n_baseline_runs": 0})
    baseline = statistics.median(values)
    n = len(values)
    ratio = actual / baseline if baseline > 0 else 1.0
    score = _score_ratio(ratio)
    rationale = f"actual={actual:.4f} baseline={baseline:.4f} ratio={ratio:.2f}"
    return score, rationale, {
        "baseline_usd": round(baseline, 6),
        "actual_usd": actual,
        "ratio": round(ratio, 4),
        "n_baseline_runs": n,
    }


# ---------------------------------------------------------------------------
# wall
# ---------------------------------------------------------------------------

def _score_wall(s: Session, run: Run) -> tuple[int, str, dict]:
    if not run.started_at or not run.ended_at:
        return (7, "no wall time recorded",
                {"baseline_seconds": None, "actual_seconds": None, "ratio": None, "n_baseline_runs": 0})
    actual = (run.ended_at - run.started_at).total_seconds()
    cutoff = datetime.utcnow() - timedelta(days=30)
    rows = (s.query(Run)
            .filter(
                Run.target_slug == run.target_slug,
                Run.kind == run.kind,
                Run.status == "success",
                Run.id != run.id,
                Run.started_at >= cutoff,
                Run.ended_at.isnot(None),
            )
            .all())
    walls = [
        (r.ended_at - r.started_at).total_seconds()
        for r in rows if r.started_at and r.ended_at
    ]
    if not walls:
        return (7, "no baseline",
                {"baseline_seconds": None, "actual_seconds": actual, "ratio": None, "n_baseline_runs": 0})
    baseline = statistics.median(walls)
    n = len(walls)
    ratio = actual / baseline if baseline > 0 else 1.0
    score = _score_ratio(ratio)
    rationale = f"actual={actual:.1f}s baseline={baseline:.1f}s ratio={ratio:.2f}"
    return score, rationale, {
        "baseline_seconds": round(baseline, 2),
        "actual_seconds": round(actual, 2),
        "ratio": round(ratio, 4),
        "n_baseline_runs": n,
    }


# ---------------------------------------------------------------------------
# mistakes
# ---------------------------------------------------------------------------

def _score_mistakes(s: Session, run: Run) -> tuple[int, str, dict]:
    events = (s.query(RunEvent)
              .filter(RunEvent.run_id == run.id)
              .order_by(RunEvent.ts)
              .all())

    error_events = sum(1 for e in events if e.kind == "error")
    failed_tool_calls = sum(
        1 for e in events
        if e.kind == "tool_call" and (e.payload or {}).get("outcome") == "error"
    )

    # Thrash: write/edit → delete → rewrite on same path within 60 s
    writes: list[tuple[str, datetime]] = []   # (path, ts)
    deletes: list[tuple[str, datetime]] = []
    thrash_pairs = 0

    for e in events:
        if e.kind != "tool_call":
            continue
        payload = e.payload or {}
        tool = (payload.get("tool") or payload.get("name") or "").lower()
        args: Any = payload.get("args") or payload.get("input") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        path = (
            args.get("path") or args.get("file_path")
            or payload.get("path") or payload.get("file_path") or ""
        )
        if tool in ("write_file", "edit_file") and path:
            writes.append((path, e.ts))
        elif tool in ("delete_file", "rm") and path:
            deletes.append((path, e.ts))

    for path_d, ts_d in deletes:
        prior = any(
            p == path_d and (ts_d - ts_w).total_seconds() <= 60
            for p, ts_w in writes if ts_w <= ts_d
        )
        rewrite = any(
            p == path_d and 0 < (ts_rw - ts_d).total_seconds() <= 60
            for p, ts_rw in writes if ts_rw > ts_d
        )
        if prior and rewrite:
            thrash_pairs += 1

    total = error_events + failed_tool_calls + thrash_pairs
    if total == 0:   score = 10
    elif total <= 2: score = 8
    elif total <= 5: score = 6
    elif total <= 10: score = 4
    else:            score = 2

    rationale = (
        f"{total} mistake(s): {error_events} errors, "
        f"{failed_tool_calls} failed tools, {thrash_pairs} thrash pairs"
    )
    return score, rationale, {
        "error_events": error_events,
        "failed_tool_calls": failed_tool_calls,
        "thrash_pairs": thrash_pairs,
    }


# ---------------------------------------------------------------------------
# lessons_applied
# ---------------------------------------------------------------------------

def _score_lessons_applied(s: Session, run: Run) -> tuple[int, str, dict]:
    _empty = {"applied": 0, "prevented": 0, "ignored": 0, "partial": 0, "rejected": 0, "total_shown": 0}

    if not run.target_id:
        return 7, "no lessons", _empty

    apps = (s.query(LessonApplication)
            .filter(
                LessonApplication.target_id == run.target_id,
                LessonApplication.applied_in_run_id == run.id,
            )
            .all())

    if not apps:
        shown = (s.query(LessonApplication)
                 .filter(
                     LessonApplication.target_id == run.target_id,
                     LessonApplication.outcome == "shown_to_pm",
                 )
                 .count())
        if shown > 0:
            return 5, "lessons shown but none recorded as applied", {**_empty, "total_shown": shown}
        return 7, "no lessons", _empty

    by_outcome: dict[str, int] = {}
    for a in apps:
        by_outcome[a.outcome] = by_outcome.get(a.outcome, 0) + 1

    applied   = by_outcome.get("applied",    0)
    prevented = by_outcome.get("prevented",  0)
    ignored   = by_outcome.get("ignored",    0)
    partial   = by_outcome.get("partial",    0)
    rejected  = by_outcome.get("rejected",   0)
    total_shown = sum(by_outcome.values())

    score = 7 + (applied + prevented) - 3 * ignored
    score = max(1, min(10, score))
    rationale = f"applied={applied} prevented={prevented} ignored={ignored} partial={partial}"
    return score, rationale, {
        "applied": applied,
        "prevented": prevented,
        "ignored": ignored,
        "partial": partial,
        "rejected": rejected,
        "total_shown": total_shown,
    }


# ---------------------------------------------------------------------------
# plan_adherence
# ---------------------------------------------------------------------------

def _score_plan_adherence(s: Session, run: Run) -> tuple[int, str, dict]:
    if run.kind != "workflow":
        return 10, "N/A — not a workflow run", {"kind": "agent"}

    wf = s.query(Workflow).filter(Workflow.slug == run.target_slug).first()
    expected_order: list[str] = []
    if wf and wf.graph:
        nodes = wf.graph.get("nodes") or []
        expected_order = [
            n.get("id") or n.get("label") or str(i)
            for i, n in enumerate(nodes)
        ]

    node_start_events = (s.query(RunEvent)
                         .filter(RunEvent.run_id == run.id, RunEvent.kind == "node_start")
                         .order_by(RunEvent.ts)
                         .all())
    actual_order: list[str] = []
    seen: set[str] = set()
    for e in node_start_events:
        nid = e.node_id or (e.payload or {}).get("agent") or ""
        if nid and nid not in seen and nid not in ("__workflow__",):
            actual_order.append(nid)
            seen.add(nid)

    if not expected_order and not actual_order:
        return 10, "no nodes to compare", {
            "expected_order": [], "actual_order": [], "match_ratio": 1.0,
        }

    max_len = max(len(expected_order), len(actual_order), 1)
    matches = sum(1 for a, b in zip(expected_order, actual_order) if a == b)
    ratio = matches / max_len

    if ratio >= 0.95:  score = 10
    elif ratio >= 0.85: score = 8
    elif ratio >= 0.75: score = 6
    elif ratio >= 0.5:  score = 4
    else:               score = 2

    rationale = f"match_ratio={ratio:.2f} ({matches}/{max_len})"
    return score, rationale, {
        "expected_order": expected_order,
        "actual_order": actual_order,
        "match_ratio": round(ratio, 4),
    }


# ---------------------------------------------------------------------------
# scope_discipline
# ---------------------------------------------------------------------------

def _allowed_roots(s: Session, run: Run) -> list[str]:
    roots = ["/tmp"]
    if not run.model_slug:
        return roots
    m = s.query(Model).filter(Model.slug == run.model_slug).first()
    if not m:
        return roots
    params = m.params or {}
    cwd = params.get("cwd")
    if cwd:
        roots.append(cwd)
    add_dirs = params.get("add_dirs") or []
    if isinstance(add_dirs, list):
        roots.extend(add_dirs)
    elif isinstance(add_dirs, str):
        roots.append(add_dirs)
    return roots


def _path_is_ok(path: str, roots: list[str]) -> bool:
    if not path or not path.startswith("/"):
        return True
    return any(
        path == r or path.startswith(r.rstrip("/") + "/")
        for r in roots
    )


def _paths_in_cmd(cmd: str) -> list[str]:
    return re.findall(r'/[^\s;|&<>\'\"\\]+', cmd)


def _score_scope_discipline(s: Session, run: Run) -> tuple[int, str, dict]:
    roots = _allowed_roots(s, run)
    events = (s.query(RunEvent)
              .filter(RunEvent.run_id == run.id, RunEvent.kind == "tool_call")
              .all())

    offences: list[dict] = []
    for e in events:
        payload = e.payload or {}
        tool = (payload.get("tool") or payload.get("name") or "").lower()
        args: Any = payload.get("args") or payload.get("input") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}

        if tool in ("write_file", "edit_file"):
            path = (
                args.get("path") or args.get("file_path")
                or payload.get("path") or payload.get("file_path") or ""
            )
            if path and not _path_is_ok(path, roots):
                offences.append({"tool": tool, "path": path, "allowed_roots": roots})

        elif tool == "run_command":
            cmd = (
                args.get("cmd") or args.get("command")
                or payload.get("cmd") or payload.get("command") or ""
            )
            if cmd:
                for p in _paths_in_cmd(cmd):
                    if not _path_is_ok(p, roots):
                        offences.append({"tool": tool, "path": p, "allowed_roots": roots})
                        break  # one offence per command

    score = max(1, 10 - 2 * len(offences))
    rationale = (
        f"{len(offences)} scope offence(s)" if offences
        else "all paths within allowed roots"
    )
    return score, rationale, {"offences": offences, "total": len(offences)}


# ---------------------------------------------------------------------------
# Public compute entry point
# ---------------------------------------------------------------------------

def compute_auto_scores(s: Session, run: Run) -> dict[str, tuple[int, str, dict]]:
    """Return {dim: (score 1-10, rationale, evidence_json)} for 6 auto dims."""
    scores: dict[str, tuple[int, str, dict]] = {}
    for dim, fn in [
        ("cost",             _score_cost),
        ("wall",             _score_wall),
        ("mistakes",         _score_mistakes),
        ("lessons_applied",  _score_lessons_applied),
        ("plan_adherence",   _score_plan_adherence),
        ("scope_discipline", _score_scope_discipline),
    ]:
        try:
            scores[dim] = fn(s, run)
        except Exception:
            logger.exception("retro_scorer: failed dim=%s run=%s", dim, run.id)
    return scores


# ---------------------------------------------------------------------------
# Weighted mean helper
# ---------------------------------------------------------------------------

def _weighted_mean(scores: dict[str, tuple[int, str, dict]], weights: dict[str, float]) -> int:
    total_w = 0.0
    weighted_sum = 0.0
    for dim, (score, _, _) in scores.items():
        w = weights.get(dim, 0.0)
        if w > 0:
            weighted_sum += score * w
            total_w += w
    if total_w == 0:
        vals = [t[0] for t in scores.values()]
        return round(sum(vals) / len(vals)) if vals else 7
    return round(weighted_sum / total_w)


# ---------------------------------------------------------------------------
# Public terminal-status hook
# ---------------------------------------------------------------------------

def score_run_terminal(run_id: str) -> None:
    """Called by executor when run hits terminal status. Idempotent."""
    try:
        with session_scope() as s:
            run = s.get(Run, run_id)
            if not run or run.status not in _TERMINAL:
                return

            # Delete any existing auto rows (idempotency)
            (s.query(RetroScore)
             .filter(RetroScore.run_id == run_id, RetroScore.source == "auto")
             .delete(synchronize_session=False))

            scores = compute_auto_scores(s, run)
            for dim, (score, rationale, evidence) in scores.items():
                s.add(RetroScore(
                    run_id=run_id, dimension=dim, score=score,
                    source="auto", rationale=rationale, evidence_json=evidence,
                ))

            weights_row = s.get(RetroScoreWeights, 1)
            weights = weights_row.weights_json if weights_row else {}
            overall = _weighted_mean(scores, weights)
            s.add(RetroScore(
                run_id=run_id, dimension="overall", score=overall, source="auto",
            ))

            run.retro_score_summary = {
                "overall": overall,
                "dims": {dim: t[0] for dim, t in scores.items()},
                "computed_at": datetime.utcnow().isoformat(),
                "n_scores": len(scores) + 1,
            }
    except Exception:
        logger.exception("retro_scorer: score_run_terminal failed run_id=%s", run_id)
