"""WhatsApp Cloud API webhook receiver for the Crispal store.

Unlike Facebook/Instagram (Graph API supports pulling full conversation
history on demand via /{page_id}/conversations), WhatsApp Cloud API only
pushes messages via webhook — there is no equivalent pull endpoint. This
router is therefore the only way inbound WhatsApp messages ever reach us;
they're persisted to CrispalWhatsappMessage (see models.py) so
scripts/crispal_watch_check.py can find unanswered conversations the same
way it already does for Facebook/Instagram.

The verify token and access token are stored in the `settings` table (see
core/security.py get_setting/set_setting) rather than env vars, so both this
process and the aw_crispal stdio MCP (which talks to the same Postgres DB)
share one source of truth without needing the same env everywhere.
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse

from ..core.security import get_setting
from ..db import session_scope
from ..models import CrispalWhatsappMessage

router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])


@router.get("/webhook")
def verify_webhook(
    hub_mode: str = Query(alias="hub.mode", default=""),
    hub_verify_token: str = Query(alias="hub.verify_token", default=""),
    hub_challenge: str = Query(alias="hub.challenge", default=""),
):
    expected = get_setting("crispal_whatsapp_verify_token", "")
    if not expected or hub_verify_token != expected:
        return PlainTextResponse("Verification token mismatch", status_code=403)
    return PlainTextResponse(hub_challenge)


@router.post("/webhook")
async def receive_webhook(request: Request):
    payload = await request.json()
    stored = 0
    with session_scope() as s:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                contacts = {c.get("wa_id"): c.get("profile", {}).get("name", "")
                            for c in value.get("contacts", [])}
                for msg in value.get("messages", []):
                    if msg.get("id") and s.query(CrispalWhatsappMessage).filter(
                        CrispalWhatsappMessage.wa_message_id == msg["id"]
                    ).first():
                        continue  # Meta retries webhooks; de-dupe on wa_message_id.
                    from_number = msg.get("from", "")
                    text = ""
                    media_url = None
                    msg_type = msg.get("type", "")
                    if msg_type == "text":
                        text = msg.get("text", {}).get("body", "")
                    elif msg_type in ("image", "video", "audio", "document", "sticker"):
                        media = msg.get(msg_type, {})
                        media_url = media.get("id")  # media id — needs a follow-up GET to resolve a URL
                        text = media.get("caption", "")
                    s.add(CrispalWhatsappMessage(
                        wa_message_id=msg.get("id"),
                        direction="in",
                        from_number=from_number,
                        contact_name=contacts.get(from_number, ""),
                        text=text,
                        media_url=media_url,
                        raw=msg,
                    ))
                    stored += 1
    return {"received": stored}
