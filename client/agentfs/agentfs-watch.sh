#!/usr/bin/env bash
# agentfs-watch.sh — keeps AgentFS FUSE process alive; connectivity tracking is inside agentfs.py
# Usage: agentfs-watch.sh <mountpoint> <client-id> [api-url] [remote-root]
set -euo pipefail
trap '' HUP  # survive when parent shell exits (SIGHUP)

MOUNTPOINT="${1:-/opt/agentic-workspace/.tmp/windows-c}"
CLIENT="${2:-aw-windows-1}"
API="${3:-http://127.0.0.1:8010}"
ROOT="${4:-C:\\}"
PYTHON="/opt/agentic-workspace/.venv/aw/bin/python"
SCRIPT="$(dirname "$(realpath "$0")")/agentfs.py"
CHECK_INTERVAL=10

FUSE_PID=""

log() { echo "$(date '+%H:%M:%S') [agentfs-watch] $*"; }

mount_fs() {
    # Lazy-unmount first (safe even on stale/dead mounts — does NOT hang)
    fusermount -uz "$MOUNTPOINT" 2>/dev/null || true
    sleep 0.3
    mkdir -p "$MOUNTPOINT"
    # --foreground keeps the Python process in the foreground so $! is the real PID.
    # The watchdog itself runs in background (launched with & by mount.sh).
    "$PYTHON" "$SCRIPT" "$MOUNTPOINT" \
        --api "$API" --client "$CLIENT" --root "$ROOT" --foreground &
    FUSE_PID=$!
    sleep 2
    log "mounted (pid=$FUSE_PID, client=$CLIENT)"
}

is_fuse_alive() {
    # Check the FUSE process is still running — NOT via ls (ls hangs on stale mounts
    # and returns errors when agent is offline, causing false "dead" detections).
    [[ -n "$FUSE_PID" ]] && kill -0 "$FUSE_PID" 2>/dev/null
}

log "Starting watchdog for $MOUNTPOINT (client=$CLIENT)"
log "Agent connectivity monitoring runs inside agentfs.py"
mount_fs

while true; do
    sleep "$CHECK_INTERVAL"
    if ! is_fuse_alive; then
        log "FUSE process gone (pid=$FUSE_PID) — remounting..."
        mount_fs
    fi
done
