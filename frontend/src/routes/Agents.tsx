import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import Page from "../components/Page";
import { api, type Agent } from "../lib/api";

export default function Agents() {
  const [list, setList] = useState<Agent[]>([]);
  const [filter, setFilter] = useState("");
  const nav = useNavigate();

  async function load() { setList(await api.listAgents()); }
  useEffect(() => { load(); }, []);

  const filtered = list.filter(a => a.name.toLowerCase().includes(filter.toLowerCase())
                                  || a.slug.toLowerCase().includes(filter.toLowerCase()));

  async function remove(slug: string) {
    if (!confirm(`Delete agent "${slug}"?`)) return;
    try { await api.deleteAgent(slug); await load(); }
    catch (e: any) { alert(e.message || e); }
  }
  async function clone(slug: string) {
    try {
      const a = await api.cloneAgent(slug);
      nav(`/agents/${a.slug}`);
    } catch (e: any) { alert(e.message || e); }
  }

  async function importJson() {
    const inp = document.createElement("input");
    inp.type = "file"; inp.accept = ".json,application/json";
    inp.onchange = async () => {
      const f = inp.files?.[0]; if (!f) return;
      try {
        const text = await f.text();
        const spec = JSON.parse(text);
        const a = await api.importAgent(spec);
        nav(`/agents/${a.slug}`);
      } catch (e: any) { alert(e.message || e); }
    };
    inp.click();
  }

  return (
    <Page title="Agents" subtitle={`${list.length} agent profile(s). Click any to edit.`}
          actions={
            <>
              <button className="btn" onClick={importJson} data-testid="agents-import">import json</button>
              <button className="btn btn-primary" onClick={() => nav("/agents/new")} data-testid="agents-new">
                + new agent
              </button>
            </>
          }>
      <div className="mb-4 max-w-sm">
        <input placeholder="filter..." value={filter}
               onChange={e => setFilter(e.target.value)}
               data-testid="agents-filter" />
      </div>
      <div className="grid grid-cols-3 gap-4">
        {filtered.map(a => (
          <div key={a.slug} className="card hover:border-accent transition-colors relative"
               data-testid={`agent-card-${a.slug}`}>
            <Link to={`/agents/${a.slug}`} className="block">
              <div className="flex items-center justify-between mb-2 pr-32">
                <div className="text-base font-semibold" style={{ color: a.color }}>{a.name}</div>
              </div>
              <div className="text-xs text-muted mb-3 line-clamp-2 min-h-[2lh] pr-32">{a.description}</div>
              <div className="flex items-center justify-between text-xs">
                <span className="kbd">{a.model_slug || "—"}</span>
                <span className="text-muted">{a.tool_specs.length} tools · {a.skill_slugs.length} skills</span>
              </div>
            </Link>
            <div className="absolute top-3 right-3 flex gap-1">
              <button className="btn text-xs py-1"
                      onClick={(e) => { e.preventDefault(); e.stopPropagation(); clone(a.slug); }}
                      data-testid={`agents-clone-${a.slug}`}>clone</button>
              <button className="btn btn-danger text-xs py-1"
                      onClick={(e) => { e.preventDefault(); e.stopPropagation(); remove(a.slug); }}
                      data-testid={`agents-delete-${a.slug}`}>delete</button>
            </div>
          </div>
        ))}
      </div>
    </Page>
  );
}
