#!/usr/bin/env bash
# Run the full test matrix: BDD + Playwright UI + MCP smoke.
set -euo pipefail
cd "$(dirname "$0")/.."

# ensure server is up
if ! curl -fsS http://127.0.0.1:8765/api/health >/dev/null 2>&1; then
  echo "==> backend not running; starting in background"
  .venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8765 --log-level warning > data/test_server.log 2>&1 &
  for _ in $(seq 1 40); do
    sleep 0.3
    curl -fsS http://127.0.0.1:8765/api/health >/dev/null 2>&1 && break
  done
fi

echo
echo "==> BDD (behave)"
.venv/bin/behave tests/features

echo
echo "==> MCP smoke"
.venv/bin/python scripts/test_mcp.py

echo
echo "==> Playwright UI"
npx playwright test --reporter=list

echo
echo "✅ all green"
