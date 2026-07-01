import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import Page from "../components/Page";
import Modal, { FormRow } from "../components/Modal";
import { api, type Target } from "../lib/api";
import { Plus, Search, Trash2, Crosshair } from "lucide-react";

const STATUS_BADGE: Record<string, string> = {
  active: "badge-info",
  completed: "badge-success",
  cancelled: "badge",
  abandoned: "badge-warn",
};

const SOURCE_LABEL: Record<string, string> = {
  manual: "manual",
  rally_story: "Rally",
  incident: "incident",
  jira: "Jira",
  github_issue: "GitHub issue",
  github_pr: "GitHub PR",
  loop: "loop",
  other: "other",
};

const BLANK = {
  slug: "",
  name: "",
  description: "",
  source_kind: "manual",
  source_ref: "",
  budget_tokens: "",
  budget_usd: "",
  tags: "",
  notes: "",
};

export default function Targets() {
  const [list, setList] = useState<Target[]>([]);
  const [q, setQ] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<any>(BLANK);
  const [error, setError] = useState<string>("");
  const [saving, setSaving] = useState(false);

  async function load() {
    setList(await api.listTargets({
      status: statusFilter || undefined,
      q: q || undefined,
      limit: 100,
    }));
  }
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [statusFilter]);

  async function remove(slug: string) {
    if (!confirm(`Delete target "${slug}"? (soft delete — can be restored)`)) return;
    await api.deleteTarget(slug);
    await load();
  }

  async function createTarget() {
    setError(""); setSaving(true);
    try {
      const payload: any = {
        slug: form.slug.trim(),
        name: form.name.trim() || form.slug,
        description: form.description,
        source_kind: form.source_kind,
        source_ref: form.source_ref || null,
        budget_tokens: form.budget_tokens ? Number(form.budget_tokens) : null,
        budget_usd: form.budget_usd ? Number(form.budget_usd) : null,
        tags: form.tags ? form.tags.split(",").map((s: string) => s.trim()).filter(Boolean) : [],
        notes: form.notes,
      };
      if (!payload.slug) throw new Error("slug required");
      await api.createTarget(payload);
      setOpen(false);
      setForm(BLANK);
      await load();
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Page
      title="Targets"
      subtitle="Umbrella goals that group a tree of runs. Each Target is a delivery (US, incident, project) you can drill into for a retro."
      actions={
        <button className="btn btn-primary flex items-center gap-2" onClick={() => { setForm(BLANK); setError(""); setOpen(true); }}>
          <Plus size={14} /> New target
        </button>
      }
    >
      <div className="flex items-center gap-3 mb-4">
        <div className="relative flex-1 max-w-md">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
          <input
            className="input pl-9"
            placeholder="Search slug, name, description, source ref…"
            value={q}
            onChange={e => setQ(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") load(); }}
          />
        </div>
        <select className="input w-44" value={statusFilter} onChange={e => setStatusFilter(e.target.value)}>
          <option value="">All statuses</option>
          <option value="active">Active</option>
          <option value="completed">Completed</option>
          <option value="cancelled">Cancelled</option>
          <option value="abandoned">Abandoned</option>
        </select>
      </div>

      {list.length === 0 ? (
        <div className="text-muted text-sm py-12 text-center">
          <Crosshair size={32} className="mx-auto mb-3 opacity-40" />
          No Targets yet. Create one to start tracking deliveries with full run lineage + retro view.
        </div>
      ) : (
        <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-muted text-xs uppercase tracking-wide">
            <tr>
              <th className="text-left py-2 px-2">Target</th>
              <th className="text-left py-2 px-2">Source</th>
              <th className="text-left py-2 px-2">Status</th>
              <th className="text-right py-2 px-2">Budget</th>
              <th className="text-left py-2 px-2">Started</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {list.map(t => (
              <tr key={t.slug} className="border-t border-line hover:bg-bg-3/40">
                <td className="py-2 px-2">
                  <Link to={`/targets/${t.slug}`} className="text-fg hover:text-accent flex items-center gap-2">
                    <Crosshair size={14} className="text-accent" />
                    <span className="font-semibold">{t.name}</span>
                    <span className="text-muted text-xs">{t.slug}</span>
                  </Link>
                  {t.description && (
                    <div className="text-muted text-xs mt-1 line-clamp-1">{t.description}</div>
                  )}
                </td>
                <td className="py-2 px-2 text-xs">
                  <div className="text-muted">{SOURCE_LABEL[t.source_kind] || t.source_kind}</div>
                  {t.source_ref && <div className="font-mono text-fg">{t.source_ref}</div>}
                </td>
                <td className="py-2 px-2">
                  <span className={`badge ${STATUS_BADGE[t.status] || ""}`}>{t.status}</span>
                </td>
                <td className="py-2 px-2 text-right text-xs font-mono text-muted">
                  {t.budget_usd != null ? `$${t.budget_usd}` : "—"}
                  {t.budget_tokens != null && <div>{(t.budget_tokens / 1000).toFixed(0)}k tok</div>}
                </td>
                <td className="py-2 px-2 text-xs text-muted">
                  {new Date(t.started_at).toLocaleString()}
                </td>
                <td className="py-2 px-2 text-right">
                  <button
                    className="btn btn-ghost btn-sm text-muted hover:text-crit"
                    title="Delete (soft)"
                    onClick={() => remove(t.slug)}
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

      <Modal open={open} onClose={() => setOpen(false)} title="Create Target">
        <FormRow label="Slug" hint="kebab-case, e.g. us1924311-acsb-alerts">
          <input className="input" value={form.slug} onChange={e => setForm({ ...form, slug: e.target.value })} />
        </FormRow>
        <FormRow label="Name">
          <input className="input" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} />
        </FormRow>
        <FormRow label="Description">
          <textarea className="input min-h-[60px]" value={form.description} onChange={e => setForm({ ...form, description: e.target.value })} />
        </FormRow>
        <FormRow label="Source">
          <div className="flex gap-2">
            <select className="input flex-1" value={form.source_kind} onChange={e => setForm({ ...form, source_kind: e.target.value })}>
              <option value="manual">manual</option>
              <option value="rally_story">Rally story</option>
              <option value="jira">Jira</option>
              <option value="github_issue">GitHub issue</option>
              <option value="github_pr">GitHub PR</option>
              <option value="incident">incident</option>
              <option value="loop">loop</option>
              <option value="other">other</option>
            </select>
            <input className="input flex-1" placeholder="ref (US1924311, INC-123, ...)" value={form.source_ref} onChange={e => setForm({ ...form, source_ref: e.target.value })} />
          </div>
        </FormRow>
        <FormRow label="Budget (USD)" hint="optional">
          <input className="input" inputMode="decimal" placeholder="20" value={form.budget_usd} onChange={e => setForm({ ...form, budget_usd: e.target.value })} />
        </FormRow>
        <FormRow label="Budget (tokens)" hint="optional">
          <input className="input" inputMode="numeric" placeholder="800000" value={form.budget_tokens} onChange={e => setForm({ ...form, budget_tokens: e.target.value })} />
        </FormRow>
        <FormRow label="Tags" hint="comma-separated">
          <input className="input" placeholder="cat-2, infra, ci" value={form.tags} onChange={e => setForm({ ...form, tags: e.target.value })} />
        </FormRow>
        {error && <div className="text-crit text-xs mt-2">{error}</div>}
        <div className="flex justify-end gap-2 mt-4">
          <button className="btn btn-ghost" onClick={() => setOpen(false)}>Cancel</button>
          <button className="btn btn-primary" onClick={createTarget} disabled={saving}>
            {saving ? "Creating…" : "Create"}
          </button>
        </div>
      </Modal>
    </Page>
  );
}
