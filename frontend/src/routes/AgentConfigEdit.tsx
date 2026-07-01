import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import Page from "../components/Page";
import { api, type AgentConfig } from "../lib/api";
import { PERMISSION_DEFS } from "../lib/permissionDefs";

const BLANK: AgentConfig = {
  slug: "", name: "", description: "", mcp_config: {}, extra_volumes: [], permissions: {},
};

export default function AgentConfigEdit() {
  const { slug } = useParams<{ slug: string }>();
  const isNew = slug === "new";
  const nav = useNavigate();
  const [c, setC] = useState<AgentConfig | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [editedSlug, setEditedSlug] = useState("");

  useEffect(() => {
    if (!slug) return;
    if (isNew) {
      setC({ ...BLANK });
    } else {
      api.getAgentConfig(slug).then(c => { setC(c); setEditedSlug(c.slug); });
    }
  }, [slug, isNew]);

  if (!c) return <Page title="Agent config">…loading…</Page>;

  async function save() {
    if (!c) return;
    setSaving(true); setError("");
    try {
      if (isNew) {
        const created = await api.createAgentConfig({
          slug: c.slug || undefined, name: c.name || c.slug, description: c.description,
          mcp_config: c.mcp_config, extra_volumes: c.extra_volumes, permissions: c.permissions || {},
        });
        nav(`/agent-configs/${created.slug}`);
      } else if (slug) {
        await api.saveAgentConfig(slug, {
          name: c.name, description: c.description,
          mcp_config: c.mcp_config, extra_volumes: c.extra_volumes, permissions: c.permissions || {},
        });
        if (editedSlug.trim() && editedSlug !== slug) {
          // slug rename isn't supported server-side yet — surface that instead of silently ignoring
          setError("Renaming the slug isn't supported yet — create a new config instead.");
        }
      }
    } catch (e: any) { setError(String(e.message || e)); }
    finally { setSaving(false); }
  }

  async function remove() {
    if (!slug || isNew) return;
    if (!confirm(`Delete agent config "${slug}"?`)) return;
    try { await api.deleteAgentConfig(slug); nav("/agent-configs"); }
    catch (e: any) { setError(String(e.message || e)); }
  }

  return (
    <Page title={isNew ? "New agent config" : c.name}
          subtitle={isNew ? "Define a reusable Permissions + Extra volumes + MCP servers bundle." : c.description}
          actions={
            <>
              <Link to="/agent-configs" className="btn">← back</Link>
              {!isNew && (
                <button className="btn btn-danger" onClick={remove}>delete</button>
              )}
              <button className="btn btn-primary" onClick={save} disabled={saving}>
                {saving ? "saving..." : (isNew ? "create" : "save")}
              </button>
            </>
          }>
      {error && <div className="codebox text-err mb-3">{error}</div>}
      <div className="space-y-4 max-w-3xl">
        <div className="card">
          <h2 className="text-base font-semibold mb-3">Configuration</h2>
          <label className="block text-xs text-muted mb-1">name</label>
          <input value={c.name} onChange={e => setC({ ...c, name: e.target.value })} />
          <label className="block text-xs text-muted mt-3 mb-1">slug</label>
          {isNew ? (
            <input value={c.slug} onChange={e => setC({ ...c, slug: e.target.value })}
                   placeholder="(leave blank to auto-generate)" className="font-mono" />
          ) : (
            <input value={editedSlug} onChange={e => setEditedSlug(e.target.value)}
                   className="font-mono" disabled title="rename not supported yet" />
          )}
          <label className="block text-xs text-muted mt-3 mb-1">description</label>
          <textarea rows={2} value={c.description} onChange={e => setC({ ...c, description: e.target.value })} />
        </div>

        {/* ───── Permissions ───── */}
        <div className="card">
          <h2 className="text-base font-semibold mb-1">Permissions</h2>
          <p className="text-xs text-muted mb-3">
            Each permission is applied when an agent using this config runs in a container — translated to volume mounts, environment variables, or other config as needed.
          </p>
          <div className="space-y-2">
            {PERMISSION_DEFS.map(({ key, label, description, defaultOn }) => (
              <label key={key} className="flex items-start gap-2 cursor-pointer select-none">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={defaultOn ? (c.permissions || {})[key] !== false : !!(c.permissions || {})[key]}
                  onChange={e => {
                    const next = { ...(c.permissions || {}), [key]: e.target.checked };
                    if (!e.target.checked && !defaultOn) delete next[key];
                    setC({ ...c, permissions: next });
                  }}
                />
                <span>
                  <span className="text-sm font-medium">{label}</span>
                  <span className="block text-xs text-muted">{description}</span>
                </span>
              </label>
            ))}
          </div>
        </div>

        {/* ───── Extra Volumes ───── */}
        <div className="card">
          <h2 className="text-base font-semibold mb-1">Extra volumes</h2>
          <p className="text-xs text-muted mb-3">
            Docker volume mounts injected when an agent using this config runs in a container.
            One entry per line in <code>host:container</code> format
            (e.g. <code>/var/run/docker.sock:/var/run/docker.sock</code>).
          </p>
          <textarea rows={4}
                    className="font-mono text-xs"
                    placeholder="/var/run/docker.sock:/var/run/docker.sock"
                    value={(c.extra_volumes || []).join("\n")}
                    onChange={e => {
                      const lines = e.target.value.split("\n").map(l => l.trim()).filter(Boolean);
                      setC({ ...c, extra_volumes: lines });
                    }} />
        </div>

        {/* ───── MCP Config ───── */}
        <div className="card">
          <h2 className="text-base font-semibold mb-1">MCP servers</h2>
          <p className="text-xs text-muted mb-3">
            When an agent using this config runs in a Docker container, these servers are injected via{" "}
            <code>--mcp-config</code>. Use <code>http://host.docker.internal:9123/mcp</code>{" "}
            to point at the AW Gateway.
          </p>
          {Object.entries(c.mcp_config?.servers ?? {}).map(([name, srv]) => (
            <div key={name} className="border border-line rounded p-3 mb-2 space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-xs font-mono font-semibold">{name}</span>
                <button className="btn btn-danger text-xs py-0.5 px-2"
                  onClick={() => {
                    const next = { ...c.mcp_config, servers: { ...(c.mcp_config?.servers ?? {}) } };
                    delete next.servers![name];
                    setC({ ...c, mcp_config: next });
                  }}>remove</button>
              </div>
              <div>
                <label className="block text-xs text-muted mb-0.5">URL</label>
                <input className="text-xs font-mono" value={srv.url}
                  onChange={e => {
                    const next = { ...c.mcp_config, servers: { ...(c.mcp_config?.servers ?? {}), [name]: { ...srv, url: e.target.value } } };
                    setC({ ...c, mcp_config: next });
                  }} />
              </div>
              <div>
                <label className="block text-xs text-muted mb-0.5">Authorization header (Bearer token)</label>
                <input className="text-xs font-mono"
                  value={srv.headers?.["Authorization"]?.replace("Bearer ", "") ?? ""}
                  placeholder="paste token here"
                  onChange={e => {
                    const tok = e.target.value.trim();
                    const h: Record<string, string> = tok ? { Authorization: `Bearer ${tok}` } : {};
                    const next = { ...c.mcp_config, servers: { ...(c.mcp_config?.servers ?? {}), [name]: { ...srv, headers: h } } };
                    setC({ ...c, mcp_config: next });
                  }} />
              </div>
            </div>
          ))}
          <button className="btn text-xs mt-1"
            onClick={() => {
              const newName = `server-${Date.now()}`;
              const next = { servers: { ...(c.mcp_config?.servers ?? {}), [newName]: { type: "streamable-http", url: "http://host.docker.internal:9123/mcp", headers: {} } } };
              setC({ ...c, mcp_config: next });
            }}>+ add server</button>
        </div>
      </div>
    </Page>
  );
}
