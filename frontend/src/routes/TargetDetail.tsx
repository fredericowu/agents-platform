import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import Page from "../components/Page";
import { api, type Run, type Target, type TargetSummary } from "../lib/api";
import { ArrowLeft, CheckCircle2, XCircle, Loader2, AlertCircle, Ban, GitPullRequest, ExternalLink, Pencil, Check, X } from "lucide-react";
import { useWsEvent } from "../lib/ws";


const STATUS_BADGE: Record<string, string> = {
  active: "badge-info",
  completed: "badge-success",
  cancelled: "badge",
  abandoned: "badge-warn",
  success: "badge-success",
  error: "badge-crit",
  running: "badge-info",
  pending: "badge",
};

function StatusIcon({ status }: { status: string }) {
  if (status === "success") return <CheckCircle2 size={14} className="text-good" />;
  if (status === "error") return <XCircle size={14} className="text-crit" />;
  if (status === "cancelled") return <Ban size={14} className="text-muted" />;
  if (status === "running") return <Loader2 size={14} className="animate-spin text-accent" />;
  return <AlertCircle size={14} className="text-muted" />;
}

function fmtSec(s: number | null): string {
  if (s == null) return "—";
  if (s < 60) return `${s.toFixed(0)}s`;
  if (s < 3600) return `${(s / 60).toFixed(1)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

function fmtCost(n: number): string {
  if (n < 1) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

export default function TargetDetail() {
  const { slug } = useParams<{ slug: string }>();
  const nav = useNavigate();
  const [target, setTarget] = useState<Target | null>(null);
  const [summary, setSummary] = useState<TargetSummary | null>(null);
  const [runs, setRuns] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>("");
  const [editing, setEditing] = useState(false);
  const [nameVal, setNameVal] = useState("");
  const [slugVal, setSlugVal] = useState("");
  const [descVal, setDescVal] = useState("");
  const [editSaving, setEditSaving] = useState(false);
  const [editError, setEditError] = useState("");

  async function load() {
    if (!slug) return;
    setLoading(true);
    setError("");
    try {
      const [t, s, r] = await Promise.all([
        api.getTarget(slug),
        api.getTargetSummary(slug),
        api.getTargetRuns(slug, 500),
      ]);
      setTarget(t);
      setSummary(s);
      setRuns(r.runs);
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [slug]);

  // Live target updates (status changes, github issue write-back, etc.)
  useWsEvent<Target>("target_update", (data) => {
    if (data.slug !== slug) return;
    setTarget(data);
    // Summary is computed — re-fetch when target changes
    if (slug) api.getTargetSummary(slug).then(setSummary).catch(() => {});
  }, [slug]);

  // Live run updates linked to this target
  useWsEvent<Run>("run_update", (data) => {
    if (!target || data.target_id !== target.id) return;
    if (slug) {
      api.getTargetRuns(slug, 500).then(r => setRuns(r.runs)).catch(() => {});
      api.getTargetSummary(slug).then(setSummary).catch(() => {});
    }
  }, [target?.id, slug]);

  async function markStatus(newStatus: string) {
    if (!target) return;
    if (!confirm(`Mark target "${target.slug}" as ${newStatus}?`)) return;
    await api.updateTarget(target.slug, { status: newStatus });
    await load();
  }

  function openEdit() {
    if (!target) return;
    setNameVal(target.name);
    setSlugVal(target.slug);
    setDescVal(target.description ?? "");
    setEditError("");
    setEditing(true);
  }

  async function saveAll() {
    if (!target) return;
    setEditSaving(true);
    setEditError("");
    try {
      const name = nameVal.trim();
      const desc = descVal.trim();
      const newSlug = slugVal.trim();

      // name / description — single PATCH
      if (name && (name !== target.name || desc !== (target.description ?? ""))) {
        const patch: Record<string, string> = {};
        if (name !== target.name) patch.name = name;
        if (desc !== (target.description ?? "")) patch.description = desc;
        if (Object.keys(patch).length) {
          const updated = await api.updateTarget(target.slug, patch);
          setTarget(updated);
        }
      }

      // slug rename (separate endpoint)
      if (newSlug && newSlug !== target.slug) {
        const updated = await api.renameTarget(target.slug, newSlug);
        setTarget(updated);
        setEditing(false);
        nav(`/targets/${updated.slug}`, { replace: true });
        return;
      }

      setEditing(false);
    } catch (e: any) {
      setEditError(String(e.message || e));
    } finally {
      setEditSaving(false);
    }
  }

  // Build a chronological list with parent-child indentation
  const orderedRuns = useMemo(() => {
    if (!runs.length) return [];
    const byId: Record<string, any> = {};
    runs.forEach(r => { byId[r.id] = r; });
    return runs.map(r => {
      let depth = 0;
      let cur = r.parent_run_id;
      while (cur && byId[cur]) { depth++; cur = byId[cur].parent_run_id; if (depth > 12) break; }
      return { ...r, _depth: depth };
    });
  }, [runs]);

  if (loading) return <Page title="Loading…"><div className="text-muted">Loading target…</div></Page>;
  if (error) return <Page title="Error"><div className="text-crit">{error}</div></Page>;
  if (!target || !summary) return <Page title="Not found"><div className="text-muted">Target not found.</div></Page>;

  const budgetUsdPct = summary.pct_of_usd_budget ?? null;
  const budgetTokPct = summary.pct_of_token_budget ?? null;

  return (
    <Page
      title={target.name}
      subtitle={
        <span className="flex flex-col gap-0.5">
          <span className="text-muted text-xs">{target.description || <em className="opacity-40">no description</em>}</span>
          <span className="font-mono text-xs text-muted">{target.slug}</span>
        </span>
      }
      actions={
        <div className="flex items-center gap-2">
          <Link to="/targets" className="btn btn-ghost flex items-center gap-1">
            <ArrowLeft size={14} /> All targets
          </Link>
          {!editing && (
            <button className="btn btn-ghost flex items-center gap-1" onClick={openEdit}>
              <Pencil size={14} /> Edit
            </button>
          )}
          {target.status === "active" && !editing && (
            <>
              <button className="btn btn-success" onClick={() => markStatus("completed")}>
                Mark completed
              </button>
              <button className="btn btn-ghost" onClick={() => markStatus("cancelled")}>
                Cancel
              </button>
            </>
          )}
        </div>
      }
    >
      {/* Inline edit panel */}
      {editing && (
        <div className="card mb-6 flex flex-col gap-4">
          <div className="text-sm font-semibold">Edit target</div>
          {editError && <div className="text-crit text-xs">{editError}</div>}
          <label className="flex flex-col gap-1">
            <span className="text-xs text-muted uppercase tracking-wide">Nome</span>
            <input
              autoFocus
              className="input"
              value={nameVal}
              onChange={e => setNameVal(e.target.value)}
              onKeyDown={e => e.key === "Escape" && setEditing(false)}
              placeholder="Target name"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs text-muted uppercase tracking-wide">Descrição</span>
            <input
              className="input"
              value={descVal}
              onChange={e => setDescVal(e.target.value)}
              onKeyDown={e => e.key === "Escape" && setEditing(false)}
              placeholder="Description"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs text-muted uppercase tracking-wide">Slug</span>
            <input
              className="input font-mono"
              value={slugVal}
              onChange={e => setSlugVal(e.target.value)}
              onKeyDown={e => e.key === "Escape" && setEditing(false)}
              placeholder="slug"
            />
            <span className="text-xs text-muted">⚠ Alterar o slug muda a URL e redireciona automaticamente.</span>
          </label>
          <div className="flex gap-2">
            <button className="btn btn-primary flex items-center gap-1" onClick={saveAll} disabled={editSaving}>
              {editSaving ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />} Save
            </button>
            <button className="btn btn-ghost flex items-center gap-1" onClick={() => setEditing(false)} disabled={editSaving}>
              <X size={14} /> Cancel
            </button>
          </div>
        </div>
      )}
      {/* Header strip */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
        <div className="card">
          <div className="text-xs text-muted uppercase tracking-wide">Status</div>
          <div className="mt-1">
            <span className={`badge ${STATUS_BADGE[target.status] || ""}`}>{target.status}</span>
          </div>
        </div>
        <div className="card">
          <div className="text-xs text-muted uppercase tracking-wide">Runs</div>
          <div className="mt-1 text-xl font-semibold">{summary.runs_count}</div>
          <div className="text-xs text-muted">
            {Object.entries(summary.runs_by_status)
              .map(([k, v]) => `${k}: ${v}`).join(" · ")}
          </div>
        </div>
        <div className="card">
          <div className="text-xs text-muted uppercase tracking-wide">Cost</div>
          <div className="mt-1 text-xl font-semibold font-mono">{fmtCost(summary.cost_usd)}</div>
          {budgetUsdPct != null && (
            <div className="text-xs text-muted">{budgetUsdPct.toFixed(1)}% of ${target.budget_usd} budget</div>
          )}
        </div>
        <div className="card">
          <div className="text-xs text-muted uppercase tracking-wide">Wall</div>
          <div className="mt-1 text-xl font-semibold">{fmtSec(summary.wall_seconds)}</div>
          <div className="text-xs text-muted">{summary.tokens_in.toLocaleString()} in / {summary.tokens_out.toLocaleString()} out</div>
        </div>
      </div>

      {/* Budget bars */}
      {(budgetUsdPct != null || budgetTokPct != null) && (
        <div className="card mb-6">
          <div className="text-xs text-muted uppercase tracking-wide mb-3">Budget</div>
          {budgetUsdPct != null && (
            <div className="mb-3">
              <div className="flex justify-between text-xs mb-1">
                <span className="text-muted">USD</span>
                <span className="font-mono">{fmtCost(summary.cost_usd)} / ${target.budget_usd}</span>
              </div>
              <div className="h-2 bg-bg-3 rounded overflow-hidden">
                <div className={`h-full ${budgetUsdPct > 100 ? "bg-crit" : budgetUsdPct > 75 ? "bg-warn" : "bg-good"}`}
                     style={{ width: `${Math.min(budgetUsdPct, 100)}%` }} />
              </div>
            </div>
          )}
          {budgetTokPct != null && (
            <div>
              <div className="flex justify-between text-xs mb-1">
                <span className="text-muted">Tokens</span>
                <span className="font-mono">{(summary.tokens_in + summary.tokens_out).toLocaleString()} / {target.budget_tokens?.toLocaleString()}</span>
              </div>
              <div className="h-2 bg-bg-3 rounded overflow-hidden">
                <div className={`h-full ${budgetTokPct > 100 ? "bg-crit" : budgetTokPct > 75 ? "bg-warn" : "bg-good"}`}
                     style={{ width: `${Math.min(budgetTokPct, 100)}%` }} />
              </div>
            </div>
          )}
        </div>
      )}

      {/* PRs */}
      {target.pr_urls && target.pr_urls.length > 0 && (
        <div className="card mb-6">
          <div className="text-xs text-muted uppercase tracking-wide mb-3 flex items-center gap-2">
            <GitPullRequest size={12} /> Pull requests ({target.pr_urls.length})
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs text-muted">
                <tr><th className="text-left py-1 px-1">URL</th><th className="text-left py-1 px-1">Title</th><th className="text-left py-1 px-1">PR status</th><th className="text-left py-1 px-1">CI</th></tr>
              </thead>
              <tbody>
                {target.pr_urls.map((pr, i) => (
                  <tr key={i} className="border-t border-line">
                    <td className="py-1 px-1">
                      <a href={pr.url} target="_blank" rel="noopener" className="text-accent hover:underline inline-flex items-center gap-1 font-mono text-xs">
                        {pr.url.replace(/^https?:\/\//, "")} <ExternalLink size={10} />
                      </a>
                    </td>
                    <td className="py-1 px-1 text-muted">{pr.title || "—"}</td>
                    <td className="py-1 px-1">{pr.status && <span className={`badge ${pr.status === "merged" ? "badge-success" : pr.status === "open" ? "badge-info" : ""}`}>{pr.status}</span>}</td>
                    <td className="py-1 px-1">{pr.ci_status && <span className={`badge ${pr.ci_status === "passing" ? "badge-success" : pr.ci_status === "failing" ? "badge-crit" : ""}`}>{pr.ci_status}</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Metadata + canvases */}
      <div className="grid md:grid-cols-2 gap-4 mb-6">
        <div className="card">
          <div className="text-xs text-muted uppercase tracking-wide mb-2">Source</div>
          <div className="text-sm">
            <div><span className="text-muted">Kind:</span> {target.source_kind}</div>
            {target.source_ref && (
              <div className="font-mono"><span className="text-muted font-sans">Ref:</span> {target.source_ref}</div>
            )}
            <div className="mt-2 text-xs text-muted">
              Started {new Date(target.started_at).toLocaleString()}
              {target.ended_at && <> · Ended {new Date(target.ended_at).toLocaleString()}</>}
            </div>
          </div>
        </div>
        <div className="card">
          <div className="text-xs text-muted uppercase tracking-wide mb-2">Canvases</div>
          {(target.plan_canvas_id || target.report_canvas_id) ? (
            <ul className="text-sm space-y-1">
              {target.plan_canvas_id && <li className="font-mono text-accent">{target.plan_canvas_id}</li>}
              {target.report_canvas_id && <li className="font-mono text-accent">{target.report_canvas_id}</li>}
            </ul>
          ) : (
            <div className="text-muted text-sm">No canvases linked yet.</div>
          )}
          {target.tags?.length > 0 && (
            <div className="mt-3 flex gap-1 flex-wrap">
              {target.tags.map(t => <span key={t} className="badge">{t}</span>)}
            </div>
          )}
        </div>
      </div>

      {/* Agents + Models used */}
      <div className="grid md:grid-cols-2 gap-4 mb-6">
        <div className="card">
          <div className="text-xs text-muted uppercase tracking-wide mb-2">Agents used</div>
          {Object.keys(summary.agents_used).length === 0 ? (
            <div className="text-muted text-sm">no runs yet</div>
          ) : (
            <ul className="text-sm space-y-1">
              {Object.entries(summary.agents_used)
                .sort((a, b) => b[1] - a[1])
                .map(([k, v]) => (
                  <li key={k} className="flex justify-between">
                    <span className="font-mono text-fg">{k}</span>
                    <span className="text-muted">{v}</span>
                  </li>
                ))}
            </ul>
          )}
        </div>
        <div className="card">
          <div className="text-xs text-muted uppercase tracking-wide mb-2">Models used</div>
          {Object.keys(summary.models_used).length === 0 ? (
            <div className="text-muted text-sm">no models recorded</div>
          ) : (
            <ul className="text-sm space-y-1">
              {Object.entries(summary.models_used)
                .sort((a, b) => b[1] - a[1])
                .map(([k, v]) => (
                  <li key={k} className="flex justify-between">
                    <span className="font-mono text-fg">{k}</span>
                    <span className="text-muted">{v}</span>
                  </li>
                ))}
            </ul>
          )}
        </div>
      </div>

      {/* Timeline */}
      <div className="card">
        <div className="text-xs text-muted uppercase tracking-wide mb-3">Run timeline ({orderedRuns.length})</div>
        {orderedRuns.length === 0 ? (
          <div className="text-muted text-sm">
            No runs yet. Pass <code>target_slug: "{target.slug}"</code> to <code>run_agent_async</code> / <code>run_workflow_async</code> / <code>run_agents_parallel</code> to link runs here.
          </div>
        ) : (
          <div className="space-y-1 text-sm">
            {orderedRuns.map((r) => (
              <Link
                key={r.id}
                to={`/runs/${r.id}`}
                className="flex items-center gap-2 py-1 px-2 hover:bg-bg-3/40 rounded"
                style={{ marginLeft: r._depth * 16 }}
              >
                <StatusIcon status={r.status} />
                <span className="font-mono text-xs text-muted">{r.id.slice(0, 8)}</span>
                <span className={`badge ${r.kind === "workflow" ? "badge-info" : ""}`}>{r.kind}</span>
                <span className="font-mono text-fg">{r.target_slug}</span>
                {r.model_slug && <span className="text-xs text-muted font-mono">[{r.model_slug}]</span>}
                <span className="text-muted text-xs ml-auto">
                  {r.tokens_in + r.tokens_out > 0 && <>{(r.tokens_in + r.tokens_out).toLocaleString()} tok · </>}
                  {fmtCost(r.cost_usd || 0)}
                </span>
              </Link>
            ))}
          </div>
        )}
      </div>

      {target.notes && (
        <div className="card mt-4">
          <div className="text-xs text-muted uppercase tracking-wide mb-2">Notes</div>
          <pre className="text-sm whitespace-pre-wrap font-sans">{target.notes}</pre>
        </div>
      )}
    </Page>
  );
}
