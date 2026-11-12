"""Local diagnostics: duplicate large files, and the bounded AI-advisor payload."""
from __future__ import annotations

import hashlib
import os
import stat
import time
from typing import Any

from .fsutil import open_parent
from .knowledge import _EXACT
from .scope import Scope, is_sensitive

MAX_READ_BYTES = 2 * 1024 * 1024 * 1024
MAX_SECONDS = 60.0
MIN_BYTES = 1024 * 1024
_EDGE = 64 * 1024
# Closing any descriptor to a file drops this process's POSIX locks on it, so a
# database the serving Hermes process has open must never be opened here.
# Backup copies nobody has open (e.g. "before-save-*.sqlite") are safe to read.
_UNSAFE_SUFFIXES = ("-wal", "-shm", "-journal", ".lock")
_LIVE_NAMES = {"state.db", "kanban.db", "response_store.db"}


def _open_by_this_process() -> set[str]:
    from .hostos import own_open_paths
    return own_open_paths()


def _hashable(item: dict[str, Any], home: str = "", open_paths: frozenset | set = frozenset()) -> bool:
    path = item.get("path") or ""
    lower = path.lower()
    name = lower.rsplit("/", 1)[-1]
    return (not item.get("sensitive") and not is_sensitive(path) and int(item.get("logicalBytes") or 0) >= MIN_BYTES
            and not lower.endswith(_UNSAFE_SUFFIXES) and name not in _LIVE_NAMES
            and os.path.join(home, path) not in open_paths)


def _open(scope: Scope, relative: str, size: int) -> int | None:
    try:
        parent, name = open_parent(scope, relative)
    except OSError:
        return None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    except OSError:
        return None
    finally:
        os.close(parent)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size != size or info.st_dev != scope.root_device:
        os.close(fd)
        return None
    return fd


def find_duplicates(scope: Scope, snapshot: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    read = 0
    by_size: dict[int, list[dict[str, Any]]] = {}
    open_paths = _open_by_this_process()
    for item in snapshot.get("largest", []):
        if _hashable(item, str(scope.home), open_paths):
            by_size.setdefault(int(item["logicalBytes"]), []).append(item)
    groups: list[dict[str, Any]] = []
    checked = 0
    incomplete = False
    for size, items in sorted(by_size.items(), reverse=True):
        if len(items) < 2:
            continue
        # Stage 1: first/last 64 KiB. Stage 2: full content for survivors.
        edges: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            if time.monotonic() - started > MAX_SECONDS or read > MAX_READ_BYTES:
                incomplete = True
                break
            fd = _open(scope, item["path"], size)
            if fd is None:
                continue
            try:
                head = os.pread(fd, _EDGE, 0)
                tail = os.pread(fd, _EDGE, max(0, size - _EDGE))
                read += len(head) + len(tail)
            finally:
                os.close(fd)
            edges.setdefault(hashlib.sha256(head + tail).hexdigest(), []).append(item)
        for candidates in edges.values():
            if len(candidates) < 2:
                continue
            full: dict[str, list[str]] = {}
            for item in candidates:
                if time.monotonic() - started > MAX_SECONDS or read + size > MAX_READ_BYTES:
                    incomplete = True
                    break
                fd = _open(scope, item["path"], size)
                if fd is None:
                    continue
                digest = hashlib.sha256()
                try:
                    offset = 0
                    while offset < size:
                        chunk = os.pread(fd, min(1024 * 1024, size - offset), offset)
                        if not chunk:
                            break
                        digest.update(chunk)
                        offset += len(chunk)
                    read += offset
                    checked += 1
                    if offset == size:
                        full.setdefault(digest.hexdigest(), []).append(item["path"])
                finally:
                    os.close(fd)
            for paths in full.values():
                if len(paths) > 1:
                    groups.append({"sizeBytes": size, "paths": sorted(paths), "extraCopiesBytes": size * (len(paths) - 1)})
    groups.sort(key=lambda group: group["extraCopiesBytes"], reverse=True)
    return {"groups": groups, "filesHashed": checked, "bytesRead": read, "complete": not incomplete,
            "extraCopiesBytes": sum(group["extraCopiesBytes"] for group in groups),
            "scope": "the largest files in the latest scan (1 MB and up); live databases Hermes has open and secret-looking files are never opened"}


def advisor_payload(snapshot: dict[str, Any] | None, categories: list[dict[str, Any]], capacity: dict[str, Any] | None) -> dict[str, Any]:
    """Metadata only: Hermes's own top-level folder names, category totals, volume numbers."""
    top = {}
    other = 0
    for name, value in ((snapshot or {}).get("topLevel") or {}).items():
        if name in _EXACT and name != "":
            top[name] = value
        else:
            other += int(value or 0)
    if other:
        top["(other files)"] = other
    volume = ((capacity or {}).get("volumes") or [{}])[0]
    return {
        "volume": {"totalBytes": volume.get("totalBytes"), "freeBytes": volume.get("freeBytes")},
        "hermesHome": {"allocatedBytes": ((snapshot or {}).get("totals") or {}).get("allocatedBytes"),
                       "files": ((snapshot or {}).get("totals") or {}).get("files"),
                       "scanComplete": ((snapshot or {}).get("coverage") or {}).get("complete")},
        "topLevelFolders": dict(sorted(top.items(), key=lambda pair: -int(pair[1] or 0))),
        "cleanupCategories": [{
            "title": category["title"], "bytes": category.get("bytes"), "summary": category.get("summary"),
            "reclaimableBytes": category["reclaimable"].get("bytes"), "reclaimableCertainty": category["reclaimable"].get("certainty"),
            "itemCount": category.get("itemTotal"),
            "oldestItemDays": max((item.get("ageDays") or 0 for item in category.get("items") or []), default=None),
        } for category in categories],
    }
