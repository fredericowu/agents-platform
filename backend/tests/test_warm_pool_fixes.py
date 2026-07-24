"""Coverage for the warm-pool fixes and the per-SESSION redesign (Target
agent-docker-coldstart-review, 2026-07-24): per-turn execution variables via
BASH_ENV, kill_run's post-restart warm-container resolution (now keyed by
session_id, not just agent_id), dispatch_turn's FIFO-write timeout, and
get_or_create's per-(agent_id, session_id) lock. (No AP_WARM_MAX cap —
Frederico explicitly rejected a docker-ps-per-dispatch cap/LRU-eviction
mechanism; the natural bound is promotion-after-first-turn plus the existing
6h in-container TTL.)

No live docker daemon required — `warm_pool._docker` and
`asyncio.create_subprocess_exec` are faked. Async tests are driven with plain
`asyncio.run()` (no pytest-asyncio dependency) to keep this runnable with the
project's existing shared venv.
"""
from __future__ import annotations

import asyncio
import json
import subprocess

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core import warm_pool
from backend.app.core.models import cli as cli_module


# ---------------------------------------------------------------------------
# Fix 1 — per-turn execution vars (turn_env / BASH_ENV)
# ---------------------------------------------------------------------------

def _local_sh_docker(rundir: str):
    """Fake `warm_pool._docker` that understands only the `exec -i <name> sh
    -c <cmd>` shape dispatch_turn issues, substitutes the hardcoded
    /home/ubuntu/.aw-warm path for a real tmp dir, and runs the shell command
    LOCALLY — validates the actual POSIX shell semantics (the if/fi exit-code
    fix) end-to-end without needing docker."""
    async def fake(*args, timeout=20.0):
        args = list(args)
        if len(args) >= 4 and args[0] == "exec" and args[1] == "-i" and "-c" in args:
            cmd = args[args.index("-c") + 1].replace("/home/ubuntu/.aw-warm", rundir)
            proc = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True)
            return proc.returncode, proc.stdout, proc.stderr
        return 0, "", ""
    return fake


class _FifoRecorder:
    """Fake process for the FIFO-write `asyncio.create_subprocess_exec` call
    — records whatever payload was written instead of touching a real FIFO."""
    def __init__(self, sink: list, *, hang: bool = False):
        self._sink = sink
        self._hang = hang
        self.returncode = 0
        self.killed = False

    async def communicate(self, data: bytes | None = None):
        if self._hang:
            await asyncio.sleep(1e9)
        self._sink.append(data)
        return b"", b""

    def kill(self):
        self.killed = True


def test_dispatch_turn_writes_turn_env(tmp_path, monkeypatch):
    """NOTION_TASK_ID/AW_SOURCE_DEVICE change every turn and turn_env is a
    plain overwrite each time. AW_SESSION_ID is deliberately NOT in turn_env
    under the per-session redesign — it's a static `-e` env var baked in at
    spawn (session_id == the container's own key), so dispatch_turn never
    needs to write or preserve it."""
    async def run():
        rundir = str(tmp_path)
        monkeypatch.setattr(warm_pool, "_docker", _local_sh_docker(rundir))
        sink: list = []
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec(sink))

        await warm_pool.dispatch_turn(
            name="aw-warm-agent1-sess1", run_id="run-1", prompt="hello",
            notion_task_id="TASK-1", source_device="watch",
        )
        turn_env = (tmp_path / "turn_env").read_text()
        assert "export NOTION_TASK_ID=TASK-1" in turn_env
        assert "export AW_SOURCE_DEVICE=watch" in turn_env
        assert "AW_SESSION_ID" not in turn_env
        assert (tmp_path / "current_run_id").read_text() == "run-1"
        assert json.loads(sink[0].decode().strip())["message"]["content"] == "hello"

        await warm_pool.dispatch_turn(
            name="aw-warm-agent1-sess1", run_id="run-2", prompt="hello again",
            notion_task_id="TASK-2", source_device="iphone",
        )
        turn_env = (tmp_path / "turn_env").read_text()
        assert "export NOTION_TASK_ID=TASK-2" in turn_env
        assert "export AW_SOURCE_DEVICE=iphone" in turn_env
        assert "TASK-1" not in turn_env
        assert (tmp_path / "current_run_id").read_text() == "run-2"

    asyncio.run(run())


def _fake_exec(sink: list):
    async def fake(*args, **kwargs):
        return _FifoRecorder(sink)
    return fake


def test_dispatch_turn_fifo_write_timeout(tmp_path, monkeypatch):
    """A wedged container's FIFO write must raise, not hang forever."""
    async def run():
        rundir = str(tmp_path)
        monkeypatch.setattr(warm_pool, "_docker", _local_sh_docker(rundir))
        monkeypatch.setattr(warm_pool, "FIFO_WRITE_TIMEOUT_S", 0.2)

        hung = _FifoRecorder([], hang=True)

        async def fake_exec(*args, **kwargs):
            return hung
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

        with pytest.raises(RuntimeError, match="did not complete"):
            await warm_pool.dispatch_turn(name="aw-warm-agent1", run_id="run-1", prompt="hi")
        assert hung.killed

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Fix 4 — get_or_create serializes concurrent calls for the same SESSION
# (2026-07-24 redesign: per (agent_id, session_id), not per agent_id alone)
# ---------------------------------------------------------------------------

class _FakeDockerRegistry:
    """In-memory stand-in for the docker daemon, just enough of `inspect`/
    `rename`/`rm`/`run`/`exec ... test -f` for get_or_create()'s flow."""
    def __init__(self, spawn_delay: float = 0.05):
        self.containers: dict[str, dict] = {}
        self.run_count = 0
        self.spawn_delay = spawn_delay

    async def __call__(self, *args, timeout: float = 20.0):
        args = list(args)
        if args[0] == "inspect":
            name = args[-1]
            c = self.containers.get(name)
            if c is None:
                return 1, "", "no such object"
            fmt = args[2]
            if "Labels" in fmt:
                return 0, json.dumps(c["labels"]), ""
            if "Running" in fmt:
                return 0, "true" if c["running"] else "false", ""
            return 0, "", ""
        if args[0] == "rename":
            old, new = args[1], args[2]
            if old not in self.containers:
                return 1, "", "no such object"
            self.containers[new] = self.containers.pop(old)
            return 0, "", ""
        if args[0] == "rm":
            self.containers.pop(args[-1], None)
            return 0, "", ""
        if args[0] == "run":
            name = None
            labels: dict[str, str] = {}
            i = 0
            while i < len(args):
                if args[i] == "--name":
                    name = args[i + 1]
                    i += 2
                    continue
                if args[i] == "--label":
                    k, _, v = args[i + 1].partition("=")
                    labels[k] = v
                    i += 2
                    continue
                i += 1
            await asyncio.sleep(self.spawn_delay)
            self.run_count += 1
            self.containers[name] = {"labels": labels, "running": True}
            return 0, "", ""
        if args[0] == "exec" and "test" in args:
            return 0, "", ""  # _wait_ready's readiness probe
        return 0, "", ""


def _build_argv(name: str, epoch: str, token: str) -> list[str]:
    return ["docker", "run", "-d", "--name", name,
            "--label", "aw.warm=1",
            "--label", f"aw.epoch={epoch}", "--label", f"aw.warm_token={token}"]


def test_get_or_create_serializes_concurrent_same_session(monkeypatch):
    """Two near-simultaneous dispatches to the SAME (agent_id, session_id)
    must not both pass the stale-check and race on `docker run` — the lock
    should turn that into a queue: exactly one spawn, both calls resolve to
    the same container name."""
    async def run():
        fake = _FakeDockerRegistry()
        monkeypatch.setattr(warm_pool, "_docker", fake)

        results = await asyncio.gather(
            warm_pool.get_or_create(agent_id="agent-1", session_id="sess-a",
                                    epoch_hash="epoch-1", build_argv=_build_argv),
            warm_pool.get_or_create(agent_id="agent-1", session_id="sess-a",
                                    epoch_hash="epoch-1", build_argv=_build_argv),
        )
        assert results[0][0] == results[1][0] == "aw-warm-agent-1-sess-a"
        assert fake.run_count == 1

    asyncio.run(run())


def test_get_or_create_different_sessions_not_serialized_together(monkeypatch):
    """Sanity check the lock is per-SESSION, not per-agent or global — this
    is the exact bug the redesign fixes: two DIFFERENT sessions of the SAME
    agent must each get their own container, fully in parallel, never
    routed through one shared FIFO/claude process."""
    async def run():
        fake = _FakeDockerRegistry()
        monkeypatch.setattr(warm_pool, "_docker", fake)

        r1, r2, r3 = await asyncio.gather(
            warm_pool.get_or_create(agent_id="agent-1", session_id="sess-a",
                                    epoch_hash="epoch-1", build_argv=_build_argv),
            warm_pool.get_or_create(agent_id="agent-1", session_id="sess-b",
                                    epoch_hash="epoch-1", build_argv=_build_argv),
            warm_pool.get_or_create(agent_id="agent-2", session_id="sess-a",
                                    epoch_hash="epoch-1", build_argv=_build_argv),
        )
        names = {r1[0], r2[0], r3[0]}
        assert names == {"aw-warm-agent-1-sess-a", "aw-warm-agent-1-sess-b", "aw-warm-agent-2-sess-a"}
        assert fake.run_count == 3  # three distinct containers, zero cross-session collisions

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Fix 2 — kill_run resolves a warm container after an AP restart
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode

    async def wait(self):
        return self.returncode

    async def communicate(self, data: bytes | None = None):
        return b"", b""

    def kill(self):
        pass


@pytest.fixture
def sqlite_db():
    """In-memory SQLite with just the Agent/Run tables — NOT the full
    `init_db()` (unrelated pre-existing breakage: some other table uses a
    Postgres-only JSONB column that SQLite's DDL compiler can't render;
    already reproduces on `main` for every existing `init_db()`-based test
    in this suite, e.g. test_retro_scores_api.py — not something this
    change touches or needs to fix)."""
    import backend.app.db as db_mod
    from backend.app.models import Agent, Run

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    orig_engine, orig_session = db_mod.engine, db_mod.SessionLocal
    db_mod.engine = eng
    db_mod.SessionLocal = sessionmaker(bind=eng, autoflush=False, autocommit=False, future=True)
    db_mod.Base.metadata.create_all(eng, tables=[Agent.__table__, Run.__table__])
    yield db_mod
    db_mod.engine, db_mod.SessionLocal = orig_engine, orig_session
    eng.dispose()


def test_kill_run_resolves_warm_container_after_restart(sqlite_db, monkeypatch):
    """After a restart wipes `_RUN_CONTAINER_NAMES`, kill_run's DB fallback
    must resolve a warm-mode run to its SESSION'S REAL warm container name
    (aw-warm-<agent_id>-<session_id>), not the ephemeral aw-run-<id> shape
    that never existed for that run, and not the agent-only shape from
    before the per-session redesign."""
    async def run():
        from backend.app.models import Agent, Run

        with sqlite_db.session_scope() as s:
            agent = Agent(slug="warm-agent-x", name="Warm Agent X")
            s.add(agent)
            s.flush()
            agent_id = agent.id
            s.add(Run(id="run-warm-1", kind="agent", target_slug="t", target_id="tid",
                      source_slug="warm-agent-x", status="running", session_id="sess-warm-1"))

        cli_module._RUN_CONTAINER_NAMES.pop("run-warm-1", None)
        monkeypatch.setattr(warm_pool, "enabled", lambda: True)
        expected_name = warm_pool.warm_container_name(agent_id, "sess-warm-1")

        async def fake_is_running(name):
            return name == expected_name
        monkeypatch.setattr(warm_pool, "is_running", fake_is_running)

        calls: list[tuple] = []

        async def fake_create_subprocess_exec(*args, **kwargs):
            calls.append(tuple(args))
            return _FakeProc(0)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        n = await cli_module.kill_run("run-warm-1")
        assert n >= 1
        assert ("docker", "kill", expected_name) in calls
        assert ("docker", "rm", "-f", expected_name) in calls

    asyncio.run(run())


def test_kill_run_falls_back_to_ephemeral_when_no_warm_container_running(sqlite_db, monkeypatch):
    """When AP_WARM_CONTAINER is off (or that agent has no running warm
    container), the DB fallback must behave exactly as before — targeting
    the ephemeral aw-run-<id> name."""
    async def run():
        from backend.app.models import Agent, Run
        from backend.app.core.tools.docker_agent import container_name_for_run

        with sqlite_db.session_scope() as s:
            agent = Agent(slug="ephemeral-agent", name="Ephemeral Agent")
            s.add(agent)
            s.flush()
            s.add(Run(id="run-ephemeral-1", kind="agent", target_slug="t", target_id="tid",
                      source_slug="ephemeral-agent", status="running"))

        cli_module._RUN_CONTAINER_NAMES.pop("run-ephemeral-1", None)
        monkeypatch.setattr(warm_pool, "enabled", lambda: False)

        calls: list[tuple] = []

        async def fake_create_subprocess_exec(*args, **kwargs):
            calls.append(tuple(args))
            return _FakeProc(0)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        expected_name = container_name_for_run("run-ephemeral-1")
        n = await cli_module.kill_run("run-ephemeral-1")
        assert n >= 1
        assert ("docker", "kill", expected_name) in calls

    asyncio.run(run())
