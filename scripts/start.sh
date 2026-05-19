#!/usr/bin/env bash
# Start the backend (serves the built frontend at /).
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m uvicorn backend.app.main:app \
  --host 127.0.0.1 --port "${AGENTS_PORT:-8765}" "$@"
