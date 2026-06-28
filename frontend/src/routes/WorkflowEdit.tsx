import { useEffect, useMemo, useState, useCallback } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  Background, Controls, ReactFlow,
  applyNodeChanges, applyEdgeChanges,
  type NodeChange, type EdgeChange,
} from "@xyflow/react";
import Page, { StatusBadge } from "../components/Page";
import { FormRow } from "../components/Modal";
import { api, streamRun, type Workflow as WorkflowT, type Agent } from "../lib/api";
import { graphToReactFlow, type NodeStatus } from "../lib/graphFlow";
type Workflow = WorkflowT;
type Mode = "run" | "edit";

// Topology = graph shape. Concurrency is a separate flag (only meaningful
// for the "nodes" topology). Kind is derived on the server from these.
type Topology = "nodes" | "stages" | "orchestrator_worker" | "group_chat";

const TOPOLOGY_LABEL: Record<Topology, string> = {
  nodes:               "Nodes (sequential or parallel)",
  stages:              "Pipeline (stages, output of one → input of next)",
  orchestrator_worker: "Orchestrator → Workers → Synthesizer (fan-out)",
  group_chat:          "Group chat (round-robin debate)",
};

const TOPOLOGY_TEMPLATES: Record<Topology, any> = {
  nodes: {
    concurrency: "sequential",
    nodes: [
      { id: "s1", agent: "coder", label: "Step 1", input_template: "{input}" },
      { id: "s2", agent: "reviewer", label: "Step 2", input_template: "Review the output below:\n{prev}" },
    ],
  },
  stages: {
    stages: [
      { id: "p1", agent: "planner", label: "Plan", input_template: "{input}" },
      { id: "p2", agent: "coder",   label: "Build", input_template: "Plan:\n{prev}" },
    ],
  },
  orchestrator_worker: {
    orchestrator: { id: "plan", agent: "planner", label: "Planner", input_template: "{input}" },
    workers: [
      { id: "w1", agent: "coder", label: "Worker 1", input_template: "{input}\n\nPLAN:\n{prev}" },
      { id: "w2", agent: "coder", label: "Worker 2", input_template: "{input}\n\nPLAN:\n{prev}" },
    ],
    synthesizer: { id: "synth", agent: "reviewer", label: "Synthesizer", input_template: "{prev}" },
  },
  group_chat: {
    participants: [
      { id: "p1", agent: "planner", label: "Planner" },
      { id: "p2", agent: "reviewer", label: "Critic" },
    ],
    max_turns: 3,
  },
};

function topologyOf(graph: any): Topology {
  if (!graph || typeof graph !== "object") return "nodes";
  if (Array.isArray(graph.stages)) return "stages";
  if (graph.orchestrator && Array.isArray(graph.workers)) return "orchestrator_worker";
  if (Array.isArray(graph.participants)) return "group_chat";
  return "nodes";
}

export default function WorkflowEdit() {
  const { slug } = useParams<{ slug: string }>();
  const isNew = slug === "new";
  const nav = useNavigate();

  const [wf, setWf] = useState<Workflow | null>(null);
  const [mode, setMode] = useState<Mode>(isNew ? "edit" : "run");
  const [nodeStatus, setNodeStatus] = useState<Record<string, NodeStatus>>({});
  const [nodeTokens, setNodeTokens] = useState<Record<string, number>>({});
  const [runId, setRunId] = useState<string | null>(null);
  const [runState, setRunState] = useState<string>("idle");
  const [output, setOutput] = useState<any>(null);
  const [input, setInput] = useState("say hi");

  type BudgetSnap = { hops?: { count: number; limit: number };
                      tokens?: { count: number; limit: number } } | null;
  const [budgetSnap, setBudgetSnap] = useState<BudgetSnap>(null);
  const [limitReached, setLimitReached] = useState<string | null>(null);

  const [editName, setEditName] = useState("");
  const [editSlug, setEditSlug] = useState("");
  const [slugLocked, setSlugLocked] = useState(false);
  const [editDescription, setEditDescription] = useState("");
  const [editTopology, setEditTopology] = useState<Topology>("nodes");
  const [editGraphText, setEditGraphText] = useState("{}");
  const [editError, setEditError] = useState<string>("");
  const [saving, setSaving] = useState(false);

  const [agents, setAgents] = useState<Agent[]>([]);
  const [otherWorkflows, setOtherWorkflows] = useState<Workflow[]>([]);
  const [resettableSet, setResettableSet] = useState<Set<string>>(new Set());
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [editorMode, setEditorMode] = useState<"visual" | "json">("visual");

  useEffect(() => {
    api.listAgents().then(setAgents);
    api.listWorkflows().then(setOtherWorkflows);
    api.listResettableWorkflows().then(r => setResettableSet(new Set(r))).catch(() => {});
  }, []);

  useEffect(() => {
    if (!slug) return;
    setMode(isNew ? "edit" : "run");
    if (isNew) {
      setWf({ slug: "", name: "", description: "", kind: "sequential",
              graph: TOPOLOGY_TEMPLATES.nodes } as any);
      setEditName(""); setEditDescription("");
      api.generateSlug("workflow").then(r => setEditSlug(r.slug)).catch(() => {});
      setEditTopology("nodes");
      setEditGraphText(JSON.stringify(TOPOLOGY_TEMPLATES.nodes, null, 2));
      return;
    }
    api.getWorkflow(slug).then(w => {
      setWf(w);
      setEditName(w.name); setEditSlug(w.slug); setEditDescription(w.description);
      setEditTopology(topologyOf(w.graph));
      setEditGraphText(JSON.stringify(w.graph, null, 2));
    });
  }, [slug, isNew]);

  // Current graph (parsed) — single source of truth in edit visual mode
  const currentGraph = useMemo(() => {
    try { return JSON.parse(editGraphText); } catch { return {}; }
  }, [editGraphText]);

  // Helper: enumerate the "node-like" objects in any graph shape with their path
  function listNodeRefs(kind: string, g: any): { id: string; path: any[]; node: any }[] {
    const out: { id: string; path: any[]; node: any }[] = [];
    if (kind === "sequential" || kind === "parallel") {
      (g.nodes || []).forEach((n: any, i: number) => out.push({ id: n.id, path: ["nodes", i], node: n }));
    } else if (kind === "pipeline") {
      (g.stages || []).forEach((n: any, i: number) => out.push({ id: n.id, path: ["stages", i], node: n }));
    } else if (kind === "orchestrator_worker") {
      if (g.orchestrator) out.push({ id: g.orchestrator.id, path: ["orchestrator"], node: g.orchestrator });
      (g.workers || []).forEach((n: any, i: number) => out.push({ id: n.id, path: ["workers", i], node: n }));
      if (g.synthesizer) out.push({ id: g.synthesizer.id, path: ["synthesizer"], node: g.synthesizer });
    } else if (kind === "group_chat") {
      (g.participants || []).forEach((n: any, i: number) => out.push({ id: n.id, path: ["participants", i], node: n }));
    }
    return out;
  }

  function setNodeAt(path: any[], updater: (n: any) => any) {
    setEditGraphText(prev => {
      let g: any;
      try { g = JSON.parse(prev); } catch { return prev; }
      // walk to parent
      const last = path[path.length - 1];
      const parent = path.slice(0, -1).reduce((o, k) => o[k], g);
      parent[last] = updater(parent[last]);
      return JSON.stringify(g, null, 2);
    });
  }

  function addNode() {
    setEditGraphText(prev => {
      let g: any;
      try { g = JSON.parse(prev); } catch { return prev; }
      const newId = `n${Date.now() % 100000}`;
      const newNode = { id: newId, agent: agents[0]?.slug || "coder",
                        label: `Node ${newId}`, input_template: "{input}" };
      if (editTopology === "nodes") {
        g.nodes = (g.nodes || []).concat(newNode);
      } else if (editTopology === "stages") {
        g.stages = (g.stages || []).concat(newNode);
      } else if (editTopology === "orchestrator_worker") {
        g.workers = (g.workers || []).concat({ ...newNode, label: `Worker ${(g.workers||[]).length + 1}` });
      } else if (editTopology === "group_chat") {
        g.participants = (g.participants || []).concat({ id: newId, agent: agents[0]?.slug || "coder", label: `Participant` });
      }
      return JSON.stringify(g, null, 2);
    });
  }

  function removeNode(id: string) {
    setEditGraphText(prev => {
      let g: any;
      try { g = JSON.parse(prev); } catch { return prev; }
      for (const arrKey of ["nodes", "stages", "workers", "participants"]) {
        if (Array.isArray(g[arrKey])) {
          g[arrKey] = g[arrKey].filter((n: any) => n.id !== id);
        }
      }
      // orchestrator_worker can't lose orchestrator/synthesizer via this path
      return JSON.stringify(g, null, 2);
    });
    if (selectedNodeId === id) setSelectedNodeId(null);
  }

  const effectiveKind = computeKind(editTopology, currentGraph);
  const { nodes: rfNodes, edges: rfEdges } = useMemo(
    () => graphToReactFlow(effectiveKind, currentGraph, nodeStatus, nodeTokens, selectedNodeId),
    [effectiveKind, currentGraph, nodeStatus, nodeTokens, selectedNodeId]
  );

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    // We don't persist position back to the graph JSON (positions are
    // computed by topology), so only react to selection.
    applyNodeChanges(changes, rfNodes);
  }, [rfNodes]);
  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    applyEdgeChanges(changes, rfEdges);
  }, [rfEdges]);

  const onRun = useCallback(async () => {
    if (!slug || isNew) return;
    setNodeStatus({}); setNodeTokens({}); setOutput(null); setRunState("running");
    setBudgetSnap(null); setLimitReached(null);
    const { run_id } = await api.runWorkflow(slug, input);
    setRunId(run_id);
    const stop = streamRun(run_id, (evt) => {
      const node = evt.node_id || "";
      // Live budget snapshot — node_start/node_end carry `budget`,
      // and `done` carries both `budget` and `limit_reached`.
      const b = evt.payload?.budget;
      if (b && typeof b === "object") {
        setBudgetSnap(b);
      }
      if (evt.payload?.limit_reached) {
        setLimitReached(String(evt.payload.limit_reached));
      }
      if (evt.kind === "node_start" && node) {
        setNodeStatus(s => ({ ...s, [node]: "running" }));
      } else if (evt.kind === "node_end" && node && node !== "__workflow__") {
        setNodeStatus(s => ({ ...s, [node]: "done" }));
        if (evt.payload?.tokens_out) {
          setNodeTokens(t => ({ ...t, [node]: evt.payload.tokens_out }));
        }
      } else if (evt.kind === "error") {
        if (node) setNodeStatus(s => ({ ...s, [node]: "error" }));
      } else if (evt.kind === "done") {
        api.getRun(run_id).then(r => {
          setOutput(r.output);
          setRunState(r.status);
          // Output payload may also carry the cap reason (orchestrators stamp
          // it; surface it even if the `done` event missed it).
          const lr = (r.output as any)?.limit_reached;
          if (lr && !limitReached) setLimitReached(String(lr));
        });
        stop();
      }
    });
  }, [slug, isNew, input]);

  const onTopologyChange = (t: Topology) => {
    setEditTopology(t);
    if (TOPOLOGY_TEMPLATES[t]) setEditGraphText(JSON.stringify(TOPOLOGY_TEMPLATES[t], null, 2));
    setSelectedNodeId(null);
  };

  const toggleConcurrency = (parallel: boolean) => {
    try {
      const g = JSON.parse(editGraphText);
      g.concurrency = parallel ? "parallel" : "sequential";
      setEditGraphText(JSON.stringify(g, null, 2));
    } catch { /* ignore JSON parse error during edit */ }
  };

  function setBudgetField(key: "max_hops" | "max_tokens", val: string,
                          clamp: { min: number; max: number }) {
    try {
      const g = JSON.parse(editGraphText);
      const trimmed = (val ?? "").trim();
      if (!trimmed) {
        delete g[key];
      } else {
        const n = Math.max(clamp.min, Math.min(clamp.max, parseInt(trimmed, 10) || 0));
        if (!n) delete g[key];
        else g[key] = n;
      }
      setEditGraphText(JSON.stringify(g, null, 2));
    } catch { /* ignore */ }
  }
  const setMaxHops   = (v: string) => setBudgetField("max_hops",   v, { min: 1,  max: 10000 });
  const setMaxTokens = (v: string) => setBudgetField("max_tokens", v, { min: 0,  max: 10_000_000 });
  const currentMaxHops: number = (() => {
    try { return Number(JSON.parse(editGraphText)?.max_hops) || 0; }
    catch { return 0; }
  })();
  const currentMaxTokens: number = (() => {
    try { return Number(JSON.parse(editGraphText)?.max_tokens) || 0; }
    catch { return 0; }
  })();

  const onSave = async () => {
    setSaving(true); setEditError("");
    try {
      const graph = JSON.parse(editGraphText);
      const kind = computeKind(editTopology, graph);
      const shapeError = validateGraphShape(kind, graph);
      if (shapeError) throw new Error(shapeError);
      const payload = { name: editName, description: editDescription, kind, graph };
      if (isNew) {
        const created = await api.createWorkflow({ slug: editSlug, ...payload });
        nav(`/workflows/${created.slug}`);
      } else if (slug) {
        const targetSlug = editSlug.trim() || slug;
        if (targetSlug !== slug) {
          await api.renameWorkflow(slug, targetSlug);
        }
        const updated = await api.saveWorkflow(targetSlug, payload);
        setWf(updated);
        if (targetSlug !== slug) nav(`/workflows/${targetSlug}`);
        else setMode("run");
      }
    } catch (e: any) {
      setEditError(String(e.message || e));
    } finally {
      setSaving(false);
    }
  };

  const onDelete = async () => {
    if (!slug || isNew) return;
    if (!confirm(`Delete workflow "${slug}"?`)) return;
    await api.deleteWorkflow(slug);
    nav("/workflows");
  };

  const onExport = async () => {
    if (!slug || isNew) return;
    const spec = await api.exportWorkflow(slug);
    const blob = new Blob([JSON.stringify(spec, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `${slug}.workflow.json`; a.click();
    URL.revokeObjectURL(url);
  };

  if (!wf) return <Page title="Workflow">…loading…</Page>;

  const headerTitle = isNew ? "New workflow" : wf.name;
  const selectedNodeInfo = selectedNodeId
    ? listNodeRefs(effectiveKind, currentGraph).find(n => n.id === selectedNodeId)
    : null;

  return (
    <Page title={headerTitle}
          subtitle={isNew ? "Define the orchestration kind and graph below." :
                   `${wf.kind} • ${wf.description}`}
          actions={<>
            <Link to="/workflows" className="btn">← back</Link>
            {!isNew && (
              <div className="flex border border-line rounded overflow-hidden">
                <button className={`btn ${mode === "run" ? "btn-primary" : ""}`}
                        onClick={() => setMode("run")} data-testid="workflow-tab-run">run</button>
                <button className={`btn ${mode === "edit" ? "btn-primary" : ""}`}
                        onClick={() => setMode("edit")} data-testid="workflow-tab-edit">edit</button>
              </div>
            )}
            {runId && mode === "run" && <Link to={`/runs/${runId}`} className="btn">view run</Link>}
            {!isNew && (
              <button className="btn" onClick={onExport} data-testid="workflow-export">export</button>
            )}
            {!isNew && slug && resettableSet.has(slug) && mode === "edit" && (
              <button className="btn" data-testid="workflow-reset"
                      onClick={async () => {
                        if (!confirm(`Reset "${slug}" to seed defaults? Your edits will be lost.`)) return;
                        const next = await api.resetWorkflow(slug);
                        setWf(next);
                        setEditName(next.name); setEditDescription(next.description);
                        setEditTopology(topologyOf(next.graph));
                        setEditGraphText(JSON.stringify(next.graph, null, 2));
                      }}>reset to default</button>
            )}
            {!isNew && mode === "edit" && (
              <button className="btn btn-danger" onClick={onDelete} data-testid="workflow-delete">
                delete
              </button>
            )}
          </>}>

      {mode === "run" && !isNew && (
        <>
          <div className="text-xs font-mono text-muted mb-3">{wf.slug}</div>
          <div className="grid grid-cols-3 gap-4">
            <div className="col-span-2 card p-0 overflow-hidden" style={{ height: 480 }} data-testid="workflow-graph">
              <ReactFlow nodes={rfNodes} edges={rfEdges} fitView proOptions={{ hideAttribution: true }}>
                <Background gap={20} size={1} color="#21262d" />
                <Controls showInteractive={false} />
              </ReactFlow>
            </div>
            <div className="card space-y-3">
              <h2 className="text-base font-semibold">Run</h2>
              <textarea rows={4} value={input} onChange={e => setInput(e.target.value)}
                        data-testid="workflow-input" placeholder="input..." />
              <button className="btn btn-primary w-full justify-center" onClick={onRun}
                      data-testid="workflow-run">
                run workflow ▸
              </button>
              {runState === "running" && runId && (
                <button className="btn btn-danger w-full justify-center"
                        data-testid="workflow-cancel"
                        onClick={async () => {
                          await api.cancelRun(runId);
                          setRunState("cancelled");
                        }}>
                  cancel ▪
                </button>
              )}
              <div className="text-xs text-muted">
                status: <StatusBadge status={runState} />
                {runId && <span className="ml-2 font-mono">{runId.slice(0, 8)}</span>}
              </div>
              {budgetSnap && (
                <div className="text-xs space-y-1" data-testid="workflow-budget">
                  {budgetSnap.hops && (
                    <div>
                      <span className="text-muted">hops:</span>{" "}
                      <span className={limitReached === "hops" ? "text-yellow-400 font-semibold" : "text-fg"}>
                        {budgetSnap.hops.count}
                      </span>
                      <span className="text-muted"> / {budgetSnap.hops.limit}</span>
                    </div>
                  )}
                  {budgetSnap.tokens && budgetSnap.tokens.limit > 0 && (
                    <div>
                      <span className="text-muted">tokens:</span>{" "}
                      <span className={limitReached === "tokens" ? "text-yellow-400 font-semibold" : "text-fg"}>
                        {budgetSnap.tokens.count}
                      </span>
                      <span className="text-muted"> / {budgetSnap.tokens.limit}</span>
                    </div>
                  )}
                  {budgetSnap.tokens && budgetSnap.tokens.limit === 0 && (
                    <div>
                      <span className="text-muted">tokens:</span>{" "}
                      <span className="text-fg">{budgetSnap.tokens.count}</span>
                      <span className="text-muted"> / unlimited</span>
                    </div>
                  )}
                </div>
              )}
              {limitReached && (
                <div className="text-xs bg-yellow-500/10 border border-yellow-500/40 text-yellow-300 rounded px-2 py-1"
                     data-testid="workflow-limit-reached">
                  ⚠ stopped gracefully — <span className="font-semibold">{limitReached}</span> limit reached.
                  Partial output below.
                </div>
              )}
              {output && (
                <pre className="codebox max-h-64 overflow-auto" data-testid="workflow-output">
                  {typeof output === "string" ? output : JSON.stringify(output, null, 2)}
                </pre>
              )}
            </div>
          </div>
        </>
      )}

      {(mode === "edit" || isNew) && (
        <>
          <div className="card mb-4" data-testid="workflow-editor">
            <h2 className="text-base font-semibold mb-3">
              {isNew ? "Create workflow" : "Edit workflow"}
            </h2>
            {editError && <div className="codebox text-err mb-3">{editError}</div>}
            <div className="grid grid-cols-2 gap-4 mb-3">
              <FormRow label="name">
                <input value={editName}
                       onChange={e => {
                         const name = e.target.value;
                         setEditName(name);
                         if (isNew && !slugLocked) {
                           api.generateSlug("workflow", name).then(r => setEditSlug(r.slug)).catch(() => {});
                         }
                       }}
                       data-testid="workflow-edit-name" />
              </FormRow>
              <FormRow label="slug" hint={isNew ? "auto-generated from name, editable" : "editable — rename updates all linked runs"}>
                <input value={editSlug}
                       onChange={e => { setSlugLocked(true); setEditSlug(e.target.value); }}
                       data-testid="workflow-edit-slug" className="font-mono" />
              </FormRow>
            </div>
            <FormRow label="description">
              <input value={editDescription} onChange={e => setEditDescription(e.target.value)}
                     data-testid="workflow-edit-description" />
            </FormRow>
            <FormRow label="topology" hint="changes the graph shape — picking a new topology resets the graph below to a starter template">
              <select value={editTopology}
                      onChange={e => onTopologyChange(e.target.value as Topology)}
                      data-testid="workflow-edit-topology">
                {(Object.keys(TOPOLOGY_LABEL) as Topology[]).map(t => (
                  <option key={t} value={t}>{TOPOLOGY_LABEL[t]}</option>
                ))}
              </select>
            </FormRow>
            {editTopology === "nodes" && (
              <FormRow label="concurrency" hint="run nodes sequentially (each sees the previous one's output) or in parallel (all see the user prompt)">
                <label className="inline-flex items-center gap-2">
                  <input type="checkbox"
                         className="w-auto"
                         data-testid="workflow-edit-concurrency"
                         checked={(() => { try { return JSON.parse(editGraphText)?.concurrency === "parallel"; } catch { return false; } })()}
                         onChange={e => toggleConcurrency(e.target.checked)} />
                  <span className="text-sm">run in parallel</span>
                </label>
              </FormRow>
            )}

            <FormRow label="budget (safety caps)"
                     hint="Apply BEFORE each next execution. Either cap being reached stops the workflow gracefully (status=success, output.limit_reached='hops' or 'tokens', partial output preserved). Sub-workflow nodes count against the same shared budget.">
              <div className="flex items-center gap-3 flex-wrap">
                <label className="inline-flex items-center gap-2 text-xs">
                  <span className="text-muted">max hops</span>
                  <input type="number" min={1} max={10000}
                         className="w-28"
                         placeholder="50 (default)"
                         value={currentMaxHops || ""}
                         onChange={e => setMaxHops(e.target.value)}
                         data-testid="workflow-edit-max-hops" />
                </label>
                <label className="inline-flex items-center gap-2 text-xs">
                  <span className="text-muted">max tokens</span>
                  <input type="number" min={0} max={10000000}
                         className="w-32"
                         placeholder="unlimited"
                         value={currentMaxTokens || ""}
                         onChange={e => setMaxTokens(e.target.value)}
                         data-testid="workflow-edit-max-tokens" />
                </label>
                <span className="text-xs text-muted">
                  one hop = one node execution. tokens accumulate in+out across the run tree.
                </span>
              </div>
            </FormRow>

            <div className="flex items-center justify-between mt-4 mb-2">
              <div className="text-xs text-muted uppercase">graph</div>
              <div className="flex border border-line rounded overflow-hidden">
                <button className={`btn ${editorMode === "visual" ? "btn-primary" : ""}`}
                        onClick={() => setEditorMode("visual")}
                        data-testid="workflow-editor-visual">visual</button>
                <button className={`btn ${editorMode === "json" ? "btn-primary" : ""}`}
                        onClick={() => setEditorMode("json")}
                        data-testid="workflow-editor-json">json</button>
              </div>
            </div>

            {editorMode === "visual" ? (
              <div className="grid grid-cols-3 gap-3">
                <div className="col-span-2 card p-0 overflow-hidden" style={{ height: 360 }}
                     data-testid="workflow-canvas">
                  <ReactFlow
                    nodes={rfNodes}
                    edges={rfEdges}
                    onNodesChange={onNodesChange}
                    onEdgesChange={onEdgesChange}
                    onNodeClick={(_, n) => setSelectedNodeId(n.id)}
                    fitView
                    proOptions={{ hideAttribution: true }}
                  >
                    <Background gap={20} size={1} color="#21262d" />
                    <Controls showInteractive={false} />
                  </ReactFlow>
                </div>
                <div className="card" data-testid="workflow-node-panel">
                  {selectedNodeInfo ? (
                    <div className="space-y-2">
                      <div className="text-xs text-muted uppercase">node {selectedNodeInfo.id}</div>
                      <FormRow label="agent or sub-workflow"
                               hint="Pick an agent to run a single LLM step, or a workflow (workflow:<slug>) to invoke a whole nested workflow.">
                        <select
                          value={selectedNodeInfo.node.agent || ""}
                          onChange={e => setNodeAt(selectedNodeInfo.path, n => ({ ...n, agent: e.target.value }))}
                          data-testid="node-panel-agent"
                        >
                          <optgroup label="Agents">
                            {agents.map(a => <option key={a.slug} value={a.slug}>{a.slug}</option>)}
                          </optgroup>
                          <optgroup label="Workflows (run as sub-workflow)">
                            {otherWorkflows
                              .filter(w => w.slug !== slug)   // don't allow self-reference here
                              .map(w => (
                                <option key={`wf:${w.slug}`} value={`workflow:${w.slug}`}>
                                  workflow:{w.slug}
                                </option>
                              ))}
                          </optgroup>
                        </select>
                      </FormRow>
                      <FormRow label="label">
                        <input value={selectedNodeInfo.node.label || ""}
                               onChange={e => setNodeAt(selectedNodeInfo.path, n => ({ ...n, label: e.target.value }))}
                               data-testid="node-panel-label" />
                      </FormRow>
                      <FormRow label="input_template" hint="{input} = user prompt; {prev} = previous stage's output">
                        <textarea rows={4} value={selectedNodeInfo.node.input_template || ""}
                                  onChange={e => setNodeAt(selectedNodeInfo.path, n => ({ ...n, input_template: e.target.value }))}
                                  data-testid="node-panel-input-template" />
                      </FormRow>
                      {selectedNodeInfo.path[0] !== "orchestrator" && selectedNodeInfo.path[0] !== "synthesizer" && (
                        <button className="btn btn-danger w-full"
                                onClick={() => removeNode(selectedNodeInfo.id)}
                                data-testid="node-panel-delete">
                          remove node
                        </button>
                      )}
                    </div>
                  ) : (
                    <div className="text-sm text-muted">Click a node on the canvas to edit it.</div>
                  )}
                  <button className="btn w-full mt-3" onClick={addNode}
                          data-testid="workflow-add-node">+ add node</button>
                </div>
              </div>
            ) : (
              <FormRow label="graph (JSON)" hint={"Schema for topology=" + editTopology + ": " + describeShape(computeKind(editTopology, (()=>{try{return JSON.parse(editGraphText);}catch{return {};}})()))}>
                <textarea rows={20} value={editGraphText}
                          onChange={e => setEditGraphText(e.target.value)}
                          data-testid="workflow-edit-graph"
                          className="font-mono text-xs" />
              </FormRow>
            )}

            <div className="flex justify-end gap-2 mt-4">
              {!isNew && <button className="btn" onClick={() => setMode("run")}>cancel</button>}
              <button className="btn btn-primary" onClick={onSave} disabled={saving}
                      data-testid="workflow-edit-save">
                {saving ? "saving..." : (isNew ? "create" : "save")}
              </button>
            </div>
          </div>
        </>
      )}
    </Page>
  );
}

function computeKind(topology: Topology, graph: any): string {
  if (topology === "stages") return "pipeline";
  if (topology === "orchestrator_worker") return "orchestrator_worker";
  if (topology === "group_chat") return "group_chat";
  // nodes — depends on concurrency
  return (graph?.concurrency === "parallel") ? "parallel" : "sequential";
}


function validateGraphShape(kind: string, g: any): string | null {
  if (typeof g !== "object" || g == null) return "graph must be a JSON object";
  const haveList = (arr: any, name: string) => Array.isArray(arr) && arr.length
    ? null : `${name} must be a non-empty array`;
  switch (kind) {
    case "sequential":
    case "parallel":
      return haveList(g.nodes, "nodes");
    case "pipeline":
      return haveList(g.stages, "stages");
    case "orchestrator_worker":
      if (!g.orchestrator || !g.orchestrator.id || !g.orchestrator.agent) return "orchestrator must have id + agent";
      if (haveList(g.workers, "workers")) return haveList(g.workers, "workers");
      if (!g.synthesizer || !g.synthesizer.id) return "synthesizer must have an id";
      return null;
    case "group_chat":
      return haveList(g.participants, "participants");
    default:
      return null;
  }
}

function describeShape(kind: string): string {
  switch (kind) {
    case "sequential": return '{"nodes": [{"id","agent","label","input_template"}]}';
    case "parallel":   return '{"nodes": [{"id","agent","label","input_template"}]}';
    case "pipeline":   return '{"stages": [{"id","agent","label","input_template"}]}';
    case "orchestrator_worker": return '{"orchestrator":{...}, "workers":[...], "synthesizer":{...}}';
    case "group_chat": return '{"participants":[{"id","agent","label"}], "max_turns": 3}';
    default: return "free-form JSON";
  }
}

