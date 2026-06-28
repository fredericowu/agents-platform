"""agent-cli — invoke agents and workflows from the command line.

Two modes:
  • Connected mode (default): talks to a running backend at AGENTS_HOST:AGENTS_PORT.
    Boots the backend on demand if not reachable.
  • Direct mode (--direct): imports backend libs and runs in-process. Useful for
    headless scripts.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import click
import httpx
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

CON = Console()
DEFAULT_BASE = os.environ.get("AGENTS_BASE", "http://127.0.0.1:8765")


def _api(path: str) -> str:
    return f"{DEFAULT_BASE}{path}"


def _running(base: str = DEFAULT_BASE) -> bool:
    try:
        r = httpx.get(f"{base}/api/health", timeout=1.5)
        return r.status_code == 200
    except Exception:
        return False


def _ensure_running(start_if_down: bool = True) -> bool:
    if _running():
        return True
    if not start_if_down:
        return False
    CON.print("[yellow]backend not reachable; starting...[/yellow]")
    repo = Path(__file__).resolve().parents[1]
    log = repo / "data" / "server.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    venv_py = repo / ".venv" / "bin" / "python"
    py = str(venv_py) if venv_py.exists() else sys.executable
    proc = subprocess.Popen(
        [py, "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1",
         "--port", "8765", "--log-level", "warning"],
        cwd=str(repo), stdout=open(log, "ab"), stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(50):
        if _running():
            CON.print(f"[green]backend started (pid {proc.pid})[/green]")
            return True
        time.sleep(0.2)
    CON.print(f"[red]backend failed to start; see {log}[/red]")
    return False


@click.group(help="agent — multi-agent platform CLI")
@click.version_option("0.1.0")
def main() -> None:
    pass


# -------- list --------

@main.group("list", help="list resources")
def list_() -> None:
    pass


@list_.command("agents")
def list_agents():
    _ensure_running()
    rows = httpx.get(_api("/api/agents")).json()
    t = Table(title="Agents")
    t.add_column("slug"); t.add_column("name"); t.add_column("model"); t.add_column("tools")
    for r in rows:
        t.add_row(r["slug"], r["name"], r["model_slug"] or "-", str(len(r["tool_specs"])))
    CON.print(t)


@list_.command("workflows")
def list_workflows():
    _ensure_running()
    rows = httpx.get(_api("/api/workflows")).json()
    t = Table(title="Workflows")
    t.add_column("slug"); t.add_column("name"); t.add_column("kind")
    for r in rows:
        t.add_row(r["slug"], r["name"], r["kind"])
    CON.print(t)


@list_.command("models")
def list_models():
    _ensure_running()
    rows = httpx.get(_api("/api/models")).json()
    t = Table(title="Models")
    t.add_column("slug"); t.add_column("provider"); t.add_column("model id"); t.add_column("enabled")
    for r in rows:
        t.add_row(r["slug"], r["provider"], r["model_id"], "yes" if r["enabled"] else "no")
    CON.print(t)


@list_.command("runs")
@click.option("--limit", default=20, type=int)
def list_runs(limit: int):
    _ensure_running()
    rows = httpx.get(_api(f"/api/runs?limit={limit}")).json()
    t = Table(title="Runs")
    t.add_column("id"); t.add_column("kind"); t.add_column("target"); t.add_column("status"); t.add_column("tokens")
    for r in rows:
        t.add_row(r["id"][:8], r["kind"], r["target_slug"], r["status"], f"{r['tokens_in']}/{r['tokens_out']}")
    CON.print(t)


@list_.command("tools")
def list_tools():
    _ensure_running()
    rows = httpx.get(_api("/api/tools")).json()
    t = Table(title="Tools")
    t.add_column("id"); t.add_column("kind"); t.add_column("server"); t.add_column("description")
    for r in rows:
        t.add_row(r["id"], r["kind"], r.get("server") or "-", (r.get("description") or "")[:60])
    CON.print(t)


@list_.command("skills")
def list_skills_cmd():
    _ensure_running()
    rows = httpx.get(_api("/api/skills")).json()
    t = Table(title="Skills")
    t.add_column("slug"); t.add_column("description")
    for r in rows:
        t.add_row(r["slug"], (r["description"] or "")[:80])
    CON.print(t)


# -------- create / delete --------

@main.group("create", help="create resources")
def create_() -> None: pass


@create_.command("agent")
@click.option("--slug", required=True)
@click.option("--name", default="")
@click.option("--description", default="")
@click.option("--prompt", default="")
@click.option("--model", "model_slug", default=None)
def create_agent(slug, name, description, prompt, model_slug):
    _ensure_running()
    body = {"slug": slug, "name": name or slug, "description": description,
            "system_prompt": prompt, "model_slug": model_slug,
            "tool_specs": [], "skill_slugs": [], "params": {}}
    r = httpx.post(_api("/api/agents"), json=body)
    CON.print(r.json() if r.status_code == 200 else f"[red]{r.text}[/red]")


@create_.command("workflow")
@click.option("--slug", required=True)
@click.option("--name", default="")
@click.option("--kind", required=True,
              type=click.Choice(["sequential", "parallel", "pipeline",
                                 "orchestrator_worker", "group_chat"]))
@click.option("--graph", default="{}", help="JSON string for the graph")
@click.option("--description", default="")
def create_wf(slug, name, kind, graph, description):
    _ensure_running()
    body = {"slug": slug, "name": name or slug, "description": description,
            "kind": kind, "graph": json.loads(graph)}
    r = httpx.post(_api("/api/workflows"), json=body)
    CON.print(r.json() if r.status_code == 200 else f"[red]{r.text}[/red]")


@create_.command("model")
@click.option("--slug", required=True)
@click.option("--provider", required=True,
              type=click.Choice(["echo", "anthropic", "openai", "bedrock", "cli"]))
@click.option("--model-id", required=True)
@click.option("--display-name", default="")
@click.option("--params", default="{}", help="JSON for provider-specific params")
def create_model(slug, provider, model_id, display_name, params):
    _ensure_running()
    body = {"slug": slug, "provider": provider, "model_id": model_id,
            "display_name": display_name or slug,
            "params": json.loads(params), "enabled": True}
    r = httpx.post(_api("/api/models"), json=body)
    CON.print(r.json() if r.status_code == 200 else f"[red]{r.text}[/red]")


@create_.command("eval")
@click.option("--slug", required=True)
@click.option("--target-kind", type=click.Choice(["agent", "workflow"]), required=True)
@click.option("--target-slug", required=True)
@click.option("--dataset", required=True, help="JSON array of {input, expected}")
@click.option("--metric", default="assert_contains")
def create_eval(slug, target_kind, target_slug, dataset, metric):
    _ensure_running()
    body = {"slug": slug, "name": slug, "description": "",
            "target_kind": target_kind, "target_slug": target_slug,
            "dataset": json.loads(dataset), "metric": metric, "metric_args": {}}
    r = httpx.post(_api("/api/evals"), json=body)
    CON.print(r.json() if r.status_code == 200 else f"[red]{r.text}[/red]")


@main.command("delete", help="delete a resource: agent/workflow/model/eval")
@click.argument("kind", type=click.Choice(["agent", "workflow", "model", "eval"]))
@click.argument("slug")
def delete_resource(kind, slug):
    _ensure_running()
    path = {"agent": "agents", "workflow": "workflows", "model": "models", "eval": "evals"}[kind]
    r = httpx.delete(_api(f"/api/{path}/{slug}"))
    CON.print(r.json() if r.status_code in (200, 204) else f"[red]{r.text}[/red]")


@main.command("export", help="export an agent or workflow as JSON to stdout")
@click.argument("kind", type=click.Choice(["agent", "workflow"]))
@click.argument("slug")
def export_resource(kind, slug):
    _ensure_running()
    path = {"agent": "agents", "workflow": "workflows"}[kind]
    r = httpx.get(_api(f"/api/{path}/{slug}/export"))
    if r.status_code == 200:
        CON.print_json(data=r.json())
    else:
        CON.print(f"[red]{r.text}[/red]")


@main.command("import", help="import an agent or workflow JSON file")
@click.argument("kind", type=click.Choice(["agent", "workflow"]))
@click.argument("path", type=click.Path(exists=True))
def import_resource(kind, path):
    _ensure_running()
    api_path = {"agent": "agents", "workflow": "workflows"}[kind]
    with open(path) as f:
        spec = json.load(f)
    r = httpx.post(_api(f"/api/{api_path}/import"), json=spec)
    CON.print(r.json() if r.status_code == 200 else f"[red]{r.text}[/red]")


@main.command("cancel", help="cancel a running run (id may be a prefix)")
@click.argument("run_id")
def cancel(run_id):
    _ensure_running()
    runs = httpx.get(_api("/api/runs?limit=200")).json()
    matches = [r for r in runs if r["id"].startswith(run_id)]
    if not matches:
        CON.print("[red]no match[/red]"); return
    rid = matches[0]["id"]
    r = httpx.post(_api(f"/api/runs/{rid}/cancel"))
    CON.print(r.json() if r.status_code == 200 else f"[red]{r.text}[/red]")


# -------- run --------

@main.command(help="run an agent once and stream output")
@click.argument("slug")
@click.option("-i", "--input", "input_text", default="", help="Prompt / input")
@click.option("--json-output", is_flag=True, help="Print final run JSON")
def run(slug: str, input_text: str, json_output: bool):
    _ensure_running()
    r = httpx.post(_api(f"/api/agents/{slug}/run"), json={"input": {"input": input_text}})
    r.raise_for_status()
    rid = r.json()["run_id"]
    CON.print(f"[dim]run {rid}[/dim]")
    _stream_run(rid)
    detail = httpx.get(_api(f"/api/runs/{rid}")).json()
    if json_output:
        CON.print_json(data=detail)
    else:
        CON.print(Panel.fit(f"[dim]tokens {detail['tokens_in']}/{detail['tokens_out']} • status {detail['status']}[/dim]"))


@main.command("run-wf", help="run a workflow once and stream node events")
@click.argument("slug")
@click.option("-i", "--input", "input_text", default="")
def run_wf(slug: str, input_text: str):
    _ensure_running()
    r = httpx.post(_api(f"/api/workflows/{slug}/run"), json={"input": {"input": input_text}})
    r.raise_for_status()
    rid = r.json()["run_id"]
    CON.print(f"[dim]workflow run {rid}[/dim]")
    _stream_run(rid)
    detail = httpx.get(_api(f"/api/runs/{rid}")).json()
    CON.print(Panel.fit(f"[dim]status {detail['status']}[/dim]"))
    CON.print_json(data=detail.get("output") or {})


def _stream_run(run_id: str) -> None:
    """Connect to SSE and live-render events. Parses SSE line-by-line."""
    url = _api(f"/api/runs/{run_id}/stream")
    current_event: str | None = None
    data_lines: list[str] = []

    def flush():
        nonlocal current_event, data_lines
        if not data_lines:
            current_event = None
            return
        try:
            evt = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            current_event, data_lines = None, []
            return
        kind = evt.get("kind") or current_event
        node = evt.get("node_id") or "-"
        payload = evt.get("payload") or {}
        if kind == "llm_token":
            sys.stdout.write(payload.get("delta", "")); sys.stdout.flush()
        elif kind == "node_start":
            CON.print(f"\n[cyan]▶ {node}[/cyan] [dim]{payload.get('label', '')}[/dim]")
        elif kind == "node_end":
            CON.print(f"\n[green]✓ {node}[/green] [dim]{payload.get('tokens_out', 0)} tok[/dim]")
        elif kind == "error":
            CON.print(f"\n[red]✗ error[/red] {payload}")
        elif kind == "log":
            CON.print(f"[dim]· {payload.get('msg', '')}[/dim]")
        current_event, data_lines = None, []

    try:
        with httpx.Client(timeout=None) as c:
            with c.stream("GET", url, headers={"Accept": "text/event-stream"}) as r:
                for line in r.iter_lines():
                    if line is None:
                        continue
                    if line == "":
                        flush()
                        continue
                    if line.startswith("event:"):
                        current_event = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[len("data:"):].strip())
                flush()
    except Exception as e:
        CON.print(f"[yellow]stream ended: {e}[/yellow]")


# -------- chat --------

@main.command(help="interactive chat with an agent")
@click.argument("slug")
def chat(slug: str):
    _ensure_running()
    CON.print(f"[bold]chat with {slug}[/bold]  ([dim]exit/quit to leave[/dim])")
    while True:
        try:
            msg = click.prompt("you", prompt_suffix="› ")
        except (EOFError, KeyboardInterrupt):
            return
        if msg.strip().lower() in {"exit", "quit"}:
            return
        r = httpx.post(_api(f"/api/playground/chat"),
                       json={"agent_slug": slug, "message": msg, "stream": True})
        if r.status_code != 200:
            CON.print(f"[red]{r.text}[/red]"); continue
        rid = r.json()["run_id"]
        _stream_run(rid)
        CON.print()


# -------- show --------

@main.command(help="show details of a run (id may be a prefix)")
@click.argument("run_id")
def show(run_id: str):
    _ensure_running()
    runs = httpx.get(_api("/api/runs?limit=200")).json()
    matches = [r for r in runs if r["id"].startswith(run_id)]
    if not matches:
        CON.print("[red]no match[/red]"); return
    full = httpx.get(_api(f"/api/runs/{matches[0]['id']}")).json()
    CON.print_json(data=full)
    events = httpx.get(_api(f"/api/runs/{matches[0]['id']}/events")).json()
    t = Table(title=f"Events ({len(events)})")
    t.add_column("kind"); t.add_column("node"); t.add_column("payload")
    for e in events:
        t.add_row(e["kind"], e.get("node_id") or "-", str(e["payload"])[:90])
    CON.print(t)


# -------- eval --------

@main.command(help="run an eval and print the score")
@click.argument("slug")
def eval(slug: str):
    _ensure_running()
    r = httpx.post(_api(f"/api/evals/{slug}/run"), timeout=120.0)
    r.raise_for_status()
    out = r.json()
    CON.print(Panel.fit(f"score [bold]{out['score']:.2%}[/bold] over {len(out['cases'])} cases"))
    t = Table()
    t.add_column("#"); t.add_column("input"); t.add_column("expected"); t.add_column("passed")
    for c in out["cases"]:
        t.add_row(str(c["i"]), c["input"][:30], c["expected"][:30], "✓" if c["passed"] else "✗")
    CON.print(t)


# -------- serve --------

@main.command(help="start backend (and optionally frontend dev server)")
@click.option("--frontend", is_flag=True, help="also start the frontend dev server")
@click.option("--port", default=8765, type=int)
def serve(frontend: bool, port: int):
    os.environ["AGENTS_PORT"] = str(port)
    repo = Path(__file__).resolve().parents[1]
    args = [sys.executable, "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1",
            "--port", str(port)]
    front_proc = None
    if frontend:
        front_proc = subprocess.Popen(["npm", "run", "dev"], cwd=str(repo / "frontend"))
    try:
        subprocess.run(args, cwd=str(repo))
    finally:
        if front_proc:
            front_proc.send_signal(signal.SIGTERM)


# -------- mcp passthrough --------

@main.command(help="start agent-mcp stdio server (forwards to backend)")
def mcp():
    from mcp_server.agent_mcp import main as mcp_main
    asyncio.run(mcp_main())


if __name__ == "__main__":
    main()
