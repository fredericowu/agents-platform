import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import Page from "../components/Page";
import { api, type Agent, type Model, type ToolItem, type Skill } from "../lib/api";

const BLANK: Agent = {
  slug: "", name: "", description: "", system_prompt: "",
  inherit_from: null, model_slug: "claude-cli-sonnet", tool_specs: [], skill_slugs: [],
  params: {}, mcp_config: {}, extra_volumes: [], icon: "bot", color: "#58a6ff",
};

export default function AgentEdit() {
  const { slug } = useParams<{ slug: string }>();
  const isNew = slug === "new";
  const nav = useNavigate();
  const [a, setA] = useState<Agent | null>(null);
  const [models, setModels] = useState<Model[]>([]);
  const [tools, setTools] = useState<ToolItem[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [resettableSet, setResettableSet] = useState<Set<string>>(new Set());
  const [allAgents, setAllAgents] = useState<Agent[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [slugLocked, setSlugLocked] = useState(false);
  const [editedSlug, setEditedSlug] = useState("");
  const [runOutput, setRunOutput] = useState<string>("");
  const [runStatus, setRunStatus] = useState<string>("");
  const [runInput, setRunInput] = useState("hello");
  const [flowiseChatflows, setFlowiseChatflows] = useState<{ id: string; name: string }[]>([]);
  const [flowiseError, setFlowiseError] = useState("");

  useEffect(() => {
    api.listModels().then(setModels);
    api.listTools().then(setTools);
    api.listSkills().then(setSkills);
    api.listAgents().then(setAllAgents).catch(() => {});
    api.listResettableAgents().then(r => setResettableSet(new Set(r))).catch(() => {});
    if (!slug) return;
    if (isNew) {
      setA({ ...BLANK });
      api.generateSlug("agent").then(r => setA(prev => prev ? { ...prev, slug: r.slug } : prev));
    } else {
      api.getAgent(slug).then(a => { setA(a); setEditedSlug(a.slug); });
    }
  }, [slug, isNew]);

  const enabledToolIds = useMemo(() => new Set(a?.tool_specs ?? []), [a]);
  const enabledSkills  = useMemo(() => new Set(a?.skill_slugs ?? []), [a]);
  const cliModels = useMemo(() => models.filter(m => m.provider === "cli"), [models]);
  const isDockerCli = useMemo(() => {
    const m = models.find(m => m.slug === a?.model_slug);
    return m?.provider === "cli";
  }, [a?.model_slug, models]);

  const agentType: "standard" | "cli" | "flowise" = useMemo(() => {
    if ((a?.params as any)?.flowise_flow_id) return "flowise";
    const m = models.find(m => m.slug === a?.model_slug);
    if (m?.provider === "cli") return "cli";
    return "standard";
  }, [a?.model_slug, a?.params, models]);

  function setAgentType(type: "standard" | "cli" | "flowise") {
    if (!a) return;
    if (type === "cli") {
      const firstCli = cliModels[0];
      const newParams = { ...a.params } as any;
      delete newParams.flowise_flow_id;
      setA({ ...a, model_slug: firstCli?.slug ?? a.model_slug, params: newParams });
    } else if (type === "flowise") {
      const newParams = { ...a.params } as any;
      if (!newParams.flowise_flow_id) newParams.flowise_flow_id = "";
      setA({ ...a, model_slug: null, params: newParams });
      if (flowiseChatflows.length === 0) {
        api.listFlowiseChatflows()
          .then(flows => setFlowiseChatflows(flows))
          .catch(e => setFlowiseError(String(e.message || e)));
      }
    } else {
      const newParams = { ...a.params } as any;
      delete newParams.flowise_flow_id;
      const firstStandard = models.find(m => m.provider !== "cli");
      setA({ ...a, model_slug: firstStandard?.slug ?? a.model_slug, params: newParams });
    }
  }

  function loadFlowiseChatflows() {
    setFlowiseError("");
    api.listFlowiseChatflows()
      .then(flows => setFlowiseChatflows(flows))
      .catch(e => setFlowiseError(String(e.message || e)));
  }

  if (!a) return <Page title="Agent">…loading…</Page>;

  async function save() {
    if (!a) return;
    setSaving(true); setError("");
    try {
      if (isNew) {
        const created = await api.createAgent({
          slug: a.slug, name: a.name || a.slug, description: a.description,
          system_prompt: a.system_prompt, inherit_from: a.inherit_from || null,
          model_slug: a.model_slug, tool_specs: a.tool_specs, skill_slugs: a.skill_slugs,
          params: a.params, mcp_config: a.mcp_config, extra_volumes: a.extra_volumes,
          color: a.color, icon: a.icon,
        });
        nav(`/agents/${created.slug}`);
      } else if (slug) {
        // rename first if slug changed
        const targetSlug = editedSlug.trim() || slug;
        if (targetSlug !== slug) {
          await api.renameAgent(slug, targetSlug);
        }
        await api.saveAgent(targetSlug, {
          name: a.name, description: a.description, system_prompt: a.system_prompt,
          inherit_from: a.inherit_from || null, model_slug: a.model_slug,
          tool_specs: a.tool_specs, skill_slugs: a.skill_slugs,
          params: a.params, mcp_config: a.mcp_config, extra_volumes: a.extra_volumes,
          color: a.color, icon: a.icon,
        });
        if (targetSlug !== slug) nav(`/agents/${targetSlug}`);
      }
    } catch (e: any) { setError(String(e.message || e)); }
    finally { setSaving(false); }
  }

  async function remove() {
    if (!slug || isNew || !a) return;
    if (!confirm(`Delete agent "${slug}"?`)) return;
    try { await api.deleteAgent(slug); nav("/agents"); }
    catch (e: any) { setError(String(e.message || e)); }
  }

  async function exportSpec() {
    if (!slug || isNew) return;
    const spec = await api.exportAgent(slug);
    const blob = new Blob([JSON.stringify(spec, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `${slug}.agent.json`; a.click();
    URL.revokeObjectURL(url);
  }

  async function run() {
    if (!slug || isNew) return;
    setRunOutput(""); setRunStatus("running");
    const { run_id } = await api.runAgent(slug, runInput);
    const es = new EventSource(`/api/runs/${run_id}/stream`);
    es.addEventListener("llm_token", (e: any) => {
      try { setRunOutput(o => o + (JSON.parse(e.data).payload.delta || "")); } catch {}
    });
    es.addEventListener("node_end", () => { setRunStatus("success"); es.close(); });
    es.addEventListener("error", () => { setRunStatus("error"); es.close(); });
    es.addEventListener("done", () => { setRunStatus("done"); es.close(); });
  }

  function toggleTool(id: string) {
    if (!a) return;
    const next = a.tool_specs.includes(id)
      ? a.tool_specs.filter(t => t !== id)
      : [...a.tool_specs, id];
    setA({ ...a, tool_specs: next });
  }
  function toggleSkill(slug: string) {
    if (!a) return;
    const next = a.skill_slugs.includes(slug)
      ? a.skill_slugs.filter(t => t !== slug)
      : [...a.skill_slugs, slug];
    setA({ ...a, skill_slugs: next });
  }

  const inlineDescription = !isNew ? (
    <input
      value={a.description}
      onChange={e => setA({ ...a, description: e.target.value })}
      placeholder="Add a description…"
      title="Click to edit description"
      className="inline-edit-subtitle"
      style={{
        background: "transparent",
        border: "none",
        borderBottom: "1px dashed transparent",
        outline: "none",
        color: "inherit",
        font: "inherit",
        fontSize: "inherit",
        width: "100%",
        padding: "0",
        cursor: "text",
      }}
      onMouseEnter={e => (e.currentTarget.style.borderBottomColor = "var(--color-muted, #666)")}
      onMouseLeave={e => { if (document.activeElement !== e.currentTarget) e.currentTarget.style.borderBottomColor = "transparent"; }}
      onFocus={e => (e.currentTarget.style.borderBottomColor = "var(--color-accent, #58a6ff)")}
      onBlur={e => (e.currentTarget.style.borderBottomColor = "transparent")}
    />
  ) : "Define a new agent profile.";

  return (
    <Page title={isNew ? "New agent" : a.name} subtitle={inlineDescription}
          actions={
            <>
              <Link to="/agents" className="btn">← back</Link>
              {!isNew && (
                <button className="btn" onClick={exportSpec} data-testid="agent-export">export</button>
              )}
              {!isNew && slug && resettableSet.has(slug) && (
                <button className="btn" data-testid="agent-reset"
                        onClick={async () => {
                          if (!confirm(`Reset "${slug}" to its seed defaults? Your edits will be lost.`)) return;
                          try { const next = await api.resetAgent(slug); setA(next); }
                          catch (e: any) { setError(String(e.message || e)); }
                        }}>reset to default</button>
              )}
              {!isNew && (
                <button className="btn btn-danger" onClick={remove} data-testid="agent-delete">delete</button>
              )}
              <button className="btn btn-primary" onClick={save} disabled={saving} data-testid="agent-save">
                {saving ? "saving..." : (isNew ? "create" : "save")}
              </button>
            </>
          }>
      {error && <div className="codebox text-err mb-3" data-testid="agent-error">{error}</div>}
      <div className="grid grid-cols-3 gap-4">
        <div className="col-span-2 space-y-4">
          <div className="card">
            <h2 className="text-base font-semibold mb-3">Configuration</h2>

            {/* ───── Agent type ───── */}
            <label className="block text-xs text-muted mb-1">agent type</label>
            <div className="flex gap-2 mb-4">
              {(["standard", "cli", "flowise"] as const).map(t => (
                <button
                  key={t}
                  type="button"
                  onClick={() => setAgentType(t)}
                  className={`btn text-xs px-3 py-1 ${agentType === t ? "btn-primary" : ""}`}
                >
                  {t}
                </button>
              ))}
            </div>

            {agentType === "cli" && (
              <div className="mb-4 p-3 border border-line rounded space-y-2">
                <label className="block text-xs text-muted mb-1">CLI</label>
                <select
                  value={a.model_slug ?? ""}
                  onChange={e => setA({ ...a, model_slug: e.target.value || null })}
                  data-testid="agent-cli-select"
                >
                  {cliModels.map(m => (
                    <option key={m.slug} value={m.slug}>{m.display_name}</option>
                  ))}
                  {cliModels.length === 0 && <option value="">no CLI models found</option>}
                </select>
              </div>
            )}

            {agentType === "flowise" && (
              <div className="mb-4 p-3 border border-line rounded space-y-2">
                <div className="flex items-center justify-between mb-1">
                  <label className="text-xs text-muted">Flowise workflow</label>
                  <button className="btn text-xs py-0.5 px-2" onClick={loadFlowiseChatflows}>↻ refresh</button>
                </div>
                {flowiseError && <div className="text-xs text-err">{flowiseError}</div>}
                <select
                  value={(a.params as any)?.flowise_flow_id ?? ""}
                  onChange={e => {
                    const p = { ...(a.params || {}) } as any;
                    p.flowise_flow_id = e.target.value;
                    setA({ ...a, params: p });
                  }}
                  data-testid="agent-flowise-select"
                >
                  <option value="">— select a flow —</option>
                  {flowiseChatflows.map(f => (
                    <option key={f.id} value={f.id}>{f.name}</option>
                  ))}
                </select>
                {flowiseChatflows.length === 0 && !flowiseError && (
                  <p className="text-xs text-muted">Click ↻ refresh to load flows from Flowise.</p>
                )}
              </div>
            )}

            <label className="block text-xs text-muted mb-1">name</label>
            <input value={a.name}
                   onChange={e => {
                     const name = e.target.value;
                     setA({ ...a, name });
                     if (isNew && !slugLocked) {
                       api.generateSlug("agent", name).then(r =>
                         setA(prev => prev ? { ...prev, name, slug: r.slug } : prev)
                       ).catch(() => {});
                     }
                   }}
                   data-testid="agent-name" />
            <label className="block text-xs text-muted mt-3 mb-1">slug</label>
            {isNew ? (
              <input value={a.slug}
                     onChange={e => { setSlugLocked(true); setA({ ...a, slug: e.target.value }); }}
                     data-testid="agent-slug" className="font-mono" />
            ) : (
              <input value={editedSlug}
                     onChange={e => setEditedSlug(e.target.value)}
                     data-testid="agent-slug" className="font-mono" />
            )}
            <label className="block text-xs text-muted mt-3 mb-1">inherit instructions from</label>
            <select value={a.inherit_from ?? ""}
                    onChange={e => setA({ ...a, inherit_from: e.target.value || null })}
                    data-testid="agent-inherit-from">
              <option value="">(none — use own system prompt)</option>
              {allAgents
                .filter(ag => ag.slug !== (slug === "new" ? undefined : slug))
                .map(ag => (
                  <option key={ag.slug} value={ag.slug}>{ag.name} ({ag.slug})</option>
                ))}
            </select>
            <label className="block text-xs text-muted mt-3 mb-1">
              system prompt
              {a.inherit_from && (
                <span className="ml-2 text-[10px] text-muted italic">
                  (inherited from <strong>{a.inherit_from}</strong> when left blank)
                </span>
              )}
            </label>
            <textarea rows={6} value={a.system_prompt}
                      onChange={e => setA({ ...a, system_prompt: e.target.value })}
                      placeholder={a.inherit_from ? `Inheriting from "${a.inherit_from}". Enter text here to override.` : ""}
                      data-testid="agent-prompt" />
            {agentType === "standard" && (
              <>
                <label className="block text-xs text-muted mt-3 mb-1">model</label>
                <select value={a.model_slug ?? ""} onChange={e => setA({ ...a, model_slug: e.target.value || null })}
                        data-testid="agent-model">
                  <option value="">(none)</option>
                  {models.filter(m => m.provider !== "cli").map(m => (
                    <option key={m.slug} value={m.slug}>{m.display_name}</option>
                  ))}
                </select>
              </>
            )}
            <div className="grid grid-cols-2 gap-3 mt-3">
              <div>
                <label className="block text-xs text-muted mb-1">color</label>
                <input value={a.color} onChange={e => setA({ ...a, color: e.target.value })} />
              </div>
              <div>
                <label className="block text-xs text-muted mb-1">icon</label>
                <input value={a.icon} onChange={e => setA({ ...a, icon: e.target.value })} />
              </div>
            </div>

            {/* ───── Security override ───── */}
            <div className="mt-4 pt-3 border-t border-line">
              <div className="text-xs text-muted uppercase mb-2">Security</div>
              <label className="block text-xs text-muted mb-1">
                security mode
                <span className="ml-1 text-[10px]">
                  (overrides the global default from Settings)
                </span>
              </label>
              <select className="w-full mb-2"
                      data-testid="agent-security-mode"
                      value={(a.params as any)?.security_mode || "inherit"}
                      onChange={e => {
                        const v = e.target.value;
                        const p = { ...(a.params || {}) } as any;
                        if (v === "inherit") delete p.security_mode;
                        else p.security_mode = v;
                        setA({ ...a, params: p });
                      }}>
                <option value="inherit">inherit (use global default)</option>
                <option value="insecure">insecure — deny-list only</option>
                <option value="secure">secure — allow-list enforced + CLI no bash</option>
              </select>
              <label className="block text-xs text-muted mb-1">
                custom allow-list (one entry per line, optional)
                <span className="ml-1 text-[10px]">
                  — when set, replaces the global allow-list for this agent in secure mode
                </span>
              </label>
              <textarea rows={4}
                        className="font-mono text-xs"
                        placeholder="(blank → use global allow-list)"
                        data-testid="agent-allowlist"
                        value={((a.params as any)?.command_allowlist || []).join("\n")}
                        onChange={e => {
                          const lines = e.target.value.split("\n")
                            .map(l => l.trim()).filter(Boolean);
                          const p = { ...(a.params || {}) } as any;
                          if (lines.length === 0) delete p.command_allowlist;
                          else p.command_allowlist = lines;
                          setA({ ...a, params: p });
                        }} />
            </div>
          </div>

          {/* ───── Extra Volumes ───── */}
          <div className="card">
            <h2 className="text-base font-semibold mb-1">Extra volumes</h2>
            <p className="text-xs text-muted mb-3">
              Docker volume mounts injected when this agent runs in a container.
              One entry per line in <code>host:container</code> format
              (e.g. <code>/var/run/docker.sock:/var/run/docker.sock</code>).
              {a.inherit_from && (
                <span className="ml-1">
                  Volumes from <strong>{a.inherit_from}</strong> are prepended automatically.
                </span>
              )}
            </p>
            <textarea rows={4}
                      className="font-mono text-xs"
                      placeholder={a.inherit_from
                        ? `(leave blank to use only inherited volumes from "${a.inherit_from}")`
                        : "/var/run/docker.sock:/var/run/docker.sock"}
                      data-testid="agent-volumes"
                      value={(a.extra_volumes || []).join("\n")}
                      onChange={e => {
                        const lines = e.target.value.split("\n")
                          .map(l => l.trim()).filter(Boolean);
                        setA({ ...a, extra_volumes: lines });
                      }} />
          </div>

          {/* ───── MCP Config ───── */}
          <div className="card">
            <h2 className="text-base font-semibold mb-1">MCP servers</h2>
            <p className="text-xs text-muted mb-3">
              When this agent runs in a Docker container, these servers are injected via{" "}
              <code>--mcp-config</code>. Use <code>http://host.docker.internal:9123/mcp</code>{" "}
              to point at the AW Gateway.
            </p>
            {/* Server list */}
            {Object.entries(a.mcp_config?.servers ?? {}).map(([name, srv]) => (
              <div key={name} className="border border-line rounded p-3 mb-2 space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-mono font-semibold">{name}</span>
                  <button className="btn btn-danger text-xs py-0.5 px-2"
                    onClick={() => {
                      const next = { ...a.mcp_config, servers: { ...(a.mcp_config?.servers ?? {}) } };
                      delete next.servers![name];
                      setA({ ...a, mcp_config: next });
                    }}>remove</button>
                </div>
                <div>
                  <label className="block text-xs text-muted mb-0.5">URL</label>
                  <input className="text-xs font-mono" value={srv.url}
                    onChange={e => {
                      const next = { ...a.mcp_config, servers: { ...(a.mcp_config?.servers ?? {}), [name]: { ...srv, url: e.target.value } } };
                      setA({ ...a, mcp_config: next });
                    }} />
                </div>
                <div>
                  <label className="block text-xs text-muted mb-0.5">Authorization header (Bearer token)</label>
                  <input className="text-xs font-mono"
                    value={srv.headers?.["Authorization"]?.replace("Bearer ", "") ?? ""}
                    placeholder="paste token here"
                    onChange={e => {
                      const tok = e.target.value.trim();
                      const h: Record<string, string> = tok ? { Authorization: `Bearer ${tok}` } : {};
                      const next = { ...a.mcp_config, servers: { ...(a.mcp_config?.servers ?? {}), [name]: { ...srv, headers: h } } };
                      setA({ ...a, mcp_config: next });
                    }} />
                </div>
              </div>
            ))}
            <button className="btn text-xs mt-1"
              onClick={() => {
                const newName = `server-${Date.now()}`;
                const next = { servers: { ...(a.mcp_config?.servers ?? {}), [newName]: { type: "streamable-http", url: "http://host.docker.internal:9123/mcp", headers: {} } } };
                setA({ ...a, mcp_config: next });
              }}>+ add server</button>
          </div>

          <div className={`card${isDockerCli ? " opacity-50 pointer-events-none select-none" : ""}`}>
            <h2 className="text-base font-semibold mb-3">
              Tools ({a.tool_specs.length} selected)
              {isDockerCli && <span className="ml-2 text-xs font-normal text-muted normal-case">(managed by Docker image)</span>}
            </h2>
            <div className="space-y-3">
              {["builtin", "mcp", "skill"].map(kind => (
                <div key={kind}>
                  <div className="text-xs text-muted uppercase mb-1">{kind}</div>
                  <div className="grid grid-cols-2 gap-1 max-h-48 overflow-y-auto">
                    {tools.filter(t => t.kind === kind).map(t => (
                      <label key={t.id} className="flex items-center gap-2 py-1 cursor-pointer text-xs">
                        <input type="checkbox" className="w-auto"
                               checked={enabledToolIds.has(t.id)}
                               disabled={isDockerCli}
                               onChange={() => !isDockerCli && toggleTool(t.id)} />
                        <span className="font-mono">{t.id}</span>
                      </label>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="card">
            <h2 className="text-base font-semibold mb-3">Skills ({a.skill_slugs.length} selected)</h2>
            <div className="grid grid-cols-2 gap-1">
              {skills.map(sk => (
                <label key={sk.slug} className="flex items-center gap-2 py-1 cursor-pointer text-xs">
                  <input type="checkbox" className="w-auto"
                         checked={enabledSkills.has(sk.slug)}
                         onChange={() => toggleSkill(sk.slug)} />
                  <span>{sk.slug}</span>
                </label>
              ))}
            </div>
          </div>
        </div>

        {!isNew && (
          <div className="card space-y-2">
            <h2 className="text-base font-semibold mb-1">Quick test</h2>
            <textarea rows={3} value={runInput} onChange={e => setRunInput(e.target.value)}
                      placeholder="prompt..." data-testid="agent-input" />
            <button className="btn btn-primary w-full justify-center" onClick={run} data-testid="agent-run">
              run ▸
            </button>
            {runStatus && <div className="text-xs text-muted">status: <span className="badge badge-info">{runStatus}</span></div>}
            {runOutput && <pre className="codebox" data-testid="agent-output">{runOutput}</pre>}
          </div>
        )}
      </div>
    </Page>
  );
}
