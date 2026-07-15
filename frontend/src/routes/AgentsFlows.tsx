import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import Page from "../components/Page";
import { api, type AgentFlow } from "../lib/api";

export default function AgentsFlows() {
  const [list, setList] = useState<AgentFlow[]>([]);
  const nav = useNavigate();

  async function load() { setList(await api.listAgentFlows()); }
  useEffect(() => { load(); }, []);

  async function remove(slug: string) {
    if (!confirm(`Delete agents flow "${slug}"?`)) return;
    try { await api.deleteAgentFlow(slug); await load(); }
    catch (e: any) { alert(e.message || e); }
  }
  async function clone(slug: string) {
    try {
      const f = await api.cloneAgentFlow(slug);
      nav(`/agents-flow/${f.slug}`);
    } catch (e: any) { alert(e.message || e); }
  }

  return (
    <Page title="Agents Flow"
          subtitle={`${list.length} flow(s). Drag agents onto a canvas and connect them to define who can hand off to whom.`}
          actions={
            <button className="btn btn-primary" onClick={() => nav("/agents-flow/new")} data-testid="agents-flows-new">
              + new flow
            </button>
          }>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        {list.map(f => (
          <div key={f.slug} className="card hover:border-accent transition-colors"
               data-testid={`agents-flow-card-${f.slug}`}>
            <Link to={`/agents-flow/${f.slug}`} className="block">
              <div className="flex items-center justify-between gap-2 mb-2 flex-wrap">
                <div className="text-base font-semibold">{f.name}</div>
                <div className="flex gap-1 flex-wrap">
                  {f.enabled && <span className="badge badge-success">● enabled</span>}
                  <span className="badge badge-info">{(f.graph?.nodes || []).length} nodes</span>
                </div>
              </div>
              <div className="text-xs font-mono text-muted mb-1">{f.slug}</div>
              <div className="text-xs text-muted">{f.description}</div>
            </Link>
            <div className="flex gap-1 items-center justify-end mt-3">
              <button className="btn text-xs py-1"
                      onClick={(e) => { e.preventDefault(); e.stopPropagation(); clone(f.slug); }}
                      data-testid={`agents-flows-clone-${f.slug}`}>clone</button>
              <button className="btn btn-danger text-xs py-1"
                      onClick={(e) => { e.preventDefault(); e.stopPropagation(); remove(f.slug); }}
                      data-testid={`agents-flows-delete-${f.slug}`}>delete</button>
            </div>
          </div>
        ))}
        {list.length === 0 && (
          <div className="text-sm text-muted">No agents flows yet — create one to connect agents together.</div>
        )}
      </div>
    </Page>
  );
}
