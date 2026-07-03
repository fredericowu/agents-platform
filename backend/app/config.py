"""Application settings, loaded from env."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


REPO_ROOT = Path(__file__).resolve().parents[2]          # repos/agents/
# Guard against shallow paths when running inside Docker (e.g. WORKDIR=/app → parents[1] DNE)
try:
    WORKSPACE_ROOT = REPO_ROOT.parents[1]
except IndexError:
    WORKSPACE_ROOT = REPO_ROOT                            # fallback: container root


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTS_", env_file=".env", extra="ignore")

    # Paths
    repo_root: Path = REPO_ROOT
    workspace_root: Path = WORKSPACE_ROOT

    # Database (PostgreSQL — shared aw-postgres instance)
    database_url: str = "postgresql://postgres:postgres@localhost:5432/agents_platform"

    # Redis — durable event stream for CLI agent runs (aw-redis exposed on host)
    redis_url: str = "redis://localhost:6379/0"

    # MCP discovery
    mcp_json_path: Path = WORKSPACE_ROOT / ".mcp.json"
    skills_path: Path = WORKSPACE_ROOT / ".claude" / "skills"

    # Server
    host: str = "127.0.0.1"
    port: int = 8765
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # Default provider/model when unspecified
    default_provider: Literal["anthropic", "openai", "bedrock", "cli", "echo"] = "echo"
    default_model: str = "claude-sonnet-4-5"

    # Limits
    max_run_concurrency: int = 16
    max_node_iterations: int = 30

    # Shared secret for POST /api/telegram/inject (internal-only synthetic
    # message injection, used by cron/task scripts). Empty disables the route.
    telegram_inject_secret: str = ""


settings = Settings()
