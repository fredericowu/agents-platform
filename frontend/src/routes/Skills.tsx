import { useEffect, useState } from "react";
import Page from "../components/Page";
import Modal, { FormRow } from "../components/Modal";
import { api, type Skill } from "../lib/api";

const BLANK = { slug: "", name: "", description: "", content: "# My Skill\n\nDescription of when to use this skill..." };

export default function SkillsPage() {
  const [list, setList] = useState<Skill[]>([]);
  const [open, setOpen] = useState(false);
  const [editingSlug, setEditingSlug] = useState<string | null>(null);
  const [form, setForm] = useState<any>(BLANK);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  async function load() { setList(await api.listSkills()); }
  useEffect(() => { load(); }, []);

  async function remove(slug: string, source?: string) {
    const msg = source === "file" || source === "override"
      ? `Delete (hide) skill "${slug}"? The underlying file is not touched — you can "reset" to restore.`
      : `Delete skill "${slug}"?`;
    if (!confirm(msg)) return;
    try { await api.deleteSkill(slug); await load(); }
    catch (e: any) { alert(e.message || e); }
  }
  async function reset(slug: string) {
    if (!confirm(`Reset "${slug}" to its file-system version? Your override will be removed.`)) return;
    try { await api.resetSkill(slug); await load(); }
    catch (e: any) { alert(e.message || e); }
  }

  function openCreate() {
    setEditingSlug(null); setForm(BLANK); setError(""); setOpen(true);
  }
  async function openEdit(sk: Skill) {
    const full = await api.getSkill(sk.slug);
    setEditingSlug(sk.slug);
    setForm({ slug: sk.slug, name: sk.name, description: sk.description,
              content: full.content || "" });
    setError(""); setOpen(true);
  }

  async function save() {
    setSaving(true); setError("");
    try {
      if (editingSlug) {
        await api.updateSkill(editingSlug, { name: form.name, description: form.description, content: form.content });
      } else {
        await api.createSkill({ slug: form.slug, name: form.name || form.slug,
                                description: form.description, content: form.content });
      }
      setOpen(false); setEditingSlug(null); setForm(BLANK); await load();
    } catch (e: any) { setError(String(e.message || e)); }
    finally { setSaving(false); }
  }

  return (
    <Page title="Skills"
          subtitle={`${list.length} skill(s) — file-system + custom`}
          actions={
            <button className="btn btn-primary" onClick={openCreate} data-testid="skills-new">
              + new skill
            </button>
          }>
      <div className="grid grid-cols-2 gap-4">
        {list.map(sk => {
          const sourceClass = sk.source === "override" ? "badge-warn"
                            : sk.source === "custom"   ? "badge-warn"
                            : "badge-info";
          return (
          <div key={sk.slug} className="card" data-testid={`skill-${sk.slug}`}>
            <div className="flex items-center justify-between mb-1">
              <div className="text-base font-semibold">{sk.name}</div>
              <span className={`badge ${sourceClass}`}>{sk.source || "file"}</span>
            </div>
            <div className="text-xs text-muted mt-1">{sk.description}</div>
            <div className="text-xs text-muted mt-2 font-mono">{sk.path}</div>
            <div className="flex gap-2 mt-3">
              <button className="btn" onClick={() => openEdit(sk)} data-testid={`skill-edit-${sk.slug}`}>
                edit
              </button>
              {sk.source === "override" && (
                <button className="btn" onClick={() => reset(sk.slug)}
                        data-testid={`skill-reset-${sk.slug}`}>reset to file</button>
              )}
              <button className="btn btn-danger" onClick={() => remove(sk.slug, sk.source)}
                      data-testid={`skill-delete-${sk.slug}`}>delete</button>
            </div>
          </div>
          );
        })}
      </div>

      <Modal open={open} onClose={() => setOpen(false)}
             title={editingSlug ? `Edit skill: ${editingSlug}` : "New skill"}
             footer={<>
               <button className="btn" onClick={() => setOpen(false)}>cancel</button>
               <button className="btn btn-primary" onClick={save} disabled={saving}
                       data-testid="skills-form-save">
                 {saving ? "saving..." : (editingSlug ? "save" : "create")}
               </button>
             </>}>
        {error && <div className="codebox text-err mb-3">{error}</div>}
        <FormRow label="slug">
          <input value={form.slug} onChange={e => setForm({ ...form, slug: e.target.value })}
                 disabled={!!editingSlug}
                 data-testid="skills-form-slug" />
        </FormRow>
        <FormRow label="name">
          <input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} />
        </FormRow>
        <FormRow label="description (one-line)">
          <input value={form.description} onChange={e => setForm({ ...form, description: e.target.value })} />
        </FormRow>
        <FormRow label="content (SKILL.md body)">
          <textarea rows={14} value={form.content}
                    onChange={e => setForm({ ...form, content: e.target.value })}
                    className="font-mono text-xs"
                    data-testid="skills-form-content" />
        </FormRow>
      </Modal>
    </Page>
  );
}
