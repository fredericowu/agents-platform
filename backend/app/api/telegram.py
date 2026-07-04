"""Telegram bot integration for Agents Platform.

Each TelegramBot row maps a bot token → an AP agent slug.
Inbound webhook → STT → agent run → reply delivery (text/voice + markers).
"""
from __future__ import annotations

import asyncio
import concurrent.futures as _cf
import hmac
import html
import logging
import os
import queue
import re
import tempfile
import threading
import time as _time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_session, session_scope
from ..models import Agent, CrispalConversationSuggestion, Run, RunEvent, Target, TelegramBot, TelegramSession

log = logging.getLogger("ap.telegram")

# Main asyncio event loop — captured on first webhook request so _dispatch
# (which runs in a thread) can schedule coroutines on it instead of creating
# a new event loop via asyncio.run() (cross-loop asyncio.Queue breaks WS streaming).
_MAIN_LOOP: asyncio.AbstractEventLoop | None = None


def _set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _MAIN_LOOP
    if _MAIN_LOOP is None:
        _MAIN_LOOP = loop

router = APIRouter(prefix="/api/telegram", tags=["telegram"])

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
TELEGRAM_FILE_API = "https://api.telegram.org/file/bot{token}/{path}"
MESSAGE_LIMIT = 3800

# ---------------------------------------------------------------------------
# Helpers — Telegram Bot API
# ---------------------------------------------------------------------------

def _tg(token: str, method: str, **kwargs) -> dict:
    url = TELEGRAM_API.format(token=token, method=method)
    r = httpx.post(url, json=kwargs, timeout=30)
    data = r.json()
    if not data.get("ok"):
        raise HTTPException(502, f"Telegram {method} error: {data.get('description')}")
    return data


def _send_message(token: str, chat_id: str, text: str, parse_mode: str = "HTML",
                  reply_markup: dict | None = None) -> None:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        _tg(token, "sendMessage", **payload)
    except HTTPException as e:
        if parse_mode == "HTML":
            # Fallback: strip HTML and retry as plain text
            plain = re.sub(r"<[^>]+>", "", text)
            try:
                _tg(token, "sendMessage", chat_id=chat_id, text=plain)
            except Exception:
                pass
        log.warning("sendMessage failed: %s", e.detail)


def _send_location(token: str, chat_id: str, lat: float, lon: float, label: str = "") -> None:
    """Native Telegram location bubble — a tappable map thumbnail, the same
    UX as WhatsApp's location share. No mini-app/HTML needed; Telegram
    renders and hosts the map itself. ``label`` (e.g. the reverse-geocoded
    address) rides along as a plain text bubble right after — Telegram's
    sendLocation has no caption field of its own."""
    try:
        _tg(token, "sendLocation", chat_id=chat_id, latitude=lat, longitude=lon)
    except Exception as e:
        log.warning("sendLocation failed: %s", e)
        return
    if label:
        _send_message(token, chat_id, label, parse_mode="HTML")


def _send_voice(token: str, chat_id: str, ogg_bytes: bytes, caption: str = "") -> None:
    import io
    files = {"voice": ("voice.ogg", io.BytesIO(ogg_bytes), "audio/ogg")}
    data: dict[str, Any] = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1024]
        data["parse_mode"] = "HTML"
    r = httpx.post(
        TELEGRAM_API.format(token=token, method="sendVoice"),
        data=data, files=files, timeout=60,
    )
    resp = r.json()
    if not resp.get("ok"):
        raise HTTPException(502, f"sendVoice error: {resp.get('description')}")


def _send_photo(token: str, chat_id: str, file_path: str, caption: str = "") -> None:
    with open(file_path, "rb") as f:
        files = {"photo": f}
        data: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption[:1024]
        r = httpx.post(
            TELEGRAM_API.format(token=token, method="sendPhoto"),
            data=data, files=files, timeout=60,
        )
    resp = r.json()
    if not resp.get("ok"):
        raise HTTPException(502, f"sendPhoto error: {resp.get('description')}")


def _send_document(token: str, chat_id: str, file_path: str, caption: str = "") -> None:
    fname = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        files = {"document": (fname, f)}
        data: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption[:1024]
        r = httpx.post(
            TELEGRAM_API.format(token=token, method="sendDocument"),
            data=data, files=files, timeout=60,
        )
    resp = r.json()
    if not resp.get("ok"):
        raise HTTPException(502, f"sendDocument error: {resp.get('description')}")


def _send_options(token: str, chat_id: str, question: str, options: list[str]) -> None:
    keyboard = [[{"text": opt, "callback_data": f"ap_opt:{i}:{opt[:32]}"}]
                for i, opt in enumerate(options)]
    _send_message(token, chat_id, question,
                  reply_markup={"inline_keyboard": keyboard})


def _answer_callback_query(token: str, cq_id: str, text: str = "") -> None:
    try:
        _tg(token, "answerCallbackQuery", callback_query_id=cq_id, text=text)
    except Exception:
        pass


def _edit_message_text(token: str, chat_id: str, message_id: int,
                       text: str, parse_mode: str = "HTML") -> None:
    try:
        _tg(token, "editMessageText", chat_id=chat_id, message_id=message_id,
            text=text, parse_mode=parse_mode,
            reply_markup={"inline_keyboard": []})
    except Exception:
        pass


def _send_chat_action(token: str, chat_id: str, action: str = "typing") -> None:
    try:
        _tg(token, "sendChatAction", chat_id=chat_id, action=action)
    except Exception:
        pass


def _send_button_message(token: str, chat_id: str, text: str, label: str,
                         url: str, web_app: bool = True) -> tuple[int | None, bool]:
    """Send a message with a single inline button; return (message_id, used_web_app).

    Used for the live "View Progress" button whose label carries the run
    lifecycle state ([processing] → [done] / [error] / [cancelled]). Prefers a
    Telegram Mini App (web_app) button so the progress view opens inside
    Telegram; falls back to a plain url button (opens in the browser) if the
    bot/domain isn't set up for web apps.
    """
    def _send(btn: dict) -> int | None:
        data = _tg(token, "sendMessage", chat_id=chat_id, text=text,
                   parse_mode="HTML", reply_markup={"inline_keyboard": [[btn]]})
        return (data.get("result") or {}).get("message_id")

    if web_app:
        try:
            return _send({"text": label, "web_app": {"url": url}}), True
        except Exception:
            log.warning("progress web_app button failed; falling back to url", exc_info=True)
    try:
        return _send({"text": label, "url": url}), False
    except Exception:
        log.warning("progress button sendMessage failed", exc_info=True)
        return None, False


def _edit_button_label(token: str, chat_id: str, message_id: int,
                       label: str, url: str, web_app: bool = False) -> None:
    """Update an inline button's label in place (e.g. [processing] → [done])."""
    btn = {"text": label, "web_app": {"url": url}} if web_app else {"text": label, "url": url}
    try:
        _tg(token, "editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
            reply_markup={"inline_keyboard": [[btn]]})
    except Exception:
        log.debug("edit progress button failed", exc_info=True)


# ---------------------------------------------------------------------------
# Live progress mini-app — a faithful port of the AW WorkspaceAgent /progress
# view (expandable per-step timeline with tool-call details, thinking, output).
# Served publicly (Caddy whitelists /api/telegram/progress/*) so the Telegram
# Mini App opens without the dashboard's aw_jwt cookie; it reads run events from
# a public, run-id-scoped feed.
# ---------------------------------------------------------------------------

@router.get("/progress/{run_id}", include_in_schema=False)
def progress_page(run_id: str) -> HTMLResponse:
    return HTMLResponse(_PROGRESS_HTML.replace("__RUN_ID__", run_id))


@router.get("/progress/{run_id}/events", include_in_schema=False)
def progress_events(run_id: str, s: Session = Depends(get_session)) -> dict:
    run = s.query(Run).filter(Run.id == run_id).first()
    if not run:
        return {"status": "not_found", "events": []}
    evs = (s.query(RunEvent)
           .filter(RunEvent.run_id == run_id)
           .order_by(RunEvent.ts)
           .all())
    return {
        "status": run.status,
        # started_at is stored as a naive UTC datetime (models._now() ==
        # datetime.utcnow()). datetime.timestamp() assumes naive values are in
        # local time, so on a non-UTC host it silently skews the epoch by the
        # host's UTC offset — pushing the mini app's timer's t0 into the future
        # and freezing it at "0s" (Math.max(0, ...) clamp). Pin tzinfo=utc so
        # the conversion is correct regardless of process timezone.
        "started_at": run.started_at.replace(tzinfo=timezone.utc).timestamp() if run.started_at else None,
        "events": [
            {"kind": e.kind, "node_id": e.node_id, "payload": e.payload or {}}
            for e in evs
        ],
    }


# ---------------------------------------------------------------------------
# Helpers — text formatting
# ---------------------------------------------------------------------------

def _md_to_html(text: str) -> str:
    _code_blocks: list[str] = []
    _inline_codes: list[str] = []

    def _stash_block(m: re.Match) -> str:
        lang = m.group(1).strip()
        code = m.group(2).strip().replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        tag = f'<code class="language-{lang}">' if lang else "<code>"
        _code_blocks.append(f"<pre>{tag}{code}</code></pre>")
        return f"\x00BLK{len(_code_blocks)-1}\x00"

    def _stash_inline(m: re.Match) -> str:
        code = m.group(1).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        _inline_codes.append(f"<code>{code}</code>")
        return f"\x00INL{len(_inline_codes)-1}\x00"

    text = re.sub(r"```([^\n]*)\n?(.*?)```", _stash_block, text, flags=re.DOTALL)
    text = re.sub(r"`([^`\n]+)`", _stash_inline, text)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"\[(.+?)\]\((https?://[^\)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    for i, blk in enumerate(_code_blocks):
        text = text.replace(f"\x00BLK{i}\x00", blk)
    for i, inl in enumerate(_inline_codes):
        text = text.replace(f"\x00INL{i}\x00", inl)
    return text


def _strip_markdown(text: str) -> str:
    text = re.sub(r"```[^\n]*\n?(.*?)```", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = re.sub(r"\[(.+?)\]\(https?://[^\)]+\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    return text


def _chunk_text(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        for sep in ("\n\n", "\n", ". ", " "):
            idx = window.rfind(sep)
            if idx > limit // 2:
                chunks.append(remaining[:idx].rstrip())
                remaining = remaining[idx + len(sep):].lstrip()
                break
        else:
            chunks.append(remaining[:limit])
            remaining = remaining[limit:]
    if remaining:
        chunks.append(remaining)
    return chunks


# ---------------------------------------------------------------------------
# Marker parsing (bracket markers in LLM output)
# ---------------------------------------------------------------------------

_ATTACH_RE = re.compile(
    r"\[\[ATTACH:\s*(?P<path>[^\]\s]+(?:\s[^\]]*?)?)"
    r'(?:\s+caption="(?P<caption>[^"]*)")?\s*\]\]',
    re.IGNORECASE,
)
_OPTIONS_RE = re.compile(
    r'\[\[OPTIONS:\s*q="(?P<q>[^"]*)"(?P<rest>[^\]]*)\]\]',
    re.IGNORECASE,
)
_MINIAPP_RE = re.compile(
    r'\[\[MINIAPP:\s*url=(?P<url>\S+)(?:\s+text="(?P<text>[^"]*)")?\s*\]\]',
    re.IGNORECASE,
)
_LOCATION_RE = re.compile(
    r"\[\[LOCATION:\s*lat=(?P<lat>-?\d+(?:\.\d+)?)\s+lon=(?P<lon>-?\d+(?:\.\d+)?)"
    r'(?:\s+label="(?P<label>[^"]*)")?\s*\]\]',
    re.IGNORECASE,
)
_VOICE_RE = re.compile(r"\[\[VOICE\]\]", re.IGNORECASE)
_TEXT_RE = re.compile(r"\[\[TEXT\]\]", re.IGNORECASE)
_LANG_RE = re.compile(r"\[\[LANG:\s*(\w+)\]\]", re.IGNORECASE)


def _parse_markers(raw: str):
    """Backward-compatible summary parse (unordered lists) — kept for any
    external caller that wants "give me everything found" without caring
    about position. Delivery itself uses `_parse_ordered_blocks` below so
    each bubble goes out in the order the agent actually wrote it."""
    force_voice = bool(_VOICE_RE.search(raw))
    force_text = bool(_TEXT_RE.search(raw))
    lang_m = _LANG_RE.search(raw)
    force_lang = lang_m.group(1).lower() if lang_m else ""

    attachments = []
    for m in _ATTACH_RE.finditer(raw):
        path_part = m.group("path").strip()
        # strip inline caption= if written without quotes
        path_part = re.sub(r'\s+caption=.*$', '', path_part).strip()
        attachments.append({"path": path_part, "caption": m.group("caption") or ""})

    options_list = []
    for m in _OPTIONS_RE.finditer(raw):
        rest = m.group("rest")
        opts = re.findall(r'[a-z]="([^"]*)"', rest)
        options_list.append({"question": m.group("q"), "options": opts})

    mini_apps = []
    for m in _MINIAPP_RE.finditer(raw):
        mini_apps.append({"url": m.group("url"), "text": m.group("text") or "Open"})

    locations = []
    for m in _LOCATION_RE.finditer(raw):
        locations.append({"lat": float(m.group("lat")), "lon": float(m.group("lon")),
                          "label": m.group("label") or ""})

    # Strip all markers from prose
    text = raw
    for pat in (_ATTACH_RE, _OPTIONS_RE, _MINIAPP_RE, _LOCATION_RE, _VOICE_RE, _TEXT_RE, _LANG_RE):
        text = pat.sub("", text)
    text = text.strip()

    return {
        "text": text,
        "force_voice": force_voice,
        "force_text": force_text,
        "force_lang": force_lang,
        "attachments": attachments,
        "options": options_list,
        "mini_apps": mini_apps,
        "locations": locations,
    }


# Content markers — each produces its own Telegram bubble, in the order it
# appears in the agent's reply. (VOICE/TEXT/LANG are modal flags, not content,
# and are scanned/stripped separately — they can appear anywhere.)
_CONTENT_MARKER_RE = re.compile(
    "(?:" + "|".join([
        _ATTACH_RE.pattern, _OPTIONS_RE.pattern, _MINIAPP_RE.pattern, _LOCATION_RE.pattern,
    ]) + ")",
    re.IGNORECASE,
)


def _parse_ordered_blocks(raw: str):
    """Split a reply into an ordered list of delivery blocks, preserving the
    position each marker was written in relative to surrounding prose — so
    "text A, then options, then text B" is delivered as three bubbles in
    that order instead of options-always-first."""
    force_voice = bool(_VOICE_RE.search(raw))
    force_text = bool(_TEXT_RE.search(raw))
    lang_m = _LANG_RE.search(raw)
    force_lang = lang_m.group(1).lower() if lang_m else ""

    body = raw
    for pat in (_VOICE_RE, _TEXT_RE, _LANG_RE):
        body = pat.sub("", body)

    blocks: list[dict] = []
    pos = 0
    for m in _CONTENT_MARKER_RE.finditer(body):
        prose = body[pos:m.start()].strip()
        if prose:
            blocks.append({"kind": "text", "text": prose})

        segment = m.group(0)
        am = _ATTACH_RE.fullmatch(segment)
        om = _OPTIONS_RE.fullmatch(segment)
        mm = _MINIAPP_RE.fullmatch(segment)
        lm = _LOCATION_RE.fullmatch(segment)
        if am:
            path_part = re.sub(r'\s+caption=.*$', '', am.group("path").strip()).strip()
            blocks.append({"kind": "attach", "path": path_part, "caption": am.group("caption") or ""})
        elif om:
            opts = re.findall(r'[a-z]="([^"]*)"', om.group("rest"))
            blocks.append({"kind": "options", "question": om.group("q"), "options": opts})
        elif mm:
            blocks.append({"kind": "miniapp", "url": mm.group("url"), "text": mm.group("text") or "Open"})
        elif lm:
            blocks.append({"kind": "location", "lat": float(lm.group("lat")),
                           "lon": float(lm.group("lon")), "label": lm.group("label") or ""})
        pos = m.end()

    trailing = body[pos:].strip()
    if trailing:
        blocks.append({"kind": "text", "text": trailing})

    return {
        "blocks": blocks,
        "force_voice": force_voice,
        "force_text": force_text,
        "force_lang": force_lang,
    }


# ---------------------------------------------------------------------------
# TTS / STT — provider-configurable, see ../core/voice.py
# ---------------------------------------------------------------------------

from ..core import voice as _voice  # noqa: E402


async def _tts(text: str, lang: str = "") -> bytes:
    """Synthesize speech via the configured provider (openai/edge). Raises on failure."""
    audio = await _voice.synthesize_async(text, language=lang)
    if not audio:
        raise RuntimeError("TTS synthesis returned no audio")
    return audio


def _detect_lang(text: str) -> str:
    """Heuristic language detection — matches the 6 supported TTS voices."""
    sample = text.lower()
    pt = len(re.findall(r"[ãõáéíóúâêîôû]", sample))
    fr = len(re.findall(r"[àâæœùûüÿëî]", sample))
    es = len(re.findall(r"[¿¡ñ]", sample))
    it = len(re.findall(r"\b(che|non|una|del|per|con|sono|questa|quello)\b", sample))
    de = len(re.findall(r"[äöüß]", sample))
    scores = {"pt": pt, "fr": fr, "es": es, "it": it, "de": de}
    best, val = max(scores.items(), key=lambda kv: kv[1])
    return best if val >= 2 else "en"


# ---------------------------------------------------------------------------
# STT — provider-configurable (OpenAI Whisper API / local faster-whisper),
# see ../core/voice.py
# ---------------------------------------------------------------------------

def _transcribe_voice(token: str, file_id: str) -> tuple[str, str]:
    """Download voice file from Telegram and transcribe. Returns (text, lang)."""
    r = httpx.get(
        TELEGRAM_API.format(token=token, method="getFile"),
        params={"file_id": file_id}, timeout=10,
    )
    result = r.json().get("result", {})
    file_path = result.get("file_path", "")
    if not file_path:
        return "", ""

    audio_url = TELEGRAM_FILE_API.format(token=token, path=file_path)
    audio_bytes = httpx.get(audio_url, timeout=30).content
    filename = file_path.rsplit("/", 1)[-1] or "voice.oga"

    result = _voice.transcribe(audio_bytes, filename)
    if result is None:
        return "", ""
    return result


_TELEGRAM_MAX_UPLOAD = 20 * 1024 * 1024  # 20 MB Bot API limit


def _save_telegram_upload(token: str, message: dict) -> str | None:
    """Download document/photo/video/audio attached to a message. Returns local path or None."""
    file_id = filename = ""
    if "document" in message:
        doc = message["document"]
        file_id = doc.get("file_id") or ""
        filename = doc.get("file_name") or "document"
    elif "photo" in message:
        photos = message["photo"]
        if isinstance(photos, list) and photos:
            largest = photos[-1]
            file_id = largest.get("file_id") or ""
            filename = f"{file_id}.jpg"
    elif "video" in message:
        vid = message["video"]
        file_id = vid.get("file_id") or ""
        filename = vid.get("file_name") or f"{file_id}.mp4"
    elif "audio" in message:
        aud = message["audio"]
        file_id = aud.get("file_id") or ""
        filename = aud.get("file_name") or f"{file_id}.mp3"

    if not file_id:
        return None

    try:
        r = httpx.get(TELEGRAM_API.format(token=token, method="getFile"),
                      params={"file_id": file_id}, timeout=10)
        meta = r.json()
        if not meta.get("ok"):
            return None
        result = meta.get("result") or {}
        if result.get("file_size", 0) > _TELEGRAM_MAX_UPLOAD:
            log.warning("upload too large: %s", file_id)
            return None
        remote_path = result.get("file_path") or ""
        if not remote_path:
            return None
        content = httpx.get(TELEGRAM_FILE_API.format(token=token, path=remote_path),
                            timeout=60).content
    except Exception as exc:
        log.warning("upload download failed: %s", exc)
        return None

    from datetime import datetime as _dt
    date_str = _dt.utcnow().strftime("%Y-%m-%d")
    upload_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "..", "..", "..", ".tmp", "telegram-uploads", date_str,
    )
    upload_dir = os.path.normpath(upload_dir)
    os.makedirs(upload_dir, exist_ok=True)
    safe_name = os.path.basename(filename).replace("/", "_").replace("\\", "_") or "file"
    local_path = os.path.join(upload_dir, f"{file_id}_{safe_name}")
    try:
        with open(local_path, "wb") as fh:
            fh.write(content)
    except OSError as exc:
        log.warning("upload write failed: %s", exc)
        return None
    log.info("upload saved %d bytes → %s", len(content), local_path)
    return local_path


# ---------------------------------------------------------------------------
# Agent Picker helpers
# ---------------------------------------------------------------------------

def _list_agents_for_picker() -> list[dict]:
    """Return all non-deleted agents ordered by name."""
    with session_scope() as s:
        from ..models import Agent as _Agent
        rows = (s.query(_Agent)
                .filter(_Agent.deleted_at.is_(None))
                .order_by(_Agent.name)
                .all())
        return [{"slug": r.slug, "name": r.name or r.slug} for r in rows]


def _list_sessions_for_agent(agent_slug: str, limit: int = 8) -> list[dict]:
    """Return recent CliSessions that have at least one Run from agent_slug."""
    with session_scope() as s:
        from ..models import CliSession as _CS, Run as _Run
        rows = (s.query(_CS)
                .join(_Run, _Run.session_id == _CS.session_id)
                .filter(_Run.source_slug == agent_slug)
                .order_by(_CS.updated_at.desc())
                .distinct()
                .limit(limit)
                .all())
        return [{"session_id": r.session_id, "name": r.name or ""} for r in rows]


def _get_agent_slug_for_chat(bot: TelegramBot, bot_id: str, chat_id: str) -> str:
    """Return the effective agent slug for this chat (override > bot default)."""
    with session_scope() as s:
        row = (s.query(TelegramSession)
               .filter(TelegramSession.bot_id == bot_id,
                       TelegramSession.chat_id == chat_id)
               .first())
        if row and row.agent_slug_override:
            return row.agent_slug_override
    return bot.agent_slug or ""


def _set_bot_agent_slug(bot_id: str, agent_slug: str) -> None:
    """Persist the agent for this bot; applies to every chat on the bot.

    Clears any leftover per-chat overrides and resets every chat's active
    session so all conversations start fresh under the new agent.
    """
    with session_scope() as s:
        bot_row = s.query(TelegramBot).filter(TelegramBot.id == bot_id).first()
        if bot_row:
            bot_row.agent_slug = agent_slug
        for row in s.query(TelegramSession).filter(TelegramSession.bot_id == bot_id).all():
            row.agent_slug_override = None
            row.session_id = None


def _set_session_override(bot_id: str, chat_id: str, session_id: str | None) -> None:
    """Switch the active session for this chat (None = start fresh)."""
    with session_scope() as s:
        row = (s.query(TelegramSession)
               .filter(TelegramSession.bot_id == bot_id,
                       TelegramSession.chat_id == chat_id)
               .first())
        if row:
            row.session_id = session_id


def _send_agent_picker(token: str, chat_id: str) -> None:
    """Send the Agent Picker inline keyboard."""
    agents = _list_agents_for_picker()
    if not agents:
        _send_message(token, chat_id, "⚠️ No agents configured in the Agents Platform.")
        return
    keyboard = []
    for ag in agents:
        label = ag["name"][:30]
        cb = f"ap_agent:{ag['slug']}"
        if len(cb) > 64:
            cb = cb[:64]
        keyboard.append([{"text": label, "callback_data": cb}])
    _send_message(
        token, chat_id,
        "🤖 <b>Agent Picker</b>\n\nChoose an agent:",
        reply_markup={"inline_keyboard": keyboard},
    )


def _send_session_picker(token: str, chat_id: str, agent_slug: str, agent_name: str) -> None:
    """Send the Session Picker inline keyboard for the chosen agent."""
    sessions = _list_sessions_for_agent(agent_slug)
    keyboard = []
    for sess in sessions:
        label = sess["name"] or sess["session_id"][:12] + "…"
        label = label[:30]
        cb = f"ap_sess:{sess['session_id']}"
        if len(cb) > 64:
            cb = f"ap_sess:{sess['session_id'][:55]}"
        keyboard.append([{"text": label, "callback_data": cb}])
    keyboard.append([{"text": "➕ New session", "callback_data": "ap_sess:__new__"}])
    _send_message(
        token, chat_id,
        f"✅ Agent set to <b>{_md_to_html(agent_name)}</b>\n\nPick a session:",
        reply_markup={"inline_keyboard": keyboard},
    )


# Per-chat serialization: one active dispatch per (bot_id, chat_id)
_CHAT_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_CHAT_LOCKS_META = threading.Lock()


def _chat_lock(bot_id: str, chat_id: str) -> threading.Lock:
    key = (bot_id, chat_id)
    with _CHAT_LOCKS_META:
        if key not in _CHAT_LOCKS:
            _CHAT_LOCKS[key] = threading.Lock()
        return _CHAT_LOCKS[key]


# Per-chat FIFO queues.
#
# A (bot_id, chat_id) pair maps 1:1 to a single Claude/agent session uuid
# (the value we hand to ``run_agent(session_id=…)`` and resume on the next
# turn). Two runs resuming the *same* uuid concurrently corrupt the session
# transcript, so every inbound message for a chat must run strictly one at a
# time, in arrival order.
#
# A bare ``threading.Lock`` serializes but does NOT preserve order — CPython
# makes no fairness guarantee about which blocked thread wins ``acquire()``,
# and the daemon-thread-per-message model doesn't even start threads in order.
# So instead we funnel each chat's messages through a ``queue.Queue`` drained
# by a single long-lived worker thread: FIFO in, FIFO out, exactly one
# ``_dispatch`` in flight per chat. Different (bot, chat) pairs stay fully
# independent — bot A never blocks bot B on the same chat_id.
_CHAT_QUEUES: dict[tuple[str, str], "queue.Queue[tuple]"] = {}
_CHAT_QUEUES_META = threading.Lock()


def _chat_worker(key: tuple[str, str], q: "queue.Queue[tuple]") -> None:
    """Drain one chat's queue forever, in FIFO order, one dispatch at a time."""
    while True:
        item = q.get()
        try:
            _dispatch(*item)
        except Exception:
            log.exception("tg chat worker: dispatch failed for %s", key)
        finally:
            q.task_done()
            log.debug("tg chat worker: queue depth after dispatch for %s = %d", key, q.qsize())


def _enqueue_dispatch(bot: TelegramBot, chat_id: str, user_id: str,
                      text: str, is_voice: bool, inbound_lang: str) -> None:
    """Append a message to the per-(bot, chat) FIFO queue.

    The chat's worker thread is created lazily on first use. Messages for one
    chat are then processed strictly in arrival order, one at a time — so the
    same session uuid is never resumed by two runs at once.
    """
    key = (bot.id, chat_id)
    with _CHAT_QUEUES_META:
        q = _CHAT_QUEUES.get(key)
        if q is None:
            q = queue.Queue()
            _CHAT_QUEUES[key] = q
            threading.Thread(
                target=_chat_worker, args=(key, q), daemon=True,
                name=f"tg-chatq-{bot.id}-{chat_id}",
            ).start()
    q.put((bot, chat_id, user_id, text, is_voice, inbound_lang, _time.perf_counter()))


# ---------------------------------------------------------------------------
# Target + session helpers
# ---------------------------------------------------------------------------

def _ensure_target(bot_id: str, chat_id: str) -> str:
    """Return (or create) a Target for this (bot, chat). Returns target_id."""
    slug = f"tg-{bot_id}-{chat_id}"
    with session_scope() as s:
        t = s.query(Target).filter(Target.slug == slug).first()
        if t:
            return t.id
        t = Target(
            slug=slug,
            name=f"Telegram {bot_id} / chat {chat_id}",
            source_kind="telegram",
            source_ref=f"{bot_id}:{chat_id}",
        )
        s.add(t)
        s.flush()
        return t.id


def _get_or_create_session(bot_id: str, chat_id: str, target_id: str) -> tuple[str | None, str]:
    """Return (session_id_or_None, tg_session_id) for the given chat."""
    with session_scope() as s:
        row = (s.query(TelegramSession)
               .filter(TelegramSession.bot_id == bot_id,
                       TelegramSession.chat_id == chat_id)
               .first())
        if row:
            return row.session_id, row.id
        row = TelegramSession(
            bot_id=bot_id, chat_id=chat_id,
            session_id=None, target_id=target_id,
        )
        s.add(row)
        s.flush()
        return None, row.id


def _save_session_id(bot_id: str, chat_id: str, session_id: str, token: str | None = None) -> None:
    """Bind ``session_id`` to this chat. When it's a genuinely new session
    (no prior session_id — e.g. right after ``/new``) and the chat has no
    custom /rename, auto-stamp the bot's Telegram display name with the new
    session id so it's visible at a glance which session is active. Pass
    ``token`` to enable this (callers without a token just skip it)."""
    auto_name: str | None = None
    with session_scope() as s:
        row = (s.query(TelegramSession)
               .filter(TelegramSession.bot_id == bot_id,
                       TelegramSession.chat_id == chat_id)
               .first())
        is_new_session = not (row and row.session_id)
        if row:
            row.session_id = session_id
        else:
            s.add(TelegramSession(bot_id=bot_id, chat_id=chat_id,
                                  session_id=session_id))

        # Propagate a user-set /rename onto the CliSession that now backs this
        # chat. /rename is commonly used before the first CLI run creates a
        # session_id, so at that moment TelegramSession.session_id is still
        # None and _apply_rename can only write the name onto Target — never
        # onto CliSession, which is what the /agent session picker reads.
        # Syncing here, whenever a session_id gets (re)bound to this chat,
        # closes that gap.
        tgt = (s.query(Target)
               .filter(Target.slug == f"tg-{bot_id}-{chat_id}")
               .first())
        default_name = f"Telegram {bot_id} / chat {chat_id}"
        if tgt and tgt.name and tgt.name != default_name:
            from ..models import CliSession as _CS
            cli_sess = s.query(_CS).filter(_CS.session_id == session_id).first()
            if cli_sess:
                cli_sess.name = tgt.name
            else:
                s.add(_CS(session_id=session_id, name=tgt.name))
        elif is_new_session and token:
            auto_name = session_id
            if tgt:
                tgt.name = auto_name
            else:
                s.add(Target(slug=f"tg-{bot_id}-{chat_id}", name=auto_name,
                              source_kind="telegram", source_ref=f"{bot_id}:{chat_id}"))
    if auto_name and token:
        _set_bot_display_name(token, auto_name)


def _reset_session(bot_id: str, chat_id: str) -> None:
    with session_scope() as s:
        row = (s.query(TelegramSession)
               .filter(TelegramSession.bot_id == bot_id,
                       TelegramSession.chat_id == chat_id)
               .first())
        if row:
            row.session_id = None


def _reset_target_name(bot_id: str, chat_id: str) -> None:
    """Clear any custom /rename on this chat's Target, back to the
    auto-generated default. Call this alongside _reset_session for a true
    "/new" — otherwise the old custom name lingers on Target and
    _save_session_id re-applies it onto the next (unrelated) CliSession."""
    with session_scope() as s:
        tgt = (s.query(Target)
               .filter(Target.slug == f"tg-{bot_id}-{chat_id}")
               .first())
        if tgt:
            tgt.name = f"Telegram {bot_id} / chat {chat_id}"


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def _deliver_reply(
    token: str,
    chat_id: str,
    raw_text: str,
    inbound_was_voice: bool,
    inbound_lang: str = "",
) -> None:
    parsed = _parse_ordered_blocks(raw_text or "")
    wants_voice = (inbound_was_voice or parsed["force_voice"]) and not parsed["force_text"]
    reply_lang = ""

    for block in parsed["blocks"]:
        kind = block["kind"]

        if kind == "attach":
            path = block["path"]
            caption = block["caption"]
            if not os.path.exists(path):
                log.warning("ATTACH path not found: %s", path)
                continue
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            if ext in ("png", "jpg", "jpeg", "webp", "gif"):
                try:
                    _send_photo(token, chat_id, path, caption)
                except Exception as e:
                    log.warning("send_photo failed: %s", e)
            else:
                try:
                    _send_document(token, chat_id, path, caption)
                except Exception as e:
                    log.warning("send_document failed: %s", e)

        elif kind == "options":
            try:
                _send_options(token, chat_id, block["question"], block["options"])
            except Exception as e:
                log.warning("send_options failed: %s", e)

        elif kind == "location":
            _send_location(token, chat_id, block["lat"], block["lon"], block["label"])

        elif kind == "miniapp":
            try:
                _send_message(
                    token, chat_id,
                    block["text"] or block["url"],
                    reply_markup={"inline_keyboard": [[
                        {"text": "🖥 Open", "web_app": {"url": block["url"]}}
                    ]]},
                )
            except Exception as e:
                log.warning("mini_app send failed: %s", e)

        elif kind == "text":
            text = block["text"]
            if not text or text.lower() in ("(sent)", "(done)", "ok", "okay", "."):
                continue

            if wants_voice:
                if not reply_lang:
                    reply_lang = parsed["force_lang"] or _detect_lang(text) or inbound_lang or "pt"
                try:
                    ogg = (asyncio.run_coroutine_threadsafe(_tts(text, reply_lang), _MAIN_LOOP).result(timeout=30)
                           if _MAIN_LOOP else asyncio.run(_tts(text, reply_lang)))
                    _send_voice(token, chat_id, ogg, caption=_md_to_html(text[:1024]))
                    continue
                except Exception as e:
                    log.warning("TTS failed, falling back to text: %s", e)

            # One Telegram message for the whole reply — only paginated when it
            # exceeds Telegram's length limit. Splitting on every blank line
            # (`_split_into_messages`) used to explode "verbose_replies" runs
            # (which join per-tool-call narration with "\n\n") into a dozen
            # disjointed bubbles that read as cut off / fragmented even though
            # the underlying text was complete — see aw-agent-telegram truncation
            # investigation, 2026-07-03.
            for chunk in _chunk_text(text):
                try:
                    _send_message(token, chat_id, _md_to_html(chunk), parse_mode="HTML")
                except Exception:
                    try:
                        # Telegram rejected the HTML — retry as plain text
                        _send_message(token, chat_id, _strip_markdown(chunk), parse_mode="")
                    except Exception as e:
                        log.warning("send_message failed: %s", e)


async def deliver_recovered_run(run_id: str, output_text: str) -> None:
    """Deliver a recovered run's reply to its originating Telegram chat.

    Called by restart recovery (``executor._reattach_run``). The run finished —
    possibly while the platform was down — and was finalised by replaying its
    Redis Stream, but the original webhook coroutine that would have delivered
    the reply died with the restart. We rebuild the chat context from the Run's
    ``initiator_kind`` / ``initiator_id`` (persisted at dispatch time as
    ``"{bot_id}:{chat_id}"``) and deliver through the SAME path as the live flow,
    guarded by a Redis-backed dedup claim so a reply is never sent twice.

    This is the "delivery follows the stream" piece: recovery is just a consumer
    that resumes reading the durable stream and ships the result — identical to
    what the live dispatcher does after ``run_agent`` returns.
    """
    if not output_text:
        return

    with session_scope() as s:
        run = s.query(Run).filter(Run.id == run_id).first()
        if not run or run.initiator_kind != "telegram" or not run.initiator_id:
            return
        initiator_id = run.initiator_id
        session_id = run.session_id

    bot_id, _, chat_id = initiator_id.rpartition(":")
    if not bot_id or not chat_id:
        log.warning("recovery: unparseable initiator_id=%r run=%s", initiator_id, run_id)
        return

    from ..core.redis_streams import mark_delivered
    if not await mark_delivered(run_id):
        log.info("recovery: run %s already delivered — skipping", run_id)
        return

    with session_scope() as s:
        bot = s.query(TelegramBot).filter(TelegramBot.id == bot_id).first()
        token = bot.token if bot else ""
    if not token:
        log.warning("recovery: no bot token for bot_id=%s run=%s", bot_id, run_id)
        return

    # Recovered replies go out as text (we no longer know the inbound modality);
    # language is auto-detected from the reply text inside _deliver_reply.
    await asyncio.to_thread(_deliver_reply, token, chat_id, output_text, False, "")
    if session_id:
        try:
            _save_session_id(bot_id, chat_id, session_id, token=token)
        except Exception:
            pass
    log.info("recovery: delivered reply for run=%s bot=%s chat=%s", run_id, bot_id, chat_id)


# ---------------------------------------------------------------------------
# Dispatch thread
# ---------------------------------------------------------------------------

def _dispatch(bot: TelegramBot, chat_id: str, user_id: str,
              text: str, is_voice: bool, inbound_lang: str,
              t_enqueue: float | None = None) -> None:
    t_dispatch = _time.perf_counter()
    token = bot.token
    agent_slug = _get_agent_slug_for_chat(bot, bot.id, chat_id)

    if not agent_slug:
        _send_message(token, chat_id,
                      "⚠️ This bot has no agent configured. Use /agent to pick one.")
        return

    # Verify agent exists
    with session_scope() as s:
        agent = s.query(Agent).filter(Agent.slug == agent_slug).first()
        if not agent:
            _send_message(token, chat_id,
                          f"⚠️ Agent <code>{agent_slug}</code> not found. Use /agent to pick another.")
            return

    # Ordering + serialization is owned by the per-chat FIFO worker
    # (_enqueue_dispatch): only that single worker thread ever calls _dispatch
    # for a given (bot, chat), in arrival order. The per-chat lock is kept as an
    # uncontended belt-and-suspenders guard against any future direct caller.
    lock = _chat_lock(bot.id, chat_id)
    with lock:
        t_session_start = _time.perf_counter()
        # Ensure target
        target_id = _ensure_target(bot.id, chat_id)

        # Get last session_id for conversation continuity
        session_id, _ = _get_or_create_session(bot.id, chat_id, target_id)
        t_session_done = _time.perf_counter()

        # Build context header that the agent receives
        header = (
            f"/aw-agent-telegram\n"
            f"CONTEXT:\n"
            f"- source: telegram\n"
            f"- chat_id: {chat_id}\n"
            f"- user_id: {user_id}\n"
            f"- bot_id: {bot.id}\n"
        )
        if is_voice:
            full_input = header + f"USER_MESSAGE:\n[VOICE] {text}"
        else:
            full_input = header + f"USER_MESSAGE:\n{text}"

        # Pre-generate run_id so we can share the progress link immediately
        run_id = str(uuid4())
        ap_url = os.environ.get("AP_PUBLIC_URL", "https://agents-platform.app.aw.tekflox.com")
        # Mini App progress view (faithful port of the AW WorkspaceAgent view).
        progress_url = f"{ap_url}/api/telegram/progress/{run_id}"

        # Progress button — the label carries the lifecycle state, mirroring the
        # AW WorkspaceAgent: [processing] → [done] / [error] / [cancelled].
        t_button_start = _time.perf_counter()
        _send_chat_action(token, chat_id)
        proc_msg_id, proc_web_app = _send_button_message(
            token, chat_id, "⚡ Processing…",
            "📊 View Progress [processing]", progress_url, web_app=True)
        t_button_done = _time.perf_counter()

        _stop_typing = threading.Event()

        def _typing_loop() -> None:
            while not _stop_typing.wait(timeout=4):
                _send_chat_action(token, chat_id)

        typing_thread = threading.Thread(target=_typing_loop, daemon=True)
        typing_thread.start()

        final_state = "error"  # button label fallback if we never reach success
        t_agent_start = _time.perf_counter()
        try:
            from ..core.executor import run_agent

            _coro = run_agent(
                agent_slug,
                full_input,
                run_id=run_id,
                target_id=target_id,
                session_id=session_id,
                initiator_kind="telegram",
                initiator_id=f"{bot.id}:{chat_id}",
            )
            log.info("dispatch: _MAIN_LOOP=%s run_id=%s agent=%s", _MAIN_LOOP, run_id, agent_slug)
            if _MAIN_LOOP is not None:
                # Schedule on the main event loop so asyncio.Queue (used by
                # CliLLM WS streaming) is created and consumed in the same loop.
                future = asyncio.run_coroutine_threadsafe(_coro, _MAIN_LOOP)
                try:
                    result = future.result(timeout=1860)
                except _cf.TimeoutError:
                    # We stopped waiting, but run_coroutine_threadsafe never
                    # cancelled the coroutine — it keeps running on
                    # _MAIN_LOOP. Detach instead of reporting a false
                    # failure: once it actually finishes, hand the reply to
                    # the same idempotent path restart-recovery uses so it
                    # still reaches the user instead of being dropped.
                    log.warning("dispatch: run %s still running past %ss wait — "
                                "detaching, will deliver on completion",
                                run_id, 1860)
                    final_state = "processing"
                    _send_message(
                        token, chat_id,
                        "⏳ Isso está a demorar mais do que o normal — continuo a trabalhar "
                        "nisso, aviso assim que terminar.")

                    def _deliver_when_done(fut: "asyncio.Future", _run_id: str = run_id) -> None:
                        try:
                            res = fut.result()
                        except Exception as e2:
                            log.warning("late-finishing run %s errored: %s", _run_id, e2)
                            return
                        out = (res or {}).get("reply") or (res or {}).get("text", "")
                        if out and _MAIN_LOOP is not None:
                            asyncio.run_coroutine_threadsafe(
                                deliver_recovered_run(_run_id, out), _MAIN_LOOP)

                    future.add_done_callback(_deliver_when_done)
                    return
            else:
                log.warning("dispatch: _MAIN_LOOP not set, using asyncio.run() for run %s (cross-loop bug)", run_id)
                result = asyncio.run(_coro)
            t_agent_done = _time.perf_counter()
            # Deliver only the concluding answer (post-last-tool text), not the
            # full progress transcript — otherwise every "Let me…/Now…" narration
            # floods the chat as its own bubble.
            output_text = result.get("reply") or result.get("text", "")
            status = result.get("status", "unknown")
            timing = result.get("timing", {})
            if status in ("success", "completed"):
                final_state = "done"
            elif status in ("cancelled", "canceled", "aborted"):
                final_state = "cancelled"

            # Persist the new session_id for conversation continuity
            with session_scope() as ss:
                from ..models import Run as _Run
                run_row = ss.query(_Run).filter(_Run.id == run_id).first()
                if run_row and run_row.session_id:
                    _save_session_id(bot.id, chat_id, run_row.session_id, token=token)

            # If the run succeeded (docker exited 0) but produced no tokens/text
            # while trying to resume a session, the session is stale. Reset and
            # retry once with a fresh session so the user gets a response.
            tokens_in = result.get("tokens_in", 0) or 0
            if (not output_text and tokens_in == 0 and session_id
                    and status in ("success", "completed")):
                log.warning("dispatch: stale session %s for bot=%s chat=%s — resetting and retrying",
                            session_id, bot.id, chat_id)
                _reset_session(bot.id, chat_id)
                retry_run_id = str(uuid4())
                _coro2 = run_agent(
                    agent_slug,
                    full_input,
                    run_id=retry_run_id,
                    target_id=target_id,
                    session_id=None,  # fresh session
                    initiator_kind="telegram",
                    initiator_id=f"{bot.id}:{chat_id}",
                )
                if _MAIN_LOOP is not None:
                    result = asyncio.run_coroutine_threadsafe(_coro2, _MAIN_LOOP).result(timeout=1860)
                else:
                    result = asyncio.run(_coro2)
                run_id = retry_run_id
                output_text = result.get("reply") or result.get("text", "")
                status = result.get("status", "unknown")
                if status in ("success", "completed"):
                    final_state = "done"
                # Save the new session from the retry run
                with session_scope() as ss:
                    from ..models import Run as _Run
                    retry_row = ss.query(_Run).filter(_Run.id == retry_run_id).first()
                    if retry_row and retry_row.session_id:
                        _save_session_id(bot.id, chat_id, retry_row.session_id, token=token)

            if output_text:
                t_deliver_start = _time.perf_counter()
                _deliver_reply(token, chat_id, output_text, is_voice, inbound_lang)
                t_deliver_done = _time.perf_counter()
                # Claim delivery so post-restart recovery never re-sends this
                # reply (shared Redis gate with deliver_recovered_run).
                try:
                    if _MAIN_LOOP is not None:
                        from ..core.redis_streams import mark_delivered
                        asyncio.run_coroutine_threadsafe(mark_delivered(run_id), _MAIN_LOOP)
                except Exception:
                    pass
            elif status not in ("success", "completed"):
                t_deliver_start = t_deliver_done = _time.perf_counter()
                _send_message(token, chat_id,
                              f"⚠️ Run finished with status <code>{status}</code>.")

        except Exception as e:
            t_agent_done = _time.perf_counter()
            t_deliver_start = t_deliver_done = t_agent_done
            timing = {}
            log.exception("Agent dispatch failed for bot=%s chat=%s", bot.id, chat_id)
            _send_message(token, chat_id, f"⚠️ Agent execution failed: {e}")
        finally:
            _stop_typing.set()
            # Flip the progress button to its terminal state.
            if proc_msg_id:
                _edit_button_label(
                    token, chat_id, proc_msg_id,
                    f"📊 View Progress [{final_state}]", progress_url,
                    web_app=proc_web_app)

            # ── Structured timing log ──────────────────────────────────────
            _t = _time.perf_counter()
            q_wait_s      = (t_dispatch - t_enqueue) if t_enqueue is not None else None
            session_s     = t_session_done - t_session_start
            button_s      = t_button_done - t_button_start
            agent_total_s = (t_agent_done if 't_agent_done' in dir() else _t) - t_agent_start
            deliver_s     = (t_deliver_done if 't_deliver_done' in dir() else _t) - \
                            (t_deliver_start if 't_deliver_start' in dir() else _t)
            total_s       = _t - t_dispatch

            # Executor-level breakdown (docker + llm phases)
            docker_ready_s  = timing.get("docker_ready_s")   # webhook → system.init
            first_token_s   = timing.get("first_token_s")    # webhook → first token
            llm_total_s     = timing.get("llm_total_s")      # llm_invoke → finalizing

            log.info(
                "[TIMING] bot=%s chat=%s run=%s | "
                "q_wait=%s session=%.2fs button=%.2fs "
                "agent=%.2fs deliver=%s total=%.2fs | "
                "docker_ready=%s first_token=%s llm=%s",
                bot.id, chat_id, run_id[:8],
                f"{q_wait_s:.2f}s" if q_wait_s is not None else "n/a",
                session_s, button_s,
                agent_total_s,
                f"{deliver_s:.2f}s" if 't_deliver_done' in dir() else "n/a",
                total_s,
                f"{docker_ready_s:.2f}s" if docker_ready_s is not None else "n/a",
                f"{first_token_s:.2f}s" if first_token_s is not None else "n/a",
                f"{llm_total_s:.2f}s" if llm_total_s is not None else "n/a",
            )


# ---------------------------------------------------------------------------
# Approval flow (human-in-the-loop secrets)
# ---------------------------------------------------------------------------
# Mirrors the SecretsManager in awserv but lives here so the AP can handle
# the full cycle: send inline keyboard via the sysadmin bot, receive the
# callback_query, call the Lambda, return the secret to the polling MCP.
# ---------------------------------------------------------------------------

import base64 as _b64
import hashlib as _hashlib
import json as _json_mod
import secrets as _secrets_mod
import time as _time_mod

_APPROVAL_TIMEOUT_S = 300  # 5 min
_SCOPE_TTL: dict[str, int] = {"one_shot": 0, "10min": 600, "60min": 3600}
# Minimum token validity window. The scope TTL controls how long a *grant* is
# reused; the token itself still has to survive the round-trip to the Lambda.
# one_shot has TTL 0, so without a floor the token's `e` == now and the Lambda's
# `time.time() > e` check rejects it as expired the instant it arrives (403).
# Single-use is enforced by clearing secret_value after the first poll, not by `e`.
_TOKEN_GRACE_S = 120

# In-memory store: request_id → {secret_name, reason, scope, status,
#                                 secret_value, created_at, chat_id, message_id, bot_token}
_pending_approvals: dict[str, dict] = {}


def _approval_cfg() -> dict:
    """Read approval config from the shared aw.json (same file awserv uses)."""
    try:
        import json as _j
        from ..config import settings  # `settings` was never imported at module scope,
        # so the old reference raised NameError → swallowed here → empty cfg → empty
        # hmac_key → every approval token failed the Lambda signature check (403).
        cfg_path = str(settings.workspace_root / "src" / "config" / "aw.json")
        with open(cfg_path) as f:
            return _j.load(f).get("approval") or {}
    except Exception:
        log.exception("approval: failed to read approval cfg from aw.json")
        return {}


def _sysadmin_bot(s: Session) -> TelegramBot | None:
    """Return the first enabled sysadmin bot, or None."""
    return (
        s.query(TelegramBot)
        .filter(TelegramBot.is_sysadmin == True, TelegramBot.enabled == True)  # noqa: E712
        .first()
    )


def _approval_make_token(secret_name: str, request_id: str, scope: str, hmac_key: str) -> str:
    payload: dict = {
        "n": secret_name,
        "r": request_id,
        "s": scope,
        "e": int(_time_mod.time()) + max(_SCOPE_TTL.get(scope, 60), _TOKEN_GRACE_S),
        "z": _secrets_mod.token_hex(8),
    }
    canonical = _json_mod.dumps(payload, sort_keys=True, separators=(",", ":"))
    sig = hmac.new(
        hmac_key.encode(),
        canonical.encode(),
        _hashlib.sha256,
    ).hexdigest()
    payload["sig"] = sig
    return _b64.urlsafe_b64encode(
        _json_mod.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")


def _approval_call_lambda(secret_name: str, token: str) -> str:
    import boto3, json as _j
    cfg = _approval_cfg()
    kwargs: dict = {"region_name": cfg.get("aws_region") or "us-east-1"}
    if cfg.get("aws_access_key_id") and cfg.get("aws_secret_access_key"):
        kwargs["aws_access_key_id"] = cfg["aws_access_key_id"]
        kwargs["aws_secret_access_key"] = cfg["aws_secret_access_key"]
    client = boto3.client("lambda", **kwargs)
    payload = _j.dumps({
        "body": _j.dumps({"secret_name": secret_name, "token": token}),
        "requestContext": {"http": {"method": "POST"}},
    })
    resp = client.invoke(FunctionName="aw-approval", Payload=payload.encode())
    result = _j.loads(resp["Payload"].read())
    body = _j.loads(result.get("body") or "{}")
    if result.get("statusCode") != 200:
        raise RuntimeError(f"Lambda error {result.get('statusCode')}: {body.get('error', body)}")
    value = body.get("value")
    if value is None:
        raise RuntimeError(f"Lambda returned no value: {body}")
    return value


class ApprovalRequest(BaseModel):
    # For request_type="secret" this is the secret name; for "agent_run" it is
    # the resource being gated (agent/workflow slug). Kept as `secret_name` for
    # backwards compatibility with the existing secretsfs callers.
    secret_name: str
    reason: str
    scope: str = "one_shot"
    request_type: str = "secret"   # "secret" | "agent_run"


class ApprovalStatus(BaseModel):
    request_id: str
    status: str          # pending | approved | denied | expired
    value: str | None = None


@router.post("/approval/request", response_model=ApprovalStatus)
def create_approval_request(body: ApprovalRequest, s: Session = Depends(get_session)):
    """Create a human-in-the-loop approval request.

    Finds the sysadmin bot, sends an inline keyboard to all its admin_user_ids,
    returns a request_id the caller can poll via GET /approval/status/{id}.
    """
    bot = _sysadmin_bot(s)
    if not bot:
        raise HTTPException(503, "No sysadmin bot configured — set is_sysadmin on a bot")

    admin_ids = bot.admin_user_ids or []
    if not admin_ids:
        raise HTTPException(503, "Sysadmin bot has no admin_user_ids configured")

    request_type = (body.request_type or "secret").strip() or "secret"
    request_id = str(uuid4())
    now = _time_mod.time()
    _pending_approvals[request_id] = {
        "secret_name": body.secret_name,
        "reason":      body.reason,
        "scope":       body.scope,
        "request_type": request_type,
        "status":      "pending",
        "secret_value": None,
        "created_at":  now,
        "chat_ids":    admin_ids,
        "message_ids": {},   # chat_id → message_id
        "bot_token":   bot.token,
    }

    if request_type == "agent_run":
        # Pure human-in-the-loop gate: no secret is fetched on approve, the
        # scope buttons (1 use / 10 / 60 min) don't apply — a run is approved
        # once. Two buttons keep callback_data well under Telegram's 64 bytes.
        text = (
            f"▶️ *Aprovação de execução*\n\n"
            f"Recurso: `{body.secret_name}`\n"
            f"Motivo: {body.reason}\n\n"
            f"O agente está aguardando sua liberação para rodar."
        )
        keyboard = [[
            {"text": "✅ Aprovar", "callback_data": f"aw_approval:approve:{request_id}"},
            {"text": "❌ Negar",   "callback_data": f"aw_approval:deny:{request_id}"},
        ]]
    else:
        text = (
            f"🔐 *Aprovação de segredo*\n\n"
            f"Segredo: `{body.secret_name}`\n"
            f"Motivo: {body.reason}\n"
            f"Escolha o escopo da liberação:"
        )
        # The approver picks the scope at approval time (1 use / 10 min / 60 min),
        # like the legacy awserv flow. callback_data is limited to 64 bytes, and a
        # uuid4 request_id is 36 chars, so the scope is encoded as a short code
        # (1 / 10 / 60) rather than the full word — "aw_approval:approve:one_shot:<uuid>"
        # would be 65 bytes and Telegram would silently drop the button.
        keyboard = [[
            {"text": "✅ 1 uso",  "callback_data": f"aw_approval:approve:1:{request_id}"},
            {"text": "⏱ 10 min", "callback_data": f"aw_approval:approve:10:{request_id}"},
            {"text": "⏱ 60 min", "callback_data": f"aw_approval:approve:60:{request_id}"},
        ], [
            {"text": "❌ Negar",   "callback_data": f"aw_approval:deny:{request_id}"},
        ]]

    for chat_id in admin_ids:
        try:
            result = _tg(bot.token, "sendMessage",
                         chat_id=chat_id,
                         text=text,
                         parse_mode="Markdown",
                         reply_markup={"inline_keyboard": keyboard})
            msg_id = (result.get("result") or {}).get("message_id")
            if msg_id:
                _pending_approvals[request_id]["message_ids"][chat_id] = msg_id
        except Exception:
            log.exception("approval: failed to send keyboard to chat %s", chat_id)

    return ApprovalStatus(request_id=request_id, status="pending")


@router.get("/approval/status/{request_id}", response_model=ApprovalStatus)
def get_approval_status(request_id: str):
    """Poll for approval status. Approved value is returned once then cleared."""
    entry = _pending_approvals.get(request_id)
    if not entry:
        raise HTTPException(404, "request_id not found or expired")

    now = _time_mod.time()
    if entry["status"] == "pending" and now - entry["created_at"] > _APPROVAL_TIMEOUT_S:
        entry["status"] = "expired"

    value = None
    if entry["status"] == "approved":
        value = entry.get("secret_value")
        if entry.get("scope") == "one_shot":
            entry["secret_value"] = None  # one-shot: clear after first read

    return ApprovalStatus(request_id=request_id, status=entry["status"], value=value)


def _handle_approval_callback(cq_id: str, cq_data: str, chat_id: str, bot_token: str) -> None:
    """Process an aw_approval: callback_query button tap.

    callback_data format (≤64 bytes):
      aw_approval:approve:<scope_code>:<request_id>   scope_code ∈ {1,10,60}
      aw_approval:deny:<request_id>
    The scope is chosen by the approver via the button they tap.
    """
    _SCOPE_BY_CODE = {"1": "one_shot", "10": "10min", "60": "60min"}
    parts = cq_data.split(":")
    # parts[0]="aw_approval", parts[1]=action
    action = parts[1] if len(parts) > 1 else ""
    scope_override = None
    if action == "approve" and len(parts) >= 4:
        scope_override = _SCOPE_BY_CODE.get(parts[2])
        request_id = ":".join(parts[3:])          # UUID may contain hyphens (not colons)
    else:
        request_id = ":".join(parts[2:]) if len(parts) > 2 else ""

    if action not in ("approve", "deny") or not request_id:
        _answer_callback_query(bot_token, cq_id, "❓ Formato inválido")
        return

    entry = _pending_approvals.get(request_id)
    if not entry:
        _answer_callback_query(bot_token, cq_id, "Pedido não encontrado ou expirado")
        return
    if entry["status"] != "pending":
        _answer_callback_query(bot_token, cq_id, f"Já processado: {entry['status']}")
        return

    is_agent_run = entry.get("request_type") == "agent_run"
    _noun = "Execução" if is_agent_run else "Segredo"

    if action == "deny":
        entry["status"] = "denied"
        _answer_callback_query(bot_token, cq_id, "❌ Negado")
        msg_id = entry["message_ids"].get(str(chat_id))
        if msg_id:
            _edit_message_text(bot_token, str(chat_id), msg_id,
                               f"❌ {_noun} <code>{entry['secret_name']}</code> negad{'a' if is_agent_run else 'o'}.")
        return

    if is_agent_run:
        # Pure gate — nothing to fetch. Mark approved so the polling caller
        # (the run choke point) can dispatch. No scope reuse, no secret value.
        entry["status"] = "approved"
        _answer_callback_query(bot_token, cq_id, "✅ Aprovado")
        msg_id = entry["message_ids"].get(str(chat_id))
        if msg_id:
            _edit_message_text(bot_token, str(chat_id), msg_id,
                               f"✅ Execução <code>{entry['secret_name']}</code> aprovada.")
        return

    # approve — retrieve the actual secret value (vault-first, Lambda fallback).
    # This mirrors the legacy awserv flow: the local vault is the source of truth
    # when populated; the Lambda is only the fallback when the key isn't in vault.
    try:
        scope = scope_override or entry.get("scope") or "one_shot"
        secret_value = None
        try:
            from src.api.vault_client import is_configured as _vault_on, get_secret as _vault_get
            if _vault_on():
                try:
                    secret_value = _vault_get(entry["secret_name"])
                except KeyError:
                    pass  # not in vault — fall through to Lambda
                except Exception:
                    log.exception("approval: vault read failed for %s, falling back to Lambda",
                                  entry["secret_name"])
        except Exception:
            pass  # vault_client unavailable in this process — Lambda only
        if secret_value is None:
            cfg = _approval_cfg()
            hmac_key = cfg.get("hmac_key") or ""
            token = _approval_make_token(entry["secret_name"], request_id, scope, hmac_key)
            secret_value = _approval_call_lambda(entry["secret_name"], token)
        entry["status"] = "approved"
        entry["secret_value"] = secret_value
        entry["scope"] = scope
        _scope_label = {"one_shot": "1 uso", "10min": "10 min", "60min": "60 min"}.get(scope, scope)
        _answer_callback_query(bot_token, cq_id, f"✅ Aprovado ({_scope_label})")
        msg_id = entry["message_ids"].get(str(chat_id))
        _edit_message_text(bot_token, str(chat_id), msg_id,
                           f"✅ Segredo <code>{entry['secret_name']}</code> aprovado "
                           f"(<b>{_scope_label}</b>).")
    except Exception as exc:
        log.exception("approval: Lambda call failed for %s", request_id)
        entry["status"] = "denied"
        _answer_callback_query(bot_token, cq_id, f"Erro ao buscar segredo: {exc}")


# ---------------------------------------------------------------------------
# Kanban approval flow (aw-system-analyst daily findings)
# ---------------------------------------------------------------------------
# Ported from awserv src/api/kanban_manager.py so Telegram is owned entirely by
# AP. awserv POSTs findings to /api/telegram/kanban/notify; AP sends the inline
# keyboard via the sysadmin bot and handles the aw_kanban: callback here
# (execute → update Notion status + fire the agent run; skip → edit message).
# ---------------------------------------------------------------------------

_NOTION_API = "https://api.notion.com/v1"


def _notion_cfg() -> dict:
    """Read the notion config block from the shared aw.json (same file awserv uses)."""
    try:
        import json as _j
        from ..config import settings
        cfg_path = str(settings.workspace_root / "src" / "config" / "aw.json")
        with open(cfg_path) as f:
            return _j.load(f).get("notion") or {}
    except Exception:
        log.exception("kanban: failed to read notion cfg from aw.json")
        return {}


def _notion_headers() -> dict:
    token = _notion_cfg().get("api_token", "")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def _kanban_cfg() -> dict:
    return _notion_cfg().get("agents_kanban", {})


def _kanban_get_page(page_id: str) -> dict:
    r = httpx.get(f"{_NOTION_API}/pages/{page_id}", headers=_notion_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def _kanban_update_status(page_id: str, status_name: str) -> None:
    httpx.patch(
        f"{_NOTION_API}/pages/{page_id}",
        headers=_notion_headers(),
        json={"properties": {"Status": {"select": {"name": status_name}}}},
        timeout=10,
    )


def _kanban_text_prop(page: dict, prop: str) -> str:
    p = page.get("properties", {}).get(prop, {})
    ptype = p.get("type", "")
    if ptype == "title":
        return "".join(t.get("plain_text", "") for t in p.get("title", []))
    if ptype == "rich_text":
        return "".join(t.get("plain_text", "") for t in p.get("rich_text", []))
    return ""


def _handle_kanban_callback(cq_id: str, cq_data: str, chat_id: str,
                            msg_id: int | None, bot_token: str) -> None:
    """Process an aw_kanban:execute/skip callback_query button tap.

    callback_data:
      aw_kanban:execute:{page_id}:{agent_slug}
      aw_kanban:skip:{page_id}
    """
    # Always clear the Telegram spinner first
    _answer_callback_query(bot_token, cq_id)

    parts = cq_data.split(":", 3)
    if len(parts) < 3:
        return
    action, page_id = parts[1], parts[2]
    agent_slug = parts[3] if len(parts) > 3 else None

    def _edit(text: str) -> None:
        if msg_id is not None:
            _edit_message_text(bot_token, chat_id, msg_id, text)

    if action == "skip":
        _edit("⏭ Pulado.")
        return
    if action != "execute":
        return

    try:
        page = _kanban_get_page(page_id)
        card_title = _kanban_text_prop(page, "Name")
        target_slug = _kanban_text_prop(page, "TargetSlug") or "kanban-tasks"
        input_text = _kanban_text_prop(page, "Input") or f"Execute task: {card_title}"
        if not agent_slug:
            agent_slug = _kanban_text_prop(page, "AgentSlug")
        if not agent_slug:
            _edit(f"❌ Card <b>{_md_to_html(card_title)}</b> não tem AgentSlug definido.")
            return

        # Move card to In Progress before firing the run
        in_progress = _kanban_cfg().get("statuses", {}).get("running", "In Progress")
        _kanban_update_status(page_id, in_progress)
        log.info("kanban callback: approved %s → In Progress, firing %s", page_id, agent_slug)

        # Resolve the target and fire the run through AP's own executor.
        from ..core.executor import start_agent_run_bg
        with session_scope() as s:
            agent = s.query(Agent).filter(Agent.slug == agent_slug).first()
            if not agent:
                _edit(f"❌ Agente <code>{agent_slug}</code> não existe no Agents Platform.")
                return
            t = s.query(Target).filter(Target.slug == target_slug).first()
            if t is None:
                _edit(f"❌ Target <code>{target_slug}</code> não existe no Agents Platform.")
                return
            target_id = t.id
        run_id = start_agent_run_bg(
            agent_slug, input_text, target_id=target_id,
            session_id=None, notion_task_id=page_id,
        )
        _edit(f"▶ Executando <b>{_md_to_html(card_title)}</b>\n<code>run_id: {run_id}</code>")
    except Exception as exc:
        log.exception("kanban callback: error for page_id=%s", page_id)
        try:
            _edit(f"❌ Erro: {exc}")
        except Exception:
            pass


def _crispal_upstream() -> dict:
    """The Crispal store's own MCP endpoint (URL + bearer token), read from
    AW's shared config so credentials live in exactly one place."""
    import json as _json
    base = os.environ.get("AW_BASE_DIR", "/opt/agentic-workspace")
    with open(os.path.join(base, "src", "config", "mcp.json")) as f:
        cfg = _json.load(f)
    return cfg["mcpServers"]["crispal"]


def _crispal_send_message(platform: str, recipient_id: str, message: str, message_type: str) -> None:
    """Call the Crispal store's social_send_message tool directly (HTTP JSON-RPC).
    This is the ONLY place a real customer-facing message actually goes out —
    triggered by a human button tap, never by an LLM's own decision."""
    upstream = _crispal_upstream()
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "social_send_message", "arguments": {
            "platform": platform, "recipient_id": recipient_id,
            "message": message, "message_type": message_type,
        }},
    }
    r = httpx.post(upstream["url"], json=payload,
                    headers={"Accept": "application/json, text/event-stream",
                             **upstream.get("headers", {})},
                    timeout=30)
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"crispal social_send_message error: {body['error']}")


def _handle_crispal_suggestion_callback(cq_id: str, cq_data: str, chat_id: str,
                                        msg_id: int | None, bot_token: str) -> None:
    """Process a crispal_suggest:send/edit/ignore callback_query button tap.

    callback_data: crispal_suggest:{action}:{suggestion_id}

    Pure backend action — no LLM in the loop. "send" delivers the exact
    suggested_text via the Crispal MCP directly; "ignore" just records the
    outcome; "edit" opens a web view (not yet built) to change the text
    before sending. Every decision (and the actual final_text, if edited) is
    persisted on the CrispalConversationSuggestion row — that row is the
    traceability log for future prompt/skill tuning.
    """
    _answer_callback_query(bot_token, cq_id)

    parts = cq_data.split(":", 2)
    if len(parts) != 3:
        return
    action, suggestion_id = parts[1], parts[2]

    def _edit(text: str) -> None:
        if msg_id is not None:
            _edit_message_text(bot_token, chat_id, msg_id, text)

    with session_scope() as s:
        row = s.query(CrispalConversationSuggestion).filter(
            CrispalConversationSuggestion.id == suggestion_id).first()
        if not row:
            _edit(f"❌ Sugestão <code>{suggestion_id}</code> não encontrada (pode já ter expirado).")
            return
        if row.status != "pending":
            _edit(f"⚠️ Essa sugestão já foi processada ({row.status}).")
            return

        customer_name = row.customer_name or row.customer_id
        orig_text = row.suggested_text

        if action == "ignore":
            row.status = "ignored"
            row.decided_at = datetime.utcnow()
            _edit(f"🚫 Ignorado — {customer_name}\n\n{orig_text}")
            return

        if action == "edit":
            # Web edit view lands in a follow-up change; for now just tell
            # the operator this action isn't wired up yet (never silently
            # no-ops without feedback).
            _edit(f"✏️ Edição pelo Telegram ainda não implementada — {customer_name}\n\n{orig_text}")
            return

        if action != "send":
            return

        try:
            _crispal_send_message(row.source, row.customer_id, orig_text, row.message_type)
        except Exception as exc:
            log.exception("crispal suggestion send failed: %s", suggestion_id)
            _edit(f"❌ Falha ao enviar pra {customer_name}: {exc}")
            return

        row.status = "sent"
        row.final_text = orig_text
        row.decided_at = datetime.utcnow()
        _edit(f"✅ Enviado — {customer_name}\n\n{orig_text}")


# ---------------------------------------------------------------------------
# Webhook receiver
# ---------------------------------------------------------------------------

def _validate_secret(secret: str, header_value: str) -> bool:
    """Telegram sends the webhook secret_token verbatim in the header (not HMAC)."""
    if not secret:
        return True
    return hmac.compare_digest(secret, header_value or "")


async def _abort_chat_runs(s: Session, bot_id: str, chat_id: str) -> int:
    """Cancel any in-flight run(s) for this chat (+ descendants), killing the
    live CLI subprocess. Mirrors the AW WorkspaceAgent /abort. Returns the
    number of runs marked cancelled."""
    from datetime import datetime
    from ..core.events import bus
    from ..core.models.cli import kill_run
    from ..core.cancel import mark_cancelled

    initiator = f"{bot_id}:{chat_id}"
    roots = (s.query(Run)
             .filter(Run.initiator_id == initiator,
                     Run.status.in_(("running", "pending", "queued")))
             .all())
    if not roots:
        return 0

    ids: list[str] = []

    def _cancel(run) -> None:
        if run.status in ("success", "error", "cancelled"):
            return
        run.status = "cancelled"
        run.ended_at = datetime.utcnow()
        ids.append(run.id)
        for c in s.query(Run).filter(Run.parent_run_id == run.id).all():
            _cancel(c)

    for r in roots:
        _cancel(r)
    s.commit()

    mark_cancelled(*ids)
    for rid in ids:
        try:
            await kill_run(rid)
        except Exception:
            log.debug("kill_run failed for %s", rid, exc_info=True)
    for r in roots:
        try:
            await bus.publish(r.id, "error", {"error": "aborted by user"})
            await bus.publish(r.id, "done", {"status": "cancelled"})
        except Exception:
            pass
    return len(ids)


# Pending /rename ForceReply prompts: (bot_id, chat_id) -> prompt message_id.
# In-memory is fine — AP is single-process; a pending prompt that doesn't
# survive a restart is an acceptable edge case (mirrors AW's .tmp marker file).
_PENDING_RENAME: dict[tuple[str, str], int] = {}


def _set_bot_display_name(token: str, session_name: str) -> None:
    """Mirror AW: set the bot's Telegram name to '{base}: {session}' (idempotent).

    base = the current name with any existing ': ...' suffix stripped, so
    repeated renames don't stack suffixes. Empty session_name resets to base.
    """
    try:
        me = httpx.get(TELEGRAM_API.format(token=token, method="getMe"), timeout=10).json()
        current = ((me.get("result") or {}).get("first_name") or "").strip()
    except Exception:
        current = ""
    base = current.split(": ")[0].strip() if ": " in current else current
    if not base:
        return
    new_name = (f"{base}: {session_name}" if session_name else base)[:64]
    try:
        httpx.post(TELEGRAM_API.format(token=token, method="setMyName"),
                   json={"name": new_name}, timeout=10)
    except Exception:
        log.debug("setMyName failed", exc_info=True)


def _current_target_name(bot_id: str, chat_id: str) -> str:
    with session_scope() as ss:
        tgt = (ss.query(Target)
               .filter(Target.slug == f"tg-{bot_id}-{chat_id}")
               .first())
        return (tgt.name if tgt else "") or ""


def _send_rename_prompt(token: str, chat_id: str, bot_id: str, current_name: str) -> None:
    """Send a ForceReply prompt (exact AW wording) and remember its message_id
    so the user's reply is recognised as the new name."""
    name_display = f" <code>{_md_to_html(current_name)}</code>" if current_name else " (unnamed)"
    try:
        data = _tg(token, "sendMessage", chat_id=chat_id,
                   text=(f"✏️ <b>Rename session</b>\n\nCurrent name:{name_display}\n\n"
                         f"Reply with the new name:"),
                   parse_mode="HTML",
                   reply_markup={"force_reply": True,
                                 "input_field_placeholder": "New session name…",
                                 "selective": True})
        mid = (data.get("result") or {}).get("message_id")
        if mid:
            _PENDING_RENAME[(bot_id, chat_id)] = mid
    except Exception:
        log.warning("send rename prompt failed", exc_info=True)


def _apply_rename(token: str, bot_id: str, chat_id: str, new_name: str) -> bool:
    """Rename this chat's Target, the active CliSession, and the bot display
    name. Returns False when there's no Target yet (no message sent in this
    chat)."""
    new_name = new_name.strip()[:80]
    if not new_name:
        return False
    renamed = False
    with session_scope() as ss:
        tgt = (ss.query(Target)
               .filter(Target.slug == f"tg-{bot_id}-{chat_id}")
               .first())
        if tgt:
            tgt.name = new_name
            renamed = True

        tg_row = (ss.query(TelegramSession)
                  .filter(TelegramSession.bot_id == bot_id,
                          TelegramSession.chat_id == chat_id)
                  .first())
        if tg_row and tg_row.session_id:
            from ..models import CliSession as _CS
            cli_sess = (ss.query(_CS)
                        .filter(_CS.session_id == tg_row.session_id)
                        .first())
            if cli_sess:
                cli_sess.name = new_name
            else:
                ss.add(_CS(session_id=tg_row.session_id, name=new_name))
    if renamed:
        _set_bot_display_name(token, new_name)
    return renamed


@router.post("/webhook/{bot_id}")
async def webhook(bot_id: str, request: Request, s: Session = Depends(get_session)):
    _set_main_loop(asyncio.get_running_loop())
    update = await request.json()

    bot = s.query(TelegramBot).filter(
        TelegramBot.id == bot_id,
        TelegramBot.enabled == True,
    ).first()
    if not bot:
        raise HTTPException(404, f"bot '{bot_id}' not found or disabled")

    # HMAC validation
    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if bot.webhook_secret and not _validate_secret(bot.webhook_secret, secret_header):
        raise HTTPException(403, "invalid webhook secret")

    # ── callback_query (inline button tap) ────────────────────────────────
    callback_query = update.get("callback_query")
    if callback_query:
        cq_id = callback_query.get("id", "")
        cq_data = callback_query.get("data", "")
        cq_from = callback_query.get("from") or {}
        cq_msg = callback_query.get("message") or {}
        cq_chat = cq_msg.get("chat") or {}
        cq_chat_id = str(cq_chat.get("id", ""))
        cq_user_id = str(cq_from.get("id", ""))

        # ACL check for callbacks too
        if bot.admin_user_ids and cq_user_id not in bot.admin_user_ids:
            _answer_callback_query(bot.token, cq_id)
            return {"ok": True, "reason": "not authorized"}

        if cq_data.startswith("aw_approval:") and cq_chat_id:
            # Human-in-the-loop secrets approval flow
            threading.Thread(
                target=_handle_approval_callback,
                args=(cq_id, cq_data, cq_chat_id, bot.token),
                daemon=True,
            ).start()
            return {"ok": True, "reason": "approval callback queued"}

        if cq_data.startswith("aw_kanban:") and cq_chat_id:
            # Kanban approval flow (aw-system-analyst daily findings)
            _kb_msg_id = cq_msg.get("message_id")
            threading.Thread(
                target=_handle_kanban_callback,
                args=(cq_id, cq_data, cq_chat_id, _kb_msg_id, bot.token),
                daemon=True,
            ).start()
            return {"ok": True, "reason": "kanban callback queued"}

        if cq_data.startswith("crispal_suggest:") and cq_chat_id:
            # Crispal Social Watch suggestion approval — pure backend action,
            # never dispatched to an agent (see _handle_crispal_suggestion_callback)
            _cs_msg_id = cq_msg.get("message_id")
            threading.Thread(
                target=_handle_crispal_suggestion_callback,
                args=(cq_id, cq_data, cq_chat_id, _cs_msg_id, bot.token),
                daemon=True,
            ).start()
            return {"ok": True, "reason": "crispal suggestion callback queued"}

        if cq_data.startswith("ap_opt:") and cq_chat_id:
            # Format: ap_opt:{index}:{option_text[:32]}
            parts = cq_data.split(":", 2)
            option_text = parts[2] if len(parts) == 3 else cq_data

            # Dismiss button spinner
            _answer_callback_query(bot.token, cq_id, f"✓ {option_text}")

            # Edit original message to show selected option
            orig_text = cq_msg.get("text", "")
            msg_id = cq_msg.get("message_id")
            if msg_id:
                _edit_message_text(bot.token, cq_chat_id, msg_id,
                                   f"{orig_text}\n\n✅ <b>{option_text}</b>")

            # Dispatch selected option text to agent
            bot_snapshot = TelegramBot(
                id=bot.id, name=bot.name, token=bot.token,
                webhook_secret=bot.webhook_secret, enabled=bot.enabled,
                agent_slug=bot.agent_slug, admin_user_ids=list(bot.admin_user_ids or []),
            )
            _enqueue_dispatch(bot_snapshot, cq_chat_id, cq_user_id,
                              option_text, False, "")

        elif cq_data.startswith("ap_agent:") and cq_chat_id:
            # Agent Picker: user chose an agent
            chosen_slug = cq_data[len("ap_agent:"):]
            _answer_callback_query(bot.token, cq_id)
            # Resolve display name
            agents_all = _list_agents_for_picker()
            chosen_name = next((a["name"] for a in agents_all if a["slug"] == chosen_slug),
                               chosen_slug)
            # Persist as the bot-wide agent; applies to every chat on this bot
            _set_bot_agent_slug(bot_id, chosen_slug)
            # Collapse the picker message
            msg_id = cq_msg.get("message_id")
            if msg_id:
                _edit_message_text(bot.token, cq_chat_id, msg_id,
                                   f"🤖 <b>Agent Picker</b>\n\n✅ <b>{_md_to_html(chosen_name)}</b> selected")
            # Show session picker
            _send_session_picker(bot.token, cq_chat_id, chosen_slug, chosen_name)

        elif cq_data.startswith("ap_sess:") and cq_chat_id:
            # Session Picker: user chose a session (or "new")
            chosen_sid = cq_data[len("ap_sess:"):]
            _answer_callback_query(bot.token, cq_id)
            msg_id = cq_msg.get("message_id")
            if chosen_sid == "__new__":
                _set_session_override(bot_id, cq_chat_id, None)
                if msg_id:
                    _edit_message_text(bot.token, cq_chat_id, msg_id,
                                       "➕ Starting a <b>new session</b> — previous context cleared.")
                _send_message(bot.token, cq_chat_id,
                              "🟢 Ready! Send a message to start a new conversation.")
            else:
                _set_session_override(bot_id, cq_chat_id, chosen_sid)
                short = chosen_sid[:8] + "…"
                if msg_id:
                    _edit_message_text(bot.token, cq_chat_id, msg_id,
                                       f"✅ Resumed session <code>{short}</code>")
                _send_message(bot.token, cq_chat_id,
                              f"🔄 Session <code>{short}</code> resumed. Send a message to continue.")

        return {"ok": True, "reason": "callback_query handled"}

    # ── Regular message ───────────────────────────────────────────────────
    message = (update.get("message") or update.get("edited_message") or
               update.get("channel_post") or {})
    if not message:
        return {"ok": True, "reason": "no message"}

    chat = message.get("chat") or {}
    from_user = message.get("from") or {}

    # Ignore bot echoes
    if from_user.get("is_bot"):
        return {"ok": True, "reason": "from bot"}

    chat_id = str(chat.get("id", ""))
    user_id = str(from_user.get("id", ""))
    if not chat_id:
        return {"ok": True, "reason": "no chat_id"}

    # ACL check
    if bot.admin_user_ids and user_id not in bot.admin_user_ids:
        log.info("bot %s: user %s not in admin list — ignored", bot_id, user_id)
        return {"ok": True, "reason": "not authorized"}

    # Reply to a pending /rename ForceReply prompt → apply the rename directly,
    # without going through the agent (mirrors the AW WorkspaceAgent flow).
    reply_to_id = (message.get("reply_to_message") or {}).get("message_id")
    if reply_to_id and _PENDING_RENAME.get((bot_id, chat_id)) == reply_to_id:
        _PENDING_RENAME.pop((bot_id, chat_id), None)
        new_name = (message.get("text") or "").strip()
        if new_name:
            ok = _apply_rename(bot.token, bot_id, chat_id, new_name)
            _send_message(bot.token, chat_id,
                          f"✅ Session renamed to <b>{_md_to_html(new_name[:80])}</b>" if ok
                          else "❌ No active session to rename. Send a message first.")
            return {"ok": True, "reason": "rename reply"}

    # ── Slash commands ────────────────────────────────────────────────────
    # Parse "/cmd@bot args" → (cmd, args). Unknown commands fall through to the
    # agent so they're handled like a normal message (matches AW behaviour).
    text_raw = (message.get("text") or message.get("caption") or "").strip()
    if text_raw.startswith("/"):
        _head, _, _args = text_raw.partition(" ")
        cmd = _head.lstrip("/").split("@", 1)[0].lower()
        args = _args.strip()

        if cmd in ("agent", "agents", "pick"):
            _send_agent_picker(bot.token, chat_id)
            return {"ok": True, "reason": "slash /agent"}

        if cmd in ("new", "newsession", "reset"):
            existed = False
            with session_scope() as ss:
                row = (ss.query(TelegramSession)
                       .filter(TelegramSession.bot_id == bot_id,
                               TelegramSession.chat_id == chat_id)
                       .first())
                existed = bool(row and row.session_id)
            _reset_session(bot_id, chat_id)
            base_msg = ("🔄 Started a fresh conversation. Previous context cleared." if existed
                        else "🟢 Ready to start a new conversation.")
            if args:
                # "/new <name>" also names the session, same UX as /rename.
                _ensure_target(bot_id, chat_id)
                ok = _apply_rename(bot.token, bot_id, chat_id, args)
                _send_message(bot.token, chat_id,
                              f"{base_msg}\nSession named <b>{_md_to_html(args.strip()[:80])}</b>." if ok
                              else base_msg)
            else:
                _reset_target_name(bot_id, chat_id)
                _set_bot_display_name(bot.token, "")  # back to the bot's base name
                _send_message(bot.token, chat_id, base_msg)
            return {"ok": True, "reason": "slash /new"}

        if cmd == "start":
            _reset_session(bot_id, chat_id)
            _reset_target_name(bot_id, chat_id)
            _set_bot_display_name(bot.token, "")  # back to the bot's base name
            _send_message(bot.token, chat_id,
                          "👋 Hi! I'm your Agents Platform agent. "
                          "Send me a message to get started.")
            return {"ok": True, "reason": "slash /start"}

        if cmd == "status":
            with session_scope() as ss:
                row = (ss.query(TelegramSession)
                       .filter(TelegramSession.bot_id == bot_id,
                               TelegramSession.chat_id == chat_id)
                       .first())
                sid = row.session_id if row else None
                slug_override = row.agent_slug_override if row else None
                last_run_id = None
                if sid:
                    last_run = (ss.query(Run)
                                .filter(Run.session_id == sid)
                                .order_by(Run.started_at.desc())
                                .first())
                    last_run_id = last_run.id if last_run else None
            effective_slug = slug_override or bot.agent_slug or "(none)"
            override_note = " (override)" if slug_override else ""
            name = _current_target_name(bot_id, chat_id)
            if not sid:
                _send_message(bot.token, chat_id,
                              "📭 No active session.\n"
                              f"Agent: <code>{effective_slug}</code>{override_note}\n"
                              "Send any message to start one.")
            else:
                name_line = f"Name: <code>{_md_to_html(name)}</code>\n" if name else ""
                run_line = f"Run id: <code>{last_run_id}</code>\n" if last_run_id else ""
                _send_message(bot.token, chat_id,
                              "📊 <b>Session status</b>\n"
                              f"{name_line}"
                              f"Agent: <code>{effective_slug}</code>{override_note}\n"
                              f"Session id: <code>{sid}</code>\n"
                              f"{run_line}")
            return {"ok": True, "reason": "slash /status"}

        if cmd == "rename":
            # No name → ForceReply prompt (reply is caught above). With a name →
            # apply immediately. Same UX as the AW WorkspaceAgent /rename.
            if not args:
                _send_rename_prompt(bot.token, chat_id, bot_id,
                                    _current_target_name(bot_id, chat_id))
                return {"ok": True, "reason": "slash /rename prompt"}
            ok = _apply_rename(bot.token, bot_id, chat_id, args)
            _send_message(bot.token, chat_id,
                          f"✅ Session renamed to <b>{_md_to_html(args.strip()[:80])}</b>" if ok
                          else "❌ No active session to rename. Send a message first.")
            return {"ok": True, "reason": "slash /rename"}

        if cmd == "abort":
            n = await _abort_chat_runs(s, bot_id, chat_id)
            _send_message(bot.token, chat_id,
                          f"🛑 Aborted {n} running run(s)." if n
                          else "Nothing running to abort.")
            return {"ok": True, "reason": "slash /abort"}

        if cmd == "compact":
            with session_scope() as ss:
                _tg_row = (ss.query(TelegramSession)
                           .filter(TelegramSession.bot_id == bot_id,
                                   TelegramSession.chat_id == chat_id)
                           .first())
                _compact_session_id = _tg_row.session_id if _tg_row else None
                _compact_slug = (_tg_row.agent_slug_override if _tg_row else None) or bot.agent_slug
            if not _compact_slug:
                _send_message(bot.token, chat_id, "⚠️ No agent configured for this chat.")
                return {"ok": True, "reason": "slash /compact no agent"}
            if not _compact_session_id:
                _send_message(bot.token, chat_id,
                              "⚠️ No active session. Send a message first to start one.")
                return {"ok": True, "reason": "slash /compact no session"}
            _send_message(bot.token, chat_id,
                          "🗜 Compacting context… (may take up to a minute)")
            _compact_target_id = _ensure_target(bot.id, chat_id)
            _compact_token = bot.token
            _compact_bot_id = bot.id
            _compact_chat_id = chat_id

            def _do_compact(slug, sess_id, target_id, tg_token, b_id, c_id):
                import asyncio as _aio
                try:
                    from ..core.executor import run_agent as _run_agent
                    _coro = _run_agent(
                        slug, "/compact",
                        target_id=target_id,
                        session_id=sess_id,
                        initiator_kind="telegram",
                        initiator_id=f"{b_id}:{c_id}",
                    )
                    if _MAIN_LOOP is not None:
                        result = _aio.run_coroutine_threadsafe(_coro, _MAIN_LOOP).result(timeout=300)
                    else:
                        result = _aio.run(_coro)
                    output_text = (result.get("text") or "").strip()
                    status = result.get("status", "unknown")
                    if status in ("success", "completed"):
                        if output_text and output_text != "(empty response)":
                            _send_message(tg_token, c_id,
                                          f"✅ Context compacted.\n\n{_md_to_html(output_text)}",
                                          parse_mode="HTML")
                        else:
                            _send_message(tg_token, c_id, "✅ Context compacted.")
                    else:
                        _send_message(tg_token, c_id,
                                      f"⚠️ Compact finished with status: <code>{status}</code>",
                                      parse_mode="HTML")
                except Exception as _e:
                    log.exception("compact failed for bot=%s chat=%s", b_id, c_id)
                    _send_message(tg_token, c_id, f"⚠️ Compact failed: {_e}")

            threading.Thread(
                target=_do_compact,
                args=(_compact_slug, _compact_session_id, _compact_target_id,
                      _compact_token, _compact_bot_id, _compact_chat_id),
                daemon=True, name=f"compact-{bot.id}-{chat_id}",
            ).start()
            return {"ok": True, "reason": "slash /compact queued"}

        if cmd == "clear":
            # Unlike /new (which drops session_id and starts a brand-new CLI
            # session), /clear passes the literal "/clear" straight through to
            # the running CLI on the SAME session_id — the CLI's own /clear
            # handles wiping its context in place. Mirrors the /compact block.
            with session_scope() as ss:
                _clear_row = (ss.query(TelegramSession)
                              .filter(TelegramSession.bot_id == bot_id,
                                      TelegramSession.chat_id == chat_id)
                              .first())
                _clear_session_id = _clear_row.session_id if _clear_row else None
                _clear_slug = (_clear_row.agent_slug_override if _clear_row else None) or bot.agent_slug
            if not _clear_slug:
                _send_message(bot.token, chat_id, "⚠️ No agent configured for this chat.")
                return {"ok": True, "reason": "slash /clear no agent"}
            if not _clear_session_id:
                _send_message(bot.token, chat_id,
                              "⚠️ No active session. Send a message first to start one.")
                return {"ok": True, "reason": "slash /clear no session"}
            _clear_target_id = _ensure_target(bot.id, chat_id)
            _clear_token = bot.token
            _clear_bot_id = bot.id
            _clear_chat_id = chat_id

            def _do_clear(slug, sess_id, target_id, tg_token, b_id, c_id):
                import asyncio as _aio
                try:
                    from ..core.executor import run_agent as _run_agent
                    _coro = _run_agent(
                        slug, "/clear",
                        target_id=target_id,
                        session_id=sess_id,
                        initiator_kind="telegram",
                        initiator_id=f"{b_id}:{c_id}",
                    )
                    if _MAIN_LOOP is not None:
                        result = _aio.run_coroutine_threadsafe(_coro, _MAIN_LOOP).result(timeout=120)
                    else:
                        result = _aio.run(_coro)
                    output_text = (result.get("text") or "").strip()
                    status = result.get("status", "unknown")
                    if status in ("success", "completed"):
                        if output_text and output_text != "(empty response)":
                            _send_message(tg_token, c_id,
                                          f"🧹 Context cleared.\n\n{_md_to_html(output_text)}",
                                          parse_mode="HTML")
                        else:
                            _send_message(tg_token, c_id, "🧹 Context cleared.")
                    else:
                        _send_message(tg_token, c_id,
                                      f"⚠️ Clear finished with status: <code>{status}</code>",
                                      parse_mode="HTML")
                except Exception as _e:
                    log.exception("clear failed for bot=%s chat=%s", b_id, c_id)
                    _send_message(tg_token, c_id, f"⚠️ Clear failed: {_e}")

            threading.Thread(
                target=_do_clear,
                args=(_clear_slug, _clear_session_id, _clear_target_id,
                      _clear_token, _clear_bot_id, _clear_chat_id),
                daemon=True, name=f"clear-{bot.id}-{chat_id}",
            ).start()
            return {"ok": True, "reason": "slash /clear queued"}

        if cmd in ("help", "?"):
            _send_message(bot.token, chat_id,
                          "🤖 <b>Agents Platform commands</b>\n"
                          "/agent — pick an agent and session\n"
                          "/new [name] — start a fresh conversation (clears context; optional name also renames the session)\n"
                          "/start — greet and start a fresh session\n"
                          "/status — show active session info\n"
                          "/rename — rename the current session (no args → prompt)\n"
                          "/abort — stop the run in progress\n"
                          "/compact — compress conversation context to save tokens\n"
                          "/clear — clear the CLI's context in place (keeps the same session)\n"
                          "/help — show this message")
            return {"ok": True, "reason": "slash /help"}
        # Unknown slash command → fall through to the agent dispatch below.

    # Voice or text
    is_voice = False
    inbound_lang = ""
    msg_id = str(message.get("message_id", ""))

    if message.get("voice") or message.get("audio"):
        file_id = (message.get("voice") or message.get("audio") or {}).get("file_id", "")
        if not file_id:
            _send_message(bot.token, chat_id,
                          "⚠️ Não consegui acessar o áudio (o Telegram não mandou o file_id).")
            return {"ok": True, "reason": "voice without file_id"}
        is_voice = True
        stt_error = ""
        try:
            text_raw, inbound_lang = _transcribe_voice(bot.token, file_id)
        except Exception as e:
            log.exception("STT failed: %s", e)
            text_raw, inbound_lang, stt_error = "", "", str(e)
        if not text_raw:
            # Never fail silently: transcribe() already tries OpenAI Whisper and
            # then falls back to local faster-whisper. If we still have nothing,
            # tell the user why instead of dropping the message.
            log.warning("STT produced no text for chat=%s file_id=%s err=%s",
                        chat_id, file_id, stt_error or "(provider returned empty)")
            detail = f"\nDetalhe: <code>{stt_error}</code>" if stt_error else ""
            _send_message(
                bot.token, chat_id,
                "⚠️ Não consegui transcrever o seu áudio. Tentei o Whisper (OpenAI) e o "
                "faster-whisper local, e nenhum dos dois retornou texto — verifique se a "
                "chave da OpenAI está configurada ou se o modelo local está disponível. "
                "Enquanto isso, pode me mandar por texto." + detail,
                parse_mode="HTML")
            return {"ok": True, "reason": "stt produced no text"}
        # Echo transcription as reply to the original voice message
        if msg_id:
            try:
                _tg(bot.token, "sendMessage",
                    chat_id=chat_id,
                    text=f"🎙 Transcription: {text_raw}",
                    reply_to_message_id=int(msg_id))
            except Exception:
                pass

    # Handle user-uploaded file (document/photo/video/audio) — not voice notes
    if not is_voice:
        upload_path = _save_telegram_upload(bot.token, message)
        if upload_path:
            caption = text_raw  # user caption, if any
            text_raw = f"[UPLOAD] File saved at: {upload_path}"
            if caption:
                text_raw += f"\n\n{caption}"

    if not text_raw:
        return {"ok": True, "reason": "no text"}

    # Inject replied-to message content so the agent has full context.
    # Skip when the reply is to a pending /rename prompt (handled above).
    reply_msg = message.get("reply_to_message")
    if reply_msg and _PENDING_RENAME.get((bot_id, chat_id)) != reply_msg.get("message_id"):
        replied_text = (reply_msg.get("text") or reply_msg.get("caption") or "").strip()
        if not replied_text:
            if reply_msg.get("voice") or reply_msg.get("audio"):
                _quoted_file_id = ((reply_msg.get("voice") or reply_msg.get("audio")) or {}).get("file_id", "")
                replied_text = ""
                if _quoted_file_id:
                    try:
                        replied_text, _ = _transcribe_voice(bot.token, _quoted_file_id)
                    except Exception as _e:
                        log.warning("STT failed for quoted voice message: %s", _e)
                if not replied_text:
                    replied_text = "[voice transcription failed]"
                    try:
                        _send_message(bot.token, chat_id,
                                      "⚠️ Could not transcribe the voice message you replied to.")
                    except Exception:
                        pass
            elif reply_msg.get("photo"):
                replied_text = "[photo]"
            elif reply_msg.get("video"):
                replied_text = "[video]"
            elif reply_msg.get("document"):
                fname = (reply_msg["document"].get("file_name") or "file")
                replied_text = f"[document: {fname}]"
            elif reply_msg.get("sticker"):
                replied_text = "[sticker]"
        if replied_text:
            text_raw = f"[Replying to: \"{replied_text}\"]\n\n{text_raw}"

    # Capture bot state before thread (avoid detached ORM object)
    bot_snapshot = TelegramBot(
        id=bot.id, name=bot.name, token=bot.token,
        webhook_secret=bot.webhook_secret, enabled=bot.enabled,
        agent_slug=bot.agent_slug, admin_user_ids=list(bot.admin_user_ids or []),
    )

    _enqueue_dispatch(bot_snapshot, chat_id, user_id, text_raw, is_voice, inbound_lang)

    return {"ok": True}


# ---------------------------------------------------------------------------
# CRUD routes
# ---------------------------------------------------------------------------

class BotIn(BaseModel):
    id: str
    name: str = ""
    token: str
    webhook_secret: str = ""
    enabled: bool = True
    is_sysadmin: bool = False
    agent_slug: str | None = None
    admin_user_ids: list[str] = []


class BotUpdate(BaseModel):
    name: str | None = None
    token: str | None = None
    webhook_secret: str | None = None
    enabled: bool | None = None
    is_sysadmin: bool | None = None
    agent_slug: str | None = None
    admin_user_ids: list[str] | None = None


class BotOut(BaseModel):
    id: str
    name: str
    token: str
    webhook_secret: str
    enabled: bool
    is_sysadmin: bool
    agent_slug: str | None
    admin_user_ids: list[str]

    model_config = {"from_attributes": True}


@router.get("/bots", response_model=list[BotOut])
def list_bots(s: Session = Depends(get_session)):
    return s.query(TelegramBot).all()


@router.post("/bots", response_model=BotOut, status_code=201)
def create_bot(body: BotIn, s: Session = Depends(get_session)):
    if s.query(TelegramBot).filter(TelegramBot.id == body.id).first():
        raise HTTPException(409, f"bot '{body.id}' already exists")
    bot = TelegramBot(**body.model_dump())
    s.add(bot)
    s.commit()
    s.refresh(bot)
    return bot


@router.get("/bots/{bot_id}", response_model=BotOut)
def get_bot(bot_id: str, s: Session = Depends(get_session)):
    bot = s.query(TelegramBot).filter(TelegramBot.id == bot_id).first()
    if not bot:
        raise HTTPException(404, f"bot '{bot_id}' not found")
    return bot


@router.put("/bots/{bot_id}", response_model=BotOut)
def update_bot(bot_id: str, body: BotUpdate, s: Session = Depends(get_session)):
    bot = s.query(TelegramBot).filter(TelegramBot.id == bot_id).first()
    if not bot:
        raise HTTPException(404, f"bot '{bot_id}' not found")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(bot, k, v)
    s.commit()
    s.refresh(bot)
    return bot


@router.delete("/bots/{bot_id}", status_code=204)
def delete_bot(bot_id: str, s: Session = Depends(get_session)):
    bot = s.query(TelegramBot).filter(TelegramBot.id == bot_id).first()
    if not bot:
        raise HTTPException(404, f"bot '{bot_id}' not found")
    s.delete(bot)
    s.commit()


@router.get("/bots/{bot_id}/sessions")
def list_bot_sessions(bot_id: str, s: Session = Depends(get_session)):
    """Per-chat state for this bot — which agent (default or /agent override)
    is currently active in each chat, and whether a session is live."""
    bot = s.query(TelegramBot).filter(TelegramBot.id == bot_id).first()
    if not bot:
        raise HTTPException(404, f"bot '{bot_id}' not found")
    rows = (s.query(TelegramSession)
            .filter(TelegramSession.bot_id == bot_id)
            .order_by(TelegramSession.updated_at.desc())
            .all())
    return [
        {
            "chat_id": r.chat_id,
            "agent_slug": r.agent_slug_override or bot.agent_slug,
            "is_override": bool(r.agent_slug_override),
            "session_id": r.session_id,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


@router.post("/bots/{bot_id}/register-webhook")
def register_webhook(bot_id: str, base_url: str = "", s: Session = Depends(get_session)):
    """Register this bot's webhook URL with Telegram.

    Points directly to the AP public subdomain (no AW proxy needed).
    URL: https://agents-platform.app.aw.tekflox.com/api/telegram/webhook/{bot_id}
    """
    import os as _os
    bot = s.query(TelegramBot).filter(TelegramBot.id == bot_id).first()
    if not bot:
        raise HTTPException(404, f"bot '{bot_id}' not found")

    base = (base_url or "").rstrip("/") or _os.environ.get(
        "AP_PUBLIC_URL", "https://agents-platform.app.aw.tekflox.com"
    )
    webhook_url = f"{base}/api/telegram/webhook/{bot_id}"

    payload: dict[str, Any] = {"url": webhook_url}
    if bot.webhook_secret:
        payload["secret_token"] = bot.webhook_secret
    result = _tg(bot.token, "setWebhook", **payload)
    return {"ok": True, "webhook_url": webhook_url, "telegram": result}


# ---------------------------------------------------------------------------
# Kanban approval — send inline keyboard via the sysadmin bot
# ---------------------------------------------------------------------------

class KanbanFinding(BaseModel):
    page_id: str
    title: str = "?"
    agent_slug: str = ""
    priority: str = "Média"
    finding_key: str = ""
    occurrence_count: int = 1
    summary: str = ""


class KanbanNotify(BaseModel):
    findings: list[KanbanFinding] = []


@router.post("/kanban/notify")
def kanban_notify(body: KanbanNotify, s: Session = Depends(get_session)):
    """Send one Telegram approval message per finding via the sysadmin bot.

    Called by awserv (src/api/kanban_manager.send_approval_batch) so Telegram
    delivery stays entirely inside AP. Returns {ok, sent}.
    """
    if not body.findings:
        return {"ok": True, "sent": 0}

    bot = _sysadmin_bot(s)
    if not bot:
        raise HTTPException(503, "No sysadmin bot configured — set is_sysadmin on a bot")
    admin_ids = bot.admin_user_ids or []
    if not admin_ids:
        raise HTTPException(503, "Sysadmin bot has no admin_user_ids configured")
    chat_id = admin_ids[0]

    prio_emoji = {"Alta": "🔴", "Média": "🟡", "Baixa": "🟢",
                  "High": "🔴", "Medium": "🟡", "Low": "🟢"}

    sent = 0
    for f in body.findings:
        emoji = prio_emoji.get(f.priority, "⚪")
        occ_tag = f" · ocorrência #{f.occurrence_count}" if f.occurrence_count > 1 else ""
        notion_url = f"https://www.notion.so/{f.page_id.replace('-', '')}"
        raw_summary = f.summary or f.finding_key or ""
        summary = raw_summary[:200] + ("…" if len(raw_summary) > 200 else "")

        lines = [f"{emoji} <b>{_md_to_html(f.title)}</b>{occ_tag}", ""]
        if summary:
            lines += [f"<i>{_md_to_html(summary)}</i>", ""]
        lines += [
            (f"🤖 Agente: <code>{f.agent_slug}</code>" if f.agent_slug
             else "🤖 Agente: <i>não definido</i>"),
            f'<a href="{notion_url}">Ver card completo no Notion</a>',
        ]
        text = "\n".join(lines)
        keyboard = {"inline_keyboard": [[
            {"text": "▶ Executar",
             "callback_data": f"aw_kanban:execute:{f.page_id}:{f.agent_slug}"},
            {"text": "⏭ Pular",
             "callback_data": f"aw_kanban:skip:{f.page_id}"},
        ]]}
        try:
            _tg(bot.token, "sendMessage", chat_id=chat_id, text=text,
                parse_mode="HTML", reply_markup=keyboard,
                disable_web_page_preview=True)
            sent += 1
        except Exception:
            log.exception("kanban notify: send failed for page %s", f.page_id)

    return {"ok": True, "sent": sent}


class TelegramInject(BaseModel):
    bot_id: str
    chat_id: str
    text: str
    # When set, the injected text is echoed into the chat first (tagged with
    # this label, e.g. "Apple Watch") before the agent runs — so a human
    # reading Telegram sees what triggered the reply. Omit (the aw-tasks cron
    # use case) and behavior is unchanged: only the agent's reply is posted.
    source: str | None = None


def _verify_inject_secret(x_internal_secret: str = Header(default="")) -> None:
    from ..config import settings
    if not settings.telegram_inject_secret:
        raise HTTPException(503, "telegram_inject_secret not configured (AGENTS_TELEGRAM_INJECT_SECRET)")
    if not hmac.compare_digest(settings.telegram_inject_secret, x_internal_secret or ""):
        raise HTTPException(403, "invalid X-Internal-Secret")


@router.get("/session-lookup")
def session_lookup(session_id: str, _auth: None = Depends(_verify_inject_secret),
                    s: Session = Depends(get_session)) -> dict:
    """Reverse lookup: given an AP CliSession id, find the (bot_id, chat_id)
    it's bound to, if any. Lets a non-Telegram caller (e.g. the AW Meta
    Glasses/Watch backend) discover whether "the session the user currently
    has selected" is also a live Telegram conversation, before calling
    /inject. Same trust boundary as /inject (internal secret)."""
    row = s.query(TelegramSession).filter(TelegramSession.session_id == session_id).first()
    if not row:
        raise HTTPException(404, "no telegram session bound to this session_id")
    return {"bot_id": row.bot_id, "chat_id": row.chat_id}


@router.post("/inject")
def inject_system_message(body: TelegramInject, _auth: None = Depends(_verify_inject_secret),
                           s: Session = Depends(get_session)) -> dict:
    """Inject a synthetic/system message into an existing (bot, chat) session.

    Reuses the same Target/session continuity as a real inbound webhook
    message (so the agent's memory treats it as part of the conversation
    history), but skips the "Processing…" progress button and typing
    indicator since no real user is watching in real time. By default the
    injected text itself is never shown in Telegram (only the agent's reply
    is) — pass `source` to echo it first, tagged with that label.

    Internal-only: called by aw-tasks cron scripts and the AW Meta
    Glasses/Watch backend, not exposed to the outside. Guarded by
    AGENTS_TELEGRAM_INJECT_SECRET (X-Internal-Secret header) since this
    endpoint can otherwise put words in any bot's mouth in any chat it's
    already talked in.
    """
    bot = s.query(TelegramBot).filter(TelegramBot.id == body.bot_id).first()
    if not bot or not bot.enabled:
        raise HTTPException(404, "bot not found or disabled")

    if body.source and body.text:
        _send_message(
            bot.token, body.chat_id,
            f"🕐 <b>{html.escape(body.source)}</b>: {html.escape(body.text)}",
        )

    agent_slug = _get_agent_slug_for_chat(bot, bot.id, body.chat_id)
    if not agent_slug:
        raise HTTPException(400, "no agent configured for this bot/chat")
    with session_scope() as s2:
        agent = s2.query(Agent).filter(Agent.slug == agent_slug).first()
        if not agent:
            raise HTTPException(404, f"agent {agent_slug} not found")

    target_id = _ensure_target(bot.id, body.chat_id)
    session_id, _ = _get_or_create_session(bot.id, body.chat_id, target_id)

    # `source` means a real person sent this from another channel (e.g. the
    # AW Watch) — NOT an automated cron/system ping. The aw-agent-telegram
    # skill's silence guard treats `origin: system` as "no real user, stay
    # quiet", so a genuine cross-channel message must NOT be tagged that way
    # or the agent silently drops it (no reply to either Telegram or the
    # originating channel).
    is_system = not body.source
    header = (
        f"/aw-agent-telegram\n"
        f"CONTEXT:\n"
        f"- source: telegram\n"
        f"- chat_id: {body.chat_id}\n"
        f"- user_id: {'system' if is_system else body.source}\n"
        f"- bot_id: {bot.id}\n"
    )
    if is_system:
        header += "- origin: system\n"
    full_input = header + f"USER_MESSAGE:\n{body.text}"

    run_id = str(uuid4())
    from ..core.executor import run_agent
    _coro = run_agent(
        agent_slug, full_input, run_id=run_id, target_id=target_id,
        session_id=session_id, initiator_kind="telegram_system",
        initiator_id=f"{bot.id}:{body.chat_id}",
    )
    if _MAIN_LOOP is not None:
        result = asyncio.run_coroutine_threadsafe(_coro, _MAIN_LOOP).result(timeout=1860)
    else:
        result = asyncio.run(_coro)

    output_text = result.get("reply") or result.get("text", "")
    status = result.get("status", "unknown")

    with session_scope() as ss:
        from ..models import Run as _Run
        run_row = ss.query(_Run).filter(_Run.id == run_id).first()
        if run_row and run_row.session_id:
            _save_session_id(bot.id, body.chat_id, run_row.session_id, token=bot.token)

    if output_text:
        _deliver_reply(bot.token, body.chat_id, output_text, False, "")
        try:
            if _MAIN_LOOP is not None:
                from ..core.redis_streams import mark_delivered
                asyncio.run_coroutine_threadsafe(mark_delivered(run_id), _MAIN_LOOP)
        except Exception:
            pass

    return {"run_id": run_id, "status": status, "delivered": bool(output_text), "reply": output_text}


# Self-contained Mini App page. __RUN_ID__ is substituted at request time.
_PROGRESS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>AP Progress</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d0d0f;--surface:#1a1a1f;--border:#2a2a32;--fg:#e8e8ed;--hint:#8e8e98;
  --blue:#0a84ff;--green:#30d158;--yellow:#ffd60a;--red:#ff453a;--purple:#bf5af2;
  --tool-bg:#1e1e28;--tool-border:#3a3a4a;
}
html,body{height:100%;overflow:hidden}
body{background:var(--bg);color:var(--fg);font-family:"SF Mono","Fira Code",Menlo,Monaco,monospace;font-size:12px;line-height:1.6;display:flex;flex-direction:column}
#header{display:flex;align-items:center;gap:10px;padding:10px 14px 8px;border-bottom:1px solid var(--border);background:var(--surface);flex-shrink:0}
#spinner{width:14px;height:14px;border:2px solid rgba(255,255,255,.15);border-top-color:var(--blue);border-radius:50%;animation:spin .7s linear infinite;flex-shrink:0}
#spinner.done{border:none;background:var(--green);display:flex;align-items:center;justify-content:center;font-size:9px;color:#000;animation:pop .2s ease-out forwards}
#status{font-size:13px;font-weight:600;color:var(--fg);flex:1;font-family:-apple-system,sans-serif}
#timer{font-size:11px;color:var(--hint);font-variant-numeric:tabular-nums;font-family:monospace}
#log{flex:1;overflow-y:auto;padding:10px 0 10px;scroll-behavior:smooth}
.row{display:flex;align-items:flex-start;gap:0;padding:2px 14px;animation:fadein .15s ease-out}
.row:hover{background:rgba(255,255,255,.03)}
.pfx{color:var(--hint);margin-right:8px;flex-shrink:0;user-select:none;font-size:11px;padding-top:1px}
.txt{color:var(--fg);white-space:pre-wrap;word-break:break-word;flex:1}
.txt.thinking{color:var(--hint);font-style:italic}
.tool-wrap{display:block;margin:1px 0}
.tool-chip{display:inline-flex;align-items:center;gap:5px;background:var(--tool-bg);border:1px solid var(--tool-border);border-radius:5px;padding:2px 8px 2px 6px;color:var(--purple);font-size:11px;cursor:pointer;user-select:none;transition:border-color .15s}
.tool-chip:hover{border-color:var(--purple)}
.tool-wrap.expanded .tool-chip{border-color:var(--purple);border-bottom-left-radius:0;border-bottom-right-radius:0}
.tool-chip .icon{font-size:12px}
.tool-chip .name{font-weight:600;letter-spacing:.2px}
.tool-chip .args{color:var(--hint);font-size:10px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tool-chip .xpd{color:var(--hint);font-size:11px;margin-left:2px;transition:transform .15s;display:inline-block}
.tool-wrap.expanded .tool-chip .xpd{transform:rotate(90deg)}
.tool-detail{display:none;background:var(--tool-bg);border:1px solid var(--purple);border-top:none;border-radius:0 0 5px 5px;padding:6px 8px;max-height:260px;overflow-y:auto}
.tool-wrap.expanded .tool-detail{display:block}
.tool-cmd{font-size:10px;color:var(--yellow);white-space:pre-wrap;word-break:break-all;margin:0 0 5px 0;line-height:1.5}
.tool-output{font-size:10px;color:var(--fg);white-space:pre-wrap;word-break:break-all;margin:0;line-height:1.5;border-top:1px solid var(--border);padding-top:5px}
.tool-out-wait{color:var(--hint);font-size:10px;font-style:italic}
.err-row .txt{color:var(--red)}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes pop{from{transform:scale(.5);opacity:0}to{transform:scale(1);opacity:1}}
@keyframes fadein{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}
</style>
</head>
<body>
<div id="header">
  <div id="spinner"></div>
  <div id="status">Processing…</div>
  <div id="timer">0s</div>
</div>
<div id="log"></div>
<script>
const RUN_ID="__RUN_ID__";
let t0=Date.now();
const timerEl=document.getElementById("timer");
const statusEl=document.getElementById("status");
const spinnerEl=document.getElementById("spinner");
const logEl=document.getElementById("log");
let iv=setInterval(()=>timerEl.textContent=Math.max(0,Math.floor((Date.now()-t0)/1000))+"s",1000);
let atBottom=true, rendered=0, stopped=false;
let speakTxt=null, speakBuf="";
const toolMap=new Map();
logEl.addEventListener("scroll",()=>{atBottom=logEl.scrollTop+logEl.clientHeight>=logEl.scrollHeight-30;});
function scrollDown(){if(atBottom) logEl.scrollTop=logEl.scrollHeight;}
function escHtml(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
function row(prefixHTML,contentHTML,extraClass){
  const d=document.createElement("div");
  d.className="row"+(extraClass?" "+extraClass:"");
  d.innerHTML=`<span class="pfx">${prefixHTML}</span><span class="txt">${contentHTML}</span>`;
  logEl.appendChild(d);scrollDown();return d;
}
function toolIcon(name){
  if(/read|open|view/i.test(name)) return "📖";
  if(/write|edit|create/i.test(name)) return "✏️";
  if(/bash|run|exec|shell/i.test(name)) return "⚡";
  if(/search|grep|find/i.test(name)) return "🔍";
  if(/list/i.test(name)) return "📋";
  if(/send|message|telegram/i.test(name)) return "📤";
  if(/web|browse|fetch/i.test(name)) return "🌐";
  return "🔧";
}
function fmtArgs(inp){
  if(!inp||typeof inp!=="object") return "";
  const keys=Object.keys(inp).filter(k=>inp[k]!==undefined&&inp[k]!=="");
  if(!keys.length) return "";
  const parts=keys.slice(0,3).map(k=>{
    let v=inp[k];
    if(typeof v==="string"&&v.length>40) v=v.slice(0,40)+"…";
    else if(typeof v==="object") v=JSON.stringify(v).slice(0,40);
    return `${k}=${v}`;
  });
  return parts.join(" ");
}
function makeToolChip(tid,name,input){
  const icon=toolIcon(name||"");
  let parsedInput=input;
  if(typeof input==="string"){try{parsedInput=JSON.parse(input);}catch(e){parsedInput={raw:input};}}
  const args=fmtArgs(parsedInput||{});
  const wrap=document.createElement("div");
  wrap.className="tool-wrap";wrap.id="tw-"+tid;
  const chip=document.createElement("div");
  chip.className="tool-chip";
  chip.innerHTML=`<span class="icon">${icon}</span><span class="name">${escHtml(name||"tool")}</span>${args?`<span class="args">${escHtml(args)}</span>`:""}<span class="xpd">›</span>`;
  const detail=document.createElement("div");
  detail.className="tool-detail";
  const isBash=/bash|run|exec|shell/i.test(name||"");
  const cmdText=isBash&&parsedInput&&parsedInput.command?("$ "+parsedInput.command):JSON.stringify(parsedInput||{},null,2);
  const outPre=document.createElement("pre");
  outPre.className="tool-output";
  outPre.innerHTML=`<span class="tool-out-wait">waiting for output…</span>`;
  detail.innerHTML=`<pre class="tool-cmd">${escHtml(cmdText)}</pre>`;
  detail.appendChild(outPre);
  toolMap.set(tid,outPre);
  chip.addEventListener("click",()=>wrap.classList.toggle("expanded"));
  wrap.appendChild(chip);wrap.appendChild(detail);
  const r=document.createElement("div");r.className="row";
  r.innerHTML=`<span class="pfx"></span>`;
  const txt=document.createElement("span");txt.className="txt";
  txt.appendChild(wrap);r.appendChild(txt);
  logEl.appendChild(r);scrollDown();
}
function flushSpeak(){speakTxt=null;speakBuf="";}
function appendSpeak(delta){
  if(!delta) return;
  if(!speakTxt){const d=row("▶","",null);speakTxt=d.querySelector(".txt");}
  speakBuf+=delta;speakTxt.textContent=speakBuf;scrollDown();
}
function setSpeak(text){
  if(!text) return;
  if(!speakTxt){const d=row("▶","",null);speakTxt=d.querySelector(".txt");}
  speakBuf=text;speakTxt.textContent=text;scrollDown();
}
function handleApEvent(e){
  const k=e.kind, p=e.payload||{};
  if(k==="thinking"){
    const short=String(p.text||"").slice(0,160).replace(/[\r\n]+/g," ");
    if(short) row("💭","<span class='thinking'>"+escHtml(short)+"…</span>",null);
  } else if(k==="tool_call"){
    flushSpeak();
    makeToolChip(p.id||("t"+Date.now()),p.name||"tool",p.input);
  } else if(k==="tool_result"){
    const outPre=toolMap.get(p.tool_use_id);
    if(outPre){
      let out=p.content;
      if(typeof out!=="string") out=JSON.stringify(out,null,2);
      out=String(out);const MAX=3000;
      outPre.textContent=out.slice(0,MAX)+(out.length>MAX?" …(truncated)":"");
    }
  } else if(k==="llm_token"){
    appendSpeak(p.delta||"");
  } else if(k==="node_end"){
    const txt=String(p.text||"");
    if(txt&&txt.length>=speakBuf.length) setSpeak(txt);
    flushSpeak();
  } else if(k==="node_start"){
    flushSpeak();
  } else if(k==="error"||k==="cli.error"||k==="cli.timeout"){
    row("⚠️","<span style='color:var(--red)'>"+escHtml(String(p.error||p.msg||"error").slice(0,300))+"</span>","err-row");
  }
}
function done(interrupted){
  clearInterval(iv);
  timerEl.textContent=Math.max(0,Math.floor((Date.now()-t0)/1000))+"s";
  if(interrupted){
    spinnerEl.innerHTML="!";spinnerEl.style.cssText="width:14px;height:14px;background:var(--yellow);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:9px;color:#000;font-weight:700;flex-shrink:0";
    statusEl.textContent="Interrupted";statusEl.style.color="var(--yellow)";
  } else {
    spinnerEl.innerHTML="✓";spinnerEl.className="done";
    statusEl.textContent="Done";statusEl.style.color="var(--green)";
  }
}
function err(msg){
  clearInterval(iv);
  spinnerEl.innerHTML="!";spinnerEl.style.cssText="width:14px;height:14px;background:var(--red);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:9px;color:#fff;font-weight:700";
  statusEl.textContent="Error";statusEl.style.color="var(--red)";
  row("⚠️","<span style='color:var(--red)'>"+escHtml(msg)+"</span>","err-row");
}
async function poll(){
  if(stopped) return;
  try{
    const r=await fetch("/api/telegram/progress/"+RUN_ID+"/events",{cache:"no-store"});
    const d=await r.json();
    if(d.started_at) t0=d.started_at*1000;
    const evs=d.events||[];
    for(let i=rendered;i<evs.length;i++) handleApEvent(evs[i]);
    rendered=evs.length;
    const st=d.status||"";
    if(["success","completed"].indexOf(st)>=0){stopped=true;flushSpeak();done(false);return;}
    if(["cancelled","canceled","aborted"].indexOf(st)>=0){stopped=true;flushSpeak();done(true);return;}
    if(["error","failed"].indexOf(st)>=0){stopped=true;flushSpeak();err("Run failed");return;}
    if(st==="not_found"&&rendered===0){statusEl.textContent="Waiting…";}
  }catch(e){}
  setTimeout(poll,1200);
}
poll();
window.Telegram&&window.Telegram.WebApp&&window.Telegram.WebApp.ready&&window.Telegram.WebApp.ready();
window.Telegram&&window.Telegram.WebApp&&window.Telegram.WebApp.expand&&window.Telegram.WebApp.expand();
</script>
</body>
</html>
"""
