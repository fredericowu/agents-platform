import { useEffect, useMemo, useRef, useState } from "react";
import Page from "../components/Page";
import { api, streamRun, type Agent } from "../lib/api";

type Msg = { who: "user" | "agent"; text: string; tokens?: number; runId?: string };

function makeSessionId() {
  return "chat-" + Math.random().toString(36).slice(2, 10);
}

export default function Playground() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agent, setAgent] = useState<string>("coder");
  const [input, setInput] = useState("");
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [sessionId, setSessionId] = useState<string>(makeSessionId());
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api.listAgents().then(rs => { setAgents(rs); if (rs[0] && !agents.length) setAgent(rs[0].slug); });
  }, []);
  useEffect(() => { boxRef.current?.scrollTo(0, 9e9); }, [msgs]);

  const currentAgent = useMemo(() => agents.find(a => a.slug === agent), [agents, agent]);
  const totals = useMemo(() => msgs.reduce((acc, m) => ({
    in: acc.in + 0,
    out: acc.out + (m.tokens || 0),
  }), { in: 0, out: 0 }), [msgs]);

  function newSession() {
    setSessionId(makeSessionId());
    setMsgs([]);
  }

  async function sendText(text: string) {
    setMsgs(m => [...m, { who: "user", text }]);
    setMsgs(m => [...m, { who: "agent", text: "" }]);
    setStreaming(true);
    const { run_id } = await api.playgroundChat(agent, text, { session_id: sessionId });
    const stop = streamRun(run_id, (e) => {
      if (e.kind === "llm_token") {
        const d = e.payload?.delta || "";
        setMsgs(m => {
          const copy = [...m]; const last = copy.length - 1;
          copy[last] = { ...copy[last], text: copy[last].text + d };
          return copy;
        });
      } else if (e.kind === "node_end") {
        setMsgs(m => {
          const copy = [...m]; const last = copy.length - 1;
          copy[last] = { ...copy[last], tokens: e.payload?.tokens_out, runId: run_id };
          return copy;
        });
      } else if (e.kind === "done" || e.kind === "error") {
        setStreaming(false); stop();
      }
    });
  }

  async function send() {
    if (!input.trim() || streaming) return;
    const text = input.trim();
    setInput("");
    await sendText(text);
  }

  async function regenerateLast() {
    if (streaming) return;
    // Find the last user message; drop everything after it; re-send.
    let lastUserIdx = -1;
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].who === "user") { lastUserIdx = i; break; }
    }
    if (lastUserIdx === -1) return;
    const prompt = msgs[lastUserIdx].text;
    // Start a fresh session so history doesn't include the previous reply.
    const fresh = makeSessionId();
    setSessionId(fresh);
    // Replay user messages up to lastUserIdx-1 (excluding the one we'll re-send)
    // For simplicity: clear the agent reply and re-send within the fresh session.
    setMsgs(prev => prev.slice(0, lastUserIdx));    // drop user msg and its reply
    await sendText(prompt);
  }

  return (
    <Page title="Playground"
          subtitle={`Multi-turn chat with any agent. Session: ${sessionId}`}
          actions={
            <>
              <button className="btn" onClick={regenerateLast}
                      disabled={streaming || !msgs.some(m => m.who === "user")}
                      data-testid="playground-regenerate">regenerate</button>
              <button className="btn" onClick={newSession} data-testid="playground-new-session">
                new session
              </button>
            </>
          }>
      <div className="grid grid-cols-[200px_1fr] gap-4 h-[70vh]">
        <div className="card overflow-y-auto" data-testid="playground-agents">
          <div className="text-xs text-muted uppercase mb-2">agents</div>
          {agents.map(a => (
            <div key={a.slug}
                 className={`p-2 mb-1 rounded text-sm cursor-pointer ${agent === a.slug ? "bg-bg-3 border border-accent" : "hover:bg-bg-3/60"}`}
                 onClick={() => setAgent(a.slug)}
                 data-testid={`playground-agent-${a.slug}`}>
              <div style={{ color: a.color }} className="font-medium">{a.name}</div>
              <div className="text-xs text-muted">{a.model_slug}</div>
            </div>
          ))}
        </div>
        <div className="flex flex-col gap-3">
          <div className="text-xs text-muted flex items-center justify-between" data-testid="playground-status">
            <div>
              agent <span className="kbd">{agent}</span>
              {currentAgent?.model_slug && <> · model <span className="kbd">{currentAgent.model_slug}</span></>}
              · {msgs.filter(m => m.who === "user").length} turn(s)
            </div>
            <div>tokens out: {totals.out}</div>
          </div>
          <div ref={boxRef} className="card flex-1 overflow-y-auto space-y-3" data-testid="playground-messages">
            {msgs.length === 0 && <div className="text-muted text-sm">start typing below…</div>}
            {msgs.map((m, i) => (
              <div key={i} className={`flex ${m.who === "user" ? "justify-end" : ""}`}>
                <div className={`max-w-[80%] p-3 rounded-lg whitespace-pre-wrap text-sm ${
                  m.who === "user" ? "bg-bg-3" : "bg-bg-2 border border-line"
                }`}>
                  {m.text || <span className="text-muted">…</span>}
                  {(m.tokens || m.runId) && (
                    <div className="text-xs text-muted mt-1">
                      {m.tokens ? `${m.tokens} tok` : ""}
                      {m.runId && <a href={`/runs/${m.runId}`} className="ml-2">view run ↗</a>}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
          <div className="flex gap-2">
            <textarea value={input} onChange={e => setInput(e.target.value)} rows={2}
                      onKeyDown={e => e.key === "Enter" && !e.shiftKey && (e.preventDefault(), send())}
                      placeholder="Message... (enter to send, shift+enter for newline)"
                      data-testid="playground-input" />
            <button className="btn btn-primary" onClick={send} disabled={streaming}
                    data-testid="playground-send">send</button>
          </div>
        </div>
      </div>
    </Page>
  );
}
