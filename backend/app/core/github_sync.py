"""GitHub Issues sync — mirrors Targets and Runs to GitHub Issues.

Uses the `gh` CLI (at /usr/bin/gh) via asyncio subprocess.
All public functions are fire-and-forget: callers should wrap in asyncio.create_task().
If gh CLI fails or GitHub sync is disabled, log and return silently —
never let GitHub sync failures break the platform.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from .security import get_setting

logger = logging.getLogger(__name__)


def _gh_sync_enabled() -> bool:
    return bool(get_setting("github_sync_enabled", False))


def _gh_repo() -> Optional[str]:
    return get_setting("github_repo", None) or None


async def _run_gh(*args) -> Optional[str]:
    """Run a gh CLI command asynchronously. Returns stdout or None on failure."""
    if not _gh_sync_enabled() or not _gh_repo():
        return None
    repo = _gh_repo()
    cmd = ["gh", *args, "--repo", repo]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            logger.warning("gh CLI failed: %s\nstderr: %s", " ".join(cmd), stderr.decode())
            return None
        return stdout.decode().strip()
    except Exception as e:
        logger.warning("gh CLI exception: %s", e)
        return None


async def _ensure_labels() -> None:
    """Create required labels if they don't exist yet (lazy, idempotent)."""
    labels = [
        ("type:target", "0075ca", "Agents platform target"),
        ("type:agent-run", "e4e669", "Agents platform agent run"),
        ("type:workflow-run", "e4e669", "Agents platform workflow run"),
        ("status:ready", "ededed", "Run is ready to start"),
        ("status:running", "fbca04", "Run is currently executing"),
        ("status:done", "0e8a16", "Run completed successfully"),
        ("status:failed", "d73a4a", "Run failed"),
        ("status:cancelled", "cccccc", "Run was cancelled"),
    ]
    for name, color, description in labels:
        await _run_gh("label", "create", name,
                      "--color", color, "--description", description, "--force")


async def _ensure_label(name: str, color: str = "ededed") -> None:
    """Create a single label idempotently."""
    await _run_gh("label", "create", name, "--color", color, "--force")


async def create_target_issue(
    target_slug: str,
    target_name: str,
    description: str,
    tags: list[str],
) -> Optional[int]:
    """Create a GitHub Issue for a Target. Returns the issue number or None."""
    if not _gh_sync_enabled():
        return None
    await _ensure_labels()

    body = f"""## Goal
{target_name}

## Description
{description or "No description provided."}

## Platform Details
- **Target Slug:** `{target_slug}`
- **Tags:** {", ".join(f"`{t}`" for t in tags) if tags else "none"}

## Runs
*Runs will appear here as agents execute.*

---
*Managed by [Agents Platform](http://localhost:9123)*"""

    # gh issue create outputs the issue URL as plain text (e.g. https://github.com/owner/repo/issues/42)
    result = await _run_gh("issue", "create",
        "--title", f"[Target] {target_name}",
        "--body", body,
        "--label", "type:target,status:ready",
    )
    if result:
        try:
            return int(result.rstrip("/").split("/")[-1])
        except Exception:
            logger.warning("Could not parse issue number from: %s", result)
            return None
    return None


async def create_run_issue(
    run_id: str,
    agent_slug: str,
    model_slug: Optional[str],
    target_issue_number: Optional[int],
    target_name: str,
    input_summary: str,
) -> Optional[int]:
    """Create a GitHub Issue for a Run. Returns the issue number or None."""
    if not _gh_sync_enabled():
        return None

    await _ensure_labels()
    model_short = (model_slug or "unknown").split("-")[-1] if model_slug else "unknown"

    # Create dynamic labels for agent and model
    await _ensure_label(f"agent:{agent_slug}", "5319e7")
    if model_slug:
        await _ensure_label(f"model:{model_short}", "bfd4f2")

    body = f"""## Task
{input_summary[:500]}{"..." if len(input_summary) > 500 else ""}

## Run Details
- **Run ID:** `{run_id}`
- **Agent:** `{agent_slug}`
- **Model:** `{model_slug or "default"}`
- **Status:** 🔄 Running
{"- **Parent Target:** #" + str(target_issue_number) if target_issue_number else ""}

---
*Managed by [Agents Platform](http://localhost:9123)*"""

    labels = ["type:agent-run", "status:running", f"agent:{agent_slug}"]
    if model_slug:
        labels.append(f"model:{model_short}")

    result = await _run_gh("issue", "create",
        "--title", f"[Run] {agent_slug}: {input_summary[:60]}{'...' if len(input_summary) > 60 else ''}",
        "--body", body,
        "--label", ",".join(labels),
    )
    if result:
        try:
            return int(result.rstrip("/").split("/")[-1])
        except Exception:
            logger.warning("Could not parse issue number from: %s", result)
            return None
    return None


async def _load_run_outcome(run_id: str) -> dict:
    """Load run output, artefacts and any canvas/PR references from the DB.
    Returns a dict with keys: output_text, artefacts, canvas_ids, pr_urls.
    Safe to call — never raises; returns empty values on any error.
    """
    result = {"output_text": "", "artefacts": [], "canvas_ids": [], "pr_urls": []}
    try:
        from ..db import session_scope
        from ..models import Run as _Run, RunArtefact as _RunArtefact
        with session_scope() as s:
            r = s.query(_Run).filter(_Run.id == run_id).first()
            if r:
                # --- extract readable output text ---
                out = r.output or {}
                if isinstance(out, dict):
                    # Common keys agents use for their main response
                    text = (out.get("text") or out.get("summary") or
                            out.get("analysis") or out.get("output") or
                            out.get("result") or out.get("findings") or "")
                    if not text and out:
                        # Fall back to a compact JSON representation
                        text = json.dumps(out, indent=2, default=str)
                    result["output_text"] = str(text)[:2500]

                    # Canvas IDs that agents surface in their output
                    for key in ("canvas_id", "plan_canvas_id", "report_canvas_id",
                                "canvases", "canvas_ids"):
                        val = out.get(key)
                        if isinstance(val, str) and val:
                            result["canvas_ids"].append(val)
                        elif isinstance(val, list):
                            result["canvas_ids"].extend(v for v in val if isinstance(v, str))

                    # PR URLs the agent recorded in output
                    for key in ("pr_url", "pr_urls", "pull_request_url"):
                        val = out.get(key)
                        if isinstance(val, str) and val:
                            result["pr_urls"].append(val)
                        elif isinstance(val, list):
                            result["pr_urls"].extend(v for v in val if isinstance(v, str))

            # --- collect artefacts (text + binary) ---
            artefacts = s.query(_RunArtefact).filter(_RunArtefact.run_id == run_id).all()
            for a in artefacts:
                result["artefacts"].append({
                    "name": a.name,
                    "mime": a.mime,
                    "size": a.size,
                    "is_binary": a.is_binary,
                })
                # Canvas artefacts often have "canvas" in the name
                if "canvas" in a.name.lower() and not a.is_binary:
                    pass  # content stays in the platform; just surface the name
    except Exception as e:
        logger.debug("_load_run_outcome failed for run %s: %s", run_id, e)
    return result


async def update_run_issue(
    issue_number: int,
    status: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    error: Optional[str] = None,
    pr_url: Optional[str] = None,
    run_id: Optional[str] = None,
) -> None:
    """Update a run's GitHub Issue when it completes.

    Posts a rich outcome comment (output, artefacts, canvases, PR link, errors)
    before updating the issue body/labels and closing it.
    """
    if not _gh_sync_enabled() or not issue_number:
        return

    status_emoji = {"success": "✅", "error": "❌", "cancelled": "⏹️"}.get(status, "❓")
    status_label = {
        "success": "status:done",
        "error": "status:failed",
        "cancelled": "status:cancelled",
    }.get(status, "status:done")
    await _ensure_label(status_label)

    # --- Load enriched outcome from DB ----------------------------------------
    outcome: dict = {}
    if run_id:
        outcome = await _load_run_outcome(run_id)

    output_text: str = outcome.get("output_text", "")
    artefacts: list = outcome.get("artefacts", [])
    canvas_ids: list = outcome.get("canvas_ids", [])
    # Merge PR URLs: explicit param takes precedence, then what the agent recorded
    all_pr_urls: list[str] = []
    if pr_url:
        all_pr_urls.append(pr_url)
    all_pr_urls.extend(u for u in outcome.get("pr_urls", []) if u not in all_pr_urls)

    # --- Build outcome comment ------------------------------------------------
    lines: list[str] = [
        f"## {status_emoji} Outcome — {status.capitalize()}",
        "",
        f"**Tokens:** {tokens_in:,} in / {tokens_out:,} out &nbsp;·&nbsp; **Cost:** ${cost_usd:.4f}",
    ]

    if all_pr_urls:
        lines += ["", "### Pull Requests"]
        for url in all_pr_urls:
            lines.append(f"- {url}")

    if error:
        lines += ["", "### Error"]
        lines.append("```")
        lines.append(error[:600])
        lines.append("```")

    if output_text:
        lines += ["", "### Output"]
        lines.append("<details><summary>Expand</summary>")
        lines.append("")
        lines.append("```")
        lines.append(output_text)
        lines.append("```")
        lines.append("</details>")

    if artefacts:
        lines += ["", "### Artefacts"]
        for a in artefacts:
            size_kb = a["size"] / 1024
            icon = "🖼️" if a["mime"].startswith("image/") else ("📊" if "canvas" in a["name"].lower() else "📄")
            lines.append(f"- {icon} `{a['name']}` &nbsp; `{a['mime']}` &nbsp; {size_kb:.1f} kB")

    if canvas_ids:
        lines += ["", "### Canvases"]
        for cid in canvas_ids:
            lines.append(f"- `{cid}`")

    comment_body = "\n".join(lines)

    # Post the outcome comment BEFORE closing the issue
    await _run_gh("issue", "comment", str(issue_number), "--body", comment_body)

    # --- Update issue body (flip status line) ---------------------------------
    current = await _run_gh("issue", "view", str(issue_number), "--json", "body")
    current_body = ""
    if current:
        try:
            current_body = json.loads(current).get("body", "")
        except Exception:
            pass

    new_body = current_body.replace(
        "- **Status:** 🔄 Running",
        f"- **Status:** {status_emoji} {status.capitalize()}",
    )

    await _run_gh("issue", "edit", str(issue_number), "--body", new_body)
    await _run_gh("issue", "edit", str(issue_number),
                  "--remove-label", "status:running",
                  "--add-label", status_label)

    if status in ("success", "error", "cancelled"):
        await _run_gh("issue", "close", str(issue_number))


async def update_target_issue(issue_number: int, status: str, run_summary: str = "") -> None:
    """Update a target's GitHub Issue status label."""
    if not _gh_sync_enabled() or not issue_number:
        return

    status_map = {
        "active": "status:ready",
        "completed": "status:done",
        "cancelled": "status:cancelled",
        "abandoned": "status:failed",
    }
    label = status_map.get(status, "status:ready")
    await _ensure_label(label)
    await _run_gh("issue", "edit", str(issue_number), "--add-label", label)

    if status in ("completed", "cancelled", "abandoned"):
        await _run_gh("issue", "close", str(issue_number))


async def test_gh_connection() -> dict:
    """Test gh CLI auth and repo access. Returns {ok, output}."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "auth", "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        output = (stdout + stderr).decode().strip()
        if proc.returncode == 0:
            return {"ok": True, "output": output}
        return {"ok": False, "output": output or "gh auth status failed"}
    except Exception as e:
        return {"ok": False, "output": str(e)}
