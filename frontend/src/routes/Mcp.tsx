import { useEffect, useState } from "react";
import Page from "../components/Page";
import Modal, { FormRow } from "../components/Modal";
import { api, type McpServer } from "../lib/api";

const BLANK = { name: "", command: "", args_text: "", env_text: "{}", enabled: true };

export default function McpPage() {
  const [list, setList] = useState<McpServer[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [editingName, setEditingName] = useState<string | null>(null);
  const [form, setForm] = useState<any>(BLANK);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  async function load() { setList(await api.listMcpServers()); }
  useEffect(() => { load(); }, []);

  async function refresh() {
    setBusy("refresh");
    setList(await api.refreshMcp());
    setBusy(null);
  }
  async function discover(name: string) {
    setBusy(name);
    await api.discoverMcpTools(name);
    await load();
    setBusy(null);
  }
  async function remove(name: string) {
    if (!confirm(`Delete MCP server "${name}"?`)) return;
    await api.deleteMcpServer(name);
    await load();
  }

  function openCreate() {
    setEditingName(null); setForm(BLANK); setError(""); setOpen(true);
  }
  function openEdit(srv: McpServer) {
    setEditingName(srv.name);
    setForm({
      name: srv.name, command: srv.command,
      args_text: (srv.args || []).join(" "),
      env_text: JSON.stringify(srv.env || {}, null, 2),
      enabled: srv.enabled,
    });
    setError(""); setOpen(true);
  }

  async function save() {
    setSaving(true); setError("");
    try {
      const args = form.args_text.trim() ? form.args_text.trim().split(/\s+/) : [];
      const env = JSON.parse(form.env_text || "{}");
      const payload = { name: form.name, command: form.command, args, env, enabled: form.enabled };
      if (editingName) {
        await api.updateMcpServer(editingName, payload);
      } else {
        await api.createMcpServer(payload);
      }
      setOpen(false); setEditingName(null); setForm(BLANK); await load();
    } catch (e: any) { setError(String(e.message || e)); }
    finally { setSaving(false); }
  }

  return (
    <Page title="MCP" subtitle="MCP servers from .mcp.json + custom ones you add here"
          actions={<>
            <button className="btn" onClick={refresh} disabled={busy === "refresh"} data-testid="mcp-refresh">
              {busy === "refresh" ? "refreshing..." : "refresh from .mcp.json"}
            </button>
            <button className="btn btn-primary" onClick={openCreate} data-testid="mcp-new">
              + new server
            </button>
          </>}>
      <div className="space-y-4">
        {list.map(srv => (
          <div key={srv.name} className="card" data-testid={`mcp-${srv.name}`}>
            <div className="flex items-center justify-between">
              <div>
                <div className="text-base font-semibold flex items-center gap-2">
                  {srv.name}
                  <span className={`badge ${srv.source === "manual" ? "badge-warn" : "badge-info"}`}>
                    {srv.source}
                  </span>
                </div>
                <div className="text-xs text-muted font-mono">{srv.command} {srv.args.join(" ")}</div>
              </div>
              <div className="flex items-center gap-2">
                <span className={`badge ${srv.enabled ? "badge-success" : "badge-pending"}`}>
                  {srv.enabled ? "enabled" : "disabled"}
                </span>
                <button className="btn" onClick={() => discover(srv.name)} disabled={busy !== null}>
                  {busy === srv.name ? "discovering..." : "discover tools"}
                </button>
                <button className="btn" onClick={() => openEdit(srv)}
                        data-testid={`mcp-edit-${srv.name}`}>edit</button>
                <button className="btn btn-danger" onClick={() => remove(srv.name)}
                        data-testid={`mcp-delete-${srv.name}`}>delete</button>
              </div>
            </div>
            {srv.discovered_tools.length > 0 && (
              <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2 text-xs">
                {srv.discovered_tools.map((t: any) => (
                  <div key={t.name} className="border border-line rounded p-2">
                    <div className="font-mono">{t.name}</div>
                    <div className="text-muted line-clamp-2">{t.description}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      <Modal open={open} onClose={() => setOpen(false)}
             title={editingName ? `Edit MCP server: ${editingName}` : "Add MCP server"}
             footer={<>
               <button className="btn" onClick={() => setOpen(false)}>cancel</button>
               <button className="btn btn-primary" onClick={save} disabled={saving}
                       data-testid="mcp-form-save">
                 {saving ? "saving..." : (editingName ? "save" : "create")}
               </button>
             </>}>
        {error && <div className="codebox text-err mb-3">{error}</div>}
        <FormRow label="name" hint="unique identifier (used in MCP tool ids)">
          <input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })}
                 disabled={!!editingName}
                 data-testid="mcp-form-name" />
        </FormRow>
        <FormRow label="command" hint="executable on PATH (e.g. python, npx, node)">
          <input value={form.command} onChange={e => setForm({ ...form, command: e.target.value })}
                 data-testid="mcp-form-command" />
        </FormRow>
        <FormRow label="args" hint="space-separated arguments">
          <input value={form.args_text} onChange={e => setForm({ ...form, args_text: e.target.value })}
                 placeholder="-y @modelcontextprotocol/server-filesystem /tmp" />
        </FormRow>
        <FormRow label="env (JSON)" hint='environment variables, e.g. {"GITHUB_TOKEN": "..."}'>
          <textarea rows={4} value={form.env_text}
                    onChange={e => setForm({ ...form, env_text: e.target.value })} />
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
