"""Step definitions shared by all feature files."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from behave import given, when, then

REPO = Path(__file__).resolve().parents[3]
BASE = os.environ.get("AGENTS_BASE", "http://127.0.0.1:8765")


# ----- given -----

@given('the backend is running')
def step_backend(context):
    r = httpx.get(f"{BASE}/api/health", timeout=5)
    assert r.status_code == 200, r.text


# ----- when (api) -----

@when('I list the agents')
def step_list_agents(context):
    context.agents = httpx.get(f"{BASE}/api/agents").json()

@when('I list the workflows')
def step_list_wfs(context):
    context.workflows = httpx.get(f"{BASE}/api/workflows").json()

@when('I list the skills')
def step_list_skills(context):
    context.skills = httpx.get(f"{BASE}/api/skills").json()

@when('I refresh the MCP servers')
def step_refresh_mcp(context):
    context.mcp = httpx.post(f"{BASE}/api/mcp/refresh", timeout=15).json()


def _wait_for_run(rid: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = httpx.get(f"{BASE}/api/runs/{rid}").json()
        if r["status"] in ("success", "error", "cancelled"):
            return r
        time.sleep(0.3)
    raise AssertionError(f"run {rid} did not finish in {timeout}s")


@when('I run the agent "{slug}" with input "{text}"')
def step_run_agent(context, slug, text):
    rid = httpx.post(f"{BASE}/api/agents/{slug}/run",
                     json={"input": {"input": text}}).json()["run_id"]
    context.run = _wait_for_run(rid)
    context.events = httpx.get(f"{BASE}/api/runs/{rid}/events").json()


@when('I run the workflow "{slug}" with input "{text}"')
def step_run_wf(context, slug, text):
    rid = httpx.post(f"{BASE}/api/workflows/{slug}/run",
                     json={"input": {"input": text}}).json()["run_id"]
    context.run = _wait_for_run(rid, timeout=60)
    context.events = httpx.get(f"{BASE}/api/runs/{rid}/events").json()


@when('I run the eval "{slug}"')
def step_run_eval(context, slug):
    context.eval_result = httpx.post(f"{BASE}/api/evals/{slug}/run", timeout=60).json()


# ----- when (cli) -----

@when('I run "{cmd}"')
def step_run_cli(context, cmd):
    venv_bin = REPO / ".venv" / "bin"
    parts = cmd.split()
    # resolve `agent` to the venv binary
    if parts and parts[0] == "agent":
        parts[0] = str(venv_bin / "agent")
    proc = subprocess.run(parts, capture_output=True, text=True,
                          env={**os.environ, "PATH": f"{venv_bin}:{os.environ.get('PATH', '')}"},
                          cwd=str(REPO), timeout=60)
    context.cli_rc = proc.returncode
    context.cli_stdout = proc.stdout
    context.cli_stderr = proc.stderr


# ----- when (mcp) -----

async def _mcp_call(name: str, args: dict | None = None, *, list_only: bool = False):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    py = REPO / ".venv" / "bin" / "python"
    params = StdioServerParameters(command=str(py), args=["-m", "mcp_server.agent_mcp"],
                                   env={**os.environ}, cwd=str(REPO))
    # Behave captures stderr; need a real file descriptor for subprocess
    errlog = open(REPO / "data" / "mcp-bdd.log", "ab")
    try:
        async with stdio_client(params, errlog=errlog) as (r, w):
            async with ClientSession(r, w) as sess:
                await sess.initialize()
                if list_only:
                    return [t.name for t in (await sess.list_tools()).tools]
                res = await sess.call_tool(name, arguments=args or {})
                return res.content[0].text if res.content else ""
    finally:
        errlog.close()


@when('I connect to agent-mcp')
def step_mcp_connect(context):
    context.mcp_tools = asyncio.run(_mcp_call("", list_only=True))


@when('I call MCP tool "{tool}" with input "{text}"')
def step_mcp_call(context, tool, text):
    context.mcp_result = asyncio.run(_mcp_call(tool, {"input": text}))


# ----- then -----

@then('I see at least {n:d} agents')
def step_n_agents(context, n):
    assert len(context.agents) >= n, f"only {len(context.agents)} agents"

@then('I see at least {n:d} workflows')
def step_n_wfs(context, n):
    assert len(context.workflows) >= n

@then('the list contains "{slug}"')
def step_list_contains(context, slug):
    target = context.agents if hasattr(context, "agents") else context.workflows
    assert any(x["slug"] == slug for x in target), f"missing {slug}"

@then('the run completes with status "{status}"')
def step_run_status(context, status):
    assert context.run["status"] == status, f"got {context.run['status']}: {context.run.get('error')}"

@then('the run output text contains "{text}"')
def step_run_output_text(context, text):
    out = context.run.get("output") or {}
    text_val = out.get("text") or json.dumps(out)
    assert text in text_val, f"{text!r} not in {text_val[:200]!r}"

@then('the run has tokens recorded')
def step_run_tokens(context):
    assert context.run["tokens_in"] > 0 or context.run["tokens_out"] > 0

@then('the run output has key "{key}"')
def step_run_output_key(context, key):
    out = context.run.get("output") or {}
    assert key in out, f"{key!r} not in output keys: {list(out.keys())}"

@then('the run has at least {n:d} events')
def step_run_events(context, n):
    assert len(context.events) >= n, f"only {len(context.events)} events"

@then('the command exits zero')
def step_cli_rc(context):
    assert context.cli_rc == 0, f"rc={context.cli_rc} stderr={context.cli_stderr[:300]}"

@then('stdout contains "{text}"')
def step_cli_stdout(context, text):
    # strip color codes for matching
    clean = "".join(ch for ch in context.cli_stdout if ch != "\x1b" )
    assert text in context.cli_stdout or text in clean, f"{text!r} not in stdout"

@then('the tool list includes "{name}"')
def step_tool_includes(context, name):
    assert name in context.mcp_tools, f"{name} not in {context.mcp_tools}"

@then('the result contains "{text}"')
def step_result_contains(context, text):
    assert text in context.mcp_result, f"{text!r} not in {context.mcp_result[:300]!r}"

@then('the eval score is {score:f}')
def step_eval_score(context, score):
    actual = context.eval_result["score"]
    assert abs(actual - score) < 1e-3, f"score {actual} != {score}"

@then('all eval cases pass')
def step_eval_all_pass(context):
    for c in context.eval_result["cases"]:
        assert c["passed"], f"case {c['i']} failed: {c}"

@then('the server list contains "{name}"')
def step_mcp_server(context, name):
    assert any(s["name"] == name for s in context.mcp), \
        f"{name} not in {[s['name'] for s in context.mcp]}"

@then('the skills list contains "{slug}"')
def step_skills_contains(context, slug):
    assert any(sk["slug"] == slug for sk in context.skills), \
        f"{slug} not in {[s['slug'] for s in context.skills]}"


# ---- CRUD steps ----

@when('I create a model with slug "{slug}" provider "{provider}"')
def step_create_model(context, slug, provider):
    r = httpx.post(f"{BASE}/api/models", json={
        "slug": slug, "provider": provider, "model_id": slug,
        "display_name": slug, "params": {}, "enabled": True,
    })
    assert r.status_code == 200, r.text


@when('I disable the model "{slug}"')
def step_disable_model(context, slug):
    r = httpx.put(f"{BASE}/api/models/{slug}", json={"enabled": False})
    assert r.status_code == 200


@when('I delete the model "{slug}"')
def step_delete_model(context, slug):
    r = httpx.delete(f"{BASE}/api/models/{slug}")
    assert r.status_code == 200


@then('the model "{slug}" exists in the list')
def step_model_exists(context, slug):
    rows = httpx.get(f"{BASE}/api/models").json()
    assert any(m["slug"] == slug for m in rows), f"{slug} not found"


@then('the model "{slug}" is disabled')
def step_model_disabled(context, slug):
    rows = httpx.get(f"{BASE}/api/models").json()
    row = next((m for m in rows if m["slug"] == slug), None)
    assert row and not row["enabled"]


@then('the model "{slug}" is not in the list')
def step_model_absent(context, slug):
    rows = httpx.get(f"{BASE}/api/models").json()
    assert not any(m["slug"] == slug for m in rows), f"{slug} still present"


@when('I add an MCP server "{name}" with command "{cmd}"')
def step_add_mcp(context, name, cmd):
    r = httpx.post(f"{BASE}/api/mcp/servers", json={
        "name": name, "command": cmd, "args": [], "env": {}, "enabled": True,
    })
    assert r.status_code == 200, r.text


@when('I delete the MCP server "{name}"')
def step_delete_mcp(context, name):
    r = httpx.delete(f"{BASE}/api/mcp/servers/{name}")
    assert r.status_code == 200


@then('the MCP server "{name}" exists in the list')
def step_mcp_exists(context, name):
    rows = httpx.get(f"{BASE}/api/mcp/servers").json()
    assert any(s["name"] == name for s in rows)


@then('the MCP server "{name}" has source "{source}"')
def step_mcp_source(context, name, source):
    rows = httpx.get(f"{BASE}/api/mcp/servers").json()
    row = next((s for s in rows if s["name"] == name), None)
    assert row and row["source"] == source


@then('the MCP server "{name}" is not in the list')
def step_mcp_absent(context, name):
    rows = httpx.get(f"{BASE}/api/mcp/servers").json()
    assert not any(s["name"] == name for s in rows)


@when('I create an eval with slug "{slug}"')
def step_create_eval(context, slug):
    r = httpx.post(f"{BASE}/api/evals", json={
        "slug": slug, "name": slug, "description": "",
        "target_kind": "agent", "target_slug": "echo-coder",
        "dataset": [{"input": "x", "expected": "x"}],
        "metric": "assert_contains", "metric_args": {},
    })
    assert r.status_code == 200, r.text


@when('I delete the eval "{slug}"')
def step_delete_eval(context, slug):
    r = httpx.delete(f"{BASE}/api/evals/{slug}")
    assert r.status_code == 200


@then('the eval "{slug}" exists in the list')
def step_eval_exists(context, slug):
    rows = httpx.get(f"{BASE}/api/evals").json()
    assert any(e["slug"] == slug for e in rows)


@then('the eval "{slug}" is not in the list')
def step_eval_absent(context, slug):
    rows = httpx.get(f"{BASE}/api/evals").json()
    assert not any(e["slug"] == slug for e in rows)


# ---- Tree lineage steps ----

@then('the run tree includes at least {n:d} total runs')
def step_tree_total(context, n):
    rid = context.run["id"]
    tree = httpx.get(f"{BASE}/api/runs/{rid}/tree").json()
    context.tree = tree
    assert tree["totals"]["runs"] >= n, f"only {tree['totals']['runs']} runs in tree"


@then('the run tree has at least {n:d} child runs')
def step_tree_children(context, n):
    children = context.tree["root"]["children"]
    assert len(children) >= n, f"only {len(children)} children"


@then('every child run has parent_run_id equal to the workflow run id')
def step_tree_parent_ok(context):
    rid = context.run["id"]
    for c in context.tree["root"]["children"]:
        assert c["parent_run_id"] == rid, f"child {c['id']} parent={c['parent_run_id']} != {rid}"
