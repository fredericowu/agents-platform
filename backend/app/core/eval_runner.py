"""Eval framework — multi-step, multi-assert.

### Dataset shape (new, preferred)

```jsonc
[
  {
    "name": "case label (optional)",
    "context": "fresh" | "keep",           // default fresh (each step is a new run)
    "steps": [
      {
        "prompt": "write a file at /tmp/x with 'hello'",
        "asserts": [
          {"kind": "response_contains", "value": "wrote"},
          {"kind": "response_regex",    "pattern": "Wrote .+\\.txt"},
          {"kind": "tool_called",       "name":  "write_file"},
          {"kind": "tool_called_with",  "name":  "write_file",
                                        "input_contains": {"path": "/tmp/x"}},
          {"kind": "tool_output_contains", "name": "write_file",
                                           "value": "File created"},
          {"kind": "no_errors"},
          {"kind": "status",            "value": "success"}
        ]
      },
      {
        "prompt": "now read it",
        "asserts": [
          {"kind": "tool_called", "name": "read_file"}
        ]
      }
    ]
  }
]
```

### Back-compat (old shape)

A list of ``{"input": "...", "expected": "..."}`` items is translated into a
single-step case with one ``response_contains`` assert per item.

### Scoring

Case passes when every step in it passes; step passes when every assert in it
passes. Overall score = passing cases / total cases. Per-step and per-assert
detail is recorded in the EvalRun's ``cases`` field.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from typing import Any

from ..db import session_scope
from ..models import Eval, EvalRun, Run, RunEvent
from .events import bus
from .executor import run_agent, run_workflow


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------

def _normalize_dataset(dataset: list[Any], legacy_metric: str,
                       legacy_metric_args: dict) -> list[dict]:
    """Convert old {input, expected} cases into the new {steps:[...]} shape."""
    out: list[dict] = []
    for raw in dataset or []:
        if isinstance(raw, dict) and "steps" in raw:
            out.append(raw)
            continue
        # legacy
        prompt = raw.get("input", "")
        expected = raw.get("expected", "")
        asserts: list[dict] = []
        if legacy_metric == "assert_contains":
            asserts.append({"kind": "response_contains", "value": expected})
        elif legacy_metric == "judge_llm":
            asserts.append({"kind": "response_contains", "value": expected})
        elif legacy_metric == "tool_sequence_match":
            asserts.append({"kind": "response_contains", "value": expected})
        elif legacy_metric == "cmd_returns_zero":
            cmd = legacy_metric_args.get("cmd", "true")
            asserts.append({"kind": "cmd_returns_zero", "cmd": cmd})
        else:
            asserts.append({"kind": "response_contains", "value": expected})
        out.append({
            "name": raw.get("name") or f"case",
            "context": "fresh",
            "steps": [{"prompt": prompt, "asserts": asserts}],
        })
    return out


# ---------------------------------------------------------------------------
# assert evaluation
# ---------------------------------------------------------------------------

def _events_for_run(run_id: str) -> list[dict]:
    with session_scope() as s:
        return [{"kind": e.kind, "node_id": e.node_id, "payload": e.payload}
                for e in s.query(RunEvent).filter(RunEvent.run_id == run_id)
                                          .order_by(RunEvent.ts).all()]


def _descend_run_ids(root_id: str) -> list[str]:
    """All run ids in a run tree (root + descendants)."""
    out: list[str] = []
    with session_scope() as s:
        def walk(rid: str):
            out.append(rid)
            for c in s.query(Run).filter(Run.parent_run_id == rid).all():
                walk(c.id)
        walk(root_id)
    return out


def _gather_events_recursive(root_id: str) -> list[dict]:
    """Flatten every event from every run in the tree (so workflow asserts can
    see tool calls made by their child agents)."""
    out: list[dict] = []
    for rid in _descend_run_ids(root_id):
        out.extend(_events_for_run(rid))
    return out


def _input_contains(actual: Any, expected: dict[str, Any]) -> bool:
    """Deep-match: every key in expected must exist in actual (recursively for
    dicts). Strings use case-insensitive substring match."""
    if not isinstance(actual, dict) or not isinstance(expected, dict):
        return False
    for k, v in expected.items():
        if k not in actual:
            return False
        a = actual[k]
        if isinstance(v, dict):
            if not _input_contains(a, v): return False
        elif isinstance(v, str):
            if v.lower() not in str(a).lower(): return False
        else:
            if a != v: return False
    return True


def evaluate_assert(spec: dict, response_text: str, events: list[dict],
                    run_status: str) -> tuple[bool, str]:
    """Returns (passed, detail-message)."""
    kind = spec.get("kind") or "response_contains"
    rt = response_text or ""

    if kind == "response_contains":
        val = str(spec.get("value", ""))
        ok = val.lower() in rt.lower()
        return ok, f"contains {val!r}" if ok else f"missing {val!r}"

    if kind == "response_regex":
        pat = str(spec.get("pattern", spec.get("value", "")))
        try:
            ok = re.search(pat, rt) is not None
        except re.error as e:
            return False, f"bad regex {pat!r}: {e}"
        return ok, f"regex {pat!r} matched" if ok else f"regex {pat!r} did not match"

    if kind == "tool_called":
        name = spec.get("name")
        hits = [e for e in events if e["kind"] == "tool_call"
                and (not name or e["payload"].get("name") == name)]
        return bool(hits), (f"tool {name!r} called {len(hits)}×" if hits
                            else f"tool {name!r} not called")

    if kind == "tool_called_with":
        name = spec.get("name")
        want = spec.get("input_contains", {})
        hits = [e for e in events if e["kind"] == "tool_call"
                and (not name or e["payload"].get("name") == name)
                and _input_contains(e["payload"].get("input"), want)]
        return bool(hits), (f"tool {name!r} called with {want}" if hits
                            else f"no {name!r} call matched input {want}")

    if kind == "tool_output_contains":
        name = spec.get("name")
        val = str(spec.get("value", ""))
        # match tool_result events; name filtering is loose (tool_result may not
        # carry the name, so we accept any with the substring or pair name+next)
        results = [e for e in events if e["kind"] == "tool_result"]
        if name:
            named = [e for e in results if (e["payload"].get("name") or "").lower() == name.lower()]
            if named:
                results = named
        ok = any(val.lower() in str(e["payload"].get("content", "")).lower() for e in results)
        return ok, (f"tool output contained {val!r}" if ok
                    else f"no tool result contained {val!r}")

    if kind == "no_errors":
        err = [e for e in events if e["kind"] == "error"]
        ok = not err and run_status != "error"
        return ok, "no error events" if ok else f"{len(err)} error event(s) and status={run_status}"

    if kind == "status":
        want = spec.get("value", "success")
        ok = run_status == want
        return ok, f"status={run_status}" if ok else f"want status={want}, got {run_status}"

    if kind == "cmd_returns_zero":
        cmd = spec.get("cmd", "true")
        rc = subprocess.run(cmd, shell=True, capture_output=True).returncode
        return rc == 0, f"`{cmd}` exit {rc}"

    return False, f"unknown assert kind {kind!r}"


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

async def run_eval(slug: str) -> dict[str, Any]:
    with session_scope() as s:
        e = s.query(Eval).filter(Eval.slug == slug).first()
        if not e:
            raise ValueError(f"eval not found: {slug}")
        raw_dataset = list(e.dataset or [])
        legacy_metric = e.metric or "assert_contains"
        legacy_metric_args = dict(e.metric_args or {})
        target_kind = e.target_kind
        target_slug = e.target_slug

        er = EvalRun(eval_slug=slug, status="running")
        s.add(er); s.flush()
        eval_run_id = er.id

    dataset = _normalize_dataset(raw_dataset, legacy_metric, legacy_metric_args)
    case_results: list[dict[str, Any]] = []
    pass_count = 0

    for i, case in enumerate(dataset):
        name = case.get("name") or f"case {i+1}"
        ctx_mode = (case.get("context") or "fresh").lower()
        steps = case.get("steps") or []
        case_passed = True
        step_results: list[dict[str, Any]] = []

        # multi-turn history for context=keep
        history: list[dict] = []

        for j, step in enumerate(steps):
            prompt = step.get("prompt", "")
            asserts = step.get("asserts") or []

            if target_kind == "agent":
                res = await run_agent(target_slug, prompt,
                                      extra_messages=list(history) if ctx_mode == "keep" else None)
            else:  # workflow
                res = await run_workflow(target_slug, prompt)

            response_text = res.get("text") if target_kind == "agent" else (
                json.dumps(res.get("output"), default=str) if res.get("output") else "")
            run_status = res.get("status") or "unknown"
            run_id = res.get("run_id")
            events = _gather_events_recursive(run_id) if run_id else []

            # evaluate each assert
            assert_results: list[dict[str, Any]] = []
            step_pass = True
            for spec in asserts:
                ok, detail = evaluate_assert(spec, response_text or "", events, run_status)
                assert_results.append({"kind": spec.get("kind"), "passed": ok, "detail": detail, "spec": spec})
                if not ok:
                    step_pass = False

            step_results.append({
                "i": j,
                "prompt": prompt,
                "response": (response_text or "")[:600],
                "run_id": run_id,
                "asserts": assert_results,
                "passed": step_pass,
            })
            if not step_pass:
                case_passed = False

            if ctx_mode == "keep":
                history.append({"role": "user", "content": prompt})
                if response_text:
                    history.append({"role": "assistant", "content": response_text})

        if case_passed:
            pass_count += 1
        case_results.append({
            "i": i, "name": name, "context": ctx_mode,
            "steps": step_results, "passed": case_passed,
        })

    score = pass_count / max(1, len(dataset))
    with session_scope() as s:
        er = s.query(EvalRun).filter(EvalRun.id == eval_run_id).first()
        if er:
            er.status = "success"
            er.score = score
            er.cases = case_results
            er.ended_at = datetime.utcnow()
    return {"eval_run_id": eval_run_id, "score": score, "cases": case_results}


# legacy single-case scoring helper retained for backwards-compat imports
def score_case(metric: str, expected: str, actual: str, args: dict[str, Any]) -> bool:
    actual = actual or ""
    expected = expected or ""
    if metric == "assert_contains":
        return expected.lower() in actual.lower()
    if metric == "cmd_returns_zero":
        cmd = args.get("cmd", "true")
        return subprocess.run(cmd, shell=True, capture_output=True).returncode == 0
    return expected in actual
