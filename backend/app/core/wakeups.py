"""Honour the claude-cli ``ScheduleWakeup`` tool inside Agents Platform, and
(since 2026-07-07) the ``call_me_back`` completion callback for
``run_agent_async`` / ``run_workflow_async``.

In an interactive Claude Code terminal the harness process stays alive, holds
the timer and re-invokes the model when it fires. In AP every run is a one-shot
``claude -p`` subprocess: the model schedules the wakeup in good faith, the
process exits seconds later and the timer dies with it.

This module closes that gap for two distinct triggers that both end up doing
the same thing — re-invoke the ORIGIN session and ship the reply back down
whatever channel it came from:

**Timer-based (``ScheduleWakeup``):**
- ``executor._run_agent_impl`` spots the ``ScheduleWakeup`` tool_call in the
  CLI event stream and, when the run finishes successfully, calls
  ``schedule_wakeup`` — persisted to the ``scheduled_wakeups`` table so it
  survives an AP restart, then armed as an asyncio timer (``_fire_after``).

**Event-based (agent-to-agent "call me back"):**
- An agent dispatches ``run_agent_async`` / ``run_workflow_async`` with
  ``call_me_back`` not explicitly ``false`` (the default is to call back).
  ``executor._run_agent_impl`` captures the child run_id from the tool_result
  and, once ITS OWN run ends, calls ``register_agent_callback`` — an in-memory
  asyncio task (``_watch_and_callback``) that polls the child run until it's
  terminal, then re-invokes the ORIGIN session with a summary of the child's
  result. NOT yet persisted across an AP restart (unlike timer wakeups) — a
  restart mid-flight silently drops pending callbacks; fine for a v1, follow
  up if that turns out to matter in practice.

Both paths converge on the same delivery: telegram via the recovery path
(``deliver_recovered_run``, bot/chat from the inherited ``initiator_id``),
watch/meta/glasses via awserv's ``POST /api/meta/agent_push`` (history + WS
broadcast + spoken TTS, falling back to a real APNs alert push if nothing's
connected — see ``_deliver_watch``).

``rearm_pending_wakeups`` re-arms pending timer rows at boot (``main.lifespan``).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from ..db import session_scope
from ..models import Run, ScheduledWakeup

log = logging.getLogger("wakeups")

_MIN_DELAY_S = 10
_MAX_DELAY_S = 24 * 3600

# Agent-callback polling: cheap and simple beats clever for a v1 — no new
# table, no bus subscription, just poll the child run's status.
_CALLBACK_POLL_S = 3
_CALLBACK_MAX_POLLS = 1200  # 3s * 1200 = 1h ceiling before we give up


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

    fired_run_id, err = await _rerun_and_deliver(
        wakeup_id, agent_slug=agent_slug, prompt=prompt, session_id=session_id,
        target_id=target_id, initiator_id=initiator_id, channel=channel,
    )

    with session_scope() as s:
        (s.query(ScheduledWakeup)
         .filter(ScheduledWakeup.id == wakeup_id)
         .update({"status": "error" if err else "fired",
                  "fired_run_id": fired_run_id, "error": err}))


async def _rerun_and_deliver(log_id: str, *, agent_slug: str, prompt: str, session_id: str,
                             target_id: str | None, initiator_id: str, channel: str,
                             ) -> tuple[str | None, str | None]:
    """Re-invoke ``agent_slug`` on ``session_id`` with ``prompt`` and ship the
    reply down ``channel``. Shared tail for both timer wakeups and
    agent-callback events — the only difference between them is what decides
    it's time to fire, not what happens once it does."""
    fired_run_id, err = None, None
    try:
        from .executor import run_agent
        log.info("%s firing: agent=%s session=%s", log_id, agent_slug, session_id[:8])
        result = await run_agent(agent_slug, prompt, session_id=session_id,
                                 target_id=target_id, initiator_kind="wakeup",
                                 initiator_id=initiator_id)
        fired_run_id = (result or {}).get("run_id")
        out = (result or {}).get("reply") or (result or {}).get("text", "")
        if (result or {}).get("status") != "success":
            err = (result or {}).get("error") or "run did not succeed"
        elif out and fired_run_id:
            if channel == "watch":
                await _deliver_watch(initiator_id, out, fired_run_id)
            else:
                from ..api.telegram import deliver_recovered_run
                await deliver_recovered_run(fired_run_id, out)
    except Exception as e:  # noqa: BLE001 — caller records the failure
        err = str(e)
        log.warning("%s failed: %s", log_id, e)
    return fired_run_id, err


def register_agent_callback(*, watch_run_id: str, origin_run_id: str) -> bool:
    """Wire up 'call me back' for a run_agent_async/run_workflow_async dispatch
    (``call_me_back`` not explicitly ``false`` — the default).

    Persists the request directly on the CHILD run's own row
    (``call_me_back``, ``callback_origin_run_id``) instead of holding it in
    memory — no denormalized copy of the origin's session/channel either;
    ``_watch_and_callback`` looks that up fresh from the origin run's row each
    time it fires, so a restart's ``rearm_pending_agent_callbacks`` can revive
    a watcher from nothing but the child run's id.
    """
    with session_scope() as s:
        r = s.query(Run).filter(Run.id == watch_run_id).first()
        if r is None:
            log.info("agent-callback ignored (watch run %s not found)", watch_run_id)
            return False
        r.call_me_back = True
        r.callback_origin_run_id = origin_run_id
        r.callback_done = False
    log.info("agent-callback armed: watch_run=%s origin=%s", watch_run_id, origin_run_id)
    asyncio.create_task(_watch_and_callback(watch_run_id))
    return True


async def _watch_and_callback(watch_run_id: str) -> None:
    terminal = {"success", "error", "cancelled"}
    status, output, run_error, origin_run_id = None, None, None, None
    for _ in range(_CALLBACK_MAX_POLLS):
        with session_scope() as s:
            r = s.query(Run).filter(Run.id == watch_run_id).first()
            if r is None:
                log.warning("agent-callback: watched run %s vanished", watch_run_id)
                return
            if r.callback_done:
                return  # already handled (e.g. a rearm raced this same task)
            origin_run_id = r.callback_origin_run_id
            if r.status in terminal:
                status, run_error = r.status, r.error
                output = (r.output or {}).get("text", "") if isinstance(r.output, dict) else None
                break
        await asyncio.sleep(_CALLBACK_POLL_S)
    else:
        log.warning("agent-callback: watched run %s never reached terminal after %ds",
                    watch_run_id, _CALLBACK_POLL_S * _CALLBACK_MAX_POLLS)
        return

    if not origin_run_id:
        log.warning("agent-callback: watch run %s has no origin_run_id", watch_run_id)
        _mark_callback_done(watch_run_id)
        return

    with session_scope() as s:
        origin = s.query(Run).filter(Run.id == origin_run_id).first()
        if origin is None:
            log.warning("agent-callback: origin run %s vanished (watch=%s)",
                        origin_run_id, watch_run_id)
            _mark_callback_done(watch_run_id)
            return
        agent_slug = origin.source_slug
        target_id = origin.target_id
        session_id = origin.session_id
        initiator_kind = origin.initiator_kind
        initiator_id = origin.initiator_id

    channel = _resolve_channel(initiator_kind, origin_run_id, session_id) if session_id else None
    # Atomic claim (False->True) so a concurrent rearm can't double-fire.
    if not _mark_callback_done(watch_run_id):
        return
    if not channel or not initiator_id or not agent_slug or not session_id:
        log.info("agent-callback: watch_run=%s not deliverable (agent=%s session=%s channel=%s)",
                 watch_run_id, agent_slug, session_id, channel)
        return

    if status == "success":
        summary = (output or "").strip() or "(sem texto de retorno)"
        prompt = (f"O agente que voce chamou via run_agent_async (run {watch_run_id}) "
                 f"terminou com sucesso. Resultado:\n\n{summary}")
    else:
        prompt = (f"O agente que voce chamou via run_agent_async (run {watch_run_id}) "
                 f"terminou com status '{status}'. Erro: {run_error or '(sem detalhe)'}")

    await _rerun_and_deliver(
        f"agent-callback {watch_run_id}", agent_slug=agent_slug, prompt=prompt,
        session_id=session_id, target_id=target_id, initiator_id=initiator_id, channel=channel,
    )


def _mark_callback_done(watch_run_id: str) -> bool:
    """Atomic False->True claim on callback_done. Returns True iff THIS call
    won the claim (guards against a boot-rearm racing a still-running task)."""
    with session_scope() as s:
        claimed = (s.query(Run)
                   .filter(Run.id == watch_run_id, Run.callback_done.is_(False))
                   .update({"callback_done": True}))
    return bool(claimed)


def rearm_pending_agent_callbacks() -> int:
    """Re-arm watchers for any run with call_me_back requested but not yet
    delivered — survives an AP restart mid-flight (call at boot)."""
    with session_scope() as s:
        ids = [r.id for r in s.query(Run).filter(
            Run.call_me_back.is_(True), Run.callback_done.is_(False)).all()]
    for rid in ids:
        asyncio.create_task(_watch_and_callback(rid))
    if ids:
        log.info("re-armed %d pending agent-callback(s) after restart", len(ids))
    return len(ids)


async def _deliver_watch(device_session_id: str, text: str, run_id: str = "") -> None:
    """Ship a wakeup reply to a glasses/watch/iOS session via awserv, which
    owns those devices: appends to the shared chat history, broadcasts over
    the session WebSocket and speaks it (TTS) if a client is connected.

    ``run_id`` (this wakeup's own fired run) MUST be forwarded — awserv's
    agent_push() stamps it as `hist_msg_id=ap-<run_id>:out` on the live
    broadcast (poll + instant silent push). Without it, awserv falls back to
    a local `db-<row>` id for the live delivery, while a later history
    refetch (_ap_history) reconstructs this same reply with the real
    `ap-<run_id>:out` id — two different ids for one reply defeats the
    Watch's seenMsgIds dedup and the reply gets spoken twice (duplicate
    playback report, 2026-07-11).
    """
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
                         json={"session_id": device_session_id, "text": text, "run_id": run_id},
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
