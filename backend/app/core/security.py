"""Security gate for command execution + claude CLI subshell.

Resolves the **effective** security mode at run-time by walking:

  agent.params.security_mode  ─→  if set, use it
        ↓ else
  Setting "security_mode"     ─→  global default (defaults to "insecure")

Modes:
  * ``insecure`` — historic behavior. ``code.run_command`` runs anything
    EXCEPT entries on the deny-list (deny-list is always enforced).
    claude CLI subshell gets ``--dangerously-skip-permissions``.
  * ``secure``   — ``code.run_command`` only runs commands whose first
    token (after stripping leading env-var assignments and `time`/`nohup`)
    matches a prefix on the effective allow-list. claude CLI subshell
    runs without ``--dangerously-skip-permissions`` and with
    ``--disallowed-tools Bash`` so the inner agent cannot shell out.

Per-agent overrides also include ``params.command_allowlist`` which, when
non-empty, *replaces* the global allow-list for that agent (so e.g. an
``app-verifier`` can be locked to just ``npm test`` / ``pytest``).
"""
from __future__ import annotations

import re
import shlex
from typing import Any, Optional

from ..db import session_scope
from ..models import Setting

# --------------------------------------------------------------------------
# Defaults — used when no row exists in the `settings` table
# --------------------------------------------------------------------------

DEFAULT_COMMAND_TIMEOUT = 300   # 5 min (was 60s)
DEFAULT_SECURITY_MODE = "insecure"

# Safe-ish read-only commands. Prefix match on the *first command word*
# after stripping `env VAR=x` prefixes / `nohup` / `time`.
DEFAULT_ALLOWLIST: list[str] = [
    "ls", "pwd", "cat", "head", "tail", "wc", "find", "grep", "rg",
    "echo", "test", "which", "stat", "file", "diff",
    "git status", "git log", "git diff", "git show", "git branch",
    "git rev-parse", "git ls-files",
    "npm list", "npm ls", "pip list", "pip show", "cargo tree",
    "node --version", "python --version", "python3 --version",
    "uname", "whoami", "date",
]

# Catastrophic footguns — always blocked, even in `insecure` mode.
# Patterns are matched against the full raw command string (case-insensitive).
DEFAULT_DENYLIST: list[str] = [
    r"\brm\s+-rf\s+/(?:\s|$)",        # rm -rf /
    r"\brm\s+-rf\s+--no-preserve-root",
    r"\bsudo\b",
    r"\bmkfs(\.|\s|$)",
    r"\bdd\s+if=.*of=/dev/",
    r"\bchmod\s+777\s+/",
    r":\(\)\s*\{[^}]*\}\s*;\s*:",     # fork bomb :(){:|:&};:
    r"\bcurl\b[^|]*\|\s*(?:sh|bash|zsh)\b",
    r"\bwget\b[^|]*\|\s*(?:sh|bash|zsh)\b",
    r"\bkill\s+-9\s+1\b",
    r">\s*/dev/sd[a-z]",
    r"\bshutdown\b",
    r"\breboot\b",
]


class CommandBlocked(Exception):
    """Raised by ``check_command`` when a command is denied by policy."""
    def __init__(self, reason: str, list_kind: str = "deny", entry: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.list_kind = list_kind   # "deny" | "allow"
        self.entry = entry


# --------------------------------------------------------------------------
# Setting helpers
# --------------------------------------------------------------------------

def get_setting(key: str, default: Any = None) -> Any:
    with session_scope() as s:
        row = s.query(Setting).filter(Setting.key == key).first()
        if row is None:
            return default
        return row.value


def set_setting(key: str, value: Any) -> None:
    with session_scope() as s:
        row = s.query(Setting).filter(Setting.key == key).first()
        if row is None:
            row = Setting(key=key, value=value)
            s.add(row)
        else:
            row.value = value


def all_settings() -> dict:
    """Return current effective settings (DB → defaults)."""
    return {
        "command_timeout_seconds": int(get_setting("command_timeout_seconds", DEFAULT_COMMAND_TIMEOUT)),
        "security_mode":           str(get_setting("security_mode",           DEFAULT_SECURITY_MODE)),
        "command_allowlist":       list(get_setting("command_allowlist",      DEFAULT_ALLOWLIST)),
        "command_denylist":        list(get_setting("command_denylist",       DEFAULT_DENYLIST)),
    }


def reset_settings() -> dict:
    """Delete every override — fall back to compiled defaults."""
    with session_scope() as s:
        for key in ("command_timeout_seconds", "security_mode",
                    "command_allowlist", "command_denylist"):
            row = s.query(Setting).filter(Setting.key == key).first()
            if row is not None:
                s.delete(row)
    return all_settings()


# --------------------------------------------------------------------------
# Effective mode + lists for one agent (or for an ad-hoc tool call)
# --------------------------------------------------------------------------

def effective_for_agent(agent_params: dict | None) -> dict:
    """Resolve mode + lists for an agent run. ``agent_params`` is the
    agent's ``params`` dict (may carry ``security_mode``,
    ``command_allowlist``)."""
    cfg = all_settings()
    params = agent_params or {}

    mode = params.get("security_mode")
    if mode in (None, "", "inherit"):
        mode = cfg["security_mode"]
    if mode not in ("insecure", "secure"):
        mode = "insecure"

    # Per-agent allow-list override REPLACES global allow-list when present.
    agent_allow = params.get("command_allowlist")
    allowlist = (list(agent_allow) if isinstance(agent_allow, list) and agent_allow
                 else list(cfg["command_allowlist"]))

    return {
        "mode": mode,
        "timeout_s": cfg["command_timeout_seconds"],
        "allowlist": allowlist,
        "denylist": list(cfg["command_denylist"]),
    }


# --------------------------------------------------------------------------
# Command gate
# --------------------------------------------------------------------------

_PREFIX_NOISE = {"nohup", "time", "exec", "sudo"}   # sudo would still be denied below

def _first_meaningful_token(cmd: str) -> str:
    """Strip leading `VAR=val` env assignments / `nohup` / `time` to find the
    actual command being run. Returns the first 1–2 tokens joined so e.g.
    ``git status`` matches its allow-list entry."""
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        # Unclosed quotes etc — fall back to whitespace split
        tokens = cmd.split()
    # drop leading VAR=val pairs
    while tokens and "=" in tokens[0] and not tokens[0].startswith("="):
        tokens.pop(0)
    # drop nohup/time/exec prefixes
    while tokens and tokens[0] in _PREFIX_NOISE:
        tokens.pop(0)
    if not tokens:
        return ""
    head = tokens[0]
    # For "git status", "npm list" etc — combine first two if second looks like a subcommand
    if len(tokens) >= 2 and re.fullmatch(r"[a-z][\w:-]*", tokens[1]):
        return f"{head} {tokens[1]}"
    return head


def check_command(cmd: str, *, mode: str, allowlist: list[str], denylist: list[str]) -> None:
    """Raises ``CommandBlocked`` if the command is forbidden. Returns None
    when the command may proceed."""
    if not cmd or not cmd.strip():
        raise CommandBlocked("empty command")

    # 1. Deny-list — ALWAYS enforced, even in insecure mode.
    for pat in denylist:
        try:
            if re.search(pat, cmd, re.IGNORECASE):
                raise CommandBlocked(
                    f"command matches deny-list pattern {pat!r}",
                    list_kind="deny", entry=pat)
        except re.error:
            # Malformed pattern — fall back to substring match
            if pat.lower() in cmd.lower():
                raise CommandBlocked(
                    f"command contains denied substring {pat!r}",
                    list_kind="deny", entry=pat)

    # 2. Insecure mode → done (deny-list was the only gate).
    if mode == "insecure":
        return

    # 3. Secure mode → must match allow-list (prefix match against first token(s)).
    head = _first_meaningful_token(cmd)
    head_l = head.lower()
    for entry in allowlist:
        e = entry.strip().lower()
        if not e:
            continue
        # accept exact match OR head startswith allow-list entry (e.g. "git" allows "git status")
        if head_l == e or head_l.startswith(e + " ") or (head_l + " ").startswith(e + " "):
            return
        # Also match against the raw cmd start (handles allowlist entries that
        # already include arg fragments, like "npm test")
        if cmd.lower().startswith(e + " ") or cmd.lower() == e:
            return
    raise CommandBlocked(
        f"command {head!r} is not on the allow-list (secure mode). "
        f"Add it to settings or use an agent with security_mode='insecure'.",
        list_kind="allow", entry=head)
