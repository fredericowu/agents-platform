#!/usr/bin/env bash
# Build the Windows client using Docker cross-compilation (no local Go required)
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT="$SCRIPT_DIR/dist/aw-remote-agent.exe"
mkdir -p "$SCRIPT_DIR/dist"

echo "Building aw-remote-agent.exe via Docker..."
docker run --rm \
  -v "$SCRIPT_DIR/windows:/src" \
  -w /src \
  golang:1.21-alpine \
  sh -c "go mod download && GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -ldflags='-s -w' -o /src/aw-remote-agent.exe ."

cp "$SCRIPT_DIR/windows/aw-remote-agent.exe" "$OUTPUT"
echo "Done: $OUTPUT"

# Generate version.json with sha256 for auto-update
SHA256=$(sha256sum "$OUTPUT" | awk '{print $1}')
VERSION=$(grep 'version = ' "$SCRIPT_DIR/windows/main.go" | head -1 | grep -oP '"\K[^"]+')
cat > "$SCRIPT_DIR/dist/version.json" << VERJSON
{"version":"$VERSION","sha256":"$SHA256"}
VERJSON
echo "version.json: $VERSION @ $SHA256"
