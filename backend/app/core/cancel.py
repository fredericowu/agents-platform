"""Tiny in-process cancellation registry.

When ``/api/runs/:id/cancel`` is hit, we add the run id and every descendant
run id to ``_CANCELLED``. The orchestrator checks this set between nodes,
and the executor checks at the start of each agent run, so newly-scheduled
work bails out instead of launching a fresh claude-CLI subprocess.
"""
from __future__ import annotations

_CANCELLED: set[str] = set()


def mark_cancelled(*run_ids: str) -> None:
    for r in run_ids:
        if r:
            _CANCELLED.add(r)


def is_cancelled(run_id: str | None) -> bool:
    return run_id is not None and run_id in _CANCELLED


def clear(run_id: str) -> None:
    _CANCELLED.discard(run_id)


class Cancelled(RuntimeError):
    """Raised when an orchestrator or executor stops because its run was cancelled."""
