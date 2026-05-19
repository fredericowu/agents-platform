/**
 * Shared workflow-graph → ReactFlow renderer.
 *
 * Used by:
 *  - WorkflowEdit (canvas + run-preview)
 *  - RunDetail   (live execution graph above the event timeline)
 *
 * Status semantics:
 *   "idle"    — node not yet visited (pending)
 *   "running" — node currently executing
 *   "done"    — node completed successfully
 *   "error"   — node failed
 *
 * Colors come from `index.css` `.react-flow__node.{running|done|error}` rules.
 */
import type { Edge, Node } from "@xyflow/react";
import { ModelBadge } from "../components/ModelBadge";

export type NodeStatus = "idle" | "running" | "done" | "error";

export function graphToReactFlow(
  kind: string,
  g: any,
  st: Record<string, NodeStatus>,
  tk: Record<string, number>,
  selected: string | null,
  nodeModels: Record<string, string> = {},
): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  const mkNode = (id: string, label: string, x: number, y: number, agent?: string): Node => ({
    id,
    position: { x, y },
    data: {
      label: (
        <div>
          <div className="font-medium text-fg">{label}</div>
          <div className="text-xs text-muted mt-1">
            {agent ? `${agent} · ` : ""}
            {st[id] ?? "idle"}
            {tk[id] ? ` · ${tk[id]} tok` : ""}
          </div>
          {nodeModels[id] && (
            <div className="mt-1">
              <ModelBadge slug={nodeModels[id]} />
            </div>
          )}
        </div>
      ) as any,
    },
    className: (st[id] ?? "idle") + (selected === id ? " ring-2" : ""),
    style: selected === id ? { boxShadow: "0 0 0 3px rgba(88,166,255,0.5)" } : undefined,
  });

  if (kind === "orchestrator_worker" && g.orchestrator) {
    nodes.push(mkNode(g.orchestrator.id, g.orchestrator.label || "Orchestrator", 40, 200, g.orchestrator.agent));
    (g.workers || []).forEach((w: any, i: number) => {
      nodes.push(mkNode(w.id, w.label || w.agent, 280, 80 + i * 120, w.agent));
      edges.push({ id: `e-${g.orchestrator.id}-${w.id}`, source: g.orchestrator.id, target: w.id });
      if (g.synthesizer) edges.push({ id: `e-${w.id}-${g.synthesizer.id}`, source: w.id, target: g.synthesizer.id });
    });
    if (g.synthesizer) nodes.push(mkNode(g.synthesizer.id, g.synthesizer.label || "Synthesizer", 520, 200, g.synthesizer.agent));
  } else if (kind === "pipeline") {
    (g.stages || []).forEach((s: any, i: number) => {
      nodes.push(mkNode(s.id, s.label || s.agent, 40 + i * 180, 200, s.agent));
      if (i > 0) edges.push({ id: `e-${g.stages[i - 1].id}-${s.id}`, source: g.stages[i - 1].id, target: s.id });
    });
  } else if (kind === "sequential") {
    (g.nodes || []).forEach((n: any, i: number) => {
      nodes.push(mkNode(n.id, n.label || n.agent, 40 + i * 200, 200, n.agent));
      if (i > 0) edges.push({ id: `e-${g.nodes[i - 1].id}-${n.id}`, source: g.nodes[i - 1].id, target: n.id });
    });
  } else if (kind === "parallel") {
    (g.nodes || []).forEach((n: any, i: number) => {
      nodes.push(mkNode(n.id, n.label || n.agent, 40 + (i % 3) * 220, 80 + Math.floor(i / 3) * 140, n.agent));
    });
  } else if (kind === "group_chat") {
    (g.participants || []).forEach((p: any, i: number) => {
      const angle = (i / Math.max(1, g.participants.length)) * Math.PI * 2;
      nodes.push(
        mkNode(p.id, p.label || p.agent, 260 + Math.cos(angle) * 180, 200 + Math.sin(angle) * 120, p.agent),
      );
    });
    (g.participants || []).forEach((p: any, i: number) => {
      const next = g.participants[(i + 1) % g.participants.length];
      edges.push({ id: `e-${p.id}-${next.id}`, source: p.id, target: next.id });
    });
  }
  return { nodes, edges };
}

/**
 * Replay a stream of run events to derive per-node status.
 *
 * Rules:
 *   node_start → "running"
 *   node_end   → "done" (preserves "error" if a prior `error` event was set)
 *   error      → "error"
 *
 * Token tallies are collected from `node_end.tokens_out` when present.
 */
export function deriveNodeStateFromEvents(events: { kind: string; node_id: string | null; payload: any }[]) {
  const status: Record<string, NodeStatus> = {};
  const tokens: Record<string, number> = {};
  for (const e of events) {
    const node = e.node_id || "";
    if (!node || node === "__workflow__") continue;
    if (e.kind === "node_start") {
      // don't overwrite a previously errored node
      if (status[node] !== "error") status[node] = "running";
    } else if (e.kind === "node_end") {
      if (status[node] !== "error") status[node] = "done";
      if (e.payload?.tokens_out) tokens[node] = e.payload.tokens_out;
    } else if (e.kind === "error") {
      status[node] = "error";
    }
  }
  return { status, tokens };
}
