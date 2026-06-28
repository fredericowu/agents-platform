"""Pydantic schemas for API."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ----- models -----
class ModelOut(_Base):
    slug: str
    provider: str
    model_id: str
    display_name: str
    params: dict[str, Any] = {}
    enabled: bool


class ModelUpdate(BaseModel):
    enabled: bool | None = None
    params: dict[str, Any] | None = None
    display_name: str | None = None
    model_id: str | None = None
    provider: str | None = None


# ----- agents -----
class AgentIn(BaseModel):
    slug: str | None = None  # auto-generated from name if omitted
    name: str
    description: str = ""
    system_prompt: str = ""
    use_cases: list[str] = []
    model_slug: str | None = None
    tool_specs: list[Any] = []
    skill_slugs: list[str] = []
    params: dict[str, Any] = {}
    mcp_config: dict[str, Any] = {}
    icon: str = "bot"
    color: str = "#58a6ff"


class AgentUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    use_cases: list[str] | None = None
    model_slug: str | None = None
    tool_specs: list[Any] | None = None
    skill_slugs: list[str] | None = None
    params: dict[str, Any] | None = None
    mcp_config: dict[str, Any] | None = None
    icon: str | None = None
    color: str | None = None


class AgentOut(_Base):
    slug: str
    name: str
    description: str
    system_prompt: str
    use_cases: list[str] = []
    model_slug: str | None
    tool_specs: list[Any]
    skill_slugs: list[str]
    params: dict[str, Any]
    mcp_config: dict[str, Any] = {}
    icon: str
    color: str
    deleted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


# ----- workflows -----
class WorkflowIn(BaseModel):
    slug: str | None = None  # auto-generated from name if omitted
    name: str
    description: str = ""
    use_cases: list[str] = []
    kind: str
    graph: dict[str, Any]


class WorkflowUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    use_cases: list[str] | None = None
    kind: str | None = None
    graph: dict[str, Any] | None = None


class WorkflowOut(_Base):
    slug: str
    name: str
    description: str
    use_cases: list[str] = []
    kind: str
    graph: dict[str, Any]
    deleted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


# ----- runs -----
class RunInput(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)
    target_slug: str | None = None   # first-class; takes precedence over input.extra
    target_id: str | None = None     # first-class; takes precedence over input.extra
    session_id: str | None = None    # resume a prior CLI session (e.g. claude --resume)
    notion_task_id: str | None = None  # Notion page ID of the kanban card that originated this run


class RunOut(_Base):
    id: str
    kind: str
    target_slug: str
    status: str
    input: dict[str, Any]
    output: dict[str, Any] | None
    error: str | None
    tokens_in: int
    tokens_out: int
    cost_usd: float
    started_at: datetime
    ended_at: datetime | None
    parent_run_id: str | None = None
    initiator_kind: str = "agent_run"
    initiator_id: str | None = None
    node_id: str | None = None
    model_slug: str | None = None
    target_id: str | None = None
    source_slug: str | None = None
    github_issue_number: int | None = None
    github_issue_url: str | None = None
    session_id: str | None = None


class RunEventOut(_Base):
    id: str
    run_id: str
    ts: datetime
    kind: str
    node_id: str | None
    payload: dict[str, Any]


# ----- targets -----
class TargetIn(BaseModel):
    slug: str
    name: str
    description: str = ""
    source_kind: str = "manual"
    source_ref: str | None = None
    plan_canvas_id: str | None = None
    report_canvas_id: str | None = None
    budget_tokens: int | None = None
    budget_usd: float | None = None
    enforce_budget: bool = False
    tags: list[str] = []
    notes: str = ""
    pr_urls: list[dict[str, Any]] = []
    created_by: str | None = None


class TargetUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    source_kind: str | None = None
    source_ref: str | None = None
    plan_canvas_id: str | None = None
    report_canvas_id: str | None = None
    budget_tokens: int | None = None
    budget_usd: float | None = None
    enforce_budget: bool | None = None
    status: str | None = None
    tags: list[str] | None = None
    notes: str | None = None
    pr_urls: list[dict[str, Any]] | None = None
    ended_at: datetime | None = None


class TargetOut(_Base):
    id: str
    slug: str
    name: str
    description: str
    source_kind: str
    source_ref: str | None
    plan_canvas_id: str | None
    report_canvas_id: str | None
    budget_tokens: int | None
    budget_usd: float | None
    enforce_budget: bool = False
    status: str
    tags: list[str] = []
    notes: str = ""
    pr_urls: list[dict[str, Any]] = []
    github_issue_number: int | None = None
    github_issue_url: str | None = None
    created_by: str | None = None
    deleted_at: datetime | None = None
    started_at: datetime
    ended_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class LinkRunIn(BaseModel):
    run_id: str
    include_descendants: bool = True


class AttachPrIn(BaseModel):
    url: str
    title: str | None = None
    status: str | None = None    # open|merged|closed
    ci_status: str | None = None # passing|failing|pending
    notes: str | None = None


class TargetSummary(BaseModel):
    """Rolled-up stats over every Run linked to a Target."""
    target_id: str
    target_slug: str
    target_name: str
    status: str
    runs_count: int
    runs_by_status: dict[str, int]   # {"running":1, "success":7, "error":1, ...}
    tokens_in: int
    tokens_out: int
    cost_usd: float
    budget_tokens: int | None
    budget_usd: float | None
    pct_of_token_budget: float | None
    pct_of_usd_budget: float | None
    agents_used: dict[str, int]      # {"explorer":2, "planner":1, ...}
    models_used: dict[str, int]      # {"claude-cli":5, "claude-cli-opus":3, ...}
    started_at: datetime
    ended_at: datetime | None
    wall_seconds: float | None


# ----- target lessons -----
class LessonIn(BaseModel):
    category: str                              # time-saver|pitfall|tooling-gap|pattern-that-worked|prompt-fix|cost-trap|scope-creep|...
    title: str
    content: str = ""
    evidence_run_ids: list[str] = []
    confidence: str = "medium"                 # low|medium|high
    applicable_tags: list[str] = []
    source: str = "retro"                      # retro|manual|cross-agent
    created_in_run_id: str | None = None       # retro run that authored this lesson


class LessonUpdate(BaseModel):
    category: str | None = None
    title: str | None = None
    content: str | None = None
    evidence_run_ids: list[str] | None = None
    confidence: str | None = None
    applicable_tags: list[str] | None = None
    source: str | None = None
    superseded_by: str | None = None
    status: str | None = None


class LessonOut(_Base):
    id: str
    target_id: str
    category: str
    title: str
    content: str
    evidence_run_ids: list[str] = []
    confidence: str
    applicable_tags: list[str] = []
    source: str
    superseded_by: str | None = None
    status: str = "active"
    created_in_run_id: str | None = None
    linked_runs: list[dict] = []
    deleted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class LessonApplicationIn(BaseModel):
    lesson_id: str
    target_id: str | None = None         # if omitted, the API resolves from lesson_id's target
    applied_in_run_id: str | None = None
    outcome: str = "retrieved"           # retrieved|shown_to_pm|applied|rejected|prevented|ignored|partial
    notes: str = ""


class LessonApplicationOut(_Base):
    id: str
    lesson_id: str
    target_id: str
    applied_in_run_id: str | None = None
    outcome: str
    notes: str
    created_at: datetime
    updated_at: datetime


class LessonEffectivenessOut(BaseModel):
    """Per-lesson effectiveness rollup."""
    lesson_id: str
    title: str
    confidence: str
    total_applications: int
    by_outcome: dict[str, int]          # {"applied": 4, "prevented": 3, "ignored": 1, ...}
    effectiveness_rate: float | None    # (applied + prevented) / total — None if total == 0
    propagation_gap_rate: float | None  # ignored / shown_to_pm — None if shown_to_pm == 0


class LessonForecastIn(BaseModel):
    """Input shape for lesson_forecast — describe the upcoming task."""
    tags: list[str] = []
    category: str | None = None         # task category from PM
    description: str = ""


class LessonForecastOut(BaseModel):
    matched_lessons: list["LessonSearchHit"]
    similar_targets: list[dict[str, str | float | int | None]]   # past Targets with matching tags
    predicted_cost_usd: dict[str, float] | None     # {"p10": 2.0, "p50": 5.0, "p90": 10.0}
    predicted_wall_seconds: dict[str, float] | None
    advisories: list[str]               # high-impact lessons to apply, or "no priors found — first delivery"


class LessonsMetrics(BaseModel):
    """Aggregate continuous-improvement metrics over all Targets."""
    total_lessons: int
    lessons_by_category: dict[str, int]
    lessons_by_confidence: dict[str, int]
    total_applications: int
    applications_by_outcome: dict[str, int]
    avg_effectiveness_rate: float | None
    avg_propagation_gap_rate: float | None
    total_targets: int
    completed_targets: int
    targets_by_status: dict[str, int]
    cost_trend: list[dict[str, str | float]]    # [{"target_slug":"...", "cost":4.68, "ended_at":"..."}]
    wall_trend: list[dict[str, str | float]]
    top_applied_lessons: list[dict[str, str | int]]   # most-applied lessons (popular = high value)
    top_ignored_lessons: list[dict[str, str | int]]   # lessons we KEEP failing to apply — biggest improvement opportunity


# ----- lesson consolidation (Wave-6 L2) -----

class LessonConsolidateIn(BaseModel):
    lesson_ids: list[str]
    title: str
    content: str
    category: str | None = None           # defaults to majority category of sources
    applicable_tags: list[str] | None = None  # defaults to union of source tags
    confidence: str | None = None         # defaults to max confidence of sources
    target_id: str | None = None          # defaults to first source's target_id


class ConsolidateSuggestion(BaseModel):
    lesson_ids: list[str]
    reason: str
    confidence: float
    common_tags: list[str]
    common_category: str | None = None


class ConsolidateDraftIn(BaseModel):
    lesson_ids: list[str]


class LessonSearchHit(BaseModel):
    """Result of a cross-target lesson search — includes the lesson + a snippet
    of its target context so the calling agent can decide if it applies."""
    lesson: LessonOut
    target_slug: str
    target_name: str
    target_status: str


# ----- run artefacts -----
class RunArtefactIn(BaseModel):
    name: str
    mime: str = "text/plain"
    content: str = ""
    is_binary: bool = False


class RunArtefactOut(_Base):
    id: str
    run_id: str
    name: str
    mime: str
    size: int
    sha: str | None
    is_binary: bool
    created_at: datetime


class RunArtefactFull(RunArtefactOut):
    content: str


# ----- mcp -----
class McpToolOut(BaseModel):
    server: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] = {}


class McpServerOut(_Base):
    name: str
    command: str
    args: list[str]
    env: dict[str, str] = {}
    enabled: bool
    source: str
    discovered_tools: list[Any]
    last_refreshed: datetime | None


# ----- skills -----
class SkillOut(BaseModel):
    slug: str
    name: str
    description: str
    path: str
    source: str = "file"   # file | custom


# ----- tools -----
class ToolOut(BaseModel):
    id: str            # "code.read_file"
    kind: str          # builtin|mcp|skill
    name: str
    description: str = ""
    server: str | None = None


# ----- eval -----
class EvalOut(_Base):
    slug: str
    name: str
    description: str
    target_kind: str
    target_slug: str
    dataset: list[Any]
    metric: str
    metric_args: dict[str, Any]


class EvalRunOut(_Base):
    id: str
    eval_slug: str
    status: str
    score: float | None
    cases: list[Any]
    started_at: datetime
    ended_at: datetime | None


# ----- playground -----
class PlaygroundIn(BaseModel):
    agent_slug: str
    message: str
    stream: bool = True
    extra: dict[str, Any] = {}


class PlaygroundOut(BaseModel):
    run_id: str
