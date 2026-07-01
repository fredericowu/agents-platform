import { useEffect, useState, useRef } from "react";
import Page from "../components/Page";
import { api, type CliSession } from "../lib/api";
import { Search, Trash2, TerminalSquare, Pencil, Check, X } from "lucide-react";

const STATUS_BADGE: Record<string, string> = {
  success: "badge-success",
  error: "badge-warn",
  running: "badge-info",
  pending: "badge",
  cancelled: "badge",
};

function InlineEdit({ value, onSave }: { value: string; onSave: (v: string) => Promise<void> }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  function startEdit() {
    setDraft(value);
    setEditing(true);
    setTimeout(() => inputRef.current?.focus(), 0);
  }

  async function commit() {
    setSaving(true);
    try { await onSave(draft); setEditing(false); }
    finally { setSaving(false); }
  }

  if (!editing) {
    return (
      <button
        className="flex items-center gap-1 group text-left"
        onClick={startEdit}
        title="Click to rename"
      >
        <span className={value ? "font-semibold text-fg" : "text-muted italic text-xs"}>
          {value || "unnamed — click to rename"}
        </span>
        <Pencil size={11} className="opacity-0 group-hover:opacity-50 text-muted" />
      </button>
    );
  }

  return (
    <div className="flex items-center gap-1">
      <input
        ref={inputRef}
        className="input text-sm py-0.5 h-7 w-48"
        value={draft}
        onChange={e => setDraft(e.target.value)}
        onKeyDown={e => { if (e.key === "Enter") commit(); if (e.key === "Escape") setEditing(false); }}
        disabled={saving}
      />
      <button className="btn btn-ghost btn-sm text-success" onClick={commit} disabled={saving}><Check size={13} /></button>
      <button className="btn btn-ghost btn-sm text-muted" onClick={() => setEditing(false)}><X size={13} /></button>
    </div>
  );
}

export default function Sessions() {
  const [list, setList] = useState<CliSession[]>([]);
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    try { setList(await api.listSessions({ q: q || undefined, limit: 200 })); }
    finally { setLoading(false); }
  }

  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  async function rename(session_id: string, name: string) {
    await api.updateSession(session_id, { name });
    setList(prev => prev.map(s => s.session_id === session_id ? { ...s, name } : s));
  }

  async function remove(session_id: string) {
    if (!confirm("Remove this session record? (runs are not deleted)")) return;
    await api.deleteSession(session_id);
    setList(prev => prev.filter(s => s.session_id !== session_id));
  }

  return (
    <Page
      title="Sessions"
      subtitle="CLI sessions from claude --resume. Each session groups a chain of runs that share the same conversation context."
    >
      <div className="flex items-center gap-3 mb-4">
        <div className="relative flex-1 max-w-md">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
          <input
            className="input pl-9"
            placeholder="Search session ID or name…"
            value={q}
            onChange={e => setQ(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") load(); }}
          />
        </div>
        <button className="btn btn-ghost btn-sm" onClick={load}>Refresh</button>
      </div>

      {loading ? (
        <div className="text-muted text-sm py-12 text-center">Loading…</div>
      ) : list.length === 0 ? (
        <div className="text-muted text-sm py-12 text-center">
          <TerminalSquare size={32} className="mx-auto mb-3 opacity-40" />
          No sessions yet. Sessions appear here when a CLI run captures a <code>session_id</code>.
        </div>
      ) : (
        <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-muted text-xs uppercase tracking-wide">
            <tr>
              <th className="text-left py-2 px-2">Name / ID</th>
              <th className="text-left py-2 px-2">Runs</th>
              <th className="text-left py-2 px-2">Last run</th>
              <th className="text-left py-2 px-2">Status</th>
              <th className="text-left py-2 px-2">Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {list.map(sess => (
              <tr key={sess.session_id} className="border-t border-line hover:bg-bg-3/40">
                <td className="py-2 px-2">
                  <InlineEdit
                    value={sess.name}
                    onSave={v => rename(sess.session_id, v)}
                  />
                  <div className="font-mono text-xs text-muted mt-0.5">{sess.session_id}</div>
                  {sess.description && (
                    <div className="text-xs text-muted mt-0.5 line-clamp-1">{sess.description}</div>
                  )}
                </td>
                <td className="py-2 px-2 text-center font-mono text-xs">
                  {sess.run_count}
                </td>
                <td className="py-2 px-2 text-xs text-muted">
                  {sess.last_run_at ? new Date(sess.last_run_at).toLocaleString() : "—"}
                </td>
                <td className="py-2 px-2">
                  {sess.last_status ? (
                    <span className={`badge ${STATUS_BADGE[sess.last_status] || "badge"}`}>
                      {sess.last_status}
                    </span>
                  ) : "—"}
                </td>
                <td className="py-2 px-2 text-xs text-muted">
                  {new Date(sess.created_at).toLocaleString()}
                </td>
                <td className="py-2 px-2 text-right">
                  <button
                    className="btn btn-ghost btn-sm text-muted hover:text-crit"
                    title="Remove session record"
                    onClick={() => remove(sess.session_id)}
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
