from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Any

from ..db import get_session
from ..models import Agent, Model
from ..schemas import ModelOut, ModelUpdate

router = APIRouter(prefix="/api/models", tags=["models"])


class ModelIn(BaseModel):
    slug: str
    provider: str          # echo|anthropic|openai|bedrock|cli|fake
    model_id: str
    display_name: str
    params: dict[str, Any] = {}
    enabled: bool = True


@router.get("", response_model=list[ModelOut])
def list_models(s: Session = Depends(get_session)):
    return s.query(Model).order_by(Model.provider, Model.slug).all()


@router.post("", response_model=ModelOut)
def create_model(body: ModelIn, s: Session = Depends(get_session)):
    if s.query(Model).filter(Model.slug == body.slug).first():
        raise HTTPException(409, "slug already exists")
    m = Model(**body.model_dump())
    s.add(m)
    s.commit()
    s.refresh(m)
    return m


@router.put("/{slug}", response_model=ModelOut)
def update_model(slug: str, patch: ModelUpdate, s: Session = Depends(get_session)):
    m = s.query(Model).filter(Model.slug == slug).first()
    if not m:
        raise HTTPException(404, "not found")
    for field in ("enabled", "params", "display_name", "model_id", "provider"):
        val = getattr(patch, field, None)
        if val is not None:
            setattr(m, field, val)
    s.commit()
    s.refresh(m)
    return m


@router.delete("/{slug}")
def delete_model(slug: str, force: bool = Query(False),
                 s: Session = Depends(get_session)):
    m = s.query(Model).filter(Model.slug == slug).first()
    if not m:
        raise HTTPException(404, "not found")
    refs = s.query(Agent).filter(Agent.model_slug == slug).all()
    if refs and not force:
        names = [a.slug for a in refs]
        raise HTTPException(409, f"model is referenced by {len(refs)} agent(s): "
                                  f"{', '.join(names)}. Repoint or pass ?force=true.")
    # if forcing, null-out the agents' model_slug
    if refs and force:
        for a in refs:
            a.model_slug = None
    s.delete(m)
    s.commit()
    return {"deleted": slug, "force_unlinked_agents": [a.slug for a in refs] if force else []}


@router.get("/providers/info")
def provider_info():
    """Static metadata about each provider — used by the UI to render
    the right form fields when creating a model.

    ``kind`` tells the UI whether tool calling uses the platform's tool
    inventory (``api``: LangChain bind_tools loop) or the provider's own
    native tools (``binary``: executed by the CLI itself).
    """
    return {
        "echo":         {"label": "Echo (offline test)", "kind": "stub", "fields": []},
        "fake":         {"label": "Fake tool-calling (test only)", "kind": "api",
                         "fields": ["script"]},
        "anthropic":    {"label": "Anthropic API", "kind": "api",
                         "fields": ["temperature", "max_tokens"],
                         "env": ["ANTHROPIC_API_KEY"]},
        "openai":       {"label": "OpenAI API", "kind": "api",
                         "fields": ["temperature", "max_tokens", "base_url"],
                         "env": ["OPENAI_API_KEY"]},
        "bedrock":      {"label": "AWS Bedrock (Converse API, tools supported)", "kind": "api",
                         "fields": ["region", "temperature"],
                         "env": ["AWS_REGION", "AWS_PROFILE"]},
        "cli":          {"label": "CLI Agent (Docker container, native tools)", "kind": "cli",
                         "fields": ["cli", "model", "cwd", "add_dirs",
                                    "allowed_tools", "disallowed_tools",
                                    "dangerous_skip_permissions", "stream_json",
                                    "timeout_s", "extra_args"]},
    }
