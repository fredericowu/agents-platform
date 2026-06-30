import asyncio
import base64
import hashlib
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from ..core.events import bus, sse_format
from ..db import get_session
from ..models import Run, RunArtefact, RunEvent
from ..schemas import RunArtefactFull, RunArtefactIn, RunArtefactOut, RunEventOut, RunOut

router = APIRouter(prefix="/api/runs", tags=["runs"])


# Cap how much of `input` we echo back when summary=true is requested.
_INPUT_SUMMARY_CHARS = 200


def _maybe_truncate(r: Run, summary: bool) -> Run:
    """Replace ``input`` with a small preview when summary=true is requested.
    The full input remains on the row; this only mutates the dict the response
    sees. Caller MUST pass a transient/copied Run, NOT a session-attached one,
    OR call s.expunge(r) first."""
    if not summary or not r.input:
        return r
    # The Run is session-attached; mutating .input would write to DB on commit.
    # Build a shallow-clone view via a __dict__ override on the response model.
    # Easiest: rewrite to dict + length-cap.
    preview: dict[str, Any] = {}
    for k, v in (r.input or {}).items():
        if isinstance(v, str) and len(v) > _INPUT_SUMMARY_CHARS:
            preview[k] = v[:_INPUT_SUMMARY_CHARS] + f"… [+{len(v)-_INPUT_SUMMARY_CHARS} chars]"
        else:
            preview[k] = v
    # SQLAlchemy will see r.input as dirty; expunge to keep it transient
    # (RunOut serialises from attributes, so set on the instance works).
    r.__dict__["input"] = preview
    return r


@router.get("", response_model=list[RunOut])
def list_runs(limit: int = Query(50, ge=1, le=500),
              kind: str | None = None,
              status: str | None = None,
              roots_only: bool = False,
              target_id: str | None = Query(None, description="Filter by Target id"),
              target_slug: str | None = Query(None, description="Filter by Target slug (resolved server-side)"),
              q: str | None = None,
              summary: bool = Query(False, description="Truncate large `input` fields to ~200 chars"),
              s: Session = Depends(get_session)):
    qry = s.query(Run).order_by(Run.started_at.desc())
    if kind:
        qry = qry.filter(Run.kind == kind)
    if status:
        qry = qry.filter(Run.status == status)
    if roots_only:
        qry = qry.filter(Run.parent_run_id.is_(None))
    if target_id:
        qry = qry.filter(Run.target_id == target_id)
    if target_slug:
        from ..models import Target as _T
        t = s.query(_T).filter(_T.slug == target_slug).first()
        if t is None:
            return []
        qry = qry.filter(Run.target_id == t.id)
    if q:
        like = f"%{q.lower()}%"
        from sqlalchemy import func, or_
        qry = qry.filter(or_(
            func.lower(Run.target_slug).like(like),
            func.lower(Run.initiator_kind).like(like),
            func.lower(Run.initiator_id).like(like),
            func.lower(Run.id).like(like),
        ))
    rows = qry.limit(limit).all()
    if summary:
        for r in rows:
            _maybe_truncate(r, True)
    return rows


@router.get("/{run_id}", response_model=RunOut)
def get_run(run_id: str, summary: bool = Query(False),
            s: Session = Depends(get_session)):
    r = s.query(Run).filter(Run.id == run_id).first()
    if not r:
        raise HTTPException(404, "not found")
    if summary:
        _maybe_truncate(r, True)
    return r


@router.get("/{run_id}/events", response_model=list[RunEventOut])
def get_events(run_id: str,
               after_ts: datetime | None = Query(None,
                   description="Only return events with ts > this value (ISO8601). "
                               "Cursor for tailing without re-pulling history."),
               kinds: str | None = Query(None,
                   description="Comma-separated list of event kinds to keep "
                               "(e.g. 'node_start,error,done'). Omit to keep all."),
               limit: int = Query(10000, ge=1, le=50000),
               s: Session = Depends(get_session)):
    qry = s.query(RunEvent).filter(RunEvent.run_id == run_id)
    if after_ts is not None:
        qry = qry.filter(RunEvent.ts > after_ts)
    if kinds:
        wanted = {k.strip() for k in kinds.split(",") if k.strip()}
        if wanted:
            qry = qry.filter(RunEvent.kind.in_(wanted))
    return qry.order_by(RunEvent.ts).limit(limit).all()


# -----------------------------------------------------------------------------
# wait_run — block until terminal status (or timeout). Kills the polling pattern.
# -----------------------------------------------------------------------------

@router.get("/{run_id}/wait", response_model=RunOut)
async def wait_run(run_id: str,
                   timeout_s: int = Query(300, ge=1, le=3600,
                       description="Hard cap on wait. Returns the row as-is on timeout."),
                   poll_interval_s: float = Query(2.0, ge=0.25, le=30,
                       description="Internal poll cadence on the DB row."),
                   max_cost_usd: float | None = Query(None, ge=0,
                       description="Cap on this run's accumulated cost. If exceeded mid-wait, "
                                   "the run (and its descendants) is cancelled and the snapshot returned."),
                   summary: bool = Query(False)):
    """Block until the run reaches a terminal status (success|error|cancelled)
    or until ``timeout_s`` elapses. Returns the full RunOut snapshot.

    Internally polls the DB row at ``poll_interval_s`` cadence. Caller does NOT
    poll. This eliminates the polling pattern that was burning context/tokens
    on the conductor side.

    If ``max_cost_usd`` is set and the rolled-up cost exceeds it mid-flight,
    the run is cancelled (cascading to descendants) and the snapshot returned
    with ``status='cancelled'``.

    On timeout: returns the row in its current (still-running) state without
    raising. Caller should check ``status`` to detect timeout vs. terminal.
    """
    from ..db import session_scope
    deadline = asyncio.get_event_loop().time() + timeout_s
    terminal = {"success", "error", "cancelled"}
    while True:
        with session_scope() as s:
            r = s.query(Run).filter(Run.id == run_id).first()
            if r is None:
                raise HTTPException(404, "not found")
            status = r.status

            # Roll up cost across descendants for accurate cap enforcement.
            cost_total = float(r.cost_usd or 0.0)
            if r.kind == "workflow":
                # Single shallow walk — children's cost_usd is itself rolled up
                # by the workflow status-flip code.
                for c in s.query(Run).filter(Run.parent_run_id == run_id).all():
                    cost_total += float(c.cost_usd or 0.0)

            cost_exceeded = max_cost_usd is not None and cost_total > max_cost_usd

            if status in terminal or asyncio.get_event_loop().time() >= deadline:
                if summary:
                    _maybe_truncate(r, True)
                s.expunge(r)
                return r

        if cost_exceeded:
            # Cancel the run + descendants and return the cancelled snapshot.
            from datetime import datetime as _dt
            from ..core.cancel import mark_cancelled
            from ..core.events import bus
            from ..core.models.cli import kill_run

            with session_scope() as s2:
                root = s2.query(Run).filter(Run.id == run_id).first()
                ids: list[str] = []
                def _cancel(run):
                    if run.status in terminal:
                        return
                    run.status = "cancelled"
                    run.error = (run.error or "") + " [cost cap exceeded]"
                    run.ended_at = _dt.utcnow()
                    ids.append(run.id)
                    for c in s2.query(Run).filter(Run.parent_run_id == run.id).all():
                        _cancel(c)
                if root:
                    _cancel(root)
                s2.commit()
            mark_cancelled(*ids)
            if kill_run:
                for rid in ids:
                    try:
                        await kill_run(rid)
                    except Exception:
                        pass
            await bus.publish(run_id, "error", {"error": "cost cap exceeded"})
            await bus.publish(run_id, "done", {"status": "cancelled", "reason": "cost_cap"})
            # Re-read final state
            with session_scope() as s3:
                r3 = s3.query(Run).filter(Run.id == run_id).first()
                if summary and r3:
                    _maybe_truncate(r3, True)
                if r3:
                    s3.expunge(r3)
                return r3

        await asyncio.sleep(poll_interval_s)


# -----------------------------------------------------------------------------
# peek_run_output — mid-flight visibility without waiting for terminal status
# -----------------------------------------------------------------------------

@router.get("/{run_id}/peek")
def peek_run_output(run_id: str, tail_events: int = Query(20, ge=1, le=200),
                    s: Session = Depends(get_session)):
    """Return a snapshot of what's accumulated for a running run so far —
    output buffer, last N events, current status, last event ts. Lets the
    conductor catch a misbehaving agent BEFORE waiting for terminal."""
    r = s.query(Run).filter(Run.id == run_id).first()
    if not r:
        raise HTTPException(404, "not found")
    evts = (s.query(RunEvent)
              .filter(RunEvent.run_id == run_id)
              .order_by(RunEvent.ts.desc())
              .limit(tail_events).all())
    last_evts = list(reversed(evts))  # chronological
    # Best-effort: concat assistant text deltas if any are streamed
    text_deltas: list[str] = []
    for e in last_evts:
        if e.kind == "llm_token" and isinstance(e.payload, dict):
            d = e.payload.get("delta")
            if isinstance(d, str):
                text_deltas.append(d)
    return {
        "run_id": r.id,
        "status": r.status,
        "started_at": r.started_at.isoformat(),
        "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        "last_event_at": last_evts[-1].ts.isoformat() if last_evts else None,
        "events_total": s.query(RunEvent).filter(RunEvent.run_id == run_id).count(),
        "tail_events": [
            {"ts": e.ts.isoformat(), "kind": e.kind, "node_id": e.node_id, "payload": e.payload}
            for e in last_evts
        ],
        "accumulated_text_preview": "".join(text_deltas)[-4000:],  # last 4KB of streamed deltas
        "output": r.output,  # may be None if still running
        "tokens_in": r.tokens_in, "tokens_out": r.tokens_out, "cost_usd": r.cost_usd,
    }


# -----------------------------------------------------------------------------
# run_agents_parallel — ephemeral fan-out without polluting `workflows` table
# -----------------------------------------------------------------------------

class _ParallelAgentSpec(BaseModel):
    slug: str
    input: str = ""
    target_id: str | None = None
    node_id: str | None = None


class _ParallelDispatch(BaseModel):
    agents: list[_ParallelAgentSpec]
    target_id: str | None = None
    target_slug: str | None = None
    max_hops: int | None = None
    max_tokens: int | None = None


@router.post("/parallel")
async def run_agents_parallel(body: _ParallelDispatch, s: Session = Depends(get_session)):
    """Dispatch N agent runs in parallel under a SYNTHETIC parent run.
    No row is written to ``workflows`` — the parent run carries kind='workflow'
    target_slug='_ephemeral_parallel_' and the children are linked via parent_run_id.

    Returns the parent run id + child run ids so the conductor can wait on
    the parent or roll up costs via ``run_tree``.

    Resolves ``target_slug`` → ``target_id`` once for the parent so the whole
    tree shares the same Target.
    """
    from datetime import datetime as _dt
    from ..core.executor import start_agent_run_bg
    from ..models import Agent, Target

    if not body.agents:
        raise HTTPException(400, "agents[] must be non-empty")
    if len(body.agents) > 20:
        raise HTTPException(400, "max 20 parallel agents per dispatch")

    # Resolve target — fall back to "agents-platform" default if not supplied
    target_id = body.target_id
    if target_id is None and body.target_slug:
        t = s.query(Target).filter(Target.slug == body.target_slug).first()
        if t is None:
            raise HTTPException(404, f"target slug '{body.target_slug}' not found")
        target_id = t.id
    if target_id is None:
        default = s.query(Target).filter(Target.slug == "agents-platform").first()
        target_id = default.id if default else None
    if target_id is None:
        raise HTTPException(400, "target_slug is required for parallel dispatch")

    # Validate every agent slug up front
    for spec in body.agents:
        a = s.query(Agent).filter(Agent.slug == spec.slug, Agent.deleted_at.is_(None)).first()
        if a is None:
            raise HTTPException(404, f"agent slug '{spec.slug}' not found or deleted")

    # Pre-flight: target budget check (raises 429 if exceeded)
    from ..core.executor import TargetBudgetExceeded, _check_target_budget
    try:
        _check_target_budget(target_id)
    except TargetBudgetExceeded as e:
        raise HTTPException(429, f"target budget exceeded: {e}")

    # Create synthetic parent run
    parent = Run(
        kind="workflow", target_slug="_ephemeral_parallel_",
        status="running", input={"input": "parallel-dispatch",
                                  "child_specs": [a.model_dump() for a in body.agents]},
        initiator_kind="mcp", initiator_id="run_agents_parallel",
        target_id=target_id, model_slug=None,
    )
    s.add(parent); s.commit(); s.refresh(parent)
    parent_id = parent.id

    # Fan out children
    child_ids: list[str] = []
    for spec in body.agents:
        try:
            cid = start_agent_run_bg(spec.slug, spec.input,
                                     parent_run_id=parent_id,
                                     node_id=spec.node_id or spec.slug,
                                     target_id=target_id)
            child_ids.append(cid)
        except TargetBudgetExceeded as e:
            # Mark parent as cancelled, return what was dispatched
            parent.status = "cancelled"
            parent.error = f"target budget exceeded mid-fanout: {e}"
            from datetime import datetime as _dt
            parent.ended_at = _dt.utcnow()
            s.commit()
            return {"parent_run_id": parent_id, "child_run_ids": child_ids,
                    "target_id": target_id, "kind": "ephemeral_parallel",
                    "partial": True, "error": str(e)}

    # Background watcher — flips the parent terminal when every child is terminal.
    # Without this the parent stays at "running" forever and `wait_run` can't unblock.
    async def _watch_children(parent_run_id: str, kids: list[str]):
        from datetime import datetime as _dt
        from ..core.events import bus
        from ..db import session_scope as _ss
        terminal = {"success", "error", "cancelled"}
        # Poll every 1s; cap at 60 min just to avoid leaks.
        for _ in range(3600):
            await asyncio.sleep(1)
            with _ss() as s2:
                rows = s2.query(Run).filter(Run.id.in_(kids)).all()
                if len(rows) < len(kids):
                    continue
                statuses = [r.status for r in rows]
                if not all(st in terminal for st in statuses):
                    continue
                # All terminal — roll up + flip parent.
                tot_in = sum(r.tokens_in or 0 for r in rows)
                tot_out = sum(r.tokens_out or 0 for r in rows)
                tot_cost = sum(r.cost_usd or 0.0 for r in rows)
                # Parent status = worst of children: error > cancelled > success
                if any(st == "error" for st in statuses):
                    p_status = "error"
                elif any(st == "cancelled" for st in statuses):
                    p_status = "cancelled"
                else:
                    p_status = "success"
                p = s2.query(Run).filter(Run.id == parent_run_id).first()
                if p and p.status not in terminal:
                    p.status = p_status
                    p.tokens_in = tot_in
                    p.tokens_out = tot_out
                    p.cost_usd = tot_cost
                    p.ended_at = _dt.utcnow()
                    p.output = {"children": [{"id": r.id, "status": r.status,
                                              "tokens_in": r.tokens_in,
                                              "tokens_out": r.tokens_out,
                                              "cost_usd": r.cost_usd}
                                             for r in rows]}
                    s2.commit()
            await bus.publish(parent_run_id, "done",
                              {"status": p_status, "children": len(kids)})
            await bus.close(parent_run_id)
            return

    asyncio.create_task(_watch_children(parent_id, list(child_ids)))

    return {
        "parent_run_id": parent_id,
        "child_run_ids": child_ids,
        "target_id": target_id,
        "kind": "ephemeral_parallel",
    }


# -----------------------------------------------------------------------------
# Run artefacts — structured outputs attached to a run
# -----------------------------------------------------------------------------

def _content_size_sha(content: str, is_binary: bool) -> tuple[int, str]:
    raw = base64.b64decode(content) if is_binary else content.encode("utf-8")
    return len(raw), hashlib.sha256(raw).hexdigest()


@router.post("/{run_id}/artefacts", response_model=RunArtefactOut)
def add_run_artefact(run_id: str, body: RunArtefactIn,
                     s: Session = Depends(get_session)):
    """Attach a named artefact to a run. Replaces by name if it already exists."""
    r = s.query(Run).filter(Run.id == run_id).first()
    if not r:
        raise HTTPException(404, "run not found")
    size, sha = _content_size_sha(body.content, body.is_binary)
    existing = (s.query(RunArtefact)
                  .filter(RunArtefact.run_id == run_id, RunArtefact.name == body.name)
                  .first())
    if existing is not None:
        existing.mime = body.mime
        existing.content = body.content
        existing.is_binary = body.is_binary
        existing.size = size
        existing.sha = sha
        s.commit(); s.refresh(existing)
        return existing
    art = RunArtefact(run_id=run_id, name=body.name, mime=body.mime,
                     content=body.content, is_binary=body.is_binary,
                     size=size, sha=sha)
    s.add(art); s.commit(); s.refresh(art)
    return art


@router.get("/{run_id}/artefacts", response_model=list[RunArtefactOut])
def list_run_artefacts(run_id: str, s: Session = Depends(get_session)):
    return (s.query(RunArtefact)
              .filter(RunArtefact.run_id == run_id)
              .order_by(RunArtefact.created_at).all())


@router.get("/{run_id}/artefacts/{name}", response_model=RunArtefactFull)
def get_run_artefact(run_id: str, name: str, s: Session = Depends(get_session)):
    a = (s.query(RunArtefact)
           .filter(RunArtefact.run_id == run_id, RunArtefact.name == name)
           .first())
    if not a:
        raise HTTPException(404, "artefact not found")
    return a


@router.delete("/{run_id}/artefacts/{name}")
def delete_run_artefact(run_id: str, name: str, s: Session = Depends(get_session)):
    a = (s.query(RunArtefact)
           .filter(RunArtefact.run_id == run_id, RunArtefact.name == name)
           .first())
    if not a:
        raise HTTPException(404, "artefact not found")
    s.delete(a); s.commit()
    return {"deleted": name}


@router.get("/{run_id}/tree")
def get_tree(run_id: str, s: Session = Depends(get_session)):
    """Return the full run lineage tree for a given run id:
        root -> direct children -> grandchildren …
    Plus rollup totals and parent info."""
    me = s.query(Run).filter(Run.id == run_id).first()
    if not me:
        raise HTTPException(404, "not found")

    # walk up to root
    chain_up: list[Run] = []
    cur = me
    seen = set()
    while cur.parent_run_id and cur.parent_run_id not in seen:
        seen.add(cur.parent_run_id)
        p = s.query(Run).filter(Run.id == cur.parent_run_id).first()
        if p is None:
            break
        chain_up.append(p)
        cur = p
    root = chain_up[-1] if chain_up else me

    # BFS from root
    def serialize(r: Run) -> dict:
        return {
            "id": r.id, "kind": r.kind, "target_slug": r.target_slug,
            "status": r.status, "tokens_in": r.tokens_in, "tokens_out": r.tokens_out,
            "cost_usd": r.cost_usd, "started_at": r.started_at.isoformat(),
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            "parent_run_id": r.parent_run_id,
            "initiator_kind": r.initiator_kind, "initiator_id": r.initiator_id,
            "node_id": r.node_id, "model_slug": r.model_slug,
        }

    def collect(root_r: Run) -> dict:
        node = serialize(root_r)
        children = s.query(Run).filter(Run.parent_run_id == root_r.id).order_by(Run.started_at).all()
        node["children"] = [collect(c) for c in children]
        return node

    tree = collect(root)
    # totals (rollup over the whole tree)
    def fold(n, acc):
        acc["runs"] += 1
        acc["tokens_in"] += n["tokens_in"]
        acc["tokens_out"] += n["tokens_out"]
        acc["cost_usd"] += n["cost_usd"]
        models = acc.setdefault("models", {})
        if n["model_slug"]:
            models[n["model_slug"]] = models.get(n["model_slug"], 0) + 1
        for c in n["children"]:
            fold(c, acc)
        return acc

    totals = fold(tree, {"runs": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0})
    return {"root": tree, "current_id": run_id, "totals": totals}


@router.post("/cancel_all")
async def cancel_all(s: Session = Depends(get_session)):
    """Cancel every running run on the system. Targets root-level running
    runs only (children are cancelled via cascade)."""
    from datetime import datetime
    from ..core.cancel import mark_cancelled
    from ..core.events import bus
    from ..core.models.cli import kill_run

    # Roots first — cancelling them cascades to descendants via the orchestrator.
    roots = (s.query(Run)
              .filter(Run.status == "running", Run.parent_run_id.is_(None))
              .all())
    # Also collect any orphan running children (parent not running for some reason)
    orphans = (s.query(Run)
                 .filter(Run.status == "running", Run.parent_run_id.isnot(None))
                 .all())

    marked_ids: list[str] = []
    def _cancel(run):
        if run.status in ("success", "error", "cancelled"):
            return
        run.status = "cancelled"
        run.ended_at = datetime.utcnow()
        marked_ids.append(run.id)
        for c in s.query(Run).filter(Run.parent_run_id == run.id).all():
            _cancel(c)
    for r in roots + orphans:
        _cancel(r)
    s.commit()
    mark_cancelled(*marked_ids)

    killed = 0
    for rid in marked_ids:
        killed += await kill_run(rid)

    for rid in marked_ids:
        await bus.publish(rid, "error", {"error": "cancelled by user (cancel-all)"})
        await bus.publish(rid, "done", {"status": "cancelled"})

    return {"cancelled_roots": len(roots), "cancelled_total": len(marked_ids),
            "subprocesses_killed": killed}


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: str, s: Session = Depends(get_session)):
    """Cancel a running run + every descendant. Sends SIGTERM to any live
    claude-CLI subprocess we registered for the run id(s), and marks the rows
    as ``cancelled``."""
    r = s.query(Run).filter(Run.id == run_id).first()
    if not r:
        raise HTTPException(404, "not found")
    from datetime import datetime
    from ..core.events import bus
    from ..core.models.cli import kill_run

    from ..core.cancel import mark_cancelled

    ids: list[str] = []
    def _cancel(run):
        if run.status in ("success", "error", "cancelled"):
            return
        run.status = "cancelled"
        run.ended_at = datetime.utcnow()
        ids.append(run.id)
        for c in s.query(Run).filter(Run.parent_run_id == run.id).all():
            _cancel(c)
    _cancel(r)
    s.commit()

    # Mark BOTH the cancelled run ids AND the root id so any newly-spawned
    # children that inherit the workflow run id as their parent are stopped
    # before they spin up a subprocess.
    mark_cancelled(run_id, *ids)

    killed = 0
    for rid in ids:
        killed += await kill_run(rid)

    await bus.publish(run_id, "error", {"error": "cancelled by user"})
    await bus.publish(run_id, "done", {"status": "cancelled"})
    return {"cancelled": run_id, "marked": len(ids), "subprocesses_killed": killed}


@router.get("/{run_id}/stream")
async def stream_run(run_id: str):
    import json as _json

    async def gen():
        # 1. replay any events already in the DB
        with __import__("backend.app.db", fromlist=["session_scope"]).session_scope() as s:
            past = (s.query(RunEvent)
                      .filter(RunEvent.run_id == run_id)
                      .order_by(RunEvent.ts).all())
            past_snap = [{"kind": e.kind, "node_id": e.node_id, "payload": e.payload,
                          "ts": e.ts.isoformat(), "run_id": e.run_id} for e in past]
        for evt in past_snap:
            yield {"event": evt.get("kind", "log"), "data": _json.dumps(evt, default=str)}

        # 2. if run already finished, close
        with __import__("backend.app.db", fromlist=["session_scope"]).session_scope() as s:
            r = s.query(Run).filter(Run.id == run_id).first()
            done = r is not None and r.status in ("success", "error", "cancelled")
        if done:
            yield {"event": "done", "data": _json.dumps({"kind": "done", "run_id": run_id, "done": True})}
            return

        # 3. stream live events from the bus
        async for evt in bus.subscribe(run_id):
            yield {"event": evt.get("kind", "log"), "data": _json.dumps(evt, default=str)}
    return EventSourceResponse(gen())
