import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import Page from "../components/Page";
import { api, type AgentConfig } from "../lib/api";
import { Plus, Settings2, Trash2 } from "lucide-react";

export default function AgentConfigs() {
  const [list, setList] = useState<AgentConfig[]>([]);
  const [error, setError] = useState("");

  async function load() {
    setList(await api.listAgentConfigs());
  }
  useEffect(() => { load(); }, []);

  async function remove(slug: string) {
    if (!confirm(`Delete agent config "${slug}"?`)) return;
    try { await api.deleteAgentConfig(slug); await load(); }
    catch (e: any) { setError(String(e.message || e)); }
  }

  return (
    <Page
      title="Agents Config"
      subtitle="Reusable bundles of Permissions, Extra volumes and MCP servers — pick one from an Agent instead of duplicating config inline."
      actions={
        <Link to="/agent-configs/new" className="btn btn-primary flex items-center gap-2">
          <Plus size={14} /> New config
        </Link>
      }
    >
      {error && <div className="codebox text-err mb-3">{error}</div>}
      {list.length === 0 ? (
        <div className="text-muted text-sm py-12 text-center">
          <Settings2 size={32} className="mx-auto mb-3 opacity-40" />
          No Agents Config yet. Create one to bundle Permissions, Extra volumes and MCP servers for reuse across agents.
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-muted text-xs uppercase tracking-wide">
              <tr>
                <th className="text-left py-2 px-2">Config</th>
                <th className="text-left py-2 px-2">Permissions</th>
                <th className="text-left py-2 px-2">Volumes</th>
                <th className="text-left py-2 px-2">MCP servers</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {list.map(c => (
                <tr key={c.slug} className="border-t border-line hover:bg-bg-3/40">
                  <td className="py-2 px-2">
                    <Link to={`/agent-configs/${c.slug}`} className="text-fg hover:text-accent flex items-center gap-2">
                      <Settings2 size={14} className="text-accent" />
                      <span className="font-semibold">{c.name}</span>
                      <span className="text-muted text-xs">{c.slug}</span>
                    </Link>
                    {c.description && (
                      <div className="text-muted text-xs mt-1 line-clamp-1">{c.description}</div>
                    )}
                  </td>
                  <td className="py-2 px-2 text-xs">
                    {Object.entries(c.permissions || {}).filter(([, v]) => v).map(([k]) => (
                      <span key={k} className="badge badge-info mr-1">{k}</span>
                    ))}
                    {Object.values(c.permissions || {}).every(v => !v) && <span className="text-muted">—</span>}
                  </td>
                  <td className="py-2 px-2 text-xs text-muted">{(c.extra_volumes || []).length || "—"}</td>
                  <td className="py-2 px-2 text-xs text-muted">{Object.keys(c.mcp_config?.servers || {}).length || "—"}</td>
                  <td className="py-2 px-2 text-right">
                    <button
                      className="btn btn-ghost btn-sm text-muted hover:text-crit"
                      title="Delete"
                      onClick={() => remove(c.slug)}
                    >
                      <Trash2 size={14} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Page>
  );
}
