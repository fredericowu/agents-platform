"""Budget guards for workflow runs — runaway-loop / runaway-spend security.

Tracks TWO independent counters per workflow tree, keyed by the **root**
workflow's ``run_id``:

  * **hops**   — total node executions (incl. sub-workflow nodes).
  * **tokens** — total tokens consumed (tokens_in + tokens_out) across the tree.

Either limit being breached aborts the **next** node execution gracefully —
already-running nodes are allowed to finish. Orchestrators catch the
``BudgetExceeded`` exception and return whatever partial output they had,
with a ``hop_limit_reached`` / ``token_limit_reached`` / ``limit_reached``
flag set in the response.

Usage:

```python
hops.init(root_run_id, max_hops=50, max_tokens=100_000)
try:
    hops.check_tokens(root_run_id)   # raise TokenLimitExceeded if hit
    hops.increment(root_run_id)      # raise HopLimitExceeded if hit
    res = await run_agent(...)
    hops.add_tokens(root_run_id, res.tokens_in + res.tokens_out)
finally:
    hops.clear(root_run_id)
```

The counters live in-process — runs are short-lived and a process restart
kills the run anyway.
"""
from __future__ import annotations


DEFAULT_MAX_HOPS = 50
DEFAULT_MAX_TOKENS = 0          # 0 = unlimited (the platform default)

_COUNTERS: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BudgetExceeded(Exception):
    """Base: budget breach. Orchestrators catch this to stop gracefully."""
    reason: str = "budget"


class HopLimitExceeded(BudgetExceeded):
    reason = "hops"


class TokenLimitExceeded(BudgetExceeded):
    reason = "tokens"


# ---------------------------------------------------------------------------
# Counter primitives
# ---------------------------------------------------------------------------

def init(root_run_id: str, *, max_hops: int = DEFAULT_MAX_HOPS,
         max_tokens: int = DEFAULT_MAX_TOKENS) -> None:
    """Create a fresh budget for a root workflow. Idempotent — re-init resets."""
    if not root_run_id:
        return
    _COUNTERS[root_run_id] = {
        "hops": 0,
        "max_hops": max(1, int(max_hops)),
        "tokens": 0,
        "max_tokens": max(0, int(max_tokens or 0)),
    }


def increment(root_run_id: str | None) -> int:
    """Charge one hop. Raises ``HopLimitExceeded`` when over budget.

    No-op (returns 0) if the counter wasn't initialised — e.g. agent runs
    that aren't nested under a workflow have no budget.
    """
    if not root_run_id:
        return 0
    c = _COUNTERS.get(root_run_id)
    if c is None:
        return 0
    c["hops"] += 1
    if c["hops"] > c["max_hops"]:
        raise HopLimitExceeded(
            f"hop limit ({c['max_hops']}) exceeded — possible runaway loop"
        )
    return c["hops"]


def add_tokens(root_run_id: str | None, n: int) -> int:
    """Accumulate tokens consumed. Does NOT raise (the next ``check_tokens``
    call gates the next execution)."""
    if not root_run_id or not n:
        return 0
    c = _COUNTERS.get(root_run_id)
    if c is None:
        return 0
    c["tokens"] += max(0, int(n))
    return c["tokens"]


def check_tokens(root_run_id: str | None) -> None:
    """Gate the next execution. Raises ``TokenLimitExceeded`` when over budget."""
    if not root_run_id:
        return
    c = _COUNTERS.get(root_run_id)
    if c is None:
        return
    if c["max_tokens"] and c["tokens"] >= c["max_tokens"]:
        raise TokenLimitExceeded(
            f"token limit ({c['max_tokens']}) reached — "
            f"{c['tokens']} tokens accumulated"
        )


def get(root_run_id: str | None) -> dict:
    """Public snapshot for events / API: ``{hops:{count,limit}, tokens:{count,limit}}``.

    Returns ``{}`` when no counter is initialised for this root.
    """
    if not root_run_id:
        return {}
    c = _COUNTERS.get(root_run_id)
    if c is None:
        return {}
    return {
        "hops":   {"count": c["hops"],   "limit": c["max_hops"]},
        "tokens": {"count": c["tokens"], "limit": c["max_tokens"]},
    }


def has(root_run_id: str | None) -> bool:
    return bool(root_run_id) and root_run_id in _COUNTERS


def clear(root_run_id: str | None) -> None:
    if not root_run_id:
        return
    _COUNTERS.pop(root_run_id, None)
