import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Background, Controls, ReactFlow, ReactFlowProvider,
  addEdge, useNodesState, useEdgesState,
  type Connection, type Node, type Edge, type ReactFlowInstance,
} from "@xyflow/react";
import Page from "../components/Page";
import { FormRow } from "../components/Modal";
import { api, type Agent, type AgentFlow, type AgentFlowNode, type AgentGroup } from "../lib/api";

const SOURCE_ID = "source";

const NODE_ICON: Record<AgentFlowNode["type"], string> = { source: "📡", agent: "🤖", group: "👥" };

function toRfNode(n: AgentFlowNode): Node {
  const label = n.type === "source" ? (n.label || "Source")
    : n.type === "group" ? (n.label || n.group_slug)
    : (n.label || n.agent_slug);
  return {
    id: n.id,
    position: n.position,
    className: n.type,
    data: {
      label: `${NODE_ICON[n.type]} ${label}`,
      flowType: n.type,
      agentSlug: n.agent_slug,
      groupSlug: n.group_slug,
      rawLabel: n.label,
    },
  };
}

function toRfEdge(e: { id: string; source: string; target: string }): Edge {
  return { id: e.id, source: e.source, target: e.target, animated: true };
}

function AgentFlowCanvas() {
  const { slug } = useParams<{ slug: string }>();
  const isNew = slug === "new";
  const nav = useNavigate();

  const [agents, setAgents] = useState<Agent[]>([]);
  const [groups, setGroups] = useState<AgentGroup[]>([]);
  const [paletteSearch, setPaletteSearch] = useState("");
  const [name, setName] = useState(isNew ? "" : "");
  const [description, setDescription] = useState("");
  const [editedSlug, setEditedSlug] = useState(isNew ? "" : "");
  const [enabled, setEnabled] = useState(false);
  const [maxHops, setMaxHops] = useState<string>("");
  const [budgetTokens, setBudgetTokens] = useState<string>("");
  const [budgetUsd, setBudgetUsd] = useState<string>("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);

  const wrapperRef = useRef<HTMLDivElement>(null);
  const [rfInstance, setRfInstance] = useState<ReactFlowInstance | null>(null);

  useEffect(() => {
    api.listAgents().then(setAgents).catch(() => {});
    api.listAgentGroups().then(setGroups).catch(() => {});
  }, []);

  useEffect(() => {
    if (isNew) {
      setNodes([toRfNode({ id: SOURCE_ID, type: "source", label: "Source", position: { x: 40, y: 200 } })]);
      return;
    }
    if (!slug) return;
    api.getAgentFlow(slug).then((f: AgentFlow) => {
      setName(f.name);
      setDescription(f.description);
      setEditedSlug(f.slug);
      setEnabled(f.enabled);
      setMaxHops(f.max_hops != null ? String(f.max_hops) : "");
      setBudgetTokens(f.budget_tokens != null ? String(f.budget_tokens) : "");
      setBudgetUsd(f.budget_usd != null ? String(f.budget_usd) : "");
      setNodes((f.graph?.nodes || []).map(toRfNode));
      setEdges((f.graph?.edges || []).map(toRfEdge));
    }).catch(e => setError(String(e.message || e)));
  }, [slug, isNew]);

  const onConnect = useCallback((c: Connection) => {
    setEdges(es => addEdge({ ...c, id: `e-${c.source}-${c.target}-${Date.now()}`, animated: true }, es));
  }, [setEdges]);

  const onDragStart = (e: DragEvent, payload: { kind: AgentFlowNode["type"]; agentSlug?: string; groupSlug?: string; label: string }) => {
    e.dataTransfer.setData("application/agents-flow", JSON.stringify(payload));
    e.dataTransfer.effectAllowed = "move";
  };

  const onDragOver = useCallback((e: DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
  }, []);

  const onDrop = useCallback((e: DragEvent) => {
    e.preventDefault();
    const raw = e.dataTransfer.getData("application/agents-flow");
    if (!raw || !rfInstance) return;
    const payload = JSON.parse(raw) as { kind: AgentFlowNode["type"]; agentSlug?: string; groupSlug?: string; label: string };
    if (payload.kind === "source" && nodes.some(n => n.className === "source")) {
      setError("Only one Source node is allowed per flow.");
      return;
    }
    const position = rfInstance.screenToFlowPosition({ x: e.clientX, y: e.clientY });
    const id = payload.kind === "source" ? SOURCE_ID
      : payload.kind === "group" ? `group-${payload.groupSlug}-${Date.now()}`
      : `agent-${payload.agentSlug}-${Date.now()}`;
    const node: AgentFlowNode = {
      id, type: payload.kind, agent_slug: payload.agentSlug, group_slug: payload.groupSlug,
      label: payload.label, position,
    };
    setNodes(ns => [...ns, toRfNode(node)]);
    setError("");
  }, [rfInstance, nodes, setNodes]);

  const onDeleteSelected = useCallback(() => {
    setNodes(ns => ns.filter(n => !n.selected));
    setEdges(es => es.filter(e => !e.selected));
  }, [setNodes, setEdges]);

  const filteredAgents = useMemo(() => {
    const q = paletteSearch.trim().toLowerCase();
    if (!q) return agents;
    return agents.filter(a =>
      a.name.toLowerCase().includes(q) ||
      a.slug.toLowerCase().includes(q) ||
      a.description?.toLowerCase().includes(q));
  }, [agents, paletteSearch]);

  const filteredGroups = useMemo(() => {
    const q = paletteSearch.trim().toLowerCase();
    if (!q) return groups;
    return groups.filter(g =>
      g.name.toLowerCase().includes(q) ||
      g.slug.toLowerCase().includes(q) ||
      g.description?.toLowerCase().includes(q));
  }, [groups, paletteSearch]);

  const graph = useMemo(() => ({
    nodes: nodes.map(n => ({
      id: n.id,
      type: (n.data as any).flowType as AgentFlowNode["type"],
      agent_slug: (n.data as any).agentSlug,
      group_slug: (n.data as any).groupSlug,
      label: (n.data as any).rawLabel ?? "",
      position: n.position,
    })),
    edges: edges.map(e => ({ id: e.id, source: e.source, target: e.target })),
  }), [nodes, edges]);

  const onSave = async () => {
    setSaving(true); setError("");
    try {
      if (!name.trim()) throw new Error("name is required");
      const max_hops = maxHops.trim() ? Number(maxHops) : null;
      const budget_tokens = budgetTokens.trim() ? Number(budgetTokens) : null;
      const budget_usd = budgetUsd.trim() ? Number(budgetUsd) : null;
      if (isNew) {
        const created = await api.createAgentFlow({
          slug: editedSlug || undefined, name, description, enabled, graph,
          max_hops, budget_tokens, budget_usd,
        });
        nav(`/agents-flow/${created.slug}`);
      } else if (slug) {
        const targetSlug = editedSlug.trim() || slug;
        if (targetSlug !== slug) {
          await api.renameAgentFlow(slug, targetSlug);
        }
        await api.saveAgentFlow(targetSlug, {
          name, description, enabled, graph, max_hops, budget_tokens, budget_usd,
        });
        if (targetSlug !== slug) nav(`/agents-flow/${targetSlug}`);
      }
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Page title={isNew ? "New Agents Flow" : name || slug}
          subtitle="Drag Source and agents onto the canvas, connect them to define who can hand off to whom."
          actions={
            <>
              <button className="btn" onClick={() => nav("/agents-flow")} data-testid="agent-flow-back">back</button>
              <button
                className={`btn ${enabled ? "btn-primary" : ""}`}
                onClick={() => setEnabled(v => !v)}
                title="When enabled, agents in this flow get the aw-agents-flow skill and their connected-agents context injected at dispatch time."
                data-testid="agent-flow-enabled-toggle">
                {enabled ? "● enabled" : "○ disabled"}
              </button>
              <button className="btn btn-primary" onClick={onSave} disabled={saving} data-testid="agent-flow-save">
                {saving ? "saving..." : (isNew ? "create" : "save")}
              </button>
            </>
          }>
      {error && <div className="codebox text-err mb-3">{error}</div>}
      <div className="grid grid-cols-2 gap-4 mb-4">
        <FormRow label="name">
          <input value={name} onChange={e => setName(e.target.value)} data-testid="agent-flow-name" />
        </FormRow>
        <FormRow label="slug" hint={isNew ? "leave blank to auto-generate" : "editable — renames the flow"}>
          <input value={editedSlug} onChange={e => setEditedSlug(e.target.value)}
                 className="font-mono" data-testid="agent-flow-slug" />
        </FormRow>
        <FormRow label="description">
          <input value={description} onChange={e => setDescription(e.target.value)} data-testid="agent-flow-description" />
        </FormRow>
        <FormRow label="max hops" hint="loop guard override for this flow — blank falls back to the global agent_chain_max_hops setting">
          <input type="number" min={1} value={maxHops} onChange={e => setMaxHops(e.target.value)}
                 placeholder="global default" data-testid="agent-flow-max-hops" />
        </FormRow>
        <FormRow label="token budget" hint="total tokens across every run in this flow — reaching it escalates to Need Human instead of continuing">
          <input type="number" min={0} value={budgetTokens} onChange={e => setBudgetTokens(e.target.value)}
                 placeholder="no cap" data-testid="agent-flow-budget-tokens" />
        </FormRow>
        <FormRow label="cost budget (USD)" hint="total cost across every run in this flow — reaching it escalates to Need Human instead of continuing">
          <input type="number" min={0} step="0.01" value={budgetUsd} onChange={e => setBudgetUsd(e.target.value)}
                 placeholder="no cap" data-testid="agent-flow-budget-usd" />
        </FormRow>
      </div>

      <div className="grid grid-cols-[220px_1fr] gap-4">
        <div className="card">
          <h2 className="text-sm font-semibold mb-2">Palette</h2>
          <div className="text-xs text-muted mb-2">Drag onto the canvas.</div>
          <input
            value={paletteSearch}
            onChange={e => setPaletteSearch(e.target.value)}
            placeholder="Search agents & groups…"
            className="mb-3"
            data-testid="agent-flow-palette-search" />
          <div
            className="border border-dashed border-line rounded px-3 py-2 mb-3 cursor-grab text-sm"
            draggable
            onDragStart={e => onDragStart(e, { kind: "source", label: "Source" })}
            data-testid="agent-flow-palette-source">
            📡 Source <span className="text-xs text-muted">(origin channel)</span>
          </div>
          {filteredGroups.length > 0 && (
            <div className="mb-3">
              <div className="text-xs text-muted mb-1">Groups</div>
              <div className="flex flex-col gap-2">
                {filteredGroups.map(g => (
                  <div key={g.slug}
                       className="border border-line rounded px-3 py-2 cursor-grab text-sm hover:border-accent"
                       draggable
                       onDragStart={e => onDragStart(e, { kind: "group", groupSlug: g.slug, label: g.name })}
                       data-testid={`agent-flow-palette-group-${g.slug}`}>
                    👥 {g.name}
                    <div className="text-xs font-mono text-muted">{g.slug}</div>
                  </div>
                ))}
              </div>
            </div>
          )}
          <div className="max-h-[380px] overflow-y-auto flex flex-col gap-2">
            {filteredAgents.map(a => (
              <div key={a.slug}
                   className="border border-line rounded px-3 py-2 cursor-grab text-sm hover:border-accent"
                   draggable
                   onDragStart={e => onDragStart(e, { kind: "agent", agentSlug: a.slug, label: a.name })}
                   data-testid={`agent-flow-palette-${a.slug}`}>
                🤖 {a.name}
                <div className="text-xs font-mono text-muted">{a.slug}</div>
              </div>
            ))}
            {filteredAgents.length === 0 && (
              <div className="text-xs text-muted">No agents match "{paletteSearch}".</div>
            )}
          </div>
          <button className="btn btn-danger text-xs mt-3 w-full" onClick={onDeleteSelected}
                  data-testid="agent-flow-delete-selected">
            delete selected
          </button>
        </div>

        <div ref={wrapperRef} className="card p-0 overflow-hidden" style={{ height: 560 }}
             data-testid="agent-flow-canvas">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onInit={setRfInstance}
            onDrop={onDrop}
            onDragOver={onDragOver}
            fitView
            proOptions={{ hideAttribution: true }}>
            <Background />
            <Controls />
          </ReactFlow>
        </div>
      </div>
    </Page>
  );
}

export default function AgentFlowEdit() {
  return (
    <ReactFlowProvider>
      <AgentFlowCanvas />
    </ReactFlowProvider>
  );
}
