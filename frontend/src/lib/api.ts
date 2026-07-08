// API client. Resolves against window.location for dev (vite proxy) or absolute URL in prod.
const BASE = (import.meta as any).env?.VITE_API_BASE || "";

export type AgentMcpServer = {
  type: string;        // "streamable-http" | "sse"
  url: string;
  headers?: Record<string, string>;
};

export type Agent = {
  slug: string;
  name: string;
  description: string;
  system_prompt: string;
  inherit_from: string | null;
  agent_config_slug: string | null;
  model_slug: string | null;
  tool_specs: string[];
  skill_slugs: string[];
  params: Record<string, any>;
  mcp_config: { servers?: Record<string, AgentMcpServer> };
  extra_volumes: string[];
  permissions: Record<string, boolean>;
  icon: string;
  color: string;
};

export type AgentConfig = {
  slug: string;
  name: string;
  description: string;
  mcp_config: { servers?: Record<string, AgentMcpServer> };
  extra_volumes: string[];
  permissions: Record<string, boolean>;
};

export type Workflow = {
  slug: string;
  name: string;
  description: string;
  kind: string;
  graph: Record<string, any>;
};

export type Model = {
  slug: string;
  provider: string;
  model_id: string;
  display_name: string;
  params: Record<string, any>;
  enabled: boolean;
};

export type Run = {
  id: string;
  kind: string;
  target_slug: string;
  status: string;
  input: Record<string, any>;
  output: Record<string, any> | null;
  error: string | null;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  started_at: string;
  ended_at: string | null;
  parent_run_id: string | null;
  initiator_kind: string;     // agent_run|workflow_run|chat|eval|mcp|cli
  initiator_id: string | null;
  node_id: string | null;
  model_slug: string | null;
  target_id: string | null;
  source_slug: string | null;
  retro_score_summary?: RetroScoreSummary | null;
};

export type TargetPr = {
  url: string;
  title?: string;
  status?: string;          // open|merged|closed
  ci_status?: string;       // passing|failing|pending
  notes?: string;
};

export type Target = {
  id: string;
  slug: string;
  name: string;
  description: string;
  source_kind: string;
  source_ref: string | null;
  plan_canvas_id: string | null;
  report_canvas_id: string | null;
  budget_tokens: number | null;
  budget_usd: number | null;
  enforce_budget: boolean;
  status: string;
  tags: string[];
  notes: string;
  pr_urls: TargetPr[];
  created_by: string | null;
  deleted_at: string | null;
  started_at: string;
  ended_at: string | null;
  created_at: string;
  updated_at: string;
};

export type RunArtefact = {
  id: string;
  run_id: string;
  name: string;
  mime: string;
  size: number;
  sha: string | null;
  is_binary: boolean;
  created_at: string;
};

export type RunArtefactFull = RunArtefact & { content: string };

export type TargetSummary = {
  target_id: string;
  target_slug: string;
  target_name: string;
  status: string;
  runs_count: number;
  runs_by_status: Record<string, number>;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  budget_tokens: number | null;
  budget_usd: number | null;
  pct_of_token_budget: number | null;
  pct_of_usd_budget: number | null;
  agents_used: Record<string, number>;
  models_used: Record<string, number>;
  started_at: string;
  ended_at: string | null;
  wall_seconds: number | null;
};

export type RunTree = {
  current_id: string;
  totals: { runs: number; tokens_in: number; tokens_out: number; cost_usd: number; models?: Record<string, number> };
  root: RunTreeNode;
};
export type RunTreeNode = Run & { children: RunTreeNode[] };

export type RunEvent = {
  id: string;
  run_id: string;
  ts: string;
  kind: string;
  node_id: string | null;
  payload: Record<string, any>;
};

export type ToolItem = {
  id: string;
  kind: "builtin" | "mcp" | "skill";
  name: string;
  description: string;
  server: string | null;
  input_schema?: any;
};

export type Skill = { slug: string; name: string; description: string; path: string; source?: "file" | "custom" | "override" };

export type McpServer = {
  name: string;
  command: string;
  args: string[];
  env?: Record<string, string>;
  enabled: boolean;
  source: string;
  discovered_tools: any[];
  last_refreshed: string | null;
};

export type RagProviderEndpoint = {
  method: string;
  path: string;
  params?: Record<string, string>;
  body?: Record<string, any>;
};

export type RagProviderConfig = {
  kind: "disabled" | "http" | "mcp";
  name?: string;
  base_url?: string;
  auth?: { header?: string; value?: string; value_from_file?: string; value_from_env?: string };
  lesson_path_prefix?: string;
  endpoints?: {
    search?: RagProviderEndpoint;
    upsert?: RagProviderEndpoint;
    delete?: RagProviderEndpoint;
  };
};

export type PlatformSettings = {
  command_timeout_seconds: number;
  security_mode: "insecure" | "secure";
  command_allowlist: string[];
  command_denylist: string[];
  rag_provider?: RagProviderConfig;
  github_sync_enabled?: boolean;
  github_repo?: string;
  github_webhook_secret?: string;
  tts_provider?: "openai" | "edge";
  stt_provider?: "openai" | "local";
  openai_api_key?: string;
  tts_voice?: string;
  edge_voice?: string;
  edge_voices?: Record<string, string>;
  openai_key_configured?: boolean;
  auto_compact_threshold_tokens?: number;
  agent_chain_max_hops?: number;
  _defaults?: {
    command_timeout_seconds: number;
    security_mode: "insecure" | "secure";
    command_allowlist: string[];
    command_denylist: string[];
    rag_provider?: RagProviderConfig;
    tts_provider?: string;
    stt_provider?: string;
    tts_voice?: string;
    edge_voice?: string;
    auto_compact_threshold_tokens?: number;
    agent_chain_max_hops?: number;
  };
};

export type RagHealth = {
  ok: boolean;
  kind: string;
  base_url?: string;
  auth_header_set?: boolean;
  error?: string;
  note?: string;
};

export type RetroScoreWeights = {
  weights: Record<string, number>;
  updated_at?: string;
};

export type RetroScoreDimension = {
  dimension: string;
  score: number | null;
  source: string;
  rationale: string | null;
  evidence_json: any | null;
};

export type RetroScoreSummary = {
  overall: number | null;
  computed_at: string | null;
  n_scores: number;
  scores: RetroScoreDimension[];
};

export type LessonLinkedRun = {
  run_id: string;
  kind: string;
  role: string;      // "primary" | "consolidated_from" | "evidence"
  status?: string;
  started_at?: string;
};

export type LessonApplication = {
  id: string;
  lesson_id: string;
  run_id: string;
  applied_at: string;
  outcome?: string;
};

export type Lesson = {
  id: string;
  target_id: string;
  category: string;
  title: string;
  content: string;
  confidence: string;          // "low" | "medium" | "high"
  applicable_tags: string[];
  source: string;              // "retro" | "manual" | "cross-agent"
  superseded_by: string | null;
  status: string;              // "active" | "pending_review" | "archived"
  created_in_run_id: string | null;
  linked_runs: LessonLinkedRun[];
  n_applied?: number;
  created_at: string;
  updated_at: string;
};

export type CliSession = {
  id: string;
  session_id: string;
  name: string;
  description: string;
  run_count: number;
  last_run_at: string | null;
  last_status: string | null;
  created_at: string;
  updated_at: string;
};

export type TelegramBot = {
  id: string;
  name: string;
  token: string;
  webhook_secret: string;
  enabled: boolean;
  is_sysadmin: boolean;
  agent_slug: string | null;
  admin_user_ids: string[];
};

export type TelegramBotSession = {
  chat_id: string;
  agent_slug: string | null;
  is_override: boolean;
  session_id: string | null;
  updated_at: string | null;
};

export type ConsolidationCluster = {
  confidence: number;
  lessons: Lesson[];
  shared_tags?: string[];
};

export type EvalRow = {
  slug: string;
  name: string;
  description: string;
  target_kind: string;
  target_slug: string;
  dataset: any[];
  metric: string;
  metric_args: Record<string, any>;
};

async function call<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const r = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText} — ${await r.text()}`);
  return r.json();
}

export const api = {
  health: () => call<{ ok: boolean }>("/api/health"),

  listAgents: () => call<Agent[]>("/api/agents"),
  getAgent: (slug: string) => call<Agent>(`/api/agents/${slug}`),
  saveAgent: (slug: string, patch: Partial<Agent>) =>
    call<Agent>(`/api/agents/${slug}`, { method: "PUT", body: JSON.stringify(patch) }),
  createAgent: (a: any) =>
    call<Agent>("/api/agents", { method: "POST", body: JSON.stringify(a) }),
  deleteAgent: (slug: string) =>
    call<{ deleted: string }>(`/api/agents/${slug}`, { method: "DELETE" }),
  runAgent: (slug: string, input: string) =>
    call<{ run_id: string }>(`/api/agents/${slug}/run`, {
      method: "POST", body: JSON.stringify({ input: { input } }),
    }),
  cloneAgent: (slug: string) =>
    call<Agent>(`/api/agents/${slug}/clone`, { method: "POST" }),
  resetAgent: (slug: string) =>
    call<Agent>(`/api/agents/${slug}/reset`, { method: "POST" }),
  listResettableAgents: () => call<string[]>("/api/agents/_resettable"),
  generateSlug: (kind: "agent" | "workflow", name?: string) =>
    call<{ slug: string }>(`/api/admin/slugs/generate?kind=${kind}${name ? `&name=${encodeURIComponent(name)}` : ""}`),
  renameAgent: (slug: string, newSlug: string) =>
    call<Agent>(`/api/agents/${slug}/rename`, { method: "POST", body: JSON.stringify({ new_slug: newSlug }) }),
  renameWorkflow: (slug: string, newSlug: string) =>
    call<Workflow>(`/api/workflows/${slug}/rename`, { method: "POST", body: JSON.stringify({ new_slug: newSlug }) }),

  listAgentConfigs: () => call<AgentConfig[]>("/api/agent-configs"),
  getAgentConfig: (slug: string) => call<AgentConfig>(`/api/agent-configs/${slug}`),
  createAgentConfig: (c: any) =>
    call<AgentConfig>("/api/agent-configs", { method: "POST", body: JSON.stringify(c) }),
  saveAgentConfig: (slug: string, patch: Partial<AgentConfig>) =>
    call<AgentConfig>(`/api/agent-configs/${slug}`, { method: "PUT", body: JSON.stringify(patch) }),
  deleteAgentConfig: (slug: string) =>
    call<{ deleted: string }>(`/api/agent-configs/${slug}`, { method: "DELETE" }),

  listWorkflows: () => call<Workflow[]>("/api/workflows"),
  getWorkflow: (slug: string) => call<Workflow>(`/api/workflows/${slug}`),
  saveWorkflow: (slug: string, patch: Partial<Workflow>) =>
    call<Workflow>(`/api/workflows/${slug}`, { method: "PUT", body: JSON.stringify(patch) }),
  createWorkflow: (w: any) =>
    call<Workflow>("/api/workflows", { method: "POST", body: JSON.stringify(w) }),
  deleteWorkflow: (slug: string) =>
    call<{ deleted: string }>(`/api/workflows/${slug}`, { method: "DELETE" }),
  runWorkflow: (slug: string, input: string) =>
    call<{ run_id: string }>(`/api/workflows/${slug}/run`, {
      method: "POST", body: JSON.stringify({ input: { input } }),
    }),
  cloneWorkflow: (slug: string) =>
    call<Workflow>(`/api/workflows/${slug}/clone`, { method: "POST" }),
  resetWorkflow: (slug: string) =>
    call<Workflow>(`/api/workflows/${slug}/reset`, { method: "POST" }),
  listResettableWorkflows: () => call<string[]>("/api/workflows/_resettable"),

  listModels: () => call<Model[]>("/api/models"),
  createModel: (m: any) =>
    call<Model>("/api/models", { method: "POST", body: JSON.stringify(m) }),
  updateModel: (slug: string, patch: any) =>
    call<Model>(`/api/models/${slug}`, { method: "PUT", body: JSON.stringify(patch) }),
  deleteModel: (slug: string) =>
    call<{ deleted: string }>(`/api/models/${slug}`, { method: "DELETE" }),
  providerInfo: () => call<Record<string, { label: string; fields: string[]; env?: string[] }>>(
    "/api/models/providers/info"),

  listRuns: (limit = 50, kind?: string, opts: { rootsOnly?: boolean; q?: string; targetId?: string; targetSlug?: string; summary?: boolean } = {}) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (kind) params.set("kind", kind);
    if (opts.rootsOnly) params.set("roots_only", "true");
    if (opts.q) params.set("q", opts.q);
    if (opts.targetId) params.set("target_id", opts.targetId);
    if (opts.targetSlug) params.set("target_slug", opts.targetSlug);
    if (opts.summary) params.set("summary", "true");
    return call<Run[]>(`/api/runs?${params}`);
  },
  getRun: (id: string) => call<Run>(`/api/runs/${id}`),
  getRunEvents: (id: string) => call<RunEvent[]>(`/api/runs/${id}/events`),
  getRunTree: (id: string) => call<RunTree>(`/api/runs/${id}/tree`),
  cancelRun: (id: string) => call<{ cancelled: string }>(`/api/runs/${id}/cancel`, { method: "POST" }),
  cancelAllRuns: () => call<{ cancelled_roots: number; cancelled_total: number; subprocesses_killed: number }>(
    "/api/runs/cancel_all", { method: "POST" }),

  exportAgent:  (slug: string) => call<any>(`/api/agents/${slug}/export`),
  importAgent:  (spec: any)    => call<Agent>("/api/agents/import",   { method: "POST", body: JSON.stringify(spec) }),
  exportWorkflow: (slug: string) => call<any>(`/api/workflows/${slug}/export`),
  importWorkflow: (spec: any)    => call<Workflow>("/api/workflows/import", { method: "POST", body: JSON.stringify(spec) }),

  listMcpServers: () => call<McpServer[]>("/api/mcp/servers"),
  createMcpServer: (m: any) =>
    call<McpServer>("/api/mcp/servers", { method: "POST", body: JSON.stringify(m) }),
  updateMcpServer: (name: string, m: any) =>
    call<McpServer>(`/api/mcp/servers/${name}`, { method: "PUT", body: JSON.stringify(m) }),
  deleteMcpServer: (name: string) =>
    call<{ deleted: string }>(`/api/mcp/servers/${name}`, { method: "DELETE" }),
  refreshMcp: () => call<McpServer[]>("/api/mcp/refresh", { method: "POST" }),
  discoverMcpTools: (name: string) =>
    call<{ server: string; tools: any[] }>(`/api/mcp/servers/${name}/discover`, { method: "POST" }),

  listSkills: () => call<Skill[]>("/api/skills"),
  getSkill: (slug: string) => call<{ slug: string; found: boolean; content?: string }>(`/api/skills/${slug}`),
  createSkill: (sk: { slug: string; name: string; description?: string; content: string }) =>
    call<Skill>("/api/skills", { method: "POST", body: JSON.stringify(sk) }),
  updateSkill: (slug: string, patch: { name?: string; description?: string; content?: string }) =>
    call<Skill>(`/api/skills/${slug}`, { method: "PUT", body: JSON.stringify(patch) }),
  deleteSkill: (slug: string) => call<{ deleted: string }>(`/api/skills/${slug}`, { method: "DELETE" }),
  resetSkill:  (slug: string) => call<any>(`/api/skills/${slug}/reset`, { method: "POST" }),
  listTools: () => call<ToolItem[]>("/api/tools"),

  listEvals: () => call<EvalRow[]>("/api/evals"),
  createEval: (e: any) =>
    call<EvalRow>("/api/evals", { method: "POST", body: JSON.stringify(e) }),
  updateEval: (slug: string, e: any) =>
    call<EvalRow>(`/api/evals/${slug}`, { method: "PUT", body: JSON.stringify(e) }),
  deleteEval: (slug: string) =>
    call<{ deleted: string }>(`/api/evals/${slug}`, { method: "DELETE" }),
  resetEval: (slug: string) =>
    call<EvalRow>(`/api/evals/${slug}/reset`, { method: "POST" }),
  listResettableEvals: () => call<string[]>("/api/evals/_resettable"),
  runEval: (slug: string) =>
    call<any>(`/api/evals/${slug}/run`, { method: "POST" }),

  playgroundChat: (agent_slug: string, message: string, extra: Record<string, any> = {}) =>
    call<{ run_id: string }>("/api/playground/chat", {
      method: "POST", body: JSON.stringify({ agent_slug, message, stream: true, extra }),
    }),

  // ----- Targets -----
  listTargets: (opts: { includeDeleted?: boolean; status?: string; q?: string; limit?: number } = {}) => {
    const p = new URLSearchParams();
    if (opts.includeDeleted) p.set("include_deleted", "true");
    if (opts.status) p.set("status", opts.status);
    if (opts.q) p.set("q", opts.q);
    if (opts.limit) p.set("limit", String(opts.limit));
    return call<Target[]>(`/api/targets${p.toString() ? "?" + p : ""}`);
  },
  getTarget: (slug: string) => call<Target>(`/api/targets/${slug}`),
  createTarget: (t: Partial<Target>) =>
    call<Target>("/api/targets", { method: "POST", body: JSON.stringify(t) }),
  updateTarget: (slug: string, patch: Partial<Target>) =>
    call<Target>(`/api/targets/${slug}`, { method: "PUT", body: JSON.stringify(patch) }),
  renameTarget: (slug: string, newSlug: string) =>
    call<Target>(`/api/targets/${slug}/rename`, { method: "POST", body: JSON.stringify({ new_slug: newSlug }) }),
  deleteTarget: (slug: string, hard = false) =>
    call<{ deleted: string }>(`/api/targets/${slug}${hard ? "?hard=true" : ""}`, { method: "DELETE" }),
  restoreTarget: (slug: string) =>
    call<Target>(`/api/targets/${slug}/restore`, { method: "POST" }),
  getTargetSummary: (slug: string) => call<TargetSummary>(`/api/targets/${slug}/summary`),
  getTargetRuns: (slug: string, limit = 500) =>
    call<{ target: { id: string; slug: string; name: string }; count: number; runs: any[] }>(
      `/api/targets/${slug}/runs?limit=${limit}`),
  linkRunToTarget: (slug: string, run_id: string, include_descendants = true) =>
    call<{ target_id: string; run_ids: string[]; linked: number }>(`/api/targets/${slug}/link_run`, {
      method: "POST", body: JSON.stringify({ run_id, include_descendants }),
    }),
  unlinkRunFromTarget: (slug: string, run_id: string, include_descendants = false) =>
    call<{ target_id: string; run_ids: string[]; unlinked: number }>(
      `/api/targets/${slug}/link_run/${run_id}${include_descendants ? "?include_descendants=true" : ""}`,
      { method: "DELETE" }),
  attachPrToTarget: (slug: string, pr: TargetPr) =>
    call<Target>(`/api/targets/${slug}/pr`, { method: "POST", body: JSON.stringify(pr) }),
  detachPrFromTarget: (slug: string, url: string) =>
    call<Target>(`/api/targets/${slug}/pr?url=${encodeURIComponent(url)}`, { method: "DELETE" }),

  // ----- Run artefacts -----
  listRunArtefacts: (run_id: string) =>
    call<RunArtefact[]>(`/api/runs/${run_id}/artefacts`),
  getRunArtefact: (run_id: string, name: string) =>
    call<RunArtefactFull>(`/api/runs/${run_id}/artefacts/${encodeURIComponent(name)}`),

  // ----- Lessons -----
  listLessons: (params: { status?: string; category?: string; q?: string; confidence?: string; tags?: string; limit?: number; offset?: number } = {}) => {
    const p = new URLSearchParams();
    if (params.status) p.set("status", params.status);
    if (params.category) p.set("category", params.category);
    if (params.q) p.set("q", params.q);
    if (params.confidence) p.set("confidence", params.confidence);
    if (params.tags) p.set("tags", params.tags);
    if (params.limit) p.set("limit", String(params.limit));
    if (params.offset) p.set("offset", String(params.offset));
    return call<Lesson[]>(`/api/lessons/${p.toString() ? "?" + p : ""}`);
  },
  getLesson: (id: string) => call<Lesson>(`/api/lessons/${id}`),
  getLessonRuns: (id: string) => call<LessonLinkedRun[]>(`/api/lessons/${id}/runs`),
  getLessonApplications: (id: string) => call<LessonApplication[]>(`/api/lessons/${id}/applications`),
  approveLesson: (id: string) => call<Lesson>(`/api/lessons/${id}/approve`, { method: "POST" }),
  archiveLesson: (id: string) => call<Lesson>(`/api/lessons/${id}/archive`, { method: "POST" }),
  restoreLesson: (id: string) => call<Lesson>(`/api/lessons/${id}/restore`, { method: "POST" }),
  consolidateLessons: (body: { lesson_ids: string[]; title: string; category: string; confidence: string; tags: string[]; content: string }) =>
    call<Lesson>("/api/lessons/consolidate", { method: "POST", body: JSON.stringify(body) }),
  getConsolidationSuggestions: (params: { limit?: number } = {}) => {
    const p = new URLSearchParams();
    if (params.limit) p.set("limit", String(params.limit));
    return call<ConsolidationCluster[]>(`/api/lessons/consolidate/suggestions${p.toString() ? "?" + p : ""}`);
  },
  draftConsolidatedLesson: (lesson_ids: string[]) =>
    call<{ run_id: string; status: string }>("/api/lessons/consolidate/draft", { method: "POST", body: JSON.stringify({ lesson_ids }) }),

  ragHealth: () => call<RagHealth>("/api/lessons/rag/health"),
  ragResync: () => call<{synced: number; failed: number; errors: any[]}>("/api/lessons/rag/resync", { method: "POST" }),
  ragConfig: () => call<{kind: string; config: RagProviderConfig}>("/api/lessons/rag/config"),

  getSettings:   () => call<PlatformSettings>("/api/settings"),
  updateSetting: (key: string, value: any) =>
    call<PlatformSettings>(`/api/settings/${key}`, {
      method: "PUT", body: JSON.stringify({ value }),
    }),
  resetSettings: () => call<PlatformSettings>("/api/settings/reset", { method: "POST" }),

  testGithubConnection: () =>
    call<{ ok: boolean; output: string }>("/api/github/test", { method: "POST" }),

  getRetroScoreWeights: () => call<RetroScoreWeights>("/api/retro-score-weights"),
  setRetroScoreWeights: (weights: Record<string, number>) =>
    call<RetroScoreWeights>("/api/retro-score-weights", {
      method: "PUT", body: JSON.stringify({ weights }),
    }),

  getRunRetroScores: (runId: string) =>
    call<RetroScoreSummary>(`/api/runs/${runId}/retro-scores`),
  overrideRetroScore: (runId: string, dimension: string, score: number, rationale?: string) =>
    call<RetroScoreDimension>(`/api/runs/${runId}/retro-scores/${dimension}`, {
      method: "PATCH", body: JSON.stringify({ score, rationale }),
    }),
  recomputeRetroScore: (runId: string) =>
    call<RetroScoreSummary>(`/api/runs/${runId}/retro-scores/recompute`, { method: "POST" }),

  // CLI Sessions
  listSessions: (opts: { q?: string; limit?: number } = {}) => {
    const p = new URLSearchParams();
    if (opts.q) p.set("q", opts.q);
    if (opts.limit) p.set("limit", String(opts.limit));
    return call<CliSession[]>(`/api/sessions${p.toString() ? "?" + p : ""}`);
  },
  updateSession: (session_id: string, patch: { name?: string; description?: string }) =>
    call<CliSession>(`/api/sessions/${session_id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteSession: (session_id: string) =>
    call<{ deleted: string }>(`/api/sessions/${session_id}`, { method: "DELETE" }),

  // Flowise
  listFlowiseChatflows: () => call<{ id: string; name: string; deployed?: boolean }[]>("/api/flowise/chatflows"),

  // Telegram bots
  listTelegramBots: () => call<TelegramBot[]>("/api/telegram/bots"),
  createTelegramBot: (b: any) =>
    call<TelegramBot>("/api/telegram/bots", { method: "POST", body: JSON.stringify(b) }),
  updateTelegramBot: (id: string, patch: any) =>
    call<TelegramBot>(`/api/telegram/bots/${id}`, { method: "PUT", body: JSON.stringify(patch) }),
  deleteTelegramBot: (id: string) =>
    call<void>(`/api/telegram/bots/${id}`, { method: "DELETE" }),
  registerTelegramWebhook: (id: string) =>
    call<{ ok: boolean; message: string }>(`/api/telegram/bots/${id}/register-webhook`, { method: "POST" }),
  listTelegramBotSessions: (id: string) =>
    call<TelegramBotSession[]>(`/api/telegram/bots/${id}/sessions`),
};

// SSE helper
export type StreamHandler = (evt: { kind: string; node_id: string | null; payload: any; ts?: string }) => void;

export function streamRun(runId: string, onEvt: StreamHandler): () => void {
  const url = `${BASE}/api/runs/${runId}/stream`;
  const es = new EventSource(url);
  const handler = (e: MessageEvent) => {
    try {
      onEvt(JSON.parse(e.data));
    } catch {/* ignore */}
  };
  // listen on all named events
  const kinds = ["log", "node_start", "node_end", "llm_token", "tool_call",
                 "tool_result", "error", "done"];
  kinds.forEach(k => es.addEventListener(k, handler as any));
  es.onerror = () => es.close();
  return () => es.close();
}
