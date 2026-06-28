import { useEffect, useState } from "react";
import Page from "../components/Page";
import Modal, { FormRow } from "../components/Modal";
import { api, type Model } from "../lib/api";

type ProviderInfo = Record<string, { label: string; fields: string[]; env?: string[]; kind?: string }>;

const KIND_BADGE_CLASS: Record<string, string> = {
  api: "badge-success",      // platform tool-binding via API (LangChain)
  cli: "badge-warn",         // CLI Docker container — native tools
  stub: "badge",             // echo / fake — no LLM
};

const BLANK = {
  slug: "", provider: "echo", model_id: "", display_name: "",
  params_json: "{}", enabled: true,
};

export default function Models() {
  const [list, setList] = useState<Model[]>([]);
  const [providers, setProviders] = useState<ProviderInfo>({});
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [editingSlug, setEditingSlug] = useState<string | null>(null);  // null = create mode
  const [form, setForm] = useState<any>(BLANK);
  const [error, setError] = useState<string>("");
  const [saving, setSaving] = useState(false);

  async function load() {
    setList(await api.listModels());
    try { setProviders(await api.providerInfo()); } catch { /* ignore */ }
  }
  useEffect(() => { load(); }, []);

  async function toggle(slug: string, enabled: boolean) {
    const m = await api.updateModel(slug, { enabled });
    setList(list => list.map(x => x.slug === slug ? m : x));
  }

  async function remove(slug: string) {
    if (!confirm(`Delete model "${slug}"?`)) return;
    await api.deleteModel(slug);
    await load();
  }

  function openCreate() {
    setEditingSlug(null); setForm(BLANK); setError(""); setOpen(true);
  }

  function openEdit(m: Model) {
    setEditingSlug(m.slug);
    setForm({
      slug: m.slug, provider: m.provider, model_id: m.model_id,
      display_name: m.display_name,
      params_json: JSON.stringify(m.params || {}, null, 2),
      enabled: m.enabled,
    });
    setError(""); setOpen(true);
  }

  async function save() {
    setSaving(true); setError("");
    try {
      const params = JSON.parse(form.params_json || "{}");
      if (editingSlug) {
        await api.updateModel(editingSlug, {
          display_name: form.display_name, params, enabled: form.enabled,
          model_id: form.model_id, provider: form.provider,
        });
      } else {
        await api.createModel({
          slug: form.slug, provider: form.provider, model_id: form.model_id,
          display_name: form.display_name || form.slug,
          params, enabled: form.enabled,
        });
      }
      setOpen(false); setEditingSlug(null); setForm(BLANK); await load();
    } catch (e: any) { setError(String(e.message || e)); }
    finally { setSaving(false); }
  }

  const filtered = list.filter(m =>
    `${m.display_name} ${m.provider} ${m.model_id} ${m.slug}`.toLowerCase().includes(q.toLowerCase()));

  return (
    <Page title="Models"
          subtitle={`${list.length} model(s) across ${new Set(list.map(m => m.provider)).size} provider(s)`}
          actions={<>
            <input className="w-64" placeholder="search..." value={q}
                   onChange={e => setQ(e.target.value)} data-testid="models-search" />
            <button className="btn btn-primary" onClick={openCreate} data-testid="models-new">
              + new model
            </button>
          </>}>
      <div className="card overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-muted uppercase">
              <th className="py-2 pr-2">slug</th>
              <th className="py-2 pr-2">provider</th>
              <th className="py-2 pr-2">model id</th>
              <th className="py-2 pr-2">name</th>
              <th className="py-2 pr-2">enabled</th>
              <th className="py-2 pr-2"></th>
            </tr>
          </thead>
          <tbody>
            {filtered.map(m => (
              <tr key={m.slug} className="border-t border-line" data-testid={`models-row-${m.slug}`}>
                <td className="py-2 pr-2 font-mono">{m.slug}</td>
                <td className="py-2 pr-2">
                  <span className="badge badge-info">{m.provider}</span>
                  {providers[m.provider]?.kind && (
                    <span className={`badge ${KIND_BADGE_CLASS[providers[m.provider].kind!] || "badge"} ml-1`}
                          title={providers[m.provider].kind === "api"
                                 ? "tool calls via LangChain (API direct)"
                                 : providers[m.provider].kind === "cli"
                                 ? "tool calls run inside Docker CLI container"
                                 : "no LLM"}>
                      {providers[m.provider].kind}
                    </span>
                  )}
                </td>
                <td className="py-2 pr-2 font-mono text-muted">{m.model_id}</td>
                <td className="py-2 pr-2">{m.display_name}</td>
                <td className="py-2 pr-2">
                  <label className="inline-flex items-center gap-2">
                    <input type="checkbox" className="w-auto" checked={m.enabled}
                           onChange={e => toggle(m.slug, e.target.checked)} />
                    <span className="text-muted text-xs">{m.enabled ? "on" : "off"}</span>
                  </label>
                </td>
                <td className="py-2 pr-2 flex gap-2">
                  <button className="btn" onClick={() => openEdit(m)}
                          data-testid={`models-edit-${m.slug}`}>edit</button>
                  <button className="btn btn-danger" onClick={() => remove(m.slug)}
                          data-testid={`models-delete-${m.slug}`}>delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <Modal open={open} onClose={() => setOpen(false)}
             title={editingSlug ? `Edit model: ${editingSlug}` : "New model"}
             footer={<>
               <button className="btn" onClick={() => setOpen(false)}>cancel</button>
               <button className="btn btn-primary" onClick={save} disabled={saving}
                       data-testid="models-form-save">
                 {saving ? "saving..." : (editingSlug ? "save" : "create")}
               </button>
             </>}>
        {error && <div className="codebox text-err mb-3">{error}</div>}
        <FormRow label="slug" hint="unique identifier, e.g. claude-cli-haiku">
          <input value={form.slug} onChange={e => setForm({ ...form, slug: e.target.value })}
                 disabled={!!editingSlug}
                 data-testid="models-form-slug" />
        </FormRow>
        <FormRow label="provider">
          <select value={form.provider} onChange={e => setForm({ ...form, provider: e.target.value })}
                  data-testid="models-form-provider">
            {Object.entries(providers).map(([slug, info]) => (
              <option key={slug} value={slug}>{info.label}</option>
            ))}
          </select>
          {providers[form.provider]?.env?.length ? (
            <div className="text-xs text-warn mt-1">
              needs env: {providers[form.provider].env!.join(", ")}
            </div>
          ) : null}
        </FormRow>
        <FormRow label="model id" hint="for cli_subshell: name passed to --model; for API providers: the provider's model name">
          <input value={form.model_id} onChange={e => setForm({ ...form, model_id: e.target.value })}
                 data-testid="models-form-modelid" />
        </FormRow>
        <FormRow label="display name">
          <input value={form.display_name} onChange={e => setForm({ ...form, display_name: e.target.value })}
                 data-testid="models-form-displayname" />
        </FormRow>
        <FormRow label="params (JSON)" hint={
            providers[form.provider]?.fields?.length
              ? `Typical fields: ${providers[form.provider].fields.join(", ")}`
              : "JSON object with provider-specific parameters"
          }>
          <textarea rows={6} value={form.params_json}
                    onChange={e => setForm({ ...form, params_json: e.target.value })}
                    data-testid="models-form-params" />
        </FormRow>
        <FormRow label="enabled">
          <label className="inline-flex items-center gap-2">
            <input type="checkbox" className="w-auto" checked={form.enabled}
                   onChange={e => setForm({ ...form, enabled: e.target.checked })} />
            <span className="text-muted text-xs">{form.enabled ? "on" : "off"}</span>
          </label>
        </FormRow>
      </Modal>
    </Page>
  );
}
