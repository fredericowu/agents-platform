"""OpenAI-compatible HTTP surface for the Agents Platform.

This is the *idea* ported from Agentic Workspace's old ``llm_proxy`` (which
wrapped raw CLIs): expose a standard OpenAI ``/v1`` API so ANY OpenAI client
— LangChain/LangGraph, OpenClaw, the OpenAI SDK, curl — can drive the
platform's own **agents and workflows** as if they were chat models.

Endpoints (mounted at the app root, NOT under ``/api``):
  GET  /v1/models             list agents + workflows as OpenAI "models"
  GET  /v1/models/{id}        describe one
  POST /v1/chat/completions   run an agent/workflow (streaming + non-streaming)
  POST /v1/responses          same, via the newer OpenAI Responses API shape
                               (see the "Responses API" section below) —
                               added because @ai-sdk/openai v3+ (used by
                               CopilotKit, Vercel AI SDK, etc.) defaults to
                               this API instead of Chat Completions when a
                               client just does ``createOpenAI({baseURL})(model)``.

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

import asyncio
import json
import time
import uuid
from typing import Any, AsyncGenerator, Optional, Union

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..db import session_scope
from ..models import ApiKey, Agent, CallerIdentity, CallerMessage, Run, Target, Workflow

router = APIRouter(tags=["openai-compat"])


def require_api_key(authorization: Optional[str] = Header(default=None)) -> ApiKey:
    """Resolve the presented bearer token to its ``ApiKey`` row.

    This surface runs real agents/workflows (real cost, real side effects),
    so unlike the rest of the app (browser JWT/session cookie) it needs a
    non-interactive credential external callers (Roblox scripts, curl, other
    services) can present. Each key is scoped to a set of agent/workflow
    slugs (Settings → Access Keys) — returning the row (not just True/False)
    lets each route enforce that scope against the specific slug it resolves,
    not just gate the surface as a whole.
    """
    presented = (authorization or "").removeprefix("Bearer ").strip()
    if not presented:
        raise HTTPException(401, "missing API key")

    with session_scope() as s:
        row = s.query(ApiKey).filter(ApiKey.token == presented).first()
        if not row or row.revoked_at is not None:
            raise HTTPException(401, "missing or invalid API key")
        import datetime as _dt
        row.last_used_at = _dt.datetime.utcnow()
        s.flush()
        # Detach the plain data we need before the session closes.
        return ApiKey(id=row.id, name=row.name, token=row.token,
                       agent_slugs=list(row.agent_slugs or []))


def _check_scope(api_key: ApiKey, slug: str) -> None:
    """Bare agent/workflow slug (e.g. "roblox-genie") must be in the key's
    ``agent_slugs`` allowlist — empty list means the key is unrestricted."""
    allowed = api_key.agent_slugs or []
    if allowed and slug not in allowed:
        raise HTTPException(403, f"this API key isn't scoped to {slug!r}")


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


class _ResponsesRequest(BaseModel):
    """Body shape for the newer ``POST /v1/responses`` API.

    ``input`` is deliberately typed ``Any`` — real clients send a bare
    string, a flat list of ``{role, content}`` message dicts, or a list of
    ``{type: "message", role, content: [{type: "input_text", text}, ...]}``
    items. ``_responses_input_to_messages`` below normalizes all three.
    """

    model: str
    input: Any = None
    stream: bool = False

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


def _responses_input_to_messages(raw_input: Any) -> tuple[str, list[dict]]:
    """Normalize a Responses API ``input`` into (last_user_text, prior_messages).

    Accepts the three shapes real clients send:
      - a bare string                              → single user turn
      - a flat list of ``{"role": ..., "content": ...}``
      - a list of ``{"type": "message", "role": ..., "content": [...]}``
        where each content part is ``{"type": "input_text"|"output_text", "text": ...}``

    Mirrors ``_split_messages``'s contract so both APIs can share ``_run``.
    """
    if raw_input is None:
        return "", []
    if isinstance(raw_input, str):
        return raw_input, []

    def _part_text(part: Any) -> str:
        if isinstance(part, str):
            return part
        if isinstance(part, dict):
            return part.get("text") or ""
        return ""

    def _content_text(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(t for t in (_part_text(p) for p in content) if t)
        return str(content)

    messages: list[dict] = []
    for item in raw_input or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role", "user")
        text = _content_text(item.get("content"))
        if text:
            messages.append({"role": role, "content": text})

    if not messages:
        return "", []
    last = messages[-1]
    return last["content"], messages[:-1]


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


def _parse_caller_meta_info(raw: Optional[str]) -> dict:
    """``X-Caller-Meta-Info`` arrives as a JSON string (Roblox JSONEncode's
    output) — parse it into a dict for the JSONB column. Callers that send a
    non-JSON string aren't punished for it: it's kept under ``raw`` so
    nothing is silently dropped."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"raw": parsed}
    except (json.JSONDecodeError, TypeError):
        return {"raw": raw}


def _upsert_caller_identity(source: Optional[str], external_id: Optional[str],
                            meta_info: Optional[str]) -> Optional[str]:
    """Upsert the (source, external_id) caller and return its row id.

    Called on every ``/v1/chat/completions`` request that carries the
    ``X-Caller-Meta-*`` headers — ``meta_info`` and ``last_seen`` are
    refreshed each time so the row always reflects what the caller last told
    us about itself (e.g. a Roblox player's current AccountAge/membership).
    Returns None when the request isn't tagged (source or external_id
    missing) so callers can skip message logging entirely.
    """
    if not source or not external_id:
        return None
    import datetime as _dt
    parsed_meta = _parse_caller_meta_info(meta_info)
    with session_scope() as s:
        row = (s.query(CallerIdentity)
                .filter(CallerIdentity.source == source,
                        CallerIdentity.external_id == external_id)
                .first())
        now = _dt.datetime.utcnow()
        if row is None:
            row = CallerIdentity(source=source, external_id=external_id,
                                 meta_info=parsed_meta, last_seen=now)
            s.add(row)
        else:
            row.meta_info = parsed_meta or row.meta_info
            row.last_seen = now
        s.flush()
        return row.id


def _log_caller_message(caller_identity_id: Optional[str], role: str, content: str) -> None:
    """Append one turn to a caller's history. No-op if not caller-tagged or empty."""
    if not caller_identity_id or not content:
        return
    with session_scope() as s:
        s.add(CallerMessage(caller_identity_id=caller_identity_id, role=role, content=content))


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
                  cid: str, model: str,
                  caller_identity_id: Optional[str] = None) -> AsyncGenerator[str, None]:
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

    _log_caller_message(caller_identity_id, "assistant", text)

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
# Responses API — request/response shapes (see openai-responses-language-model.ts
# in @ai-sdk/openai for the exact event-by-event contract this mirrors; only
# the plain-text-message path is implemented, no tool calls/reasoning, since
# the platform's own executor — not the calling client — owns tool use).
# ---------------------------------------------------------------------------


def _responses_object(rid: str, model: str, status: str, text: str,
                      usage: dict, *, msg_id: str) -> dict:
    return {
        "id": rid,
        "object": "response",
        "created_at": int(time.time()),
        "model": model,
        "status": status,
        "output": [{
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "status": status,
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }],
        "output_text": text,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
        "incomplete_details": None,
    }


def _responses_sse(event_type: str, **fields: Any) -> str:
    return f"data: {json.dumps({'type': event_type, **fields})}\n\n"


async def _responses_stream(kind: str, slug: str, user_input: str,
                            prior: list[dict], rid: str, model: str) -> AsyncGenerator[str, None]:
    """Run to completion, then emit the result as Responses-API SSE events.

    Same "resolve, then chunk the final text" approach as ``_stream`` for
    Chat Completions — agents/workflows don't give us token-level
    passthrough, so streaming here is about shape-compatibility with
    Responses-API clients, not real incremental generation.
    """
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    output_index = 0

    yield _responses_sse("response.created", response={
        "id": rid, "object": "response", "created_at": int(time.time()),
        "model": model, "status": "in_progress",
    })
    yield _responses_sse(
        "response.output_item.added", output_index=output_index,
        item={"id": msg_id, "type": "message", "role": "assistant",
              "status": "in_progress", "content": []},
    )

    try:
        text, usage = await _run(kind, slug, user_input, prior)
    except Exception as exc:  # surface as content, never break the stream
        text, usage = f"[error] {exc}", {}

    step = 80
    for i in range(0, len(text), step):
        yield _responses_sse(
            "response.output_text.delta", item_id=msg_id,
            output_index=output_index, delta=text[i:i + step],
        )

    yield _responses_sse(
        "response.output_item.done", output_index=output_index,
        item={"id": msg_id, "type": "message", "role": "assistant",
              "status": "completed",
              "content": [{"type": "output_text", "text": text, "annotations": []}]},
    )
    yield _responses_sse(
        "response.completed",
        response=_responses_object(rid, model, "completed", text, usage, msg_id=msg_id),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/v1/models")
def list_models(api_key: ApiKey = Depends(require_api_key)):
    allowed = set(api_key.agent_slugs or [])
    data: list[dict] = []
    with session_scope() as s:
        for a in s.query(Agent).filter(Agent.deleted_at.is_(None)).all():
            if allowed and a.slug not in allowed:
                continue
            data.append({
                "id": f"agent/{a.slug}", "object": "model", "created": 0,
                "owned_by": "agents-platform", "description": a.description or a.name,
            })
        for w in s.query(Workflow).filter(Workflow.deleted_at.is_(None)).all():
            if allowed and w.slug not in allowed:
                continue
            data.append({
                "id": f"workflow/{w.slug}", "object": "model", "created": 0,
                "owned_by": "agents-platform", "description": w.description or w.name,
            })
    return {"object": "list", "data": data}


@router.get("/v1/models/{model_id:path}")
def get_model(model_id: str, api_key: ApiKey = Depends(require_api_key)):
    kind, slug = _parse_model(model_id)
    _check_scope(api_key, slug)
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
async def chat_completions(
    req: _ChatRequest,
    api_key: ApiKey = Depends(require_api_key),
    x_caller_meta_id: Optional[str] = Header(default=None, alias="X-Caller-Meta-Id"),
    x_caller_meta_info: Optional[str] = Header(default=None, alias="X-Caller-Meta-Info"),
    x_caller_meta_source: Optional[str] = Header(default=None, alias="X-Caller-Meta-Source"),
):
    kind, slug = _parse_model(req.model)
    _check_scope(api_key, slug)

    with session_scope() as s:
        cls = Workflow if kind == "workflow" else Agent
        if not s.query(cls).filter(cls.slug == slug, cls.deleted_at.is_(None)).first():
            raise HTTPException(
                400,
                f"unknown {kind}: {slug!r}. Use 'agent/<slug>' or 'workflow/<slug>'.",
            )

    user_input, prior = _split_messages(req.messages)
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    caller_identity_id = _upsert_caller_identity(
        x_caller_meta_source, x_caller_meta_id, x_caller_meta_info)
    _log_caller_message(caller_identity_id, "user", user_input)

    if req.stream:
        return StreamingResponse(
            _stream(kind, slug, user_input, prior, cid, req.model,
                    caller_identity_id=caller_identity_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    text, usage = await _run(kind, slug, user_input, prior)
    _log_caller_message(caller_identity_id, "assistant", text)
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


# ---------------------------------------------------------------------------
# Persistent-session chat — real CLI session continuity (--resume), no
# history resending. For callers like the aw-roblox Genie NPC that want the
# agent to remember the conversation the way a stateful chat session would,
# instead of the OpenAI-style "resend the whole transcript every call" shape
# above.
# ---------------------------------------------------------------------------

_SESSION_TARGET_LOCKS: dict[str, "asyncio.Lock"] = {}


class _SessionChatRequest(BaseModel):
    external_id: str
    message: str
    initiator_kind: str = "api"
    # Folded in front of ``message`` ONLY on this external_id's very first
    # call (i.e. only when this endpoint itself determines is_new_session is
    # true) -- never on later calls, since the resumed CLI session already
    # has it live. Deliberately NOT the caller's job to decide "is this
    # new" (a caller-side DB row can easily outlive/predate the actual
    # agents-platform session -- e.g. a player who talked to the agent
    # before this endpoint existed, or after an admin resets the session --
    # this endpoint is the only accurate source for "is this session new").
    context: Optional[str] = None

    model_config = {"extra": "allow"}


@router.post("/v1/agents/{slug}/session_chat")
async def session_chat(slug: str, req: _SessionChatRequest,
                       api_key: ApiKey = Depends(require_api_key)):
    """Persistent-session chat: one Target is auto-provisioned per
    (agent slug, external_id) — same convention as the internal
    ``/api/agents/{slug}/run_sync`` endpoint used by Watch/Glasses — and the
    most recent CLI ``session_id`` recorded against that Target is resumed
    automatically, so the CLI process itself remembers the conversation
    server-side (``claude --resume``). The caller only ever sends the new
    message, never the prior transcript.

    ``context`` (optional) is folded in front of the message, but only when
    THIS call turns out to be the first one ever for ``external_id`` — safe
    for the caller to pass on every single call (e.g. the current Roblox
    player profile), since whether it actually gets used is decided here
    from the Target's own Run history, not from any state the caller tracks
    itself. Also returned as ``is_new_session`` for callers that want to
    know either way.
    """
    _check_scope(api_key, slug)
    with session_scope() as s:
        agent = s.query(Agent).filter(Agent.slug == slug, Agent.deleted_at.is_(None)).first()
        if not agent:
            raise HTTPException(404, f"agent not found: {slug}")

    target_slug = f"{slug}-{req.external_id}"
    with session_scope() as s:
        target = s.query(Target).filter(Target.slug == target_slug).first()
        if target is None:
            target = Target(slug=target_slug, name=f"{slug} / {req.external_id}",
                            source_kind="external", source_ref=req.external_id)
            s.add(target)
            s.flush()
            s.commit()
        target_id = target.id

    # Same "read latest session, then run" locking as run_sync — guards
    # against two rapid-fire calls for the same external_id both reading the
    # same stale session_id and forking the conversation into two branches.
    lock = _SESSION_TARGET_LOCKS.setdefault(target_id, asyncio.Lock())
    from ..core.executor import run_agent
    async with lock:
        with session_scope() as s:
            session_id = (
                s.query(Run.session_id)
                .filter(Run.target_id == target_id, Run.session_id.isnot(None))
                .order_by(Run.started_at.desc())
                .limit(1)
                .scalar()
            )
        is_new_session = session_id is None
        message = req.message
        if is_new_session and req.context:
            message = f"{req.context}\n\n[MENSAGEM DO JOGADOR]\n{req.message}"
        result = await run_agent(
            slug, message, target_id=target_id, session_id=session_id,
            initiator_kind=req.initiator_kind, initiator_id=req.external_id,
        )

    return {
        "reply": result.get("reply") or result.get("text") or "",
        "run_id": result.get("run_id"),
        "status": result.get("status"),
        "is_new_session": is_new_session,
    }


@router.post("/v1/responses")
async def responses(req: _ResponsesRequest, api_key: ApiKey = Depends(require_api_key)):
    kind, slug = _parse_model(req.model)
    _check_scope(api_key, slug)

    with session_scope() as s:
        cls = Workflow if kind == "workflow" else Agent
        if not s.query(cls).filter(cls.slug == slug, cls.deleted_at.is_(None)).first():
            raise HTTPException(
                400,
                f"unknown {kind}: {slug!r}. Use 'agent/<slug>' or 'workflow/<slug>'.",
            )

    user_input, prior = _responses_input_to_messages(req.input)
    rid = f"resp_{uuid.uuid4().hex[:24]}"

    if req.stream:
        return StreamingResponse(
            _responses_stream(kind, slug, user_input, prior, rid, req.model),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    text, usage = await _run(kind, slug, user_input, prior)
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    return _responses_object(rid, req.model, "completed", text, usage, msg_id=msg_id)
