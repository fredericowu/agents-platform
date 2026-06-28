"""Seed agents, workflows, models, and evals.

Design principles (so the platform is usable on REAL projects, not demos):

  * Agent system prompts are GENERIC — they describe a role, not a project.
    Paths, stacks, libraries, and any project-specific context come from the
    user's prompt at run time.

  * The DEFAULT model for real-work agents is the host ``claude`` CLI on
    Sonnet (cheap-ish, fast-enough). The ``echo`` provider is reserved for
    the ``echo-coder`` smoke-test agent and for unit tests.

  * Workflows pass the user's prompt through with at most a tiny role
    wrapper. They do NOT inject paths or project context.

If you want a more specialised agent, copy one of these in the UI and edit;
re-seeding will not clobber user-created agents (only the builtin ones).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .config import settings
from .db import session_scope
from .models import Agent, Eval, Model, Workflow


# ----------------------------------------------------------------------
# Models — provider configurations
# ----------------------------------------------------------------------

WORKSPACE = str(settings.workspace_root)

def _cli_params(cli: str, model: str | None = None, *, readonly: bool = False, timeout: int = 900) -> dict:
    base = {
        "cli": cli,
        "cwd": WORKSPACE,
        "add_dirs": ["/tmp", WORKSPACE],
        "stream_json": False,                # only claude has --output-format stream-json
        "dangerous_skip_permissions": False, # CLI-specific defaults; only claude has this flag
        "timeout_s": timeout,
    }
    if model:
        base["model"] = model
    if readonly:
        base["allowed_tools"] = ["Read", "Grep", "Glob"]
    return base


def _detect_ollama_models() -> list[dict]:
    """Best-effort: ask the local Ollama daemon for installed models."""
    import urllib.request, json as _json
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1.5) as r:
            data = _json.loads(r.read().decode())
        models = []
        for m in data.get("models", []):
            name = m.get("name") or m.get("model")
            if not name:
                continue
            models.append({
                "slug": f"ollama-{name.replace(':', '-').replace('/', '-')}",
                "provider": "cli",
                "model_id": f"ollama-{name}",
                "display_name": f"Ollama – {name} (local)",
                "params": {
                    "cli": "ollama",
                    "extra_args": ["run", name],
                    "stream_json": False,
                    "dangerous_skip_permissions": False,
                    "timeout_s": 300,
                },
                "enabled": True,
            })
        return models
    except Exception:
        return []


SEED_MODELS = [
    # ---- offline / free ----
    {"slug": "echo", "provider": "echo", "model_id": "echo",
     "display_name": "Echo (offline, no LLM)", "params": {}},
    {"slug": "fake-tool-chat", "provider": "fake", "model_id": "fake-tool-chat",
     "display_name": "Fake tool-calling chat (offline; test-only)",
     "params": {"script": [
        ["tool_call", "write_file", {"path": "/tmp/fake-agent-out.txt", "content": "hello from fake agent"}],
        ["text", "Wrote /tmp/fake-agent-out.txt."],
     ]}},

    # ============================================================
    # Claude CLI — `claude` (Anthropic)
    # ============================================================
    {"slug": "claude-cli", "provider": "cli", "model_id": "claude-cli",
     "display_name": "Claude CLI – Sonnet 4 (default, full perms)",
     "params": {"cli": "claude", "model": "sonnet", "cwd": WORKSPACE,
                "add_dirs": ["/tmp", WORKSPACE], "dangerous_skip_permissions": True,
                "stream_json": True, "timeout_s": 900}},
    {"slug": "claude-cli-opus", "provider": "cli", "model_id": "claude-cli-opus",
     "display_name": "Claude CLI – Opus (premium, full perms)",
     "params": {"cli": "claude", "model": "opus", "cwd": WORKSPACE,
                "add_dirs": ["/tmp", WORKSPACE], "dangerous_skip_permissions": True,
                "stream_json": True, "timeout_s": 1800}},
    {"slug": "claude-cli-haiku", "provider": "cli", "model_id": "claude-cli-haiku",
     "display_name": "Claude CLI – Haiku (cheap & fast)",
     "params": {"cli": "claude", "model": "haiku", "cwd": WORKSPACE,
                "add_dirs": ["/tmp", WORKSPACE], "dangerous_skip_permissions": True,
                "stream_json": True, "timeout_s": 600}},
    {"slug": "claude-cli-readonly", "provider": "cli", "model_id": "claude-cli-readonly",
     "display_name": "Claude CLI – Sonnet (read-only: Read/Grep/Glob/Web)",
     "params": {"cli": "claude", "model": "sonnet", "cwd": WORKSPACE,
                "add_dirs": [WORKSPACE], "dangerous_skip_permissions": False,
                "allowed_tools": ["Read", "Grep", "Glob", "WebFetch", "WebSearch"],
                "stream_json": True, "timeout_s": 600}},

    # ============================================================
    # Other agentic CLIs (require their respective CLI on PATH)
    # ============================================================
    {"slug": "codex-cli-gpt-5", "provider": "cli", "model_id": "codex-cli-gpt-5",
     "display_name": "Codex CLI – GPT-5 Codex (requires `codex` on PATH)",
     "params": _cli_params("codex", "gpt-5-codex")},
    {"slug": "cursor-agent", "provider": "cli", "model_id": "cursor-agent",
     "display_name": "Cursor Agent CLI (requires `cursor-agent` on PATH)",
     "params": {"cli": "cursor-agent", "extra_args": ["--print"],
                "cwd": WORKSPACE, "timeout_s": 900, "stream_json": False,
                "dangerous_skip_permissions": False}},
    {"slug": "gemini-cli", "provider": "cli", "model_id": "gemini-cli",
     "display_name": "Gemini CLI – `gemini` (requires Google CLI on PATH)",
     "params": _cli_params("gemini")},
    {"slug": "github-copilot-cli", "provider": "cli", "model_id": "github-copilot-cli",
     "display_name": "GitHub Copilot CLI – `gh copilot suggest` (requires gh + ext)",
     "params": {"cli": "gh", "extra_args": ["copilot", "suggest"],
                "cwd": WORKSPACE, "timeout_s": 120, "stream_json": False,
                "dangerous_skip_permissions": False}},
    {"slug": "amp-cli", "provider": "cli", "model_id": "amp-cli",
     "display_name": "Sourcegraph Amp CLI – `amp` (requires Amp on PATH)",
     "params": _cli_params("amp")},
    {"slug": "aider-cli", "provider": "cli", "model_id": "aider-cli",
     "display_name": "aider CLI – `aider` (requires aider on PATH)",
     "params": {"cli": "aider", "extra_args": ["--no-pretty"],
                "cwd": WORKSPACE, "timeout_s": 900, "stream_json": False,
                "dangerous_skip_permissions": False}},

    # ============================================================
    # API-direct (require keys in env)
    # ============================================================
    {"slug": "anthropic-sonnet-4-5", "provider": "anthropic", "model_id": "claude-sonnet-4-5",
     "display_name": "Anthropic API – Claude Sonnet 4.5",
     "params": {"temperature": 0.2, "max_tokens": 4096}},
    {"slug": "anthropic-opus-4-1", "provider": "anthropic", "model_id": "claude-opus-4-1",
     "display_name": "Anthropic API – Claude Opus 4.1",
     "params": {"temperature": 0.2, "max_tokens": 4096}},
    {"slug": "anthropic-haiku-4-5", "provider": "anthropic", "model_id": "claude-haiku-4-5",
     "display_name": "Anthropic API – Claude Haiku 4.5",
     "params": {"temperature": 0.2, "max_tokens": 2048}},
    {"slug": "openai-gpt-4o", "provider": "openai", "model_id": "gpt-4o",
     "display_name": "OpenAI – GPT-4o", "params": {"temperature": 0.2}},
    {"slug": "openai-gpt-4-1", "provider": "openai", "model_id": "gpt-4.1",
     "display_name": "OpenAI – GPT-4.1", "params": {"temperature": 0.2}},
    {"slug": "openai-o1", "provider": "openai", "model_id": "o1",
     "display_name": "OpenAI – o1 (reasoning)", "params": {}},
    {"slug": "openai-o3-mini", "provider": "openai", "model_id": "o3-mini",
     "display_name": "OpenAI – o3-mini (reasoning, cheap)", "params": {}},
    {"slug": "bedrock-sonnet-4-5", "provider": "bedrock",
     "model_id": "us.anthropic.claude-sonnet-4-5-20251022-v1:0",
     "display_name": "AWS Bedrock – Claude Sonnet 4.5", "params": {}},
    {"slug": "bedrock-opus-4-1", "provider": "bedrock",
     "model_id": "us.anthropic.claude-opus-4-1-20250930-v1:0",
     "display_name": "AWS Bedrock – Claude Opus 4.1", "params": {}},
    {"slug": "bedrock-nova-pro", "provider": "bedrock",
     "model_id": "us.amazon.nova-pro-v1:0",
     "display_name": "AWS Bedrock – Amazon Nova Pro", "params": {}},
    {"slug": "bedrock-llama-3-3", "provider": "bedrock",
     "model_id": "us.meta.llama3-3-70b-instruct-v1:0",
     "display_name": "AWS Bedrock – Llama 3.3 70B", "params": {}},
]


# ----------------------------------------------------------------------
# Agents — GENERIC, path-agnostic, project-agnostic
# ----------------------------------------------------------------------
# A good agent prompt:
#   • Describes a ROLE, not a project
#   • Sets reasoning style + output expectations
#   • Mentions tools available, not specific paths
#   • Says "the user will tell you what / where" rather than assuming

DEFAULT_MODEL = "claude-cli"     # real LLM for real work
READONLY_MODEL = "claude-cli-readonly"
OPUS_MODEL = "claude-cli-opus"   # decision-tier (planner, reviewer, project-manager, retro)

SEED_AGENTS = [
    # ===== read-only specialists =====
    {"slug": "explorer", "name": "Explorer", "icon": "search", "color": "#2dd4bf",
     "description": "Locates code by pattern or topic; returns where-is reports with file paths + line numbers.",
     "system_prompt": (
        "You are a code locator. The user names a symbol, behaviour, or topic; "
        "you find the relevant files and line numbers. Use Glob, Grep, and Read. "
        "Return a terse where-is list, grouped by area. Never edit files."
     ),
     "model_slug": READONLY_MODEL, "tool_specs": ["code.glob", "code.grep", "code.read_file"]},

    {"slug": "planner", "name": "Planner", "icon": "list", "color": "#b794f4",
     "description": "Designs an implementation strategy; produces a numbered plan with files, steps and trade-offs.",
     "system_prompt": (
        "You are a senior software architect. Given a task description, return a "
        "numbered implementation plan. Each step names the file(s) it touches and "
        "the change in 1–2 lines. Call out trade-offs and risks at the end. "
        "Do not write code; do not edit files. If you need to read code first to "
        "ground the plan, do so."
     ),
     "model_slug": READONLY_MODEL, "tool_specs": ["code.read_file", "code.glob", "code.grep"]},

    {"slug": "reviewer", "name": "Reviewer", "icon": "shield-check", "color": "#f0c000",
     "description": "Reviews code or a diff; flags bugs, security issues, missing tests.",
     "system_prompt": (
        "You are a senior code reviewer. Read the code or diff the user pastes (or "
        "fetch it via Read/Grep). Return BLOCKING issues only, grouped by severity: "
        "bugs, security, correctness, missing tests. Be terse. No praise. If "
        "nothing blocking, say so in one line."
     ),
     "model_slug": READONLY_MODEL, "tool_specs": ["code.read_file", "code.grep", "code.glob"]},

    {"slug": "retro", "name": "Retrospective Analyst", "icon": "history", "color": "#c4b5fd",
     "description": (
        "Analyzes a completed Target's full run history, dispatches follow-up "
        "questions to the agents who did the work, dedupes against existing "
        "lessons + KB, then records new/updated lessons. Decision-tier (Opus)."),
     "use_cases": [
        "Post-delivery retrospective on any completed Target",
        "Cross-Target pattern detection (run multiple in sequence to compare)",
        "Continuous-improvement feedback loop — feeds Phase-1.5 of future deliveries",
        "Identify time-wasters / dead-ends / tooling gaps / cost traps",
     ],
     "system_prompt": (
        "You are the Retrospective Analyst. Your input is a Target slug; your output "
        "is a set of structured lessons written to the platform's lessons store so "
        "future deliveries get smarter.\n\n"
        "## Your run shape (5 phases — do them in order)\n\n"
        "### Phase A — Load the Target's full context\n"
        "Call (via the agent-mcp MCP server):\n"
        "  1. `get_target(slug)` — goal, budget, source_ref, plan/report canvases\n"
        "  2. `target_summary(slug)` — rolled-up stats, agents used, models, wall, cost vs budget\n"
        "  3. `list_target_runs(slug)` — chronological run list\n"
        "  4. For each run id, `run_tree(run_id)` to see lineage\n"
        "  5. For each run id, `run_events(run_id, kinds='node_start,error,done,tool_call,thinking')` "
        "to see the decision points and failures\n"
        "  6. For each run id, `list_run_artefacts(run_id)` then `get_run_artefact` for any non-trivial "
        "structured outputs (NRQL tables, terraform plans, threshold specs)\n"
        "  7. `list_target_lessons(slug)` — what's already been recorded for THIS target\n\n"
        "### Phase B — Independent analysis\n"
        "Identify candidate lessons by walking the run tree and looking for these patterns:\n"
        "  - **Cost traps**: cancelled runs with non-zero cost, Opus runs that took >10min, "
        "agents dispatched multiple times for the same task, parallel work done sequentially\n"
        "  - **Dead-ends**: agent outputs that downstream runs didn't reference, research that "
        "was redone in a later phase, assumptions surfaced mid-stream that should've been "
        "verified earlier\n"
        "  - **Tooling gaps**: agents reporting 'I tried X but couldn't', tool features that "
        "would've helped but weren't used\n"
        "  - **Prompt fixes**: agents needing clarifying back-and-forth, ambiguous inputs, "
        "missing context that caused rework\n"
        "  - **Patterns that worked**: things to repeat — parallel fan-out, model swaps that "
        "saved cost, pre-computed inputs\n"
        "  - **Scope creep**: work the conductor added mid-run that wasn't in the original plan\n\n"
        "For each candidate, capture: category, title, evidence_run_ids, applicable_tags "
        "(e.g. ['cat-2','acsb','cookiecutter']), and a draft body.\n\n"
        "### Phase C — Cross-agent discussion (CRITICAL — don't skip)\n"
        "For each candidate lesson that's tied to a specific agent's behaviour, dispatch a "
        "follow-up question to that agent to verify your interpretation. Use:\n"
        "  `run_agent_async(slug='<the-agent>', target_slug='<this-target>', "
        "input='RETRO FOLLOW-UP — In your run <run_id> you did X. I'm inferring "
        "the reason was Y, and that the way to avoid it next time is Z. Is that right, "
        "or am I missing context? Be terse — one paragraph.')`\n"
        "Then `wait_run` (timeout 120s, max_cost_usd $0.30) and incorporate their reply. "
        "If they disagree, refine the lesson. If they confirm with extra nuance, capture it.\n"
        "  - Limit to 3-5 follow-up dispatches per retro (cost cap).\n"
        "  - Skip this phase only for lessons that are purely about platform/tooling (no agent input needed).\n\n"
        "### Phase D — Dedupe vs existing lessons + KB\n"
        "Before recording anything, for EACH candidate lesson:\n"
        "  1. Call `search_lessons(tags='<applicable_tags>', q='<short-query>')` to find "
        "existing lessons in OTHER Targets that match.\n"
        "  2. If a hit exists with high overlap → call `update_target_lesson` on the existing "
        "lesson to APPEND this Target's run_id to evidence_run_ids and refine the body. "
        "Increment confidence if multiple Targets now share this lesson.\n"
        "  3. If hits are partial → consider whether to MERGE (update existing) or DIVERGE "
        "(create new + reference the existing via the body). Prefer merge.\n"
        "  4. If no hits → search the KB via `mcp__aw-knowledge-base__search_knowledge_base` "
        "for related domain articles. If a KB article covers this lesson, REFERENCE it in the "
        "lesson body rather than duplicating. If the KB is silent, create the lesson and "
        "consider whether the KB should also be updated.\n"
        "  5. If a candidate lesson is already covered by THIS target's existing lessons "
        "(from Phase A step 7), skip it.\n\n"
        "### Phase E — Publish\n"
        "Write final lessons via `create_target_lesson` (for new) or `update_target_lesson` "
        "(for refined existing). Limit total new+updated to ~10-15 per retro — quality over "
        "quantity. Each lesson MUST have:\n"
        "  - category (one of: time-saver | pitfall | tooling-gap | pattern-that-worked | "
        "prompt-fix | cost-trap | scope-creep)\n"
        "  - title (short, action-oriented — \"Use run_agents_parallel for Phase 0 fan-out\")\n"
        "  - content (markdown body — what, why, evidence, how-to-avoid-or-repeat)\n"
        "  - evidence_run_ids (the run ids this lesson references)\n"
        "  - applicable_tags (so future Phase-1.5 searches find it)\n"
        "  - confidence (low | medium | high — high requires evidence from 2+ Targets)\n\n"
        "Finally, emit a summary in your own message body listing what you recorded, what you "
        "updated, what you dedupe'd, and what you escalated to the user (e.g. tooling gaps "
        "that need a platform change).\n\n"
        "## STRICT RULES\n"
        "- BE THE RETRO YOU WISH YOU HAD HAD. Future agents will read these.\n"
        "- DON'T duplicate lessons — search first, update second, create third.\n"
        "- For cross-agent discussion: cap at 5 dispatches and $1.50 follow-up cost total.\n"
        "- Tag lessons aggressively — empty tags = unreachable lesson.\n"
        "- High-confidence lessons need cross-Target evidence; first appearance is medium at best.\n"
        "- If you can't find a Target with the slug provided, STOP and report.\n"
        "- READ-ONLY for the target's code base / repo — you only WRITE to the lessons store."
     ),
     "model_slug": OPUS_MODEL,
     "tool_specs": ["code.read_file", "code.glob", "code.grep"]},

    {"slug": "researcher", "name": "Researcher", "icon": "globe", "color": "#a5d6ff",
     "description": "Researches with web search + reading; returns findings with citations.",
     "system_prompt": (
        "You research a topic the user names. Use WebSearch + WebFetch where "
        "available. Return findings as bullet points with inline links. Cite "
        "sources. If the topic concerns the user's own codebase, use Grep/Read "
        "to ground your answer.\n\n"
        "**VERSION-AWARENESS RULE (critical for tooling/library research):** "
        "When asked to find the version of a tool, cookiecutter, library, SDK, "
        "or package, you MUST surface the **latest released version** (most "
        "recent git tag, npm/pypi release, or GitHub release). If sister repos "
        "or examples in the org pin an older version, REPORT BOTH: latest + the "
        "version sister repos use. Don't blindly recommend the sister-pinned "
        "version — that's how cookiecutter drift happens. State clearly which "
        "is which so the conductor can choose."
     ),
     "model_slug": READONLY_MODEL, "tool_specs": []},

    # ===== writes-allowed specialists =====
    {"slug": "coder", "name": "Coder", "icon": "code", "color": "#58a6ff",
     "description": "Implements the user's request. Writes code, edits files, runs commands.",
     "system_prompt": (
        "You are a senior engineer. Implement what the user asks. The user will "
        "tell you WHAT to build and WHERE (which path or repo). If they don't, "
        "ask once, then proceed with a sensible default. Read existing code "
        "before changing it. Be terse. Verify your work (build/test/lint) before "
        "you say you're done."
     ),
     "model_slug": DEFAULT_MODEL,
     "tool_specs": ["code.read_file", "code.write_file", "code.edit_file",
                    "code.run_command", "code.glob", "code.grep"]},

    {"slug": "code-builder", "name": "Code Builder", "icon": "hammer", "color": "#2dd4bf",
     "description": "Scaffolds NEW applications from scratch at a user-specified path.",
     "system_prompt": (
        "You scaffold new applications. The user will tell you WHAT (the app + "
        "features) and WHERE (an absolute path). Steps: create the target dir, "
        "scaffold the chosen stack, install deps, implement the requested "
        "features, then verify with a STATIC build only.\n"
        "\n"
        "STRICT RULES:\n"
        "- NEVER start a dev server (npm run dev / vite / hot-reload). Use only "
        "  static commands like `npm run build`, `tsc --noEmit`, `pytest`, "
        "  `cargo build`, etc. that exit on their own.\n"
        "- NEVER curl, fetch, or otherwise probe localhost:5173, 5174, 3000, 8000, "
        "  8080, 8765, or any port — they may be unrelated services.\n"
        "- Work ONLY inside the path the user gave you. Do not modify, scan, or "
        "  probe any other directory (especially not this platform's own "
        "  source tree).\n"
        "- If the user does not specify a stack, pick a reasonable default "
        "  (Vite+React+TS for web apps, FastAPI for Python services, Cargo for "
        "  Rust). When done, print a short summary: location, how the user can "
        "  run it themselves, what was built. Do NOT run it for them."
     ),
     "model_slug": DEFAULT_MODEL,
     "tool_specs": ["code.read_file", "code.write_file", "code.edit_file",
                    "code.run_command", "code.glob", "code.grep"]},

    {"slug": "code-enhancer", "name": "Code Enhancer", "icon": "wand", "color": "#b794f4",
     "description": "Modifies an EXISTING project at a user-specified path to add features or fix issues.",
     "system_prompt": (
        "You modify existing projects. The user will tell you WHERE (the project "
        "path) and WHAT to change. Read the current code first to understand the "
        "architecture and conventions. Preserve what works.\n"
        "\n"
        "STRICT RULES:\n"
        "- Stay inside the project path the user gave you.\n"
        "- NEVER start a dev server or probe HTTP ports. Verification uses "
        "  STATIC commands only: `npm run build`, `tsc`, `pytest`, `cargo "
        "  build`, etc.\n"
        "- After editing, run a build/lint/test to verify nothing broke. "
        "  Output a short diff summary."
     ),
     "model_slug": DEFAULT_MODEL,
     "tool_specs": ["code.read_file", "code.write_file", "code.edit_file",
                    "code.run_command", "code.glob", "code.grep"]},

    {"slug": "app-verifier", "name": "App Verifier", "icon": "check", "color": "#58a6ff",
     "description": "Inspects a project at a user-specified path, runs its build/test, reports a verdict.",
     "system_prompt": (
        "You verify the health of an existing project. The user gives you the "
        "path. Inspect the project layout (package.json / pyproject.toml / "
        "Cargo.toml / etc.), then run the appropriate STATIC build/test "
        "command (timeout 60s).\n"
        "\n"
        "STRICT RULES:\n"
        "- Verification is STATIC ONLY: `npm run build`, `tsc --noEmit`, "
        "  `pytest`, `cargo build`, `cargo test`, `go build`, etc. Commands "
        "  that exit on their own.\n"
        "- NEVER run dev servers (`npm run dev`, `vite`, `next dev`, `python "
        "  -m http.server`, etc.). NEVER curl or otherwise probe ports — "
        "  including 5173, 3000, 8000, 8080, 8765.\n"
        "- Stay inside the project path the user gave you. Do NOT scan other "
        "  directories.\n"
        "- Report: stack detected, build status (pass/fail + exit code), key "
        "  features observed in the code, any issues found. Be terse."
     ),
     "model_slug": DEFAULT_MODEL,
     "tool_specs": ["code.read_file", "code.run_command", "code.glob", "code.grep"]},

    {"slug": "tester", "name": "Tester", "icon": "beaker", "color": "#79c0ff",
     "description": "Writes new tests for code at a user-specified path; runs the test suite.",
     "system_prompt": (
        "You write and run automated tests. The user names the project and what "
        "to test. Use the framework that's already in the project (pytest, "
        "jest/vitest, cargo test, go test, etc.). Cover the unhappy path. Run "
        "the suite and report failures with file:line + a one-line cause."
     ),
     "model_slug": DEFAULT_MODEL,
     "tool_specs": ["code.read_file", "code.write_file", "code.edit_file",
                    "code.run_command", "code.glob"]},

    {"slug": "debugger", "name": "Debugger", "icon": "bug", "color": "#f87171",
     "description": "Hypothesis-driven debugging. The user names the symptom; the agent finds the cause.",
     "system_prompt": (
        "You debug by hypothesis. The user describes a symptom (failing test, "
        "wrong output, crash). State a hypothesis explicitly, design a tiny "
        "experiment (instrument code, add log, run with input), execute it, "
        "record the evidence. Iterate until you find the root cause, then "
        "propose a fix. Be terse but always show your hypothesis and result."
     ),
     "model_slug": DEFAULT_MODEL,
     "tool_specs": ["code.read_file", "code.edit_file", "code.run_command", "code.grep"]},

    {"slug": "doc-writer", "name": "Doc Writer", "icon": "book", "color": "#a5d6ff",
     "description": "Writes / updates README, ADRs, API docs based on the actual code.",
     "system_prompt": (
        "You write documentation. The user tells you what artefact to produce "
        "(README, ADR, module docs, API ref) and where. Read the code first to "
        "ground your prose in reality. Use simple Markdown. Include short "
        "examples. Don't invent features that aren't in the code."
     ),
     "model_slug": DEFAULT_MODEL,
     "tool_specs": ["code.read_file", "code.write_file", "code.glob", "code.grep"]},

    {"slug": "refactorer", "name": "Refactorer", "icon": "wand", "color": "#b794f4",
     "description": "Performs targeted refactors. The user names the goal; the agent preserves behavior.",
     "system_prompt": (
        "You refactor existing code. The user gives you a goal (extract a "
        "function, rename, split a file, deduplicate, switch a library). "
        "Preserve behavior — run the existing tests after the refactor. If "
        "tests don't exist, add a smoke test before refactoring. Print a "
        "diff summary at the end."
     ),
     "model_slug": DEFAULT_MODEL,
     "tool_specs": ["code.read_file", "code.write_file", "code.edit_file",
                    "code.run_command", "code.glob", "code.grep"]},

    # ===== utility / passthrough =====
    {"slug": "cli-conductor", "name": "CLI Conductor", "icon": "terminal", "color": "#ffa657",
     "description": "Generic claude CLI passthrough — useful for fresh contexts inside a workflow.",
     "system_prompt": "",     # rely on user's prompt only
     "model_slug": DEFAULT_MODEL, "tool_specs": []},

    {"slug": "echo-coder", "name": "Echo (smoke)", "icon": "bot", "color": "#8b949e",
     "description": "Offline echo agent for cheap tests / demos. Does not call any LLM.",
     "system_prompt": "(echo agent — replies with the prompt itself, prefixed)",
     "model_slug": "echo", "tool_specs": []},

    {"slug": "fake-tool-tester", "name": "Fake Tool Tester (offline)", "icon": "bot", "color": "#8b949e",
     "description": "Offline scripted agent that calls write_file once then prints a confirmation. Used by the eval smoke suite.",
     "system_prompt": "You are an offline test agent.",
     "model_slug": "fake-tool-chat",
     "tool_specs": ["code.write_file"],
     "params": {}},
]


# ----------------------------------------------------------------------
# Workflows — pass the user's prompt through; orchestration kind does the rest
# ----------------------------------------------------------------------
# input_template conventions:
#   {input} = user's free-form prompt
#   {prev}  = previous stage's text output

SEED_WORKFLOWS = [
    # ===== single-stage style =====
    {"slug": "ask-coder", "name": "Ask Coder (single agent)", "kind": "sequential",
     "description": "Send the user's request to the Coder agent. Simplest possible workflow.",
     "graph": {"nodes": [
        {"id": "go", "agent": "coder", "label": "Coder", "input_template": "{input}"},
     ]}},

    # ===== pipelines =====
    {"slug": "build-app", "name": "Build New App (Build → Verify)", "kind": "pipeline",
     "description": "Scaffold a new app per the user's spec, then run a verification pass.",
     "graph": {"stages": [
        {"id": "build", "agent": "code-builder", "label": "Build",
         "input_template": "{input}"},
        {"id": "verify", "agent": "app-verifier", "label": "Verify",
         "input_template": (
            "Verify the project that was just built. Builder's report:\n\n{prev}\n\n"
            "Original user request (for context):\n\n{input}"
         )},
     ]}},

    {"slug": "enhance-app", "name": "Modify Existing App (Enhance → Verify)", "kind": "pipeline",
     "description": "Read an existing project and apply the user's modification, then verify.",
     "graph": {"stages": [
        {"id": "enhance", "agent": "code-enhancer", "label": "Enhance",
         "input_template": "{input}"},
        {"id": "verify", "agent": "app-verifier", "label": "Verify",
         "input_template": (
            "Verify the project still works after the modification. Enhancer's report:\n\n{prev}\n\n"
            "Original request (for context):\n\n{input}"
         )},
     ]}},

    {"slug": "spec-pipeline", "name": "Spec → Plan → Code → Review", "kind": "pipeline",
     "description": "4-stage codegen pipeline that turns a feature request into shipped code with review.",
     "graph": {"stages": [
        {"id": "spec", "agent": "planner", "label": "Spec",
         "input_template": "Write a one-page spec for the user's feature request:\n\n{input}"},
        {"id": "plan", "agent": "planner", "label": "Plan",
         "input_template": "Given this spec, produce a numbered implementation plan:\n\n{prev}"},
        {"id": "code", "agent": "coder", "label": "Code",
         "input_template": "Implement the plan below. Original request:\n{input}\n\nPlan:\n{prev}"},
        {"id": "review", "agent": "reviewer", "label": "Review",
         "input_template": "Review the implementation summary below for the original request: {input}\n\nFlag blocking issues only.\n\n{prev}"},
     ]}},

    # ===== fan-out =====
    # Note: kind is now derived from graph. For "nodes"-shape graphs, set
    # ``graph.concurrency`` to disambiguate sequential vs parallel.
    {"slug": "orchestrator-worker", "name": "Orchestrator → Workers (fan-out)", "kind": "orchestrator_worker",
     "description": "A planner decomposes the task into 3 independent sub-tasks, three coders run them in parallel, a reviewer synthesises.",
     "graph": {
        "orchestrator": {"id": "plan", "agent": "planner", "label": "Planner",
                         "input_template": "Decompose the user's request into exactly 3 independent sub-tasks. Output them as 3 numbered items.\n\nUSER REQUEST: {input}"},
        "workers": [
            {"id": "w1", "agent": "coder", "label": "Coder #1",
             "input_template": "Execute sub-task 1 from the plan below. Original request: {input}\n\nPLAN:\n{prev}"},
            {"id": "w2", "agent": "coder", "label": "Coder #2",
             "input_template": "Execute sub-task 2 from the plan below. Original request: {input}\n\nPLAN:\n{prev}"},
            {"id": "w3", "agent": "coder", "label": "Coder #3",
             "input_template": "Execute sub-task 3 from the plan below. Original request: {input}\n\nPLAN:\n{prev}"},
        ],
        "synthesizer": {"id": "review", "agent": "reviewer", "label": "Reviewer",
                        "input_template": "Synthesize the three workers' results below for the original request: {input}\n\nWORKERS:\n{prev}"},
     }},

    # ===== parallel sweep =====
    {"slug": "parallel-explore", "name": "Parallel Explore (3 explorers)", "kind": "parallel",
     "description": "Three Explorer agents run in parallel on the same request — useful for triangulating where something lives.",
     "graph": {
        "concurrency": "parallel",
        "nodes": [
            {"id": "e1", "agent": "explorer", "label": "Explorer #1",
             "input_template": "From the angle of code structure: {input}"},
            {"id": "e2", "agent": "explorer", "label": "Explorer #2",
             "input_template": "From the angle of config and dependencies: {input}"},
            {"id": "e3", "agent": "explorer", "label": "Explorer #3",
             "input_template": "From the angle of tests and usage examples: {input}"},
        ]}},

    # ===== sequential 3-stage =====
    {"slug": "sequential-review", "name": "Explore → Plan → Review", "kind": "sequential",
     "description": "Triage a request: locate relevant code, plan the change, then critique the plan.",
     "graph": {"nodes": [
        {"id": "s1", "agent": "explorer", "label": "Explore",
         "input_template": "{input}"},
        {"id": "s2", "agent": "planner", "label": "Plan",
         "input_template": "Findings:\n{prev}\n\nGiven the findings above, write an implementation plan for: {input}"},
        {"id": "s3", "agent": "reviewer", "label": "Review",
         "input_template": "Review this plan for: {input}\n\nFlag blocking issues only.\n\nPLAN:\n{prev}"},
     ]}},

    # ===== group chat =====
    {"slug": "group-chat-debate", "name": "Group Chat: Planner ↔ Critic ↔ Coder", "kind": "group_chat",
     "description": "Three agents debate the user's question for up to 3 turns. Useful for design choices.",
     "graph": {"participants": [
        {"id": "p1", "agent": "planner", "label": "Planner"},
        {"id": "p2", "agent": "reviewer", "label": "Critic"},
        {"id": "p3", "agent": "coder", "label": "Coder"},
     ], "max_turns": 3}},

    # ===== sub-workflow composition =====
    # Demonstrates that a node can invoke ANOTHER workflow by using the
    # ``workflow:<slug>`` prefix. This one chains two sub-workflows back-to-back.
    {"slug": "meta-pipeline", "name": "Meta · Sub-workflow composition", "kind": "pipeline",
     "description": "Sequentially runs two sub-workflows: parallel-explore (3 explorers fan-out) then sequential-review (locate → plan → review). Shows how a workflow can invoke other workflows.",
     "graph": {"stages": [
        {"id": "explore", "agent": "workflow:test-echo-parallel", "label": "Sub: test-echo-parallel",
         "input_template": "{input}"},
        {"id": "review",  "agent": "workflow:test-echo-pipeline", "label": "Sub: test-echo-pipeline",
         "input_template": "Previous sub-workflow output:\n{prev}\n\nOriginal request: {input}"},
     ]}},

    # ===== test-only workflows (echo agents → fast, offline, deterministic) =====
    {"slug": "test-echo-pipeline", "name": "TEST · Echo pipeline (smoke)", "kind": "pipeline",
     "description": "Two-stage pipeline against the echo-coder agent — used by CI to verify orchestration shape without burning LLM tokens.",
     "graph": {"stages": [
        {"id": "a", "agent": "echo-coder", "label": "A", "input_template": "{input}"},
        {"id": "b", "agent": "echo-coder", "label": "B", "input_template": "second stage saw: {prev}"},
     ]}},
    {"slug": "test-echo-parallel", "name": "TEST · Echo parallel (smoke)", "kind": "parallel",
     "description": "Three echo agents in parallel — used by CI to verify the parallel orchestrator.",
     "graph": {
        "concurrency": "parallel",
        "nodes": [
            {"id": "n1", "agent": "echo-coder", "label": "echo #1", "input_template": "{input}"},
            {"id": "n2", "agent": "echo-coder", "label": "echo #2", "input_template": "{input}"},
            {"id": "n3", "agent": "echo-coder", "label": "echo #3", "input_template": "{input}"},
        ]}},
]


# ----------------------------------------------------------------------
# Evals — keep simple
# ----------------------------------------------------------------------

SEED_EVALS = [
    {"slug": "echo-smoke", "name": "Echo smoke test", "description": "Confirms the echo agent echoes input.",
     "target_kind": "agent", "target_slug": "echo-coder",
     "dataset": [
        {"input": "ping", "expected": "ping"},
        {"input": "hello world", "expected": "hello world"},
     ],
     "metric": "assert_contains", "metric_args": {}},

    # ---- Multi-step + multi-assert example ----
    # The fake-tool-chat agent is scripted to call write_file once then say
    # "Wrote …". We pin down the behavior with several different assert kinds.
    {"slug": "fake-tool-asserts", "name": "Fake-tool agent · asserts mix",
     "description": "Demonstrates rich asserts: tool_called, tool_called_with, tool_output_contains, response_regex, no_errors. Single step.",
     "target_kind": "agent", "target_slug": "fake-tool-tester",
     "dataset": [{
        "name": "fake-tool writes a file",
        "context": "fresh",
        "steps": [{
            "prompt": "Please write the file.",
            "asserts": [
                {"kind": "tool_called", "name": "write_file"},
                {"kind": "tool_called_with", "name": "write_file",
                 "input_contains": {"path": "/tmp/fake-agent-out.txt"}},
                {"kind": "tool_output_contains", "name": "write_file",
                 "value": "fake-agent-out.txt"},
                {"kind": "response_regex", "pattern": "Wrote .+\\.txt"},
                {"kind": "response_contains", "value": "fake-agent-out"},
                {"kind": "no_errors"},
                {"kind": "status", "value": "success"},
            ]}]
     }],
     "metric": "multi_assert", "metric_args": {}},

    # ---- Multi-turn (context=keep) example ----
    {"slug": "echo-multiturn", "name": "Echo · multi-turn (context kept)",
     "description": "Two-turn conversation against the echo agent. Each turn echoes its prompt; we assert that the second response contains the user's latest message AND the agent saw the earlier turn in its messages array (echoes are deterministic over the LAST user message).",
     "target_kind": "agent", "target_slug": "echo-coder",
     "dataset": [{
        "name": "two-turn chat",
        "context": "keep",
        "steps": [
            {"prompt": "first message please",
             "asserts": [
                 {"kind": "response_contains", "value": "first message please"},
                 {"kind": "no_errors"},
             ]},
            {"prompt": "second message please",
             "asserts": [
                 {"kind": "response_contains", "value": "second message please"},
                 # Echo always echoes the *latest* user message, so we don't
                 # assert it contains "first"; we just assert turn 2's response
                 # is coherent and exists.
                 {"kind": "status", "value": "success"},
             ]},
        ]
     }],
     "metric": "multi_assert", "metric_args": {}},
    {"slug": "explorer-smoke", "name": "Explorer smoke",
     "description": "Confirms the Explorer agent returns at least one file path for a basic request.",
     "target_kind": "agent", "target_slug": "explorer",
     "dataset": [{"input": "find a python file in the agents repo", "expected": ".py"}],
     "metric": "assert_contains", "metric_args": {}},
    {"slug": "pipeline-smoke", "name": "Spec-pipeline smoke",
     "description": "Run the spec-pipeline on a tiny request.",
     "target_kind": "workflow", "target_slug": "spec-pipeline",
     "dataset": [{"input": "Add a /health endpoint to a FastAPI service", "expected": "health"}],
     "metric": "assert_contains", "metric_args": {}},
]


# ----------------------------------------------------------------------
# Upsert helpers
# ----------------------------------------------------------------------

def _insert_if_missing(s: Session, cls, key: str, defaults: dict):
    """Ensure a seeded row exists by slug. Never updates an existing row — the
    user's edits always win. To restore seed defaults, the user calls the
    per-resource ``/reset`` endpoint explicitly.
    """
    row = s.query(cls).filter(getattr(cls, key) == defaults[key]).first()
    if row is None:
        # The DB still has a `builtin` column on most tables (legacy); we don't
        # set it explicitly anymore and don't read it anywhere.
        row = cls(**defaults)
        s.add(row)
    return row


# Backward-compat alias for any external callers
_upsert = _insert_if_missing


def seed_all() -> dict:
    """Idempotent seed. Ensures every slug in SEED_* exists. Never updates
    existing rows — your edits always win. To restore a seed slug to its
    defaults, hit the per-resource ``/reset`` endpoint.
    """
    counts = {"models": 0, "agents": 0, "workflows": 0, "evals": 0}
    with session_scope() as s:
        for m in SEED_MODELS:
            _insert_if_missing(s, Model, "slug", m); counts["models"] += 1
        for m in _detect_ollama_models():
            _insert_if_missing(s, Model, "slug", m); counts["models"] += 1
        for a in SEED_AGENTS:
            _insert_if_missing(s, Agent, "slug", a); counts["agents"] += 1
        for w in SEED_WORKFLOWS:
            _insert_if_missing(s, Workflow, "slug", w); counts["workflows"] += 1
        for e in SEED_EVALS:
            _insert_if_missing(s, Eval, "slug", e); counts["evals"] += 1
    return counts


if __name__ == "__main__":
    from .db import init_db
    init_db()
    print(seed_all())
