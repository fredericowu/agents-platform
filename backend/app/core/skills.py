"""Skills loader. Reads .claude/skills/<name>/SKILL.md AND DB-stored custom skills.

A CustomSkill row can:
  * override a file skill (same slug, content set) — DB content wins
  * hide a file skill   (same slug, hidden=True)   — entire skill suppressed
  * define a new skill (no file equivalent)        — appears as source="custom"
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import settings
from ..db import session_scope
from ..models import CustomSkill


def _read_file_skill(child: Path) -> dict[str, Any] | None:
    skill_file = child / "SKILL.md"
    if not skill_file.exists():
        return None
    text = skill_file.read_text(errors="replace")
    name = child.name
    desc = ""
    for line in text.splitlines():
        s = line.strip()
        if s.lower().startswith("description:"):
            desc = s.split(":", 1)[1].strip().strip("'\"")
            break
        if s.startswith("# "):
            desc = s.lstrip("# ").strip()
    return {"slug": name, "name": name, "description": desc[:240],
            "path": str(skill_file), "source": "file", "content": text}


def _list_file_skills() -> dict[str, dict[str, Any]]:
    root = settings.skills_path
    if not root.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        sk = _read_file_skill(child)
        if sk:
            out[sk["slug"]] = sk
    return out


def list_skills() -> list[dict[str, Any]]:
    """Return the merged skill list, applying DB overrides and tombstones."""
    file_by_slug = _list_file_skills()
    # snapshot DB rows into plain dicts while still in session
    with session_scope() as s:
        db_by_slug = {
            r.slug: {
                "slug": r.slug, "name": r.name, "description": r.description,
                "content": r.content, "hidden": bool(r.hidden),
            }
            for r in s.query(CustomSkill).all()
        }

    merged: dict[str, dict[str, Any]] = {}
    for slug, sk in file_by_slug.items():
        db = db_by_slug.get(slug)
        if db and db["hidden"]:
            continue                # tombstone — suppress
        if db and db["content"]:
            merged[slug] = {**sk,
                            "name": db["name"] or sk["name"],
                            "description": db["description"] or sk["description"],
                            "source": "override"}
        else:
            merged[slug] = sk
    for slug, db in db_by_slug.items():
        if slug in merged:
            continue
        if db["hidden"]:
            continue
        merged[slug] = {
            "slug": db["slug"], "name": db["name"] or db["slug"],
            "description": db["description"], "path": f"db:{db['slug']}",
            "source": "custom",
        }
    out = [{k: v for k, v in s.items() if k != "content"} for s in merged.values()]
    out.sort(key=lambda x: x["slug"])
    return out


def load_skill(slug: str) -> str | None:
    """Return the content of a skill, applying overrides and tombstones."""
    with session_scope() as s:
        db = s.query(CustomSkill).filter(CustomSkill.slug == slug).first()
        if db is not None:
            hidden, content = bool(db.hidden), db.content
            if hidden:
                return None
            if content:
                return content
    p = settings.skills_path / slug / "SKILL.md"
    if not p.exists():
        return None
    return p.read_text(errors="replace")