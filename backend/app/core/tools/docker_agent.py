#!/usr/bin/env python3
"""docker_agent.py — Run an agent CLI inside an isolated on-demand container.

Each container image contains only the bare minimum: ubuntu:24.04 + Node.js +
the specific CLI. The container is ephemeral (--rm) and exits when the agent
finishes. Mounts, credentials, skills, and MCP config are all opt-in.

Usage:
    python -m backend.app.core.tools.docker_agent -p "prompt" [options]

Examples:
    # Run claude on a directory
    python -m backend.app.core.tools.docker_agent -p "summarise this repo" \\
        --mount /opt/agentic-workspace

    # Run codex with credentials forwarded
    python -m backend.app.core.tools.docker_agent --cli codex -p "fix the bug" \\
        --mount /tmp/myproject --creds

    # Full setup: workspace + skills + mcp + creds
    python -m backend.app.core.tools.docker_agent -p "implement the feature" \\
        --mount /opt/agentic-workspace --skills --mcp --creds

    # Dry-run: print the docker command without executing
    python -m backend.app.core.tools.docker_agent -p "hello" --mount /tmp/proj --dry-run
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Workspace root (the AW repo, not the agents-platform sub-repo).
# Prefer the AW_BASE_DIR env var so nothing breaks if the directory layout changes.
BASE_DIR = Path(os.environ.get("AW_BASE_DIR", "/opt/agentic-workspace"))

# Images live in ghcr.io (private packages, built by the build-agent-images CI
# workflow with the workflow-scoped GITHUB_TOKEN). Pulls use the ghcr.io read
# token already in the container's ~/.docker/config.json. Override via env.
REGISTRY = os.environ.get("AW_AGENT_REGISTRY", "ghcr.io")
IMAGE_PREFIX = os.environ.get("AW_AGENT_IMAGE_PREFIX", "fredericowu/aw-sandbox-agent-cli")
DEFAULT_TAG = os.environ.get("AW_AGENT_TAG", "latest")

# Per-CLI configuration
CLI_SPECS: dict[str, dict] = {
    "claude": {
        "bin": "claude",
        "subcmd": None,
        "prompt_flag": "-p",
        "default_extra": [
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            "--verbose",
        ],
        "model_flag": "--model",
        "add_dir_flag": "--add-dir",
        "creds_dir": ".claude",
        # Root config file sitting next to the creds dir (e.g. ~/.claude.json)
        "creds_file": ".claude.json",
        "env_keys": ["ANTHROPIC_API_KEY"],
    },
    "codex": {
        "bin": "codex",
        "subcmd": "exec",      # codex exec <prompt>  (non-interactive)
        "prompt_flag": None,   # prompt is positional, not a flag
        "default_extra": [
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
        ],
        "model_flag": "-c",    # -c model="o3"  (TOML config override)
        "add_dir_flag": None,
        "creds_dir": ".codex",
        "creds_file": None,
        "env_keys": ["OPENAI_API_KEY"],
    },
    "gemini": {
        "bin": "gemini",
        "subcmd": None,
        "prompt_flag": "-p",
        "default_extra": ["--yolo"],   # auto-approve all actions (non-interactive)
        "model_flag": "--model",
        "add_dir_flag": None,
        "creds_dir": ".gemini",
        "creds_file": None,
        "env_keys": ["GOOGLE_API_KEY", "GEMINI_API_KEY"],
    },
    "copilot": {
        "bin": "copilot",
        "subcmd": None,
        "prompt_flag": "-p",
        "default_extra": ["--allow-all-tools"],   # auto-approve tools (non-interactive)
        "model_flag": "--model",
        "add_dir_flag": "--add-dir",
        "creds_dir": ".copilot",
        "creds_file": None,
        "env_keys": ["GITHUB_TOKEN", "GH_TOKEN"],
    },
    "cursor": {
        "bin": "cursor-agent",
        "subcmd": None,
        "prompt_flag": None,   # prompt is positional; -p means --print (non-interactive)
        "default_extra": ["--print"],  # required for non-interactive scripting
        "model_flag": "--model",
        "add_dir_flag": None,
        "creds_dir": ".cursor",
        "creds_file": None,
        "env_keys": ["CURSOR_API_KEY"],
    },
}


def parse_mount(raw: str) -> tuple[str, str]:
    """Parse --mount value into (host_path, container_path).
    Accepts 'host_path' or 'host_path:container_path'.
    Bare paths map to the same absolute path inside the container.
    """
    if ":" in raw:
        host, container = raw.split(":", 1)
    else:
        host = raw
        container = str(Path(raw).resolve())
    return str(Path(host).resolve()), container


def build_docker_argv(
    *,
    cli: str,
    prompt: str,
    mounts: list[str],
    skills: bool,
    mcp: bool,
    creds: bool,
    add_dirs: list[str],
    env_file: str | None,
    forward_env: bool,
    model: str | None,
    extra_args: list[str],
    tag: str,
    image_override: str | None,
    mcp_config_dir: str | None = None,
    agent_id: str | None = None,
    target_id: str | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    extra_docker_env: dict[str, str] | None = None,
    extra_volumes: list[str] | None = None,
    ws_mode: bool = False,
    redis_mode: bool = False,
    share_network: bool = False,
    workdir: str | None = None,
    workdir_tmpfs: bool = False,
) -> list[str]:
    spec = CLI_SPECS[cli]
    image = image_override or f"{REGISTRY}/{IMAGE_PREFIX}-{cli}:{tag}"

    if share_network:
        # Join aw-sandbox's network namespace directly — 127.0.0.1 then reaches
        # every service that shares that netns (awserv, redis, postgres, the
        # agents-platform backend itself). Docker forbids combining a container
        # network mode with --add-host, so that flag is dropped in this branch.
        argv: list[str] = ["docker", "run", "--rm", "-i",
                           "--network", "container:aw-sandbox"]
    else:
        argv = ["docker", "run", "--rm", "-i",
               "--add-host=host.docker.internal:host-gateway"]

    # ── Volume mounts ──────────────────────────────────────────────────────────
    seen_mounts: set[str] = set()

    def add_mount(host: str, container: str, readonly: bool = False) -> None:
        host_abs = str(Path(host).resolve())
        flag = f"{host_abs}:{container}"
        if readonly:
            flag += ":ro"
        if flag not in seen_mounts:
            seen_mounts.add(flag)
            argv.extend(["-v", flag])

    # Working directory: use /home/ubuntu when using agent-specific MCP config to
    # prevent claude from auto-discovering the workspace .mcp.json in the cwd.
    # In normal mode, use the first mounted directory.
    _cwd_set = False

    # Explicit working directory, decoupled from the mounts. When set it wins over
    # all auto-cwd logic below (first-mount / isolated). This keeps the CLI's cwd
    # — and therefore its ~/.claude/projects/<encoded-cwd>/ session store —
    # constant regardless of what is mounted, so sessions are SHARED across agents
    # (all use the same cwd) and survive toggling the workspace mount.
    # workdir_tmpfs mounts an empty, writable tmpfs at that path so the dir exists
    # even when the real repo is NOT bind-mounted there ("workspace access off").
    if workdir:
        if workdir_tmpfs:
            argv.extend(["--tmpfs", f"{workdir}:rw,mode=1777"])
        argv.extend(["-w", workdir])
        _cwd_set = True

    # User-specified mounts
    for raw in mounts:
        h, c = parse_mount(raw)
        add_mount(h, c)
        if not _cwd_set and not mcp_config_dir:
            argv.extend(["-w", c])
            _cwd_set = True

    # Optional: workspace skills
    if skills:
        skills_host = BASE_DIR / "skills"
        if skills_host.is_dir():
            add_mount(str(skills_host), "/workspace/skills", readonly=True)

    # Optional: .mcp.json
    if mcp:
        mcp_host = BASE_DIR / ".mcp.json"
        if mcp_host.is_file():
            add_mount(str(mcp_host), "/workspace/.mcp.json", readonly=True)

    # Optional: CLI credentials from data/home/
    if creds:
        creds_dir = spec["creds_dir"]
        creds_host = BASE_DIR / "data" / "home" / creds_dir
        if creds_host.is_dir():
            add_mount(str(creds_host), f"/home/ubuntu/{creds_dir}")
        # Some CLIs store auth in a root config file alongside the dir (e.g. .claude.json)
        creds_file = spec.get("creds_file")
        if creds_file:
            creds_file_host = BASE_DIR / "data" / "home" / creds_file
            if creds_file_host.is_file():
                add_mount(str(creds_file_host), f"/home/ubuntu/{creds_file}")

    # --add-dir targets: mount and track for CLI flag
    add_dir_mounts: list[str] = []
    for raw in add_dirs:
        h, c = parse_mount(raw)
        add_mount(h, c)
        add_dir_mounts.append(c)

    # Per-run isolated cwd for session persistence (always when agent_id+run_id present).
    # Each run gets its own project dir under ~/.claude/isolated/{agent_id}/{run_id}/
    # so the claude CLI never auto-loads sessions from a sibling run.
    # The .claude dir is already bind-mounted via creds=True so no extra mount needed.
    if agent_id and run_id and not _cwd_set:
        isolated_host = BASE_DIR / "data" / "home" / ".claude" / "isolated" / agent_id / run_id
        isolated_host.mkdir(parents=True, exist_ok=True)
        isolated_cwd = f"/home/ubuntu/.claude/isolated/{agent_id}/{run_id}"
        argv.extend(["-w", isolated_cwd])
        _cwd_set = True

    # Optional: per-agent MCP config dir (data/agents-platform/{agent_id}/)
    if mcp_config_dir and Path(mcp_config_dir).is_dir():
        add_mount(str(Path(mcp_config_dir).resolve()), "/agent-config", readonly=True)
        if not _cwd_set:
            argv.extend(["-w", "/home/ubuntu"])

    # Per-agent extra volumes (e.g. ["/var/run/docker.sock:/var/run/docker.sock"])
    for vol in (extra_volumes or []):
        if ":" in vol:
            h, c = vol.split(":", 1)
            add_mount(h, c)
        else:
            add_mount(vol, vol)

    # ── Environment ────────────────────────────────────────────────────────────
    if env_file:
        argv.extend(["--env-file", env_file])
    elif forward_env:
        # Forward known API keys from current environment
        for key in spec["env_keys"]:
            val = os.environ.get(key)
            if val:
                argv.extend(["-e", f"{key}={val}"])
    if extra_docker_env:
        for key, val in extra_docker_env.items():
            argv.extend(["-e", f"{key}={val}"])

    # ── Connector mount (must be registered BEFORE the image — it's a -v flag) ───
    # redis_mode: aw-connector-redis isn't baked into the image (unlike the WS
    # aw-connector), so we bind-mount it from the repo. add_mount appends a `-v`
    # flag to argv, which docker requires to come before the image name.
    if redis_mode:
        connector_host = str(
            BASE_DIR / "repos" / "agents-platform" / "agent-images" / "shared" / "aw-connector-redis"
        )
        add_mount(connector_host, "/usr/local/bin/aw-connector-redis", readonly=True)

    # ── Image ──────────────────────────────────────────────────────────────────
    argv.append(image)

    # ── CLI command ────────────────────────────────────────────────────────────
    # ws_mode: aw-connector wraps the CLI, streams stdout back via WebSocket.
    # redis_mode: aw-connector-redis wraps the CLI, publishes stdout to Redis Stream directly.
    # Env vars (AW_RUN_ID, AW_AGENT_TOKEN/AW_REDIS_URL) injected above via extra_docker_env.
    if redis_mode:
        argv.append("aw-connector-redis")
    elif ws_mode:
        argv.append("aw-connector")
    argv.append(spec["bin"])
    if spec.get("subcmd"):
        argv.append(spec["subcmd"])

    # --resume must come before -p for the claude CLI
    if session_id and cli == "claude":
        argv.extend(["--resume", session_id])

    prompt_flag = spec.get("prompt_flag")
    if prompt_flag:
        argv.extend([prompt_flag, prompt])
    else:
        argv.append(prompt)   # positional (e.g. codex exec "prompt")

    if model and spec["model_flag"]:
        # codex uses -c model="value" syntax; others use --model value
        if spec["model_flag"] == "-c":
            argv.extend(["-c", f'model="{model}"'])
        else:
            argv.extend([spec["model_flag"], model])

    add_dir_flag = spec.get("add_dir_flag")
    if add_dir_flag:
        for d in add_dir_mounts:
            argv.extend([add_dir_flag, d])

    # MCP config file — CLI-specific injection
    if mcp_config_dir and Path(mcp_config_dir).is_dir():
        if cli == "claude" and (Path(mcp_config_dir) / "mcp.json").exists():
            argv.extend(["--mcp-config", "/agent-config/mcp.json"])

    argv.extend(spec["default_extra"])
    argv.extend(extra_args)

    return argv


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run an agent CLI inside an isolated on-demand container.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-p", "--prompt", required=True, help="Prompt to pass to the agent CLI")
    parser.add_argument(
        "--cli", choices=list(CLI_SPECS), default="claude",
        help="Which CLI to use (default: claude)",
    )
    parser.add_argument(
        "--mount", "-m", metavar="PATH[:CONTAINER_PATH]", action="append", default=[],
        help="Mount a host directory (repeatable). Bare path maps to same path inside container.",
    )
    parser.add_argument(
        "--skills", action="store_true",
        help="Mount workspace skills/ dir (ro) → /workspace/skills/",
    )
    parser.add_argument(
        "--mcp", action="store_true",
        help="Mount workspace .mcp.json (ro) → /workspace/.mcp.json",
    )
    parser.add_argument(
        "--creds", action="store_true",
        help="Mount CLI credentials from data/home/<creds_dir>/ → /home/ubuntu/<creds_dir>/",
    )
    parser.add_argument(
        "--add-dir", metavar="PATH[:CONTAINER_PATH]", action="append", default=[],
        dest="add_dirs",
        help="Mount dir AND pass --add-dir to the CLI (claude only). Repeatable.",
    )
    parser.add_argument(
        "--env-file", metavar="FILE",
        help="Pass an env file to docker (--env-file). Overrides --forward-env.",
    )
    parser.add_argument(
        "--forward-env", action="store_true", default=True,
        help="Forward known API keys from current env (default: true). Use --no-forward-env to disable.",
    )
    parser.add_argument("--no-forward-env", dest="forward_env", action="store_false")
    parser.add_argument("--model", help="Model override passed to the CLI (e.g. claude-opus-4-8)")
    parser.add_argument("--tag", default=DEFAULT_TAG, help=f"Image tag (default: {DEFAULT_TAG})")
    parser.add_argument("--image", help="Override full image name (skips registry/prefix/tag)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the docker command without executing it",
    )
    parser.add_argument(
        "extra", nargs=argparse.REMAINDER,
        help="Extra args appended verbatim to the CLI invocation",
    )

    args = parser.parse_args()

    argv = build_docker_argv(
        cli=args.cli,
        prompt=args.prompt,
        mounts=args.mount,
        skills=args.skills,
        mcp=args.mcp,
        creds=args.creds,
        add_dirs=args.add_dirs,
        env_file=args.env_file,
        forward_env=args.forward_env,
        model=args.model,
        extra_args=args.extra,
        tag=args.tag,
        image_override=args.image,
    )

    if args.dry_run:
        import shlex
        print(shlex.join(argv))
        return 0

    try:
        result = subprocess.run(argv, check=False)
        return result.returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
