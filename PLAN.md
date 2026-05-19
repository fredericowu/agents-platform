# Agents Platform — Master Plan

A multi-agent platform for defining, orchestrating, observing, and evaluating
LangGraph-based agents and workflows, with three first-class interfaces:
**Web UI**, **agent-cli**, and **agent-mcp**.

## Goals (acceptance criteria)

1. Web UI (React + Vite + Tailwind + shadcn-style components, dark theme)
   - Dashboard, Agents, Workflows (graph editor), Playground, Runs (live observability),
     Evals, Models, MCP, Skills, Settings
   - Seeded with common agent profiles + 5 orchestration patterns out of the box
   - Visual graph editor (React Flow) for workflows, shows live node activity during execution
2. Python backend (FastAPI + SQLAlchemy + SQLite)
   - REST + SSE/WebSocket APIs
   - LangGraph orchestration core
   - Multi-provider model registry (Anthropic, OpenAI, Bedrock, CLI subshell)
   - MCP client that discovers tools from `../../.mcp.json`
   - Skills loader from `../../.claude/skills`
   - Code tools (read, write, edit, exec, grep, glob)
   - Event bus → SSE for live observability (token usage, tool calls, ins/outs)
   - Eval framework
3. `agent-cli` — packaged python CLI invokable like `agent run <name>` or `agent chat`
4. `agent-mcp` — MCP server that exposes seeded agents + workflows as tools
5. BDD tests (behave) covering backend, CLI, MCP
6. Playwright e2e covering all UI flows + headed CLI/MCP smoke
7. Everything runs from `repos/agents/` with `.venv/` at that path

## Tech stack

| Layer       | Choice                          | Why                                                |
|-------------|----------------------------------|----------------------------------------------------|
| Backend     | FastAPI + uvicorn               | Async, SSE-friendly, typed                         |
| ORM         | SQLAlchemy 2 + SQLite           | Zero ops, easy to inspect, migrations via Alembic  |
| Orchestrator| LangGraph + LangChain           | User-requested, mature, supports parallel + cycles |
| LLM SDKs    | anthropic, openai, boto3        | Provider coverage; BYOK via env vars               |
| MCP         | `mcp` python sdk                | First-party                                        |
| CLI         | Click + Rich                    | Same UX as claude-cli                              |
| Frontend    | React 18 + Vite + TS            | Fast dev loop                                      |
| UI kit      | Tailwind + shadcn-style + Radix | Polished dark theme                                |
| Graph       | React Flow (`@xyflow/react`)    | N8N-like editor + live execution overlay           |
| Tests       | behave + pytest + playwright    | BDD + e2e + units                                  |
| Python      | 3.13 (homebrew)                 | Required for LangGraph 0.2+                        |

## Directory layout

```
repos/agents/
├── PLAN.md                       (this file)
├── README.md
├── pyproject.toml                (backend + cli + mcp; src layout)
├── .venv/                        (created with python3.13)
├── data/
│   └── agents.db                 (sqlite; created at runtime)
├── backend/
│   └── app/
│       ├── main.py               FastAPI app, mounts routers
│       ├── config.py             Settings via pydantic-settings
│       ├── db.py                 Engine + session factory
│       ├── models.py             SQLAlchemy tables
│       ├── schemas.py            Pydantic schemas
│       ├── seed.py               Seed agents + workflows + models
│       ├── api/
│       │   agents.py · workflows.py · runs.py · models.py · mcp.py
│       │   tools.py · skills.py · evals.py · playground.py · health.py
│       └── core/
│           registry.py · executor.py · events.py · mcp_client.py
│           skills.py · eval_runner.py
│           models/{anthropic.py, openai.py, bedrock.py, cli_subshell.py}
│           tools/{code.py, mcp_tool.py, builtin.py}
│           orchestrators/{sequential.py, parallel.py, orchestrator_worker.py,
│                          pipeline.py, group_chat.py}
├── cli/
│   └── agent_cli.py              Click app exposing run/chat/list/eval/serve
├── mcp_server/
│   └── agent_mcp.py              stdio MCP server exposing agents+workflows
├── frontend/
│   ├── package.json · vite.config.ts · tsconfig.json · tailwind.config.js
│   └── src/
│       ├── main.tsx · App.tsx · routes/ · components/ · lib/
├── tests/
│   ├── features/                 behave BDD (.feature files + steps)
│   ├── e2e/                      playwright .spec.ts
│   └── unit/                     pytest
└── scripts/
    └── setup.sh · start.sh · seed.sh · dev.sh
```

## Data model (SQLite)

| Table         | Key columns                                                                   |
|---------------|-------------------------------------------------------------------------------|
| models        | id, slug, provider, model_id, params_json, enabled                            |
| agents        | id, slug, name, description, system_prompt, model_id, tool_specs_json, skill_slugs_json, params_json, builtin |
| workflows     | id, slug, name, description, kind, graph_json, builtin                        |
| runs          | id, kind('agent'|'workflow'), target_id, status, started_at, ended_at, input_json, output_json, tokens_in, tokens_out, cost_usd |
| run_events    | id, run_id, ts, kind('llm_start','llm_end','tool_call','tool_result','node_start','node_end','error'), node_id, payload_json |
| evals         | id, slug, name, target_kind, target_id, dataset_json, metric, builtin         |
| eval_runs     | id, eval_id, run_id, started_at, ended_at, score, report_json                 |
| mcp_servers   | id, name, command, args_json, env_json, enabled, source('file'|'manual'), discovered_tools_json |
| settings      | key, value (kv: api keys, defaults, etc.)                                     |

## Seeded content (visible on first page load)

### 8 Agent profiles
1. **Coder** — Claude Sonnet 4.5, full code tools, system prompt: "implement, terse"
2. **Reviewer** — Opus, read-only code tools, structured critique output
3. **Explorer** — Haiku, grep/glob/read, returns where-is reports
4. **Planner** — Opus, no edit tools, returns numbered plan
5. **Tester** — Sonnet, code tools + exec, runs tests and reports
6. **Debugger** — Sonnet, exec + code read, hypothesis-driven
7. **Researcher** — Sonnet, web_search + web_fetch + MCP, citing sources
8. **CLI-Conductor** — wraps the installed `claude` CLI via subshell

### 5 Workflows
1. **Orchestrator-Worker (fan-out)** — Planner spawns 3 Coders in parallel, Reviewer synthesizes.
2. **Pipeline (spec→plan→tasks→execute→verify)** — Sequential 5-stage codegen pipeline.
3. **Group chat (Planner ↔ Critic ↔ Executor)** — bounded to 6 turns.
4. **Agent Teams (shared task list)** — Lead writes tasks; 3 worker agents claim atomically.
5. **Parallel fan-out (sweep N items)** — Run same agent N times in parallel over a list.

### 3 Evals
- "summary-quality" — judge LLM scores playground output vs. reference
- "code-edit-correctness" — runs pytest in temp repo after edit
- "tool-usage" — checks expected tool call sequence

## API surface

```
GET  /api/health
GET  /api/models                       list providers + models
PUT  /api/models/:slug                 update params
GET  /api/agents                       list
GET  /api/agents/:slug                 detail
POST /api/agents                       create
PUT  /api/agents/:slug                 update
DELETE /api/agents/:slug
POST /api/agents/:slug/run             run with input (returns run_id)
GET  /api/workflows                    list
GET  /api/workflows/:slug              detail (graph_json)
POST /api/workflows                    create
PUT  /api/workflows/:slug              update
DELETE /api/workflows/:slug
POST /api/workflows/:slug/run          run with input
GET  /api/runs                         list (filter by status/target)
GET  /api/runs/:id                     detail + events
GET  /api/runs/:id/stream              SSE event stream
GET  /api/mcp/servers                  list discovered MCP servers
POST /api/mcp/refresh                  re-read .mcp.json
GET  /api/mcp/tools                    list discovered tools
GET  /api/skills                       list available skills
GET  /api/tools                        list all tools (builtin + MCP + skills-as-tools)
GET  /api/evals · POST /api/evals/:slug/run
POST /api/playground/chat              one-shot agent chat
```

## CLI surface (`agent`)

```
agent --help
agent list agents|workflows|models|runs
agent run <agent-slug> --input "..."
agent run-wf <workflow-slug> --input '{"k":"v"}'
agent chat <agent-slug>                 # interactive REPL
agent serve                             # starts backend + UI
agent eval <eval-slug>
agent mcp                               # starts agent-mcp stdio server
agent show run <id>                     # last events, token usage
```

## MCP server (`agent-mcp`)

Exposes each enabled agent and workflow as a tool. Tool names mirror slugs:
- `agent_<slug>`  → runs agent with `{input}`
- `workflow_<slug>` → runs workflow with `{input}`
- Plus introspection tools: `list_agents`, `list_workflows`, `get_run`

## Observability

- Each agent + tool call emits an event into `run_events` and the in-memory event bus.
- UI Runs page subscribes via SSE → shows live waterfall: node start, llm tokens streaming, tool args + result preview, total tokens, $ cost.
- Workflow editor: when a run is selected, the React Flow graph animates — node pulses green when active, red on error.

## Eval framework

- Eval defines: target (agent|workflow), dataset (list of inputs + expected), metric (one of `judge_llm`, `assert_contains`, `cmd_returns_zero`, `tool_sequence_match`).
- Eval run creates regular agent/workflow runs and aggregates a score.
- Reported per-case + overall in UI.

## Multi-provider models

| Provider     | Auth                  | Notes                                              |
|--------------|------------------------|----------------------------------------------------|
| anthropic    | ANTHROPIC_API_KEY      | Sonnet 4.5/4.7, Opus, Haiku                        |
| openai       | OPENAI_API_KEY         | GPT-4o, GPT-4.1                                    |
| bedrock      | AWS_PROFILE/STS        | Claude on Bedrock, Nova                            |
| cli_subshell | none (uses CLI binary) | Invoke `claude`, `codex`, `gemini` CLIs via subshell with `-p` prompt mode |

Models page in UI shows them all in a searchable combobox; each has params (temp, max_tokens, base_url override).

## Test plan

### BDD (`tests/features/`)
- `agents.feature` — create, edit, run an agent
- `workflows.feature` — create, run a workflow, observe parallel branches complete
- `cli.feature` — `agent list`, `agent run`, `agent chat`
- `mcp.feature` — invoke each seeded tool via MCP stdio
- `evals.feature` — run an eval, score is computed
- `mcp_discovery.feature` — `.mcp.json` is parsed, tools are exposed

### Playwright (`tests/e2e/`)
- Dashboard renders with seeded counts
- Agents page lists 8 agents; edit → save → run
- Workflows page renders graph for "orchestrator-worker"; clicking Run shows nodes light up
- Playground: pick agent, send message, see streaming reply with tokens
- Runs: open latest, see events table populated
- MCP page lists discovered servers from .mcp.json; refresh updates
- Evals: pick eval, click run, see score

## Phases (mapped to dt-loco-stubborn cycles)

| Phase | Cycles | What ships                                                  |
|-------|--------|-------------------------------------------------------------|
| 0     | 1–2    | Plan, venv, scaffolding, commit milestone 0                 |
| 1     | 3–8    | Backend: db, models, schemas, registry, basic agents + runs API |
| 2     | 9–12   | MCP discovery, skills loader, code tools, model providers   |
| 3     | 13–18  | LangGraph orchestrators (all 5), event bus, SSE streaming   |
| 4     | 19–22  | Seed data (agents + workflows + models + evals); playground |
| 5     | 23–32  | Frontend: routes, graph editor, live runs, polished theme   |
| 6     | 33–36  | agent-cli                                                   |
| 7     | 37–40  | agent-mcp                                                   |
| 8     | 41–48  | Eval framework + UI                                          |
| 9     | 49–55  | BDD + Playwright tests                                       |
| 10    | 56–60  | E2E demo: start servers, smoke each interface, ship         |

## Stop criteria

Everything passes:
- `agent serve` boots backend + frontend
- UI loads, all 8 agents and 5 workflows visible
- A workflow run streams to the UI with live graph animation
- `agent run coder --input "say hi"` returns text via CLI
- `agent-mcp` registered into a test client and a workflow tool is invoked
- `behave` passes >= 90% scenarios
- `playwright test` passes all spec files
- One eval runs and reports score
