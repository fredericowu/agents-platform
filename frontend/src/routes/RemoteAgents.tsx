import { useState, useEffect, useRef } from "react";
import {
  Monitor, Server, Apple, Laptop2,
  Circle, Wifi, WifiOff,
  Plus, Trash2, Pencil, Check, X, Copy,
  Terminal, Info, Link2, Play,
  Cpu, MemoryStick, Clock, User, Tag, FolderOpen,
} from "lucide-react";
// ── Types ────────────────────────────────────────────────────────────────────

interface TunnelSpec {
  name: string;
  target_port: number;
  public_port: number;
}

interface RemoteAgent {
  id: string;
  name: string;
  description: string;
  tunnels: TunnelSpec[];
  auto_mount_fuse: boolean;
  connected: boolean;
  info: Record<string, unknown> | null;
  connected_at: number | null;
  created_at: number;
}

interface HistoryEntry {
  cmd: string;
  ts: number;
  stdout?: string;
  stderr?: string;
  exit?: number;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function osIcon(os?: string) {
  const s = (os ?? "").toLowerCase();
  if (s === "windows") return <Monitor size={16} />;
  if (s === "darwin")  return <Apple size={16} />;
  if (s === "linux")   return <Server size={16} />;
  return <Laptop2 size={16} />;
}

function timeAgo(ts: number | null): string {
  if (!ts) return "";
  const diff = Math.floor(Date.now() / 1000) - ts;
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  return `${Math.floor(diff / 3600)}h ago`;
}

function fmtBytes(bytes?: unknown): string {
  if (!bytes || typeof bytes !== "number") return "—";
  const gb = bytes / 1024 / 1024 / 1024;
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${(bytes / 1024 / 1024).toFixed(0)} MB`;
}

function copyText(text: string) {
  navigator.clipboard.writeText(text).catch(() => {});
}

// ── Agent Card ───────────────────────────────────────────────────────────────

function AgentCard({ agent, selected, onClick }: { agent: RemoteAgent; selected: boolean; onClick: () => void }) {
  const info = agent.info ?? {};
  const on = agent.connected;
  return (
    <button
      onClick={onClick}
      style={{
        width: "100%",
        textAlign: "left",
        padding: "10px 12px",
        borderRadius: 8,
        border: `1px solid ${selected ? "var(--accent)" : "var(--line)"}`,
        background: selected ? "rgba(88,166,255,0.08)" : "var(--bg-2)",
        cursor: "pointer",
        transition: "all 120ms",
        display: "flex",
        alignItems: "center",
        gap: 10,
      }}
    >
      <span style={{ color: on ? "var(--fg)" : "var(--muted)", flexShrink: 0 }}>
        {osIcon(info.os as string)}
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontWeight: 600, fontSize: 13, color: "var(--fg)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {agent.name}
        </div>
        <div style={{ fontSize: 12, color: "var(--muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {on ? ((info.hostname as string) || (info.username as string) || "—") : "offline"}
        </div>
      </div>
      <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 3, flexShrink: 0 }}>
        <span style={{ color: on ? "#22c55e" : "var(--bg-3)" }}>
          <Circle size={8} fill="currentColor" />
        </span>
        {on && (info.version as string | undefined) && (
          <span style={{ fontSize: 10, color: "var(--muted)", fontFamily: '"SF Mono", Menlo, Consolas, monospace' }}>
            v{info.version as string}
          </span>
        )}
      </div>
    </button>
  );
}

// ── Create Agent Modal ───────────────────────────────────────────────────────

function CreateAgentModal({ onClose, onCreate }: { onClose: () => void; onCreate: (agent: RemoteAgent) => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setLoading(true);
    try {
      const res = await fetch("/api/remote-agents", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim(), description }),
      });
      onCreate(await res.json());
    } finally {
      setLoading(false);
    }
  }

  return (
    <div style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.7)",
      display: "flex", alignItems: "center", justifyContent: "center", zIndex: 50,
    }}>
      <form onSubmit={submit} style={{
        background: "var(--bg-2)", border: "1px solid var(--line)", borderRadius: 12,
        padding: 24, width: 380, display: "flex", flexDirection: "column", gap: 16,
        boxShadow: "0 20px 60px rgba(0,0,0,0.5)",
      }}>
        <div style={{ fontWeight: 700, fontSize: 16, color: "var(--fg)" }}>New Remote Agent</div>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <label className="section-label">Name</label>
          <input autoFocus placeholder="e.g. Windows Dev Machine" value={name} onChange={e => setName(e.target.value)} />
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <label className="section-label">Description (optional)</label>
          <input placeholder="e.g. Fred's workstation" value={description} onChange={e => setDescription(e.target.value)} />
        </div>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", paddingTop: 4 }}>
          <button type="button" className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn btn-primary" disabled={!name.trim() || loading}>
            {loading ? "Creating…" : "Create"}
          </button>
        </div>
      </form>
    </div>
  );
}

// ── Info Grid row ────────────────────────────────────────────────────────────

function InfoRow({ icon, label, value, mono }: { icon: React.ReactNode; label: string; value?: string | null; mono?: boolean }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 0", borderBottom: "1px solid var(--bg-3)" }}>
      <span style={{ color: "var(--muted)", flexShrink: 0 }}>{icon}</span>
      <span style={{ color: "var(--muted)", fontSize: 12, width: 90, flexShrink: 0 }}>{label}</span>
      <span style={{ color: "var(--fg)", fontSize: 12, fontFamily: mono ? '"SF Mono", Menlo, Consolas, monospace' : undefined, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {value || "—"}
      </span>
    </div>
  );
}

// ── Tunnels section ──────────────────────────────────────────────────────────
//
// Ports this profile exposes via the WS tunnel (see backend api/tunnels.py) —
// no VPN, no second service. Saving PUTs the whole agent (name/description
// kept as-is) so the change takes effect immediately: the backend opens/
// closes the corresponding TCP listeners the moment this request lands, and
// again automatically every time this agent (re)connects.

function TunnelsSection({ agent, onUpdated }: {
  agent: RemoteAgent;
  onUpdated: (agent: RemoteAgent) => void;
}) {
  const [rows, setRows] = useState<TunnelSpec[]>(agent.tunnels);
  const [saving, setSaving] = useState(false);
  const dirty = JSON.stringify(rows) !== JSON.stringify(agent.tunnels);

  function updateRow(i: number, field: keyof TunnelSpec, value: string) {
    setRows(rs => rs.map((r, idx) => idx === i
      ? { ...r, [field]: field === "name" ? value : (Number(value) || 0) }
      : r));
  }

  function addRow() {
    setRows(rs => [...rs, { name: "", target_port: 0, public_port: 0 }]);
  }

  function removeRow(i: number) {
    setRows(rs => rs.filter((_, idx) => idx !== i));
  }

  async function save() {
    setSaving(true);
    try {
      const res = await fetch(`/api/remote-agents/${agent.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: agent.name, description: agent.description, tunnels: rows,
          auto_mount_fuse: agent.auto_mount_fuse,
        }),
      });
      onUpdated(await res.json());
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="card" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div className="section-label">Tunnels</div>
      <div style={{ fontSize: 11, color: "var(--muted)" }}>
        Expose a port on this agent's loopback (e.g. VNC on 5900) through a public port — no VPN, applied immediately on save.
      </div>
      {rows.map((r, i) => (
        <div key={i} style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input placeholder="name (e.g. vnc)" value={r.name}
            onChange={e => updateRow(i, "name", e.target.value)}
            style={{ flex: 1, minWidth: 0 }} />
          <input placeholder="local port" type="number" value={r.target_port || ""}
            onChange={e => updateRow(i, "target_port", e.target.value)}
            style={{ width: 90 }} />
          <span style={{ color: "var(--muted)", fontSize: 12 }}>→</span>
          <input placeholder="public port" type="number" value={r.public_port || ""}
            onChange={e => updateRow(i, "public_port", e.target.value)}
            style={{ width: 90 }} />
          <button className="btn-icon btn-danger" onClick={() => removeRow(i)} title="Remove"><Trash2 size={13} /></button>
        </div>
      ))}
      <div style={{ display: "flex", gap: 8, marginTop: 4 }}>
        <button type="button" className="btn btn-ghost" onClick={addRow}><Plus size={13} />Add tunnel</button>
        {dirty && (
          <button type="button" className="btn btn-primary" onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </button>
        )}
      </div>
    </div>
  );
}

// ── Auto Mount FUSE section ──────────────────────────────────────────────────
//
// Whether src/services/remote_agent_fs_watcher.py should auto-mount this
// profile's filesystem under mnt/<id>/ while it's connected. Saving PUTs the
// whole agent (name/description/tunnels kept as-is) — the watcher picks up
// the change on its next poll cycle (every 5s).

function AutoMountSection({ agent, onUpdated }: {
  agent: RemoteAgent;
  onUpdated: (agent: RemoteAgent) => void;
}) {
  const [saving, setSaving] = useState(false);

  async function toggle() {
    setSaving(true);
    try {
      const res = await fetch(`/api/remote-agents/${agent.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: agent.name, description: agent.description, tunnels: agent.tunnels,
          auto_mount_fuse: !agent.auto_mount_fuse,
        }),
      });
      onUpdated(await res.json());
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="card" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div className="section-label">Auto Mount FUSE</div>
      <div style={{ fontSize: 11, color: "var(--muted)" }}>
        Automatically mount this agent's filesystem under mnt/ while it's connected. Takes effect on the watcher's next poll cycle (~5s).
      </div>
      <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: saving ? "not-allowed" : "pointer" }}>
        <input type="checkbox" checked={agent.auto_mount_fuse} disabled={saving} onChange={toggle} />
        <span style={{ fontSize: 13, color: "var(--fg)" }}>
          {agent.auto_mount_fuse ? "Enabled" : "Disabled"}
        </span>
      </label>
    </div>
  );
}

// ── Agent Info panel ─────────────────────────────────────────────────────────

function AgentInfo({ agent, serverBase, onDelete, onUpdated }: {
  agent: RemoteAgent;
  serverBase: string;
  onDelete: (id: string) => void;
  onUpdated: (agent: RemoteAgent) => void;
}) {
  const info = agent.info ?? {};
  const launchCmd = `aw-remote-agent.exe --server ${serverBase} --profile ${agent.id}`;
  const [copied, setCopied] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState(agent.name);
  const [editDesc, setEditDesc] = useState(agent.description);
  const [editId, setEditId] = useState(agent.id);
  const [saving, setSaving] = useState(false);
  const [editError, setEditError] = useState("");

  function copy() {
    copyText(launchCmd);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  function startEdit() {
    setEditName(agent.name);
    setEditDesc(agent.description);
    setEditId(agent.id);
    setEditError("");
    setEditing(true);
  }

  async function saveEdit() {
    const newId = editId.trim();
    setSaving(true);
    setEditError("");
    try {
      // Full replace — must carry the current tunnels along or this save
      // (name/description/id only) would wipe them out.
      const res = await fetch(`/api/remote-agents/${agent.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...(newId !== agent.id ? { id: newId } : {}),
          name: editName, description: editDesc, tunnels: agent.tunnels,
          auto_mount_fuse: agent.auto_mount_fuse,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setEditError(body.detail || "Failed to save");
        return;
      }
      onUpdated(await res.json());
      setEditing(false);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "flex-start", gap: 14 }}>
        <div style={{
          width: 44, height: 44, borderRadius: 10,
          background: "var(--bg-3)", border: "1px solid var(--line)",
          display: "flex", alignItems: "center", justifyContent: "center",
          color: agent.connected ? "var(--accent)" : "var(--muted)",
          flexShrink: 0,
        }}>
          {osIcon(info.os as string)}
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          {editing ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                <span style={{ fontSize: 11, color: "var(--muted)" }}>Name</span>
                <input value={editName} onChange={e => setEditName(e.target.value)} style={{ fontSize: 15, fontWeight: 700 }} disabled={saving} />
              </label>
              <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                <span style={{ fontSize: 11, color: "var(--muted)" }}>Description</span>
                <input value={editDesc} onChange={e => setEditDesc(e.target.value)} placeholder="Description" disabled={saving} />
              </label>
              <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                <span style={{ fontSize: 11, color: "var(--muted)" }}>Profile Name (value machines connect with, --profile)</span>
                <input value={editId} onChange={e => setEditId(e.target.value)} className="mono" style={{ fontSize: 12 }} disabled={saving} />
              </label>
              {editId.trim() !== agent.id && (
                <div style={{ fontSize: 11, color: "var(--muted)" }}>
                  Renaming breaks any already-connected machine until it's relaunched with the new name.
                </div>
              )}
              {editError && <div style={{ fontSize: 11, color: "var(--danger)" }}>{editError}</div>}
              <div style={{ display: "flex", gap: 6 }}>
                <button className="btn btn-primary" onClick={saveEdit} disabled={saving}><Check size={13} />{saving ? "Saving…" : "Save"}</button>
                <button className="btn btn-ghost" onClick={() => setEditing(false)} disabled={saving}><X size={13} />Cancel</button>
              </div>
            </div>
          ) : (
            <>
              <div style={{ fontWeight: 700, fontSize: 18, color: "var(--fg)", lineHeight: 1.2 }}>{agent.name}</div>
              {agent.description && <div style={{ fontSize: 13, color: "var(--muted)", marginTop: 2 }}>{agent.description}</div>}
              <div style={{ display: "flex", gap: 8, marginTop: 8, alignItems: "center" }}>
                <span className={`badge ${agent.connected ? "badge-ok" : ""}`}>
                  {agent.connected ? <Wifi size={10} /> : <WifiOff size={10} />}
                  {agent.connected ? "Connected" : "Offline"}
                </span>
                <button className="btn-icon" onClick={startEdit} title="Edit"><Pencil size={13} /></button>
                <button className="btn-icon btn-danger" onClick={() => onDelete(agent.id)} title="Delete"><Trash2 size={13} /></button>
              </div>
            </>
          )}
        </div>
      </div>

      {/* Launch command */}
      <div className="card" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <div className="section-label">Launch command</div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <code className="mono" style={{ flex: 1, fontSize: 12, color: "var(--ok)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {launchCmd}
          </code>
          <button className="btn-icon" onClick={copy} title="Copy">
            {copied ? <Check size={14} style={{ color: "var(--ok)" }} /> : <Copy size={14} />}
          </button>
        </div>
      </div>

      {/* Profile Name (the id clients connect with) — edited via the header's Edit above */}
      <div className="card" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <div className="section-label">Profile Name</div>
        <code className="mono" style={{ fontSize: 12, color: "var(--muted)" }}>{agent.id}</code>
      </div>

      <AutoMountSection agent={agent} onUpdated={onUpdated} />

      <TunnelsSection agent={agent} onUpdated={onUpdated} />

      {/* System info (connected only) */}
      {agent.connected && (
        <div className="card" style={{ display: "flex", flexDirection: "column", gap: 0 }}>
          <div className="section-label" style={{ marginBottom: 8 }}>System info</div>
          <InfoRow icon={<Monitor size={13} />}     label="OS"        value={(info.os_version as string) || `${info.os as string} ${info.arch as string}`} />
          <InfoRow icon={<Laptop2 size={13} />}     label="Hostname"  value={info.hostname as string} mono />
          <InfoRow icon={<User size={13} />}        label="User"      value={info.username as string} mono />
          <InfoRow icon={<Cpu size={13} />}         label="CPUs"      value={info.cpus ? `${info.cpus as number} cores` : undefined} />
          <InfoRow icon={<MemoryStick size={13} />} label="RAM"       value={fmtBytes(info.ram_bytes)} />
          <InfoRow icon={<Tag size={13} />}         label="Version"   value={info.version as string} mono />
          <InfoRow icon={<FolderOpen size={13} />}  label="Root dir"  value={(info.root_dir as string) || ((info.os as string) === "windows" ? "C:\\ (unrestricted)" : "/ (unrestricted)")} mono />
          <InfoRow icon={<Clock size={13} />}       label="Connected" value={timeAgo(agent.connected_at)} />
        </div>
      )}
    </div>
  );
}

// ── Terminal ─────────────────────────────────────────────────────────────────

function TerminalPanel({ clientId }: { clientId: string }) {
  const [command, setCommand] = useState("");
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [running, setRunning] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [history]);

  async function run(e: React.FormEvent) {
    e.preventDefault();
    if (!command.trim() || running) return;
    const cmd = command.trim();
    setCommand("");
    setRunning(true);
    setHistory(h => [...h, { cmd, ts: Date.now() }]);
    try {
      const res = await fetch(`/api/clients/${clientId}/exec`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command: cmd, timeout: 30 }),
      });
      const data = await res.json();
      setHistory(h => [...h.slice(0, -1), { cmd, ts: Date.now(), stdout: data.stdout, stderr: data.stderr, exit: data.exit_code }]);
    } catch (err) {
      setHistory(h => [...h.slice(0, -1), { cmd, ts: Date.now(), stderr: String(err), exit: 1 }]);
    } finally {
      setRunning(false);
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div style={{
        flex: 1, overflowY: "auto", padding: 16,
        background: "var(--bg-1)", borderRadius: 8,
        fontFamily: '"SF Mono", Menlo, Consolas, monospace', fontSize: 13,
        display: "flex", flexDirection: "column", gap: 12,
      }}>
        {history.length === 0 && (
          <span style={{ color: "var(--muted)", fontStyle: "italic" }}>Type a command and press Enter</span>
        )}
        {history.map((h, i) => (
          <div key={i}>
            <div style={{ color: "var(--accent)" }}>$ {h.cmd}</div>
            {h.stdout && <pre style={{ margin: "4px 0 0", color: "var(--fg)", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{h.stdout}</pre>}
            {h.stderr && <pre style={{ margin: "4px 0 0", color: "var(--err)", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{h.stderr}</pre>}
            {h.exit !== undefined && h.exit !== 0 && (
              <div style={{ marginTop: 2, fontSize: 11, color: "var(--warn)" }}>exit {h.exit}</div>
            )}
            {h.exit === undefined && running && i === history.length - 1 && (
              <div style={{ color: "var(--muted)", fontStyle: "italic", marginTop: 4 }}>running…</div>
            )}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
      <form onSubmit={run} style={{ display: "flex", gap: 8, paddingTop: 12 }}>
        <span style={{ color: "var(--accent)", alignSelf: "center", fontFamily: "monospace" }}>$</span>
        <input
          style={{ fontFamily: '"SF Mono", Menlo, Consolas, monospace', fontSize: 13 }}
          value={command}
          onChange={e => setCommand(e.target.value)}
          placeholder="powershell command…"
          disabled={running}
          autoFocus
        />
        <button type="submit" className="btn btn-primary" disabled={running}>
          <Play size={13} />{running ? "Running…" : "Run"}
        </button>
      </form>
    </div>
  );
}

// ── WebSocket hook ───────────────────────────────────────────────────────────

function useAgentsWS(setAgents: React.Dispatch<React.SetStateAction<RemoteAgent[]>>) {
  const wsRef = useRef<WebSocket | null>(null);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    let backoff = 1000;

    function connect() {
      if (!aliveRef.current) return;
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${window.location.host}/ws/ui`);
      wsRef.current = ws;

      ws.onopen = () => { backoff = 1000; };

      ws.onmessage = (evt) => {
        let msg: { type: string; agents?: RemoteAgent[]; agent_id?: string; info?: Record<string, unknown>; connected_at?: number };
        try { msg = JSON.parse(evt.data as string); } catch { return; }

        if (msg.type === "ping") {
          ws.send(JSON.stringify({ type: "pong" }));
          return;
        }
        if (msg.type === "snapshot") {
          setAgents(msg.agents ?? []);
          return;
        }
        if (msg.type === "agent_connected") {
          setAgents(prev => prev.map(a =>
            a.id === msg.agent_id
              ? { ...a, connected: true, info: msg.info ?? null, connected_at: msg.connected_at ?? null }
              : a
          ));
          return;
        }
        if (msg.type === "agent_disconnected") {
          setAgents(prev => prev.map(a =>
            a.id === msg.agent_id
              ? { ...a, connected: false, info: null, connected_at: null }
              : a
          ));
        }
      };

      ws.onclose = () => {
        if (!aliveRef.current) return;
        backoff = Math.min(backoff * 1.5, 15000);
        setTimeout(connect, backoff);
      };

      ws.onerror = () => ws.close();
    }

    connect();
    return () => {
      aliveRef.current = false;
      wsRef.current?.close();
    };
  }, []);
}

// ── Root component ───────────────────────────────────────────────────────────

export default function RemoteAgents() {
  const [agents, setAgents] = useState<RemoteAgent[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState("info");
  const [showCreate, setShowCreate] = useState(false);

  const serverBase = `${window.location.protocol.replace("http", "ws")}//${window.location.host}`;

  useAgentsWS(setAgents);

  async function load() {
    try {
      const res = await fetch("/api/remote-agents");
      setAgents(await res.json());
    } catch {}
  }

  useEffect(() => { load(); }, []);

  function onCreate(agent: RemoteAgent) {
    setShowCreate(false);
    setSelected(agent.id);
    setTab("info");
    load();
  }

  async function onDelete(id: string) {
    if (!confirm("Delete this remote agent?")) return;
    await fetch(`/api/remote-agents/${id}`, { method: "DELETE" });
    if (selected === id) setSelected(null);
    load();
  }

  function onUpdated(agent: RemoteAgent) {
    setAgents(a => a.map(x => x.id === agent.id ? agent : x));
  }

  const selectedAgent = agents.find(a => a.id === selected);
  const connectedCount = agents.filter(a => a.connected).length;

  const TABS = [
    { key: "info",     icon: <Info size={13} />,     label: "Info" },
    { key: "terminal", icon: <Terminal size={13} />,  label: "Terminal", disabled: !selectedAgent?.connected },
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden", background: "var(--bg)", color: "var(--fg)" }}>
      {showCreate && <CreateAgentModal onClose={() => setShowCreate(false)} onCreate={onCreate} />}

      <div style={{ display: "flex", flex: 1, overflow: "hidden" }}>
        {/* Left column: agent list */}
        <aside style={{
          width: 240, flexShrink: 0,
          borderRight: "1px solid var(--line)",
          display: "flex", flexDirection: "column",
          background: "var(--bg-1)",
        }}>
          <div style={{ padding: "14px 14px 10px", borderBottom: "1px solid var(--line)" }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
                <Link2 size={15} style={{ color: "var(--accent)" }} />
                <span style={{ fontWeight: 700, fontSize: 14 }}>Agents</span>
              </div>
              <button
                className="btn-icon btn-primary"
                style={{
                  background: "var(--accent)", color: "#0a0f17", borderRadius: 5, padding: "3px 8px",
                  fontSize: 12, fontWeight: 700, border: "none", display: "flex", alignItems: "center", gap: 4,
                }}
                onClick={() => setShowCreate(true)}
              >
                <Plus size={12} />New
              </button>
            </div>
            <div style={{ fontSize: 12, color: "var(--muted)" }}>
              {connectedCount} of {agents.length} connected
            </div>
          </div>

          <nav style={{ flex: 1, overflowY: "auto", padding: 8, display: "flex", flexDirection: "column", gap: 4 }}>
            {agents.length === 0 ? (
              <div style={{ padding: "12px 8px", color: "var(--muted)", fontSize: 13, fontStyle: "italic" }}>
                No agents yet.<br />Click New to create one.
              </div>
            ) : (
              agents.map(a => (
                <AgentCard
                  key={a.id}
                  agent={a}
                  selected={selected === a.id}
                  onClick={() => { setSelected(a.id); setTab("info"); }}
                />
              ))
            )}
          </nav>
        </aside>

        {/* Right column: detail panel */}
        <main style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
          {selectedAgent ? (
            <>
              <div style={{ display: "flex", borderBottom: "1px solid var(--line)", padding: "0 16px", background: "var(--bg-1)" }}>
                {TABS.map(({ key, icon, label, disabled }) => (
                  <button
                    key={key}
                    onClick={() => !(disabled as boolean | undefined) && setTab(key)}
                    disabled={!!(disabled as boolean | undefined)}
                    style={{
                      display: "flex", alignItems: "center", gap: 6,
                      padding: "10px 14px", fontSize: 13, fontWeight: 500,
                      background: "transparent", border: "none",
                      borderBottom: `2px solid ${tab === key ? "var(--accent)" : "transparent"}`,
                      color: tab === key ? "var(--fg)" : (disabled as boolean | undefined) ? "var(--bg-3)" : "var(--muted)",
                      cursor: (disabled as boolean | undefined) ? "not-allowed" : "pointer",
                      transition: "color 120ms",
                      marginBottom: -1,
                    }}
                  >
                    {icon}{label}
                  </button>
                ))}
              </div>
              <div style={{ flex: 1, overflowY: "auto", padding: 24 }}>
                {tab === "info" && (
                  <AgentInfo
                    key={selectedAgent.id}
                    agent={selectedAgent}
                    serverBase={serverBase}
                    onDelete={onDelete}
                    onUpdated={onUpdated}
                  />
                )}
                {tab === "terminal" && selectedAgent.connected && (
                  <div style={{ height: "calc(100vh - 200px)", display: "flex", flexDirection: "column" }}>
                    <TerminalPanel clientId={selectedAgent.id} />
                  </div>
                )}
              </div>
            </>
          ) : (
            <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--muted)", fontSize: 14 }}>
              Select an agent to view details
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
