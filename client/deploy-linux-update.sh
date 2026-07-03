#!/usr/bin/env bash
# deploy-linux-update.sh — publish agent.py as an auto-update for the Linux
# client's self-update check. Mirrors deploy-update.sh (Windows) but there's
# no compile step — agent.py IS the artifact, so this just computes its
# sha256 and drops the version manifest agents-platform serves at
# /api/update/linux-latest + /api/update/linux-script.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENT_PY="$SCRIPT_DIR/linux/agent.py"
DATA_DIR="/opt/agentic-workspace/.tmp/remote-agents/update"

VERSION=$(grep -oP '^VERSION = "\K[^"]+' "$AGENT_PY")
if [ -z "$VERSION" ]; then
  echo "Could not parse VERSION from $AGENT_PY" >&2
  exit 1
fi

# Sanity check: the script must at least parse as Python before we publish
# it as an update — connected Linux agents will self-check this same way
# before swapping it in, but fail fast here too.
python3 -m py_compile "$AGENT_PY"

SHA256=$(sha256sum "$AGENT_PY" | awk '{print $1}')

mkdir -p "$DATA_DIR"
cp "$AGENT_PY" "$DATA_DIR/agent.py"
cat > "$DATA_DIR/linux-version.json" <<JSON
{"version":"$VERSION","sha256":"$SHA256"}
JSON

echo "Deployed Linux client update:"
cat "$DATA_DIR/linux-version.json"
echo ""
echo "Connected Linux agents will auto-update within $(( $(grep -oP 'UPDATE_CHECK_INTERVAL = \K[0-9]+' "$AGENT_PY") / 60 )) minutes."
