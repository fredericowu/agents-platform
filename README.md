# agents-platform

A multi-agent platform for defining, orchestrating, observing, and evaluating
LangGraph-based agents and workflows. Three first-class interfaces:

- **Web UI** (React + Vite + Tailwind + React Flow) — dashboard, agent editor,
  visual workflow editor, playground, live runs, evals, models, MCP, skills.
- **`agent-cli`** — Click + Rich, streams runs to the terminal.
- **`agent-mcp`** — stdio MCP server that exposes every seeded agent and
  workflow as a tool.

Backed by **FastAPI + SQLite + LangGraph**. Multi-provider model registry
(Anthropic, OpenAI, AWS Bedrock, CLI subshell + offline `echo` fallback).
Reads workspace `.mcp.json` and `.claude/skills` automatically.

## Quick start

```bash
./scripts/setup.sh       # venv, deps, frontend build, seed
./scripts/start.sh       # serves UI + API at http://127.0.0.1:8765
```

Open <http://127.0.0.1:8765/> — dashboard renders with 8 agents, 5 workflows,
10 models, 10 MCP servers, 10 skills already seeded and ready to use.

## CLI

```bash
agent list agents|workflows|models|runs|tools|skills
agent run <agent-slug>     -i "prompt"
agent run-wf <workflow-slug> -i "prompt"
agent chat <agent-slug>
agent show <run-id-prefix>
agent eval <eval-slug>
agent serve
agent mcp                   # stdio MCP server
```

## MCP

Register it with any MCP-compatible client:

```jsonc
"agent-platform": {
  "command": "/abs/path/repos/agents/.venv/bin/python",
  "args": ["-m", "mcp_server.agent_mcp"]
}
```

Exposed tools: `list_agents`, `list_workflows`, `get_run`, `agent_<slug>`,
`workflow_<slug>` for each seeded agent/workflow.

## Tests

```bash
./scripts/test_all.sh
```

Runs: behave BDD (7 features / 14 scenarios), MCP smoke, Playwright UI
(11 tests). All three surfaces (UI, CLI, MCP) are covered.

## Architecture

| Layer | What |
|-------|------|
| `backend/app/main.py` | FastAPI app — serves API + built frontend |
| `backend/app/models.py` | SQLAlchemy models (agents, workflows, runs, events, evals, models, mcp_servers) |
| `backend/app/seed.py`   | Idempotent seed: 8 agents, 5 workflows, 10 models, 3 evals |
| `backend/app/core/executor.py` | Run an agent or a workflow, emit events, persist |
| `backend/app/core/orchestrators/` | 5 orchestration patterns: sequential, parallel, orchestrator_worker, pipeline, group_chat |
| `backend/app/core/models/` | Anthropic, OpenAI, Bedrock, CLI-subshell, Echo |
| `backend/app/core/mcp_client.py` | Discovers `.mcp.json`, lists tools, calls them |
| `backend/app/core/skills.py` | Loads `.claude/skills/<name>/SKILL.md` |
| `backend/app/core/tools/code.py` | read/write/edit/exec/glob/grep |
| `backend/app/core/eval_runner.py` | Dataset-based eval with 4 metrics |
| `backend/app/core/events.py` | In-process event bus → SSE |
| `frontend/src/` | React UI; React Flow editor in `routes/WorkflowEdit.tsx` |
| `cli/agent_cli.py` | Click CLI with SSE streaming via Rich |
| `mcp_server/agent_mcp.py` | stdio MCP server proxying to the backend |
| `tests/features/` | Behave BDD covering API, CLI, MCP, evals, discovery |
| `tests/e2e/ui.spec.ts` | Playwright covering every major UI flow |

## Orchestration patterns (seeded)

| Slug | Kind | Topology |
|------|------|----------|
| `orchestrator-worker` | `orchestrator_worker` | Planner → 3 parallel Coders → Reviewer |
| `spec-pipeline`       | `pipeline`            | Spec → Plan → Tasks → Code → Verify |
| `group-chat-debate`   | `group_chat`          | Planner ↔ Critic ↔ Executor (6 turns) |
| `parallel-explore`    | `parallel`            | N Explorers in parallel over a list |
| `sequential-review`   | `sequential`          | Explore → Plan → Review |

## Agent profiles (seeded)

`coder`, `reviewer`, `explorer`, `planner`, `tester`, `debugger`, `researcher`, `cli-conductor`.

Each is editable in the UI: model, system prompt, tool whitelist, skill set, params.

## Models (seeded)

Anthropic (Sonnet 4.5, Opus 4.1, Haiku 4.5), OpenAI (GPT-4o, GPT-4.1),
Bedrock (Claude Sonnet 4.5, Nova Pro), CLI subshell (`claude`, `codex`), and
an offline `echo` model so the platform works without any API keys.

Set keys in env: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, AWS profile for
Bedrock. Falls back to `echo` if a provider isn't configured.

## Eval framework

Four metrics: `assert_contains`, `judge_llm`, `cmd_returns_zero`, `tool_sequence_match`.
Datasets are JSON in the eval record; scores are persisted per run.
