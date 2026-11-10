"""Worker process entry point for bounded metadata scans."""
from __future__ import annotations

import importlib
import json
import os
import pickle
import select
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

from .contracts import Binding
from .inventory import scan
from .scope import Scope


def run_scan(connection, home: str, profile_id: str, cancel_event, binding: dict[str, Any] | None = None) -> None:
    """Run with explicit nonsecret scope data and echo the parent's binding.

    A spawned interpreter has its own process-local epoch cache.  Passing the
    admitted binding (rather than rebuilding it in the child) prevents entries
    and results from being silently attributed to a new execution epoch.
    """
    try:
        requested = Binding.from_value(binding) if binding is not None else None
        scope = Scope(Path(home), profile_id, epoch=requested.epoch if requested else None)
        if requested is not None and scope.binding != requested:
            raise ValueError("worker scope binding mismatch")
        result = scan(scope, cancel_event=cancel_event)
        result["binding"] = scope.binding.as_dict()
        connection.send({"ok": True, "result": result})
    except Exception as exc:
        try:
            connection.send({"ok": False, "error": type(exc).__name__})
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


class _ChildConnection:
    def __init__(self, fd: int):
        self._fd = fd

    def fileno(self) -> int:
        return self._fd

    def send(self, message: dict[str, Any]) -> None:
        payload = pickle.dumps(message, protocol=4)
        header = struct.pack("!i", len(payload))
        data = header + payload
        view = memoryview(data)
        while view:
            written = os.write(self._fd, view)
            view = view[written:]

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass


class _ChildCancelEvent:
    def __init__(self, fd: int):
        self._fd = fd
        self._set = False

    def is_set(self) -> bool:
        if self._set:
            return True
        try:
            readable, _, _ = select.select([self._fd], [], [], 0)
        except (OSError, ValueError):
            self._set = True
            return True
        if readable:
            try:
                os.read(self._fd, 1)
            except OSError:
                pass
            self._set = True
        return self._set


def _resolve_target(module_name: str, qualname: str):
    target: Any = importlib.import_module(module_name)
    for component in qualname.split("."):
        if component == "<locals>":
            raise ValueError("worker target must be top-level")
        target = getattr(target, component)
    return target


def _minimal_environment() -> dict[str, str]:
    paths: list[str] = []
    for entry in sys.path:
        if entry and entry not in paths:
            paths.append(entry)
    environment = {"PYTHONNOUSERSITE": "1"}
    if paths:
        environment["PYTHONPATH"] = os.pathsep.join(paths)
    return environment


def supervise_spawned(module_name: str, qualname: str, home: str, profile_id: str, binding: dict[str, Any], data_path: str, cancel_path: str, executable: str | None = None) -> int:
    """Own scan launch from the explicitly exec'd clean interpreter."""
    environment = _minimal_environment()
    os.environ.clear()
    os.environ.update(environment)
    try:
        os.setpgid(0, 0)
    except OSError:
        pass
    command = [
        executable or sys.executable,
        "-m",
        "storage_utility.scan_worker",
        module_name,
        qualname,
        home,
        profile_id,
        json.dumps(binding, sort_keys=True, separators=(",", ":")),
        data_path,
        cancel_path,
    ]
    worker = None
    worker = subprocess.Popen(command, close_fds=True, env=environment)
    return worker.wait()


def launch_scan_worker(module_name: str, qualname: str, home: str, profile_id: str, binding: dict[str, Any], data_path: str, cancel_path: str, ready_path: str) -> int:
    """Launch the ordinary scan from a manager-owned clean child.

    This entry point is intentionally top-level and spawn-compatible.  It
    scrubs inherited host environment before starting the supervisor and makes
    the launcher its own process group so the parent can terminate both the
    launcher and any late-created scan child with one bounded escalation.
    """
    environment = _minimal_environment()
    os.environ.clear()
    os.environ.update(environment)
    try:
        os.setsid()
    except OSError:
        try:
            os.setpgid(0, 0)
        except OSError:
            pass
    command = [
        sys.executable,
        "-m",
        "storage_utility.scan_worker",
        "--supervise",
        module_name,
        qualname,
        home,
        profile_id,
        json.dumps(binding, sort_keys=True, separators=(",", ":")),
        data_path,
        cancel_path,
        ready_path,
    ]
    os.execve(sys.executable, command, environment)
    raise RuntimeError("clean scan supervisor exec returned")


def _supervise(argv: list[str]) -> int:
    """Own the ordinary worker launch in a disposable clean interpreter.

    The parent manager owns this process group.  If ``Popen`` stalls before it
    returns, the manager can kill this supervisor group without leaving the
    host request thread or watchdog waiting on an unowned launch call.
    """
    if len(argv) != 10:
        return 2
    _, module_name, qualname, home, profile_id, binding_json, data_fd_text, cancel_fd_text, ready_path = argv[1:]
    try:
        os.setsid()
    except OSError:
        try:
            os.setpgid(0, 0)
        except OSError:
            pass
    try:
        fd = os.open(ready_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
    except OSError:
        return 2
    return supervise_spawned(module_name, qualname, home, profile_id, json.loads(binding_json), data_fd_text, cancel_fd_text, sys.executable)


def _main(argv: list[str]) -> int:
    if len(argv) != 8:
        return 2
    module_name, qualname, home, profile_id, binding_json, data_fd_text, cancel_fd_text = argv[1:]
    data_fd = os.open(data_fd_text, os.O_WRONLY)
    cancel_fd = os.open(cancel_fd_text, os.O_RDONLY | os.O_NONBLOCK)
    connection = _ChildConnection(data_fd)
    cancel_event = _ChildCancelEvent(cancel_fd)
    try:
        target = _resolve_target(module_name, qualname)
        binding = json.loads(binding_json)
        target(connection, home, profile_id, cancel_event, binding)
    except Exception as exc:
        try:
            connection.send({"ok": False, "error": type(exc).__name__})
        except (BrokenPipeError, OSError):
            pass
    finally:
        connection.close()
        try:
            os.close(cancel_event._fd)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--supervise":
        raise SystemExit(_supervise(sys.argv))
    raise SystemExit(_main(sys.argv))
