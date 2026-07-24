#!/usr/bin/env python3
"""AW Remote Agent — Linux/Docker client.

Connects to the agents-platform WebSocket and responds to exec and fs commands.
Run this inside any machine/container you want to control from AW.

Usage:
    python3 /opt/agentic-workspace/src/remote_agent_client.py \
        --id <profile-uuid> \
        --url ws://host.docker.internal:10005

The profile UUID must exist in the agents-platform remote_agents table.
Use 'aw start remote-agent' to run with the pre-registered aw-sandbox profile.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error

VERSION = "1.2.0"

UPDATE_CHECK_INTERVAL = 300  # seconds, matches the Windows client's 5 min poll
UPDATE_CHECK_TIMEOUT = 30

log = logging.getLogger("aw-remote-agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Set once in main() from --dir. None = unrestricted (legacy behavior,
# back-compat with installs that don't pass --dir): every fs op and the
# exec cwd are confined to this directory when set.
SHARE_ROOT: str | None = None

# Set once in main() from --foreground. When true, every inbound message and
# outbound reply is logged at INFO level (command text, fs ops, exec chunks) —
# a live traffic dump for a human watching the terminal, not just connection
# lifecycle events.
LIVE_TRAFFIC = False


def _resolve_scoped(path: str) -> str:
    """Resolve `path` against SHARE_ROOT and reject anything that escapes it.

    Relative paths are joined onto SHARE_ROOT; absolute paths are only
    accepted if they already fall inside it. Raises PermissionError
    otherwise, which callers turn into a normal error response instead of
    crashing the connection.
    """
    if not SHARE_ROOT:
        return path
    candidate = path if os.path.isabs(path) else os.path.join(SHARE_ROOT, path)
    resolved = os.path.realpath(candidate)
    if resolved != SHARE_ROOT and not resolved.startswith(SHARE_ROOT + os.sep):
        raise PermissionError(f"path escapes shared directory {SHARE_ROOT}: {path}")
    return resolved


def _system_info() -> dict:
    try:
        import psutil
        ram_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
        cpu_count = psutil.cpu_count(logical=True)
    except ImportError:
        ram_gb = 0
        cpu_count = os.cpu_count() or 1
    return {
        "hostname": platform.node(),
        "os": platform.system().lower(),
        "os_version": platform.version()[:80],
        "cpu_count": cpu_count,
        "ram_gb": ram_gb,
        "arch": platform.machine(),
        "python": platform.python_version(),
    }


async def _handle_exec(ws, req_id: str, command: str, timeout: float = 900) -> None:
    """Run `command` and stream stdout/stderr back chunk by chunk as it's produced.

    Sends 0+ `exec_chunk` messages followed by exactly one `exec_done`, instead
    of buffering the whole output and replying once — long-running commands
    (e.g. `docker compose logs -f`, builds) are visible to the caller live
    instead of only after they finish or hit the request timeout.
    """
    import websockets

    send_lock = asyncio.Lock()

    async def _send(payload: dict) -> None:
        async with send_lock:
            try:
                await ws.send(json.dumps(payload))
            except (websockets.exceptions.ConnectionClosed, OSError) as e:
                # Connection dropped mid-exec (server restart, network blip).
                # Swallow here instead of letting it escape the fire-and-forget
                # create_task() call, which would otherwise surface as an
                # unhandled "Task exception was never retrieved" log line.
                log.debug("exec[%s]: dropped %s, connection closed: %s",
                          req_id, payload.get("type"), e)

    async def _pump(stream, stream_name: str) -> None:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            text = chunk.decode(errors="replace")
            if LIVE_TRAFFIC:
                for line in text.splitlines():
                    log.info("   [%s/%s] %s", req_id, stream_name, line)
            await _send({
                "type": "exec_chunk",
                "req_id": req_id,
                "stream": stream_name,
                "data": text,
            })

    if LIVE_TRAFFIC:
        log.info(">> exec[%s]: %s", req_id, command)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=SHARE_ROOT or os.environ.get("HOME", "/"),
        )
        try:
            await asyncio.wait_for(
                asyncio.gather(_pump(proc.stdout, "stdout"), _pump(proc.stderr, "stderr"), proc.wait()),
                timeout=timeout,
            )
            returncode = proc.returncode
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            await _send({
                "type": "exec_chunk", "req_id": req_id, "stream": "stderr",
                "data": f"\nCommand timed out after {timeout:.0f}s\n",
            })
            returncode = -1
        if LIVE_TRAFFIC:
            log.info("<< exec[%s] done, returncode=%s", req_id, returncode)
        await _send({"type": "exec_done", "req_id": req_id, "returncode": returncode})
    except Exception as e:
        if LIVE_TRAFFIC:
            log.info("<< exec[%s] error: %s", req_id, e)
        await _send({"type": "exec_chunk", "req_id": req_id, "stream": "stderr", "data": str(e)})
        await _send({"type": "exec_done", "req_id": req_id, "returncode": -1})


async def _handle_fs(ws, req_id: str, op: str, path: str,
                     data: str = "", offset: int = 0,
                     size: int = 65536, dest: str = "") -> None:
    if LIVE_TRAFFIC:
        log.info(">> fs[%s]: %s %s%s", req_id, op, path, f" -> {dest}" if dest else "")
    try:
        path = _resolve_scoped(path)
        if dest:
            dest = _resolve_scoped(dest)
        if op == "read":
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read(size)
            await ws.send(json.dumps({
                "type": "fs_response", "req_id": req_id,
                "data": base64.b64encode(chunk).decode(),
            }))
        elif op == "write":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "wb") as f:
                f.write(base64.b64decode(data))
            await ws.send(json.dumps({"type": "fs_response", "req_id": req_id, "ok": True}))
        elif op == "stat":
            s = os.stat(path)
            await ws.send(json.dumps({
                "type": "fs_response", "req_id": req_id,
                "size": s.st_size, "is_dir": os.path.isdir(path),
                "mtime": int(s.st_mtime), "mode": s.st_mode,
            }))
        elif op == "list":
            entries = []
            for name in os.listdir(path):
                full = os.path.join(path, name)
                try:
                    s = os.stat(full)
                    entries.append({"name": name, "size": s.st_size,
                                    "is_dir": os.path.isdir(full), "mtime": int(s.st_mtime)})
                except OSError:
                    pass
            await ws.send(json.dumps({"type": "fs_response", "req_id": req_id, "entries": entries}))
        elif op == "mkdir":
            os.makedirs(path, exist_ok=True)
            await ws.send(json.dumps({"type": "fs_response", "req_id": req_id, "ok": True}))
        elif op == "delete":
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            await ws.send(json.dumps({"type": "fs_response", "req_id": req_id, "ok": True}))
        elif op == "move":
            shutil.move(path, dest)
            await ws.send(json.dumps({"type": "fs_response", "req_id": req_id, "ok": True}))
        else:
            await ws.send(json.dumps({"type": "fs_response", "req_id": req_id,
                                      "error": f"unknown op: {op}"}))
    except Exception as e:
        await ws.send(json.dumps({"type": "fs_response", "req_id": req_id, "error": str(e)}))


async def _handle_write_chunk(ws, req_id: str, path: str, data: str, eof: bool,
                              _state: dict = {}) -> None:
    """Streaming upload: append one base64 chunk to a `path + ".part"` temp
    file, hashing incrementally; on eof, atomically rename the temp file onto
    `path` and ack with the sha256 of the complete file, so the backend (which
    independently hashes the bytes it forwarded) can cross-check the two.

    Writing to a temp file and renaming only once the transfer completes means
    a reader of `path` never observes a partially-written file, and a
    failed/aborted upload never corrupts an existing file at `path`.

    `_state` is a per-process dict keyed by req_id tracking the running
    hasher for this request, so repeated calls with the same req_id
    append/update rather than overwrite. Cleaned up on eof/error.
    """
    try:
        path = _resolve_scoped(path)
    except PermissionError as e:
        await ws.send(json.dumps({"type": "fs_write_chunk_ack", "req_id": req_id, "error": str(e)}))
        return
    tmp_path = path + ".part"
    try:
        raw = base64.b64decode(data) if data else b""
        if req_id not in _state:
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            # Truncate/create the temp file on the first chunk of this request.
            with open(tmp_path, "wb"):
                pass
            _state[req_id] = hashlib.sha256()
        hasher = _state[req_id]
        if raw:
            with open(tmp_path, "ab") as f:
                f.write(raw)
            hasher.update(raw)
        if eof:
            _state.pop(req_id, None)
            os.replace(tmp_path, path)
            await ws.send(json.dumps({
                "type": "fs_write_chunk_ack", "req_id": req_id, "ok": True,
                "sha256": hasher.hexdigest(),
            }))
    except Exception as e:
        _state.pop(req_id, None)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        await ws.send(json.dumps({"type": "fs_write_chunk_ack", "req_id": req_id, "error": str(e)}))


async def _handle_read_request(ws, req_id: str, path: str,
                               chunk_size: int = 256 * 1024) -> None:
    """Streaming download: read `path` and push it back as fs_read_chunk
    messages of ~chunk_size bytes each. The last message carries eof=true
    plus the sha256 of the complete file, hashed incrementally as it's read
    (documents the checksum for any consumer of the raw WS protocol; the
    backend's REST /download endpoint additionally verifies bytes on its own
    by re-hashing what it spools to disk, rather than trusting this blindly)."""
    try:
        path = _resolve_scoped(path)
        if not os.path.isfile(path):
            await ws.send(json.dumps({
                "type": "fs_read_chunk", "req_id": req_id,
                "error": f"No such file: {path}",
            }))
            return
        hasher = hashlib.sha256()
        with open(path, "rb") as f:
            # One-chunk lookahead so `eof` is only true on the message that
            # actually carries the last bytes (avoids an extra empty final
            # message and correctly handles files whose size is an exact
            # multiple of chunk_size).
            current = f.read(chunk_size)
            hasher.update(current)
            while True:
                nxt = f.read(chunk_size)
                eof = len(nxt) == 0
                if not eof:
                    hasher.update(nxt)
                msg = {
                    "type": "fs_read_chunk", "req_id": req_id,
                    "data": base64.b64encode(current).decode(),
                    "eof": eof,
                }
                if eof:
                    msg["sha256"] = hasher.hexdigest()
                await ws.send(json.dumps(msg))
                if eof:
                    break
                current = nxt
    except Exception as e:
        await ws.send(json.dumps({
            "type": "fs_read_chunk", "req_id": req_id, "error": str(e),
        }))


# ── Tunnel ───────────────────────────────────────────────────────────────────

async def _handle_tunnel(ws_url: str, tunnel_id: str, target_port: int) -> None:
    """Bridge one TCP connection: dial 127.0.0.1:target_port on THIS machine
    and relay bytes to/from a dedicated websocket the server opened for this
    tunnel_id. One WS per tunnel connection, not multiplexed on the control
    channel — a stalled tunnel can't head-of-line-block exec/fs traffic or
    any other concurrent tunnel. Always dials loopback only — target_port is
    server-supplied but the host is hardcoded, so the server can never make
    this agent reach out to an arbitrary network address.
    """
    import websockets

    if LIVE_TRAFFIC:
        log.info(">> tunnel[%s]: dialing 127.0.0.1:%s", tunnel_id, target_port)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", target_port)
    except Exception as e:
        log.warning("tunnel[%s]: local connect to port %s failed: %s", tunnel_id, target_port, e)
        return

    tunnel_url = f"{ws_url}/ws/tunnel/{tunnel_id}"
    try:
        async with websockets.connect(
            tunnel_url, ping_interval=20, ping_timeout=30, open_timeout=10,
        ) as tws:
            async def _local_to_ws():
                try:
                    while True:
                        chunk = await reader.read(65536)
                        if not chunk:
                            break
                        await tws.send(chunk)
                except Exception:
                    pass

            async def _ws_to_local():
                try:
                    async for chunk in tws:
                        writer.write(chunk if isinstance(chunk, bytes) else chunk.encode())
                        await writer.drain()
                except Exception:
                    pass

            await asyncio.gather(_local_to_ws(), _ws_to_local(), return_exceptions=True)
    except Exception as e:
        log.warning("tunnel[%s]: error: %s", tunnel_id, e)
    finally:
        writer.close()
        if LIVE_TRAFFIC:
            log.info("<< tunnel[%s]: closed", tunnel_id)


# ── Self-update ───────────────────────────────────────────────────────────────

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _http_get(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


async def _check_and_apply_update(base_url: str, script_path: str) -> None:
    """Check /api/update/linux-latest; if a newer version is published,
    download the new agent.py, verify its sha256, and — only if that passes —
    swap it into place and exit(0) so systemd (Restart=always) respawns us
    running the new code. Never applies an update that fails checksum
    verification, and never overwrites the running script before the
    download is fully verified (avoids bricking the service).
    """
    latest_url = f"{base_url}/api/update/linux-latest"
    try:
        raw = await asyncio.to_thread(_http_get, latest_url, UPDATE_CHECK_TIMEOUT)
        info = json.loads(raw)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return  # no update published
        log.warning("update check failed: HTTP %s", e.code)
        return
    except Exception as e:
        log.warning("update check failed: %s", e)
        return

    new_version = info.get("version", "")
    sha256 = info.get("sha256", "")
    if not new_version or new_version == VERSION:
        return

    log.info("update available: %s -> %s, downloading...", VERSION, new_version)
    script_url = f"{base_url}/api/update/linux-script"
    tmp_path = script_path + ".new"
    try:
        data = await asyncio.to_thread(_http_get, script_url, 60)
        with open(tmp_path, "wb") as f:
            f.write(data)
    except Exception as e:
        log.warning("update download failed: %s", e)
        return

    if sha256:
        got = _sha256_file(tmp_path)
        if got != sha256:
            log.warning("update sha256 mismatch (got %s, want %s), aborting", got, sha256)
            os.remove(tmp_path)
            return

    # Sanity check: the new file must at least parse as Python before we
    # commit to it — a corrupt/partial download must not replace a working
    # script (systemd would then loop-crash with no rollback).
    try:
        compile(data, tmp_path, "exec")
    except SyntaxError as e:
        log.warning("update failed syntax check: %s, aborting", e)
        os.remove(tmp_path)
        return

    backup_path = script_path + ".bak"
    try:
        shutil.copy2(script_path, backup_path)
        os.replace(tmp_path, script_path)
    except Exception as e:
        log.warning("update swap failed: %s, aborting", e)
        return

    log.info("update applied (%s -> %s, backup at %s). Exiting for systemd restart.",
              VERSION, new_version, backup_path)
    # Give the log line time to flush, then exit cleanly. Restart=always in
    # aw-remote-agent.service respawns us immediately, running the new file.
    await asyncio.sleep(1)
    sys.exit(0)


async def _update_loop(base_url: str, script_path: str) -> None:
    while True:
        await asyncio.sleep(UPDATE_CHECK_INTERVAL)
        try:
            await _check_and_apply_update(base_url, script_path)
        except SystemExit:
            raise
        except Exception as e:
            log.warning("update loop error: %s", e)


async def connect_and_serve(profile_id: str, ws_url: str, token: str) -> None:
    import websockets
    from urllib.parse import quote

    # token is optional here (server-side rollout note in remote_agents.py):
    # during the transition, already-running/auto-updated clients that
    # haven't been given a token yet still connect the old way (no query
    # param) against a server running in grace mode; once each machine is
    # relaunched with --token, it switches to the authenticated form. Once
    # every profile has reconnected with a token, grace mode is turned off
    # server-side and the old (tokenless) form stops working entirely.
    url = f"{ws_url}/ws/client/{profile_id}"
    if token:
        url += f"?token={quote(token)}"
    backoff = 2.0

    while True:
        try:
            log.info("Connecting to %s ...", url)
            async with websockets.connect(url, ping_interval=20, ping_timeout=30,
                                          open_timeout=10) as ws:
                backoff = 2.0
                log.info("Connected. Sending handshake.")
                await ws.send(json.dumps({"type": "handshake", "info": _system_info()}))

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    kind = msg.get("type")
                    if LIVE_TRAFFIC and kind != "ping":
                        log.info(">> recv: %s", raw)
                    if kind == "exec":
                        asyncio.create_task(_handle_exec(
                            ws, msg["req_id"], msg["command"], msg.get("timeout", 900),
                        ))
                    elif kind == "fs_request":
                        asyncio.create_task(_handle_fs(
                            ws, msg["req_id"], msg.get("op", ""), msg.get("path", ""),
                            msg.get("data", ""), msg.get("offset", 0),
                            msg.get("size", 65536), msg.get("dest", ""),
                        ))
                    elif kind == "fs_write_chunk":
                        asyncio.create_task(_handle_write_chunk(
                            ws, msg["req_id"], msg.get("path", ""),
                            msg.get("data", ""), msg.get("eof", False),
                        ))
                    elif kind == "fs_read_request":
                        asyncio.create_task(_handle_read_request(
                            ws, msg["req_id"], msg.get("path", ""),
                        ))
                    elif kind == "tunnel_request":
                        asyncio.create_task(_handle_tunnel(
                            ws_url, msg["tunnel_id"], msg["target_port"],
                        ))
                    elif kind == "ping":
                        await ws.send(json.dumps({"type": "pong"}))

        except Exception as e:
            log.warning("Disconnected (%s). Reconnecting in %.0fs ...", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 30)


def _ws_to_http(ws_url: str) -> str:
    if ws_url.startswith("wss://"):
        return "https://" + ws_url[len("wss://"):]
    if ws_url.startswith("ws://"):
        return "http://" + ws_url[len("ws://"):]
    return ws_url


async def _run(profile_id: str, ws_url: str, token: str, no_update: bool) -> None:
    base_url = _ws_to_http(ws_url)
    script_path = os.path.abspath(__file__)
    tasks = [asyncio.create_task(connect_and_serve(profile_id, ws_url, token))]
    if not no_update:
        tasks.append(asyncio.create_task(_update_loop(base_url, script_path)))
    try:
        await asyncio.gather(*tasks)
    except SystemExit:
        for t in tasks:
            t.cancel()
        raise


def main():
    global SHARE_ROOT, LIVE_TRAFFIC
    parser = argparse.ArgumentParser(description="AW Remote Agent — Linux client")
    parser.add_argument("--id", required=True, help="Profile UUID from agents-platform")
    parser.add_argument("--token", default="",
                        help="Per-profile connection secret from the Remote Agents UI "
                             "(RemoteAgentRow.token). Optional only during the grace-mode "
                             "rollout window — the server will require it once grace mode "
                             "is turned off.")
    parser.add_argument("--url", default="ws://localhost:10005",
                        help="WebSocket base URL of agents-platform (default: ws://localhost:10005)")
    parser.add_argument("--no-update", action="store_true",
                        help="Disable the self-update background check")
    parser.add_argument("--dir", default="",
                        help="Confine all file ops and command execution to this directory "
                             "(default: unrestricted, full filesystem access)")
    parser.add_argument("--foreground", "--live-session", dest="foreground", action="store_true",
                        help="Run attended in this terminal only: no service/daemon involved "
                             "(that's install.sh's job, not this flag), every inbound command "
                             "and outbound chunk is logged live, self-update is disabled, and "
                             "Ctrl+C cleanly disconnects and exits.")
    args = parser.parse_args()

    if args.dir:
        SHARE_ROOT = os.path.realpath(os.path.expanduser(args.dir))
        os.makedirs(SHARE_ROOT, exist_ok=True)

    LIVE_TRAFFIC = args.foreground
    no_update = args.no_update or args.foreground

    log.info("AW Remote Agent starting (v%s). Profile: %s -> %s", VERSION, args.id, args.url)
    if SHARE_ROOT:
        log.info("Scoped to shared directory: %s", SHARE_ROOT)
    if args.foreground:
        log.info("Foreground/live session — press Ctrl+C to disconnect and exit.")
    try:
        asyncio.run(_run(args.id, args.url, args.token, no_update))
    except SystemExit:
        raise
    except KeyboardInterrupt:
        if args.foreground:
            log.info("Ctrl+C received — disconnecting, session ended.")


if __name__ == "__main__":
    main()
