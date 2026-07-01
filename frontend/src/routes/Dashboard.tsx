import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import Page, { StatusBadge } from "../components/Page";
import { api, type Agent, type Workflow, type Run, type Model, type McpServer } from "../lib/api";

export default function Dashboard() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [wfs, setWfs] = useState<Workflow[]>([]);
  const [models, setModels] = useState<Model[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [mcp, setMcp] = useState<McpServer[]>([]);

  useEffect(() => {
    Promise.all([
      api.listAgents().then(setAgents),
      api.listWorkflows().then(setWfs),
      api.listModels().then(setModels),
      api.listRuns(10, undefined, { rootsOnly: true }).then(setRuns),
      api.listMcpServers().then(setMcp),
    ]).catch(console.error);
  }, []);

  return (
    <Page title="Dashboard" subtitle="At-a-glance overview of the platform">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        <Metric label="Agents" value={agents.length} link="/agents" />
        <Metric label="Workflows" value={wfs.length} link="/workflows" />
        <Metric label="Models" value={models.length} link="/models" />
        <Metric label="MCP servers" value={mcp.length} link="/mcp" />
      </div>

      <section className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="card" data-testid="recent-runs">
          <h2 className="text-base font-semibold mb-3">Recent runs</h2>
          {runs.length === 0 && <div className="text-muted text-sm">no runs yet — try the playground</div>}
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <tbody>
                {runs.map(r => (
                  <tr key={r.id} className="border-b border-line last:border-0">
                    <td className="py-2 pr-2"><Link to={`/runs/${r.id}`} className="font-mono">{r.id.slice(0, 8)}</Link></td>
                    <td className="py-2 pr-2"><span className="badge badge-info">{r.kind}</span></td>
                    <td className="py-2 pr-2">{r.target_slug}</td>
                    <td className="py-2 pr-2"><StatusBadge status={r.status} /></td>
                    <td className="py-2 pr-2 text-muted text-right">{r.tokens_in}/{r.tokens_out} tok</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="card">
          <h2 className="text-base font-semibold mb-3">Quick start</h2>
          <ol className="list-decimal list-inside space-y-2 text-sm">
            <li>Try a workflow: <Link to="/workflows/orchestrator-worker">Orchestrator → Workers</Link></li>
            <li>Edit an agent: <Link to="/agents/coder">Coder</Link></li>
            <li>Chat in the <Link to="/playground">playground</Link></li>
            <li>Run an <Link to="/evals">eval</Link> against an agent</li>
            <li>Inspect MCP discovery on the <Link to="/mcp">MCP</Link> page</li>
          </ol>
        </div>
      </section>

      <section className="mt-6 grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="card">
          <h2 className="text-base font-semibold mb-3">Workflows ({wfs.length})</h2>
          <ul className="space-y-1 text-sm">
            {wfs.map(w => (
              <li key={w.slug} className="flex items-center justify-between border-b border-line py-2 last:border-0">
                <Link to={`/workflows/${w.slug}`}>{w.name}</Link>
                <span className="badge badge-info">{w.kind}</span>
              </li>
            ))}
          </ul>
        </div>
        <div className="card">
          <h2 className="text-base font-semibold mb-3">Agents ({agents.length})</h2>
          <ul className="grid grid-cols-2 gap-2 text-sm">
            {agents.map(a => (
              <li key={a.slug}>
                <Link to={`/agents/${a.slug}`} className="flex flex-col">
                  <span className="font-medium">{a.name}</span>
                  <span className="text-muted text-xs">{a.description.slice(0, 50)}</span>
                </Link>
              </li>
            ))}
          </ul>
        </div>
      </section>
    </Page>
  );
}

function Metric({ label, value, link }: { label: string; value: number; link: string }) {
  return (
    <Link to={link} className="card hover:border-accent transition-colors block">
      <div className="text-3xl font-bold text-fg">{value}</div>
      <div className="text-xs text-muted uppercase tracking-wider">{label}</div>
    </Link>
  );
}
