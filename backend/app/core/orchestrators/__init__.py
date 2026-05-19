"""Orchestrator dispatcher. Each kind drives the graph differently and emits events."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

from ..cancel import Cancelled, is_cancelled
from ..events import bus
from .. import hops

RunAgentFn = Callable[..., Awaitable[dict]]


def derive_kind_from_graph(graph: dict, fallback: str | None = None) -> str:
    """Infer the workflow kind from the graph shape.

    Unambiguous topologies:
      - graph has ``stages``            → pipeline
      - graph has ``orchestrator``+``workers`` → orchestrator_worker
      - graph has ``participants``      → group_chat

    Ambiguous (``nodes`` shape — sequential vs parallel): use
    ``graph.concurrency`` ("sequential" | "parallel"; default sequential),
    falling back to the legacy ``kind`` argument if neither is present.
    """
    g = graph or {}
    if "stages" in g:
        return "pipeline"
    if "orchestrator" in g and "workers" in g:
        return "orchestrator_worker"
    if "participants" in g:
        return "group_chat"
    if "nodes" in g:
        concurrency = (g.get("concurrency") or "").lower()
        if concurrency == "parallel":
            return "parallel"
        if concurrency in {"sequential", "serial", ""} and fallback in {"parallel"}:
            # Honor an explicit legacy kind=parallel when the graph itself is silent.
            return "parallel"
        return "sequential"
    # last resort
    return fallback or "sequential"


async def dispatch(kind: str, graph: dict, user_input: Any, run_id: str,
                   run_agent: RunAgentFn, *, root_run_id: str | None = None) -> Any:
    # The graph is the source of truth. The stored ``kind`` is a hint /
    # disambiguator only.
    resolved = derive_kind_from_graph(graph, fallback=kind)
    fn_map = {
        "sequential": run_sequential,
        "parallel": run_parallel,
        "orchestrator_worker": run_orchestrator_worker,
        "pipeline": run_pipeline,
        "group_chat": run_group_chat,
    }
    fn = fn_map.get(resolved)
    if fn is None:
        raise ValueError(f"unknown workflow kind: {resolved}")
    return await fn(graph, user_input, run_id, run_agent,
                    root_run_id=root_run_id or run_id)


def _node_input(template: str | None, user_input: Any, prev: Any) -> str:
    if template is None:
        text = prev if isinstance(prev, str) else json.dumps(prev) if prev is not None else (
            user_input if isinstance(user_input, str) else json.dumps(user_input))
        return text
    sub_input = user_input if isinstance(user_input, str) else json.dumps(user_input)
    sub_prev = prev if isinstance(prev, str) else json.dumps(prev) if prev is not None else ""
    return template.replace("{input}", sub_input).replace("{prev}", sub_prev)


async def _run_node(node: dict, payload: str, run_id: str, run_agent: RunAgentFn,
                    root_run_id: str | None = None) -> dict:
    if is_cancelled(run_id):
        raise Cancelled(f"workflow {run_id} cancelled before node {node.get('id')}")
    root = root_run_id or run_id
    # Gate this execution against the token budget first. Then charge a hop.
    # Both raise (HopLimitExceeded / TokenLimitExceeded) when over budget —
    # orchestrators catch BudgetExceeded and stop gracefully.
    hops.check_tokens(root)
    count = hops.increment(root)
    nid = node["id"]
    label = node.get("label", nid)
    agent_slug = node["agent"]
    snapshot = hops.get(root)
    await bus.publish(run_id, "node_start",
                      {"label": label, "agent": agent_slug, "hop": count,
                       "budget": snapshot},
                      node_id=nid)
    res = await run_agent(agent_slug, payload, node_id=nid)
    # Charge tokens — but only for *leaf* (agent) executions. Sub-workflow
    # nodes (``agent: "workflow:<slug>"``) already had their inner agent
    # nodes charge tokens, so the outer node must not double-count the rollup.
    if not (isinstance(agent_slug, str) and agent_slug.startswith("workflow:")):
        used = (res.get("tokens_in", 0) or 0) + (res.get("tokens_out", 0) or 0)
        if used:
            hops.add_tokens(root, used)
    out = res.get("text", "")
    snapshot = hops.get(root)
    await bus.publish(run_id, "node_end", {"label": label, "text": out[:500],
                                           "tokens_in": res.get("tokens_in", 0),
                                           "tokens_out": res.get("tokens_out", 0),
                                           "child_run_id": res.get("run_id"),
                                           "hop": count,
                                           "budget": snapshot},
                      node_id=nid)
    return res


def _stamp_limit(out: dict, reason: str | None) -> dict:
    """Tag an orchestrator's return dict with which budget was hit (if any)."""
    if reason:
        out["limit_reached"] = reason            # "hops" or "tokens"
        out[f"{reason}_limit_reached"] = True    # back-compat / explicit flag
    return out


# ---- 1. sequential ----
async def run_sequential(graph, user_input, run_id, run_agent, *, root_run_id=None):
    nodes = graph.get("nodes", [])
    prev = None
    last = None
    completed = 0
    reason: str | None = None
    for n in nodes:
        try:
            payload = _node_input(n.get("input_template"), user_input, prev)
            last = await _run_node(n, payload, run_id, run_agent, root_run_id)
        except hops.BudgetExceeded as be:
            reason = be.reason
            break
        prev = last.get("text")
        completed += 1
    return _stamp_limit({"output": prev, "final_run": last,
                         "completed": completed, "total": len(nodes)}, reason)


# ---- 2. parallel (fan-out, single barrier) ----
async def run_parallel(graph, user_input, run_id, run_agent, *, root_run_id=None):
    nodes = graph.get("nodes", [])
    payload_t = graph.get("input_template")
    async def _run_one(n):
        payload = _node_input(n.get("input_template", payload_t), user_input, None)
        return await _run_node(n, payload, run_id, run_agent, root_run_id)
    results = await asyncio.gather(*[_run_one(n) for n in nodes], return_exceptions=True)
    out = []
    reason: str | None = None
    for n, r in zip(nodes, results):
        if isinstance(r, hops.BudgetExceeded):
            reason = r.reason
            out.append({"node": n["id"], "skipped": f"{r.reason}_limit_reached"})
        elif isinstance(r, Exception):
            out.append({"node": n["id"], "error": str(r)})
        else:
            out.append({"node": n["id"], "text": r.get("text", "")})
    return _stamp_limit({"outputs": out}, reason)


# ---- 3. orchestrator-worker ----
async def run_orchestrator_worker(graph, user_input, run_id, run_agent, *, root_run_id=None):
    orch = graph["orchestrator"]
    workers = graph["workers"]
    synth = graph["synthesizer"]
    reason: str | None = None

    try:
        orch_payload = _node_input(orch.get("input_template"), user_input, None)
        orch_res = await _run_node(orch, orch_payload, run_id, run_agent, root_run_id)
        plan = orch_res.get("text", "")
    except hops.BudgetExceeded as be:
        return _stamp_limit({"plan": "", "workers": [], "synthesis": ""}, be.reason)

    async def _worker(n, idx):
        payload = _node_input(n.get("input_template", "{prev}"), user_input, plan)
        payload = payload + f"\n\n[worker #{idx + 1} of {len(workers)}]"
        return await _run_node(n, payload, run_id, run_agent, root_run_id)

    worker_results = await asyncio.gather(*[_worker(w, i) for i, w in enumerate(workers)],
                                          return_exceptions=True)
    parts = []
    for w, r in zip(workers, worker_results):
        if isinstance(r, hops.BudgetExceeded):
            reason = r.reason
            parts.append(f"[{w['id']} skipped — {r.reason}_limit_reached]")
        elif isinstance(r, Exception):
            parts.append(f"[{w['id']} error] {r}")
        else:
            parts.append(f"[{w['id']}]\n{r.get('text', '')}")
    combined = "\n\n---\n\n".join(parts)

    synthesis = ""
    try:
        synth_payload = _node_input(synth.get("input_template"), user_input, combined)
        synth_res = await _run_node(synth, synth_payload, run_id, run_agent, root_run_id)
        synthesis = synth_res.get("text", "")
    except hops.BudgetExceeded as be:
        reason = be.reason
    return _stamp_limit({"plan": plan, "workers": parts, "synthesis": synthesis}, reason)


# ---- 4. pipeline ----
async def run_pipeline(graph, user_input, run_id, run_agent, *, root_run_id=None):
    stages = graph.get("stages", [])
    prev = None
    history = []
    reason: str | None = None
    for st in stages:
        try:
            payload = _node_input(st.get("input_template"), user_input, prev)
            res = await _run_node(st, payload, run_id, run_agent, root_run_id)
        except hops.BudgetExceeded as be:
            reason = be.reason
            break
        prev = res.get("text", "")
        history.append({"stage": st["id"], "text": prev})
    return _stamp_limit({"final": prev, "history": history,
                         "completed": len(history), "total": len(stages)}, reason)


# ---- 5. group chat ----
async def run_group_chat(graph, user_input, run_id, run_agent, *, root_run_id=None):
    participants = graph.get("participants", [])
    max_turns = int(graph.get("max_turns", 6))
    transcript: list[dict] = [{"agent": "user", "text": user_input if isinstance(user_input, str) else json.dumps(user_input)}]
    reason: str | None = None
    for turn in range(max_turns):
        for p in participants:
            history = "\n\n".join(f"[{m['agent']}]\n{m['text']}" for m in transcript[-10:])
            payload = f"Conversation so far:\n{history}\n\n[Your turn: {p.get('label', p['id'])}]"
            try:
                res = await _run_node(p, payload, run_id, run_agent, root_run_id)
            except hops.BudgetExceeded as be:
                reason = be.reason
                break
            text = res.get("text", "")
            transcript.append({"agent": p.get("agent", p["id"]), "text": text})
            if "[DONE]" in text.upper() or "[STOP]" in text.upper():
                return {"transcript": transcript, "stopped_early": True, "turn": turn + 1}
        if reason:
            break
    return _stamp_limit({"transcript": transcript,
                         "stopped_early": bool(reason),
                         "turn": max_turns}, reason)
