"""Skill CRUD that handles file skills, custom skills, overrides, and tombstones uniformly."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..core.skills import list_skills, load_skill
from ..db import get_session
from ..models import CustomSkill
from ..schemas import SkillOut

router = APIRouter(prefix="/api/skills", tags=["skills"])


class SkillIn(BaseModel):
    slug: str
    name: str = ""
    description: str = ""
    content: str = ""


class SkillUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    content: str | None = None


def _file_skill_exists(slug: str) -> bool:
    return (settings.skills_path / slug / "SKILL.md").exists()


@router.get("", response_model=list[SkillOut])
def list_skills_ep():
    return list_skills()


@router.get("/{slug}")
def get_skill(slug: str):
    content = load_skill(slug)
    if content is None:
        return {"slug": slug, "found": False}
    return {"slug": slug, "found": True, "content": content}


@router.post("", response_model=SkillOut)
def create_skill(body: SkillIn, s: Session = Depends(get_session)):
    if s.query(CustomSkill).filter(CustomSkill.slug == body.slug).first():
        raise HTTPException(409, "slug already exists")
    if _file_skill_exists(body.slug):
        raise HTTPException(409, f"a file skill named {body.slug!r} already exists; "
                                  "use PUT to create an override instead")
    sk = CustomSkill(slug=body.slug, name=body.name or body.slug,
                     description=body.description, content=body.content, hidden=False)
    s.add(sk); s.commit(); s.refresh(sk)
    return {"slug": sk.slug, "name": sk.name, "description": sk.description,
            "path": f"db:{sk.slug}", "source": "custom"}


@router.put("/{slug}", response_model=SkillOut)
def update_skill(slug: str, patch: SkillUpdate, s: Session = Depends(get_session)):
    """Edit a skill. If it's a file skill (no DB row), this creates an
    override row that takes precedence at runtime."""
    sk = s.query(CustomSkill).filter(CustomSkill.slug == slug).first()
    if sk is None:
        if not _file_skill_exists(slug):
            raise HTTPException(404, "not found")
        # create an override row with file content as base
        base_content = load_skill(slug) or ""
        sk = CustomSkill(slug=slug, name=slug, description="",
                         content=base_content, hidden=False)
        s.add(sk); s.flush()
    if sk.hidden:
        sk.hidden = False
    for f in ("name", "description", "content"):
        v = getattr(patch, f, None)
        if v is not None:
            setattr(sk, f, v)
    s.commit(); s.refresh(sk)
    return {"slug": sk.slug, "name": sk.name or sk.slug,
            "description": sk.description,
            "path": f"db:{sk.slug}",
            "source": "override" if _file_skill_exists(slug) else "custom"}


@router.delete("/{slug}")
def delete_skill(slug: str, s: Session = Depends(get_session)):
    """Delete behavior:
      * pure custom skill (no file)        → row removed
      * file skill with no override        → insert a hidden tombstone
      * file skill with an override        → drop the override + insert tombstone
    The file on disk is never touched.
    """
    sk = s.query(CustomSkill).filter(CustomSkill.slug == slug).first()
    is_file = _file_skill_exists(slug)
    if sk is None and not is_file:
        raise HTTPException(404, "not found")
    if not is_file:
        s.delete(sk); s.commit()
        return {"deleted": slug, "method": "removed"}
    # is_file = True → install tombstone
    if sk is None:
        sk = CustomSkill(slug=slug, name=slug, hidden=True)
        s.add(sk)
    else:
        sk.hidden = True
        sk.content = ""
    s.commit()
    return {"deleted": slug, "method": "tombstoned"}


@router.post("/{slug}/reset")
def reset_skill(slug: str, s: Session = Depends(get_session)):
    """Revert any override/tombstone for a file skill. No-op for pure custom skills."""
    if not _file_skill_exists(slug):
        raise HTTPException(400, "no underlying file skill — nothing to reset to")
    sk = s.query(CustomSkill).filter(CustomSkill.slug == slug).first()
    if sk is None:
        return {"slug": slug, "noop": True}
    s.delete(sk); s.commit()
    return {"slug": slug, "reset": True}
