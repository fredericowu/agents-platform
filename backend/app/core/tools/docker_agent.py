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
            "--json",           # structured JSONL events (thread/turn/item.*) — see cli.py astream()
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


def container_name_for_run(run_id: str) -> str:
    """Deterministic docker container name for a run_id — lets /abort and
    /runs/:id/cancel `docker kill` it by name without needing a live handle
    to the subprocess that started it (works across an AP restart too)."""
    return f"aw-run-{run_id}"


# Warm-container mode (AP_WARM_CONTAINER=1, claude CLI only — see
# backend/app/core/warm_pool.py) mounts these two scripts read-only into the
# container instead of running the CLI directly as the container command.
_WARM_SHARED_DIR = BASE_DIR / "repos" / "agents-platform" / "agent-images" / "shared"
WARM_WRAPPER_HOST_PATH = _WARM_SHARED_DIR / "aw-warm-wrapper"
WARM_RELAY_HOST_PATH = _WARM_SHARED_DIR / "aw-warm-relay.py"


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
    warm_mode: bool = False,
    warm_epoch_hash: str | None = None,
    warm_token: str | None = None,
) -> list[str]:
    spec = CLI_SPECS[cli]
    image = image_override or f"{REGISTRY}/{IMAGE_PREFIX}-{cli}:{tag}"

    # Warm mode (AP_WARM_CONTAINER=1, claude only — see backend/app/core/
    # warm_pool.py): the container is DETACHED and long-lived, not --rm/-i.
    # Turns are fed in later via `docker exec` against the FIFO the wrapper
    # script sets up, never via this `docker run`'s own stdin.
    run_flags = ["-d"] if warm_mode else ["--rm", "-i"]

    if share_network:
        # Join aw-sandbox's network namespace directly — 127.0.0.1 then reaches
        # every service that shares that netns (awserv, redis, postgres, the
        # agents-platform backend itself). Docker forbids combining a container
        # network mode with --add-host, so that flag is dropped in this branch.
        argv: list[str] = ["docker", "run", *run_flags,
                           "--network", "container:aw-sandbox"]
    else:
        argv = ["docker", "run", *run_flags,
               "--add-host=host.docker.internal:host-gateway"]

    if warm_mode:
        assert agent_id, "warm_mode requires agent_id (stable container name is aw-warm-<agent_id>)"
        # Stable name (not per-run) — a dispatch reuses this container across
        # many turns/runs. Labeled with the epoch hash so a later dispatch can
        # tell, without any extra process, whether it's still safe to reuse.
        argv.extend(["--name", f"aw-warm-{agent_id}"])
        argv.extend(["--label", "aw.warm=1"])
        argv.extend(["--label", f"aw.agent_id={agent_id}"])
        argv.extend(["--label", f"aw.epoch={warm_epoch_hash or ''}"])
        argv.extend(["--label", f"aw.warm_token={warm_token or ''}"])
    elif run_id:
        # Deterministic container name from run_id lets /runs/:id/cancel target it
        # with `docker kill` even when it has no locally-tracked subprocess handle
        # (e.g. a run recovered after an agents-platform restart — see cli.py kill_run).
        argv.extend(["--name", container_name_for_run(run_id)])

    # ── Volume mounts ──────────────────────────────────────────────────────────
    seen_mounts: set[str] = set()
    # Docker rejects two -v flags targeting the same container path ("Duplicate
    # mount point") even if their host sources differ. Track container paths
    # separately so the FIRST mount to a given container path wins and later
    # callers silently no-op instead of producing a rejected docker invocation.
    seen_containers: set[str] = set()

    def add_mount(host: str, container: str, readonly: bool = False) -> None:
        if container in seen_containers:
            return
        host_abs = str(Path(host).resolve())
        flag = f"{host_abs}:{container}"
        if readonly:
            flag += ":ro"
        if flag not in seen_mounts:
            seen_mounts.add(flag)
            seen_containers.add(container)
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

    # Always mount the aw venv, readonly, at the same absolute path — independent
    # of "workspace access" (which only controls whether the repo *source* is
    # bind-mounted). Agents need `.venv/aw/bin/python` to run workspace tooling
    # (e.g. the agents-platform CLI) even when the repo mount is off. Skipped when
    # BASE_DIR itself is already bind-mounted (workspace access on), since .venv
    # comes along with it — mounting it again would be a harmless but redundant flag.
    base_already_mounted = any(
        Path(parse_mount(raw)[0]).resolve() == BASE_DIR.resolve() for raw in mounts
    )
    venv_host = BASE_DIR / ".venv"
    if not base_already_mounted and venv_host.is_dir():
        add_mount(str(venv_host), str(BASE_DIR / ".venv"), readonly=True)

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

    # Per-agent extra volumes (e.g. ["/var/run/docker.sock:/var/run/docker.sock"]).
    # Applied BEFORE the generic --add-dir mounts below so an explicit permission
    # grant (e.g. tmp_access's real "/tmp" source) wins the container path over a
    # naive same-path add-dir mount — add_mount() is first-wins per container path.
    for vol in (extra_volumes or []):
        if ":" in vol:
            h, c = vol.split(":", 1)
            add_mount(h, c)
        else:
            add_mount(vol, vol)

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
        # codex has no --mcp-config flag — it only reads MCP servers from
        # $CODEX_HOME/config.toml or a layered $CODEX_HOME/<profile>.config.toml
        # (--profile/-p). Bind the generated mcp_codex.toml straight into the
        # mounted .codex creds dir under that profile filename so `-p agentmcp`
        # (added below) picks it up.
        codex_mcp_host = Path(mcp_config_dir) / "mcp_codex.toml"
        if cli == "codex" and codex_mcp_host.is_file():
            add_mount(str(codex_mcp_host.resolve()), "/home/ubuntu/.codex/agentmcp.config.toml", readonly=True)

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

    if warm_mode:
        # No per-turn prompt yet — the container starts one long-lived claude
        # process (fed later, turn by turn, over a FIFO via `docker exec`) so
        # the CLI command here is the WRAPPER script, not the CLI itself.
        # --entrypoint must precede the image name, so this whole branch
        # short-circuits before the ordinary `argv.append(image)` below.
        assert cli == "claude", "warm_mode is only supported for the claude CLI"
        # Mounted into /usr/local/bin/ (like the baked-in aw-connector*
        # scripts) rather than a fresh /aw/ path — bind-mounting individual
        # files into a not-yet-existing directory creates that directory as
        # root, and the image runs as USER ubuntu (see agent-images/claude/
        # Dockerfile), so a root-owned /aw/ would block mkfifo etc. The
        # wrapper itself creates its runtime dir under $HOME (owned by ubuntu).
        argv.extend(["-v", f"{WARM_WRAPPER_HOST_PATH}:/usr/local/bin/aw-warm-wrapper:ro"])
        argv.extend(["-v", f"{WARM_RELAY_HOST_PATH}:/usr/local/bin/aw-warm-relay.py:ro"])
        argv.extend(["--entrypoint", "/usr/local/bin/aw-warm-wrapper"])
        argv.append(image)
        claude_argv = [spec["bin"], "--input-format", "stream-json", *spec["default_extra"]]
        if model and spec["model_flag"]:
            claude_argv.extend([spec["model_flag"], model])
        add_dir_flag = spec.get("add_dir_flag")
        if add_dir_flag:
            for d in add_dir_mounts:
                claude_argv.extend([add_dir_flag, d])
        if mcp_config_dir and (Path(mcp_config_dir) / "mcp.json").exists():
            claude_argv.extend(["--mcp-config", "/agent-config/mcp.json"])
        claude_argv.extend(extra_args)
        # CMD becomes the wrapper's "$@" — the entrypoint above is already
        # the wrapper itself, so no need to name it again here.
        argv.extend(claude_argv)
        return argv

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

    # Claude resumes with a flag that must come before -p; Codex resumes with a
    # subcommand: codex exec resume <thread_id> <prompt>.
    if session_id and cli == "claude":
        argv.extend(["--resume", session_id])
    elif session_id and cli == "codex":
        argv.extend(["resume", session_id])

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
        elif cli == "codex" and (Path(mcp_config_dir) / "mcp_codex.toml").exists():
            # Dead end: codex's -p/--profile only layers scalar settings
            # (model, sandbox, approvals) — verified empirically that a
            # profile-layered mcp_servers table is silently ignored, so the
            # previous "-p agentmcp" approach never actually loaded aw-gateway
            # from the per-run mcp_codex.toml. codex only ever reads
            # mcp_servers from the base $CODEX_HOME/config.toml (shared,
            # generated by `./aw agent sync` via sync_codex_mcp), which
            # already has an "aw-gateway" entry — but hardcoded to
            # http://127.0.0.1:9200/mcp, which is correct for a native
            # (non-docker) codex process on the sandbox host but unreachable
            # from inside this nested container. Override just the URL via
            # -c (works on both fresh and resumed runs, unlike -p). The
            # bearer token itself is forwarded via the MCP_BEARER_AW_GATEWAY
            # env var (see cli.py astream()), matching the base config's
            # existing bearer_token_env_var.
            _gw_host = "127.0.0.1" if share_network else "host.docker.internal"
            argv.extend(["-c", f'mcp_servers.aw-gateway.url="http://{_gw_host}:9123/mcp"'])

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
