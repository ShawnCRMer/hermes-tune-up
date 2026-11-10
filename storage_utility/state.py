"""Private plugin state: no-follow JSON files under <home>/storage-utility, one lock."""
from __future__ import annotations

import fcntl
import functools
import json
import os
import secrets
import stat
import threading
from pathlib import Path
from typing import Any

from .scope import Scope, _open_directory_no_follow

STATE_LOCK = threading.RLock()
_MAX_STATE_BYTES = 8 * 1024 * 1024


def _state_dir_fd(scope: Scope) -> int:
    root = _open_directory_no_follow(scope.home)
    try:
        info = os.fstat(root)
        if (info.st_dev, info.st_ino) != (scope.root_device, scope.root_inode):
            raise OSError("Hermes home identity changed")
        try:
            os.mkdir("storage-utility", 0o700, dir_fd=root)
        except FileExistsError:
            pass
        return os.open("storage-utility", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root)
    finally:
        os.close(root)


def _valid_name(name: str) -> None:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise OSError("invalid state name")


def read_json(scope: Scope, name: str, default: Any) -> Any:
    _valid_name(name)
    directory = _state_dir_fd(scope)
    fd = None
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        except FileNotFoundError:
            return default
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_STATE_BYTES:
            raise OSError("state file is unsafe or oversized")
        chunks, remaining = [], info.st_size
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        try:
            return json.loads(b"".join(chunks) or b"null") if chunks else default
        except ValueError:
            return default
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


def write_json(scope: Scope, name: str, value: Any) -> None:
    _valid_name(name)
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > _MAX_STATE_BYTES:
        raise OSError("state value is too large")
    directory = _state_dir_fd(scope)
    temporary = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short state write")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def remove(scope: Scope, name: str) -> None:
    _valid_name(name)
    directory = _state_dir_fd(scope)
    try:
        os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        pass
    finally:
        os.close(directory)


def list_names(scope: Scope, prefix: str) -> list[str]:
    directory = _state_dir_fd(scope)
    try:
        with os.scandir(directory) as entries:
            return sorted(entry.name for entry in entries if entry.name.startswith(prefix) and entry.is_file(follow_symlinks=False))
    finally:
        os.close(directory)


def serialized(operation):
    """Serialize state mutations across threads and processes sharing a home."""
    @functools.wraps(operation)
    def run(scope: Scope, *args, **kwargs):
        with STATE_LOCK:
            directory = _state_dir_fd(scope)
            lock_fd = None
            try:
                lock_fd = os.open("state.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
                if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                    raise OSError("unsafe state lock")
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                return operation(scope, *args, **kwargs)
            finally:
                if lock_fd is not None:
                    os.close(lock_fd)
                os.close(directory)
    return run


DEFAULT_SETTINGS = {"showSensitiveNames": False}


def settings(scope: Scope) -> dict[str, Any]:
    value = read_json(scope, "settings.json", {})
    result = dict(DEFAULT_SETTINGS)
    if isinstance(value, dict):
        result["showSensitiveNames"] = value.get("showSensitiveNames") is True
    return result
