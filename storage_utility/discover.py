"""Discover Hermes-owned things that live outside the Hermes home, read-only.

* Git repositories where Hermes created ``.worktrees/`` (Hermes's worktree
  registry plus Kanban task workspace paths).
* Processes and systemd units holding the session database, so compaction
  guidance can list exactly what must be stopped (including path watchers
  that would otherwise respawn a holder).

SQLite files are only ever read in a short-lived subprocess: closing any
descriptor to a database inside the serving Hermes process would release that
process's own POSIX locks on it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .scope import Scope
from .childenv import minimal_env

_CACHE: dict[str, tuple[float, Any]] = {}
_LOCK = threading.Lock()
_TTL = 300.0

_DISCOVER = r"""
import glob, json, os, sqlite3, sys
home = sys.argv[1]
repos = set()
try:
    with open(os.path.join(home, 'worktree_repos.json')) as handle:
        repos.update(r for r in json.load(handle).get('repos', []) if isinstance(r, str))
except Exception:
    pass
dbs = glob.glob(os.path.join(home, 'kanban.db')) + glob.glob(os.path.join(home, 'kanban', 'boards', '*', 'kanban.db'))
for db in dbs:
    try:
        conn = sqlite3.connect('file:' + db + '?mode=ro', uri=True, timeout=2)
        cols = [row[1] for row in conn.execute('pragma table_info(tasks)')]
        if 'workspace_path' in cols:
            for (path,) in conn.execute("select workspace_path from tasks where workspace_path like '%/.worktrees/%'"):
                repos.add(path.split('/.worktrees/')[0])
        conn.close()
    except Exception:
        pass
out = []
for repo in sorted(repos):
    tree = os.path.join(repo, '.worktrees')
    if not os.path.isdir(os.path.join(repo, '.git')) and not os.path.isfile(os.path.join(repo, '.git')):
        continue
    total, trees, complete = 0, 0, True
    if os.path.isdir(tree) and not os.path.islink(tree):
        trees = sum(1 for entry in os.scandir(tree) if entry.is_dir(follow_symlinks=False))
        dev = os.lstat(tree).st_dev
        seen = set()
        count = 0
        for current, dirs, files in os.walk(tree):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(current, d))]
            for name in files:
                count += 1
                if count > 400000:
                    complete = False
                    break
                try:
                    info = os.lstat(os.path.join(current, name))
                except OSError:
                    continue
                if info.st_dev != dev or (info.st_dev, info.st_ino) in seen:
                    continue
                seen.add((info.st_dev, info.st_ino))
                total += info.st_blocks * 512
            if not complete:
                break
    out.append({'repo': repo, 'worktrees': trees, 'bytes': total, 'complete': complete})
print(json.dumps(out))
"""


def _cached(key: str, compute):
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.monotonic() - hit[0] < _TTL:
            return hit[1]
    value = compute()
    with _LOCK:
        _CACHE[key] = (time.monotonic(), value)
    return value


def invalidate() -> None:
    with _LOCK:
        _CACHE.clear()


def worktree_repos(scope: Scope) -> list[dict[str, Any]]:
    def compute() -> list[dict[str, Any]]:
        try:
            done = subprocess.run([sys.executable, "-I", "-c", _DISCOVER, str(scope.home)], capture_output=True,
                                  text=True, timeout=60, stdin=subprocess.DEVNULL, env=minimal_env())
            rows = json.loads(done.stdout or "[]")
        except (OSError, subprocess.SubprocessError, ValueError):
            return []
        return [row for row in rows if isinstance(row, dict) and isinstance(row.get("repo"), str) and os.path.isabs(row["repo"])]
    return _cached(f"repos:{scope.home}", compute)


# ----- session database holders ------------------------------------------------------

_UNIT = re.compile(r"/([A-Za-z0-9@._-]+\.service)(?:/|$)")


def _short_command(pid: str) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return "unknown"
    parts = [part.decode("utf-8", "replace") for part in raw if part]
    if not parts:
        return "unknown"
    # Inline Python programs are long; show the interpreter and the meaningful trailing args.
    if "-c" in parts:
        index = parts.index("-c")
        parts = parts[:index] + ["-c …"] + [part for part in parts[index + 2:] if len(part) < 60]
    text = " ".join(Path(parts[0]).name if i == 0 else part for i, part in enumerate(parts))
    return text[:120]


def db_holders(scope: Scope) -> list[dict[str, Any]]:
    """Processes with state.db (or its WAL/SHM) open. Reads /proc only."""
    targets = {str(scope.home / name) for name in ("state.db", "state.db-wal", "state.db-shm")}
    found: dict[str, dict[str, Any]] = {}
    try:
        pids = [entry for entry in os.listdir("/proc") if entry.isdigit()]
    except OSError:
        return []
    for pid in pids:
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if link in targets:
                unit = None
                try:
                    match = _UNIT.search(Path(f"/proc/{pid}/cgroup").read_text())
                    unit = match.group(1) if match else None
                except OSError:
                    pass
                found[pid] = {"pid": int(pid), "command": _short_command(pid), "unit": unit, "self": int(pid) == os.getpid()}
                break
    return sorted(found.values(), key=lambda row: row["pid"])


def hermes_units() -> list[dict[str, str]]:
    """Active Hermes systemd units (services, path watchers, timers)."""
    if shutil.which("systemctl") is None:
        return []
    try:
        done = subprocess.run(["systemctl", "list-units", "--all", "--plain", "--no-legend", "--no-pager", "hermes*"],
                              capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL, env=minimal_env())
    except (OSError, subprocess.SubprocessError):
        return []
    units = []
    for line in done.stdout.splitlines():
        fields = line.split(None, 4)
        if len(fields) >= 4 and re.fullmatch(r"[A-Za-z0-9@._-]+\.(service|path|timer|socket)", fields[0]):
            units.append({"unit": fields[0], "load": fields[1], "active": fields[2], "sub": fields[3],
                          "description": fields[4] if len(fields) > 4 else ""})
    return units


def stop_plan(scope: Scope) -> dict[str, Any]:
    """Exactly what must stop for exclusive access to state.db, and how to bring it back."""
    holders = db_holders(scope)
    units = hermes_units()
    running = [unit for unit in units if unit["active"] in {"active", "activating", "reloading"}]
    # Path/timer watchers first, so nothing respawns a holder while services stop.
    watchers = [unit["unit"] for unit in running if unit["unit"].endswith((".path", ".timer"))]
    holder_units = {row["unit"] for row in holders if row["unit"]}
    services = [unit["unit"] for unit in running if unit["unit"].endswith(".service") and
                (unit["unit"] in holder_units or unit["active"] in {"active", "activating"})]
    for unit in holder_units:
        if unit not in services:
            services.append(unit)
    loose = [row for row in holders if not row["unit"] or not row["unit"].startswith("hermes")]
    sudo = "" if hasattr(os, "geteuid") and os.geteuid() == 0 else "sudo "
    stop_units = watchers + services
    return {
        "holders": holders,
        "units": units,
        "stopUnits": stop_units,
        "stopCommand": f"{sudo}systemctl stop {' '.join(stop_units)}" if stop_units else None,
        "startCommand": f"{sudo}systemctl start {' '.join(list(reversed(services)) + watchers)}" if stop_units else None,
        "looseHolders": loose,
        "crashLooping": [unit["unit"] for unit in units if unit["sub"] == "auto-restart"],
    }
