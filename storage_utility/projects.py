"""Find folders Hermes created outside its home, and the reclaimable junk inside them.

Runs in a separate low-priority process (``python -m storage_utility.projects``)
so database reads and heavy hashing never happen inside the serving Hermes
process. Output is JSON on stdout.

Provenance is evidence, never a guess: a folder is a "Hermes project" only if
Hermes's own session history shows its tools writing there (write_file/patch
targets, or terminal commands that create files run in or naming it), or a
Kanban task / worktree registry points at it. Credential and config folders
are excluded outright.

Every finding carries dependency evidence and is only offered for selection
when it is: untracked by git, not in use by a running process, not referenced
by a systemd unit or crontab, not referenced by the project's own text files,
and not changed in the last 7 days. Dependency folders (node_modules, .venv)
are never offered while their project was active in the last 7 days.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

DAY = 86400
RECENT_DAYS = 7
LARGE_OLD_BYTES = 50 * 1024 * 1024
LARGE_OLD_DAYS = 30
DUP_MIN_BYTES = 1024 * 1024
MAX_ENTRIES = 2_000_000
MAX_SECONDS = 240.0
MAX_HASH_BYTES = 16 * 1024 ** 3
ITEM_LIMIT = 400

# Never treated as a project, whatever the history says.
EXCLUDED_TOP = {
    ".ssh", ".gnupg", ".config", ".cloudflared", ".claude", ".codex", ".aws", ".kube", ".docker", ".password-store",
    ".pki", ".local", ".hermes", ".cua-driver", ".mozilla", ".vscode-server", ".bashrc", ".profile", ".netrc",
    "incoming", "backups", "snap", ".cache", ".npm", ".pnpm-store", ".nvm", ".cargo", ".rustup", ".bun",
}
SECRET_PART = re.compile(r"(secret|credential|private[_-]?key|\.env$|^\.env\.|token|password|\.pem$|\.key$|id_rsa|id_ed25519)", re.I)
# Caches that Hermes's own tools populate outside the home.
TOOL_CACHES = {
    ".cache/ms-playwright": ("Playwright browsers", "Browsers downloaded for web automation; reinstalled with `playwright install` when needed."),
    ".npm/_cacache": ("npm download cache", "Packages npm downloaded; fetched again on the next install."),
    ".cache/pip": ("pip download cache", "Python packages pip downloaded; fetched again when needed."),
    ".cache/uv": ("uv cache (outside Hermes)", "Python packages uv downloaded for projects; fetched again when needed."),
    ".cache/node-gyp": ("node-gyp headers", "Build headers for native Node modules; fetched again when needed."),
    ".cache/huggingface": ("Hugging Face downloads", "Downloaded models and datasets; large to re-download."),
    ".pnpm-store": ("pnpm store", "Packages pnpm downloaded; fetched again on the next install."),
}
DEPENDENCY_DIRS = {
    "node_modules": "npm install",
    ".venv": "uv sync / pip install -r requirements.txt",
    "venv": "pip install -r requirements.txt",
    ".next": "next build",
    ".turbo": "rebuilt automatically",
    ".parcel-cache": "rebuilt automatically",
    ".pytest_cache": "rebuilt automatically",
    ".mypy_cache": "rebuilt automatically",
    ".ruff_cache": "rebuilt automatically",
}
BACKUP_NAME = re.compile(r"(^|[-_.])(before|backup|bak|pre-?save|snapshot|copy of)[-_. ]|\.(bak|orig|old)$|~$", re.I)
TEXT_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".json", ".md", ".html", ".sh", ".yaml", ".yml", ".toml",
            ".txt", ".sql", ".css", ".cfg", ".ini", ".service", ".env.example"}
CREATING = re.compile(r"(>|\bcp\b|\bmv\b|mkdir|git clone|unzip|\btar\b|npm (i|install|ci)|pnpm|pip install|uv (venv|sync|pip|run)"
                      r"|playwright install|python3? |node |curl .*-o|wget )")
PATH_RE = re.compile(r"(/[A-Za-z0-9._+\-]+(?:/[A-Za-z0-9._+\-]+)*)")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ----- provenance ----------------------------------------------------------------------

def _project_root(path: str, user_home: str, hermes_home: str) -> str | None:
    if not path.startswith(user_home.rstrip("/") + "/") or path.startswith(hermes_home.rstrip("/") + "/") or path == hermes_home:
        return None
    parts = path[len(user_home.rstrip("/")) + 1:].split("/")
    top = parts[0]
    if not top or top in EXCLUDED_TOP or SECRET_PART.search(top):
        return None
    first = os.path.join(user_home, top)
    if not os.path.isdir(first) or os.path.islink(first):
        return None
    # A plain container (e.g. ~/workspace) holding many projects: use the child.
    if not os.path.exists(os.path.join(first, ".git")) and len(parts) > 1:
        second = os.path.join(first, parts[1])
        if os.path.isdir(second) and not os.path.islink(second) and not SECRET_PART.search(parts[1]):
            return second
    return first


def provenance(hermes_home: str, user_home: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = defaultdict(lambda: {"sessions": set(), "writes": 0, "commands": 0, "last": 0.0, "kanban": 0})
    db = os.path.join(hermes_home, "state.db")
    if os.path.isfile(db):
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            rows = conn.execute("select session_id, timestamp, tool_calls from messages where tool_calls is not null and role = 'assistant'")
            for session, stamp, raw in rows:
                try:
                    calls = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                for call in calls if isinstance(calls, list) else []:
                    function = (call or {}).get("function") or {}
                    name = function.get("name")
                    try:
                        args = json.loads(function.get("arguments") or "{}")
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(args, dict):
                        continue
                    targets: list[tuple[str, str]] = []
                    if name in {"write_file", "patch"} and isinstance(args.get("path"), str):
                        targets.append((args["path"], "writes"))
                    elif name == "terminal" and isinstance(args.get("command"), str) and CREATING.search(args["command"]):
                        targets += [(match, "commands") for match in PATH_RE.findall(args["command"])]
                        if isinstance(args.get("workdir"), str):
                            targets.append((args["workdir"], "commands"))
                    for target, kind in targets:
                        root = _project_root(os.path.normpath(target), user_home, hermes_home)
                        if root is None:
                            continue
                        entry = found[root]
                        entry["sessions"].add(session)
                        entry[kind] += 1
                        entry["last"] = max(entry["last"], float(stamp or 0))
            conn.close()
        except sqlite3.Error:
            pass
    boards = [os.path.join(hermes_home, "kanban.db")]
    boards_dir = os.path.join(hermes_home, "kanban", "boards")
    if os.path.isdir(boards_dir):
        boards += [os.path.join(boards_dir, name, "kanban.db") for name in os.listdir(boards_dir)]
    for board in boards:
        if not os.path.isfile(board):
            continue
        try:
            conn = sqlite3.connect(f"file:{board}?mode=ro", uri=True, timeout=2)
            cols = [row[1] for row in conn.execute("pragma table_info(tasks)")]
            if "workspace_path" in cols:
                for (path,) in conn.execute("select workspace_path from tasks where workspace_path is not null"):
                    root = _project_root(os.path.normpath(str(path).split("/.worktrees/")[0]), user_home, hermes_home)
                    if root:
                        found[root]["kanban"] += 1
            conn.close()
        except sqlite3.Error:
            pass
    result = {}
    for root, entry in found.items():
        if entry["writes"] or entry["commands"] >= 2 or entry["kanban"]:
            result[root] = {"sessions": len(entry["sessions"]), "writes": entry["writes"], "commands": entry["commands"],
                            "kanbanTasks": entry["kanban"], "lastActivity": int(entry["last"]) or None}
    return result


# ----- live dependencies -----------------------------------------------------------------

class Blind(Exception):
    """This host does not let Storage see every program's open files, so "not in use" cannot be proven."""


def in_use_paths(user_home: str) -> list[str]:
    """cwd, executable and open files of EVERY process, below the user's home.

    Fails closed: raises Blind unless every process could be inspected. A partial view
    (no /proc, hidden processes, another user's programs) would make "not in use" a guess.
    """
    if not os.path.isdir("/proc/self/fd"):
        raise Blind("this kind of computer doesn't let Storage see which programs have files open")
    if not os.path.exists("/proc/1"):
        raise Blind("this computer hides other programs from Hermes")
    prefix = user_home.rstrip("/") + "/"
    paths = set()
    for pid in (entry for entry in os.listdir("/proc") if entry.isdigit()):
        try:
            os.listdir(f"/proc/{pid}/fd")
        except FileNotFoundError:
            continue  # the process just exited
        except PermissionError:
            raise Blind("Hermes isn't allowed to see what other users' programs have open (it isn't running as an administrator)") from None
        except OSError:
            continue
        for link in ("cwd", "exe"):
            try:
                target = os.readlink(f"/proc/{pid}/{link}")
                if target.startswith(prefix):
                    paths.add(target)
            except OSError:
                pass
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    target = os.readlink(f"/proc/{pid}/fd/{fd}")
                except OSError:
                    continue
                if target.startswith(prefix):
                    paths.add(target)
        except OSError:
            pass
    return sorted(paths)


def service_text() -> list[tuple[str, str]]:
    """(source, text) for systemd units and crontabs that could start things from a project."""
    sources = []
    for directory in ("/etc/systemd/system", "/lib/systemd/system", os.path.expanduser("~/.config/systemd/user"), "/etc/cron.d"):
        try:
            for name in os.listdir(directory):
                path = os.path.join(directory, name)
                if os.path.isfile(path) and os.path.getsize(path) < 256 * 1024:
                    try:
                        sources.append((name, Path(path).read_text(errors="replace")))
                    except OSError:
                        pass
        except OSError:
            pass
    try:
        crontab = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5).stdout
        if crontab:
            sources.append(("crontab", crontab))
    except (OSError, subprocess.SubprocessError):
        pass
    return sources


# ----- scanning one project -------------------------------------------------------------

class Budget:
    def __init__(self):
        self.entries = 0
        self.started = time.monotonic()
        self.hashed = 0
        self.reason: str | None = None

    def spent(self) -> bool:
        if self.reason:
            return True
        if self.entries > MAX_ENTRIES:
            self.reason = "entry limit"
        elif time.monotonic() - self.started > MAX_SECONDS:
            self.reason = "time limit"
        return bool(self.reason)


def _identity(info: os.stat_result) -> dict[str, Any]:
    """Must match fsutil.identity exactly: the delete engine compares them."""
    kind = "dir" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "other"
    value: dict[str, Any] = {"device": int(info.st_dev), "inode": int(info.st_ino), "kind": kind}
    if kind == "file":
        value.update(size=int(info.st_size), mtimeNs=int(info.st_mtime_ns))
    return value


def _tree_size(path: str, device: int, budget: Budget) -> tuple[int, int, int]:
    """(allocated bytes, files, newest mtime) without following links or leaving the disk."""
    total = files = 0
    newest = 0
    seen = set()
    for current, dirs, names in os.walk(path):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(current, d))]
        try:
            newest = max(newest, int(os.lstat(current).st_mtime))
        except OSError:
            pass
        for name in names:
            budget.entries += 1
            try:
                info = os.lstat(os.path.join(current, name))
            except OSError:
                continue
            if info.st_dev != device or not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) in seen:
                continue
            seen.add((info.st_dev, info.st_ino))
            total += info.st_blocks * 512
            files += 1
            newest = max(newest, int(info.st_mtime))
        if budget.spent():
            break
    return total, files, newest


def _git_tracked(root: str) -> set[str] | None:
    if not os.path.exists(os.path.join(root, ".git")):
        return None
    try:
        done = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return set()
    return {item.decode("utf-8", "surrogateescape") for item in done.stdout.split(b"\0") if item}


def scan_project(root: str, budget: Budget) -> dict[str, Any]:
    info = os.lstat(root)
    device = int(info.st_dev)
    total = files = 0
    newest = int(info.st_mtime)
    big_files: list[dict[str, Any]] = []
    dependency_dirs: list[dict[str, Any]] = []
    text_files: list[str] = []
    repos = {}  # nested git repos (relative dir -> tracked set)
    seen = set()
    for current, dirs, names in os.walk(root):
        relative_dir = os.path.relpath(current, root)
        relative_dir = "" if relative_dir == "." else relative_dir
        keep = []
        for name in dirs:
            full = os.path.join(current, name)
            if os.path.islink(full) or SECRET_PART.search(name):
                continue
            if name == ".git":
                size, count, _ = _tree_size(full, device, budget)
                total += size
                files += count
                repos[relative_dir] = None
                continue
            if name == ".worktrees":
                continue  # owned by `hermes worktree` (its own category)
            if name in DEPENDENCY_DIRS and (name != "venv" or os.path.isfile(os.path.join(full, "pyvenv.cfg"))):
                size, count, mtime = _tree_size(full, device, budget)
                total += size
                files += count
                rel = os.path.join(relative_dir, name) if relative_dir else name
                dependency_dirs.append({"path": rel, "name": name, "allocatedBytes": size, "files": count, "modifiedAt": mtime,
                                        "rebuild": DEPENDENCY_DIRS[name], "identity": _identity(os.lstat(full))})
                continue
            keep.append(name)
        dirs[:] = keep
        for name in names:
            budget.entries += 1
            full = os.path.join(current, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_dev != device or (st.st_dev, st.st_ino) in seen:
                continue
            seen.add((st.st_dev, st.st_ino))
            total += st.st_blocks * 512
            files += 1
            newest = max(newest, int(st.st_mtime))
            rel = os.path.join(relative_dir, name) if relative_dir else name
            if SECRET_PART.search(name):
                continue
            if st.st_size >= DUP_MIN_BYTES:
                big_files.append({"path": rel, "name": name, "size": int(st.st_size), "allocatedBytes": int(st.st_blocks * 512),
                                  "modifiedAt": int(st.st_mtime), "identity": _identity(st)})
            if os.path.splitext(name)[1].lower() in TEXT_EXT and st.st_size <= 256 * 1024 and len(text_files) < 4000:
                text_files.append(rel)
        if budget.spent():
            break
    tracked: set[str] = set()
    for repo_dir in repos:
        listed = _git_tracked(os.path.join(root, repo_dir) if repo_dir else root) or set()
        tracked |= {os.path.join(repo_dir, item) if repo_dir else item for item in listed}
    return {"root": root, "device": device, "rootIdentity": {"device": device, "inode": int(info.st_ino)},
            "allocatedBytes": total, "files": files, "newest": newest, "bigFiles": big_files,
            "dependencyDirs": dependency_dirs, "textFiles": text_files, "tracked": tracked, "isRepo": bool(repos)}


def _hash(path: str, size: int, budget: Budget, *, edges: bool) -> str | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        digest = hashlib.sha256()
        if edges:
            chunk = 64 * 1024
            digest.update(os.pread(fd, chunk, 0))
            digest.update(os.pread(fd, chunk, max(0, size - chunk)))
            budget.hashed += 2 * chunk
            return digest.hexdigest()
        offset = 0
        while offset < size:
            block = os.pread(fd, min(4 * 1024 * 1024, size - offset), offset)
            if not block:
                return None
            digest.update(block)
            offset += len(block)
            budget.hashed += len(block)
            if budget.hashed > MAX_HASH_BYTES:
                budget.reason = "read limit"
                return None
        return digest.hexdigest()
    finally:
        os.close(fd)


# ----- references -----------------------------------------------------------------------

class References:
    """Bounded text search: which project files or services mention an item."""

    def __init__(self, project: dict[str, Any], services: list[tuple[str, str]]):
        self.root = project["root"]
        self.corpus: list[tuple[str, str]] = []
        budget = 64 * 1024 * 1024
        for rel in project["textFiles"]:
            try:
                text = Path(self.root, rel).read_text(errors="ignore")
            except OSError:
                continue
            budget -= len(text)
            if budget < 0:
                break
            self.corpus.append((rel, text))
        self.services = services

    def services_for(self, absolute: str) -> list[str]:
        pattern = re.compile(re.escape(absolute) + r"(?=$|[/\s\"':;=])", re.M)
        return [f"service {source}" for source, text in self.services if pattern.search(text)]

    def find(self, rel: str, *, also_name: bool, relative: bool = True) -> list[str]:
        absolute = os.path.join(self.root, rel)
        name = os.path.basename(rel)
        needles = [absolute, rel] if relative else [absolute]
        if also_name and (len(name) >= 12 or re.search(r"\d", name)):
            needles.append(name)
        hits = self.services_for(absolute)
        for source, text in self.corpus:
            if source == rel:
                continue
            if any(needle in text for needle in needles):
                hits.append(source)
                if len(hits) >= 3:
                    break
        return hits[:3]


# ----- findings -------------------------------------------------------------------------

def _gate(item: dict[str, Any], *, project: dict[str, Any], in_use: list[str], refs: References, now: float,
          tracked_check: bool = True, also_name: bool = True, relative: bool = True) -> None:
    absolute = os.path.join(project["root"], item["path"])
    reasons = []
    if tracked_check and (item["path"] in project["tracked"] or any(path.startswith(item["path"] + "/") for path in project["tracked"] if item["identity"]["kind"] == "dir")):
        reasons.append("tracked in git")
    users = [path for path in in_use if path == absolute or path.startswith(absolute + "/")]
    if users:
        reasons.append("in use by a running program")
    if now - item["modifiedAt"] < RECENT_DAYS * DAY:
        reasons.append(f"changed in the last {RECENT_DAYS} days")
    references = refs.find(item["path"], also_name=also_name, relative=relative)
    if references:
        reasons.append("referenced by " + ", ".join(references))
    item["selectable"] = not reasons
    item["note"] = "; ".join(reasons) or None
    item["ageDays"] = round((now - item["modifiedAt"]) / DAY, 1)


def _owner_label(package: str, hermes_home: str) -> str:
    """Human label for an install that uses a shared cache."""
    if "/hermes-agent/" in package or package.startswith(hermes_home):
        return "Hermes (browser tool)" if "/hermes-agent/" in package else "Hermes plugin tooling"
    parts = [part for part in package.split("/") if part]
    for marker in (".venv", "venv", "node_modules", "site-packages"):
        if marker in parts:
            return "/".join(parts[max(0, parts.index(marker) - 1):parts.index(marker)]) or package
    return package


def build(hermes_home: str, user_home: str) -> dict[str, Any]:
    os.nice(10)
    now = time.time()
    budget = Budget()
    started = _now()
    projects_found = provenance(hermes_home, user_home)
    try:
        in_use, blind = in_use_paths(user_home), None
    except Blind as error:
        in_use, blind = [], str(error)
    services = service_text()
    projects: list[dict[str, Any]] = []
    findings: dict[str, list[dict[str, Any]]] = {"duplicates": [], "backups": [], "dependencies": [], "large-old": [], "tool-caches": []}
    by_size: dict[int, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    scanned: dict[str, dict[str, Any]] = {}
    refs_by_root: dict[str, References] = {}
    # A plain (non-git) container holding other projects gives way to them; otherwise scan the outermost.
    roots = sorted(projects_found)
    roots = [root for root in roots if os.path.exists(os.path.join(root, ".git"))
             or not any(other.startswith(root + "/") for other in roots)]
    roots = [root for root in roots if not any(root != other and root.startswith(other + "/") for other in roots)]
    for root in roots:
        if budget.spent():
            break
        try:
            project = scan_project(root, budget)
        except OSError:
            continue
        scanned[root] = project
        evidence = projects_found[root]
        projects.append({"root": root, "name": os.path.basename(root), "allocatedBytes": project["allocatedBytes"],
                         "files": project["files"], "newest": project["newest"], "isRepo": project["isRepo"],
                         "active": now - project["newest"] < RECENT_DAYS * DAY, **evidence})
        refs = References(project, services)
        refs_by_root[root] = refs
        base = {"root": root, "rootIdentity": project["rootIdentity"]}
        for item in project["bigFiles"]:
            by_size[item["size"]].append((project, item))
            if BACKUP_NAME.search(item["name"]):
                row = {**base, **item, "kind": "file", "files": 1}
                _gate(row, project=project, in_use=in_use, refs=refs, now=now)
                findings["backups"].append(row)
            elif item["size"] >= LARGE_OLD_BYTES and now - item["modifiedAt"] > LARGE_OLD_DAYS * DAY:
                row = {**base, **item, "kind": "file", "files": 1}
                _gate(row, project=project, in_use=in_use, refs=refs, now=now)
                findings["large-old"].append(row)
        for item in project["dependencyDirs"]:
            row = {**base, **item, "kind": "dir"}
            # Rebuildable: mentioning "node_modules" in a config is normal; only absolute paths count.
            _gate(row, project=project, in_use=in_use, refs=refs, now=now, also_name=False, relative=False)
            extra = []
            owner = os.path.join(root, os.path.dirname(item["path"])).rstrip("/")  # the folder this dependency belongs to
            runs_as = refs.services_for(owner)
            if runs_as:
                extra.append("the project runs as " + ", ".join(runs_as))
            if any(path == owner or path.startswith(owner + "/") for path in in_use):
                extra.append("a running program is working in this project")
            if now - project["newest"] < RECENT_DAYS * DAY:
                extra.append("project used in the last 7 days")
            if extra:
                row["selectable"] = False
                row["note"] = "; ".join(filter(None, [row["note"], *extra]))
            findings["dependencies"].append(row)
    # Exact duplicates across all projects: edge hash, then full hash.
    for size, members in sorted(by_size.items(), reverse=True):
        if len(members) < 2 or budget.reason == "read limit":
            continue
        edge_groups: dict[str, list] = defaultdict(list)
        for project, item in members:
            edge = _hash(os.path.join(project["root"], item["path"]), size, budget, edges=True)
            if edge:
                edge_groups[edge].append((project, item))
        for group in edge_groups.values():
            if len(group) < 2:
                continue
            full_groups: dict[str, list] = defaultdict(list)
            for project, item in group:
                digest = _hash(os.path.join(project["root"], item["path"]), size, budget, edges=False)
                if digest:
                    full_groups[digest].append((project, item))
            for digest, copies in full_groups.items():
                if len(copies) < 2:
                    continue
                # Keep: a git-tracked copy if any, else the most recently modified, else the shortest path.
                copies.sort(key=lambda pair: (pair[1]["path"] not in pair[0]["tracked"], -pair[1]["modifiedAt"], len(pair[1]["path"])))
                keeper_project, keeper = copies[0]
                keeper_ref = {"root": keeper_project["root"], "rootIdentity": keeper_project["rootIdentity"],
                              "path": keeper["path"], "identity": keeper["identity"]}
                for project, item in copies[1:]:
                    row = {"root": project["root"], "rootIdentity": project["rootIdentity"], **item, "kind": "file", "files": 1,
                           "keeper": keeper_ref, "group": digest[:16],
                           "keeperDisplay": os.path.join(keeper_project["root"], keeper["path"])}
                    _gate(row, project=project, in_use=in_use, refs=refs_by_root[project["root"]], now=now, also_name=False)
                    findings["duplicates"].append(row)
    duplicate_paths = {(row["root"], row["path"]) for row in findings["duplicates"]}
    for key in ("backups", "large-old"):
        findings[key] = [row for row in findings[key] if (row["root"], row["path"]) not in duplicate_paths]
    # Tool caches Hermes populated outside its home.
    for relative, (title, description) in TOOL_CACHES.items():
        path = os.path.join(user_home, relative)
        if not os.path.isdir(path) or os.path.islink(path):
            continue
        info = os.lstat(path)
        size, count, mtime = _tree_size(path, info.st_dev, budget)
        users = [p for p in in_use if p == path or p.startswith(path + "/")]
        findings["tool-caches"].append({
            "root": user_home, "rootIdentity": {"device": int(os.lstat(user_home).st_dev), "inode": int(os.lstat(user_home).st_ino)},
            "path": relative, "name": title, "kind": "dir", "allocatedBytes": size, "files": count, "modifiedAt": mtime,
            "identity": _identity(info), "description": description, "ageDays": round((now - mtime) / DAY, 1),
            "selectable": not users, "note": "in use by a running program" if users else None})
    # Playwright browser builds: Playwright's own .links registry says which installs need which build.
    playwright = os.path.join(user_home, ".cache", "ms-playwright")
    if os.path.isdir(playwright) and not os.path.islink(playwright):
        needed: dict[str, list[str]] = defaultdict(list)
        links = os.path.join(playwright, ".links")
        for entry in (os.scandir(links) if os.path.isdir(links) else []):
            try:
                package = Path(entry.path).read_text().strip()
                browsers = json.loads(Path(package, "browsers.json").read_text())["browsers"]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            owner = _owner_label(package, hermes_home)
            for browser in browsers:
                if isinstance(browser, dict) and browser.get("name") and browser.get("revision"):
                    needed[f"{browser['name'].replace('-', '_')}-{browser['revision']}"].append(owner)
        home_info = os.lstat(user_home)
        users_of_cache = sorted({owner for owners in needed.values() for owner in owners})
        for row in findings["tool-caches"]:
            if row["path"] == ".cache/ms-playwright" and users_of_cache:
                row["selectable"] = False
                row["note"] = "needed by " + ", ".join(users_of_cache)
        for entry in os.scandir(playwright):
            if not entry.is_dir(follow_symlinks=False) or not re.fullmatch(r".+-\d+", entry.name):
                continue
            owners = needed.get(entry.name, [])
            info = os.lstat(entry.path)
            size, count, mtime = _tree_size(entry.path, info.st_dev, budget)
            users = [p for p in in_use if p.startswith(entry.path + "/")]
            reasons = (["needed by " + ", ".join(sorted(set(owners)))] if owners else []) + (["in use by a running program"] if users else [])
            if not reasons:
                description = "No installed Playwright uses this build any more; nothing needs to re-download it."
            else:
                description = "Still required by an installed Playwright."
            findings["tool-caches"].append({
                "root": user_home, "rootIdentity": {"device": int(home_info.st_dev), "inode": int(home_info.st_ino)},
                "path": f".cache/ms-playwright/{entry.name}", "name": f"Playwright browser {entry.name}",
                "kind": "dir", "allocatedBytes": size, "files": count, "modifiedAt": mtime, "identity": _identity(info),
                "description": description, "ageDays": round((now - mtime) / DAY, 1),
                "selectable": not reasons, "note": "; ".join(reasons) or None})
    for key, rows in findings.items():
        rows.sort(key=lambda row: row["allocatedBytes"], reverse=True)
        findings[key] = rows[:ITEM_LIMIT]
        if blind:
            for row in findings[key]:
                row["selectable"] = False
                row["note"] = f"Storage can't check whether programs are using it: {blind}"
    projects.sort(key=lambda row: row["allocatedBytes"], reverse=True)
    return {"state": "complete" if not budget.reason else "partial", "budgetReason": budget.reason, "startedAt": started,
            "finishedAt": _now(), "userHome": user_home, "projects": projects, "findings": findings, "blind": blind,
            "hashedBytes": budget.hashed}


def main() -> None:
    hermes_home, user_home = sys.argv[1], sys.argv[2]
    print(json.dumps(build(hermes_home, user_home)))


if __name__ == "__main__":
    main()
