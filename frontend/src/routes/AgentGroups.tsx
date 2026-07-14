import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import Page from "../components/Page";
import { api, type Agent, type AgentGroup } from "../lib/api";
import { Plus, Users, Trash2 } from "lucide-react";

export default function AgentGroups() {
  const [list, setList] = useState<AgentGroup[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [error, setError] = useState("");

  async function load() {
    setList(await api.listAgentGroups());
    setAgents(await api.listAgents());
  }
  useEffect(() => { load(); }, []);

  async function remove(slug: string) {
    if (!confirm(`Delete agent group "${slug}"? Member agents keep their own prompt but lose the group instructions.`)) return;
    try { await api.deleteAgentGroup(slug); await load(); }
    catch (e: any) { setError(String(e.message || e)); }
  }

  const memberCount = (slug: string) => agents.filter(a => a.group_slug === slug).length;

  return (
    <Page
      title="Agent Group"
      subtitle="Cluster agents (e.g. different models) under shared instructions — appended before each agent's own system prompt."
      actions={
        <Link to="/agent-groups/new" className="btn btn-primary flex items-center gap-2">
          <Plus size={14} /> New group
        </Link>
      }
    >
      {error && <div className="codebox text-err mb-3">{error}</div>}
      {list.length === 0 ? (
        <div className="text-muted text-sm py-12 text-center">
          <Users size={32} className="mx-auto mb-3 opacity-40" />
          No Agent Groups yet. Create one to share instructions across agents (e.g. one prompt, many models).
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-muted text-xs uppercase tracking-wide">
              <tr>
                <th className="text-left py-2 px-2">Group</th>
                <th className="text-left py-2 px-2">Members</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {list.map(g => (
                <tr key={g.slug} className="border-t border-line hover:bg-bg-3/40">
                  <td className="py-2 px-2">
                    <Link to={`/agent-groups/${g.slug}`} className="text-fg hover:text-accent flex items-center gap-2">
                      <Users size={14} className="text-accent" />
                      <span className="font-semibold">{g.name}</span>
                      <span className="text-muted text-xs">{g.slug}</span>
                    </Link>
                    {g.description && (
                      <div className="text-muted text-xs mt-1 line-clamp-1">{g.description}</div>
                    )}
                  </td>
                  <td className="py-2 px-2 text-xs text-muted">{memberCount(g.slug)}</td>
                  <td className="py-2 px-2 text-right">
                    <button
                      className="btn btn-ghost btn-sm text-muted hover:text-crit"
                      title="Delete"
                      onClick={() => remove(g.slug)}
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
