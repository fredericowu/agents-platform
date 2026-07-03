"""OpenAI-compatible HTTP surface for the Agents Platform.

This is the *idea* ported from Agentic Workspace's old ``llm_proxy`` (which
wrapped raw CLIs): expose a standard OpenAI ``/v1`` API so ANY OpenAI client
— LangChain/LangGraph, OpenClaw, the OpenAI SDK, curl — can drive the
platform's own **agents and workflows** as if they were chat models.

Endpoints (mounted at the app root, NOT under ``/api``):
  GET  /v1/models             list agents + workflows as OpenAI "models"
  GET  /v1/models/{id}        describe one
  POST /v1/chat/completions   run an agent/workflow (streaming + non-streaming)

Model-name grammar accepted by ``/v1/chat/completions``:
  agent/<slug>       → run the agent <slug>
  workflow/<slug>    → run the workflow <slug>
  <slug>             → bare slug is treated as an agent

Unlike the AW original we don't spawn CLIs or manage session UUIDs here — the
platform's ``executor`` already owns run lifecycle, targets, budgets and event
streaming. We just translate OpenAI request/response shapes to/from a single
``run_agent`` / ``run_workflow`` call and back.

The loop closes on itself: an agent whose model is ``provider=openai`` with
``base_url`` pointing back at this endpoint will call the platform through its
own OpenAI surface — that's what the seeded ``openai-compat-demo`` workflow
exercises.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncGenerator, Optional, Union

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..db import session_scope
from ..models import Agent, Target, Workflow

router = APIRouter(tags=["openai-compat"])


# ---------------------------------------------------------------------------
# Request models (permissive — OpenAI clients send many extra fields)
# ---------------------------------------------------------------------------


class _ContentPart(BaseModel):
    type: str
    text: Optional[str] = None


class _Message(BaseModel):
    role: str
    content: Union[str, list[_ContentPart], None] = None
    name: Optional[str] = None

    def as_text(self) -> str:
        if self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        return "\n".join(p.text for p in self.content if p.text)


class _ChatRequest(BaseModel):
    model: str
    messages: list[_Message]
    stream: bool = False
    # everything else (temperature, tools, tool_choice, max_tokens, …) is
    # accepted and ignored — the target agent/workflow owns its own config.

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_model(model: str) -> tuple[str, str]:
    """``agent/foo`` → ("agent", "foo"); bare ``foo`` → ("agent", "foo")."""
    m = model.strip()
    if m.startswith("agent/"):
        return "agent", m[len("agent/"):]
    if m.startswith("workflow/"):
        return "workflow", m[len("workflow/"):]
    if m.startswith("wf/"):
        return "workflow", m[len("wf/"):]
    return "agent", m


def _split_messages(messages: list[_Message]) -> tuple[str, list[dict]]:
    """Return (last_user_text, prior_messages_as_dicts).

    The final message becomes the run's ``user_input``; anything before it is
    forwarded as ``extra_messages`` so multi-turn OpenAI conversations carry
    their history into the agent.
    """
    if not messages:
        return "", []
    prior = [
        {"role": m.role, "content": m.as_text()}
        for m in messages[:-1]
        if m.as_text()
    ]
    return messages[-1].as_text(), prior


def _adhoc_target_id() -> str:
    """Get (or create) the well-known bucket target for OpenAI-compat runs.

    ``runs.target_id`` is NOT NULL, so top-level runs need a Target. We reuse
    the same ``ad-hoc`` bucket the REST ``/run`` endpoints use.
    """
    with session_scope() as s:
        t = s.query(Target).filter(Target.slug == "ad-hoc").first()
        if t is None:
            t = Target(
                slug="ad-hoc",
                name="Ad-hoc runs",
                description="Auto-created bucket for ad-hoc agent/workflow runs "
                            "(OpenAI-compat API, quick tests).",
            )
            s.add(t)
            s.flush()
        return t.id


async def _run(kind: str, slug: str, user_input: str,
               prior: list[dict]) -> tuple[str, dict]:
    """Execute an agent or workflow to completion. Returns (text, usage)."""
    from ..core.executor import run_agent, run_workflow

    target_id = _adhoc_target_id()
    if kind == "workflow":
        res = await run_workflow(slug, user_input, target_id=target_id)
        out = res.get("output")
        if isinstance(out, dict):
            text = out.get("text") or out.get("reply") or json.dumps(out)
        else:
            text = str(out) if out is not None else ""
    else:
        res = await run_agent(slug, user_input, target_id=target_id,
                              extra_messages=prior or None)
        text = res.get("reply") or res.get("text") or ""

    if res.get("status") not in ("success", None) and not text:
        text = f"[{kind} {slug} {res.get('status')}] {res.get('error') or ''}".strip()

    usage = {
        "prompt_tokens": res.get("tokens_in", 0) or 0,
        "completion_tokens": res.get("tokens_out", 0) or 0,
        "total_tokens": (res.get("tokens_in", 0) or 0) + (res.get("tokens_out", 0) or 0),
        "cost_usd": res.get("cost_usd", 0.0) or 0.0,
    }
    return text, usage


# ---------------------------------------------------------------------------
# SSE helpers (OpenAI chat.completion.chunk shape)
# ---------------------------------------------------------------------------


def _sse_chunk(cid: str, model: str, *, role: Optional[str] = None,
               content: Optional[str] = None,
               finish_reason: Optional[str] = None) -> str:
    delta: dict = {}
    if role:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    payload = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n"


async def _stream(kind: str, slug: str, user_input: str, prior: list[dict],
                  cid: str, model: str) -> AsyncGenerator[str, None]:
    """Run to completion, then emit the result as OpenAI SSE chunks.

    Agents/workflows resolve as a single logical answer, so we don't attempt
    token-level passthrough here — we chunk the final text. This keeps the
    surface identical for streaming and non-streaming clients.
    """
    yield _sse_chunk(cid, model, role="assistant")
    try:
        text, usage = await _run(kind, slug, user_input, prior)
    except Exception as exc:  # surface as content, never break the stream
        text, usage = f"[error] {exc}", {}

    step = 80
    for i in range(0, len(text), step):
        yield _sse_chunk(cid, model, content=text[i:i + step])
    yield _sse_chunk(cid, model, finish_reason="stop")
    if usage:
        yield ("data: " + json.dumps({
            "id": cid, "object": "chat.completion.chunk", "model": model,
            "choices": [], "usage": usage,
        }) + "\n\n")
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/v1/models")
def list_models():
    data: list[dict] = []
    with session_scope() as s:
        for a in s.query(Agent).filter(Agent.deleted_at.is_(None)).all():
            data.append({
                "id": f"agent/{a.slug}", "object": "model", "created": 0,
                "owned_by": "agents-platform", "description": a.description or a.name,
            })
        for w in s.query(Workflow).filter(Workflow.deleted_at.is_(None)).all():
            data.append({
                "id": f"workflow/{w.slug}", "object": "model", "created": 0,
                "owned_by": "agents-platform", "description": w.description or w.name,
            })
    return {"object": "list", "data": data}


@router.get("/v1/models/{model_id:path}")
def get_model(model_id: str):
    kind, slug = _parse_model(model_id)
    with session_scope() as s:
        cls = Workflow if kind == "workflow" else Agent
        row = s.query(cls).filter(cls.slug == slug, cls.deleted_at.is_(None)).first()
        if not row:
            raise HTTPException(404, f"model not found: {model_id}")
        return {
            "id": model_id, "object": "model", "created": 0,
            "owned_by": "agents-platform",
            "description": row.description or row.name,
        }


@router.post("/v1/chat/completions")
async def chat_completions(req: _ChatRequest):
    kind, slug = _parse_model(req.model)

    with session_scope() as s:
        cls = Workflow if kind == "workflow" else Agent
        if not s.query(cls).filter(cls.slug == slug, cls.deleted_at.is_(None)).first():
            raise HTTPException(
                400,
                f"unknown {kind}: {slug!r}. Use 'agent/<slug>' or 'workflow/<slug>'.",
            )

    user_input, prior = _split_messages(req.messages)
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    if req.stream:
        return StreamingResponse(
            _stream(kind, slug, user_input, prior, cid, req.model),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    text, usage = await _run(kind, slug, user_input, prior)
    return {
        "id": cid,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
