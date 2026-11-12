"""The few operating-system facts that differ between the computers Tune-up serves.

Linux keeps its original /proc reads exactly. macOS uses the kernel's own answers
(fcntl F_GETPATH for "which folder is this descriptor", psutil/sysctl for processes).
Nothing here reads another process's environment: it can hold provider keys.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Iterable

IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = os.name == "nt"

_MAXPATHLEN = 1024  # macOS <sys/param.h>; F_GETPATH writes at most this many bytes


def kind() -> str:
    return "linux" if IS_LINUX else "macos" if IS_MACOS else "windows" if IS_WINDOWS else "other"


def fd_path(fd: int) -> str:
    """Absolute path of the directory or file an open descriptor names.

    Linux: the /proc/self/fd symlink (unchanged from earlier releases). macOS: F_GETPATH,
    which answers from the open vnode, so a later rename or symlink swap cannot change it.
    Raises OSError when the host has no such primitive.
    """
    if IS_MACOS:
        import fcntl
        if not hasattr(fcntl, "F_GETPATH"):
            raise OSError("F_GETPATH is unavailable")
        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(_MAXPATHLEN))
        path = os.fsdecode(raw.split(b"\0", 1)[0])
        if not path or not os.path.isabs(path):
            raise OSError("F_GETPATH returned no absolute path")
        return path
    if IS_LINUX:
        return os.path.realpath(f"/proc/self/fd/{fd}")
    raise OSError("this host cannot name an open descriptor")


def own_open_paths() -> set[str]:
    """Paths this process has open right now (never opened again here)."""
    paths: set[str] = set()
    directory = "/dev/fd" if IS_MACOS else "/proc/self/fd"
    try:
        entries = os.listdir(directory)
    except OSError:
        return paths
    for entry in entries:
        if IS_MACOS:
            try:
                paths.add(fd_path(int(entry)))
            except (OSError, ValueError):
                pass
            continue
        try:
            paths.add(os.readlink(f"/proc/self/fd/{entry}"))
        except OSError:
            pass
    return paths


def psutil_module():
    """psutil when installed (a Hermes core dependency), else None. Callers fail closed."""
    try:
        import psutil  # type: ignore
    except Exception:
        return None
    return psutil


def process_start(pid: int) -> int | None:
    """Start time of a live process in the same units Hermes records in processes.json
    (``host_start_time``): Hermes's own helper first, then psutil (centiseconds since the epoch,
    which is what Hermes's helper returns on macOS). None when the process is gone."""
    if not pid or pid <= 0:
        return None
    try:
        from gateway.status import get_process_start_time  # Hermes's own PID-reuse guard
        value = get_process_start_time(pid)
        if value is not None:
            return int(value)
    except Exception:
        pass
    psutil = psutil_module()
    if psutil is None:
        return None
    try:
        return round(psutil.Process(pid).create_time() * 100)  # Hermes rounds the same way
    except Exception:
        return None


def ancestry(pid: int, limit: int = 64) -> list[dict[str, Any]]:
    """[{pid, command}] from the parent of ``pid`` upward, stopping before pid 1 (launchd/init).
    Commands come from the process table only (no environment)."""
    psutil = psutil_module()
    chain: list[dict[str, Any]] = []
    if psutil is None:
        return chain
    try:
        current = psutil.Process(pid).ppid()
    except Exception:
        return chain
    while current and current > 1 and len(chain) < limit:
        try:
            process = psutil.Process(current)
            try:
                command = " ".join(process.cmdline())
            except Exception:
                command = ""
            chain.append({"pid": current, "command": command})
            current = process.ppid()
        except Exception:
            break
    return chain


def listening_ports(pids: Iterable[int]) -> dict[int, list[int]]:
    """pid -> TCP ports it listens on, for processes this user may inspect."""
    psutil = psutil_module()
    owners: dict[int, list[int]] = {}
    if psutil is None:
        return owners
    for pid in pids:
        try:
            connections = psutil.Process(pid).net_connections(kind="inet")
        except Exception:
            continue
        ports = sorted({conn.laddr.port for conn in connections
                        if conn.status == psutil.CONN_LISTEN and getattr(conn, "laddr", None)})
        if ports:
            owners[pid] = ports
    return owners
