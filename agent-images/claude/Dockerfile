FROM ubuntu:24.04

ARG TARGETARCH

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=UTC \
    NPM_CONFIG_PREFIX=/opt/agent-npm \
    PATH="/opt/agent-npm/bin:$PATH"

# ── Minimal system packages ───────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    git openssh-client \
    curl wget unzip \
    ca-certificates gnupg \
    build-essential procps sudo less \
    jq sqlite3 \
    python3 python3-pip python3-venv \
    tzdata \
    iputils-ping telnet netcat-openbsd dnsutils traceroute \
    && rm -rf /var/lib/apt/lists/*

# ── Docker CLI ────────────────────────────────────────────────────────────────
RUN curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
        > /etc/apt/sources.list.d/docker.list && \
    apt-get update && apt-get install -y --no-install-recommends docker-ce-cli && \
    rm -rf /var/lib/apt/lists/*

# ── GitHub CLI ────────────────────────────────────────────────────────────────
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && apt-get install -y --no-install-recommends gh && \
    rm -rf /var/lib/apt/lists/*

# ── Node.js LTS ───────────────────────────────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# ── Runtime user ──────────────────────────────────────────────────────────────
RUN echo 'ubuntu ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers && \
    ln -sf /usr/bin/python3 /usr/local/bin/python && \
    mkdir -p /opt/agent-npm && \
    chown -R ubuntu:ubuntu /opt/agent-npm

# ── aw-connector (WS event streamer) ─────────────────────────────────────────
RUN pip3 install --no-cache-dir --break-system-packages websockets
COPY shared/aw-connector /usr/local/bin/aw-connector
RUN chmod +x /usr/local/bin/aw-connector

# ── Claude CLI ────────────────────────────────────────────────────────────────
USER ubuntu
RUN npm install -g @anthropic-ai/claude-code@latest && claude --version

# ── Canonical session working directory ──────────────────────────────────────
# Baked so it exists even when the real repo is NOT bind-mounted here (the
# "Agentic Workspace Folder Access = off" case). Owned by ubuntu so the CLI can
# use it as a writable cwd; sessions live in ~/.claude regardless. When access
# is on, the repo is bind-mounted over this dir.
USER root
RUN mkdir -p /opt/agentic-workspace && chown ubuntu:ubuntu /opt/agentic-workspace
USER ubuntu
WORKDIR /workspace
CMD ["claude", "--version"]
