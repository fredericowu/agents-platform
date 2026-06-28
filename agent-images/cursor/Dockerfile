FROM ubuntu:24.04

ARG TARGETARCH

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=UTC \
    NPM_CONFIG_PREFIX=/opt/agent-npm \
    PATH="/opt/agent-npm/bin:/usr/local/bin:$PATH"

# ── Minimal system packages ───────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    git openssh-client \
    curl wget unzip \
    ca-certificates gnupg \
    build-essential procps sudo less \
    jq sqlite3 \
    python3 python3-pip python3-venv \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

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

# ── Cursor Agent ──────────────────────────────────────────────────────────────
# The official installer is $HOME-aware and drops the binary at
# $HOME/.local/bin/cursor-agent. We redirect HOME to /opt/cursor-agent so
# the binary lives outside any bind-mount path, then symlink to /usr/local/bin.
# Runs as root so /opt is writable; resulting binary is 0755 for ubuntu user.
USER root
RUN mkdir -p /opt/cursor-agent \
    && curl -fsS https://cursor.com/install | HOME=/opt/cursor-agent bash \
    && /opt/cursor-agent/.local/bin/cursor-agent --version \
    && ln -sf /opt/cursor-agent/.local/bin/cursor-agent /usr/local/bin/cursor-agent \
    && cursor-agent --version

USER ubuntu
WORKDIR /workspace
CMD ["cursor-agent", "--version"]
