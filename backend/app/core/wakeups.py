"""Honour the claude-cli ``ScheduleWakeup`` tool inside Agents Platform.

In an interactive Claude Code terminal the harness process stays alive, holds
the timer and re-invokes the model when it fires. In AP every run is a one-shot
``claude -p`` subprocess: the model schedules the wakeup in good faith, the
process exits seconds later and the timer dies with it.

This module closes that gap:

- ``executor._run_agent_impl`` spots the ``ScheduleWakeup`` tool_call in the
  CLI event stream and, when the run finishes successfully, calls
  ``schedule_wakeup`` — persisted to the ``scheduled_wakeups`` table so it
  survives an AP restart, then armed as an asyncio timer.
- When due, ``_fire_after`` runs the wakeup prompt on the SAME session
  (``executor.run_agent`` — the per-session lock queues it behind any
  conversation in flight) and ships the reply down the origin channel:
  telegram via the recovery path (``deliver_recovered_run``, bot/chat from
  the inherited ``initiator_id``), watch/meta/glasses via awserv's
  ``POST /api/meta/agent_push`` (history + WS broadcast + spoken TTS).
- ``rearm_pending_wakeups`` re-arms pending rows at boot (``main.lifespan``).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from ..db import session_scope
from ..models import ScheduledWakeup

log = logging.getLogger("wakeups")

_MIN_DELAY_S = 10
_MAX_DELAY_S = 24 * 3600


def _resolve_channel(initiator_kind: str | None, origin_run_id: str,
                     session_id: str) -> str | None:
    """Map the origin run's initiator to a delivery channel.

    A wakeup-fired run has ``initiator_kind == "wakeup"`` — for chains we
    inherit the channel from the wakeup row that is currently firing on this
    session (its ``fired_run_id`` is only written after the run returns, so
    match on the in-flight row instead)."""
    if initiator_kind in ("telegram", "watch"):
        return initiator_kind
    if initiator_kind == "wakeup":
        with session_scope() as s:
            prev = (s.query(ScheduledWakeup)
                    .filter(ScheduledWakeup.fired_run_id == origin_run_id)
                    .first()) or (s.query(ScheduledWakeup)
                                  .filter(ScheduledWakeup.session_id == session_id,
                                          ScheduledWakeup.status == "firing")
                                  .order_by(ScheduledWakeup.fire_at.desc())
                                  .first())
            return prev.channel if prev else None
    return None


def schedule_wakeup(*, origin_run_id: str, agent_slug: str, target_id: str | None,
                    session_id: str | None, initiator_kind: str | None,
                    initiator_id: str | None, req: dict) -> str | None:
    """Persist + arm a wakeup captured from a finished run. Returns the wakeup id."""
    prompt = str(req.get("prompt") or "").strip()
    try:
        delay = float(req.get("delaySeconds") or 0)
    except (TypeError, ValueError):
        delay = 0
    if not prompt or not session_id:
        log.info("wakeup ignored (no prompt/session) run=%s", origin_run_id)
        return None
    channel = _resolve_channel(initiator_kind, origin_run_id, session_id)
    if not channel or not initiator_id:
        log.info("wakeup ignored (initiator %s/%s not deliverable) run=%s",
                 initiator_kind, initiator_id, origin_run_id)
        return None
    delay = max(_MIN_DELAY_S, min(delay, _MAX_DELAY_S))
    fire_at = datetime.utcnow() + timedelta(seconds=delay)
    with session_scope() as s:
        # Idempotent per origin run — a re-attach replay must not double-arm.
        if s.query(ScheduledWakeup).filter(
                ScheduledWakeup.origin_run_id == origin_run_id).first():
            return None
        w = ScheduledWakeup(
            origin_run_id=origin_run_id, agent_slug=agent_slug, target_id=target_id,
            session_id=session_id, initiator_id=initiator_id, channel=channel,
            prompt=prompt, reason=str(req.get("reason") or "") or None, fire_at=fire_at,
        )
        s.add(w)
        s.flush()
        wid = w.id
    log.info("wakeup %s armed: agent=%s session=%s fires in %.0fs (%s)",
             wid, agent_slug, session_id[:8], delay, req.get("reason") or "no reason")
    asyncio.create_task(_fire_after(wid))
    return wid


async def _fire_after(wakeup_id: str) -> None:
    with session_scope() as s:
        w = s.query(ScheduledWakeup).filter(ScheduledWakeup.id == wakeup_id).first()
        if not w or w.status != "pending":
            return
        fire_at, agent_slug, target_id = w.fire_at, w.agent_slug, w.target_id
        session_id, initiator_id, prompt = w.session_id, w.initiator_id, w.prompt
        channel = w.channel or "telegram"

    delay = (fire_at - datetime.utcnow()).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)

    # Atomic pending→firing claim so boot re-arm + live arm can't both fire it.
    with session_scope() as s:
        claimed = (s.query(ScheduledWakeup)
                   .filter(ScheduledWakeup.id == wakeup_id,
                           ScheduledWakeup.status == "pending")
                   .update({"status": "firing"}))
    if not claimed:
        return

    fired_run_id, err = None, None
    try:
        from .executor import run_agent
        log.info("wakeup %s firing: agent=%s session=%s", wakeup_id, agent_slug, session_id[:8])
        result = await run_agent(agent_slug, prompt, session_id=session_id,
                                 target_id=target_id, initiator_kind="wakeup",
                                 initiator_id=initiator_id)
        fired_run_id = (result or {}).get("run_id")
        out = (result or {}).get("reply") or (result or {}).get("text", "")
        if (result or {}).get("status") != "success":
            err = (result or {}).get("error") or "wakeup run did not succeed"
        elif out and fired_run_id:
            if channel == "watch":
                await _deliver_watch(initiator_id, out)
            else:
                from ..api.telegram import deliver_recovered_run
                await deliver_recovered_run(fired_run_id, out)
    except Exception as e:  # noqa: BLE001 — must record any failure on the row
        err = str(e)
        log.warning("wakeup %s failed: %s", wakeup_id, e)

    with session_scope() as s:
        (s.query(ScheduledWakeup)
         .filter(ScheduledWakeup.id == wakeup_id)
         .update({"status": "error" if err else "fired",
                  "fired_run_id": fired_run_id, "error": err}))


async def _deliver_watch(device_session_id: str, text: str) -> None:
    """Ship a wakeup reply to a glasses/watch/iOS session via awserv, which
    owns those devices: appends to the shared chat history, broadcasts over
    the session WebSocket and speaks it (TTS) if a client is connected."""
    import os

    import httpx

    from ..config import settings

    awserv = os.environ.get("AWSERV_BASE", "http://127.0.0.1:9123")
    headers = {"X-Internal-Secret": settings.telegram_inject_secret}
    # awserv's global API middleware wants its own key on top of the shared
    # secret — same dance as _notify_kanban_run_done.
    try:
        key_path = os.path.join(os.environ.get("AW_BASE_DIR", "/opt/agentic-workspace"),
                                ".tmp", "awserv_api_key")
        with open(key_path) as f:
            headers["X-Api-Key"] = f.read().strip()
    except Exception:
        pass
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(f"{awserv}/api/meta/agent_push",
                         json={"session_id": device_session_id, "text": text},
                         headers=headers)
        r.raise_for_status()
        log.info("watch wakeup delivered to session %s: %s", device_session_id, r.json())


def rearm_pending_wakeups() -> int:
    """Arm asyncio timers for every pending wakeup (call once at boot)."""
    with session_scope() as s:
        ids = [w.id for w in s.query(ScheduledWakeup)
               .filter(ScheduledWakeup.status == "pending").all()]
    for wid in ids:
        asyncio.create_task(_fire_after(wid))
    if ids:
        log.info("re-armed %d pending wakeup(s) after restart", len(ids))
    return len(ids)
