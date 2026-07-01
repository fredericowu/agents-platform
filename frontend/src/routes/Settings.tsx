import React, { useEffect, useState } from "react";
import { Laptop2, Trash2, Check, Copy, Eye, EyeOff, RefreshCcw, FolderOpen, FileText, Play } from "lucide-react";
import Page from "../components/Page";
import { FormRow } from "../components/Modal";
import { api, type PlatformSettings, type RagHealth, type RagProviderConfig } from "../lib/api";

const OPENAI_VOICE_OPTIONS = [
  { id: "alloy",   label: "Alloy",   desc: "Neutral, balanced" },
  { id: "echo",    label: "Echo",    desc: "Male, calm" },
  { id: "fable",   label: "Fable",   desc: "British, warm" },
  { id: "onyx",    label: "Onyx",    desc: "Deep, authoritative" },
  { id: "nova",    label: "Nova",    desc: "Female, energetic" },
  { id: "shimmer", label: "Shimmer", desc: "Female, soft" },
];

// Curated Edge Neural voice list per language — the configured map
// (setting: edge_voices) is the source of truth at runtime.
const EDGE_VOICES_BY_LANG: Record<string, { id: string; label: string }[]> = {
  pt: [
    { id: "pt-BR-AntonioNeural",   label: "Antônio (BR, male)" },
    { id: "pt-BR-FranciscaNeural", label: "Francisca (BR, female)" },
    { id: "pt-BR-ThalitaNeural",   label: "Thalita (BR, female)" },
    { id: "pt-PT-DuarteNeural",    label: "Duarte (PT, male)" },
    { id: "pt-PT-RaquelNeural",    label: "Raquel (PT, female)" },
  ],
  en: [
    { id: "en-US-AndrewMultilingualNeural", label: "Andrew (US, multilingual)" },
    { id: "en-US-AriaNeural",               label: "Aria (US, female)" },
    { id: "en-US-GuyNeural",                label: "Guy (US, male)" },
    { id: "en-GB-RyanNeural",               label: "Ryan (UK, male)" },
    { id: "en-GB-SoniaNeural",              label: "Sonia (UK, female)" },
  ],
  es: [
    { id: "es-MX-JorgeNeural",  label: "Jorge (MX, male)" },
    { id: "es-MX-DaliaNeural",  label: "Dalia (MX, female)" },
    { id: "es-ES-AlvaroNeural", label: "Álvaro (ES, male)" },
    { id: "es-ES-ElviraNeural", label: "Elvira (ES, female)" },
  ],
  it: [
    { id: "it-IT-DiegoNeural",    label: "Diego (male)" },
    { id: "it-IT-ElsaNeural",     label: "Elsa (female)" },
    { id: "it-IT-IsabellaNeural", label: "Isabella (female)" },
  ],
  fr: [
    { id: "fr-FR-HenriNeural",  label: "Henri (male)" },
    { id: "fr-FR-DeniseNeural", label: "Denise (female)" },
  ],
  de: [
    { id: "de-DE-ConradNeural", label: "Conrad (male)" },
    { id: "de-DE-KatjaNeural",  label: "Katja (female)" },
  ],
};

const EDGE_LANG_LABELS: Record<string, string> = {
  pt: "Portuguese", en: "English", es: "Spanish", it: "Italian", fr: "French", de: "German",
};

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

  // GitHub settings
  const [ghEnabled, setGhEnabled] = useState(false);
  const [ghRepo, setGhRepo] = useState("");
  const [ghSecret, setGhSecret] = useState("");
  const [ghTestResult, setGhTestResult] = useState<{ ok: boolean; output: string } | null>(null);

  // Voice & STT settings
  const [voiceOpenaiKey, setVoiceOpenaiKey] = useState("");

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

  // Remote Agents MCP settings
  const [raApiKey, setRaApiKey] = useState("");
  const [raApiKeyMasked, setRaApiKeyMasked] = useState(true);
  const [raApiKeyCopied, setRaApiKeyCopied] = useState(false);
  const [raRegenerating, setRaRegenerating] = useState(false);

  async function load() {
    setError("");
    try {
      const res = await api.getSettings();
      setS(res);
      setTimeoutStr(String(res.command_timeout_seconds));
      setAllow((res.command_allowlist || []).join("\n"));
      setDeny((res.command_denylist || []).join("\n"));
      setRagJson(JSON.stringify(res.rag_provider || {}, null, 2));
      setGhEnabled(!!res.github_sync_enabled);
      setGhRepo(res.github_repo || "");
      setGhSecret(res.github_webhook_secret || "");
      setVoiceOpenaiKey(res.openai_api_key || "");
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
  useEffect(() => {
    load();
    fetch("/api/config").then(r => r.json()).then((d: { mcp_api_key?: string }) => setRaApiKey(d.mcp_api_key || "")).catch(() => {});
  }, []);

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

      {/* ─────── Voice & STT ─────── */}
      <div className="card mb-4" data-testid="settings-voice">
        <h2 className="text-base font-semibold mb-1">Voice & STT</h2>
        <div className="text-xs text-muted mb-3">
          Controls how the Telegram bots transcribe inbound voice notes and speak replies.
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-3">
          <FormRow label="STT provider" hint="Speech-to-text used to transcribe inbound voice messages.">
            <div className="flex gap-1">
              {[
                { id: "openai", label: "Whisper API", desc: "OpenAI Whisper (cloud)" },
                { id: "local",  label: "faster-whisper", desc: "Local Whisper (offline, tiny model)" },
              ].map(({ id, label, desc }) => (
                <button key={id} type="button" title={desc}
                        className={`btn ${s.stt_provider === id ? "btn-primary" : "btn-ghost"}`}
                        disabled={saving === "stt_provider"}
                        onClick={() => save("stt_provider", id)}
                        data-testid={`settings-stt-${id}`}>
                  {label}
                </button>
              ))}
            </div>
          </FormRow>

          <FormRow label="TTS provider" hint="Text-to-speech used to speak bot replies.">
            <div className="flex gap-1">
              {[
                { id: "openai", label: "OpenAI", desc: "tts-1 (cloud, multilingual)" },
                { id: "edge",   label: "Edge",   desc: "Microsoft Edge Neural (per-language)" },
              ].map(({ id, label, desc }) => (
                <button key={id} type="button" title={desc}
                        className={`btn ${s.tts_provider === id ? "btn-primary" : "btn-ghost"}`}
                        disabled={saving === "tts_provider"}
                        onClick={() => save("tts_provider", id)}
                        data-testid={`settings-tts-${id}`}>
                  {label}
                </button>
              ))}
            </div>
          </FormRow>
        </div>

        {(s.stt_provider === "openai" || s.tts_provider === "openai") && (
          <FormRow label="OpenAI API key" hint="Shared by Whisper STT and OpenAI TTS. Falls back to the OPENAI_API_KEY env var if left blank.">
            <div className="flex items-center gap-2">
              <input type="password" className="w-80 font-mono text-sm"
                     placeholder="sk-..."
                     value={voiceOpenaiKey}
                     onChange={e => setVoiceOpenaiKey(e.target.value)}
                     data-testid="settings-voice-openai-key" />
              <button className="btn btn-primary"
                      disabled={saving === "openai_api_key" || voiceOpenaiKey === (s.openai_api_key || "")}
                      onClick={() => save("openai_api_key", voiceOpenaiKey)}
                      data-testid="settings-voice-openai-key-save">
                {saving === "openai_api_key" ? "saving…" : "save"}
              </button>
              <span className={`text-xs ${s.openai_key_configured ? "text-ok" : "text-err"}`}>
                {s.openai_key_configured ? "✓ key detected" : "⚠ no key — voice off when provider=OpenAI"}
              </span>
            </div>
          </FormRow>
        )}

        {s.tts_provider === "edge" ? (
          <div className="mt-3">
            <div className="text-xs text-muted mb-2">
              Edge voice per language — picked by the language Whisper/langdetect detected on the reply.
            </div>
            <div className="space-y-2">
              {Object.keys(EDGE_VOICES_BY_LANG).map(lang => {
                const options = EDGE_VOICES_BY_LANG[lang];
                const current = s.edge_voices?.[lang] || (lang === "pt" ? (s.edge_voice || "pt-BR-AntonioNeural") : options[0]?.id);
                return (
                  <div key={lang} className="grid grid-cols-[130px_1fr] gap-2 items-center">
                    <span className="text-xs">{EDGE_LANG_LABELS[lang]} <span className="text-muted">({lang})</span></span>
                    <select value={current} className="w-64"
                            onChange={e => save("edge_voices", { ...(s.edge_voices || {}), [lang]: e.target.value })}
                            data-testid={`settings-edge-voice-${lang}`}>
                      {options.map(({ id, label }) => <option key={id} value={id}>{label}</option>)}
                    </select>
                  </div>
                );
              })}
            </div>
            <FormRow label="Default voice" hint="Used when the detected language has no entry above.">
              <select value={s.edge_voices?._default || s.edge_voice || "pt-BR-AntonioNeural"} className="w-64"
                      onChange={e => save("edge_voices", { ...(s.edge_voices || {}), _default: e.target.value })}
                      data-testid="settings-edge-voice-default">
                {Object.values(EDGE_VOICES_BY_LANG).flat().map(({ id, label }) => (
                  <option key={id} value={id}>{label} — {id}</option>
                ))}
              </select>
            </FormRow>
          </div>
        ) : (
          <FormRow label="TTS voice" hint="Default OpenAI voice for all bot replies.">
            <div className="grid grid-cols-3 md:grid-cols-6 gap-2">
              {OPENAI_VOICE_OPTIONS.map(({ id, label, desc }) => (
                <button key={id} type="button" title={desc}
                        className={`btn ${(s.tts_voice || "alloy") === id ? "btn-primary" : "btn-ghost"}`}
                        disabled={saving === "tts_voice"}
                        onClick={() => save("tts_voice", id)}
                        data-testid={`settings-tts-voice-${id}`}>
                  {label}
                </button>
              ))}
            </div>
          </FormRow>
        )}
      </div>

      {/* ─────── RAG Provider ─────── */}
      <div className="card mb-4">
        <h2 className="text-base font-semibold mb-1">RAG Provider (knowledge-base backend for lessons)</h2>
        <div className="text-xs text-muted mb-3">
          Configures where lessons learned are <b>stored</b> (as markdown documents) and <b>semantically searched</b>.
          The structured side (effectiveness, tags, applications) always stays in the platform's <code>target_lessons</code> table —
          this setting controls the vector / RAG side. Default points at the local <code>aw-knowledge-base</code> via its HTTP API.
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mb-3">
          <div className="card" style={{ padding: 10 }}>
            <div className="text-xs text-muted uppercase">Active kind</div>
            <div className="font-mono mt-1">{s.rag_provider?.kind || "(default)"}</div>
          </div>
          <div className="card" style={{ padding: 10 }}>
            <div className="text-xs text-muted uppercase">Backend</div>
            <div className="font-mono mt-1 text-sm">{s.rag_provider?.name || "aw-knowledge-base"}</div>
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
            <div><b>http</b> — generic HTTP backend with templated endpoints. Default for aw-knowledge-base. Endpoint paths can interpolate <code>$path</code> / <code>$query</code> / <code>$n_results</code> / <code>$content</code>.</div>
            <div><b>mcp</b> — future: dispatch to an MCP server. Not implemented yet.</div>
          </div>
        </details>
      </div>

      {/* ─────── GitHub Integration ─────── */}
      <div className="card mb-4" data-testid="settings-github">
        <h2 className="text-base font-semibold mb-1">GitHub Issues Integration</h2>
        <div className="text-xs text-muted mb-3">
          Mirror Targets and Runs to GitHub Issues. Requires the <span className="kbd">gh</span> CLI
          to be authenticated. Bidirectional: closing a GitHub Issue cancels the linked run.
        </div>

        <FormRow label="Enable GitHub sync"
                 hint="When enabled, new Targets and Runs are mirrored to GitHub Issues.">
          <div className="flex items-center gap-2">
            <input type="checkbox" checked={ghEnabled}
                   onChange={e => {
                     setGhEnabled(e.target.checked);
                     save("github_sync_enabled", e.target.checked);
                   }}
                   data-testid="settings-gh-enabled" />
            <span className="text-sm">{ghEnabled ? "Enabled" : "Disabled"}</span>
          </div>
        </FormRow>

        <FormRow label="GitHub Repository"
                 hint="Repository in 'owner/repo' format where issues will be created.">
          <div className="flex items-center gap-2">
            <input type="text" className="w-64 font-mono text-sm"
                   placeholder="owner/repo"
                   value={ghRepo}
                   onChange={e => setGhRepo(e.target.value)}
                   data-testid="settings-gh-repo" />
            <button className="btn btn-primary"
                    disabled={saving === "github_repo" || ghRepo === (s.github_repo || "")}
                    onClick={() => save("github_repo", ghRepo)}
                    data-testid="settings-gh-repo-save">
              {saving === "github_repo" ? "saving…" : "save"}
            </button>
          </div>
        </FormRow>

        <FormRow label="Webhook Secret"
                 hint="Secret to verify incoming GitHub webhooks. Leave blank to skip verification.">
          <div className="flex items-center gap-2">
            <input type="password" className="w-64 font-mono text-sm"
                   value={ghSecret}
                   onChange={e => setGhSecret(e.target.value)}
                   data-testid="settings-gh-secret" />
            <button className="btn btn-primary"
                    disabled={saving === "github_webhook_secret" || ghSecret === (s.github_webhook_secret || "")}
                    onClick={() => save("github_webhook_secret", ghSecret)}
                    data-testid="settings-gh-secret-save">
              {saving === "github_webhook_secret" ? "saving…" : "save"}
            </button>
          </div>
        </FormRow>

        <div className="card mt-3 mb-2" style={{ padding: 10, background: "var(--bg-muted, #f5f5f5)" }}>
          <div className="text-xs text-muted">
            <b>Webhook URL:</b> <span className="font-mono">https://your-domain/api/github/webhook</span>
            <br />Configure this in your GitHub repo → Settings → Webhooks. Select <b>Issues</b> events.
          </div>
        </div>

        <div className="flex items-center gap-2 mt-2 flex-wrap">
          <button className="btn btn-ghost"
                  onClick={async () => {
                    setGhTestResult(null);
                    try {
                      const r = await api.testGithubConnection();
                      setGhTestResult(r);
                    } catch (e: any) {
                      setGhTestResult({ ok: false, output: String(e.message || e) });
                    }
                  }}
                  data-testid="settings-gh-test">test connection</button>
          {ghTestResult && (
            <span className={`badge ${ghTestResult.ok ? "badge-success" : "badge-crit"}`}
                  data-testid="settings-gh-test-result">
              {ghTestResult.ok ? "✓ ok" : "✗ failed"}
            </span>
          )}
          {ghTestResult?.output && (
            <span className="text-xs text-muted font-mono" style={{ maxWidth: 400, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {ghTestResult.output}
            </span>
          )}
        </div>
      </div>

      {/* ─────── Remote Agents ─────── */}
      <div className="card mb-4">
        <h2 className="text-base font-semibold mb-1">Remote Agents</h2>
        <div className="text-xs text-muted mb-4">
          The <code className="font-mono">aw-remote-agent</code> MCP server lets AI agents control remote machines.
          Windows / macOS / Linux agents connect via WebSocket and expose file and command tools.
        </div>

        <FormRow label="MCP API Key" hint="Pass this key in every tool call. Shown masked — toggle to reveal.">
          <div className="flex items-center gap-2">
            <code className="font-mono text-sm flex-1 min-w-0"
                  style={{ background: "var(--bg-2)", padding: "5px 10px", borderRadius: 6, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "var(--ok)" }}>
              {raApiKey
                ? (raApiKeyMasked ? raApiKey.slice(0, 8) + "•".repeat(Math.max(0, raApiKey.length - 8)) : raApiKey)
                : "loading…"}
            </code>
            <button className="btn btn-ghost btn-icon" title={raApiKeyMasked ? "Show" : "Hide"}
                    onClick={() => setRaApiKeyMasked(m => !m)}>
              {raApiKeyMasked ? <Eye size={14} /> : <EyeOff size={14} />}
            </button>
            <button className="btn btn-ghost btn-icon" title="Copy"
                    onClick={() => { navigator.clipboard.writeText(raApiKey); setRaApiKeyCopied(true); setTimeout(() => setRaApiKeyCopied(false), 1500); }}>
              {raApiKeyCopied ? <Check size={14} style={{ color: "var(--ok)" }} /> : <Copy size={14} />}
            </button>
            <button className="btn btn-ghost btn-danger" disabled={raRegenerating}
                    onClick={async () => {
                      if (!confirm("Regenerate the MCP API key? Existing connections using the old key will stop working.")) return;
                      setRaRegenerating(true);
                      try {
                        const res = await fetch("/api/config/regenerate", { method: "POST" });
                        const d: { mcp_api_key?: string } = await res.json();
                        setRaApiKey(d.mcp_api_key || "");
                      } finally { setRaRegenerating(false); }
                    }}>
              <RefreshCcw size={13} />{raRegenerating ? "Regenerating…" : "Regenerate"}
            </button>
          </div>
        </FormRow>

        <FormRow label="MCP Gateway Endpoint" hint="Use this URL when adding the aw-remote-agent server to your MCP client.">
          <code className="font-mono text-sm" style={{ color: "var(--accent)" }}>
            {(() => {
              try {
                const h = window.location.host.replace(/:\d+$/, "");
                return `${window.location.protocol}//${h}:9200/mcp`;
              } catch { return "http://…:9200/mcp"; }
            })()}
          </code>
          <span className="text-xs text-muted ml-3">
            Server: <code className="font-mono">aw-remote-agent</code> · Tools: <code className="font-mono">aw_remote_agent__*</code>
          </span>
        </FormRow>

        <div className="mt-3">
          <div className="text-xs font-semibold uppercase text-muted mb-2">Available Tools</div>
          <div className="flex flex-col gap-1">
            {([
              [<Laptop2 size={13} />,   "list_remote_agents",  "List all agents, status, hardware info"],
              [<Play size={13} />,      "execute_command",      "Run a command on the remote machine"],
              [<FileText size={13} />,  "read_file",            "Read a file from the remote machine"],
              [<FileText size={13} />,  "write_file",           "Write/create a file on the remote machine"],
              [<FolderOpen size={13}/>, "list_directory",       "List a directory on the remote machine"],
              [<Trash2 size={13} />,    "delete_file",          "Delete a file on the remote machine"],
              [<FolderOpen size={13}/>, "create_directory",     "Create a directory on the remote machine"],
            ] as [React.ReactNode, string, string][]).map(([icon, name, desc]) => (
              <div key={name} className="flex items-center gap-3 py-1 border-b border-line text-sm">
                <span className="text-muted shrink-0">{icon}</span>
                <code className="font-mono text-xs shrink-0 w-40" style={{ color: "var(--ok)" }}>{name}</code>
                <span className="text-muted text-xs">{desc}</span>
              </div>
            ))}
          </div>
        </div>
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
