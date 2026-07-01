#!/usr/bin/env bash
# deploy-update.sh — build the Windows exe and deploy it as an auto-update.
# The agents-platform serves it at /api/update/exe
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="/opt/agentic-workspace/.tmp/remote-agents/update"

echo "Building..."
bash "$SCRIPT_DIR/build-windows.sh"

echo "Deploying to $DATA_DIR..."
mkdir -p "$DATA_DIR"
cp "$SCRIPT_DIR/dist/aw-remote-agent.exe" "$DATA_DIR/aw-remote-agent.exe"
cp "$SCRIPT_DIR/dist/version.json"        "$DATA_DIR/version.json"

echo "Deployed:"
cat "$DATA_DIR/version.json"
echo ""
echo "Agents will auto-update within 5 minutes."
