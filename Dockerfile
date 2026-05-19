# Agents Platform — AW App
# Python 3.11 slim, deps baked in, source mounted at runtime for live reload.

FROM python:3.11-slim

# Build tools (needed by some Python wheels: e.g. pydantic-core, uvloop)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ------------------------------------------------------------------
# Install Python deps (copy full source so setuptools finds packages)
# ------------------------------------------------------------------
COPY pyproject.toml ./
COPY backend/   backend/
COPY cli/       cli/
COPY mcp_server/ mcp_server/
RUN pip install --no-cache-dir -e .

# ------------------------------------------------------------------
# Copy remaining assets (frontend dist, data seed, etc.)
# Overridden by volume mount in dev mode.
# ------------------------------------------------------------------
COPY frontend/dist/ frontend/dist/

# ------------------------------------------------------------------
# Runtime config
# ------------------------------------------------------------------
# Bind to all interfaces so AW proxy can reach us
ENV AGENTS_HOST=0.0.0.0
ENV AGENTS_PORT=8765

# Suppress path errors for MCP/skills discovery (not available in container)
ENV AGENTS_MCP_JSON_PATH=/nonexistent/.mcp.json
ENV AGENTS_SKILLS_PATH=/nonexistent/skills

EXPOSE 8765

# uvicorn with --reload watches /app/backend for Python changes.
# The frontend is served as pre-built static from /app/frontend/dist/.
# Use shell form so ${AGENTS_PORT} is expanded at container start-time
CMD uvicorn backend.app.main:app \
    --host 0.0.0.0 \
    --port ${AGENTS_PORT:-8765} \
    --reload \
    --reload-dir /app/backend
