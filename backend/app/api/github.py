"""GitHub webhook receiver — handles issue events from GitHub.

Configure the webhook in GitHub repo settings:
  URL: https://your-domain/api/github/webhook
  Content type: application/json
  Secret: value of the github_webhook_secret setting
  Events: Issues
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..db import get_session
from ..core import security
from ..core.cancel import mark_cancelled

router = APIRouter(prefix="/api/github", tags=["github"])
logger = logging.getLogger(__name__)


def _verify_signature(payload: bytes, sig_header: str | None, secret: str) -> bool:
    if not secret:
        return True  # no secret configured → allow all
    if not sig_header or not sig_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig_header[7:], expected)


@router.post("/webhook")
async def github_webhook(request: Request, s: Session = Depends(get_session)):
    """Receive GitHub webhook events and act on them.

    Supported events:
    - issues.closed: if the issue has a run linked (type:agent-run label),
      cancel the matching run by looking up github_issue_number in the DB.
    """
    payload = await request.body()
    event = request.headers.get("X-GitHub-Event", "")
    sig = request.headers.get("X-Hub-Signature-256", "")

    secret = security.get_setting("github_webhook_secret", "") or ""
    if not _verify_signature(payload, sig, secret):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        body = json.loads(payload)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    action = body.get("action")
    issue = body.get("issue", {})
    issue_number = issue.get("number")

    if event == "issues" and action == "closed" and issue_number:
        from ..models import Run
        runs = s.query(Run).filter(
            Run.github_issue_number == issue_number,
            Run.status.in_(["pending", "running"]),
        ).all()

        if runs:
            all_cancelled_ids: list[str] = []

            def _cancel(run: Run) -> None:
                if run.status in ("success", "error", "cancelled"):
                    return
                run.status = "cancelled"
                run.ended_at = datetime.utcnow()
                all_cancelled_ids.append(run.id)
                for child in s.query(Run).filter(Run.parent_run_id == run.id).all():
                    _cancel(child)

            for run in runs:
                logger.info("GitHub issue #%s closed → cancelling run %s",
                            issue_number, run.id)
                _cancel(run)

            s.commit()
            mark_cancelled(*all_cancelled_ids)

    return {"ok": True, "event": event, "action": action}


@router.post("/test")
async def test_github_connection():
    """Test GitHub CLI authentication."""
    from ..core.github_sync import test_gh_connection
    return await test_gh_connection()
