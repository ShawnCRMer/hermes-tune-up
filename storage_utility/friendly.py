"""Everyday-language layer: sorts cleanup into four groups anyone can act on.

green  - "Safe to clean": Hermes can rebuild or re-download it, or a copy stays.
         The Free up button deletes these right away (space back immediately).
yellow - "Take a quick look": probably not needed, but only the user knows.
         Removing goes to the Hermes Trash, which empties itself after 30 days.
pause  - "Needs a short pause": Hermes must stop for a few minutes.
locked - "Kept safe on purpose": items the safety checks hold back, with why.

The technical categories, safety checks and plans underneath are unchanged.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

from . import actions, cleanup, commands, state
from .contracts import error_body, response
from .fsutil import volume_free
from .scope import Scope

TRASH_DAYS = 30
_EXPIRY_CHECK = "trash-expiry.json"

# id: (tier, plain title, what it is, is it safe, if I remove it, can I undo)
COPY: dict[str, tuple[str, str, str, str, str, str]] = {
    "project-duplicates": ("green", "Extra copies of the same file",
        "Identical copies of files in folders Hermes made, like your book's save files.",
        "Yes. One copy of every file always stays.", "Nothing you'll notice.", "Not needed: a copy stays."),
    "tool-caches": ("green", "Download leftovers from Hermes's tools",
        "Installers that tools like npm and pip saved after downloading them once.",
        "Yes. They're copies of things on the internet.", "A tool may take a little longer the next time it installs something.",
        "Not needed: they download again by themselves."),
    "package-cache": ("green", "Hermes's saved Python downloads",
        "Python packages Hermes downloaded. Only ones nothing uses any more are removed.",
        "Yes. Anything still in use stays.", "Nothing you'll notice.", "Not needed: they download again by themselves."),
    "runtime-versions": ("green", "Old versions of Hermes's helper programs",
        "Older copies of tools left behind after Hermes updated. The current ones stay.",
        "Yes. Only versions nothing uses are removed.", "Going back to an older Hermes version may need a download.",
        "Not needed: they download again by themselves."),
    "logs": ("green", "Old diary files",
        "Notes Hermes writes about what it did. Only older, finished ones are removed; today's stay.",
        "Yes. Today's notes are kept.", "You can't read those old notes any more.", "No, but they're only old notes."),
    "system-logs": ("green", "Old system activity logs",
        "The computer's own diary of what every program did. The newest 200 MB is kept.",
        "Yes. Only the oldest entries go.", "Very old entries can't be looked at for troubleshooting.", "No, but they're only old log entries."),
    "package-downloads": ("green", "Saved Linux updates",
        "Copies of system updates that are already installed.",
        "Yes. The updates themselves stay installed.", "Nothing you'll notice.", "Not needed: they download again if ever needed."),
    "pnpm-store": ("green", "Saved JavaScript downloads",
        "A shared store of JavaScript packages. Only ones no project uses are removed.",
        "Yes. Anything a project uses stays.", "Nothing you'll notice.", "Not needed: they download again by themselves."),
    "project-git": ("green", "Pack project history",
        "Code projects Hermes made keep their full history. Git can pack it tighter.",
        "Yes. Every file, version and branch stays; it's only packed tighter.", "Nothing you'll notice.", "Nothing is removed."),
    "webui-journals": ("yellow", "Old web-chat activity records",
        "Step-by-step records from the separate web chat app.",
        "Probably. Your chats themselves aren't affected.", "The web chat can't replay those old runs.", "Yes, for 30 days."),
    "backups": ("yellow", "Backups Hermes made before changes",
        "Copies Hermes saved before risky changes, in case something went wrong.",
        "Probably, if you're happy with how things are now.", "You can't go back to those older versions.", "Yes, for 30 days."),
    "project-backups": ("yellow", "Old backups in your projects",
        "\"Before I changed it\" snapshots saved in folders Hermes made, more than a week old.",
        "Probably. The project itself isn't touched.", "You can't roll back to those snapshots.", "Yes, for 30 days."),
    "project-dependencies": ("yellow", "Parts of old projects that can be re-installed",
        "Building blocks (like node_modules) for projects nobody has used in a while.",
        "Probably. They can be installed again.", "That project needs its install step run once before it works again.", "Yes, for 30 days."),
    "project-large-old": ("yellow", "Big files nobody has opened in a month",
        "Large files in folders Hermes made. Big doesn't mean junk, so check the names.",
        "Maybe. Only you know what these are.", "That file is gone from its project.", "Yes, for 30 days."),
    "temporary": ("yellow", "Old results from Hermes's tools",
        "Saved output from tools and helpers that hasn't been looked at in a week or more.",
        "Probably.", "Older chats can't show those saved results any more.", "Yes, for 30 days."),
    "workspaces": ("yellow", "Hermes work folders",
        "Folders made for agent and board work. They can contain real project files, so look inside first.",
        "Only if you're done with that project.", "Everything in the folder goes.", "Yes, for 30 days."),
    "your-files": ("yellow", "Your attachments and files Hermes made",
        "Files you sent in chats, and files Hermes created for you. These are yours, so look carefully.",
        "Only the ones you don't want.", "Chats that showed that file will say it's missing.", "Yes, for 30 days."),
    "worktrees": ("yellow", "Spare copies of code projects",
        "Extra working copies Hermes makes so agents don't touch your main copy. Hermes's own cleanup keeps any with unsaved work.",
        "Yes. Unsaved work is always kept.", "Those spare copies are gone; the main project stays.", "Work Hermes can't prove is saved is kept."),
    "checkpoints": ("yellow", "Undo history for file edits",
        "Snapshots Hermes takes before editing files, so you can undo its changes.",
        "Probably, for old projects.", "You can't undo those older edits any more.", "No."),
    "old-sessions": ("yellow", "Old conversations",
        "Finished chats you haven't opened in months. Pinned and archived chats are always kept.",
        "Only if you don't need them. Chats are small, so this saves little.", "Those chats are gone for good.", "No."),
    "session-db": ("pause", "Squeeze empty space out of the chat history file",
        "Hermes keeps every conversation in one file (state.db). When old chats or search data are removed, the file doesn't get smaller by itself: it keeps the empty room inside. Packing it squeezes that room out.",
        "Yes. Nothing is deleted; every conversation stays. Tune-up pauses Hermes, packs the file with Hermes's own command, checks every conversation is still there, and always starts Hermes again, even if something goes wrong.",
        "Hermes is offline for a few minutes: chats, agents, bots and this screen disconnect and come back by themselves.", "Nothing is removed."),
}

GREEN_COMMANDS = {"package-cache": "uv-cache-prune", "runtime-versions": "pm-gc", "system-logs": "journal-vacuum",
                  "package-downloads": "apt-clean", "pnpm-store": "pnpm-prune", "project-git": "git-gc"}


def _plain_reason(note: str | None) -> str:
    text = (note or "").lower()
    if "can't check whether programs" in text:
        return "Storage can't check whether programs on this computer are using it."
    if "runs as" in text or ".service" in text or "systemd" in text or "cron" in text:
        return "An app that's running on this computer uses it."
    if "running program" in text or "in use" in text or "last hour" in text:
        return "Something is using it right now."
    if "git" in text:
        return "It's part of a saved code project."
    if "referenced" in text or "mentioned" in text or "points" in text or "refers" in text:
        return "Another file still points to it."
    if "needed by" in text or "playwright" in text or "registry" in text:
        return "Hermes's browser or another app still needs it."
    if "last 7 days" in text or "recent" in text or "used in the last" in text or "active" in text:
        return "It was used recently."
    if "changed" in text or "gone" in text:
        return "It changed since the last check; Storage will look again."
    if "too large" in text:
        return "Too big to check completely right now."
    return note or "Storage couldn't confirm it's safe."


_GENERIC_PARENTS = {"", "workspace", "workspaces", "projects", "project", "code", "src", "repos", "home", "root"}


def project_label(item: dict[str, Any], category_id: str = "") -> str:
    """Everyday name for where an item lives: the project folder Hermes made, or Hermes's own folder."""
    root = item.get("root")
    if not root:
        if category_id == "workspaces":
            return item.get("name") or "Hermes work folder"
        return "Hermes's own folder"
    path = str(root).rstrip("/")
    home = os.path.expanduser("~").rstrip("/")
    if path in (home, "/root"):
        return "your tool downloads"
    parts = path.split("/")
    last, parent = parts[-1], (parts[-2] if len(parts) > 1 else "")
    if parent in _GENERIC_PARENTS or path.rsplit("/", 1)[0] in (home, "/root"):
        return last
    return f"{parent}/{last}"


def _projects(category: dict[str, Any], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        name = project_label(item, category["id"])
        row = grouped.setdefault(name, {"name": name, "bytes": 0, "count": 0})
        row["bytes"] += int(item.get("allocatedBytes") or 0)
        row["count"] += 1
    return sorted(grouped.values(), key=lambda row: row["bytes"], reverse=True)


def _row(category: dict[str, Any]) -> dict[str, Any]:
    tier, title, what, safe, removed, undo = COPY[category["id"]]
    items = category.get("items") or []
    selectable = [item for item in items if item.get("selectable")]
    row: dict[str, Any] = {
        "id": category["id"], "tier": tier, "title": title, "what": what,
        "answers": {"what": what, "safe": safe, "removed": removed, "undo": undo},
        "technical": {"title": category.get("title"), "summary": category.get("summary"),
                      "commands": [command["display"] for command in category.get("commands") or []]},
        "kind": category.get("action"), "external": category.get("scope") == "projects",
        "count": len(selectable), "bytes": None, "sizeKnown": True,
        "examples": [(item.get("display") or item.get("path") or "").rsplit("/", 1)[-1] for item in selectable[:3]],
        "projects": _projects(category, selectable) if category.get("action") == "review" else [],
    }
    if category.get("action") == "review":
        row["bytes"] = sum(int(item.get("allocatedBytes") or 0) for item in selectable)
    elif category["id"] == "session-db":
        row["bytes"] = (category.get("reclaimable") or {}).get("bytes")
        row["sizeKnown"] = row["bytes"] is not None
        row["guidance"] = category.get("guidance")
    elif category["id"] in GREEN_COMMANDS:
        # Only the command knows what it can remove: show a measured estimate when there is one, else "varies".
        estimate = (category.get("reclaimable") or {}).get("bytes")
        row["bytes"] = estimate if estimate else None
        row["sizeKnown"] = bool(estimate)
        if category["id"] == "project-git":
            row["projects"] = [{"name": project_label({"root": repo["repo"]}), "bytes": repo["bytes"], "count": 1}
                               for repo in (category.get("details") or {}).get("repos", [])]
        command = next((command for command in category.get("commands") or [] if command["commandId"] == GREEN_COMMANDS[category["id"]]), None)
        row["available"] = bool(command and command.get("available"))
        row["count"] = 1 if row["available"] else 0
    else:
        row["bytes"] = category.get("bytes")
        row["sizeKnown"] = row["bytes"] is not None
    return row


def _locked(groups: list[dict[str, Any]]) -> dict[str, Any]:
    held = []
    for category in groups:
        if category["id"] not in COPY or category.get("action") != "review":
            continue
        for item in category.get("items") or []:
            if item.get("selectable"):
                continue
            held.append({"name": (item.get("display") or item.get("path") or "").replace("/root/", "~/"),
                         "bytes": int(item.get("allocatedBytes") or 0), "why": _plain_reason(item.get("note")),
                         "detail": item.get("note"), "group": COPY[category["id"]][1],
                         "project": project_label(item, category["id"])})
    held.sort(key=lambda row: row["bytes"], reverse=True)
    return {"bytes": sum(row["bytes"] for row in held), "count": len(held), "items": held[:12]}


def home(scope: Scope, groups: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [_row(category) for category in groups if category["id"] in COPY and category.get("status") != "empty"]
    tiers: dict[str, list[dict[str, Any]]] = {"green": [], "yellow": [], "pause": []}
    for row in rows:
        if row["tier"] == "pause" and not row.get("guidance"):
            continue  # nothing worth packing right now
        if row["tier"] != "pause" and not row["count"]:
            continue
        tiers[row["tier"]].append(row)
    for tier in tiers.values():
        tier.sort(key=lambda row: row["bytes"] or 0, reverse=True)
    totals = {name: sum(row["bytes"] or 0 for row in tier) for name, tier in tiers.items()}
    notices = []
    projects = next((category for category in groups if category["id"] == "hermes-projects"), None)
    blind = ((projects or {}).get("details") or {}).get("blind")
    if blind:
        notices.append(f"Folders Hermes made outside its own folder are only listed, not offered for cleanup: {blind}. "
                       "Everything inside the Hermes folder can still be cleaned.")
    return {"tiers": tiers, "totals": totals, "locked": _locked(groups), "trashDays": TRASH_DAYS, "notices": notices,
            "empty": not any(tiers.values())}


# ----- Free up: one reviewed plan per green group, deleted right away ------------------

def freeup_plan(scope: Scope, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    wanted = body.get("categoryIds")
    if not isinstance(wanted, list) or not wanted or not all(isinstance(value, str) for value in wanted):
        return 400, error_body("invalid_selection", "choose at least one thing to clean", retryable=False, binding=scope.binding)
    plans, command_rows, skipped = [], [], []
    for category_id in dict.fromkeys(wanted):
        if COPY.get(category_id, ("",))[0] != "green":
            return 400, error_body("invalid_selection", "only safe-to-clean groups can be freed in one step", retryable=False, binding=scope.binding)
        category = cleanup.find(scope, category_id, fresh=True)
        if category is None:
            skipped.append(category_id)
            continue
        if category_id in GREEN_COMMANDS:
            command = next((row for row in category.get("commands") or [] if row["commandId"] == GREEN_COMMANDS[category_id]), None)
            if command and command.get("available") and not command.get("needsPreview"):
                choices = command.get("params") or {}
                runs = [{"repo": repo} for repo in choices.get("repo", [])] if "repo" in choices else [{}]
                for params in runs:
                    command_rows.append({"categoryId": category_id, "commandId": command["commandId"], "params": params,
                                         "title": COPY[category_id][1] + (f" — {project_label({'root': params['repo']})}" if params.get("repo") else ""),
                                         "display": command["display"]})
            else:
                skipped.append(category_id)
            continue
        ids = [item["itemId"] for item in category.get("items") or [] if item.get("selectable")][: actions.MAX_ITEMS_PER_PLAN]
        if not ids:
            skipped.append(category_id)
            continue
        status, plan = actions.create_plan(scope, category, {"itemIds": ids, "operation": "permanent"})
        if status != 200:
            skipped.append(category_id)
            continue
        plans.append({"categoryId": category_id, "title": COPY[category_id][1], "planId": plan["planId"], "digest": plan["digest"],
                      "totalBytes": plan["totalBytes"], "count": len(plan["items"]),
                      "examples": [row["path"].replace("/root/", "~/") for row in plan["items"][:4]]})
    if not plans and not command_rows:
        return 409, error_body("nothing_to_free", "nothing in the selected groups can be cleaned right now", retryable=True, binding=scope.binding)
    return 200, response(scope.binding, plans=plans, commands=command_rows, skipped=skipped,
                         totalBytes=sum(plan["totalBytes"] for plan in plans))


def freeup_apply(scope: Scope, body: dict[str, Any], on_finish) -> tuple[int, dict[str, Any]]:
    if (body.get("confirmation") or {}).get("acknowledged") is not True:
        return 409, error_body("confirmation_required", "confirm first", retryable=False, binding=scope.binding)
    key = body.get("idempotencyKey")
    if not isinstance(key, str) or not key or len(key) > 100:
        return 400, error_body("invalid_idempotency_key", "an idempotency key is required", retryable=False, binding=scope.binding)
    raw_plans = body.get("plans") or []
    raw_commands = body.get("commands") or []
    if not isinstance(raw_plans, list) or not isinstance(raw_commands, list) or len(raw_plans) > 10 or len(raw_commands) > 40:
        return 400, error_body("invalid_request", "unexpected selection", retryable=False, binding=scope.binding)
    free_before = volume_free(scope)
    results, handled, refused = [], 0, 0
    for row in raw_plans:
        if not isinstance(row, dict):
            continue
        status, result = actions.apply_plan(scope, str(row.get("planId")), {
            "digest": row.get("digest"), "confirmation": {"operation": "permanent", "acknowledged": True},
            "idempotencyKey": f"{key}:{row.get('planId')}"})
        if status != 200:
            results.append({"planId": row.get("planId"), "error": (result.get("error") or {}).get("message", "could not clean")})
            continue
        handled += int(result.get("bytesHandled") or 0)
        refused += int(result.get("refusedCount") or 0)
        results.append({"planId": row.get("planId"), "applied": result.get("appliedCount"), "refused": result.get("refusedCount"),
                        "bytes": result.get("bytesHandled")})
    allowed = set(GREEN_COMMANDS.values())
    queue = []
    for value in raw_commands:
        entry = value if isinstance(value, dict) else {"commandId": value, "params": {}}
        if entry.get("commandId") in allowed and isinstance(entry.get("params") or {}, dict):
            queue.append((entry["commandId"], entry.get("params") or {}))  # params re-validated by commands.start
    job = _chain(scope, queue, on_finish) if queue else None
    free_after = volume_free(scope)
    return 200, response(scope.binding, results=results, bytesHandled=handled, refusedCount=refused,
                         volumeFreeDelta=(free_after - free_before) if free_before is not None and free_after is not None else None,
                         freeAfter=free_after, commandJob=job)


def _chain(scope: Scope, queue: list[tuple[str, dict]], on_finish) -> dict[str, Any] | None:
    """Run the green owner commands one after another (only one may run at a time).
    A command that can't start (e.g. its project changed) is skipped, never retried blindly."""
    while queue:
        (command_id, params), rest = queue[0], queue[1:]

        def finished(rest=rest) -> None:
            if on_finish:
                on_finish()
            if rest:
                _chain(scope, rest, on_finish)

        status, job = commands.start(scope, command_id, params, None, finished)
        if status == 200:
            return job
        queue = rest
    return None


# ----- Trash empties itself after 30 days, whenever Storage is opened ---------------------

def expire_trash(scope: Scope) -> dict[str, Any] | None:
    marker = state.read_json(scope, _EXPIRY_CHECK, {})
    if isinstance(marker, dict) and time.time() - float(marker.get("at") or 0) < 3600:
        return None
    state.write_json(scope, _EXPIRY_CHECK, {"at": time.time()})
    listing = actions.list_trash(scope)
    cutoff = time.time() - TRASH_DAYS * 86400
    old = []
    for row in listing.get("items") or []:
        try:
            moved = datetime.fromisoformat(str(row.get("trashedAt")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if moved.tzinfo is None:
            moved = moved.replace(tzinfo=timezone.utc)
        if row.get("status") == "present" and moved.timestamp() < cutoff:
            old.append(row["receiptId"])
    if not old:
        return None
    status, result = actions.remove_from_trash(scope, {"receiptIds": old[:200], "confirmation": {"acknowledged": True}})
    return result if status == 200 else None
