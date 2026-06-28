"""Shared slug utilities for agents and workflows.

Slugs are unique across BOTH tables — an agent-slug and a workflow-slug
cannot collide. This module owns the cross-table uniqueness check and the
auto-generation helper.
"""
import random
import re
import string

from sqlalchemy.orm import Session

from .models import Agent, Workflow


_CHARS = string.ascii_lowercase + string.digits


def _slug_taken(slug: str, session: Session, exclude_id: str | None = None) -> bool:
    """Return True if slug is already used by any agent or workflow (even soft-deleted)."""
    aq = session.query(Agent).filter(Agent.slug == slug)
    wq = session.query(Workflow).filter(Workflow.slug == slug)
    if exclude_id:
        aq = aq.filter(Agent.slug != exclude_id)
        wq = wq.filter(Workflow.slug != exclude_id)
    return aq.first() is not None or wq.first() is not None


def generate_unique_slug(prefix: str, session: Session, name: str | None = None) -> str:
    """Generate a slug unique across agents+workflows.

    If *name* is given we derive a readable base from it first, then append
    random chars if that base collides.  Without a name we use 6 random chars.

    Examples:
        generate_unique_slug("agent", s, "My Coder")   → "agent-my-coder"
        generate_unique_slug("workflow", s)             → "workflow-a3kx9z"
    """
    if name:
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
        candidate = f"{prefix}-{base}"
        if not _slug_taken(candidate, session):
            return candidate
        # base collides — append random suffix
        for _ in range(20):
            suffix = "".join(random.choices(_CHARS, k=4))
            candidate = f"{prefix}-{base}-{suffix}"
            if not _slug_taken(candidate, session):
                return candidate
    # pure random
    for _ in range(50):
        suffix = "".join(random.choices(_CHARS, k=6))
        candidate = f"{prefix}-{suffix}"
        if not _slug_taken(candidate, session):
            return candidate
    raise RuntimeError(f"could not generate a unique {prefix} slug after 50 tries")


def assert_slug_available(slug: str, session: Session) -> None:
    """Raise ValueError if slug is taken in either table."""
    if _slug_taken(slug, session):
        raise ValueError(f"slug '{slug}' is already taken by an agent or workflow")
