import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import Page from "../components/Page";
import { api, type Workflow } from "../lib/api";

export default function Workflows() {
  const [list, setList] = useState<Workflow[]>([]);
  const nav = useNavigate();

  async function load() { setList(await api.listWorkflows()); }
  useEffect(() => { load(); }, []);

  async function remove(slug: string) {
    if (!confirm(`Delete workflow "${slug}"?`)) return;
    try { await api.deleteWorkflow(slug); await load(); }
    catch (e: any) { alert(e.message || e); }
  }
  async function clone(slug: string) {
    try {
      const w = await api.cloneWorkflow(slug);
      nav(`/workflows/${w.slug}`);
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
        const w = await api.importWorkflow(spec);
        nav(`/workflows/${w.slug}`);
      } catch (e: any) { alert(e.message || e); }
    };
    inp.click();
  }

  return (
    <Page title="Workflows" subtitle={`${list.length} orchestration(s). Click to edit and run.`}
          actions={
            <>
              <button className="btn" onClick={importJson} data-testid="workflows-import">import json</button>
              <button className="btn btn-primary" onClick={() => nav("/workflows/new")} data-testid="workflows-new">
                + new workflow
              </button>
            </>
          }>
      <div className="grid grid-cols-2 gap-4">
        {list.map(w => (
          <div key={w.slug} className="card hover:border-accent transition-colors relative"
               data-testid={`workflow-card-${w.slug}`}>
            <Link to={`/workflows/${w.slug}`} className="block">
              <div className="flex items-center justify-between mb-2 pr-44">
                <div className="text-base font-semibold">{w.name}</div>
                <span className="badge badge-info">{w.kind}</span>
              </div>
              <div className="text-xs text-muted pr-44">{w.description}</div>
            </Link>
            <div className="absolute top-3 right-3 flex gap-1 items-center">
              <button className="btn text-xs py-1"
                      onClick={(e) => { e.preventDefault(); e.stopPropagation(); clone(w.slug); }}
                      data-testid={`workflows-clone-${w.slug}`}>clone</button>
              <button className="btn btn-danger text-xs py-1"
                      onClick={(e) => { e.preventDefault(); e.stopPropagation(); remove(w.slug); }}
                      data-testid={`workflows-delete-${w.slug}`}>delete</button>
            </div>
          </div>
        ))}
      </div>
    </Page>
  );
}
