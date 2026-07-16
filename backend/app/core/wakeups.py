"""Honour the claude-cli ``ScheduleWakeup`` tool inside Agents Platform, and
(since 2026-07-07) the ``call_me_back`` completion callback for
``run_agent_async`` / ``run_workflow_async``.

In an interactive Claude Code terminal the harness process stays alive, holds
the timer and re-invokes the model when it fires. In AP every run is a one-shot
``claude -p`` subprocess: the model schedules the wakeup in good faith, the
process exits seconds later and the timer dies with it.

This module closes that gap for three distinct triggers that all end up doing
the same thing — re-invoke an ORIGIN session and ship the reply back down
whatever channel it came from:

**Timer-based (``ScheduleWakeup``):**
- ``executor._run_agent_impl`` spots the ``ScheduleWakeup`` tool_call in the
  CLI event stream and, when the run finishes successfully, calls
  ``schedule_wakeup`` — persisted to the ``scheduled_wakeups`` table so it
  survives an AP restart, then armed as an asyncio timer (``_fire_after``).

**Event-based, agent-level (agent-to-agent "call me back"):**
- An agent dispatches ``run_agent_async`` / ``run_workflow_async`` with
  ``call_me_back`` not explicitly ``false`` (the default is to call back).
  ``executor._run_agent_impl`` captures the child run_id from the tool_result
  and, once ITS OWN run ends, calls ``register_agent_callback`` — an in-memory
  asyncio task (``_watch_and_callback``) that polls the child run until it's
  terminal, then re-invokes the ORIGIN session with a summary of the child's
  result. NOT yet persisted across an AP restart (unlike timer wakeups) — a
  restart mid-flight silently drops pending callbacks; fine for a v1, follow
  up if that turns out to matter in practice. This only ever resolves ONE
  hop — it wakes whoever dispatched THIS run, nothing further up the chain.

**Event-based, flow-level (2026-07-16 — "flow finished", distinct from "agent
finished"):** in a multi-hop chain (e.g. Telegram -> Architect -> Product
Owner, each hop dispatched with ``call_me_back=true``), the agent-level
callback above resolves as soon as each hop's OWN turn ends — Telegram gets
woken the moment Architect's first turn finishes, long before the flow (which
may bounce through several more agents) actually concludes, and there is no
per-hop mechanism left to notify Telegram once it does. ``FlowWaiter``
(``models.py``) closes that gap: keyed by ``flow_run_id`` (shared by every hop
in the chain, see ``Run.flow_run_id`` / ``executor._record_flow_hop``),
first-writer-wins — ``register_agent_callback`` calls
``_register_flow_waiter`` to record whichever run FIRST asked for a callback
anywhere in the flow as that flow's waiter. When any hop later calls
``mark_flow_done``/``mark_flow_planned`` (however many hops deep), ``_deliver_
flow_done`` resumes that waiter once — a separate wake-up from the agent-level
one, not a replacement for it.

All three paths converge on the same delivery: telegram via the recovery path
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
from typing import Any

from ..db import session_scope
from ..models import FlowWaiter, Run, ScheduledWakeup

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
        if fired_run_id:
            # Tag this resumed turn with whatever flow the session's own
            # prior turn belonged to — see executor._inherit_flow_from_session.
            from .executor import _inherit_flow_from_session
            _inherit_flow_from_session(fired_run_id, session_id)
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


RETURN_KINDS = ("result", "question", "blocker")

_RETURN_KIND_LABEL = {
    "result": "✅ Resultado do agente que você chamou",
    "question": "❓ Pergunta do agente que você chamou",
    "blocker": "🚧 Bloqueio reportado pelo agente que você chamou",
}


async def return_to_caller(*, run_id: str, message: str, kind: str) -> dict[str, Any]:
    """Explicit agentic-flow action: the CALLEE decides to send ``message``
    back to whoever called it — resolved via its own row's ``parent_run_id``
    (always set on any run_agent_async-dispatched run, independent of
    ``call_me_back`` — see ``_resolve_hop_count``), NOT via the
    callback_origin_run_id/register_agent_callback machinery (which only
    arms when the caller set call_me_back not-false).

    ``kind`` (one of RETURN_KINDS) is the agent's structured declaration of
    WHAT it's sending back — persisted on ``Run.return_kind`` so a caller
    (or a human auditing runs) can check the *tool call*, not just parse
    ``message`` prose. Validated here (not just at the MCP schema layer) so
    a malformed direct HTTP call can't slip an unlisted value through.

    No-op (but still ``{"ok": True, "noop": True}``, not a silent failure)
    when this run's own ``call_me_back`` is already True — the automatic
    callback path (``_watch_and_callback``) will already resume the caller
    when this run ends, and firing both would double-resume it.
    """
    if kind not in RETURN_KINDS:
        return {"ok": False, "reason": f"kind must be one of {RETURN_KINDS}, got {kind!r}"}

    with session_scope() as s:
        own = s.query(Run).filter(Run.id == run_id).first()
        if own is None:
            return {"ok": False, "reason": f"run {run_id} not found"}
        # Mark unconditionally (even on the "no caller"/no-op branches below) —
        # calling this tool at all is the signal that the agent deliberately
        # tried to report back, which is what the Agents Flow "lost agent"
        # safety net (_took_flow_action) checks for.
        own.return_to_caller_done = True
        own.return_kind = kind
        if own.call_me_back:
            return {"ok": True, "noop": True,
                    "reason": ("call_me_back is already true on this run — the automatic "
                              "callback will deliver your result to the caller when this "
                              "run ends, no need to call return_to_caller_agent")}
        parent_run_id = own.parent_run_id

    if not parent_run_id:
        return {"ok": False, "reason": "no caller — this run has no parent_run_id (it's the root of its chain)"}

    with session_scope() as s:
        parent = s.query(Run).filter(Run.id == parent_run_id).first()
        if parent is None:
            return {"ok": False, "reason": f"caller run {parent_run_id} not found"}
        agent_slug = parent.source_slug
        target_id = parent.target_id
        session_id = parent.session_id
        initiator_kind = parent.initiator_kind
        initiator_id = parent.initiator_id

    if not agent_slug or not session_id:
        return {"ok": False, "reason": "caller run is missing agent_slug/session_id — can't resume it"}

    channel = _resolve_channel(initiator_kind, parent_run_id, session_id)
    label = _RETURN_KIND_LABEL[kind]
    prompt = f"{label} (run {run_id}):\n\n{message}"
    fired_run_id, err = await _rerun_and_deliver(
        f"return-to-caller {run_id}", agent_slug=agent_slug, prompt=prompt,
        session_id=session_id, target_id=target_id, initiator_id=initiator_id or "",
        channel=channel or "",
    )
    if err:
        return {"ok": False, "reason": err}
    return {"ok": True, "resumed_run_id": fired_run_id, "caller_agent": agent_slug, "kind": kind}


_FLOW_DONE_LABEL = {
    "success": "concluído com sucesso",
    "partial": "concluído parcialmente",
    "failed": "encerrado sem sucesso",
    "planned": "planejado (aguardando implementação)",
}


async def _deliver_flow_done(*, run_id: str, summary: str, outcome: str) -> None:
    """Flow-level wakeup — distinct from the per-hop callback in
    ``_watch_and_callback``/``register_agent_callback`` (which only ever
    resolves ONE hop and typically already fired long before the flow as a
    whole concluded). Called from mark_flow_done / mark_flow_planned: resumes
    whichever run first registered as this flow's waiter
    (``_register_flow_waiter``), however many hops removed that is from
    ``run_id``. No-ops quietly if no one registered as a waiter for this
    flow, the waiter was already delivered to (one-shot per flow instance),
    or the waiter run can't be resumed (missing agent/session) — this must
    never raise, since it's a best-effort tail on top of the actual
    mark_flow_done/mark_flow_planned outcome."""
    try:
        with session_scope() as s:
            own = s.query(Run).filter(Run.id == run_id).first()
            if own is None or not own.flow_run_id:
                return
            waiter = (s.query(FlowWaiter)
                      .filter(FlowWaiter.flow_run_id == own.flow_run_id,
                              FlowWaiter.delivered.is_(False))
                      .first())
            if waiter is None:
                return
            # Atomic claim — guards a race if mark_flow_done fired from more
            # than one hop of the same flow around the same time.
            claimed = (s.query(FlowWaiter)
                       .filter(FlowWaiter.id == waiter.id, FlowWaiter.delivered.is_(False))
                       .update({"delivered": True, "delivered_at": datetime.utcnow()}))
            if not claimed:
                return
            origin_run_id = waiter.origin_run_id

        with session_scope() as s:
            origin = s.query(Run).filter(Run.id == origin_run_id).first()
            if origin is None:
                log.warning("flow-done: waiter origin run %s vanished (flow=%s)",
                           origin_run_id, run_id)
                return
            agent_slug = origin.source_slug
            target_id = origin.target_id
            session_id = origin.session_id
            initiator_kind = origin.initiator_kind
            initiator_id = origin.initiator_id

        if not agent_slug or not session_id:
            log.info("flow-done: waiter run=%s can't resume (missing agent=%s session=%s)",
                     origin_run_id, agent_slug, session_id)
            return

        channel = _resolve_channel(initiator_kind, origin_run_id, session_id)
        label = _FLOW_DONE_LABEL.get(outcome, outcome)
        prompt = (f"O fluxo (Agents Flow) que você iniciou foi {label} (run {run_id}). "
                 f"Resumo:\n\n{summary.strip() or '(sem resumo)'}")
        await _rerun_and_deliver(
            f"flow-done {run_id}", agent_slug=agent_slug, prompt=prompt, session_id=session_id,
            target_id=target_id, initiator_id=initiator_id or "", channel=channel or "",
        )
    except Exception:
        log.warning("flow-done delivery failed run=%s", run_id, exc_info=True)


FLOW_OUTCOMES = ("success", "partial", "failed")


def _find_qa_run_for_context(s, *, notion_task_id: str | None, target_id: str | None,
                             exclude_run_id: str) -> str | None:
    """Best-effort lookup for mark_flow_done's QA auto-resolution: the most
    recent SUCCEEDED run of any agent whose slug starts with ``qa-`` (the
    project's naming convention — qa-haiku, qa-sonnet, ...) against the same
    context. Same Kanban card (``notion_task_id``) when there is one,
    otherwise same ``target_id``. Never matches ``exclude_run_id`` itself.
    Returns None (not an error) when nothing matches — the caller decides
    what that means."""
    from ..models import Agent
    qa_slugs = [a.slug for a in
                s.query(Agent.slug).filter(Agent.slug.like("qa-%"), Agent.deleted_at.is_(None)).all()]
    if not qa_slugs:
        return None
    q = s.query(Run).filter(Run.source_slug.in_(qa_slugs), Run.status == "success",
                            Run.id != exclude_run_id)
    if notion_task_id:
        q = q.filter(Run.notion_task_id == notion_task_id)
    elif target_id:
        q = q.filter(Run.target_id == target_id)
    else:
        return None
    match = q.order_by(Run.ended_at.desc().nullslast(), Run.started_at.desc()).first()
    return match.id if match else None


async def mark_flow_done(*, run_id: str, summary: str, outcome: str,
                         qa_run_id: str | None = None, qa_not_needed: bool = False) -> dict[str, Any]:
    """Explicit agentic-flow action: the agent declares the task finished —
    the third of the 3 terminal actions (alongside handoff and
    return_to_caller_agent). Marks ``Run.marked_flow_done`` and persists
    ``outcome`` on ``Run.flow_outcome`` — the agent's structured verdict,
    checkable via the tool call itself (see FLOW_OUTCOMES) rather than by
    parsing ``summary`` prose.

    ``outcome`` also decides which Kanban status this drives when the run
    carries a card (notion_task_id): "success"/"partial" move it to
    ``done``; "failed" moves it to ``need_human`` instead (the task did NOT
    conclude successfully — that's a human-needed outcome, not a done one),
    reusing ``summary`` as the required need_human comment. Without a card,
    marking the run is the only effect.

    **QA accountability — enforced here, not just at the MCP schema layer:**
    exactly one of ``qa_run_id`` (the Run.id of the QA agent run that
    reviewed this work) or ``qa_not_needed=True`` (an explicit declaration
    that no QA pass applies) must end up set. Both given at once →
    rejected (contradictory). Neither given → before rejecting, this
    AUTO-RESOLVES: it looks for a QA agent run (any agent whose slug starts
    with ``qa-``) that already succeeded against the same context (same
    ``notion_task_id``, or same ``target_id`` when there's no card) and
    uses that run's id — a flow can fan out into several hops before the
    one that finally calls mark_flow_done, and that hop has no way to know
    which earlier hop's run_id a QA agent used. Only if no such run is
    found does this actually reject and ask the caller to pass one of the
    two explicitly. A supplied ``qa_run_id`` must also resolve to a real
    run — a fabricated id is rejected rather than silently trusted.

    If the card IS linked but the move is rejected (e.g. the hard lock in
    /api/notion/kanban/move that blocks done/ready_to_deploy/need_human while
    QAStatus=In Progress — see notion_kanban.py), this must NOT fall back to
    set-qa-status: that would stamp QAStatus=Done and force the card to
    "done" out from under an *active* QA review that this run has nothing to
    do with, silently clobbering it. Instead — same pattern as
    core.executor._escalate_need_human — ping sysadmins on Telegram so a
    human notices the card wasn't updated, rather than dropping it silently."""
    if outcome not in FLOW_OUTCOMES:
        return {"ok": False, "reason": f"outcome must be one of {FLOW_OUTCOMES}, got {outcome!r}"}
    if outcome == "failed" and not summary.strip():
        return {"ok": False, "reason": "summary is required when outcome='failed' — it becomes the "
                                       "need_human comment explaining what went wrong"}

    qa_run_id = (qa_run_id or "").strip() or None
    if qa_run_id and qa_not_needed:
        return {"ok": False, "reason": "pass either qa_run_id or qa_not_needed=True, not both"}

    with session_scope() as s:
        own = s.query(Run).filter(Run.id == run_id).first()
        if own is None:
            return {"ok": False, "reason": f"run {run_id} not found"}

        if qa_run_id:
            qa_run = s.query(Run.id).filter(Run.id == qa_run_id).first()
            if qa_run is None:
                return {"ok": False, "reason": f"qa_run_id {qa_run_id!r} does not match any known run"}
        elif not qa_not_needed:
            auto_qa_run_id = _find_qa_run_for_context(
                s, notion_task_id=own.notion_task_id, target_id=own.target_id, exclude_run_id=run_id)
            if auto_qa_run_id:
                qa_run_id = auto_qa_run_id
                log.info("mark-flow-done auto-resolved qa_run_id=%s for run=%s (context match, "
                        "caller didn't pass qa_run_id/qa_not_needed)", auto_qa_run_id, run_id)
            else:
                return {"ok": False, "reason": "QA accountability is required — call mark_flow_done "
                                               "again with either qa_run_id=<the QA run's Run.id> (if "
                                               "a QA agent reviewed this work) or qa_not_needed=True "
                                               "(if no QA pass applies here). No prior QA run was found "
                                               "automatically for this context."}

        own.marked_flow_done = True
        own.flow_outcome = outcome
        own.qa_run_id = qa_run_id
        own.qa_not_needed = qa_not_needed
        notion_task_id = own.notion_task_id

    await _deliver_flow_done(run_id=run_id, summary=summary, outcome=outcome)

    if not notion_task_id:
        return {"ok": True, "notion_task_id": None}

    kanban_status = "need_human" if outcome == "failed" else "done"
    import os as _os

    def _notify_mark_done_rejected(extra: str) -> None:
        try:
            from ..api.telegram import notify_sysadmins
            notify_sysadmins(f"🆘 Agents Flow mark_flow_done couldn't update Kanban — run "
                             f"{run_id}, card {notion_task_id}, outcome={outcome}.{extra}\n"
                             f"https://agents-platform.app.aw.tekflox.com/runs/{run_id}")
        except Exception:
            log.warning("mark-flow-done sysadmin notify failed run=%s", run_id, exc_info=True)

    try:
        import httpx as _httpx
        awserv = _os.environ.get("AWSERV_BASE", "http://127.0.0.1:9123")
        api_key = ""
        try:
            key_path = _os.path.join(_os.environ.get("AW_BASE_DIR", "/opt/agentic-workspace"), ".tmp", "awserv_api_key")
            with open(key_path) as _f:
                api_key = _f.read().strip()
        except Exception:
            pass
        headers = {"X-Api-Key": api_key} if api_key else {}
        async with _httpx.AsyncClient(timeout=10.0) as c:
            resp = await c.post(f"{awserv}/api/notion/kanban/move",
                                json={"page_id": notion_task_id, "status": kanban_status,
                                      "comment": summary, "run_id": run_id},
                                headers=headers)
        if resp.status_code != 200:
            _notify_mark_done_rejected(
                f"\n(move to {kanban_status} was rejected — {resp.status_code}: {resp.text[:200]} — "
                "likely mid-QA-cycle hard lock. Card status was NOT updated, check it manually.)")
            return {"ok": False, "reason": f"kanban move to {kanban_status} failed: {resp.status_code} {resp.text[:200]}"}
    except Exception as e:
        _notify_mark_done_rejected(f"\n(move to {kanban_status} failed — {e}. Card status was NOT updated, check it manually.)")
        return {"ok": False, "reason": f"kanban move to {kanban_status} failed: {e}"}
    return {"ok": True, "notion_task_id": notion_task_id, "kanban_status": kanban_status, "outcome": outcome}


async def mark_flow_planned(*, run_id: str, summary: str) -> dict[str, Any]:
    """A 4th terminal action, alongside handoff / return_to_caller_agent /
    mark_flow_done — for PLANNING work (design, ADR, spec) rather than
    implementation. Distinct from mark_flow_done("success") because
    planning concluding does NOT mean the feature is done/shippable — it
    means a plan now exists and is ready for someone to build against. No
    QA accountability is required here (there's no code to review yet).

    Sets ``Run.marked_flow_done = True`` (so the Agents Flow safety net's
    ``_took_flow_action`` check is satisfied — planning IS a valid way to
    conclude a flow turn, not a special case it needs to know about
    separately) and ``Run.flow_outcome = "planned"`` (distinguishable from
    success/partial/failed in the run's own record).

    If this run carries a Kanban card, moves it to the ``planned`` status
    (a new column, alongside backlog/ready/done/etc. — see
    notion.agents_kanban.statuses in aw.json) with ``summary`` as the
    card comment. Without a card, marking the run is the only effect — the
    plan lives in this run's own output/context, which is exactly where
    Frederico asked for it to be when there's nothing to persist it to."""
    with session_scope() as s:
        own = s.query(Run).filter(Run.id == run_id).first()
        if own is None:
            return {"ok": False, "reason": f"run {run_id} not found"}
        own.marked_flow_done = True
        own.flow_outcome = "planned"
        notion_task_id = own.notion_task_id

    await _deliver_flow_done(run_id=run_id, summary=summary, outcome="planned")

    if not notion_task_id:
        return {"ok": True, "notion_task_id": None, "outcome": "planned"}

    import os as _os

    def _notify_mark_planned_rejected(extra: str) -> None:
        try:
            from ..api.telegram import notify_sysadmins
            notify_sysadmins(f"🆘 Agents Flow mark_flow_planned couldn't update Kanban — run "
                             f"{run_id}, card {notion_task_id}.{extra}\n"
                             f"https://agents-platform.app.aw.tekflox.com/runs/{run_id}")
        except Exception:
            log.warning("mark-flow-planned sysadmin notify failed run=%s", run_id, exc_info=True)

    try:
        import httpx as _httpx
        awserv = _os.environ.get("AWSERV_BASE", "http://127.0.0.1:9123")
        api_key = ""
        try:
            key_path = _os.path.join(_os.environ.get("AW_BASE_DIR", "/opt/agentic-workspace"), ".tmp", "awserv_api_key")
            with open(key_path) as _f:
                api_key = _f.read().strip()
        except Exception:
            pass
        headers = {"X-Api-Key": api_key} if api_key else {}
        async with _httpx.AsyncClient(timeout=10.0) as c:
            resp = await c.post(f"{awserv}/api/notion/kanban/move",
                                json={"page_id": notion_task_id, "status": "planned",
                                      "comment": summary, "run_id": run_id},
                                headers=headers)
        if resp.status_code != 200:
            _notify_mark_planned_rejected(
                f"\n(move to planned was rejected — {resp.status_code}: {resp.text[:200]} — "
                "likely mid-QA-cycle hard lock. Card status was NOT updated, check it manually.)")
            return {"ok": False, "reason": f"kanban move to planned failed: {resp.status_code} {resp.text[:200]}"}
    except Exception as e:
        _notify_mark_planned_rejected(f"\n(move to planned failed — {e}. Card status was NOT updated, check it manually.)")
        return {"ok": False, "reason": f"kanban move to planned failed: {e}"}
    return {"ok": True, "notion_task_id": notion_task_id, "kanban_status": "planned", "outcome": "planned"}


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
        _register_flow_waiter(s, origin_run_id)
    log.info("agent-callback armed: watch_run=%s origin=%s", watch_run_id, origin_run_id)
    asyncio.create_task(_watch_and_callback(watch_run_id))
    return True


def _register_flow_waiter(s, origin_run_id: str) -> None:
    """First-writer-wins: record ``origin_run_id`` as the run waiting for its
    ENTIRE flow to conclude, not just the one child it dispatched — keyed by
    ``flow_run_id`` (shared by every hop in the chain, see Run.flow_run_id).
    Only the first call_me_back dispatch inside a flow instance claims this
    row; a later nested dispatch (e.g. Architect calling Product Owner) must
    NOT overwrite it, or a deep hop's own immediate caller would wrongly
    become "the flow's waiter" instead of the channel-facing root that
    actually needs the final wake-up. See mark_flow_done's flow-done delivery
    below for the consumer side."""
    origin = s.query(Run).filter(Run.id == origin_run_id).first()
    if origin is None or not origin.flow_run_id:
        return
    if s.query(FlowWaiter.id).filter(FlowWaiter.flow_run_id == origin.flow_run_id).first():
        return
    s.add(FlowWaiter(flow_run_id=origin.flow_run_id, origin_run_id=origin_run_id))


_CALLBACK_DB_MAX_RETRIES = 5  # transient DB errors per poll (pool timeout, dropped connection)
_CALLBACK_DB_RETRY_BACKOFF_S = 2  # doubles each retry: 2, 4, 8, 16, 32s
# Redis pub/sub (executor.notify_run_finished) wakes this up immediately when
# the watched run finalises. This fallback interval only covers a missed
# publish (subscribe/publish race, Redis blip) — normal case never waits it out.
_CALLBACK_FALLBACK_POLL_S = 30
_CALLBACK_MAX_ITERS = (_CALLBACK_POLL_S * _CALLBACK_MAX_POLLS) // _CALLBACK_FALLBACK_POLL_S  # same ~1h ceiling


async def _watch_and_callback(watch_run_id: str) -> None:
    """Wait for ``watch_run_id`` to go terminal, then deliver the callback.

    Event-driven: blocks on a Redis pub/sub signal (``notify_run_finished``,
    fired by executor.py right after it commits the run's terminal status)
    instead of sleeping and re-querying Postgres on a fixed cadence. The DB
    row is still the source of truth — a wake-up (real or fallback-timeout)
    only means "go check it", never "assume success". If Redis is down or a
    publish is missed, ``wait_run_finished`` returns False after
    ``_CALLBACK_FALLBACK_POLL_S`` and we just re-check the DB anyway, so this
    degrades to the old polling behaviour (at a coarser interval) rather than
    hanging forever.

    A transient DB error here (pool exhaustion, dropped connection) must not
    kill this asyncio task outright — an unhandled exception in a
    fire-and-forget ``asyncio.create_task`` is only visible as "Task
    exception was never retrieved" in the logs, with no retry and no signal
    to the user that their callback was silently dropped (confirmed
    2026-07-14: a QueuePool timeout did exactly this). Each check gets its
    own short retry-with-backoff before giving up on THAT check and moving to
    the next wait cycle; only exhausting retries across
    ``_CALLBACK_DB_MAX_RETRIES`` consecutive attempts (not just one) escalates
    to ERROR and abandons the watch."""
    from .redis_streams import wait_run_finished
    terminal = {"success", "error", "cancelled"}
    status, output, run_error, origin_run_id = None, None, None, None
    consecutive_db_errors = 0
    for _ in range(_CALLBACK_MAX_ITERS):
        try:
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
        except Exception as e:  # noqa: BLE001 — transient DB error, retry before giving up
            consecutive_db_errors += 1
            log.warning("agent-callback: DB error checking watch_run=%s (attempt %d/%d): %s",
                       watch_run_id, consecutive_db_errors, _CALLBACK_DB_MAX_RETRIES, e)
            if consecutive_db_errors >= _CALLBACK_DB_MAX_RETRIES:
                log.error("agent-callback: giving up on watch_run=%s after %d consecutive DB "
                         "errors — callback NOT delivered, origin_run=%s left unresumed",
                         watch_run_id, consecutive_db_errors, origin_run_id, exc_info=True)
                return
            await asyncio.sleep(_CALLBACK_DB_RETRY_BACKOFF_S * (2 ** (consecutive_db_errors - 1)))
            continue
        consecutive_db_errors = 0
        await wait_run_finished(watch_run_id, timeout_s=_CALLBACK_FALLBACK_POLL_S)
    else:
        log.warning("agent-callback: watched run %s never reached terminal after %ds",
                    watch_run_id, _CALLBACK_FALLBACK_POLL_S * _CALLBACK_MAX_ITERS)
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

    # `channel` only decides how (or whether) a human gets notified — it must
    # NOT gate whether the caller's session gets resumed. A run whose origin
    # isn't telegram/watch/wakeup (e.g. dispatched by the Kanban webhook, or
    # itself a plain agent-to-agent call — initiator_kind=="agent_run" in
    # both cases) has no channel, but the caller's session must still resume:
    # that's the entire point of agentic-flow hand-offs, where the "delivery"
    # that matters is the resumed session continuing to work the Kanban card,
    # not a human-facing notification. See _rerun_and_deliver / the
    # `deliver_recovered_run` / `_deliver_watch` callees — both already
    # self-guard when there's nothing to notify (2026-07-13 fix).
    channel = _resolve_channel(initiator_kind, origin_run_id, session_id) if session_id else None
    # Atomic claim (False->True) so a concurrent rearm can't double-fire.
    if not _mark_callback_done(watch_run_id):
        return
    if not agent_slug or not session_id:
        log.info("agent-callback: watch_run=%s can't resume (missing agent=%s session=%s)",
                 watch_run_id, agent_slug, session_id)
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
        session_id=session_id, target_id=target_id, initiator_id=initiator_id or "", channel=channel or "",
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


async def _fire_stuck_wakeup_run(run_id: str) -> None:
    """Actually execute a pre-created wakeup Run row that never got its
    scheduled `run_agent()` coroutine off the ground (see
    `rearm_stuck_wakeup_runs` for why this happens)."""
    with session_scope() as s:
        r = s.query(Run).filter(Run.id == run_id).first()
        if r is None or r.status != "pending":
            return  # vanished or already picked up by something else
        agent_slug = r.source_slug
        session_id = r.session_id
        target_id = r.target_id
        notion_task_id = r.notion_task_id
        user_input = (r.input or {}).get("input", "")
    if not agent_slug or not user_input:
        log.warning("stuck-wakeup-run %s missing agent_slug/input — leaving as-is", run_id)
        return
    try:
        from .executor import run_agent
        log.info("firing stuck wakeup run=%s agent=%s session=%s", run_id, agent_slug,
                 (session_id or "")[:8])
        result = await run_agent(agent_slug, user_input, run_id=run_id, session_id=session_id,
                                 target_id=target_id, notion_task_id=notion_task_id,
                                 initiator_kind="wakeup")
        out = (result or {}).get("reply") or (result or {}).get("text", "")
        if out:
            from ..api.telegram import deliver_recovered_run
            await deliver_recovered_run(run_id, out)
    except Exception:
        log.warning("failed to fire stuck wakeup run=%s", run_id, exc_info=True)


_STUCK_WAKEUP_MIN_AGE_S = 20  # don't race a run whose fire-and-forget task is merely still in flight


def rearm_stuck_wakeup_runs() -> int:
    """Re-fire wakeup-initiated Run rows that were pre-created (to avoid a
    race — see `_rerun_and_deliver` / `ask_human`'s answer handler, both of
    which insert the Run row with status='pending' BEFORE scheduling the
    actual `run_agent()` coroutine as fire-and-forget via
    `asyncio.run_coroutine_threadsafe`) but never executed, because the
    process that scheduled them died/restarted before that coroutine ran.

    Unlike `rearm_pending_agent_callbacks` (which re-arms WATCHERS for runs
    still in flight) and `recover_orphaned_runs` (which only re-attaches runs
    that reached status='running'), nothing previously covered this case: a
    Run stuck at status='pending' with initiator_kind='wakeup' is invisible
    to both — the fire-and-forget scheduling was the only thing that would
    have ever moved it forward, and that's exactly what a restart destroys.
    Confirmed live 2026-07-15 (run left pending indefinitely after an
    `ask_human` answer landed right as the backend was mid-restart for an
    unrelated deploy).

    Only re-fires rows older than `_STUCK_WAKEUP_MIN_AGE_S` — a fresh pending
    row's fire-and-forget task may simply not have started yet (this function
    also runs at boot, when nothing has had a chance to run at all)."""
    cutoff = datetime.utcnow() - timedelta(seconds=_STUCK_WAKEUP_MIN_AGE_S)
    with session_scope() as s:
        ids = [r.id for r in s.query(Run).filter(
            Run.status == "pending", Run.initiator_kind == "wakeup",
            Run.started_at < cutoff).all()]
    for rid in ids:
        asyncio.create_task(_fire_stuck_wakeup_run(rid))
    if ids:
        log.info("re-fired %d stuck wakeup run(s)", len(ids))
    return len(ids)
