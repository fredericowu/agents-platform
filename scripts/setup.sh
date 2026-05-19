#!/usr/bin/env bash
# Bootstrap the platform from scratch.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -d .venv ]]; then
  echo "==> creating venv with python3.13"
  /opt/homebrew/bin/python3.13 -m venv .venv
fi
echo "==> installing python deps"
.venv/bin/pip install --upgrade pip --quiet
.venv/bin/pip install -e ".[dev]" --quiet

echo "==> installing frontend deps"
cd frontend && npm install --silent && cd ..

echo "==> installing playwright deps (test runner)"
npm install --silent

echo "==> building frontend"
cd frontend && npm run build && cd ..

echo "==> seeding DB"
.venv/bin/python -m backend.app.seed

echo
echo "✅ ready. start with:  ./scripts/start.sh"
