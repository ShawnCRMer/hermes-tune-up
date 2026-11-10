"""Anchored, no-follow filesystem primitives used by every destructive action.

All paths are resolved one component at a time from the scoped Hermes home
using O_NOFOLLOW directory descriptors, so a symlink or a swapped-in folder
anywhere along the path is refused rather than followed. Renames use
RENAME_NOREPLACE where the kernel supports it, so an action can never
overwrite an object that appeared after review.
"""
from __future__ import annotations

import ctypes
import errno
import os
import stat
import time
from pathlib import Path, PurePosixPath
from typing import Any

from .scope import Scope, _open_directory_no_follow

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

try:
    _LIBC = ctypes.CDLL(None, use_errno=True)
    _RENAMEAT2 = getattr(_LIBC, "renameat2", None)
except OSError:  # pragma: no cover - non-glibc platforms
    _RENAMEAT2 = None
_RENAME_NOREPLACE = 1


def split_relative(relative: str) -> tuple[str, ...]:
    if not isinstance(relative, str) or not relative or relative.startswith("/") or "\\" in relative or "\x00" in relative:
        raise OSError("invalid relative path")
    parts = PurePosixPath(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise OSError("invalid relative path")
    return parts


def open_root(scope: Scope) -> int:
    fd = _open_directory_no_follow(scope.home)
    info = os.fstat(fd)
    if (info.st_dev, info.st_ino) != (scope.root_device, scope.root_inode):
        os.close(fd)
        raise OSError("Hermes home identity changed")
    return fd


def open_dir_at(base_fd: int, parts: tuple[str, ...], device: int) -> int:
    """Open nested directories below base_fd without following links or crossing volumes."""
    fd = os.dup(base_fd)
    try:
        for part in parts:
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
            if os.fstat(fd).st_dev != device:
                raise OSError("path crosses onto another volume")
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_parent(scope: Scope, relative: str) -> tuple[int, str]:
    parts = split_relative(relative)
    root = open_root(scope)
    try:
        return open_dir_at(root, parts[:-1], scope.root_device), parts[-1]
    finally:
        os.close(root)


def identity(info: os.stat_result) -> dict[str, Any]:
    kind = "dir" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "other"
    value: dict[str, Any] = {"device": int(info.st_dev), "inode": int(info.st_ino), "kind": kind}
    if kind == "file":
        value.update(size=int(info.st_size), mtimeNs=int(info.st_mtime_ns))
    return value


def stat_at(parent_fd: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)


def rename_noreplace(src_dir: int, src: str, dst_dir: int, dst: str) -> None:
    if _RENAMEAT2 is not None:
        result = _RENAMEAT2(src_dir, os.fsencode(src), dst_dir, os.fsencode(dst), _RENAME_NOREPLACE)
        if result == 0:
            return
        error = ctypes.get_errno()
        if error not in (errno.ENOSYS, errno.EINVAL):
            raise OSError(error, os.strerror(error))
    try:
        os.stat(dst, dir_fd=dst_dir, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(errno.EEXIST, "destination exists")
    os.rename(src, dst, src_dir_fd=src_dir, dst_dir_fd=dst_dir)


def measure_fd(dir_fd: int, device: int, *, deadline: float, show_newest: bool = True) -> dict[str, Any]:
    """Walk an already-open directory: allocated/logical bytes, files, newest mtime."""
    allocated = logical = files = 0
    newest = int(os.fstat(dir_fd).st_mtime)
    seen: set[tuple[int, int]] = set()
    complete = True
    stack = [[os.dup(dir_fd), None]]
    try:
        while stack:
            if time.monotonic() > deadline:
                complete = False
                break
            frame = stack[-1]
            if frame[1] is None:
                try:
                    with os.scandir(frame[0]) as entries:
                        frame[1] = iter(list(entries))
                except OSError:
                    complete = False
                    os.close(frame[0])
                    stack.pop()
                    continue
            entry = next(frame[1], None)
            if entry is None:
                os.close(frame[0])
                stack.pop()
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                complete = False
                continue
            if info.st_dev != device or stat.S_ISLNK(info.st_mode):
                continue
            if int(info.st_mtime) > newest:
                newest = int(info.st_mtime)
            if stat.S_ISDIR(info.st_mode):
                if len(stack) > 96:
                    complete = False
                    continue
                try:
                    child = os.open(entry.name, _DIR_FLAGS, dir_fd=frame[0])
                except OSError:
                    complete = False
                    continue
                stack.append([child, None])
            elif stat.S_ISREG(info.st_mode):
                files += 1
                if info.st_nlink > 1:
                    key = (info.st_dev, info.st_ino)
                    if key in seen:
                        continue
                    seen.add(key)
                allocated += int(getattr(info, "st_blocks", 0)) * 512
                logical += int(info.st_size)
    finally:
        for frame in stack:
            try:
                os.close(frame[0])
            except OSError:
                pass
    return {"allocatedBytes": allocated, "logicalBytes": logical, "files": files, "modifiedAt": newest, "complete": complete}


def measure(scope: Scope, relative: str, *, budget_seconds: float = 10.0) -> dict[str, Any] | None:
    """Measure a file or folder below the home; None when missing or unsafe."""
    try:
        parent, name = open_parent(scope, relative)
    except OSError:
        return None
    try:
        try:
            info = stat_at(parent, name)
        except OSError:
            return None
        if info.st_dev != scope.root_device or stat.S_ISLNK(info.st_mode):
            return None
        if stat.S_ISREG(info.st_mode):
            return {"allocatedBytes": int(getattr(info, "st_blocks", 0)) * 512, "logicalBytes": int(info.st_size),
                    "files": 1, "modifiedAt": int(info.st_mtime), "complete": True, "identity": identity(info)}
        if not stat.S_ISDIR(info.st_mode):
            return None
        try:
            child = os.open(name, _DIR_FLAGS, dir_fd=parent)
        except OSError:
            return None
        try:
            opened = os.fstat(child)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                return None
            result = measure_fd(child, scope.root_device, deadline=time.monotonic() + budget_seconds)
            result["identity"] = identity(info)
            return result
        finally:
            os.close(child)
    finally:
        os.close(parent)


def list_children(scope: Scope, relative: str) -> list[tuple[str, os.stat_result]]:
    """Direct children (name, lstat) of a home-relative folder; [] when missing/unsafe."""
    try:
        root = open_root(scope)
    except OSError:
        return []
    try:
        try:
            fd = open_dir_at(root, split_relative(relative), scope.root_device)
        except OSError:
            return []
        try:
            result = []
            with os.scandir(fd) as entries:
                for entry in entries:
                    try:
                        result.append((entry.name, entry.stat(follow_symlinks=False)))
                    except OSError:
                        continue
            return result
        finally:
            os.close(fd)
    finally:
        os.close(root)


def volume_free(scope: Scope) -> int | None:
    try:
        stats = os.statvfs(scope.home)
        return int(stats.f_bavail) * int(stats.f_frsize)
    except OSError:
        return None


class Anchor:
    """A folder outside the Hermes home that destructive operations are anchored to.

    Quacks like Scope for the fsutil primitives (home, root_device, root_inode):
    every path is resolved one no-follow component at a time below it, and the
    folder's own identity must match what was reviewed.
    """

    def __init__(self, root: str, expected: dict[str, Any] | None = None):
        path = Path(root)
        if not path.is_absolute() or ".." in path.parts:
            raise OSError("project folder must be an absolute path")
        fd = _open_directory_no_follow(path)
        try:
            info = os.fstat(fd)
        finally:
            os.close(fd)
        if expected is not None and (info.st_dev, info.st_ino) != (expected.get("device"), expected.get("inode")):
            raise OSError("project folder was replaced since review")
        self.home = path
        self.root_device = int(info.st_dev)
        self.root_inode = int(info.st_ino)

    def identity(self) -> dict[str, int]:
        return {"device": self.root_device, "inode": self.root_inode}
