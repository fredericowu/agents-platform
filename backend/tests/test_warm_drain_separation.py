"""Pins the hard product requirement from Target agent-docker-coldstart-review
(2026-07-24 correction): the warm-container graceful drain path and
`kill_run` (cli.py's hard-abort path) must NEVER share an implementation or
a verb, even accidentally.

kill_run (backend/app/core/models/cli.py:84) must stay pure SIGKILL
`docker kill` + `docker rm -f` — no signals, no `/aw/drain` awareness, ever.
Drain (backend/app/core/warm_pool.py's `drain()`) must stay a flag file
(`docker exec <name> touch .../drain`) — no `docker kill`/`docker stop`/any
signal verb against docker, ever. This is a static, grep-based check
(rather than a live-docker integration test) so it fails fast in CI with no
docker daemon required.
"""
from __future__ import annotations

import inspect
import re

from backend.app.core import warm_pool
from backend.app.core.models import cli as cli_module


def _source(fn) -> str:
    return inspect.getsource(fn)


def test_kill_run_never_references_drain_flag():
    src = _source(cli_module.kill_run)
    assert "/aw/drain" not in src
    assert ".aw-warm" not in src
    assert "drain" not in src.lower()


def test_drain_never_shells_out_to_docker_kill_or_stop():
    src = _source(warm_pool.drain)
    # The only docker verb `drain()` may issue is "exec ... touch <flag>".
    # Explicitly forbid the abort verbs (as actual `_docker(...)` calls, not
    # just the word appearing in prose/log text) so a future edit can't
    # quietly turn this into (or start additionally calling) kill_run's
    # SIGKILL mechanism.
    assert not re.search(r'_docker\(\s*"(kill|stop)"', src)
    assert "touch" in src


def test_drain_module_has_no_kill_or_stop_docker_calls():
    """Broader net over the whole module (not just `drain()` itself) —
    catches a helper added later that routes through kill/stop on drain's
    behalf without `drain()`'s own source changing."""
    src = inspect.getsource(warm_pool)
    forbidden = [
        m for m in re.finditer(r'_docker\(\s*"(kill|stop)"', src)
    ]
    assert not forbidden, f"warm_pool.py must never call docker kill/stop: {forbidden}"


def test_warm_pool_drain_uses_docker_exec_touch():
    src = _source(warm_pool.drain)
    assert re.search(r'_docker\(\s*"exec"', src), "drain() must use `docker exec ... touch`"
