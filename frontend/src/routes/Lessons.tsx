import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import Page from "../components/Page";
import Modal, { FormRow } from "../components/Modal";
import { api, type ConsolidationCluster, type Lesson, type LessonApplication } from "../lib/api";
import { BookOpen, Search } from "lucide-react";

/* ── helpers ── */

function shortId(id: string) { return id.slice(0, 8); }

function majorityCategory(ls: Lesson[]): string {
  if (!ls.length) return "other";
  const counts: Record<string, number> = {};
  ls.forEach(l => { counts[l.category] = (counts[l.category] || 0) + 1; });
  return Object.entries(counts).sort((a, b) => b[1] - a[1])[0][0];
}

function maxConfidence(ls: Lesson[]): string {
  if (ls.some(l => l.confidence === "high")) return "high";
  if (ls.some(l => l.confidence === "medium")) return "medium";
  return "low";
}

function unionTags(ls: Lesson[]): string[] {
  const s = new Set<string>();
  ls.forEach(l => l.applicable_tags?.forEach(t => s.add(t)));
  return Array.from(s);
}

/* ── badges ── */

function StatusPill({ status }: { status: string }) {
  const cls: Record<string, string> = {
    active: "badge-success",
    pending_review: "badge-warn",
    archived: "badge",
  };
  return <span className={`badge ${cls[status] ?? "badge"}`}>{status.replace(/_/g, " ")}</span>;
}

function ConfidencePill({ confidence }: { confidence: string }) {
  const cls: Record<string, string> = {
    high: "badge-success",
    medium: "badge-info",
    low: "badge-warn",
  };
  return <span className={`badge ${cls[confidence] ?? "badge"}`}>{confidence}</span>;
}

/* ── ConsolidateModal ── */

const CATEGORIES = [
  "time-saver", "pitfall", "tooling-gap", "pattern-that-worked",
  "prompt-fix", "cost-trap", "scope-creep", "other",
];

function ConsolidateModal({ open, onClose, sourceIds, sourceLessons, onSuccess }: {
  open: boolean;
  onClose: () => void;
  sourceIds: string[];
  sourceLessons: Lesson[];
  onSuccess: () => void;
}) {
  const [form, setForm] = useState({ title: "", category: "other", confidence: "medium", tags: "", content: "" });
  const [drafting, setDrafting] = useState(false);
  const [draftError, setDraftError] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, []);

  useEffect(() => {
    if (!open) return;
    setForm({
      title: "",
      category: majorityCategory(sourceLessons),
      confidence: maxConfidence(sourceLessons),
      tags: unionTags(sourceLessons).join(", "),
      content: "",
    });
    setError(""); setDraftError("");
  }, [open]); // eslint-disable-line

  function stopDraft() {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    setDrafting(false);
  }

  async function draftWithAI() {
    setDrafting(true); setDraftError("");
    try {
      const { run_id } = await api.draftConsolidatedLesson(sourceIds);
      let attempts = 0;
      pollRef.current = setInterval(async () => {
        if (++attempts > 30) { stopDraft(); setDraftError("Draft timed out — fill manually."); return; }
        try {
          const run = await api.getRun(run_id);
          if (run.status === "success") {
            stopDraft();
            const out = typeof run.output === "string" ? run.output
              : JSON.stringify(run.output?.content ?? run.output ?? "");
            const extract = (key: string) => out.match(new RegExp(`${key}:\\s*(.+)`, "i"))?.[1]?.trim() ?? "";
            const body = out.match(/BODY:\s*([\s\S]+)/i)?.[1]?.trim() ?? "";
            setForm(f => ({
              ...f,
              title: extract("TITLE") || f.title,
              category: extract("CATEGORY") || f.category,
              tags: extract("TAGS") || f.tags,
              content: body || f.content,
            }));
          } else if (["error", "cancelled"].includes(run.status)) {
            stopDraft();
            setDraftError(`Draft ${run.status}: ${run.error ?? "no detail"}`);
          }
        } catch { /* keep polling */ }
      }, 2000);
    } catch (e: any) {
      stopDraft();
      setDraftError(String(e.message ?? e));
    }
  }

  function fillDefaults() {
    setForm(f => ({
      ...f,
      category: majorityCategory(sourceLessons),
      confidence: maxConfidence(sourceLessons),
      tags: unionTags(sourceLessons).join(", "),
    }));
  }

  async function doConsolidate() {
    if (!form.title.trim()) { setError("Title is required."); return; }
    if (!form.content.trim()) { setError("Body is required."); return; }
    setSaving(true); setError("");
    try {
      await api.consolidateLessons({
        lesson_ids: sourceIds,
        title: form.title.trim(),
        category: form.category,
        confidence: form.confidence,
        tags: form.tags.split(",").map(s => s.trim()).filter(Boolean),
        content: form.content.trim(),
      });
      onSuccess();
    } catch (e: any) {
      setError(String(e.message ?? e));
    } finally {
      setSaving(false);
    }
  }

  const handleClose = () => { stopDraft(); onClose(); };

  return (
    <Modal open={open} onClose={handleClose} title={`Consolidate ${sourceIds.length} lessons → 1`}>
      <div className="flex gap-2 mb-4">
        <button className="btn btn-primary" onClick={draftWithAI} disabled={drafting}>
          {drafting ? "Drafting…" : "Draft body with AI"}
        </button>
        <button className="btn" onClick={fillDefaults} disabled={drafting}>Use deterministic defaults</button>
        {drafting && <button className="btn btn-ghost" onClick={stopDraft}>Cancel</button>}
      </div>
      {draftError && <div className="text-crit text-xs mb-3">{draftError}</div>}

      <FormRow label="Sources (read-only)">
        <div className="bg-bg-2 rounded p-2 space-y-1 text-xs">
          {sourceLessons.length > 0 ? sourceLessons.map(s => (
            <div key={s.id} className="flex items-center gap-2">
              <span className="font-mono text-muted">{shortId(s.id)}</span>
              <span className="text-fg flex-1 truncate">{s.title}</span>
              <span className="badge badge-info">{s.category}</span>
              <span className="text-muted">{s.linked_runs?.length ?? 0} runs</span>
            </div>
          )) : sourceIds.map(id => (
            <div key={id} className="font-mono text-muted">{shortId(id)}</div>
          ))}
        </div>
      </FormRow>
      <FormRow label="Title">
        <input className="input" value={form.title} placeholder="Merged lesson title"
               onChange={e => setForm(f => ({ ...f, title: e.target.value }))} />
      </FormRow>
      <FormRow label="Category">
        <select className="input" value={form.category}
                onChange={e => setForm(f => ({ ...f, category: e.target.value }))}>
          {CATEGORIES.map(c => <option key={c} value={c}>{c}</option>)}
        </select>
      </FormRow>
      <FormRow label="Confidence">
        <select className="input" value={form.confidence}
                onChange={e => setForm(f => ({ ...f, confidence: e.target.value }))}>
          <option value="high">high</option>
          <option value="medium">medium</option>
          <option value="low">low</option>
        </select>
      </FormRow>
      <FormRow label="Tags" hint="comma-separated">
        <input className="input" value={form.tags}
               onChange={e => setForm(f => ({ ...f, tags: e.target.value }))} />
      </FormRow>
      <FormRow label="Body (markdown)">
        <textarea className="input min-h-[160px] font-mono text-xs" value={form.content}
                  onChange={e => setForm(f => ({ ...f, content: e.target.value }))} />
      </FormRow>
      {error && <div className="text-crit text-xs mt-1">{error}</div>}
      <div className="flex justify-end gap-2 mt-4">
        <button className="btn btn-ghost" onClick={handleClose}>Cancel</button>
        <button className="btn btn-primary" onClick={doConsolidate} disabled={saving || drafting}>
          {saving ? "Consolidating…" : `Consolidate → 1`}
        </button>
      </div>
    </Modal>
  );
}

/* ── LessonDrawer ── */

function LessonDrawer({ lesson, onClose, onAction }: {
  lesson: Lesson;
  onClose: () => void;
  onAction: (action: "approve" | "archive" | "restore") => void;
}) {
  const [apps, setApps] = useState<LessonApplication[] | null>(null);

  useEffect(() => {
    setApps(null);
    api.getLessonApplications(lesson.id).then(setApps).catch(() => setApps([]));
  }, [lesson.id]);

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-start justify-between p-4 border-b border-line gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-2 mb-1">
            <StatusPill status={lesson.status} />
            <span className="badge badge-info">{lesson.category}</span>
            <ConfidencePill confidence={lesson.confidence} />
          </div>
          <h2 className="text-base font-semibold text-fg leading-tight">{lesson.title}</h2>
        </div>
        <button className="btn flex-shrink-0" onClick={onClose} aria-label="close">✕</button>
      </div>

      {/* Scrollable body */}
      <div className="flex-1 overflow-y-auto p-4 space-y-5 text-sm">
        {/* Content */}
        <div>
          <div className="text-xs text-muted uppercase tracking-wider mb-2">Content</div>
          <pre className="bg-bg-2 rounded p-3 text-xs text-fg whitespace-pre-wrap font-sans leading-relaxed">
            {lesson.content}
          </pre>
        </div>

        {/* Authored by */}
        {lesson.created_in_run_id && (
          <div>
            <div className="text-xs text-muted uppercase tracking-wider mb-1">Authored by</div>
            <Link to={`/runs/${lesson.created_in_run_id}`}
                  className="badge badge-info font-mono hover:underline"
                  onClick={onClose}>
              retro run {shortId(lesson.created_in_run_id)}
            </Link>
          </div>
        )}

        {/* Linked runs */}
        <div>
          <div className="text-xs text-muted uppercase tracking-wider mb-2">
            Linked agent runs ({lesson.linked_runs?.length ?? 0})
          </div>
          {!lesson.linked_runs?.length ? (
            <div className="text-xs text-muted">No linked runs.</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-muted uppercase">
                    <th className="py-1 pr-3">Run ID</th>
                    <th className="py-1 pr-3">Role</th>
                    <th className="py-1 pr-3">Kind</th>
                    <th className="py-1 pr-3">Status</th>
                    <th className="py-1">Started</th>
                  </tr>
                </thead>
                <tbody>
                  {lesson.linked_runs.map(r => (
                    <tr key={r.run_id} className="border-t border-line hover:bg-bg-3/40">
                      <td className="py-1.5 pr-3">
                        <Link to={`/runs/${r.run_id}`} className="font-mono text-accent hover:underline"
                              onClick={onClose}>
                          {shortId(r.run_id)}
                        </Link>
                      </td>
                      <td className="py-1.5 pr-3"><span className="badge">{r.role}</span></td>
                      <td className="py-1.5 pr-3"><span className="badge badge-info">{r.kind}</span></td>
                      <td className="py-1.5 pr-3">
                        {r.status ? <span className="badge">{r.status}</span> : "—"}
                      </td>
                      <td className="py-1.5 text-muted">
                        {r.started_at ? new Date(r.started_at).toLocaleString() : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Applications */}
        <div>
          <div className="text-xs text-muted uppercase tracking-wider mb-2">Applications</div>
          {apps === null ? (
            <div className="text-xs text-muted">Loading…</div>
          ) : apps.length === 0 ? (
            <div className="text-xs text-muted">—</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-muted uppercase">
                    <th className="py-1 pr-3">Run</th>
                    <th className="py-1 pr-3">Applied at</th>
                    <th className="py-1">Outcome</th>
                  </tr>
                </thead>
                <tbody>
                  {apps.map(a => (
                    <tr key={a.id} className="border-t border-line">
                      <td className="py-1.5 pr-3">
                        <Link to={`/runs/${a.run_id}`} className="font-mono text-accent hover:underline"
                              onClick={onClose}>
                          {shortId(a.run_id)}
                        </Link>
                      </td>
                      <td className="py-1.5 pr-3 text-muted">{new Date(a.applied_at).toLocaleString()}</td>
                      <td className="py-1.5 text-muted">{a.outcome ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Tags */}
        {(lesson.applicable_tags?.length ?? 0) > 0 && (
          <div>
            <div className="text-xs text-muted uppercase tracking-wider mb-2">Tags</div>
            <div className="flex flex-wrap gap-1">
              {lesson.applicable_tags.map(t => (
                <span key={t} className="badge badge-info">{t}</span>
              ))}
            </div>
          </div>
        )}

        {/* Superseded */}
        {lesson.superseded_by && (
          <div>
            <div className="text-xs text-muted uppercase tracking-wider mb-1">Superseded by</div>
            <span className="badge font-mono">{shortId(lesson.superseded_by)}</span>
          </div>
        )}
      </div>

      {/* Footer actions */}
      <div className="p-4 border-t border-line">
        {lesson.status === "pending_review" && (
          <div className="flex gap-2">
            <button className="btn btn-primary flex-1" onClick={() => onAction("approve")}>Approve</button>
            <button className="btn flex-1" onClick={() => onAction("archive")}>Archive</button>
          </div>
        )}
        {lesson.status === "active" && (
          <button className="btn w-full" onClick={() => onAction("archive")}>Archive</button>
        )}
        {lesson.status === "archived" && !lesson.superseded_by && (
          <button className="btn w-full" onClick={() => onAction("restore")}>Restore to active</button>
        )}
        {lesson.status === "archived" && lesson.superseded_by && (
          <div className="text-xs text-muted text-center">
            Superseded by <span className="font-mono">{shortId(lesson.superseded_by)}</span>
          </div>
        )}
      </div>
    </div>
  );
}

/* ── Lessons page ── */

export default function Lessons() {
  const [lessons, setLessons] = useState<Lesson[]>([]);
  const [q, setQ] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [categoryFilter, setCategoryFilter] = useState("");
  const [confidenceFilter, setConfidenceFilter] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [drawerLesson, setDrawerLesson] = useState<Lesson | null>(null);
  const [consolidateOpen, setConsolidateOpen] = useState(false);
  const [consolidateSourceIds, setConsolidateSourceIds] = useState<string[]>([]);
  const [consolidateSourceLessons, setConsolidateSourceLessons] = useState<Lesson[]>([]);
  const [suggestionsOpen, setSuggestionsOpen] = useState(false);
  const [suggestions, setSuggestions] = useState<ConsolidationCluster[]>([]);
  const [suggestionsLoading, setSuggestionsLoading] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const toastRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  async function load() {
    const params: {
      status?: string; category?: string; q?: string; confidence?: string; limit?: number;
    } = { limit: 200 };
    if (statusFilter !== "all") params.status = statusFilter;
    if (categoryFilter) params.category = categoryFilter;
    if (q) params.q = q;
    if (confidenceFilter) params.confidence = confidenceFilter;
    try {
      setLessons(await api.listLessons(params));
    } catch { /* silent */ }
  }

  useEffect(() => { load(); }, [statusFilter, categoryFilter, confidenceFilter]); // eslint-disable-line

  function showToast(msg: string) {
    setToast(msg);
    if (toastRef.current) clearTimeout(toastRef.current);
    toastRef.current = setTimeout(() => setToast(null), 3500);
  }

  async function handleDrawerAction(action: "approve" | "archive" | "restore") {
    if (!drawerLesson) return;
    try {
      if (action === "approve") await api.approveLesson(drawerLesson.id);
      else if (action === "archive") await api.archiveLesson(drawerLesson.id);
      else await api.restoreLesson(drawerLesson.id);
      await load();
      try {
        const updated = await api.getLesson(drawerLesson.id);
        setDrawerLesson(updated);
      } catch {
        setDrawerLesson(null);
      }
      const verb = action === "approve" ? "approved" : action === "archive" ? "archived" : "restored";
      showToast(`Lesson ${verb}.`);
    } catch (e: any) {
      alert(String(e.message ?? e));
    }
  }

  async function loadSuggestions() {
    setSuggestionsLoading(true);
    try {
      setSuggestions(await api.getConsolidationSuggestions());
    } catch (e: any) {
      alert(String(e.message ?? e));
    } finally {
      setSuggestionsLoading(false);
    }
  }

  function openConsolidate(ids: string[], srcLessons?: Lesson[]) {
    setConsolidateSourceIds(ids);
    setConsolidateSourceLessons(srcLessons ?? lessons.filter(l => ids.includes(l.id)));
    setConsolidateOpen(true);
  }

  function toggleSelect(id: string) {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleAll() {
    setSelected(selected.size === lessons.length ? new Set() : new Set(lessons.map(l => l.id)));
  }

  const stats = {
    active: lessons.filter(l => l.status === "active").length,
    pending: lessons.filter(l => l.status === "pending_review").length,
    archived: lessons.filter(l => l.status === "archived").length,
  };

  const categories = [...new Set(lessons.map(l => l.category))].filter(Boolean).sort();

  return (
    <>
      <Page
        title="Lessons"
        subtitle="Cross-target learned lessons. Click a lesson to see linked agent runs and consolidate."
        actions={
          <button
            className="btn btn-ghost flex items-center gap-2"
            onClick={() => {
              const next = !suggestionsOpen;
              setSuggestionsOpen(next);
              if (next) loadSuggestions();
            }}
          >
            🔍 Find consolidation candidates
          </button>
        }
      >
        {/* Stat tiles */}
        <div className="flex gap-4 mb-5">
          {(
            [
              { label: "Active", count: stats.active, cls: "text-accent", val: "active" },
              { label: "Pending review", count: stats.pending, cls: "text-warn", val: "pending_review" },
              { label: "Archived", count: stats.archived, cls: "text-muted", val: "archived" },
            ] as const
          ).map(({ label, count, cls, val }) => (
            <button
              key={label}
              className={`card flex flex-col items-center px-6 py-3 flex-1 hover:bg-bg-3/60 cursor-pointer ${statusFilter === val ? "ring-1 ring-accent" : ""}`}
              onClick={() => setStatusFilter(statusFilter === val ? "all" : val)}
            >
              <div className={`text-2xl font-bold ${cls}`}>{count}</div>
              <div className="text-xs text-muted">{label}</div>
            </button>
          ))}
        </div>

        {/* Suggestions panel */}
        {suggestionsOpen && (
          <div className="card mb-5">
            <div className="flex items-center justify-between mb-3">
              <div className="font-semibold text-sm">Consolidation candidates</div>
              <button className="btn btn-ghost text-xs" onClick={() => setSuggestionsOpen(false)}>✕</button>
            </div>
            {suggestionsLoading ? (
              <div className="text-muted text-xs">Loading…</div>
            ) : suggestions.length === 0 ? (
              <div className="text-muted text-xs">No consolidation candidates found.</div>
            ) : (
              <div className="space-y-3">
                {suggestions.map((cluster, i) => (
                  <div key={i} className="border border-line rounded p-3">
                    <div className="flex items-start justify-between gap-3 mb-2">
                      <div className="text-xs text-muted">
                        Cluster #{i + 1} — confidence {cluster.confidence.toFixed(2)} — {cluster.lessons.length} lessons
                        {cluster.shared_tags?.length
                          ? ` share tags {${cluster.shared_tags.join(", ")}}`
                          : ""}
                      </div>
                      <button
                        className="btn btn-primary text-xs flex-shrink-0"
                        onClick={() => {
                          const ids = cluster.lessons.map(l => l.id);
                          setSelected(new Set(ids));
                          openConsolidate(ids, cluster.lessons);
                        }}
                      >
                        Consolidate this cluster
                      </button>
                    </div>
                    <div className="space-y-0.5">
                      {cluster.lessons.map(l => (
                        <div key={l.id} className="text-xs text-fg ml-2">— {l.title}</div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Filters */}
        <div className="flex flex-wrap items-center gap-3 mb-4">
          <div className="relative flex-1 min-w-[200px] max-w-sm">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
            <input
              className="input pl-9"
              placeholder="Search title, tags…"
              value={q}
              onChange={e => setQ(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter") load(); }}
            />
          </div>
          <select className="input w-44" value={categoryFilter}
                  onChange={e => setCategoryFilter(e.target.value)}>
            <option value="">All categories</option>
            {categories.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
          <select className="input w-44" value={statusFilter}
                  onChange={e => setStatusFilter(e.target.value)}>
            <option value="all">All statuses</option>
            <option value="active">Active</option>
            <option value="pending_review">Pending review</option>
            <option value="archived">Archived</option>
          </select>
          <select className="input w-36" value={confidenceFilter}
                  onChange={e => setConfidenceFilter(e.target.value)}>
            <option value="">Any confidence</option>
            <option value="high">High</option>
            <option value="medium">Medium</option>
            <option value="low">Low</option>
          </select>
        </div>

        {/* Selection bar */}
        {selected.size > 0 && (
          <div className="flex items-center gap-3 mb-4 px-3 py-2.5 bg-bg-2 rounded border border-accent/30">
            <span className="text-sm font-medium">{selected.size} selected</span>
            <button
              className="btn btn-primary text-sm"
              disabled={selected.size < 2}
              onClick={() => openConsolidate(Array.from(selected))}
            >
              Consolidate selected
            </button>
            <button className="btn btn-ghost text-sm" onClick={() => setSelected(new Set())}>
              Clear selection
            </button>
          </div>
        )}

        {/* Table */}
        <div className="card overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-muted uppercase tracking-wide">
                <th className="py-2 px-2">
                  <input
                    type="checkbox"
                    className="w-auto"
                    checked={lessons.length > 0 && selected.size === lessons.length}
                    onChange={toggleAll}
                  />
                </th>
                <th className="py-2 px-2">Title</th>
                <th className="py-2 px-2">Category</th>
                <th className="py-2 px-2">Confidence</th>
                <th className="py-2 px-2">Status</th>
                <th className="py-2 px-2">Runs</th>
                <th className="py-2 px-2">Applied</th>
                <th className="py-2 px-2">Tags</th>
                <th className="py-2 px-2">Updated</th>
              </tr>
            </thead>
            <tbody>
              {lessons.length === 0 ? (
                <tr>
                  <td colSpan={9} className="py-12 text-center text-muted text-sm">
                    <BookOpen size={32} className="mx-auto mb-3 opacity-40" />
                    <div>No lessons found. Lessons are created automatically during agent retros.</div>
                  </td>
                </tr>
              ) : (
                lessons.map(l => (
                  <tr
                    key={l.id}
                    className={`border-t border-line cursor-pointer hover:bg-bg-3/40 ${selected.has(l.id) ? "bg-bg-3/60" : ""}`}
                    onClick={() => setDrawerLesson(l)}
                  >
                    <td className="py-2 px-2" onClick={e => { e.stopPropagation(); toggleSelect(l.id); }}>
                      <input type="checkbox" className="w-auto" checked={selected.has(l.id)} onChange={() => {}} />
                    </td>
                    <td className="py-2 px-2 max-w-[280px]">
                      <div className="font-semibold text-fg truncate">{l.title}</div>
                      <div className="text-xs text-muted line-clamp-1 mt-0.5">{l.content?.slice(0, 80)}</div>
                    </td>
                    <td className="py-2 px-2">
                      <span className="badge badge-info">{l.category}</span>
                    </td>
                    <td className="py-2 px-2"><ConfidencePill confidence={l.confidence} /></td>
                    <td className="py-2 px-2"><StatusPill status={l.status} /></td>
                    <td className="py-2 px-2 text-muted">{l.linked_runs?.length ?? 0}</td>
                    <td className="py-2 px-2 text-muted">{l.n_applied ?? "—"}</td>
                    <td className="py-2 px-2">
                      <div className="flex flex-wrap gap-1">
                        {l.applicable_tags?.slice(0, 3).map(t => (
                          <span key={t} className="badge text-[10px]">{t}</span>
                        ))}
                        {(l.applicable_tags?.length ?? 0) > 3 && (
                          <span className="badge text-[10px] text-muted">
                            +{l.applicable_tags.length - 3}
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="py-2 px-2 text-xs text-muted whitespace-nowrap">
                      {new Date(l.updated_at).toLocaleString()}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </Page>

      {/* Drawer backdrop */}
      {drawerLesson && (
        <div className="fixed inset-0 z-40 bg-black/40" onClick={() => setDrawerLesson(null)} />
      )}

      {/* Detail drawer */}
      <div
        className={`fixed top-0 right-0 bottom-0 z-50 w-[520px] bg-bg-1 border-l border-line shadow-2xl flex flex-col transition-transform duration-200 ${
          drawerLesson ? "translate-x-0" : "translate-x-full"
        }`}
      >
        {drawerLesson && (
          <LessonDrawer
            lesson={drawerLesson}
            onClose={() => setDrawerLesson(null)}
            onAction={handleDrawerAction}
          />
        )}
      </div>

      {/* Consolidate modal */}
      <ConsolidateModal
        open={consolidateOpen}
        onClose={() => setConsolidateOpen(false)}
        sourceIds={consolidateSourceIds}
        sourceLessons={consolidateSourceLessons}
        onSuccess={() => {
          setConsolidateOpen(false);
          setSelected(new Set());
          load();
          showToast(`Consolidated ${consolidateSourceIds.length} lessons → 1.`);
        }}
      />

      {/* Toast */}
      {toast && (
        <div className="fixed bottom-4 right-4 z-[60] bg-bg-2 border border-line rounded-lg shadow-lg px-4 py-2.5 text-sm text-fg">
          {toast}
        </div>
      )}
    </>
  );
}
