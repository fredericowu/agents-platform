import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import Page from "../components/Page";
import { api, type Agent, type AgentGroup } from "../lib/api";

const BLANK: AgentGroup = { slug: "", name: "", description: "", instructions: "" };

export default function AgentGroupEdit() {
  const { slug } = useParams<{ slug: string }>();
  const isNew = slug === "new";
  const nav = useNavigate();
  const [g, setG] = useState<AgentGroup | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [editedSlug, setEditedSlug] = useState("");
  const [addSlug, setAddSlug] = useState("");

  async function loadAgents() { setAgents(await api.listAgents()); }

  useEffect(() => {
    if (!slug) return;
    loadAgents();
    if (isNew) {
      setG({ ...BLANK });
    } else {
      api.getAgentGroup(slug).then(g => { setG(g); setEditedSlug(g.slug); });
    }
  }, [slug, isNew]);

  if (!g) return <Page title="Agent group">…loading…</Page>;

  const members = agents.filter(a => a.group_slug === slug);
  const nonMembers = agents.filter(a => a.group_slug !== slug);

  async function save() {
    if (!g) return;
    setSaving(true); setError("");
    try {
      if (isNew) {
        const created = await api.createAgentGroup({
          slug: g.slug || undefined, name: g.name || g.slug,
          description: g.description, instructions: g.instructions,
        });
        nav(`/agent-groups/${created.slug}`);
      } else if (slug) {
        const targetSlug = editedSlug.trim() || slug;
        if (targetSlug !== slug) {
          await api.renameAgentGroup(slug, targetSlug);
        }
        await api.saveAgentGroup(targetSlug, { name: g.name, description: g.description, instructions: g.instructions });
        if (targetSlug !== slug) nav(`/agent-groups/${targetSlug}`);
      }
    } catch (e: any) { setError(String(e.message || e)); }
    finally { setSaving(false); }
  }

  async function remove() {
    if (!slug || isNew) return;
    if (!confirm(`Delete agent group "${slug}"?`)) return;
    try { await api.deleteAgentGroup(slug); nav("/agent-groups"); }
    catch (e: any) { setError(String(e.message || e)); }
  }

  async function addMember() {
    if (!slug || !addSlug) return;
    try { await api.addAgentGroupMember(slug, addSlug); setAddSlug(""); await loadAgents(); }
    catch (e: any) { setError(String(e.message || e)); }
  }

  async function removeMember(agentSlug: string) {
    if (!slug) return;
    try { await api.removeAgentGroupMember(slug, agentSlug); await loadAgents(); }
    catch (e: any) { setError(String(e.message || e)); }
  }

  return (
    <Page title={isNew ? "New agent group" : g.name}
          subtitle={isNew ? "Define shared instructions and pick which agents belong to this group." : g.description}
          actions={
            <>
              <Link to="/agent-groups" className="btn">← back</Link>
              {!isNew && <button className="btn btn-danger" onClick={remove}>delete</button>}
              <button className="btn btn-primary" onClick={save} disabled={saving}>
                {saving ? "saving..." : (isNew ? "create" : "save")}
              </button>
            </>
          }>
      {error && <div className="codebox text-err mb-3">{error}</div>}
      <div className="space-y-4 max-w-3xl">
        <div className="card">
          <h2 className="text-base font-semibold mb-3">Group</h2>
          <label className="block text-xs text-muted mb-1">name</label>
          <input value={g.name} onChange={e => setG({ ...g, name: e.target.value })} />
          <label className="block text-xs text-muted mt-3 mb-1">slug</label>
          {isNew ? (
            <input value={g.slug} onChange={e => setG({ ...g, slug: e.target.value })}
                   placeholder="(leave blank to auto-generate)" className="font-mono" />
          ) : (
            <input value={editedSlug} onChange={e => setEditedSlug(e.target.value)}
                   className="font-mono" title="editable — rename updates member agents' group_slug too" />
          )}
          <label className="block text-xs text-muted mt-3 mb-1">description</label>
          <input value={g.description} onChange={e => setG({ ...g, description: e.target.value })} />
        </div>

        <div className="card">
          <h2 className="text-base font-semibold mb-1">Instructions</h2>
          <p className="text-xs text-muted mb-3">
            Prepended to each member agent's own system prompt at run time — group instructions
            first, then the agent's own. Move shared boilerplate here instead of duplicating it
            per agent.
          </p>
          <textarea value={g.instructions} onChange={e => setG({ ...g, instructions: e.target.value })}
                    rows={12} className="font-mono text-sm w-full" />
        </div>

        {!isNew && (
          <div className="card">
            <h2 className="text-base font-semibold mb-3">Members ({members.length})</h2>
            <div className="flex flex-col gap-2 mb-3">
              {members.map(a => (
                <div key={a.slug} className="flex items-center justify-between border border-line rounded px-3 py-2">
                  <div>
                    <Link to={`/agents/${a.slug}`} className="font-medium hover:text-accent">{a.name}</Link>
                    <div className="text-xs font-mono text-muted">{a.slug} · {a.model_slug}</div>
                  </div>
                  <button className="btn btn-danger text-xs py-1" onClick={() => removeMember(a.slug)}>remove</button>
                </div>
              ))}
              {members.length === 0 && <div className="text-xs text-muted">No agents in this group yet.</div>}
            </div>
            <div className="flex gap-2">
              <select value={addSlug} onChange={e => setAddSlug(e.target.value)}>
                <option value="">add agent…</option>
                {nonMembers.map(a => (
                  <option key={a.slug} value={a.slug}>{a.name} ({a.slug})</option>
                ))}
              </select>
              <button className="btn" onClick={addMember} disabled={!addSlug}>add</button>
            </div>
          </div>
        )}
      </div>
    </Page>
  );
}
