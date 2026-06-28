from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..core.executor import start_agent_run_bg
from ..db import get_session
from ..models import Agent
from ..schemas import AgentIn, AgentOut, AgentUpdate, RunInput
from ..slug_utils import assert_slug_available, generate_unique_slug

router = APIRouter(prefix="/api/agents", tags=["agents"])

# Where per-agent config files are written. Bind-mounted from host data/ dir so
# they survive container restarts and are accessible to docker_agent containers.
_AGENTS_DATA_DIR = Path(
    os.environ.get("AW_BASE_DIR", "/opt/agentic-workspace")
) / "data" / "agents-platform"


def _current_gateway_token() -> str | None:
    """Return the current AW MCP gateway token (from aw.json mcp_gateway.token)."""
    base = os.environ.get("AW_BASE_DIR", "/opt/agentic-workspace")
    p = Path(base) / "src" / "config" / "aw.json"
    try:
        import json as _json
        cfg = _json.loads(p.read_text())
        return cfg.get("mcp_gateway", {}).get("token") or None
    except Exception:
        return None


def _inject_gateway_token(cfg: dict) -> dict:
    """Replace stale bearer tokens on AW gateway URLs with the current one."""
    url = cfg.get("url", "")
    # Detect AW gateway (host.docker.internal or localhost on port 9200)
    if not ("host.docker.internal:9200" in url or "localhost:9200" in url or "127.0.0.1:9200" in url):
        return cfg
    token = _current_gateway_token()
    if not token:
        return cfg
    updated = dict(cfg)
    updated["headers"] = {**(cfg.get("headers") or {}), "Authorization": f"Bearer {token}"}
    return updated


def _write_agent_mcp_config(agent: Agent, cli: str | None = None) -> None:
    """Generate CLI-specific MCP config files in data/agents-platform/{agent.id}/."""
    mcp = agent.mcp_config or {}
    servers: dict = mcp.get("servers") or {}
    if not servers:
        return

    agent_dir = _AGENTS_DATA_DIR / agent.id
    agent_dir.mkdir(parents=True, exist_ok=True)

    # ── Claude format (used for --mcp-config flag) ─────────────────────────
    claude_mcp = {
        "mcpServers": {
            name: {
                "type": cfg.get("type", "streamable-http"),
                "url": cfg["url"],
                **({"headers": h} if (h := _inject_gateway_token(cfg).get("headers")) else {}),
            }
            for name, cfg in servers.items()
            if cfg.get("url")
        }
    }
    (agent_dir / "mcp.json").write_text(json.dumps(claude_mcp, indent=2))

    # ── Gemini / Cursor format (.gemini/settings.json / .cursor/mcp.json) ──
    # Same structure as claude but different wrapping — we reuse the same file.

    # ── Codex format (TOML) ─────────────────────────────────────────────────
    lines = ["[mcp_servers]"]
    for name, cfg in servers.items():
        if not cfg.get("url"):
            continue
        lines.append(f"[mcp_servers.{name}]")
        lines.append(f'type = "{cfg.get("type", "streamable-http")}"')
        lines.append(f'url = "{cfg["url"]}"')
        if cfg.get("headers"):
            for k, v in cfg["headers"].items():
                lines.append(f'[mcp_servers.{name}.headers]')
                lines.append(f'{k} = "{v}"')
                break  # only first header section needed
    (agent_dir / "mcp_codex.toml").write_text("\n".join(lines) + "\n")


@router.get("/_resettable")
def list_resettable_agents():
    """Slugs that have seed defaults — used by the UI to decide where to show
    the 'reset to default' button."""
    from ..seed import SEED_AGENTS
    return {a["slug"] for a in SEED_AGENTS}


@router.get("", response_model=list[AgentOut])
def list_agents(include_deleted: bool = Query(False),
                deleted_only: bool = Query(False),
                exclude_pattern: str | None = Query(None,
                    description="SQL LIKE pattern (use % wildcards) applied to slug; matching rows excluded. "
                                "E.g. 'agent-ui-%' hides the UI-test clutter."),
                s: Session = Depends(get_session)):
    """List agents. By default soft-deleted rows are excluded.

    Query params:
      include_deleted=true → return active **and** soft-deleted rows
      deleted_only=true    → return only soft-deleted rows (trash view)
      exclude_pattern      → SQL LIKE pattern to drop matching slugs (clutter filter)
    """
    q = s.query(Agent)
    if deleted_only:
        q = q.filter(Agent.deleted_at.is_not(None))
    elif not include_deleted:
        q = q.filter(Agent.deleted_at.is_(None))
    if exclude_pattern:
        q = q.filter(~Agent.slug.like(exclude_pattern))
    return q.order_by(Agent.name).all()


@router.get("/{slug}", response_model=AgentOut)
def get_agent(slug: str, include_deleted: bool = Query(False),
              s: Session = Depends(get_session)):
    a = s.query(Agent).filter(Agent.slug == slug).first()
    if not a:
        raise HTTPException(404, "not found")
    if a.deleted_at is not None and not include_deleted:
        raise HTTPException(404, "deleted (use include_deleted=true to view)")
    return a


@router.post("", response_model=AgentOut)
def create_agent(body: AgentIn, s: Session = Depends(get_session)):
    """Create a new agent. Slug is auto-generated from name if not supplied.
    Slugs are unique across agents AND workflows.  If a soft-deleted agent with
    the same slug exists, creation fails with 409 — restore it first."""
    slug = (body.slug or "").strip() or generate_unique_slug("agent", s, body.name)
    existing = s.query(Agent).filter(Agent.slug == slug).first()
    if existing is not None:
        if existing.deleted_at is not None:
            raise HTTPException(409,
                "slug exists but is soft-deleted — restore it or pick another slug")
        raise HTTPException(409, "slug already exists")
    try:
        assert_slug_available(slug, s)
    except ValueError as e:
        raise HTTPException(409, str(e))
    data = body.model_dump()
    data["slug"] = slug
    a = Agent(**data)
    s.add(a)
    s.commit()
    s.refresh(a)
    _write_agent_mcp_config(a)
    return a


@router.put("/{slug}", response_model=AgentOut)
def update_agent(slug: str, body: AgentUpdate, s: Session = Depends(get_session)):
    a = s.query(Agent).filter(Agent.slug == slug).first()
    if not a:
        raise HTTPException(404, "not found")
    if a.deleted_at is not None:
        raise HTTPException(409, "agent is soft-deleted — restore it first")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(a, k, v)
    s.commit()
    s.refresh(a)
    _write_agent_mcp_config(a)
    return a


@router.delete("/{slug}")
def delete_agent(slug: str, hard: bool = Query(False),
                 s: Session = Depends(get_session)):
    """Soft-delete an agent (sets ``deleted_at`` to now). The row stays in the
    DB and can be restored. Pass ``?hard=true`` to permanently delete (irreversible)."""
    a = s.query(Agent).filter(Agent.slug == slug).first()
    if not a:
        raise HTTPException(404, "not found")
    if hard:
        s.delete(a)
        s.commit()
        return {"deleted": slug, "soft": False}
    if a.deleted_at is None:
        a.deleted_at = datetime.utcnow()
        s.commit()
    return {"deleted": slug, "soft": True, "deleted_at": a.deleted_at}


from pydantic import BaseModel as _BM2
class _RenameAgent(_BM2):
    new_slug: str


@router.post("/{slug}/rename", response_model=AgentOut)
def rename_agent(slug: str, body: _RenameAgent, s: Session = Depends(get_session)):
    """Rename an agent's slug. Also updates Run.source_slug for all existing runs."""
    a = s.query(Agent).filter(Agent.slug == slug).first()
    if not a:
        raise HTTPException(404, "not found")
    new_slug = body.new_slug.strip()
    if not new_slug:
        raise HTTPException(400, "new_slug is required")
    if new_slug == slug:
        return a
    try:
        assert_slug_available(new_slug, s)
    except ValueError as e:
        raise HTTPException(409, str(e))
    from ..models import Run
    s.query(Run).filter(Run.source_slug == slug).update(
        {"source_slug": new_slug}, synchronize_session=False)
    a.slug = new_slug
    s.commit(); s.refresh(a)
    return a


@router.post("/{slug}/restore", response_model=AgentOut)
def restore_agent(slug: str, s: Session = Depends(get_session)):
    """Undo a soft-delete by clearing ``deleted_at``."""
    a = s.query(Agent).filter(Agent.slug == slug).first()
    if not a:
        raise HTTPException(404, "not found")
    if a.deleted_at is None:
        raise HTTPException(409, "not deleted")
    a.deleted_at = None
    s.commit()
    s.refresh(a)
    return a


@router.post("/{slug}/reset", response_model=AgentOut)
def reset_agent(slug: str, s: Session = Depends(get_session)):
    """Restore an agent to its seed-list defaults. Only works for slugs that
    exist in SEED_AGENTS (the platform's bundled list)."""
    from ..seed import SEED_AGENTS
    spec = next((a for a in SEED_AGENTS if a["slug"] == slug), None)
    if spec is None:
        raise HTTPException(400, "no seed defaults exist for this slug")
    a = s.query(Agent).filter(Agent.slug == slug).first()
    if a is None:
        a = Agent(**spec)
        s.add(a)
    else:
        for k, v in spec.items():
            setattr(a, k, v)
    s.commit(); s.refresh(a)
    return a


@router.get("/{slug}/export")
def export_agent(slug: str, s: Session = Depends(get_session)):
    a = s.query(Agent).filter(Agent.slug == slug).first()
    if not a:
        raise HTTPException(404, "not found")
    return {
        "_kind": "agent", "_version": 1,
        "slug": a.slug, "name": a.name, "description": a.description,
        "system_prompt": a.system_prompt, "model_slug": a.model_slug,
        "tool_specs": a.tool_specs, "skill_slugs": a.skill_slugs,
        "params": a.params, "icon": a.icon, "color": a.color,
    }


from pydantic import BaseModel as _BM
class _ImportAgent(_BM):
    slug: str | None = None
    name: str
    description: str = ""
    system_prompt: str = ""
    model_slug: str | None = None
    tool_specs: list = []
    skill_slugs: list = []
    params: dict = {}
    icon: str = "bot"
    color: str = "#58a6ff"


@router.post("/import", response_model=AgentOut)
def import_agent(body: _ImportAgent, s: Session = Depends(get_session)):
    """Import an agent. If slug exists, picks <slug>-imported[-N]."""
    base = body.slug or "imported-agent"
    new_slug = base
    i = 2
    while s.query(Agent).filter(Agent.slug == new_slug).first():
        new_slug = f"{base}-imported" if i == 2 else f"{base}-imported-{i}"
        i += 1
    a = Agent(slug=new_slug, name=body.name, description=body.description,
              system_prompt=body.system_prompt, model_slug=body.model_slug,
              tool_specs=body.tool_specs, skill_slugs=body.skill_slugs,
              params=body.params, icon=body.icon, color=body.color)
    s.add(a); s.commit(); s.refresh(a)
    return a


@router.post("/{slug}/clone", response_model=AgentOut)
def clone_agent(slug: str, s: Session = Depends(get_session)):
    src = s.query(Agent).filter(Agent.slug == slug).first()
    if not src:
        raise HTTPException(404, "not found")
    # find a unique new slug: <slug>-copy, -copy-2, etc.
    base = f"{slug}-copy"
    new_slug = base
    i = 2
    while s.query(Agent).filter(Agent.slug == new_slug).first():
        new_slug = f"{base}-{i}"
        i += 1
    clone = Agent(
        slug=new_slug,
        name=f"{src.name} (copy)",
        description=src.description,
        system_prompt=src.system_prompt,
        model_slug=src.model_slug,
        tool_specs=list(src.tool_specs or []),
        skill_slugs=list(src.skill_slugs or []),
        params=dict(src.params or {}),
        icon=src.icon, color=src.color,
    )
    s.add(clone); s.commit(); s.refresh(clone)
    return clone


@router.post("/{slug}/run")
async def run_agent_ep(slug: str, body: RunInput, s: Session = Depends(get_session)):
    a = s.query(Agent).filter(Agent.slug == slug).first()
    if not a:
        raise HTTPException(404, "not found")
    payload = body.input.get("input", "") if isinstance(body.input, dict) else str(body.input)
    # Prefer first-class body fields; fall back to legacy input.extra for compat.
    extra = body.input.get("extra", {}) if isinstance(body.input, dict) else {}
    target_id = body.target_id or (extra.get("target_id") if isinstance(extra, dict) else None)
    target_slug = body.target_slug or (extra.get("target_slug") if isinstance(extra, dict) else None)
    session_id = body.session_id or (extra.get("session_id") if isinstance(extra, dict) else None)
    if target_id is None and target_slug:
        from ..models import Target
        t = s.query(Target).filter(Target.slug == target_slug).first()
        if t is None:
            raise HTTPException(404, f"target slug '{target_slug}' not found")
        target_id = t.id
    if target_id is None:
        raise HTTPException(400, "target_slug is required — pass a target_slug to link this run to a delivery Target")
    try:
        rid = start_agent_run_bg(slug, payload, target_id=target_id, session_id=session_id)
    except __import__("backend.app.core.executor", fromlist=["TargetBudgetExceeded"]).TargetBudgetExceeded as e:
        raise HTTPException(429, f"target budget exceeded: {e}")
    return {"run_id": rid, "target_id": target_id}
