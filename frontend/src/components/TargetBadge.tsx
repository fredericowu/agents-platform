import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Crosshair } from "lucide-react";
import { api, type Target } from "../lib/api";
import { useWsEvent } from "../lib/ws";

const cache = new Map<string, Target>();
let fetchPromise: Promise<void> | null = null;

function warmCache(): Promise<void> {
  if (fetchPromise) return fetchPromise;
  fetchPromise = api.listTargets({ includeDeleted: true, limit: 500 }).then(targets => {
    targets.forEach(t => cache.set(t.id, t));
  }).catch(() => {});
  return fetchPromise;
}

export function TargetBadge({ id, slug }: { id: string | null; slug?: string | null }) {
  const [target, setTarget] = useState<Target | null>(
    id ? (cache.get(id) ?? null) : null,
  );

  useEffect(() => {
    if (!id) return;
    if (cache.has(id)) {
      setTarget(cache.get(id)!);
      return;
    }
    // Warm the bulk cache first; if still missing, fetch by slug directly.
    warmCache().then(() => {
      if (cache.has(id)) {
        setTarget(cache.get(id)!);
      } else if (slug) {
        api.getTarget(slug).then(t => {
          cache.set(t.id, t);
          setTarget(t);
        }).catch(() => {});
      }
    });
  }, [id, slug]);

  // Live updates — keep cache in sync so every badge re-renders on change.
  useWsEvent<Target>("target_update", (t) => {
    cache.set(t.id, t);
    if (t.id === id) setTarget(t);
  }, [id]);

  if (!id || !target) return null;
  return (
    <Link
      to={`/targets/${target.slug}`}
      className="badge badge-info inline-flex items-center gap-1 hover:opacity-80"
      title={target.description || target.name}
    >
      <Crosshair size={10} />
      <span className="font-mono">{target.slug}</span>
    </Link>
  );
}
