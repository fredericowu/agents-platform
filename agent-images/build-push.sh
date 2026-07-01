#!/usr/bin/env bash
# Build and push aw-sandbox-agent-cli-* images (one per CLI).
# Each image is lean: ubuntu:24.04 + Node.js + the specific CLI only.
# Dockerfiles live alongside this script in agent-images/<cli>/Dockerfile.
#
# Usage (from anywhere):
#   repos/agents-platform/agent-images/build-push.sh [--push] [--no-cache] [--tag <tag>] [--registry <registry>] [--cli <name>]
#
# Defaults:
#   Registry : docker.io
#   Tag      : latest
#   CLIs     : all (claude codex gemini copilot cursor)
#
# Examples:
#   ./agent-images/build-push.sh                          # local build, all CLIs
#   ./agent-images/build-push.sh --cli claude              # local build, claude only
#   ./agent-images/build-push.sh --push                    # push multi-arch, all CLIs
#   ./agent-images/build-push.sh --push --no-cache         # force latest CLI versions
#   ./agent-images/build-push.sh --push --cli codex        # push multi-arch, codex only
#   REGISTRY=ghcr.io ./agent-images/build-push.sh --push

set -euo pipefail
# Build context is this directory — Dockerfiles don't COPY from host so no workspace root needed
cd "$(dirname "$0")"

PUSH=false
NO_CACHE=false
REGISTRY="${REGISTRY:-ghcr.io}"
IMAGE_PREFIX="${IMAGE_PREFIX:-fredericowu/aw-sandbox-agent-cli}"
TAG="${TAG:-latest}"
SELECTED_CLI=""

ALL_CLIS=(claude codex gemini copilot cursor)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --push)       PUSH=true ;;
    --no-cache)   NO_CACHE=true ;;
    --tag)        TAG="$2"; shift ;;
    --registry)   REGISTRY="$2"; shift ;;
    --cli)        SELECTED_CLI="$2"; shift ;;
    *) echo "Unknown flag: $1"; exit 1 ;;
  esac
  shift
done

if [[ -n "$SELECTED_CLI" ]]; then
  CLIS=("$SELECTED_CLI")
else
  CLIS=("${ALL_CLIS[@]}")
fi

BUILDER="aw-agent-cli-builder"

# Ensure a buildx builder with multi-arch support
if ! docker buildx inspect "$BUILDER" &>/dev/null; then
  docker buildx create --name "$BUILDER" --driver docker-container --bootstrap
fi
docker buildx use "$BUILDER"

LOCAL_ARCH=$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')

for CLI in "${CLIS[@]}"; do
  FULL_IMAGE="$REGISTRY/$IMAGE_PREFIX-$CLI:$TAG"
  DOCKERFILE="$CLI/Dockerfile"

  echo ""
  echo "==> CLI      : $CLI"
  echo "==> Image    : $FULL_IMAGE"
  echo "==> Push     : $PUSH"
  echo "==> No-cache : $NO_CACHE"

  CACHE_FLAG=()
  $NO_CACHE && CACHE_FLAG=(--no-cache)

  if $PUSH; then
    echo "==> Building multi-arch (linux/amd64, linux/arm64) and pushing..."
    docker buildx build \
      --platform linux/amd64,linux/arm64 \
      --file "$DOCKERFILE" \
      --tag "$FULL_IMAGE" \
      --push \
      "${CACHE_FLAG[@]}" \
      .
  else
    echo "==> Building for local use ($LOCAL_ARCH, no push)..."
    docker buildx build \
      --platform "linux/$LOCAL_ARCH" \
      --file "$DOCKERFILE" \
      --tag "$FULL_IMAGE" \
      --load \
      "${CACHE_FLAG[@]}" \
      .
    echo "    Run with:"
    echo "      docker run --rm $FULL_IMAGE"
    echo "      docker run --rm -v \$(pwd):/workspace $FULL_IMAGE $CLI -p 'hello'"
  fi
done

echo ""
echo "Done!"
