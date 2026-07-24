"""Redis Streams helpers for durable CLI-agent event transport.

Two transport modes — both terminate in the same Redis Stream per run:

  WS mode (legacy, AP_CLI_WS_STREAM=1):
    aw-connector → WebSocket → ws_agent.py → XADD run:{run_id}:events

  Redis mode (default new path, AP_CLI_REDIS_STREAM=1):
    aw-connector-redis → XADD run:{run_id}:events directly (no WS hop)

Consumer side (both modes):
    cli.py astream() → consume_stream_into_queue() → asyncio.Queue → parse & emit

Token persistence for WS reconnect after platform restart:
  register_run() → SET run:{run_id}:token <token> EX 86400
  ws_agent.py lookup → GET run:{run_id}:token (fallback when not in memory)

When Redis is unreachable the helpers log a warning and return gracefully so
the caller can fall back to the direct-stdout path.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

log = logging.getLogger("ap.redis_streams")

_client = None
_lock = asyncio.Lock()

STREAM_TTL_S = 86400        # 1 day
STREAM_MAXLEN = 50_000      # max events per run before trimming
TOKEN_TTL_S = 86400         # token survives 1 day (well beyond any run)
GROUP_NAME = "ap-platform"


async def get_client():
    """Return a shared async Redis client, or None if Redis is unreachable."""
    global _client
    async with _lock:
        if _client is not None:
            return _client
        try:
            import redis.asyncio as aioredis
            from ..config import settings
            # socket_timeout MUST exceed the blocking XREAD window (block_ms, 2s).
            # If they're equal a normal "no new data for 2s" gap (common while an
            # agent is thinking / using tools) raises a socket TimeoutError, which
            # would abort the stream consumer and finalise the run empty.
            c = aioredis.from_url(settings.redis_url, decode_responses=True,
                                  socket_connect_timeout=2, socket_timeout=30,
                                  health_check_interval=30)
            await c.ping()
            _client = c
            log.info("Redis connected at %s", settings.redis_url)
        except Exception as e:
            log.warning("Redis unavailable (%s) — falling back to in-memory queue", e)
            _client = None
    return _client


async def reset_client() -> None:
    """Force reconnect on next call (e.g. after a connection error)."""
    global _client
    async with _lock:
        _client = None


def _stream_key(run_id: str) -> str:
    return f"run:{run_id}:events"


def _token_key(run_id: str) -> str:
    return f"run:{run_id}:token"


async def persist_token(run_id: str, token: str) -> None:
    """Persist the per-run WS auth token to Redis so aw-connector can reconnect after a platform restart."""
    r = await get_client()
    if r is None:
        return
    try:
        await r.set(_token_key(run_id), token, ex=TOKEN_TTL_S)
    except Exception as e:
        log.warning("persist_token failed run=%s: %s", run_id, e)
        await reset_client()


async def lookup_token(run_id: str) -> str | None:
    """Return the stored token for a run_id (used to validate reconnecting aw-connectors)."""
    r = await get_client()
    if r is None:
        return None
    try:
        return await r.get(_token_key(run_id))
    except Exception as e:
        log.warning("lookup_token failed run=%s: %s", run_id, e)
        await reset_client()
        return None


async def publish_line(run_id: str, line: str) -> None:
    """Append a raw CLI stdout line to the run's Redis Stream."""
    r = await get_client()
    if r is None:
        return
    try:
        await r.xadd(
            _stream_key(run_id),
            {"line": line},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
    except Exception as e:
        log.warning("publish_line failed run=%s: %s", run_id, e)
        await reset_client()


async def publish_done(run_id: str, returncode: int = 0) -> None:
    """Append the terminal 'done' sentinel to the run's Redis Stream."""
    r = await get_client()
    if r is None:
        return
    try:
        await r.xadd(
            _stream_key(run_id),
            {"done": "1", "returncode": str(returncode)},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
        await r.expire(_stream_key(run_id), STREAM_TTL_S)
    except Exception as e:
        log.warning("publish_done failed run=%s: %s", run_id, e)
        await reset_client()


async def ensure_group(run_id: str) -> bool:
    """Create the consumer group for a run's stream (idempotent). Returns True on success."""
    r = await get_client()
    if r is None:
        return False
    try:
        await r.xgroup_create(_stream_key(run_id), GROUP_NAME, id="0", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            log.warning("ensure_group failed run=%s: %s", run_id, e)
            await reset_client()
            return False
    return True


async def consume_stream_into_queue(run_id: str, queue: asyncio.Queue,
                                    block_ms: int = 2000) -> None:
    """Consume the Redis Stream for a run and push lines into an asyncio.Queue.

    Puts each stdout line as a str, and None as the done sentinel.
    Called as a background task from cli.py astream() in Redis mode.
    """
    r = await get_client()
    if r is None:
        await queue.put(None)
        return

    await ensure_group(run_id)
    key = _stream_key(run_id)
    consumer = f"exec-{run_id[:8]}"

    try:
        while True:
            try:
                entries = await r.xreadgroup(GROUP_NAME, consumer, {key: ">"},
                                             count=100, block=block_ms)
            except Exception as e:
                if "timeout" in str(e).lower():
                    continue  # blocking read window elapsed with no data — keep polling
                log.warning("consume_stream_into_queue error run=%s: %s", run_id, e)
                await reset_client()
                await queue.put(None)
                return

            if not entries:
                continue  # no new messages yet — keep blocking

            for _key, messages in entries:
                for msg_id, fields in messages:
                    try:
                        await r.xack(key, GROUP_NAME, msg_id)
                    except Exception:
                        pass
                    if fields.get("done"):
                        await queue.put(None)
                        return
                    # Accept both the connector's {type: stdout, line} format and
                    # the WS-copy {line} format — forward any entry carrying a line.
                    line = fields.get("line", "")
                    if line:
                        await queue.put(line)
    except Exception as e:
        log.warning("consume_stream_into_queue fatal run=%s: %s", run_id, e)
        await queue.put(None)


def _finished_channel(run_id: str) -> str:
    return f"run:{run_id}:finished"


async def notify_run_finished(run_id: str) -> None:
    """Publish a fire-and-forget signal that ``run_id`` reached a terminal status.

    Best-effort wake-up for anyone blocked in ``wait_run_finished`` on this run
    (e.g. the ``call_me_back`` watcher in wakeups.py). The DB row remains the
    source of truth — this is only a low-latency nudge, never load-bearing:
    a missed publish (no subscriber yet, Redis blip) just means the caller's
    own fallback poll picks up the terminal status on its next cycle instead.
    """
    r = await get_client()
    if r is None:
        return
    try:
        await r.publish(_finished_channel(run_id), "1")
    except Exception as e:
        log.warning("notify_run_finished failed run=%s: %s", run_id, e)
        await reset_client()


async def wait_run_finished(run_id: str, timeout_s: float) -> bool:
    """Block up to ``timeout_s`` for a ``notify_run_finished(run_id)`` signal.

    Returns True if a signal arrived, False on timeout/no-Redis/error — callers
    must treat False as "check the DB yourself", not as "still running".
    """
    r = await get_client()
    if r is None:
        return False
    pubsub = r.pubsub()
    try:
        await pubsub.subscribe(_finished_channel(run_id))
        # The first get_message() call after subscribe() typically just consumes
        # the subscribe-confirmation message itself — with ignore_subscribe_messages
        # that call returns None immediately WITHOUT waiting out `timeout`, it does
        # not keep blocking for a real publish. Must loop our own calls against a
        # wall-clock deadline so a stray subscribe ack doesn't short-circuit the wait.
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_s
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=remaining)
            if msg is not None:
                return True
    except Exception as e:
        log.warning("wait_run_finished failed run=%s: %s", run_id, e)
        await reset_client()
        return False
    finally:
        try:
            await pubsub.unsubscribe(_finished_channel(run_id))
            await pubsub.aclose()
        except Exception:
            pass


def _delivered_key(run_id: str) -> str:
    return f"run:{run_id}:delivered"


async def mark_delivered(run_id: str) -> bool:
    """Atomically claim delivery of a run's reply.

    Returns True if THIS caller won the claim (the reply had not been delivered
    yet), False if another path already delivered it. Redis-backed so the claim
    survives a platform restart: the live Telegram dispatch and the post-restart
    recovery path share one gate and can never both send the same reply.

    Fails **open** (returns True) when Redis is unavailable — a missing Redis
    should never silently swallow a user's reply.
    """
    r = await get_client()
    if r is None:
        return True
    try:
        won = await r.set(_delivered_key(run_id), "1", nx=True, ex=STREAM_TTL_S)
        return bool(won)
    except Exception as e:
        log.warning("mark_delivered failed run=%s: %s", run_id, e)
        await reset_client()
        return True


def _session_channel(session_id: str) -> str:
    return f"session:{session_id}:events"


_SESSION_CHANNEL_PATTERN = "session:*:events"


async def publish_session_event(session_id: str, source: str, text: str, run_id: str = "") -> None:
    """Fire-and-forget: a reply was just delivered on ``session_id`` by
    ``source`` (e.g. "telegram", "meta"). Cross-channel listeners (the other
    side's `run_session_event_listener`) use this to make every OTHER channel
    pinned to the same session aware of it — the originating channel already
    delivered normally through its own path and must never re-deliver to
    itself. Best-effort: a missed publish just means the other channel stays
    unaware of this one reply, nothing durable is lost."""
    if not session_id:
        return
    r = await get_client()
    if r is None:
        return
    try:
        payload = json.dumps({"source": source, "text": text, "run_id": run_id})
        await r.publish(_session_channel(session_id), payload)
    except Exception as e:
        log.warning("publish_session_event failed session=%s: %s", session_id, e)
        await reset_client()


async def run_session_event_listener(handler) -> None:
    """Long-lived background task: PSUBSCRIBE to every session-scoped event
    (across ALL sessions, via the `session:*:events` pattern) and call
    ``handler(session_id, data)`` for each one, where ``data`` is the dict
    passed to `publish_session_event` (source/text/run_id). ``handler`` may be
    sync or async. Reconnects with backoff on any Redis error so a transient
    blip doesn't permanently kill cross-channel delivery. Never returns —
    schedule with asyncio.create_task() once at process startup."""
    backoff = 1.0
    while True:
        r = await get_client()
        if r is None:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        pubsub = r.pubsub()
        try:
            await pubsub.psubscribe(_SESSION_CHANNEL_PATTERN)
            backoff = 1.0
            while True:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=30)
                if msg is None:
                    continue
                channel = (msg.get("channel") or "")
                parts = channel.split(":")
                if len(parts) != 3:
                    continue
                session_id = parts[1]
                try:
                    data = json.loads(msg["data"])
                except Exception:
                    continue
                try:
                    result = handler(session_id, data)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    log.exception("session_event handler failed for session=%s", session_id)
        except Exception as e:
            log.warning("session event listener error, reconnecting: %s", e)
            await reset_client()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
        finally:
            try:
                await pubsub.aclose()
            except Exception:
                pass


async def stream_has_data(run_id: str) -> bool:
    """Return True if a Redis Stream exists for this run and holds at least one entry.

    Used by startup recovery to decide whether an interrupted 'running' run still
    has a durable event log to re-attach to (vs. one that must be cancelled).
    """
    r = await get_client()
    if r is None:
        return False
    try:
        return bool(await r.xlen(_stream_key(run_id)))
    except Exception as e:
        log.warning("stream_has_data failed run=%s: %s", run_id, e)
        await reset_client()
        return False


async def replay_stream_into_queue(run_id: str, queue: asyncio.Queue,
                                   block_ms: int = 2000) -> None:
    """Replay a run's Redis Stream from the beginning into an asyncio.Queue.

    Unlike consume_stream_into_queue (which reads only *new* entries via a consumer
    group), this reads the FULL history from id 0 and then tails until the 'done'
    sentinel. Used to re-attach to an in-flight run after a platform restart: the
    container kept publishing while the platform was down, so we replay everything
    to rebuild the run's text/tokens and finalise it.
    """
    r = await get_client()
    if r is None:
        await queue.put(None)
        return

    key = _stream_key(run_id)
    last_id = "0"  # start from the very first entry, then advance
    try:
        while True:
            try:
                entries = await r.xread({key: last_id}, count=200, block=block_ms)
            except Exception as e:
                if "timeout" in str(e).lower():
                    continue  # blocking read window elapsed with no data — keep polling
                log.warning("replay_stream_into_queue error run=%s: %s", run_id, e)
                await reset_client()
                await queue.put(None)
                return

            if not entries:
                continue  # still waiting for the producer's next entry / done

            for _key, messages in entries:
                for msg_id, fields in messages:
                    last_id = msg_id
                    if fields.get("done"):
                        await queue.put(None)
                        return
                    line = fields.get("line", "")
                    if line:
                        await queue.put(line)
    except Exception as e:
        log.warning("replay_stream_into_queue fatal run=%s: %s", run_id, e)
        await queue.put(None)


async def consume_stream(run_id: str, consumer: str = "executor",
                         block_ms: int = 2000) -> AsyncIterator[str | None]:
    """Yield raw CLI stdout lines from the Redis Stream. Yields None when 'done' sentinel arrives."""
    r = await get_client()
    if r is None:
        return

    key = _stream_key(run_id)
    last_id = ">"  # consume only new entries for this consumer group

    while True:
        try:
            entries = await r.xreadgroup(GROUP_NAME, consumer, {key: last_id},
                                         count=100, block=block_ms)
        except Exception as e:
            if "timeout" in str(e).lower():
                continue  # blocking read window elapsed with no data — keep polling
            log.warning("consume_stream error run=%s: %s", run_id, e)
            await reset_client()
            return

        if not entries:
            continue

        for _key, messages in entries:
            for msg_id, fields in messages:
                try:
                    await r.xack(key, GROUP_NAME, msg_id)
                except Exception:
                    pass

                if "done" in fields:
                    yield None
                    return

                line = fields.get("line", "")
                if line:
                    yield line
