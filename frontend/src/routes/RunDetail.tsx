import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Background, Controls, ReactFlow } from "@xyflow/react";
import Page, { StatusBadge } from "../components/Page";
import Modal from "../components/Modal";
import { api, type Run, type RunArtefact, type RunEvent, type RunTree, type RunTreeNode, type Workflow, type RetroScoreSummary, type RetroScoreDimension } from "../lib/api";
import { graphToReactFlow, deriveNodeStateFromEvents } from "../lib/graphFlow";

const KIND_BADGE: Record<string, string> = {
  node_start: "badge-info",
  node_end: "badge-success",
  llm_token: "badge",
  tool_call: "badge-warn",
  tool_result: "badge-success",
  thinking: "badge-info",
  "system.init": "badge-info",
  error: "badge-error",
  "cli.error": "badge-error",
  "cli.timeout": "badge-error",
  log: "badge",
  done: "badge-success",
};

const INITIATOR_ICON: Record<string, string> = {
  agent_run: "🤖",
  workflow_run: "🔀",
  chat: "💬",
  eval: "📏",
  mcp: "🔌",
  cli: "⌨️",
};

export default function RunDetail() {
  const { id } = useParams<{ id: string }>();
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [artefacts, setArtefacts] = useState<RunArtefact[]>([]);
  const [openArtefact, setOpenArtefact] = useState<{ name: string; content: string; mime: string } | null>(null);
  const [tree, setTree] = useState<RunTree | null>(null);
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const [retroScores, setRetroScores] = useState<RetroScoreSummary | null>(null);
  const [retroLoading, setRetroLoading] = useState(false);
  const [retroRecomputing, setRetroRecomputing] = useState(false);
  const [overrideModal, setOverrideModal] = useState<RetroScoreDimension | null>(null);
  const [overrideScore, setOverrideScore] = useState(5);
  const [overrideRationale, setOverrideRationale] = useState("");
  const [overrideSaving, setOverrideSaving] = useState(false);
  const [expandedEvidence, setExpandedEvidence] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (!id) return;
    const load = () => {
      api.getRun(id).then(setRun);
      api.getRunEvents(id).then(setEvents);
      api.getRunTree(id).then(setTree).catch(() => {});
      api.listRunArtefacts(id).then(setArtefacts).catch(() => setArtefacts([]));
    };
    load();
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  }, [id]);

  useEffect(() => {
    if (!id) return;
    setRetroLoading(true);
    api.getRunRetroScores(id)
      .then(setRetroScores)
      .catch(() => setRetroScores(null))
      .finally(() => setRetroLoading(false));
  }, [id]);

  async function recomputeRetro() {
    if (!id) return;
    setRetroRecomputing(true);
    try {
      const res = await api.recomputeRetroScore(id);
      setRetroScores(res);
    } catch { /* ignore */ }
    finally { setRetroRecomputing(false); }
  }

  async function submitOverride() {
    if (!id || !overrideModal) return;
    setOverrideSaving(true);
    try {
      await api.overrideRetroScore(id, overrideModal.dimension, overrideScore, overrideRationale || undefined);
      const res = await api.getRunRetroScores(id);
      setRetroScores(res);
      setOverrideModal(null);
    } catch { /* ignore */ }
    finally { setOverrideSaving(false); }
  }

  function openOverride(row: RetroScoreDimension) {
    setOverrideModal(row);
    setOverrideScore(row.score ?? 5);
    setOverrideRationale(row.rationale ?? "");
  }

  // For workflow runs: fetch the workflow definition once so we can render the
  // live execution graph. Sub-workflow children also fetch their parent's
  // graph (parent_run_id → workflow's target_slug) so step-into views still
  // show context — but we only fetch the *direct* target for v1.
  useEffect(() => {
    if (!run) return;
    if (run.kind !== "workflow") { setWorkflow(null); return; }
    let cancelled = false;
    api.getWorkflow(run.target_slug).then(w => {
      if (!cancelled) setWorkflow(w);
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [run?.kind, run?.target_slug]);

  // Build node_id → model_slug map from run tree child runs.
  const nodeModels = useMemo(() => {
    if (!tree) return {} as Record<string, string>;
    const map: Record<string, string> = {};
    const walk = (node: RunTreeNode) => {
      if (node.node_id && node.model_slug) map[node.node_id] = node.model_slug;
      node.children.forEach(walk);
    };
    walk(tree.root);
    return map;
  }, [tree]);

  // Derive node statuses from the event stream — recomputed every poll.
  const { graphNodes, graphEdges, statusCounts } = useMemo(() => {
    if (!workflow) return { graphNodes: [], graphEdges: [], statusCounts: {} as Record<string, number> };
    const { status, tokens } = deriveNodeStateFromEvents(events as any);
    const { nodes, edges } = graphToReactFlow(workflow.kind, workflow.graph, status, tokens, null, nodeModels);
    const counts: Record<string, number> = { idle: 0, running: 0, done: 0, error: 0 };
    for (const n of nodes) {
      const cls = (n.className || "").toString().split(" ")[0] as
        "idle" | "running" | "done" | "error";
      counts[cls] = (counts[cls] || 0) + 1;
    }
    return { graphNodes: nodes, graphEdges: edges, statusCounts: counts };
  }, [workflow, events]);

  if (!run) return <Page title="Run">…loading…</Page>;

  const assembled: Record<string, string> = {};
  for (const e of events) {
    if (e.kind === "llm_token" && e.node_id) {
      assembled[e.node_id] = (assembled[e.node_id] || "") + (e.payload?.delta || "");
    }
  }
  const display = events.filter(e => e.kind !== "llm_token");
  const toggle = (id: string) => setExpanded(s => {
    const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n;
  });

  const subtitle = (
    <span>
      {INITIATOR_ICON[run.initiator_kind] || ""} <strong>{run.initiator_kind}</strong>
      {run.initiator_id && <> · <span className="kbd">{run.initiator_id}</span></>}
      {" · "}{run.kind}:{run.target_slug}
      {run.model_slug && <> · model <span className="kbd">{run.model_slug}</span></>}
    </span>
  );

  return (
    <Page title={`Run ${run.id.slice(0, 12)}`}
          subtitle={subtitle as any}
          actions={<>
            <Link to="/runs" className="btn">← back</Link>
            {run.status === "running" && (
              <button className="btn btn-danger" data-testid="run-cancel"
                      onClick={async () => { await api.cancelRun(run.id); }}>
                cancel
              </button>
            )}
          </>}>

      {/* lineage breadcrumb */}
      {run.parent_run_id && (
        <div className="card mb-4" data-testid="run-parent">
          <div className="text-xs text-muted mb-1">parent run</div>
          <Link to={`/runs/${run.parent_run_id}`} className="font-mono">
            ↑ {run.parent_run_id.slice(0, 12)}
          </Link>
        </div>
      )}

      {/* tree + totals */}
      {tree && tree.root && (tree.totals.runs > 1 || tree.root.id !== run.id) && (
        <div className="card mb-4" data-testid="run-tree">
          <h2 className="text-base font-semibold mb-2">Run group</h2>
          <div className="flex gap-6 mb-3 text-sm">
            <div><span className="text-muted">runs:</span> <strong>{tree.totals.runs}</strong></div>
            <div><span className="text-muted">tokens in/out:</span> <strong>{tree.totals.tokens_in} / {tree.totals.tokens_out}</strong></div>
            {tree.totals.cost_usd > 0 && <div><span className="text-muted">cost:</span> <strong>${tree.totals.cost_usd.toFixed(4)}</strong></div>}
            {tree.totals.models && Object.keys(tree.totals.models).length > 0 && (
              <div><span className="text-muted">models:</span> {Object.entries(tree.totals.models).map(([m, c]) =>
                <span key={m} className="kbd ml-1">{m}×{c}</span>
              )}</div>
            )}
          </div>
          <TreeRows node={tree.root} currentId={run.id} depth={0} />
        </div>
      )}

      <div className="grid grid-cols-4 gap-4 mb-4">
        <Stat label="status">{<StatusBadge status={run.status} />}</Stat>
        <Stat label="started">{new Date(run.started_at).toLocaleString()}</Stat>
        <Stat label="tokens">{run.tokens_in} in / {run.tokens_out} out</Stat>
        <Stat label="events">{events.length}</Stat>
      </div>

      <div className="grid grid-cols-2 gap-4 mb-4">
        <div className="card">
          <h2 className="text-base font-semibold mb-2">Input</h2>
          <pre className="codebox max-h-48 overflow-auto">{JSON.stringify(run.input, null, 2)}</pre>
          {run.error && <>
            <h2 className="text-base font-semibold mb-2 mt-4 text-err">Error</h2>
            <pre className="codebox text-err">{run.error}</pre>
          </>}
        </div>
        <div className="card">
          <h2 className="text-base font-semibold mb-2">Output</h2>
          <pre className="codebox max-h-48 overflow-auto">{run.output ? JSON.stringify(run.output, null, 2) : "(none yet)"}</pre>
        </div>
      </div>

      {artefacts.length > 0 && (
        <div className="card mb-4" data-testid="run-artefacts">
          <h2 className="text-base font-semibold mb-3">Artefacts <span className="text-muted text-xs">({artefacts.length})</span></h2>
          <div className="space-y-1 text-sm">
            {artefacts.map(a => (
              <div key={a.id} className="flex items-center justify-between py-1 px-2 hover:bg-bg-3/40 rounded">
                <div className="flex items-center gap-2 min-w-0">
                  <span className="font-mono text-accent truncate">{a.name}</span>
                  <span className="badge">{a.mime}</span>
                  <span className="text-muted text-xs">{(a.size / 1024).toFixed(1)} kB</span>
                </div>
                <div className="flex items-center gap-2">
                  {!a.is_binary && (
                    <button
                      className="btn btn-ghost btn-sm"
                      onClick={async () => {
                        const full = await api.getRunArtefact(run.id, a.name);
                        setOpenArtefact({ name: full.name, content: full.content, mime: full.mime });
                      }}
                    >view</button>
                  )}
                  <a
                    className="btn btn-ghost btn-sm"
                    href={`/api/runs/${run.id}/artefacts/${encodeURIComponent(a.name)}`}
                    target="_blank"
                    rel="noopener"
                  >open</a>
                </div>
              </div>
            ))}
          </div>
          {openArtefact && (
            <div className="mt-4 border-t border-line pt-3">
              <div className="flex items-center justify-between mb-2">
                <span className="font-mono text-sm text-accent">{openArtefact.name}</span>
                <button className="btn btn-ghost btn-sm" onClick={() => setOpenArtefact(null)}>close</button>
              </div>
              <pre className="codebox max-h-96 overflow-auto whitespace-pre-wrap">{openArtefact.content}</pre>
            </div>
          )}
        </div>
      )}

      {/* ─────── Retro Score ─────── */}
      {(retroScores || retroLoading) && (
        <div className="card mb-4" data-testid="run-retro-scores">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-base font-semibold">Retro Score</h2>
            <button className="btn btn-ghost"
                    disabled={retroRecomputing}
                    onClick={recomputeRetro}
                    data-testid="retro-recompute">
              {retroRecomputing ? "recomputing…" : "recompute auto scores"}
            </button>
          </div>

          {retroLoading && !retroScores && <div className="text-sm text-muted">loading…</div>}

          {retroScores && (
            <>
              <div className="flex items-center gap-3 mb-3">
                <RetroOverallBadge score={retroScores.overall} size="lg" />
                <div className="text-xs text-muted">
                  {retroScores.computed_at && <>computed {retroScores.computed_at} · </>}
                  n_scores: {retroScores.n_scores}
                </div>
              </div>

              <div className="overflow-x-auto">
                <table className="w-full text-sm" data-testid="retro-scores-table">
                  <thead>
                    <tr className="text-left text-xs text-muted uppercase border-b border-line">
                      <th className="py-1 pr-3">dimension</th>
                      <th className="py-1 pr-3">score</th>
                      <th className="py-1 pr-3">source</th>
                      <th className="py-1 pr-3">rationale</th>
                      <th className="py-1 pr-3">evidence</th>
                      <th className="py-1"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {retroScores.scores.map(row => {
                      const evKey = row.dimension;
                      const evOpen = expandedEvidence.has(evKey);
                      return (
                        <>
                          <tr key={row.dimension} className="border-b border-line">
                            <td className="py-1.5 pr-3 font-mono text-xs">{row.dimension}</td>
                            <td className="py-1.5 pr-3">
                              {row.score !== null
                                ? <RetroOverallBadge score={row.score} size="sm" />
                                : <span className="text-muted text-xs">(—) (not scored)</span>}
                            </td>
                            <td className="py-1.5 pr-3 text-xs text-muted">{row.source || "(not scored)"}</td>
                            <td className="py-1.5 pr-3 text-xs max-w-xs truncate">{row.rationale || ""}</td>
                            <td className="py-1.5 pr-3 text-xs">
                              {row.evidence_json && (
                                <button className="btn btn-ghost btn-sm text-xs py-0"
                                        onClick={() => setExpandedEvidence(s => {
                                          const n = new Set(s); n.has(evKey) ? n.delete(evKey) : n.add(evKey); return n;
                                        })}>
                                  {evOpen ? "collapse" : "expand"}
                                </button>
                              )}
                            </td>
                            <td className="py-1.5 text-xs">
                              <button className="btn btn-ghost btn-sm text-xs py-0"
                                      onClick={() => openOverride(row)}
                                      data-testid={`retro-override-${row.dimension}`}>
                                override
                              </button>
                            </td>
                          </tr>
                          {evOpen && row.evidence_json && (
                            <tr key={`${row.dimension}-ev`}>
                              <td colSpan={6} className="pb-2 pt-0">
                                <pre className="codebox text-[11px] max-h-48 overflow-auto whitespace-pre-wrap">
                                  {JSON.stringify(row.evidence_json, null, 2)}
                                </pre>
                              </td>
                            </tr>
                          )}
                        </>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}

      <Modal open={!!overrideModal} onClose={() => setOverrideModal(null)}
             title={`Override: ${overrideModal?.dimension ?? ""}`}
             footer={
               <>
                 <button className="btn" onClick={() => setOverrideModal(null)}>cancel</button>
                 <button className="btn btn-primary" disabled={overrideSaving} onClick={submitOverride}>
                   {overrideSaving ? "saving…" : "save override"}
                 </button>
               </>
             }>
        {overrideModal && (
          <div className="space-y-3">
            <div>
              <label className="block text-xs text-muted uppercase tracking-wider mb-1">score (1–10)</label>
              <div className="flex items-center gap-3">
                <input type="range" min={1} max={10} step={1}
                       value={overrideScore} onChange={e => setOverrideScore(Number(e.target.value))}
                       className="flex-1" />
                <span className="font-mono font-semibold w-6 text-center">{overrideScore}</span>
              </div>
            </div>
            <div>
              <label className="block text-xs text-muted uppercase tracking-wider mb-1">rationale (optional)</label>
              <textarea rows={3} className="w-full text-sm"
                        value={overrideRationale}
                        onChange={e => setOverrideRationale(e.target.value)}
                        placeholder="Why are you overriding this score?" />
            </div>
          </div>
        )}
      </Modal>

      {Object.keys(assembled).length > 0 && (
        <div className="card mb-4" data-testid="run-thread">
          <h2 className="text-base font-semibold mb-2">Assistant text per node</h2>
          {Object.entries(assembled).map(([node, text]) => (
            <div key={node} className="mb-3">
              <div className="text-xs text-muted mb-1">node <span className="font-mono">{node}</span></div>
              <pre className="codebox max-h-64 overflow-auto whitespace-pre-wrap">{text}</pre>
            </div>
          ))}
        </div>
      )}

      {workflow && graphNodes.length > 0 && (
        <div className="card mb-4" data-testid="run-graph">
          <div className="flex items-center justify-between mb-2">
            <h2 className="text-base font-semibold">Execution graph</h2>
            <div className="flex items-center gap-3 text-xs">
              <Legend swatch="bg-bg-2 border-line"        label="pending"  count={statusCounts.idle    || 0} />
              <Legend swatch="bg-bg-2 border-accent"      label="running"  count={statusCounts.running || 0} pulse />
              <Legend swatch="bg-bg-2 border-ok"          label="done"     count={statusCounts.done    || 0} />
              <Legend swatch="bg-bg-2 border-err"         label="error"    count={statusCounts.error   || 0} />
              <span className="text-muted">total: {graphNodes.length}</span>
            </div>
          </div>
          <div style={{ height: 320 }} className="border border-line rounded overflow-hidden">
            <ReactFlow
              nodes={graphNodes}
              edges={graphEdges}
              fitView
              proOptions={{ hideAttribution: true }}
              nodesDraggable={false}
              nodesConnectable={false}
              elementsSelectable={false}
            >
              <Background gap={20} size={1} color="#21262d" />
              <Controls showInteractive={false} />
            </ReactFlow>
          </div>
        </div>
      )}

      <div className="card" data-testid="run-events">
        <h2 className="text-base font-semibold mb-3">Event timeline ({events.length})</h2>
        <div className="max-h-[600px] overflow-y-auto space-y-1">
          {display.map(e => {
            const cls = KIND_BADGE[e.kind] || "badge";
            const expandedNow = expanded.has(e.id);
            const summary = renderSummary(e);
            return (
              <div key={e.id} className="border-b border-line py-1">
                <div className="flex items-start gap-2 text-xs cursor-pointer hover:bg-bg-3/40 px-1"
                     onClick={() => toggle(e.id)}>
                  <span className="text-muted font-mono w-20 shrink-0">{new Date(e.ts).toLocaleTimeString()}</span>
                  <span className={`badge ${cls} w-28 shrink-0 text-center`}>{e.kind}</span>
                  <span className="font-mono text-muted w-24 shrink-0 truncate">{e.node_id || ""}</span>
                  <span className="flex-1 truncate">{summary}</span>
                </div>
                {expandedNow && (
                  <>
                    {(e.payload?.child_run_id || e.payload?.run_id) && (e.payload.child_run_id || e.payload.run_id) !== run.id && (
                      <div className="ml-24 mt-1 text-xs">
                        →&nbsp;
                        <Link to={`/runs/${e.payload.child_run_id || e.payload.run_id}`} className="font-mono">
                          child run {(e.payload.child_run_id || e.payload.run_id).slice(0, 8)}
                        </Link>
                      </div>
                    )}
                    <pre className="codebox mt-1 ml-24 text-[11px] max-h-72 overflow-auto whitespace-pre-wrap">
                      {JSON.stringify(e.payload, null, 2)}
                    </pre>
                  </>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </Page>
  );
}

function TreeRows({ node, currentId, depth }: { node: RunTreeNode; currentId: string; depth: number }) {
  const indent = "│ ".repeat(Math.max(0, depth - 1)) + (depth > 0 ? "├ " : "");
  const isCur = node.id === currentId;
  return (
    <>
      <div className={`flex items-center gap-2 py-1 text-xs ${isCur ? "bg-bg-3/40 -mx-2 px-2 rounded" : ""}`}>
        <span className="font-mono text-muted w-32 shrink-0 whitespace-pre">{indent}</span>
        <Link to={`/runs/${node.id}`} className="font-mono w-28 shrink-0">{node.id.slice(0, 8)}</Link>
        <span className="badge badge-info w-20 text-center shrink-0">{node.kind}</span>
        <span className="font-medium w-40 shrink-0 truncate">{node.target_slug}</span>
        <span className={`badge ${node.status === "success" ? "badge-success" :
                          node.status === "error" ? "badge-error" :
                          node.status === "running" ? "badge-running" : "badge-pending"}`}>
          {node.status}
        </span>
        <span className="text-muted">{node.tokens_in}/{node.tokens_out}</span>
        {node.model_slug && <span className="kbd text-[10px]">{node.model_slug}</span>}
      </div>
      {node.children.map(c => <TreeRows key={c.id} node={c} currentId={currentId} depth={depth + 1} />)}
    </>
  );
}

function Legend({ swatch, label, count, pulse }:
                 { swatch: string; label: string; count: number; pulse?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1.5" data-testid={`run-graph-legend-${label}`}>
      <span className={`inline-block w-3 h-3 rounded-sm border ${swatch} ${pulse ? "animate-pulse" : ""}`} />
      <span className="text-muted">{label}</span>
      <span className="font-mono">{count}</span>
    </span>
  );
}

function Stat({ label, children }: { label: string; children: any }) {
  return (
    <div className="card">
      <div className="text-xs text-muted uppercase mb-1">{label}</div>
      <div className="text-sm">{children}</div>
    </div>
  );
}

function renderSummary(e: RunEvent): string {
  const p = e.payload || {};
  switch (e.kind) {
    case "node_start": return `${p.label || p.agent || ""} ▸ ${p.provider || ""} ${p.model || ""}`;
    case "node_end":   return `→ ${(p.text || "").slice(0, 100)}${p.tokens_out ? ` (${p.tokens_out} tok)` : ""}`;
    case "tool_call":  return `${p.name}(${shortJson(p.input)})`;
    case "tool_result": return `${(p.content || "").slice(0, 140).replace(/\n/g, " ⏎ ")}`;
    case "thinking":   return `💭 ${(p.text || "").slice(0, 140).replace(/\n/g, " ⏎ ")}`;
    case "system.init": return `session ${(p.session_id || "").slice(0, 8)} · ${p.model || ""}`;
    case "error":      return `${p.error || ""}`;
    case "log":        return `${p.msg || ""}`;
    case "done":       return `${p.status || ""}`;
    default:           return JSON.stringify(p).slice(0, 140);
  }
}
function shortJson(v: any): string {
  if (v === undefined) return "";
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s.length > 80 ? s.slice(0, 80) + "…" : s;
}

function RetroOverallBadge({ score, size = "sm" }: { score: number | null; size?: "sm" | "lg" }) {
  if (score === null) return <span className="badge text-muted">—</span>;
  const cls = score >= 9 ? "badge-success"
            : score >= 7 ? "badge-ok"
            : score >= 5 ? "badge-warn"
            : score >= 3 ? "badge-orange"
            : "badge-crit";
  return (
    <span className={`badge ${cls} ${size === "lg" ? "text-2xl px-3 py-1" : ""}`}>
      {score}
    </span>
  );
}
