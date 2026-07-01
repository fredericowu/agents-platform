#!/usr/bin/env python3
"""AgentFS — FUSE driver that mounts a remote client via aw-remote-agent.

Usage:
    python3 agentfs.py <mountpoint> [--client <id>] [--api http://127.0.0.1:8010] [--root C:\\]
    python3 agentfs.py /opt/agentic-workspace/.tmp/windows-c --client 2f33bc76-...
"""
from __future__ import annotations

import argparse
import base64
import errno
import logging
import os
import stat
import sys
import threading
import time

import requests
from fuse import FUSE, FuseOSError, Operations

log = logging.getLogger("agentfs")

_SESSION = requests.Session()
_SESSION.headers["Content-Type"] = "application/json"


def _fs(api: str, client: str, op: str, path: str, **kwargs):
    """Call the aw-remote-agent fs API. Raises FuseOSError on any error."""
    try:
        resp = _SESSION.post(
            f"{api}/api/clients/{client}/fs",
            json={"op": op, "path": path, **kwargs},
            timeout=30,
        )
    except requests.ConnectionError:
        raise FuseOSError(errno.ENETDOWN)
    except requests.Timeout:
        raise FuseOSError(errno.ETIMEDOUT)

    if resp.status_code == 404:
        # "Client not connected" — agent is offline
        raise FuseOSError(errno.ENETDOWN)
    if resp.status_code == 408:
        raise FuseOSError(errno.ETIMEDOUT)
    if not resp.ok:
        raise FuseOSError(errno.EIO)

    data = resp.json()
    if data.get("error"):
        err = data["error"].lower()
        if "access" in err or "denied" in err or "unauthorized" in err:
            raise FuseOSError(errno.EACCES)
        if "not exist" in err or "cannot find" in err or "no such" in err:
            raise FuseOSError(errno.ENOENT)
        raise FuseOSError(errno.EIO)
    return data


class ConnectivityMonitor(threading.Thread):
    """Daemon thread — polls agent connectivity and logs online/offline transitions."""

    def __init__(self, api: str, client: str, interval: int = 10):
        super().__init__(daemon=True, name="connectivity-monitor")
        self._api = api
        self._client = client
        self._interval = interval
        self._online: bool | None = None  # None = not yet known

    def _check(self) -> bool:
        try:
            resp = _SESSION.get(
                f"{self._api}/api/remote-agents/{self._client}",
                timeout=5,
            )
            if resp.ok:
                return bool(resp.json().get("connected", False))
        except Exception:
            pass
        return False

    def run(self):
        while True:
            online = self._check()
            if online != self._online:
                if self._online is not None:
                    if online:
                        log.info("✓ agent %s ONLINE — filesystem ready", self._client)
                    else:
                        log.warning("✗ agent %s OFFLINE — ops will return errors until it reconnects", self._client)
                else:
                    state = "online" if online else "offline"
                    log.info("agent %s initial state: %s", self._client, state)
                self._online = online
            time.sleep(self._interval)


class AgentFS(Operations):
    def __init__(self, api: str, client: str, remote_root: str):
        self._api = api
        self._client = client
        # Ensure root always ends with backslash (C:\ not C:)
        self._root = remote_root if remote_root.endswith("\\") else remote_root + "\\"
        self._open_fds: dict[int, str] = {}
        self._next_fd = 100

    # ── helpers ──────────────────────────────────────────────

    def _win(self, fuse_path: str) -> str:
        """Convert FUSE path to Windows path on the remote machine."""
        rel = fuse_path.strip("/").replace("/", "\\")
        return self._root + rel if rel else self._root

    def _op(self, op: str, fuse_path: str, **kw):
        return _fs(self._api, self._client, op, self._win(fuse_path), **kw)

    def _entry_to_attr(self, e: dict) -> dict:
        if e.get("is_dir"):
            mode = stat.S_IFDIR | 0o755
        else:
            mode = stat.S_IFREG | 0o644
        mtime = float(e.get("mtime", time.time()))
        sz = e.get("size", 0)
        return {
            "st_mode":    mode,
            "st_nlink":   2 if e.get("is_dir") else 1,
            "st_size":    sz,
            "st_atime":   mtime,
            "st_mtime":   mtime,
            "st_ctime":   mtime,
            "st_uid":     os.getuid(),
            "st_gid":     os.getgid(),
            "st_blocks":  (sz + 511) // 512,
            "st_blksize": 4096,
        }

    # ── FUSE operations ──────────────────────────────────────

    def getattr(self, path, fh=None):
        r = self._op("stat", path)
        return self._entry_to_attr(r["stat"])

    def readdir(self, path, fh):
        yield "."
        yield ".."
        try:
            r = self._op("readdir", path)
            for e in (r.get("entries") or []):
                yield e["name"]
        except FuseOSError:
            pass  # yield nothing extra — dir appears empty when offline

    def open(self, path, flags):
        fd = self._next_fd
        self._next_fd += 1
        self._open_fds[fd] = path
        return fd

    def create(self, path, mode, fi=None):
        self._op("write", path, data="", offset=0)
        fd = self._next_fd
        self._next_fd += 1
        self._open_fds[fd] = path
        return fd

    def release(self, path, fh):
        self._open_fds.pop(fh, None)
        return 0

    def read(self, path, size, offset, fh):
        r = self._op("read", path, offset=offset, size=size)
        return base64.b64decode(r.get("data") or "")

    def write(self, path, data, offset, fh):
        self._op("write", path,
                 data=base64.b64encode(data).decode(),
                 offset=offset)
        return len(data)

    def truncate(self, path, length, fh=None):
        self._op("truncate", path, offset=length)

    def mkdir(self, path, mode):
        self._op("mkdir", path)

    def unlink(self, path):
        self._op("unlink", path)

    def rmdir(self, path):
        self._op("unlink", path)

    def rename(self, old, new, flags=0):
        _fs(self._api, self._client, "rename", self._win(old), dest=self._win(new))

    # Windows has no Unix permissions/ownership — silently ignore
    def chmod(self, path, mode):    return 0
    def chown(self, path, u, g):    return 0
    def utimens(self, path, t=None): return 0


def _is_alive(mountpoint: str) -> bool:
    """Return True if the FUSE mount is responsive."""
    try:
        os.listdir(mountpoint)
        return True
    except OSError:
        return False


def main():
    parser = argparse.ArgumentParser(description="AgentFS FUSE driver")
    parser.add_argument("mountpoint", nargs="?",
                        default="/opt/agentic-workspace/.tmp/windows-c")
    parser.add_argument("--client",     default="aw-windows-1")
    parser.add_argument("--api",        default="http://127.0.0.1:10005")
    parser.add_argument("--root",       default="C:\\")
    parser.add_argument("--debug",      action="store_true")
    parser.add_argument("--foreground", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [agentfs] %(levelname)s %(message)s",
    )

    mountpoint = os.path.abspath(args.mountpoint)
    try:
        os.makedirs(mountpoint, exist_ok=True)
    except OSError:
        pass  # Mountpoint dir exists (possibly as stale FUSE) — kernel will mount over it

    log.info("Mounting %s:%s -> %s", args.client, args.root, mountpoint)
    log.info("API: %s", args.api)

    fs = AgentFS(api=args.api, client=args.client, remote_root=args.root)

    # Start internal connectivity monitor — logs online/offline transitions
    monitor = ConnectivityMonitor(api=args.api, client=args.client)
    monitor.start()

    FUSE(
        fs,
        mountpoint,
        nothreads=False,
        foreground=args.foreground,
        allow_other=True,
        nonempty=True,
        auto_unmount=True,  # kernel cleans up mount point when this process dies
    )


if __name__ == "__main__":
    main()
