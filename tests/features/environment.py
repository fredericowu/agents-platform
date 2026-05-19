"""Behave environment: ensures the backend is running before scenarios."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
BASE = os.environ.get("AGENTS_BASE", "http://127.0.0.1:8765")
SERVER_PROC = None


def _running():
    try:
        return httpx.get(f"{BASE}/api/health", timeout=1.5).status_code == 200
    except Exception:
        return False


def before_all(context):
    global SERVER_PROC
    context.base = BASE
    if _running():
        return
    log_path = REPO / "data" / "behave-server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    venv_py = REPO / ".venv" / "bin" / "python"
    py = str(venv_py) if venv_py.exists() else sys.executable
    SERVER_PROC = subprocess.Popen(
        [py, "-m", "uvicorn", "backend.app.main:app",
         "--host", "127.0.0.1", "--port", "8765", "--log-level", "warning"],
        cwd=str(REPO), stdout=open(log_path, "ab"), stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(60):
        if _running():
            return
        time.sleep(0.25)
    raise RuntimeError(f"backend never came up; see {log_path}")


def after_all(context):
    pass  # leave the server running; user may want to inspect
