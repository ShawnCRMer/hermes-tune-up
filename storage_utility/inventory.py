"""Bounded, metadata-only aggregate inventory of one Hermes home.

The walk never reads file contents and never follows symlinks: every
directory is opened relative to its already-open parent with O_NOFOLLOW and
its inode is checked against the entry that was listed. Results are
aggregated per directory, so the output size is bounded no matter how many
files the home holds (a typical home has hundreds of thousands).

Hard links are counted once. Other profiles, other mounted volumes, symlinks
and special files are skipped and reported in coverage. Credential/secret-looking
files contribute to folder totals but their names are only recorded when the
user has explicitly turned off the privacy default.
"""
from __future__ import annotations

import heapq
import json
import os
import stat
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .knowledge import describe
from .scope import TOP_LEVEL_EXCLUDED, Scope, _open_directory_no_follow, is_sensitive

MAX_VISITED = 3_000_000
MAX_SECONDS = 110.0
MAX_DEPTH = 96
TREE_NODE_LIMIT = 4000
LARGEST_LIMIT = 400
MIN_NODE_BYTES = 256 * 1024

_ERROR_MESSAGES = {
    "inventory_unreadable": "some folders or files could not be read",
    "inventory_identity_changed": "a folder changed while it was being scanned",
    "inventory_unsupported": "required safe filesystem primitives are unavailable",
    "inventory_root_changed": "the Hermes home changed identity during the scan",
}

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _supported() -> bool:
    return all(hasattr(os, name) for name in ("open", "scandir", "O_DIRECTORY", "O_NOFOLLOW")) and os.scandir in os.supports_fd


def _show_sensitive_names(scope: Scope) -> bool:
    """Read the privacy setting written by the API (default: hide)."""
    try:
        path = scope.home / "storage-utility" / "settings.json"
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            return False
        value = json.loads(path.read_text(encoding="utf-8"))
        return bool(isinstance(value, dict) and value.get("showSensitiveNames") is True)
    except (OSError, ValueError):
        return False


def scan(scope: Scope, *, cancel_event=None, max_entries: int = MAX_VISITED, deadline_seconds: float = MAX_SECONDS) -> dict:
    started = time.monotonic()
    started_at = _now()
    max_entries = max(1, min(int(max_entries), MAX_VISITED))
    deadline_seconds = max(0.001, min(float(deadline_seconds), MAX_SECONDS))
    show_sensitive = _show_sensitive_names(scope)

    # rel -> [logical, allocated, files, newest_mtime]; rolled up at the end.
    dirs: dict[str, list[int]] = {"": [0, 0, 0, 0]}
    largest: list[tuple[int, str, int, int, bool]] = []  # min-heap of (allocated, rel, logical, mtime, sensitive)
    seen_links: set[tuple[int, int]] = set()
    excluded = Counter()
    errors: set[str] = set()
    hidden_files = 0
    hidden_bytes = 0
    unreadable = 0
    visited = 0
    hardlink_duplicates = 0
    complete = True
    budget_reason: str | None = None
    stack: list[list] = []

    def stop_reason() -> str | None:
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled"
        if visited >= max_entries:
            return "entry_budget"
        if time.monotonic() - started >= deadline_seconds:
            return "time_budget"
        return None

    try:
        if not _supported():
            complete = False
            errors.add("inventory_unsupported")
        else:
            try:
                root_fd = _open_directory_no_follow(scope.home)
            except (OSError, ValueError):
                root_fd = None
                complete = False
                errors.add("inventory_root_changed")
            if root_fd is not None:
                root_stat = os.fstat(root_fd)
                if (int(root_stat.st_dev), int(root_stat.st_ino)) != (scope.root_device, scope.root_inode):
                    os.close(root_fd)
                    complete = False
                    errors.add("inventory_root_changed")
                else:
                    stack.append([root_fd, "", None])
        check_every = 0
        while stack:
            check_every += 1
            if check_every >= 256:
                check_every = 0
                reason = stop_reason()
                if reason is not None:
                    complete = False
                    budget_reason = reason
                    break
            frame = stack[-1]
            if frame[2] is None:
                try:
                    with os.scandir(frame[0]) as iterator:
                        frame[2] = iter(list(iterator))
                except OSError:
                    unreadable += 1
                    complete = False
                    errors.add("inventory_unreadable")
                    os.close(frame[0])
                    stack.pop()
                    continue
            entry = next(frame[2], None)
            if entry is None:
                os.close(frame[0])
                stack.pop()
                continue
            visited += 1
            parent_rel = frame[1]
            rel = f"{parent_rel}/{entry.name}" if parent_rel else entry.name
            if not parent_rel and entry.name in TOP_LEVEL_EXCLUDED:
                excluded["other_profiles"] += 1
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                unreadable += 1
                complete = False
                errors.add("inventory_unreadable")
                continue
            mode = info.st_mode
            if stat.S_ISLNK(mode):
                excluded["symlink"] += 1
                continue
            if int(info.st_dev) != scope.root_device:
                excluded["other_volume"] += 1
                continue
            if stat.S_ISDIR(mode):
                if len(stack) >= MAX_DEPTH:
                    excluded["too_deep"] += 1
                    complete = False
                    continue
                try:
                    child_fd = os.open(entry.name, _DIR_FLAGS, dir_fd=frame[0])
                except OSError:
                    unreadable += 1
                    complete = False
                    errors.add("inventory_unreadable")
                    continue
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    os.close(child_fd)
                    complete = False
                    errors.add("inventory_identity_changed")
                    continue
                dirs[rel] = [0, 0, 0, int(info.st_mtime)]
                stack.append([child_fd, rel, None])
                continue
            if not stat.S_ISREG(mode):
                excluded["special"] += 1
                continue
            logical = int(info.st_size)
            allocated = int(getattr(info, "st_blocks", 0)) * 512
            if info.st_nlink > 1:
                key = (int(info.st_dev), int(info.st_ino))
                if key in seen_links:
                    hardlink_duplicates += 1
                    logical = allocated = 0
                else:
                    seen_links.add(key)
            mtime = int(info.st_mtime)
            bucket = dirs[parent_rel]
            bucket[0] += logical
            bucket[1] += allocated
            bucket[2] += 1
            if mtime > bucket[3]:
                bucket[3] = mtime
            sensitive = is_sensitive(rel)
            if sensitive:
                hidden_files += 1
                hidden_bytes += allocated
                if not show_sensitive:
                    continue
            if allocated <= 0:
                continue
            item = (allocated, rel, logical, mtime, sensitive)
            if len(largest) < LARGEST_LIMIT:
                heapq.heappush(largest, item)
            elif allocated > largest[0][0]:
                heapq.heapreplace(largest, item)
    finally:
        while stack:
            try:
                os.close(stack.pop()[0])
            except OSError:
                pass

    # Roll each directory's totals up into every ancestor.
    totals = {key: list(value) for key, value in dirs.items()}
    for rel in sorted(dirs, key=lambda item: item.count("/") + (1 if item else 0), reverse=True):
        if not rel:
            continue
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        child = totals[rel]
        target = totals.setdefault(parent, [0, 0, 0, 0])
        target[0] += child[0]
        target[1] += child[1]
        target[2] += child[2]
        if child[3] > target[3]:
            target[3] = child[3]

    root = totals[""]
    threshold = max(MIN_NODE_BYTES, int(root[1] * 0.0002))
    chosen = [rel for rel, value in totals.items() if rel and (rel.count("/") == 0 or value[1] >= threshold)]
    chosen.sort(key=lambda rel: totals[rel][1], reverse=True)
    chosen = chosen[:TREE_NODE_LIMIT]
    included = set(chosen)
    for rel in list(chosen):  # ancestors are always at least as large, but be explicit
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        while parent and parent not in included:
            included.add(parent)
            parent = parent.rsplit("/", 1)[0] if "/" in parent else ""
    nodes = [_node("", root)] + [_node(rel, totals[rel]) for rel in sorted(included, key=lambda r: totals[r][1], reverse=True)]

    largest_files = [
        {
            "path": rel, "name": rel.rsplit("/", 1)[-1], "parent": rel.rsplit("/", 1)[0] if "/" in rel else "",
            "allocatedBytes": allocated, "logicalBytes": logical, "modifiedAt": mtime,
            "sensitive": sensitive, "description": describe(rel),
        }
        for allocated, rel, logical, mtime, sensitive in sorted(largest, reverse=True)
    ]
    top_level = {node["path"]: node["allocatedBytes"] for node in nodes if node["path"] and "/" not in node["path"]}
    for item in largest_files:
        if not item["parent"]:
            top_level[item["path"]] = item["allocatedBytes"]
    return {
        "tree": nodes,
        "largest": largest_files,
        "topLevel": top_level,
        "coverage": {
            "visited": visited,
            "files": root[2],
            "excluded": sum(excluded.values()),
            "excludedReasons": dict(sorted(excluded.items())),
            "unreadable": unreadable,
            "hardlinkDuplicates": hardlink_duplicates,
            "complete": bool(complete),
            "budgetReason": budget_reason,
        },
        "privacy": {"sensitiveFiles": hidden_files, "sensitiveBytes": hidden_bytes, "namesShown": show_sensitive},
        "errors": [{"code": code, "message": _ERROR_MESSAGES[code], "retryable": True} for code in sorted(errors)],
        "totals": {"logicalBytes": root[0], "allocatedBytes": root[1], "files": root[2]},
        "startedAt": started_at,
        "finishedAt": _now(),
        "elapsedMs": int((time.monotonic() - started) * 1000),
    }


def _node(rel: str, value: list[int]) -> dict:
    return {
        "path": rel,
        "name": rel.rsplit("/", 1)[-1] if rel else "Hermes home",
        "parent": None if not rel else (rel.rsplit("/", 1)[0] if "/" in rel else ""),
        "logicalBytes": value[0],
        "allocatedBytes": value[1],
        "files": value[2],
        "modifiedAt": value[3],
        "sensitive": bool(rel) and is_sensitive(rel + "/x"),
        "description": describe(rel),
    }
