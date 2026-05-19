#!/usr/bin/env bash
# Dev mode: backend + frontend HMR
set -euo pipefail
cd "$(dirname "$0")/.."

trap 'kill 0' EXIT
.venv/bin/python -m uvicorn backend.app.main:app --reload --port 8765 &
cd frontend && npm run dev &
wait
