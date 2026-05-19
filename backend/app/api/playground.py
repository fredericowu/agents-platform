import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..core.events import bus
from ..core.executor import run_agent
from ..db import session_scope
from ..db import get_session
from ..models import Agent, Run
from ..schemas import PlaygroundIn, PlaygroundOut

router = APIRouter(prefix="/api/playground", tags=["playground"])


@router.post("/chat", response_model=PlaygroundOut)
async def playground_chat(body: PlaygroundIn, s: Session = Depends(get_session)):
    """Run an agent as part of a chat session.

    Pass ``extra.session_id`` to thread the conversation. Prior turns in the
    same session are fed back to the agent as the message history.
    """
    a = s.query(Agent).filter(Agent.slug == body.agent_slug).first()
    if not a:
        raise HTTPException(404, "agent not found")

    session_id = (body.extra or {}).get("session_id") or f"chat-{uuid.uuid4().hex[:8]}"

    # Build prior history from the same session's prior runs
    prior_runs = (s.query(Run)
                   .filter(Run.initiator_kind == "chat",
                           Run.initiator_id == session_id,
                           Run.status == "success")
                   .order_by(Run.started_at)
                   .all())
    extra_messages: list[dict] = []
    for r in prior_runs:
        user_text = (r.input or {}).get("input", "")
        ai_text   = (r.output or {}).get("text", "")
        if user_text:
            extra_messages.append({"role": "user", "content": user_text})
        if ai_text:
            extra_messages.append({"role": "assistant", "content": ai_text})

    # Create the new run row up-front so the client can subscribe immediately
    with session_scope() as ss:
        new_run = Run(kind="agent", target_slug=body.agent_slug, status="running",
                      input={"input": body.message},
                      initiator_kind="chat", initiator_id=session_id,
                      model_slug=a.model_slug)
        ss.add(new_run); ss.flush()
        rid = new_run.id

    async def _go():
        try:
            await run_agent(body.agent_slug, body.message,
                            run_id=rid,
                            initiator_kind="chat",
                            initiator_id=session_id,
                            extra_messages=extra_messages)
        finally:
            await bus.publish(rid, "done", {})
            await bus.close(rid)

    asyncio.create_task(_go())
    return PlaygroundOut(run_id=rid)


@router.get("/sessions/{session_id}/runs")
async def list_session_runs(session_id: str):
    """Return all runs that belong to a chat session, in order."""
    with session_scope() as s:
        runs = (s.query(Run)
                  .filter(Run.initiator_kind == "chat", Run.initiator_id == session_id)
                  .order_by(Run.started_at)
                  .all())
        return [{
            "id": r.id, "input": r.input, "output": r.output, "status": r.status,
            "tokens_in": r.tokens_in, "tokens_out": r.tokens_out,
            "model_slug": r.model_slug, "target_slug": r.target_slug,
            "started_at": r.started_at.isoformat(),
        } for r in runs]
