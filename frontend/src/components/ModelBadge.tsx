import { useEffect, useState } from "react";
import { api, type Model } from "../lib/api";

const cache = new Map<string, Model>();
let fetchPromise: Promise<void> | null = null;

function warmCache(): Promise<void> {
  if (fetchPromise) return fetchPromise;
  fetchPromise = api.listModels().then(models => {
    models.forEach(m => cache.set(m.slug, m));
  }).catch(() => {});
  return fetchPromise;
}

export function ModelBadge({ slug }: { slug: string | null }) {
  const [displayName, setDisplayName] = useState<string | null>(
    slug ? (cache.get(slug)?.display_name ?? null) : null,
  );

  useEffect(() => {
    if (!slug) return;
    if (cache.has(slug)) {
      setDisplayName(cache.get(slug)!.display_name);
      return;
    }
    warmCache().then(() => {
      setDisplayName(cache.get(slug)?.display_name ?? null);
    });
  }, [slug]);

  if (!slug || !displayName) return null;
  return <span className="badge badge-info">{displayName}</span>;
}
