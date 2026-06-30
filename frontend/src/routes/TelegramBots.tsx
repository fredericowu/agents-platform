import { useEffect, useState } from "react";
import Page from "../components/Page";
import Modal, { FormRow } from "../components/Modal";
import { api, type TelegramBot, type Agent } from "../lib/api";

const BLANK = {
  id: "", name: "", token: "", webhook_secret: "",
  enabled: true, agent_slug: "", admin_user_ids: "",
};

export default function TelegramBots() {
  const [list, setList] = useState<TelegramBot[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [open, setOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<any>(BLANK);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [webhookMsg, setWebhookMsg] = useState<Record<string, string>>({});
  const [showToken, setShowToken] = useState<Record<string, boolean>>({});

  async function load() {
    setList(await api.listTelegramBots());
  }
  useEffect(() => {
    load();
    api.listAgents().then(setAgents).catch(() => {});
  }, []);

  function openCreate() {
    setEditingId(null); setForm(BLANK); setError(""); setOpen(true);
  }

  function openEdit(b: TelegramBot) {
    setEditingId(b.id);
    setForm({
      id: b.id,
      name: b.name,
      token: b.token,
      webhook_secret: b.webhook_secret,
      enabled: b.enabled,
      agent_slug: b.agent_slug ?? "",
      admin_user_ids: b.admin_user_ids.join(", "),
    });
    setError(""); setOpen(true);
  }

  async function save() {
    setSaving(true); setError("");
    try {
      const payload = {
        name: form.name,
        token: form.token,
        webhook_secret: form.webhook_secret,
        enabled: form.enabled,
        agent_slug: form.agent_slug || null,
        admin_user_ids: form.admin_user_ids
          .split(",").map((s: string) => s.trim()).filter(Boolean),
      };
      if (editingId) {
        await api.updateTelegramBot(editingId, payload);
      } else {
        await api.createTelegramBot({ ...payload, id: form.id });
      }
      setOpen(false); setEditingId(null); setForm(BLANK); await load();
    } catch (e: any) { setError(String(e.message || e)); }
    finally { setSaving(false); }
  }

  async function remove(id: string) {
    if (!confirm(`Delete bot "${id}"?`)) return;
    await api.deleteTelegramBot(id);
    await load();
  }

  async function registerWebhook(id: string) {
    setWebhookMsg(m => ({ ...m, [id]: "registering…" }));
    try {
      const res = await api.registerTelegramWebhook(id);
      setWebhookMsg(m => ({ ...m, [id]: res.message || "ok" }));
    } catch (e: any) {
      setWebhookMsg(m => ({ ...m, [id]: `error: ${e.message || e}` }));
    }
  }

  function toggleShowToken(id: string) {
    setShowToken(s => ({ ...s, [id]: !s[id] }));
  }

  function maskToken(token: string) {
    if (!token) return "—";
    const parts = token.split(":");
    if (parts.length === 2) return `${parts[0]}:${"•".repeat(Math.min(parts[1].length, 12))}`;
    return "•".repeat(Math.min(token.length, 16));
  }

  return (
    <Page
      title="Telegram Bots"
      subtitle={`${list.length} bot(s) configured`}
      actions={
        <button className="btn btn-primary" onClick={openCreate}>
          + new bot
        </button>
      }
    >
      <div className="card overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-muted uppercase">
              <th className="py-2 pr-3">id</th>
              <th className="py-2 pr-3">name</th>
              <th className="py-2 pr-3">token</th>
              <th className="py-2 pr-3">agent</th>
              <th className="py-2 pr-3">admins</th>
              <th className="py-2 pr-3">enabled</th>
              <th className="py-2 pr-3"></th>
            </tr>
          </thead>
          <tbody>
            {list.map(b => (
              <tr key={b.id} className="border-t border-line align-top">
                <td className="py-2 pr-3 font-mono">{b.id}</td>
                <td className="py-2 pr-3">{b.name || <span className="text-muted">—</span>}</td>
                <td className="py-2 pr-3 font-mono text-xs">
                  <span
                    className="cursor-pointer select-none"
                    title="click to reveal"
                    onClick={() => toggleShowToken(b.id)}
                  >
                    {showToken[b.id] ? b.token : maskToken(b.token)}
                  </span>
                </td>
                <td className="py-2 pr-3">
                  {b.agent_slug
                    ? <span className="badge badge-info">{b.agent_slug}</span>
                    : <span className="text-muted text-xs">—</span>}
                </td>
                <td className="py-2 pr-3 text-xs text-muted">
                  {b.admin_user_ids.length ? b.admin_user_ids.join(", ") : "—"}
                </td>
                <td className="py-2 pr-3">
                  <label className="inline-flex items-center gap-1">
                    <input
                      type="checkbox"
                      className="w-auto"
                      checked={b.enabled}
                      onChange={async e => {
                        await api.updateTelegramBot(b.id, { enabled: e.target.checked });
                        await load();
                      }}
                    />
                    <span className="text-muted text-xs">{b.enabled ? "on" : "off"}</span>
                  </label>
                </td>
                <td className="py-2 pr-3">
                  <div className="flex flex-col gap-1 items-start">
                    <div className="flex gap-1">
                      <button className="btn" onClick={() => openEdit(b)}>edit</button>
                      <button className="btn btn-danger" onClick={() => remove(b.id)}>delete</button>
                      <button className="btn" onClick={() => registerWebhook(b.id)}>
                        register webhook
                      </button>
                    </div>
                    {webhookMsg[b.id] && (
                      <span className={`text-xs ${webhookMsg[b.id].startsWith("error") ? "text-err" : "text-success"}`}>
                        {webhookMsg[b.id]}
                      </span>
                    )}
                  </div>
                </td>
              </tr>
            ))}
            {list.length === 0 && (
              <tr>
                <td colSpan={7} className="py-6 text-center text-muted text-sm">
                  No bots yet. Add one to connect a Telegram bot to an agent.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title={editingId ? `Edit bot: ${editingId}` : "New Telegram bot"}
        footer={
          <>
            <button className="btn" onClick={() => setOpen(false)}>cancel</button>
            <button className="btn btn-primary" onClick={save} disabled={saving}>
              {saving ? "saving…" : editingId ? "save" : "create"}
            </button>
          </>
        }
      >
        {error && <div className="codebox text-err mb-3">{error}</div>}

        <FormRow label="id" hint="unique slug, e.g. aw-17 — cannot change after creation">
          <input
            value={form.id}
            onChange={e => setForm({ ...form, id: e.target.value })}
            disabled={!!editingId}
            placeholder="aw-17"
          />
        </FormRow>

        <FormRow label="name" hint="display name shown in the UI">
          <input
            value={form.name}
            onChange={e => setForm({ ...form, name: e.target.value })}
            placeholder="My Bot"
          />
        </FormRow>

        <FormRow label="token" hint="BotFather API token (kept server-side, never sent to the browser after creation)">
          <input
            value={form.token}
            onChange={e => setForm({ ...form, token: e.target.value })}
            placeholder="123456789:ABCDef..."
            type="text"
          />
        </FormRow>

        <FormRow label="webhook secret" hint="optional HMAC secret — set the same value in BotFather setWebhook">
          <input
            value={form.webhook_secret}
            onChange={e => setForm({ ...form, webhook_secret: e.target.value })}
            placeholder="(optional)"
          />
        </FormRow>

        <FormRow label="agent" hint="AP agent that handles messages from this bot">
          <select
            value={form.agent_slug}
            onChange={e => setForm({ ...form, agent_slug: e.target.value })}
          >
            <option value="">(none)</option>
            {agents.map(ag => (
              <option key={ag.slug} value={ag.slug}>
                {ag.name} ({ag.slug})
              </option>
            ))}
          </select>
        </FormRow>

        <FormRow label="admin user IDs" hint="comma-separated Telegram user IDs allowed to use this bot">
          <input
            value={form.admin_user_ids}
            onChange={e => setForm({ ...form, admin_user_ids: e.target.value })}
            placeholder="1223642032, 987654321"
          />
        </FormRow>

        <FormRow label="enabled">
          <label className="inline-flex items-center gap-2">
            <input
              type="checkbox"
              className="w-auto"
              checked={form.enabled}
              onChange={e => setForm({ ...form, enabled: e.target.checked })}
            />
            <span className="text-muted text-xs">{form.enabled ? "on" : "off"}</span>
          </label>
        </FormRow>
      </Modal>
    </Page>
  );
}
