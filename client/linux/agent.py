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
import json
import logging
import os
import platform
import shutil
import subprocess
import sys

log = logging.getLogger("aw-remote-agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


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
    send_lock = asyncio.Lock()

    async def _send(payload: dict) -> None:
        async with send_lock:
            await ws.send(json.dumps(payload))

    async def _pump(stream, stream_name: str) -> None:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            await _send({
                "type": "exec_chunk",
                "req_id": req_id,
                "stream": stream_name,
                "data": chunk.decode(errors="replace"),
            })

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.environ.get("HOME", "/"),
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
        await _send({"type": "exec_done", "req_id": req_id, "returncode": returncode})
    except Exception as e:
        await _send({"type": "exec_chunk", "req_id": req_id, "stream": "stderr", "data": str(e)})
        await _send({"type": "exec_done", "req_id": req_id, "returncode": -1})


async def _handle_fs(ws, req_id: str, op: str, path: str,
                     data: str = "", offset: int = 0,
                     size: int = 65536, dest: str = "") -> None:
    try:
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


async def connect_and_serve(profile_id: str, ws_url: str) -> None:
    import websockets

    url = f"{ws_url}/ws/client/{profile_id}"
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
                    elif kind == "ping":
                        await ws.send(json.dumps({"type": "pong"}))

        except Exception as e:
            log.warning("Disconnected (%s). Reconnecting in %.0fs ...", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 30)


def main():
    parser = argparse.ArgumentParser(description="AW Remote Agent — Linux client")
    parser.add_argument("--id", required=True, help="Profile UUID from agents-platform")
    parser.add_argument("--url", default="ws://localhost:10005",
                        help="WebSocket base URL of agents-platform (default: ws://localhost:10005)")
    args = parser.parse_args()

    log.info("AW Remote Agent starting. Profile: %s → %s", args.id, args.url)
    asyncio.run(connect_and_serve(args.id, args.url))


if __name__ == "__main__":
    main()
