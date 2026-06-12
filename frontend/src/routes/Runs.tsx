import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import Page, { StatusBadge } from "../components/Page";
import { ModelBadge } from "../components/ModelBadge";
import { TargetBadge } from "../components/TargetBadge";
import { api, type Run, type RetroScoreSummary } from "../lib/api";
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

export default function Runs() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [kind, setKind] = useState<string>("");
  const [q, setQ] = useState<string>("");
  const [rootsOnly, setRootsOnly] = useState<boolean>(false);

  async function load() {
    setRuns(await api.listRuns(100, kind || undefined, { q: q || undefined, rootsOnly }));
  }
  // Initial load + reload when filters change
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [kind, q, rootsOnly]);

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
              {runs.some(r => r.status === "running") && (
                <button className="btn btn-danger" data-testid="runs-cancel-all"
                        onClick={async () => {
                          if (!confirm("Cancel every running flow and all its child runs?")) return;
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
              <th className="py-2 pr-2">slug</th>
              <th className="py-2 pr-2">Target</th>
              <th className="py-2 pr-2">initiator</th>
              <th className="py-2 pr-2">model</th>
              <th className="py-2 pr-2">status</th>
              <th className="py-2 pr-2">score</th>
              <th className="py-2 pr-2">tokens</th>
              <th className="py-2 pr-2">started</th>
              <th className="py-2 pr-2"></th>
            </tr>
          </thead>
          <tbody>
            {runs.map(r => (
              <tr key={r.id} className="border-t border-line" data-testid={`runs-row-${r.id.slice(0,8)}`}>
                <td className="py-2 pr-2">
                  <Link to={`/runs/${r.id}`} className="font-mono">{r.id.slice(0, 12)}</Link>
                  {r.parent_run_id && (
                    <Link to={`/runs/${r.parent_run_id}`} className="ml-2 text-muted text-xs" title="parent run">↑</Link>
                  )}
                </td>
                <td className="py-2 pr-2"><span className="badge badge-info">{r.kind}</span></td>
                <td className="py-2 pr-2 font-medium">{r.target_slug}</td>
                <td className="py-2 pr-2"><TargetBadge id={r.target_id} slug={r.target_slug} /></td>
                <td className="py-2 pr-2 text-xs">
                  <span className="badge">{r.initiator_kind}</span>
                  {r.initiator_id && r.initiator_id !== r.target_slug && (
                    <span className="kbd ml-1 text-[10px]">{r.initiator_id}</span>
                  )}
                </td>
                <td className="py-2 pr-2 text-xs">
                  <ModelBadge slug={r.model_slug} />
                </td>
                <td className="py-2 pr-2"><StatusBadge status={r.status} /></td>
                <td className="py-2 pr-2"><ScoreBadge summary={r.retro_score_summary} /></td>
                <td className="py-2 pr-2 text-muted">{r.tokens_in}/{r.tokens_out}</td>
                <td className="py-2 pr-2 text-muted">{new Date(r.started_at).toLocaleString()}</td>
                <td className="py-2 pr-2">
                  {r.status === "running" && (
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
            ))}
          </tbody>
        </table>
      </div>
    </Page>
  );
}
