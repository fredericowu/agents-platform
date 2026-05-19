import { useEffect, useMemo, useState } from "react";
import Page from "../components/Page";
import Modal, { FormRow } from "../components/Modal";
import { api, type Agent, type EvalRow, type Workflow } from "../lib/api";

const BLANK = {
  slug: "", name: "", description: "",
  target_kind: "agent", target_slug: "echo-coder",
  dataset_json: '[{"input":"ping","expected":"ping"}]',
  metric: "assert_contains", metric_args_json: "{}",
};

export default function Evals() {
  const [list, setList] = useState<EvalRow[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [running, setRunning] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, any>>({});
  const [open, setOpen] = useState(false);
  const [editingSlug, setEditingSlug] = useState<string | null>(null);
  const [form, setForm] = useState<any>(BLANK);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const [resettable, setResettable] = useState<Set<string>>(new Set());

  async function load() {
    setList(await api.listEvals());
    setAgents(await api.listAgents());
    setWorkflows(await api.listWorkflows());
    try { setResettable(new Set(await api.listResettableEvals())); } catch {}
  }
  useEffect(() => { load(); }, []);

  const targetOptions = useMemo(() =>
    form.target_kind === "agent"
      ? agents.map(a => ({ slug: a.slug, name: a.name }))
      : workflows.map(w => ({ slug: w.slug, name: w.name })),
    [form.target_kind, agents, workflows]);

  async function runOne(slug: string) {
    setRunning(slug);
    try {
      const res = await api.runEval(slug);
      setResults(r => ({ ...r, [slug]: res }));
    } finally { setRunning(null); }
  }

  async function remove(slug: string) {
    if (!confirm(`Delete eval "${slug}"?`)) return;
    await api.deleteEval(slug);
    await load();
  }

  function openCreate() {
    setEditingSlug(null); setForm(BLANK); setError(""); setOpen(true);
  }
  function openEdit(e: EvalRow) {
    setEditingSlug(e.slug);
    setForm({
      slug: e.slug, name: e.name, description: e.description,
      target_kind: e.target_kind, target_slug: e.target_slug,
      dataset_json: JSON.stringify(e.dataset, null, 2),
      metric: e.metric, metric_args_json: JSON.stringify(e.metric_args || {}),
    });
    setError(""); setOpen(true);
  }

  async function save() {
    setSaving(true); setError("");
    try {
      const dataset = JSON.parse(form.dataset_json);
      const metric_args = JSON.parse(form.metric_args_json || "{}");
      const payload = {
        slug: form.slug, name: form.name || form.slug, description: form.description,
        target_kind: form.target_kind, target_slug: form.target_slug,
        dataset, metric: form.metric, metric_args,
      };
      if (editingSlug) {
        await api.updateEval(editingSlug, payload);
      } else {
        await api.createEval(payload);
      }
      setOpen(false); setEditingSlug(null); setForm(BLANK); await load();
    } catch (e: any) { setError(String(e.message || e)); }
    finally { setSaving(false); }
  }

  return (
    <Page title="Evals" subtitle="Automated scoring against datasets"
          actions={
            <button className="btn btn-primary" onClick={openCreate} data-testid="evals-new">
              + new eval
            </button>
          }>
      <div className="space-y-4">
        {list.map(e => (
          <div key={e.slug} className="card" data-testid={`eval-${e.slug}`}>
            <div className="flex items-center justify-between">
              <div>
                <div className="text-base font-semibold">{e.name}</div>
                <div className="text-xs text-muted">{e.description}</div>
                <div className="text-xs text-muted mt-1">
                  target: <span className="kbd">{e.target_kind}:{e.target_slug}</span>
                  · metric: <span className="kbd">{e.metric}</span>
                  · {e.dataset.length} cases
                </div>
              </div>
              <div className="flex gap-2">
                <button className="btn btn-primary" onClick={() => runOne(e.slug)}
                        disabled={running === e.slug} data-testid={`eval-run-${e.slug}`}>
                  {running === e.slug ? "running..." : "run eval"}
                </button>
                <button className="btn" onClick={() => openEdit(e)}
                        data-testid={`evals-edit-${e.slug}`}>edit</button>
                {resettable.has(e.slug) && (
                  <button className="btn" data-testid={`evals-reset-${e.slug}`}
                          onClick={async () => {
                            if (!confirm(`Reset eval "${e.slug}" to seed defaults?`)) return;
                            await api.resetEval(e.slug); await load();
                          }}>reset</button>
                )}
                <button className="btn btn-danger" onClick={() => remove(e.slug)}
                        data-testid={`evals-delete-${e.slug}`}>delete</button>
              </div>
            </div>
            {results[e.slug] && (
              <div className="mt-4 border-t border-line pt-3">
                <div className="text-sm">
                  score: <span className="text-2xl font-bold text-ok">{(results[e.slug].score * 100).toFixed(0)}%</span>
                  <span className="text-muted text-xs ml-2">over {results[e.slug].cases.length} cases</span>
                </div>
                <div className="mt-2 space-y-3">
                  {results[e.slug].cases.map((c: any) => {
                    // legacy shape: {input, expected, passed} — render the old way
                    if (c.input !== undefined && c.steps === undefined) {
                      return (
                        <div key={c.i} className="flex gap-3 text-xs border-b border-line py-1">
                          <span className={c.passed ? "text-ok" : "text-err"}>{c.passed ? "✓" : "✗"}</span>
                          <span className="font-mono flex-1 truncate">{c.input}</span>
                          <span className="text-muted">→ {c.expected}</span>
                        </div>
                      );
                    }
                    // new shape: {name, context, steps:[{prompt, asserts:[...]}]}
                    return (
                      <div key={c.i} className="border border-line rounded p-3">
                        <div className="flex items-center gap-2 mb-2 text-sm">
                          <span className={c.passed ? "text-ok" : "text-err"}>{c.passed ? "✓" : "✗"}</span>
                          <span className="font-semibold">{c.name || `case ${c.i + 1}`}</span>
                          <span className="badge badge-info">ctx: {c.context}</span>
                          <span className="text-muted text-xs">{c.steps.length} step(s)</span>
                        </div>
                        {c.steps.map((st: any) => (
                          <div key={st.i} className="ml-4 mb-2 pl-3 border-l border-line">
                            <div className="flex items-center gap-2 text-xs mb-1">
                              <span className={st.passed ? "text-ok" : "text-err"}>{st.passed ? "✓" : "✗"}</span>
                              <span className="text-muted">step {st.i + 1} —</span>
                              <span className="font-mono flex-1 truncate">{st.prompt}</span>
                              {st.run_id && <a href={`/runs/${st.run_id}`} className="text-xs">view run ↗</a>}
                            </div>
                            <div className="ml-4 grid grid-cols-1 gap-1">
                              {st.asserts.map((a: any, ai: number) => (
                                <div key={ai} className="flex gap-2 text-xs">
                                  <span className={a.passed ? "text-ok" : "text-err"}>{a.passed ? "✓" : "✗"}</span>
                                  <span className="kbd text-[10px]">{a.kind}</span>
                                  <span className={`flex-1 truncate ${a.passed ? "text-muted" : "text-err"}`}>{a.detail}</span>
                                </div>
                              ))}
                            </div>
                          </div>
                        ))}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        ))}
      </div>

      <Modal open={open} onClose={() => setOpen(false)}
             title={editingSlug ? `Edit eval: ${editingSlug}` : "New eval"}
             footer={<>
               <button className="btn" onClick={() => setOpen(false)}>cancel</button>
               <button className="btn btn-primary" onClick={save} disabled={saving}
                       data-testid="evals-form-save">
                 {saving ? "saving..." : (editingSlug ? "save" : "create")}
               </button>
             </>}>
        {error && <div className="codebox text-err mb-3">{error}</div>}
        <FormRow label="slug">
          <input value={form.slug} onChange={e => setForm({ ...form, slug: e.target.value })}
                 disabled={!!editingSlug}
                 data-testid="evals-form-slug" />
        </FormRow>
        <FormRow label="name">
          <input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} />
        </FormRow>
        <FormRow label="description">
          <input value={form.description} onChange={e => setForm({ ...form, description: e.target.value })} />
        </FormRow>
        <div className="grid grid-cols-2 gap-3">
          <FormRow label="target kind">
            <select value={form.target_kind}
                    onChange={e => setForm({ ...form, target_kind: e.target.value })}>
              <option value="agent">agent</option>
              <option value="workflow">workflow</option>
            </select>
          </FormRow>
          <FormRow label="target slug">
            <select value={form.target_slug}
                    onChange={e => setForm({ ...form, target_slug: e.target.value })}
                    data-testid="evals-form-target-slug">
              {targetOptions.map(o => (
                <option key={o.slug} value={o.slug}>{o.slug} — {o.name}</option>
              ))}
            </select>
          </FormRow>
        </div>
        <FormRow label="dataset (JSON array)">
          <textarea rows={6} value={form.dataset_json}
                    onChange={e => setForm({ ...form, dataset_json: e.target.value })} />
        </FormRow>
        <div className="grid grid-cols-2 gap-3">
          <FormRow label="metric">
            <select value={form.metric}
                    onChange={e => setForm({ ...form, metric: e.target.value })}>
              <option value="assert_contains">assert_contains</option>
              <option value="judge_llm">judge_llm</option>
              <option value="cmd_returns_zero">cmd_returns_zero</option>
              <option value="tool_sequence_match">tool_sequence_match</option>
            </select>
          </FormRow>
          <FormRow label="metric args (JSON)">
            <input value={form.metric_args_json}
                   onChange={e => setForm({ ...form, metric_args_json: e.target.value })} />
          </FormRow>
        </div>
      </Modal>
    </Page>
  );
}
