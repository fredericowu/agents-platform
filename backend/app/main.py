"""FastAPI entrypoint."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api import api_router
from .api import openai_compat
from .config import settings
from .core.mcp_client import sync_mcp_servers_from_file
from .core.executor import recover_orphaned_runs
from .db import init_db
from .seed import seed_all


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_all()
    # Re-attach interrupted runs that still have a durable Redis Stream; cancel
    # the rest. Replaces the old blind cancel-all so runs survive a restart.
    try:
        await recover_orphaned_runs()
    except Exception as e:
        print(f"[main] run recovery skipped: {e}")
    try:
        sync_mcp_servers_from_file()
    except Exception as e:
        print(f"[main] mcp sync skipped: {e}")
    yield


app = FastAPI(title="Agents Platform", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins + ["*"],  # dev: permissive
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)
app.include_router(api_router)
# OpenAI-compatible surface (/v1/*) — mounted at root, BEFORE the SPA
# catch-all below so GET /v1/models isn't swallowed by the frontend fallback.
app.include_router(openai_compat.router)


# Serve frontend if built — with SPA fallback for client-side routes
frontend_dist = settings.repo_root / "frontend" / "dist"
if frontend_dist.exists():
    from fastapi.responses import FileResponse

    app.mount("/assets", StaticFiles(directory=str(frontend_dist / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        # let /api paths fall through (FastAPI matches more specific routes first,
        # but with this catch-all we need to be careful with order)
        if full_path.startswith("api/"):
            from fastapi import HTTPException as _H
            raise _H(404)
        # serve actual file if it exists (e.g. /vite.svg, /favicon.ico)
        f = frontend_dist / full_path
        if f.is_file():
            return FileResponse(f)
        # otherwise: SPA fallback to index.html
        return FileResponse(frontend_dist / "index.html")


def dev_run() -> None:
    import uvicorn
    uvicorn.run("backend.app.main:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    dev_run()
