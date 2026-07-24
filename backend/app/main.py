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


def _otel_resource_attrs() -> dict:
    return {
        "service.name": "agents-platform",
        "service.version": "0.1.0",
        "deployment.environment": os.environ.get("AW_ENV", "production"),
    }


def _setup_otel() -> None:
    """Wire up OpenTelemetry → SigNoz when OTEL_ENABLED=1.

    Mirrors agentic-workspace's src/api/app.py setup, but with
    service.name="agents-platform" so the two services are distinguishable
    in SigNoz. Uses the ASGI middleware approach (not FastAPIInstrumentor)
    to coexist with ddtrace if it's ever enabled here too.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        endpoint = os.environ.get(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"
        )
        resource = Resource.create(_otel_resource_attrs())
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        # Outbound HTTP client spans — this service calls the Anthropic API
        # for every agent run and delivers every Telegram reply, so without
        # this those two calls (the most important ones in the whole system)
        # were completely invisible in tracing. Both libraries instrumented
        # since the codebase uses httpx in some places and requests in others.
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
        HTTPXClientInstrumentor().instrument()
        RequestsInstrumentor().instrument()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("OTel setup failed: %s", exc)


def _add_otel_middleware(app: FastAPI) -> None:
    """Add the ASGI tracing middleware to an existing FastAPI app."""
    try:
        from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
        app.add_middleware(OpenTelemetryMiddleware)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("OTel middleware setup failed: %s", exc)


def _setup_otel_logs() -> None:
    """Ship every `logging.getLogger(...)` record to SigNoz as an OTLP log."""
    import logging

    try:
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

        endpoint = os.environ.get(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"
        )
        resource = Resource.create(_otel_resource_attrs())
        provider = LoggerProvider(resource=resource)
        exporter = OTLPLogExporter(endpoint=f"{endpoint}/v1/logs")
        provider.add_log_record_processor(BatchLogRecordProcessor(exporter))

        handler = LoggingHandler(level=logging.INFO, logger_provider=provider)
        root = logging.getLogger()
        root.addHandler(handler)
        # Root defaults to WARNING; without lowering it, INFO-level app logs
        # never reach the handler above regardless of its own level.
        if root.level == 0 or root.level > logging.INFO:
            root.setLevel(logging.INFO)
    except Exception as exc:
        logging.getLogger(__name__).warning("OTel log export setup failed: %s", exc)


def _setup_local_logging() -> None:
    """Make app-level `logging.getLogger(...)` records visible in the local
    process log, not just SigNoz.

    Without this, the root logger has no handler unless OTel is enabled
    (see `_setup_otel_logs`), and even then its only handler ships to
    SigNoz — so every `log.warning(...)`/`log.exception(...)` in this
    codebase (container-collision retries, inject delivery failures, etc.)
    was invisible in `/tmp/aw-agents-platform.log`, the file actually
    tailed when debugging live. uvicorn's own access/error loggers already
    write to stdout independently of this; this just extends the same
    stdout stream to every other logger in the app.
    """
    import logging
    import sys

    root = logging.getLogger()
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
        root.addHandler(handler)
    if root.level == 0 or root.level > logging.INFO:
        root.setLevel(logging.INFO)


_setup_local_logging()

if os.environ.get("OTEL_ENABLED") == "1":
    _setup_otel()
    _setup_otel_logs()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_all()
    # Capture the running event loop NOW, before any recovery runs. Startup
    # recovery re-enqueues pending Telegram messages whose chat-worker threads
    # bridge back to this loop via asyncio.run_coroutine_threadsafe(_MAIN_LOOP).
    # Previously _MAIN_LOOP was only captured on the first webhook request, so a
    # boot-time recovery had _MAIN_LOOP=None → the re-enqueued messages hit the
    # cross-loop asyncio.run() fallback and never drained (stuck 'pending'
    # forever, blocking the chat's FIFO queue). Setting it here makes the
    # startup auto-heal actually deliver.
    try:
        import asyncio as _asyncio
        from .api.telegram import _set_main_loop
        _set_main_loop(_asyncio.get_running_loop())
    except Exception as e:
        print(f"[main] main-loop capture skipped: {e}")
    # Re-attach interrupted runs that still have a durable Redis Stream; cancel
    # the rest. Replaces the old blind cancel-all so runs survive a restart.
    try:
        await recover_orphaned_runs()
    except Exception as e:
        print(f"[main] run recovery skipped: {e}")
    try:
        from .api.telegram import recover_pending_telegram_messages
        recover_pending_telegram_messages()
    except Exception as e:
        print(f"[main] telegram message recovery skipped: {e}")
    try:
        sync_mcp_servers_from_file()
    except Exception as e:
        print(f"[main] mcp sync skipped: {e}")
    try:
        from .core.wakeups import rearm_pending_wakeups
        rearm_pending_wakeups()
    except Exception as e:
        print(f"[main] wakeup re-arm skipped: {e}")
    try:
        from .core.wakeups import rearm_pending_agent_callbacks
        rearm_pending_agent_callbacks()
    except Exception as e:
        print(f"[main] agent-callback re-arm skipped: {e}")
    try:
        from .core.wakeups import rearm_stuck_wakeup_runs
        rearm_stuck_wakeup_runs()
    except Exception as e:
        print(f"[main] stuck-wakeup-run re-arm skipped: {e}")
    # Cross-channel fan-out: a pinned session's reply delivered by one channel
    # (e.g. Meta Display/Watch) is echoed to every OTHER channel pinned to the
    # same session_id (e.g. a Telegram chat) via Redis PSUBSCRIBE — see
    # core/redis_streams.py's run_session_event_listener + api/telegram.py's
    # deliver_foreign_session_event. Telegram-originated events are dropped
    # here so a chat never gets its own reply echoed back to itself.
    try:
        import asyncio as _asyncio
        from .core.redis_streams import run_session_event_listener
        from .api.telegram import deliver_foreign_session_event

        async def _on_session_event(session_id: str, data: dict) -> None:
            if (data or {}).get("source") == "telegram":
                return
            await deliver_foreign_session_event(session_id, data)

        _asyncio.create_task(run_session_event_listener(_on_session_event))
    except Exception as e:
        print(f"[main] session event listener skipped: {e}")
    # Warm-container reconcile sweep (AP_WARM_CONTAINER=1 only — no-op
    # otherwise, not even a docker ps call). One-time on boot: adopt any
    # warm container whose epoch still matches its agent's current config,
    # drain everything else (agent deleted/reconfigured while we were down,
    # crash orphans, ...). See core/warm_pool.py.
    try:
        from .core import warm_pool
        if warm_pool.enabled():
            await warm_pool.reconcile_on_boot(warm_pool.current_epoch_for_agent)
    except Exception as e:
        print(f"[main] warm-pool reconcile skipped: {e}")
    yield


app = FastAPI(title="Agents Platform", version="0.1.0", lifespan=lifespan)

if os.environ.get("OTEL_ENABLED") == "1":
    _add_otel_middleware(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins + ["*"],  # dev: permissive
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)
app.include_router(api_router)
# OpenAI-compatible surface (/v1/*) — mounted at root, BEFORE the SPA
# catch-all below so GET /v1/models isn't swallowed by the frontend fallback.
app.include_router(openai_compat.router)


# Public static files (e.g. remote-agent install page) — mounted before the
# SPA catch-all so /static/* wins; not gated by any auth middleware here.
static_public = settings.repo_root / "static_public"
if static_public.exists():
    app.mount("/static", StaticFiles(directory=str(static_public), html=True), name="static")


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
