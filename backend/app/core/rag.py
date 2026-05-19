"""Pluggable RAG provider — settings-configurable knowledge-base backend.

The agents-platform stores **lessons learned** (and potentially other long-form
knowledge) in two places:

  1. The structured ``target_lessons`` SQLite table (tag-overlap, confidence,
     applications). This is the SOURCE OF TRUTH for metadata.
  2. A vector / RAG store for semantic search. This is REPLICATED from #1 and
     kept in sync via this provider.

The provider is **pluggable** because users want the freedom to swap RAG
backends without rewriting agent prompts or the lessons API. Configuration
lives in the ``rag_provider`` setting (key/value JSON, default below).

## Supported `kind` values

* ``http``      — A generic HTTP backend (aw-knowledge-base by default).
                  Templated endpoints support arbitrary REST-shaped RAGs.
* ``disabled``  — No RAG. ``search_lessons`` falls back to SQL-only,
                  upserts/deletes are no-ops.
* ``mcp``       — Future: dispatch to an MCP server tool. Not implemented
                  yet (requires the backend to host an MCP client).

## Default config

The default points at the local ``aw-knowledge-base`` service running on
the aw port (read from .tmp/awserv_api_key for auth). Set
``settings.rag_provider`` to override.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from . import security


# ----- defaults ---------------------------------------------------------------

_aw_API_KEY_PATH = "/opt/agentic-workspace/.tmp/awserv_api_key"
_aw_DEFAULT_PORT = 9123


def _read_aw_api_key() -> str | None:
    try:
        return Path(_aw_API_KEY_PATH).read_text().strip()
    except Exception:
        return None


def _default_provider() -> dict:
    return {
        "kind": "http",
        "name": "aw-knowledge-base",
        "base_url": f"http://127.0.0.1:{_aw_DEFAULT_PORT}",
        "auth": {
            # The actual key is read at call-time from this file so we don't
            # cache stale keys. If the file is missing, calls go un-keyed.
            "header": "x-api-key",
            "value_from_file": _aw_API_KEY_PATH,
        },
        "lesson_path_prefix": "agent-platform/lessons/",
        "endpoints": {
            "search": {"method": "GET", "path": "/api/kb/mcp-search",
                       "params": {"q": "$query", "n": "$n_results"}},
            "upsert": {"method": "PUT", "path": "/api/kb/file/$path",
                       "body":   {"content": "$content"}},
            "delete": {"method": "DELETE", "path": "/api/kb/file/$path"},
        },
    }


# ----- provider ---------------------------------------------------------------

class RagProvider:
    """Wraps a configured RAG backend behind a uniform interface."""

    def __init__(self, config: dict | None = None):
        self.config = config or _default_provider()
        self.kind = self.config.get("kind", "disabled")

    # ----- public API -----

    def search(self, query: str, n_results: int = 5) -> dict:
        """Semantic search. Returns ``{"results": [{path, score, content?}], "error": null}``."""
        if self.kind == "disabled":
            return {"results": [], "skipped": True, "reason": "rag disabled"}
        if self.kind == "http":
            return self._http("search", {"query": query, "n_results": n_results})
        return {"results": [], "error": f"unsupported kind: {self.kind}"}

    def upsert(self, path: str, content: str) -> dict:
        """Write or update a document. Path is relative to the RAG root.

        The configured ``lesson_path_prefix`` is prepended if ``path`` doesn't
        already start with it (so callers can pass either the short slug or
        the fully-qualified path)."""
        if self.kind == "disabled":
            return {"skipped": True, "reason": "rag disabled"}
        prefix = self.config.get("lesson_path_prefix", "")
        full_path = path if (not prefix or path.startswith(prefix)) else f"{prefix.rstrip('/')}/{path.lstrip('/')}"
        if self.kind == "http":
            return self._http("upsert", {"path": full_path, "content": content})
        return {"error": f"unsupported kind: {self.kind}"}

    def delete(self, path: str) -> dict:
        if self.kind == "disabled":
            return {"skipped": True, "reason": "rag disabled"}
        prefix = self.config.get("lesson_path_prefix", "")
        full_path = path if (not prefix or path.startswith(prefix)) else f"{prefix.rstrip('/')}/{path.lstrip('/')}"
        if self.kind == "http":
            return self._http("delete", {"path": full_path})
        return {"error": f"unsupported kind: {self.kind}"}

    def health(self) -> dict:
        """One-shot connectivity test for the configured backend."""
        if self.kind == "disabled":
            return {"ok": True, "kind": "disabled", "note": "RAG is disabled — only SQL search will run."}
        if self.kind == "http":
            try:
                r = self.search("health", n_results=1)
                return {"ok": "results" in r and "error" not in r, "kind": "http",
                        "base_url": self.config.get("base_url"),
                        "auth_header_set": bool(self._auth_header_value())}
            except Exception as e:
                return {"ok": False, "kind": "http", "error": str(e)}
        return {"ok": False, "kind": self.kind, "error": "unsupported kind"}

    # ----- HTTP implementation -----

    def _auth_header_value(self) -> str | None:
        auth = self.config.get("auth") or {}
        if "value" in auth and auth["value"]:
            return auth["value"]
        if auth.get("value_from_file"):
            try:
                return Path(auth["value_from_file"]).read_text().strip()
            except Exception:
                return None
        if auth.get("value_from_env"):
            return os.environ.get(auth["value_from_env"])
        return None

    def _headers(self) -> dict[str, str]:
        auth = self.config.get("auth") or {}
        header = auth.get("header")
        value = self._auth_header_value()
        if header and value:
            return {header: value}
        return {}

    def _render(self, template: Any, variables: dict[str, Any]) -> Any:
        """Replace ``$key`` substrings inside a template with values from variables."""
        if isinstance(template, str):
            # full-key match: replace value
            if template.startswith("$") and template[1:] in variables:
                return variables[template[1:]]
            # interpolate any $key occurrences inline
            out = template
            for k, v in variables.items():
                out = out.replace(f"${k}", str(v))
            return out
        if isinstance(template, dict):
            return {k: self._render(v, variables) for k, v in template.items()}
        if isinstance(template, list):
            return [self._render(v, variables) for v in template]
        return template

    def _http(self, op: str, variables: dict[str, Any]) -> dict:
        endpoint = (self.config.get("endpoints") or {}).get(op)
        if not endpoint:
            return {"error": f"no endpoint configured for op={op}"}
        method = endpoint.get("method", "GET").upper()
        path = self._render(endpoint.get("path", ""), variables)
        url = f"{self.config.get('base_url', '').rstrip('/')}{path if path.startswith('/') else '/' + path}"
        params = self._render(endpoint.get("params") or {}, variables) or None
        body = self._render(endpoint.get("body") or None, variables)
        headers = self._headers()

        try:
            with httpx.Client(timeout=30) as client:
                r = client.request(
                    method, url,
                    params=params,
                    json=body if body is not None else None,
                    headers=headers,
                )
        except Exception as e:
            return {"error": f"http call failed: {e}", "url": url}

        if r.status_code >= 400:
            return {"error": f"http {r.status_code}: {r.text[:300]}", "url": url}

        try:
            payload = r.json()
        except Exception:
            return {"results": [{"path": "(non-json response)", "content": r.text[:500]}]}

        # Normalize: aw's mcp-search returns ``{"results":[{path, content, score}]}``
        # — keep as-is. Other backends may differ; normalize when we add them.
        if op == "search":
            if isinstance(payload, list):
                return {"results": payload}
            if isinstance(payload, dict):
                return payload if "results" in payload else {"results": payload.get("hits", [])}
        return payload if isinstance(payload, dict) else {"result": payload}


# ----- factory ----------------------------------------------------------------

def get_rag_provider() -> RagProvider:
    """Construct a RagProvider from the current ``rag_provider`` setting."""
    cfg = security.get_setting("rag_provider")
    if not cfg or not isinstance(cfg, dict):
        cfg = _default_provider()
    return RagProvider(cfg)


def slugify_for_kb(title: str) -> str:
    """Convert a lesson title into a kebab-case slug suitable for the RAG path."""
    import re
    s = re.sub(r"[^a-zA-Z0-9\s-]", "", title).lower().strip()
    s = re.sub(r"\s+", "-", s)
    return s[:80]


def render_lesson_markdown(lesson: dict, target: dict | None = None) -> str:
    """Render a lesson row into the canonical markdown form stored in the RAG."""
    badge = {"high": "🟢 HIGH confidence", "medium": "🟡 MEDIUM confidence", "low": "🟠 LOW confidence"}
    target_name = (target or {}).get("name", lesson.get("target_id", "unknown"))
    target_slug = (target or {}).get("slug", "")
    tags = ", ".join(f"`{t}`" for t in (lesson.get("applicable_tags") or [])) or "(none)"
    evidence = "\n".join(f"- `{r}`" for r in (lesson.get("evidence_run_ids") or [])) or "(no run IDs recorded)"
    return f"""# {lesson.get('title', '(untitled)')}

> **Category:** `{lesson.get('category', 'unknown')}` · {badge.get(lesson.get('confidence', 'medium'), lesson.get('confidence', ''))}
>
> **Source Target:** {target_name}{f' (`{target_slug}`)' if target_slug else ''}
>
> **Tags:** {tags}
>
> **Lesson ID:** `{lesson.get('id', '')}`

{lesson.get('content') or '(no body)'}

---

## Evidence (run IDs)

{evidence}

## How it's used

Auto-loaded by the agent-platform conductor at Phase 1.4 when tags overlap. PM must `APPLY` / `REJECT` / `DEFER` the lesson.
After delivery, the `retro` agent records `record_lesson_application(outcome=...)`. Effectiveness via `/api/lessons/<id>/effectiveness`.
"""
