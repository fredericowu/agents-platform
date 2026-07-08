"""CLI Sessions — named claude --resume sessions with run stats."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import CliSession, PendingSessionCommand, Run
from ..schemas import CliSessionOut, CliSessionUpdate

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _enrich(s: Session, row: CliSession) -> dict:
    """Attach run stats to a CliSession row."""
    stats = (
        s.query(
            func.count(Run.id).label("run_count"),
            func.max(Run.started_at).label("last_run_at"),
        )
        .filter(Run.session_id == row.session_id)
        .one()
    )
    last_run = (
        s.query(Run.status)
        .filter(Run.session_id == row.session_id)
        .order_by(Run.started_at.desc())
        .first()
    )
    return {
        "id": row.id,
        "session_id": row.session_id,
        "name": row.name,
        "description": row.description,
        "run_count": stats.run_count or 0,
        "last_run_at": stats.last_run_at,
        "last_status": last_run.status if last_run else None,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


@router.get("", response_model=list[CliSessionOut])
def list_sessions(
    q: str | None = Query(None),
    source_slug: str | None = Query(None, description="Only sessions with at least one run from this agent/workflow slug"),
    limit: int = Query(100, ge=1, le=500),
    s: Session = Depends(get_session),
):
    qry = s.query(CliSession).order_by(CliSession.updated_at.desc())
    if source_slug:
        qry = (qry.join(Run, Run.session_id == CliSession.session_id)
                  .filter(Run.source_slug == source_slug)
                  .distinct())
    if q:
        like = f"%{q.lower()}%"
        from sqlalchemy import func as f, or_
        qry = qry.filter(or_(
            f.lower(CliSession.session_id).like(like),
            f.lower(CliSession.name).like(like),
            f.lower(CliSession.description).like(like),
        ))
    rows = qry.limit(limit).all()
    return [_enrich(s, r) for r in rows]


@router.get("/{session_id}", response_model=CliSessionOut)
def get_session_detail(session_id: str, s: Session = Depends(get_session)):
    row = s.query(CliSession).filter(CliSession.session_id == session_id).first()
    if not row:
        raise HTTPException(404, "Session not found")
    return _enrich(s, row)


@router.patch("/{session_id}", response_model=CliSessionOut)
def update_session(session_id: str, body: CliSessionUpdate, s: Session = Depends(get_session)):
    row = s.query(CliSession).filter(CliSession.session_id == session_id).first()
    if not row:
        raise HTTPException(404, "Session not found")
    if body.name is not None:
        row.name = body.name
    if body.description is not None:
        row.description = body.description
    s.commit()
    s.refresh(row)
    return _enrich(s, row)


@router.delete("/{session_id}")
def delete_session(session_id: str, s: Session = Depends(get_session)):
    row = s.query(CliSession).filter(CliSession.session_id == session_id).first()
    if not row:
        raise HTTPException(404, "Session not found")
    s.delete(row)
    s.commit()
    return {"deleted": session_id}


@router.post("/{session_id}/pending-command")
def set_pending_command(session_id: str, body: dict, s: Session = Depends(get_session)):
    """Queue a /clear or /compact for ``session_id``, run automatically right
    before its next resumed turn (see executor.run_agent), then cleared.
    Called by the clear_session / compact_session MCP tools."""
    command = (body.get("command") or "").strip().lower()
    if command not in ("clear", "compact"):
        raise HTTPException(400, "command must be 'clear' or 'compact'")
    row = s.query(PendingSessionCommand).filter(
        PendingSessionCommand.session_id == session_id).first()
    if row:
        row.command = command
    else:
        row = PendingSessionCommand(session_id=session_id, command=command)
        s.add(row)
    s.commit()
    return {"session_id": session_id, "command": command, "status": "queued"}


@router.get("/{session_id}/pending-command")
def get_pending_command(session_id: str, s: Session = Depends(get_session)):
    row = s.query(PendingSessionCommand).filter(
        PendingSessionCommand.session_id == session_id).first()
    return {"session_id": session_id, "command": row.command if row else None}
