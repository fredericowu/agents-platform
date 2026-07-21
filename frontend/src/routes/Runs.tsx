import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import Page, { StatusBadge } from "../components/Page";
import { ModelBadge } from "../components/ModelBadge";
import { TargetBadge } from "../components/TargetBadge";
import { api, type Run, type RetroScoreSummary } from "../lib/api";
import type { Agent, Workflow } from "../lib/api";
import { useWsEvent } from "../lib/ws";

function ScoreBadge({ summary }: { summary?: RetroScoreSummary | null }) {
  const score = summary?.overall ?? null;
  if (score === null) return <span className="badge text-muted">—</span>;
  const cls = score >= 9 ? "badge-success"
            : score >= 7 ? "badge-ok"
            : score >= 5 ? "badge-warn"
            : score >= 3 ? "badge-orange"
            : "badge-crit";
  return <span className={`badge ${cls}`} title={`overall retro score: ${score}`}>{score}</span>;
}

// Fixed 10-color palette for grouping same-flow rows in the table — purely
// visual (a stable hash of flow_run_id picks the index), no backend meaning.
const FLOW_COLORS = [
  "#5b8cff", "#f97316", "#22c55e", "#a855f7", "#ec4899",
  "#eab308", "#06b6d4", "#ef4444", "#84cc16", "#94a3b8",
];
function flowColor(flowRunId: string): string {
  let h = 0;
  for (let i = 0; i < flowRunId.length; i++) h = (h * 31 + flowRunId.charCodeAt(i)) >>> 0;
  return FLOW_COLORS[h % FLOW_COLORS.length];
}

// For each session_id in the current page of runs, find the earliest run (by
// started_at) that used it — any later run sharing that session_id is a resume
// of it (the CLI keeps the same session_id across `--resume`, see executor.py).
function firstRunPerSession(runs: Run[]): Record<string, string> {
  const first: Record<string, Run> = {};
  for (const r of runs) {
    if (!r.session_id) continue;
    const cur = first[r.session_id];
    if (!cur || new Date(r.started_at).getTime() < new Date(cur.started_at).getTime()) {
      first[r.session_id] = r;
    }
  }
  const out: Record<string, string> = {};
  for (const sid in first) out[sid] = first[sid].id;
  return out;
}

export default function Runs() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [kind, setKind] = useState<string>("");
  const [q, setQ] = useState<string>("");
  const [rootsOnly, setRootsOnly] = useState<boolean>(false);
  const [nameMap, setNameMap] = useState<Record<string, string>>({});

  async function load() {
    setRuns(await api.listRuns(100, kind || undefined, { q: q || undefined, rootsOnly }));
  }
  // Initial load + reload when filters change
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [kind, q, rootsOnly]);

  // Load agent + workflow names once for slug→name lookup
  useEffect(() => {
    Promise.all([api.listAgents(), api.listWorkflows()]).then(([agents, workflows]) => {
      const map: Record<string, string> = {};
      (agents as Agent[]).forEach(a => { map[a.slug] = a.name; });
      (workflows as Workflow[]).forEach(w => { map[w.slug] = w.name; });
      setNameMap(map);
    }).catch(() => {});
  }, []);

  // Live run updates via WebSocket — upsert or prepend new runs
  useWsEvent<Run>("run_update", (newRun) => {
    setRuns(prev => {
      const idx = prev.findIndex(r => r.id === newRun.id);
      if (idx >= 0) {
        const next = [...prev];
        next[idx] = { ...next[idx], ...newRun };
        return next;
      }
      // New run — prepend (cap at 100)
      return [newRun, ...prev.slice(0, 99)];
    });
  }, []);

  return (
    <Page title="Runs" subtitle="Recent executions"
          actions={
            <>
              <input className="w-64" placeholder="search id/target/initiator..." value={q}
                     onChange={e => setQ(e.target.value)} data-testid="runs-search" />
              <select value={kind} onChange={e => setKind(e.target.value)} className="w-auto"
                      data-testid="runs-kind-filter">
                <option value="">all kinds</option>
                <option value="agent">agent</option>
                <option value="workflow">workflow</option>
              </select>
              <label className="text-xs flex items-center gap-1">
                <input type="checkbox" className="w-auto" checked={rootsOnly}
                       onChange={e => setRootsOnly(e.target.checked)}
                       data-testid="runs-roots-only" />
                <span className="text-muted">roots only</span>
              </label>
              {runs.some(r => r.status === "running" || r.status === "queued") && (
                <button className="btn btn-danger" data-testid="runs-cancel-all"
                        onClick={async () => {
                          if (!confirm("Cancel every running/queued flow and all its child runs?")) return;
                          try {
                            const res = await api.cancelAllRuns();
                            await load();
                            alert(`Cancelled ${res.cancelled_total} run(s); ${res.subprocesses_killed} subprocess(es) killed.`);
                          } catch (e: any) { alert(e.message || e); }
                        }}>cancel all running</button>
              )}
            </>
          }>
      <div className="card overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-muted uppercase">
              <th className="py-2 pr-2">id</th>
              <th className="py-2 pr-2">kind</th>
              <th className="py-2 pr-2">name</th>
              <th className="py-2 pr-2">Target</th>
              <th className="py-2 pr-2">Flow</th>
              <th className="py-2 pr-2">initiator</th>
              <th className="py-2 pr-2">model</th>
              <th className="py-2 pr-2">session</th>
              <th className="py-2 pr-2">status</th>
              <th className="py-2 pr-2">score</th>
              <th className="py-2 pr-2">tokens</th>
              <th className="py-2 pr-2">started</th>
              <th className="py-2 pr-2"></th>
            </tr>
          </thead>
          <tbody>
            {(() => { const firstBySession = firstRunPerSession(runs); return runs.map(r => {
              const flowColorHex = r.flow_run_id ? flowColor(r.flow_run_id) : null;
              return (
              <tr key={r.id} className="border-t border-line" data-testid={`runs-row-${r.id.slice(0,8)}`}
                  style={flowColorHex ? {
                    background: `${flowColorHex}14`,
                    borderLeft: `3px solid ${flowColorHex}`,
                  } : undefined}>
                <td className="py-2 pr-2">
                  <Link to={`/runs/${r.id}`} className="font-mono">{r.id.slice(0, 12)}</Link>
                  {r.parent_run_id && (
                    <Link to={`/runs/${r.parent_run_id}`} className="ml-2 text-muted text-xs" title="parent run">↑</Link>
                  )}
                </td>
                <td className="py-2 pr-2"><span className="badge badge-info">{r.kind}</span></td>
                <td className="py-2 pr-2 font-medium">
                  {r.source_slug ? (
                    <Link to={`/${r.kind === "workflow" ? "workflows" : "agents"}/${r.source_slug}`}>
                      {nameMap[r.source_slug] ?? r.source_slug}
                    </Link>
                  ) : (
                    <span className="text-muted">—</span>
                  )}
                </td>
                <td className="py-2 pr-2"><TargetBadge id={r.target_id} slug={r.target_slug} /></td>
                <td className="py-2 pr-2 text-xs">
                  {r.flow_run_id && flowColorHex ? (
                    <span className="badge cursor-pointer"
                          title={r.flow_needs_human ? "this flow escalated to a human at some point" : "click to filter this flow"}
                          style={{
                            background: `${flowColorHex}22`, color: flowColorHex,
                            border: r.flow_needs_human ? "1px solid #eab308" : `1px solid ${flowColorHex}55`,
                            boxShadow: r.flow_needs_human ? "0 0 0 1px #eab308" : undefined,
                          }}
                          onClick={() => setQ(r.flow_run_id!)}>
                      {r.flow_slug}: {r.flow_run_id.slice(0, 8)}
                    </span>
                  ) : (
                    <span className="text-muted">—</span>
                  )}
                </td>
                <td className="py-2 pr-2 text-xs">
                  <span className="badge">{r.initiator_kind}</span>
                  {r.initiator_id && r.initiator_id !== r.target_slug && (
                    <span className="kbd ml-1 text-[10px]">{r.initiator_id}</span>
                  )}
                </td>
                <td className="py-2 pr-2 text-xs">
                  <ModelBadge slug={r.model_slug} />
                </td>
                <td className="py-2 pr-2 text-xs">
                  {r.session_id ? (
                    <>
                      <span className="kbd" title={r.session_id}>{r.session_id.slice(0, 8)}</span>
                      {firstBySession[r.session_id] && firstBySession[r.session_id] !== r.id && (
                        <span className="badge badge-info ml-1" title="resumed from an earlier run's session">resumed</span>
                      )}
                    </>
                  ) : (
                    <span className="text-muted">—</span>
                  )}
                </td>
                <td className="py-2 pr-2"><StatusBadge status={r.status} /></td>
                <td className="py-2 pr-2"><ScoreBadge summary={r.retro_score_summary} /></td>
                <td className="py-2 pr-2 text-muted">{r.tokens_in}/{r.tokens_out}</td>
                <td className="py-2 pr-2 text-muted">{new Date(r.started_at).toLocaleString()}</td>
                <td className="py-2 pr-2">
                  {(r.status === "running" || r.status === "queued") && (
                    <button className="btn btn-danger text-xs py-1"
                            data-testid={`runs-cancel-${r.id.slice(0,8)}`}
                            onClick={async () => {
                              if (!confirm(`Cancel run ${r.id.slice(0,12)}${r.kind === "workflow" ? " and all its child runs" : ""}?`)) return;
                              try { await api.cancelRun(r.id); await load(); }
                              catch (e: any) { alert(e.message || e); }
                            }}>cancel</button>
                  )}
                </td>
              </tr>
            ); }); })()}
          </tbody>
        </table>
      </div>
    </Page>
  );
}
