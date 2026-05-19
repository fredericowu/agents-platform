import { useEffect, useState } from "react";
import Page from "../components/Page";
import { FormRow } from "../components/Modal";
import { api, type PlatformSettings, type RagHealth, type RagProviderConfig } from "../lib/api";

export default function Settings() {
  const [s, setS] = useState<PlatformSettings | null>(null);
  const [saving, setSaving] = useState<string>("");   // key currently saving
  const [error, setError] = useState<string>("");
  const [msg, setMsg] = useState<string>("");

  // local editable mirrors (so users can type freely before save)
  const [timeoutStr, setTimeoutStr] = useState("");
  const [allow, setAllow] = useState("");
  const [deny, setDeny]   = useState("");
  const [ragJson, setRagJson] = useState("");
  const [ragHealth, setRagHealth] = useState<RagHealth | null>(null);
  const [ragResyncResult, setRagResyncResult] = useState<string>("");

  const RETRO_DIMS = ["cost", "wall", "mistakes", "lessons_applied", "plan_adherence",
                      "scope_discipline", "accuracy", "output_quality", "recovery"];
  const RETRO_DEFAULTS: Record<string, number> = {
    cost: 0.10, wall: 0.10, mistakes: 0.15, lessons_applied: 0.15,
    plan_adherence: 0.15, scope_discipline: 0.10, accuracy: 0.10,
    output_quality: 0.10, recovery: 0.05,
  };
  const [retroWeights, setRetroWeights] = useState<Record<string, string>>(() =>
    Object.fromEntries(RETRO_DIMS.map(d => [d, String(RETRO_DEFAULTS[d])]))
  );
  const [retroSaving, setRetroSaving] = useState(false);
  const [retroMsg, setRetroMsg] = useState("");

  async function load() {
    setError("");
    try {
      const res = await api.getSettings();
      setS(res);
      setTimeoutStr(String(res.command_timeout_seconds));
      setAllow((res.command_allowlist || []).join("\n"));
      setDeny((res.command_denylist || []).join("\n"));
      setRagJson(JSON.stringify(res.rag_provider || {}, null, 2));
    } catch (e: any) { setError(String(e.message || e)); }
    try {
      const rw = await api.getRetroScoreWeights();
      setRetroWeights(Object.fromEntries(RETRO_DIMS.map(d => [d, String(rw.weights[d] ?? RETRO_DEFAULTS[d])])));
    } catch { /* best-effort */ }
  }

  async function testRag() {
    setRagHealth(null);
    try {
      const h = await api.ragHealth();
      setRagHealth(h);
    } catch (e: any) { setRagHealth({ ok: false, kind: "?", error: String(e.message || e) }); }
  }

  async function resyncRag() {
    setRagResyncResult("syncing…");
    try {
      const r = await api.ragResync();
      setRagResyncResult(`✓ synced ${r.synced} · ${r.failed} failed${r.failed ? " — see server log" : ""}`);
    } catch (e: any) { setRagResyncResult(`✗ ${e.message || e}`); }
  }

  const retroSum = RETRO_DIMS.reduce((acc, d) => acc + (parseFloat(retroWeights[d]) || 0), 0);
  const retroSumOk = Math.abs(retroSum - 1) < 0.011;

  async function saveRetroWeights() {
    setRetroSaving(true); setRetroMsg("");
    try {
      const w = Object.fromEntries(RETRO_DIMS.map(d => [d, parseFloat(retroWeights[d]) || 0]));
      await api.setRetroScoreWeights(w);
      setRetroMsg("saved — applies to next computed retro_score_summary");
      setTimeout(() => setRetroMsg(""), 4000);
    } catch (e: any) { setRetroMsg(`✗ ${e.message || e}`); }
    finally { setRetroSaving(false); }
  }

  function resetRetroWeights() {
    setRetroWeights(Object.fromEntries(RETRO_DIMS.map(d => [d, String(RETRO_DEFAULTS[d])])));
  }

  async function saveRagProvider() {
    setError(""); setMsg("");
    let parsed: RagProviderConfig;
    try {
      parsed = JSON.parse(ragJson);
    } catch (e: any) {
      setError(`rag_provider must be valid JSON: ${e.message || e}`);
      return;
    }
    setSaving("rag_provider");
    try {
      const res = await api.updateSetting("rag_provider", parsed);
      setS(res);
      setRagJson(JSON.stringify(res.rag_provider || {}, null, 2));
      setMsg("saved: rag_provider — test connection to confirm");
      setTimeout(() => setMsg(""), 4000);
    } catch (e: any) { setError(String(e.message || e)); }
    finally { setSaving(""); }
  }
  useEffect(() => { load(); }, []);

  async function save(key: string, value: any) {
    setSaving(key); setError(""); setMsg("");
    try {
      const res = await api.updateSetting(key, value);
      setS(res);
      setMsg(`saved: ${key}`);
      setTimeout(() => setMsg(""), 2500);
    } catch (e: any) { setError(String(e.message || e)); }
    finally { setSaving(""); }
  }

  async function reset() {
    if (!confirm("Reset all settings to platform defaults?")) return;
    setSaving("_reset"); setError(""); setMsg("");
    try {
      const res = await api.resetSettings();
      setS(res);
      setTimeoutStr(String(res.command_timeout_seconds));
      setAllow((res.command_allowlist || []).join("\n"));
      setDeny((res.command_denylist || []).join("\n"));
      setMsg("reset to defaults");
      setTimeout(() => setMsg(""), 2500);
    } catch (e: any) { setError(String(e.message || e)); }
    finally { setSaving(""); }
  }

  if (!s) return <Page title="Settings">…loading…</Page>;

  const isDefaultMode    = s.security_mode === s._defaults?.security_mode;
  const isDefaultTimeout = s.command_timeout_seconds === s._defaults?.command_timeout_seconds;

  return (
    <Page
      title="Settings"
      subtitle="Platform-wide defaults. Agents can override the security mode in their own params."
      actions={
        <button className="btn btn-danger" onClick={reset} disabled={!!saving}
                data-testid="settings-reset">reset all to defaults</button>
      }
    >
      {error && <div className="codebox text-err mb-3" data-testid="settings-error">{error}</div>}
      {msg   && <div className="codebox text-ok mb-3"  data-testid="settings-msg">{msg}</div>}

      {/* ─────── Command execution ─────── */}
      <div className="card mb-4">
        <h2 className="text-base font-semibold mb-1">Command execution</h2>
        <div className="text-xs text-muted mb-3">
          Applies to the <span className="kbd">code.run_command</span> tool.
        </div>

        <FormRow label="timeout (seconds)"
                 hint={`Max wall-time for a single command (5–3600). Default: ${s._defaults?.command_timeout_seconds}s.`}>
          <div className="flex items-center gap-2">
            <input type="number" min={5} max={3600} className="w-32"
                   value={timeoutStr}
                   onChange={e => setTimeoutStr(e.target.value)}
                   data-testid="settings-cmd-timeout" />
            <button className="btn btn-primary"
                    disabled={saving === "command_timeout_seconds" || timeoutStr === String(s.command_timeout_seconds)}
                    onClick={() => save("command_timeout_seconds", parseInt(timeoutStr, 10) || 0)}
                    data-testid="settings-cmd-timeout-save">
              {saving === "command_timeout_seconds" ? "saving..." : "save"}
            </button>
            {!isDefaultTimeout && (
              <span className="text-xs text-muted">overrides default ({s._defaults?.command_timeout_seconds}s)</span>
            )}
          </div>
        </FormRow>
      </div>

      {/* ─────── Security ─────── */}
      <div className="card mb-4">
        <h2 className="text-base font-semibold mb-1">Security</h2>
        <div className="text-xs text-muted mb-3">
          Resolves per agent: <span className="kbd">agent.params.security_mode</span> wins if set, else this global default.
          Agents that don't have <span className="kbd">code.run_command</span> in their tool_specs are unaffected.
        </div>

        <FormRow label="default mode"
                 hint="insecure: deny-list only. secure: allow-list + claude CLI loses bash.">
          <div className="flex items-center gap-2">
            <select value={s.security_mode}
                    onChange={e => save("security_mode", e.target.value)}
                    disabled={saving === "security_mode"}
                    data-testid="settings-security-mode"
                    className="w-48">
              <option value="insecure">insecure (current)</option>
              <option value="secure">secure</option>
            </select>
            {!isDefaultMode && (
              <span className="text-xs text-muted">overrides default ({s._defaults?.security_mode})</span>
            )}
          </div>
        </FormRow>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mt-4">
          <div>
            <FormRow label={`allow-list (${(s.command_allowlist || []).length})`}
                     hint="One entry per line. Prefix match against first command word(s) — e.g. 'git status' matches 'git status -s'. Only enforced when mode=secure.">
              <textarea rows={12}
                        className="font-mono text-xs"
                        value={allow}
                        onChange={e => setAllow(e.target.value)}
                        data-testid="settings-allowlist" />
            </FormRow>
            <button className="btn btn-primary mt-1"
                    disabled={saving === "command_allowlist" ||
                              allow === (s.command_allowlist || []).join("\n")}
                    onClick={() => save("command_allowlist",
                      allow.split("\n").map(l => l.trim()).filter(Boolean))}
                    data-testid="settings-allowlist-save">
              {saving === "command_allowlist" ? "saving..." : "save allow-list"}
            </button>
          </div>
          <div>
            <FormRow label={`deny-list (${(s.command_denylist || []).length})`}
                     hint="Always enforced — even in insecure mode. Each line is a Python regex matched against the full command (case-insensitive).">
              <textarea rows={12}
                        className="font-mono text-xs"
                        value={deny}
                        onChange={e => setDeny(e.target.value)}
                        data-testid="settings-denylist" />
            </FormRow>
            <button className="btn btn-primary mt-1"
                    disabled={saving === "command_denylist" ||
                              deny === (s.command_denylist || []).join("\n")}
                    onClick={() => save("command_denylist",
                      deny.split("\n").map(l => l.trim()).filter(Boolean))}
                    data-testid="settings-denylist-save">
              {saving === "command_denylist" ? "saving..." : "save deny-list"}
            </button>
          </div>
        </div>
      </div>

      {/* ─────── RAG Provider ─────── */}
      <div className="card mb-4">
        <h2 className="text-base font-semibold mb-1">RAG Provider (knowledge-base backend for lessons)</h2>
        <div className="text-xs text-muted mb-3">
          Configures where lessons learned are <b>stored</b> (as markdown documents) and <b>semantically searched</b>.
          The structured side (effectiveness, tags, applications) always stays in the platform's <code>target_lessons</code> table —
          this setting controls the vector / RAG side. Default points at the local <code>loco-knowledge-base</code> via its HTTP API.
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mb-3">
          <div className="card" style={{ padding: 10 }}>
            <div className="text-xs text-muted uppercase">Active kind</div>
            <div className="font-mono mt-1">{s.rag_provider?.kind || "(default)"}</div>
          </div>
          <div className="card" style={{ padding: 10 }}>
            <div className="text-xs text-muted uppercase">Backend</div>
            <div className="font-mono mt-1 text-sm">{s.rag_provider?.name || "loco-knowledge-base"}</div>
            <div className="text-xs text-muted">{s.rag_provider?.base_url}</div>
          </div>
          <div className="card" style={{ padding: 10 }}>
            <div className="text-xs text-muted uppercase">Path prefix</div>
            <div className="font-mono mt-1 text-sm">{s.rag_provider?.lesson_path_prefix || "agent-platform/lessons/"}</div>
          </div>
        </div>

        <FormRow label="rag_provider (JSON)"
                 hint="Edit the full provider config. Fields: kind, base_url, auth, lesson_path_prefix, endpoints. Save then click 'Test connection'.">
          <textarea rows={20}
                    className="font-mono text-xs w-full"
                    value={ragJson}
                    onChange={e => setRagJson(e.target.value)}
                    data-testid="settings-rag-json" />
        </FormRow>

        <div className="flex items-center gap-2 mt-1 flex-wrap">
          <button className="btn btn-primary"
                  disabled={saving === "rag_provider"}
                  onClick={saveRagProvider}
                  data-testid="settings-rag-save">
            {saving === "rag_provider" ? "saving…" : "save rag_provider"}
          </button>
          <button className="btn btn-ghost"
                  onClick={testRag}
                  data-testid="settings-rag-test">test connection</button>
          <button className="btn btn-ghost"
                  onClick={resyncRag}
                  data-testid="settings-rag-resync"
                  title="Re-push every non-deleted lesson into the configured RAG. Use after switching providers.">
            re-sync all lessons
          </button>
          {ragHealth && (
            <span className={`badge ${ragHealth.ok ? "badge-success" : "badge-crit"}`}>
              {ragHealth.ok ? "✓ ok" : "✗ failed"} · {ragHealth.kind}
              {ragHealth.error && ` — ${ragHealth.error}`}
              {!ragHealth.ok && ragHealth.auth_header_set !== undefined &&
                ` · auth: ${ragHealth.auth_header_set ? "set" : "missing"}`}
            </span>
          )}
          {ragResyncResult && (
            <span className="text-xs text-muted">{ragResyncResult}</span>
          )}
        </div>

        <details className="mt-4">
          <summary className="text-xs text-muted cursor-pointer">📘 supported `kind` values + example config</summary>
          <div className="text-xs text-muted mt-2 space-y-2">
            <div><b>disabled</b> — no RAG. Lessons live only in <code>target_lessons</code> SQL table. <code>search_lessons</code> falls back to tag/SQL only.</div>
            <div><b>http</b> — generic HTTP backend with templated endpoints. Default for loco-knowledge-base. Endpoint paths can interpolate <code>$path</code> / <code>$query</code> / <code>$n_results</code> / <code>$content</code>.</div>
            <div><b>mcp</b> — future: dispatch to an MCP server. Not implemented yet.</div>
          </div>
        </details>
      </div>

      {/* ─────── Retro-Score Weights ─────── */}
      <div className="card mb-4" data-testid="settings-retro-weights">
        <h2 className="text-base font-semibold mb-1">Retro-Score Weights</h2>
        <div className="text-xs text-muted mb-3">
          How each dimension contributes to the composite overall score. Must sum to ~1.00.
        </div>

        <div className="grid grid-cols-3 gap-3 mb-3">
          {RETRO_DIMS.map(dim => (
            <div key={dim}>
              <label className="block text-xs text-muted uppercase tracking-wider mb-1">{dim.replace(/_/g, " ")}</label>
              <input
                type="number" min={0} max={1} step={0.01}
                className="w-full font-mono text-sm"
                value={retroWeights[dim]}
                onChange={e => setRetroWeights(w => ({ ...w, [dim]: e.target.value }))}
                data-testid={`retro-weight-${dim}`}
              />
            </div>
          ))}
        </div>

        <div className="flex items-center gap-3 flex-wrap">
          <span className={`text-sm font-mono font-semibold ${retroSumOk ? "text-ok" : "text-err"}`}
                data-testid="retro-weight-sum">
            Σ = {retroSum.toFixed(2)} {retroSumOk ? "✓" : "✗"}
          </span>
          <button className="btn btn-primary"
                  disabled={retroSaving || !retroSumOk}
                  onClick={saveRetroWeights}
                  data-testid="retro-weights-save">
            {retroSaving ? "saving…" : "save weights"}
          </button>
          <button className="btn btn-ghost" onClick={resetRetroWeights}
                  data-testid="retro-weights-reset">
            reset to defaults
          </button>
          {retroMsg && (
            <span className={`text-xs ${retroMsg.startsWith("✗") ? "text-err" : "text-ok"}`}
                  data-testid="retro-weights-msg">{retroMsg}</span>
          )}
        </div>
      </div>
    </Page>
  );
}
