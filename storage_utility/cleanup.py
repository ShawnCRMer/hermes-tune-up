"""Cleanup categories: what is using space, what can be done, and what you lose.

Each category is measured on the serving host and carries plain-language
explanation, consequence and recovery semantics. Three kinds of action exist:

* review   - per-item files/folders the user selects, then Trash or delete
* command  - an allow-listed Hermes/uv maintenance command with its owner's rules
* guidance - an exact offline procedure when Hermes must be stopped first

Nothing is preselected or deleted automatically; age and size are evidence,
not proof that something is disposable.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Any

from . import commands, discover
from .fsutil import list_children, measure, volume_free
from .scope import Scope, is_sensitive
from .childenv import minimal_env

_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_SECONDS = 300.0
HOUR = 3600
DAY = 86400
ITEM_LIMIT = 300
_ROTATED_LOG = re.compile(r"(\.log\.\d+|\.\d+|\.gz|\.old|\.bak|\.zip)$")


def invalidate(scope: Scope) -> None:
    discover.invalidate()
    with _CACHE_LOCK:
        _CACHE.pop(str(scope.home), None)


def categories(scope: Scope, *, fresh: bool = False) -> list[dict[str, Any]]:
    key = str(scope.home)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and not fresh and time.monotonic() - cached[0] < _CACHE_SECONDS:
            return cached[1]
    result = [category for category in _build(scope) if category is not None]
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), result)
    return result


def summary(category: dict[str, Any]) -> dict[str, Any]:
    """Category without its item list (for overview cards)."""
    value = {key: item for key, item in category.items() if key != "items"}
    items = category.get("items") or []
    value["itemCount"] = len(items)
    value["selectableCount"] = sum(1 for item in items if item.get("selectable"))
    return value


def find(scope: Scope, category_id: str, *, fresh: bool = False) -> dict[str, Any] | None:
    """Return one category; fresh=True re-measures only that category."""
    if fresh and category_id in _PROJECT_CATEGORIES:
        return _project_category(scope, category_id, fresh=True)
    if fresh:
        builder = BUILDERS.get(category_id)
        return builder(scope) if builder is not None else None
    for category in categories(scope):
        if category["id"] == category_id:
            return category
    return None


# ----- helpers -------------------------------------------------------------------

def _size(scope: Scope, *paths: str) -> int | None:
    total = None
    for relative in paths:
        result = measure(scope, relative, budget_seconds=20)
        if result is not None:
            total = (total or 0) + int(result["allocatedBytes"])
    return total


def _file_size(scope: Scope, name: str) -> int:
    try:
        info = (scope.home / name).lstat()
    except OSError:
        return 0
    return int(getattr(info, "st_blocks", 0)) * 512 if stat.S_ISREG(info.st_mode) else 0


def _item_id(category_id: str, path: str, ident: dict[str, Any]) -> str:
    raw = f"{category_id}:{path}:{ident['device']}:{ident['inode']}:{ident['kind']}"
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:24]


def _items(scope: Scope, category_id: str, roots: tuple[str, ...], *, idle_days: float = 0, name_filter=None) -> list[dict[str, Any]]:
    now = time.time()
    items: list[dict[str, Any]] = []
    for root in roots:
        for name, info in list_children(scope, root):
            relative = f"{root}/{name}"
            if name.startswith(".storage-claim-") or is_sensitive(relative) or stat.S_ISLNK(info.st_mode):
                continue
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or info.st_dev != scope.root_device:
                continue
            if name_filter is not None and not name_filter(name, info):
                continue
            measured = measure(scope, relative, budget_seconds=8)
            if measured is None:
                continue
            modified = int(measured["modifiedAt"])
            age = max(0.0, now - modified)
            selectable, note = True, None
            if not measured["complete"]:
                selectable, note = False, "Too large to measure completely right now"
            elif age < HOUR:
                selectable, note = False, "Changed in the last hour - may be in use"
            elif idle_days and age < idle_days * DAY:
                selectable, note = False, f"Used in the last {int(idle_days)} days"
            elif age < 7 * DAY:
                note = "Used in the last 7 days"
            items.append({
                "itemId": _item_id(category_id, relative, measured["identity"]), "path": relative, "name": name,
                "kind": measured["identity"]["kind"], "allocatedBytes": int(measured["allocatedBytes"]),
                "files": int(measured["files"]), "modifiedAt": modified, "ageDays": round(age / DAY, 1),
                "selectable": selectable, "note": note, "identity": measured["identity"],
            })
    items.sort(key=lambda item: item["allocatedBytes"], reverse=True)
    return items


def _command(scope: Scope, command_id: str) -> dict[str, Any]:
    spec = commands.SPECS[command_id]
    available, reason = commands.availability(scope, spec)
    return {"commandId": command_id, "label": spec.label, "display": spec.display, "description": spec.description,
            "consequence": spec.consequence, "destructive": spec.destructive, "available": available, "reason": reason,
            "needsPreview": spec.preview is not None,
            "params": {name: list(values) for name, values in commands.allowed_values(scope, spec).items()}}


def _category(**values: Any) -> dict[str, Any]:
    base = {"items": [], "commands": [], "guidance": None, "reclaimable": {"bytes": None, "certainty": "unknown"},
            "action": "review", "status": "ok"}
    base.update(values)
    items = base["items"]
    base["itemTotal"] = len(items)
    base["items"] = items[:ITEM_LIMIT]
    if base["action"] == "review":
        base["bytes"] = sum(item["allocatedBytes"] for item in items) if base.get("bytes") is None else base["bytes"]
        if not items:
            base["status"] = "empty"
    return base


def _human(value: int | None) -> str:
    if value is None:
        return "unknown"
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1000 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit in {"B", "KB"} or amount >= 10 else f"{amount:.1f} {unit}"
        amount /= 1000
    return f"{value} B"


# ----- category builders ---------------------------------------------------------

def _db_stats(path: Path) -> dict[str, Any] | None:
    """Hermes's lock-tracked read-only stats. Never raw-open the live DB in-process:
    closing any descriptor to it would drop this process's own SQLite locks."""
    try:
        from hermes_state_dbfile import collect_state_db_stats
    except Exception:
        return None
    try:
        stats = collect_state_db_stats(path)
    except Exception:
        return None
    return stats if isinstance(stats, dict) and stats.get("page_size") else None


def _session_db(scope: Scope) -> dict[str, Any] | None:
    db = scope.home / "state.db"
    if not db.is_file() or db.is_symlink():
        return None
    size = _file_size(scope, "state.db") + _file_size(scope, "state.db-wal") + _file_size(scope, "state.db-shm")
    stats = _db_stats(db)
    dead = live = None
    upgrade = False
    if stats:
        page = int(stats["page_size"])
        dead = int(stats.get("freelist_count") or 0) * page
        live = max(0, int(stats.get("page_count") or 0) * page - dead)
        upgrade = bool(stats.get("fts_storage_upgrade_needed") or stats.get("fts_teardown_pending") or stats.get("fts_rebuild_pending"))
    free = volume_free(scope)
    command = "hermes sessions optimize-storage --yes" if upgrade else "hermes sessions optimize"
    worth = dead is not None and dead >= 64 * 1024 * 1024
    plan = discover.stop_plan(scope) if (worth or upgrade) else {}
    holders = plan.get("holders") or []
    if plan.get("stopCommand"):
        stop_text = ("Stop everything that keeps the session database open, watchers first so nothing respawns: "
                     + ", ".join(plan["stopUnits"]) + ". This Storage panel disconnects too; that is expected.")
        stop_command, start_command = plan["stopCommand"], plan["startCommand"]
    else:
        stop_text = "Stop every Hermes process using this profile: the gateway/background service, the Desktop app and any dashboard."
        stop_command, start_command = "hermes gateway stop", "hermes gateway start"
    steps = [{"text": stop_text, "command": stop_command}]
    for row in plan.get("looseHolders") or []:
        steps.append({"text": f"Also close this process, which has the database open outside a Hermes service: {row['command']} (PID {row['pid']}).",
                      "command": f"kill {row['pid']}"})
    steps += [
        {"text": "Check that nothing still holds the database (no output means you can continue).",
         "command": f"fuser -v {scope.home}/state.db {scope.home}/state.db-wal 2>&1 | grep -v '^$' || true"},
        {"text": "Compact the session database. Conversations, settings and search are unchanged; only empty space is removed." +
                 (" This also finishes the search-index upgrade." if upgrade else " The search index is already in the compact layout, so only the VACUUM step runs."),
         "command": command},
        {"text": "Start everything again.", "command": start_command},
    ]
    warning = None
    need = live * 2 if live is not None else None
    if need is not None and free is not None and free < need:
        warning = (f"Compaction temporarily needs about {_human(need)} free (a copy of the {_human(live)} of live data plus its journal); "
                   f"this disk has {_human(free)}. Free space first, or compact into another disk with VACUUM INTO and swap the files.")
    if stats is None:
        summary_text = "Size measured; empty space could not be read on this host"
    elif worth:
        summary_text = f"{_human(dead)} of empty space inside - reclaimable by compaction"
    else:
        summary_text = "Compact - little empty space inside"
    return _category(
        id="session-db", title="Session database", icon="database", action="guidance", bytes=size,
        status="attention" if worth else "ok",
        summary=summary_text,
        explanation="state.db holds every conversation and its search index. When sessions are deleted or the index is rebuilt, SQLite keeps the freed pages inside the file for reuse instead of returning them to the disk.",
        consequence="Compaction changes no data. While it runs Hermes must be stopped.",
        recovery="Not destructive.",
        reclaimable={"bytes": dead, "certainty": "verified" if stats else "unknown",
                     "label": "empty pages measured by SQLite" if stats else "not measurable from this host"},
        details={"sessions": stats.get("sessions") if stats else None, "messages": stats.get("messages") if stats else None,
                 "liveBytes": live, "emptyBytes": dead, "searchIndexUpgrade": upgrade},
        guidance={"why": "Storage runs inside Hermes, so it cannot stop Hermes to compact its own database. Run these steps in a terminal on the serving host. "
                         "Use --force only as a last resort: rewriting under a live writer can leave agents refusing turns until every writer stops.",
                  "steps": steps, "warning": warning,
                  "holders": holders, "crashLooping": plan.get("crashLooping") or []} if worth or upgrade else None,
    )


def _old_sessions(scope: Scope) -> dict[str, Any] | None:
    if not (scope.home / "state.db").is_file():
        return None
    return _category(
        id="old-sessions", title="Old conversations (optional)", icon="history", action="command", bytes=None,
        priority="low",
        summary="Optional tidy-up · conversations are small; compaction is what frees space",
        explanation="Uses `hermes sessions prune`. Pinned, archived and still-open sessions are always skipped. Preview first: it runs a dry run and changes nothing.",
        consequence=commands.SPECS["sessions-prune"].consequence,
        recovery="Permanent. Export anything you want to keep first (hermes sessions export).",
        commands=[_command(scope, "sessions-prune")],
    )


def _package_cache(scope: Scope) -> dict[str, Any] | None:
    size = _size(scope, "cache/uv")
    if size is None:
        return None
    return _category(
        id="package-cache", title="Package download cache", icon="package", action="command", bytes=size,
        summary=f"{_human(size)} of downloaded Python packages",
        explanation="Hermes's package manager keeps downloaded packages so installs and updates are fast. `uv cache prune` removes the ones no environment uses.",
        consequence=commands.SPECS["uv-cache-prune"].consequence, recovery="Rebuildable.",
        commands=[_command(scope, "uv-cache-prune")],
    )


def _runtimes(scope: Scope) -> dict[str, Any] | None:
    size = _size(scope, "installs", "tools")
    if size is None:
        return None
    return _category(
        id="runtime-versions", title="Hermes runtimes & package store", icon="layers", action="command", bytes=size,
        summary=f"{_human(size)} of environments and bundled tools",
        explanation="Python/Node runtimes, browsers and dependency environments installed by Hermes. Updates leave older generations behind until the package manager garbage-collects them.",
        consequence=commands.SPECS["pm-gc"].consequence, recovery="Rebuildable by reinstalling.",
        commands=[_command(scope, "pm-gc")],
    )


def _checkpoints(scope: Scope) -> dict[str, Any] | None:
    size = _size(scope, "checkpoints") or 0
    return _category(
        id="checkpoints", title="Checkpoints", icon="discard", action="command", bytes=size,
        status="empty" if size == 0 else "ok",
        summary="No rollback history stored" if size == 0 else f"{_human(size)} of /rollback history",
        explanation="Snapshots Hermes takes before editing files so /rollback can undo agent changes. Managed by `hermes checkpoints`.",
        consequence="Pruned or cleared history can no longer be rolled back.", recovery="Permanent.",
        commands=[] if size == 0 else [_command(scope, "checkpoints-prune"), _command(scope, "checkpoints-clear")],
    )


def _worktrees(scope: Scope) -> dict[str, Any] | None:
    repos = [row for row in discover.worktree_repos(scope) if row.get("worktrees")]
    if not repos:
        return None
    total = sum(int(row.get("bytes") or 0) for row in repos)
    trees = sum(int(row.get("worktrees") or 0) for row in repos)
    return _category(
        id="worktrees", title="Agent worktrees", icon="git-branch", action="command", bytes=total,
        summary=f"{trees} worktrees in {len(repos)} repositories",
        explanation="Separate checkouts Hermes makes so agents and Kanban tasks can work without touching your main copy. "
                    "They live in each repository's .worktrees folder, outside the Hermes home. Hermes's own prune keeps anything with "
                    "unsaved or unpushed work, and leaves Kanban task trees to Kanban (they go when the task is archived).",
        consequence=commands.SPECS["worktree-prune"].consequence, recovery="Hermes archives work it cannot prove is saved elsewhere.",
        details={"repos": repos},
        commands=[_command(scope, "worktree-prune")],
    )


def _webui(scope: Scope) -> dict[str, Any] | None:
    root = "webui/sessions/_run_journal"
    items = _items(scope, "webui-journals", (root,))
    if not items:
        return None
    newest = max(item["modifiedAt"] for item in items)
    idle = int((time.time() - newest) / DAY)
    return _category(
        id="webui-journals", title="Hermes WebUI run journals", icon="browser", items=items,
        summary=f"{len(items)} run journals · last written {idle} days ago",
        explanation="Replay logs written by the separate Hermes WebUI app (the `webui` folder), not by Hermes itself. If you no longer use WebUI, these are leftovers.",
        consequence="WebUI can no longer replay those runs. Its conversations themselves are not affected.",
        recovery="Trash keeps them restorable until you remove them from the Hermes Trash.",
    )


def _backups(scope: Scope) -> dict[str, Any]:
    # .curator_backups holds the skill ledger's live blob store, not removable backups.
    items = _items(scope, "backups", ("backups", "state-snapshots"))
    return _category(
        id="backups", title="Backups & snapshots", icon="archive", items=items,
        summary=f"{len(items)} backups and snapshots" if items else "No backups",
        explanation="Copies Hermes tools made before risky changes: profile backups and state snapshots. Older ones are usually no longer needed once you are happy with the change.",
        consequence="You can no longer restore from a removed backup.",
        recovery="Trash keeps them restorable until you remove them from the Hermes Trash.",
    )


def _your_files(scope: Scope) -> dict[str, Any]:
    items = _items(scope, "your-files", ("attachments", "outputs", "images"))
    return _category(
        id="your-files", title="Attachments & outputs", icon="file-media", items=items,
        summary=f"{len(items)} items saved from chats and tools" if items else "Nothing saved",
        explanation="Files you attached to chats, and files tools or agents produced. Hermes does not delete these on its own.",
        consequence="Conversations that referenced a removed file will show it as missing.",
        recovery="Trash keeps them restorable until you remove them from the Hermes Trash.",
    )


def _workspaces(scope: Scope) -> dict[str, Any] | None:
    items = _items(scope, "workspaces", ("workspaces",))
    if not items:
        return None
    return _category(
        id="workspaces", title="Workspaces", icon="folder", items=items,
        summary=f"{len(items)} project workspaces",
        explanation="Folders created for agent and board work. They may contain real project files - open them in Explorer before removing anything.",
        consequence="Everything inside the folder is removed. Boards or scheduled jobs that still use it may fail.",
        recovery="Trash keeps them restorable until you remove them from the Hermes Trash.",
    )


def _logs(scope: Scope) -> dict[str, Any]:
    current = _size(scope, "logs") or 0
    items = _items(scope, "logs", ("logs",), name_filter=lambda name, info: stat.S_ISREG(info.st_mode) and bool(_ROTATED_LOG.search(name)))
    return _category(
        id="logs", title="Old logs", icon="output", items=items, bytes=current,
        summary=f"{_human(current)} of logs · {len(items)} rotated files" if items else f"{_human(current)} of logs · rotated automatically",
        explanation="Hermes rotates its logs automatically. Only rotated older copies are offered; active logs are never touched.",
        consequence="Older log history is no longer available for troubleshooting.",
        recovery="Trash keeps them restorable until you remove them from the Hermes Trash.",
    )


def _temporary(scope: Scope) -> dict[str, Any]:
    scratch = _size(scope, "cache/scratch") or 0
    roots = tuple(f"cache/{name}" for name in ("spillover", "terminal-output", "exec", "delegation", "web", "generated"))
    items = _items(scope, "temporary", roots, idle_days=7)
    total = scratch + sum(item["allocatedBytes"] for item in items)
    return _category(
        id="temporary", title="Scratch & temporary output", icon="clock", items=items, bytes=total,
        summary=f"{_human(scratch)} scratch (auto-cleaned) · {sum(1 for item in items if item['selectable'])} saved outputs unused for 7+ days",
        explanation="Scratch space is cleaned by Hermes automatically after 24 idle hours, so it is not listed. Saved tool outputs, web fetches and delegation transcripts are kept until removed; ones unused for 7 days can be selected.",
        consequence="Old tool output and delegation transcripts referenced by past chats can no longer be expanded.",
        recovery="Trash keeps them restorable until you remove them from the Hermes Trash.",
        details={"scratchBytes": scratch},
    )


# ----- Hermes-made folders outside the home ---------------------------------------------

_PROJECT_CATEGORIES = {
    "project-duplicates": ("Duplicate copies in Hermes projects", "copy",
        "Exact byte-for-byte copies of the same file in folders Hermes created. One copy is always kept: one git tracks if any, otherwise the newest.",
        "Only the extra copy goes. If the kept copy changes or disappears before you confirm, its twin is left alone."),
    "project-backups": ("Old backup copies in Hermes projects", "history",
        "Backup and snapshot files Hermes tools saved before changing things (\"before-…-save\", .bak and similar), older than a week.",
        "You can no longer roll back to that snapshot."),
    "project-dependencies": ("Rebuildable dependencies", "package",
        "node_modules, Python virtual environments and build caches inside Hermes projects. Each can be recreated with the command shown on it.",
        "The project needs its install step run again before it works."),
    "project-large-old": ("Large old files in Hermes projects", "file-binary",
        "Files over 50 MB in Hermes-made folders, untouched for 30 days. Large is not junk by itself, so review each.",
        "The file is gone from that project."),
    "tool-caches": ("Tool download caches", "cloud-download",
        "Download caches that Hermes's tools filled outside its home (npm, pip, uv, Playwright browsers…).",
        "Anything needed again is downloaded again."),
}


def _project_name(root: str) -> str:
    from .friendly import project_label
    return project_label({"root": root})


def _project_item(category_id: str, row: dict[str, Any]) -> dict[str, Any]:
    display = f"{row['root'].rstrip('/')}/{row['path']}"
    note = row.get("note")
    if row.get("keeperDisplay"):
        note = "; ".join(filter(None, [f"same as {row['keeperDisplay']} (kept)", note]))
    if row.get("rebuild"):
        note = "; ".join(filter(None, [f"rebuild: {row['rebuild']}", note]))
    if row.get("description"):
        note = "; ".join(filter(None, [row["description"], note]))
    return {
        "itemId": _item_id(category_id, display, row["identity"]), "path": row["path"], "root": row["root"],
        "rootIdentity": row.get("rootIdentity"), "keeper": row.get("keeper"), "display": display,
        "name": row.get("name") or display.rsplit("/", 1)[-1], "kind": row["identity"]["kind"],
        "allocatedBytes": int(row["allocatedBytes"]), "files": int(row.get("files") or 1),
        "modifiedAt": int(row.get("modifiedAt") or 0), "ageDays": row.get("ageDays"),
        "selectable": bool(row.get("selectable")), "note": note, "identity": row["identity"], "external": True,
        "project": _project_name(row["root"]),
    }


def _revalidate(item: dict[str, Any], in_use: list[str]) -> dict[str, Any]:
    """Re-check a scanned item against the disk right now; anything different is not selectable."""
    from .fsutil import Anchor
    try:
        current = measure(Anchor(item["root"], item.get("rootIdentity")), item["path"], budget_seconds=15)
    except OSError:
        current = None
    fresh = dict(item)
    if current is None or current["identity"] != item["identity"]:
        fresh.update(selectable=False, note="changed or gone since the project scan; rescan projects")
        return fresh
    if item["kind"] == "dir" and int(current["modifiedAt"]) > int(item["modifiedAt"]):
        fresh.update(selectable=False, note="written to since the project scan; rescan projects")
        return fresh
    absolute = item["display"]
    if any(path == absolute or path.startswith(absolute + "/") for path in in_use):
        fresh.update(selectable=False, note="in use by a running program")
    fresh["allocatedBytes"] = int(current["allocatedBytes"])
    fresh["modifiedAt"] = int(current["modifiedAt"])
    return fresh


def _project_category(scope: Scope, category_id: str, *, fresh: bool = False) -> dict[str, Any] | None:
    from . import project_jobs
    saved = project_jobs.latest(scope)
    if not saved:
        return None
    key = category_id.replace("project-", "") if category_id != "tool-caches" else "tool-caches"
    rows = (saved.get("findings") or {}).get(key) or []
    if not rows:
        return None
    items = [_project_item(category_id, row) for row in rows if isinstance(row, dict) and row.get("identity")]
    if fresh:
        from .projects import Blind, in_use_paths
        try:
            in_use = in_use_paths(saved.get("userHome") or project_jobs.user_home())
        except Blind as error:
            blind = f"Storage can't check whether programs are using it: {error}"
            items = [{**item, "selectable": False, "note": blind} for item in items]
        else:
            items = [_revalidate(item, in_use) if item["selectable"] else item for item in items]
    title, icon, explanation, consequence = _PROJECT_CATEGORIES[category_id]
    selectable = [item for item in items if item["selectable"]]
    selectable_bytes = sum(item["allocatedBytes"] for item in selectable)
    return _category(
        id=category_id, title=title, icon=icon, items=items, scope="projects", bytes=selectable_bytes,
        summary=f"{_human(selectable_bytes)} safe to review · {len(items) - len(selectable)} held back by safety checks",
        explanation=explanation + " Outside the Hermes home, so each item shows why it is or is not offered.",
        consequence=consequence,
        recovery="Trash keeps them restorable until you remove them from the Hermes Trash.",
        details={"selectableBytes": selectable_bytes, "scannedAt": saved.get("finishedAt")},
    )


def _hermes_projects(scope: Scope) -> dict[str, Any] | None:
    from . import project_jobs
    saved = project_jobs.latest(scope)
    job = project_jobs.status(scope)
    projects = (saved or {}).get("projects") or []
    total = sum(int(row.get("allocatedBytes") or 0) for row in projects)
    return _category(
        id="hermes-projects", title="Folders Hermes created", icon="folder-library", action="projects", bytes=total if saved else None,
        scope="projects",
        summary=(f"{len(projects)} folders outside the Hermes home" if saved else "Not scanned yet") + (" · scanning…" if job["running"] else ""),
        explanation="Folders outside ~/.hermes that Hermes's own session history shows it writing to, plus Kanban task folders. "
                    "Credential and config folders (.ssh, .config…) are never included. Storage scans these for duplicates, old backups, "
                    "rebuildable dependencies and large old files, and checks each for git tracking, running programs, services, cron and references before offering it.",
        consequence="Scanning only reads; nothing is changed.", recovery="Not destructive.",
        details={"projects": projects, "scan": job, "scannedAt": (saved or {}).get("finishedAt"),
                 "state": (saved or {}).get("state"), "budgetReason": (saved or {}).get("budgetReason"),
                 "blind": (saved or {}).get("blind")},
    )


# ----- system-level downloads and logs, and project history (all via owner tools) ---------

def _tree_bytes(path: Path, limit_seconds: float = 20.0) -> int | None:
    total, deadline = 0, time.monotonic() + limit_seconds
    try:
        for current, dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.lstat(os.path.join(current, name)).st_blocks * 512
                except OSError:
                    pass
            if time.monotonic() > deadline:
                return None
    except OSError:
        return None
    return total


def _journal_bytes() -> int | None:
    import subprocess
    try:
        out = subprocess.run(["journalctl", "--disk-usage"], capture_output=True, text=True, timeout=20, env=minimal_env()).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"take up ([0-9.]+)([KMGT])", out)
    if not match:
        return None
    return int(float(match.group(1)) * {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[match.group(2)])


def _system_logs(scope: Scope) -> dict[str, Any] | None:
    size = _journal_bytes()
    if size is None:
        return None
    extra = max(0, size - 200 * 1024 * 1024)
    return _category(id="system-logs", title="System activity logs", icon="output", action="command", bytes=size,
                     status="empty" if extra < 50 * 1024 * 1024 else "ok",
                     summary=f"{_human(size)} of Linux's own logs; about {_human(extra)} older than the newest 200 MB",
                     explanation="journald keeps every program's log messages. `journalctl --vacuum-size` trims the oldest.",
                     consequence=commands.SPECS["journal-vacuum"].consequence, recovery="Old entries are gone.",
                     reclaimable={"bytes": extra, "certainty": "estimate", "label": "beyond the newest 200 MB"},
                     commands=[_command(scope, "journal-vacuum")])


def _package_downloads(scope: Scope) -> dict[str, Any] | None:
    archives = Path("/var/cache/apt/archives")
    if not archives.is_dir():
        return None
    size = _tree_bytes(archives) or 0
    return _category(id="package-downloads", title="Saved Linux updates", icon="package", action="command", bytes=size,
                     status="empty" if size < 20 * 1024 * 1024 else "ok", summary=f"{_human(size)} of downloaded system packages",
                     explanation="apt keeps a copy of every system package it downloaded. `apt-get clean` removes them.",
                     consequence=commands.SPECS["apt-clean"].consequence, recovery="Downloaded again if needed.",
                     reclaimable={"bytes": size, "certainty": "measured"}, commands=[_command(scope, "apt-clean")])


def _pnpm_store(scope: Scope) -> dict[str, Any] | None:
    import shutil as _shutil
    if not _shutil.which("pnpm"):
        return None
    store = Path.home() / ".local" / "share" / "pnpm"
    size = _tree_bytes(store) if store.is_dir() else None
    if not size:
        return None
    return _category(id="pnpm-store", title="pnpm download store", icon="package", action="command", bytes=size,
                     summary=f"{_human(size)} shared JavaScript packages", status="empty" if size < 20 * 1024 * 1024 else "ok",
                     explanation="pnpm keeps one shared copy of every package. `pnpm store prune` removes ones no project uses.",
                     consequence=commands.SPECS["pnpm-prune"].consequence, recovery="Downloaded again if needed.",
                     commands=[_command(scope, "pnpm-prune")])


def _project_git(scope: Scope) -> dict[str, Any] | None:
    repos = commands.git_repos(scope)
    if not repos:
        return None
    rows = []
    for repo in repos:
        size = _tree_bytes(Path(repo) / ".git", 10)
        if size:
            rows.append({"repo": repo, "bytes": size})
    rows.sort(key=lambda row: row["bytes"], reverse=True)
    total = sum(row["bytes"] for row in rows)
    big = [row for row in rows if row["bytes"] >= 20 * 1024 * 1024]
    if not big:
        return None
    return _category(id="project-git", title="Project history (git)", icon="git-commit", action="command", bytes=total,
                     summary=f"{len(big)} code projects Hermes made, {_human(total)} of history", scope="projects",
                     explanation="Code projects keep their full history in a .git folder. `git gc` packs it tighter; nothing in the project changes.",
                     consequence=commands.SPECS["git-gc"].consequence, recovery="Nothing is removed.",
                     details={"repos": [{"repo": row["repo"], "bytes": row["bytes"], "worktrees": 0} for row in big]},
                     commands=[_command(scope, "git-gc")])


BUILDERS = {
    "session-db": _session_db, "your-files": _your_files, "webui-journals": _webui, "backups": _backups,
    "workspaces": _workspaces, "package-cache": _package_cache, "runtime-versions": _runtimes,
    "temporary": _temporary, "logs": _logs, "checkpoints": _checkpoints, "worktrees": _worktrees,
    "old-sessions": _old_sessions,
    "hermes-projects": _hermes_projects,
    "system-logs": _system_logs,
    "package-downloads": _package_downloads,
    "pnpm-store": _pnpm_store,
    "project-git": _project_git,
    **{category_id: (lambda scope, category_id=category_id: _project_category(scope, category_id)) for category_id in _PROJECT_CATEGORIES},
}


def _build(scope: Scope) -> list[dict[str, Any] | None]:
    return [builder(scope) for builder in BUILDERS.values()]


def verified_reclaimable(items: list[dict[str, Any]]) -> dict[str, Any]:
    sources = [{"categoryId": c["id"], "title": c["title"], "bytes": c["reclaimable"]["bytes"]}
               for c in items if c["reclaimable"].get("certainty") == "verified" and c["reclaimable"].get("bytes")]
    return {"bytes": sum(row["bytes"] for row in sources) if sources else None, "sources": sources}
