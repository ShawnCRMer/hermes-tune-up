"""Saved scan snapshots, daily history for growth, and Explorer views."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from . import state
from .knowledge import describe
from .scope import Scope

LATEST = "snapshot-latest.json"
HISTORY = "history.json"
HISTORY_DAYS = 120
MIN_BASELINE_AGE = 20 * 3600

# Explorer paths that have a matching Cleanup category.
_CATEGORY_PREFIXES = (
    ("state.db", "session-db"), ("attachments", "your-files"), ("outputs", "your-files"), ("images", "your-files"),
    ("webui/sessions/_run_journal", "webui-journals"), ("backups", "backups"), ("state-snapshots", "backups"),
    ("workspaces", "workspaces"), ("cache/uv", "package-cache"), ("installs", "runtime-versions"),
    ("tools", "runtime-versions"), ("logs", "logs"), ("cache", "temporary"), ("checkpoints", "checkpoints"),
)


def category_for(path: str) -> str | None:
    for prefix, category in _CATEGORY_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return category
    return None


def _epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@state.serialized
def save(scope: Scope, result: dict[str, Any]) -> bool:
    """Persist a finished scan once; returns True when it became the latest."""
    if result.get("state") not in {"complete", "partial"} or not isinstance(result.get("tree"), list):
        return False
    current = state.read_json(scope, LATEST, None)
    if isinstance(current, dict) and current.get("snapshotId") == result.get("snapshotId"):
        return False
    snapshot = {key: result.get(key) for key in (
        "snapshotId", "state", "startedAt", "finishedAt", "elapsedMs", "totals", "coverage", "privacy", "errors",
        "tree", "largest", "topLevel")}
    state.write_json(scope, LATEST, snapshot)
    history = state.read_json(scope, HISTORY, [])
    if not isinstance(history, list):
        history = []
    day = (result.get("finishedAt") or "")[:10]
    entry = {"day": day, "finishedAt": result.get("finishedAt"), "complete": bool((result.get("coverage") or {}).get("complete")),
             "allocatedBytes": (result.get("totals") or {}).get("allocatedBytes"), "topLevel": result.get("topLevel") or {}}
    # One entry per day (the latest complete scan of that day wins).
    history = [row for row in history if isinstance(row, dict) and not (row.get("day") == day and (entry["complete"] or not row.get("complete")))]
    if entry["complete"] or not any(row.get("day") == day for row in history):
        history.append(entry)
    history.sort(key=lambda row: row.get("finishedAt") or "")
    state.write_json(scope, HISTORY, history[-HISTORY_DAYS:])
    return True


def latest(scope: Scope) -> dict[str, Any] | None:
    value = state.read_json(scope, LATEST, None)
    return value if isinstance(value, dict) and isinstance(value.get("tree"), list) else None


def growth(scope: Scope, snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not snapshot or not (snapshot.get("coverage") or {}).get("complete"):
        return {"status": "unavailable", "reason": "needs a complete scan"}
    history = state.read_json(scope, HISTORY, [])
    now = _epoch(snapshot.get("finishedAt")) or time.time()
    candidates = [row for row in history if isinstance(row, dict) and row.get("complete")
                  and (_epoch(row.get("finishedAt")) or now) <= now - MIN_BASELINE_AGE]
    if not candidates:
        stamps = [_epoch(row.get("finishedAt")) for row in history if isinstance(row, dict) and row.get("complete")]
        first = min((stamp for stamp in stamps if stamp is not None), default=None)
        ready_at = datetime.fromtimestamp((first or now) + MIN_BASELINE_AGE, timezone.utc).isoformat().replace("+00:00", "Z")
        return {"status": "baseline", "reason": "growth baseline not yet established", "readyAt": ready_at}
    target = now - 7 * 86400
    baseline = min(candidates, key=lambda row: abs((_epoch(row.get("finishedAt")) or 0) - target))
    current_top = snapshot.get("topLevel") or {}
    old_top = baseline.get("topLevel") or {}
    changes = [{"path": name, "deltaBytes": int(current_top.get(name, 0)) - int(old_top.get(name, 0))}
               for name in set(current_top) | set(old_top)]
    changes = [row for row in changes if row["deltaBytes"]]
    changes.sort(key=lambda row: abs(row["deltaBytes"]), reverse=True)
    return {"status": "comparable", "since": baseline.get("finishedAt"),
            "deltaBytes": int((snapshot.get("totals") or {}).get("allocatedBytes") or 0) - int(baseline.get("allocatedBytes") or 0),
            "changes": changes[:6]}


def explorer(snapshot: dict[str, Any], path: str) -> dict[str, Any] | None:
    nodes = {node["path"]: node for node in snapshot.get("tree", [])}
    node = nodes.get(path)
    if node is None:
        return None
    children = sorted((child for child in nodes.values() if child.get("parent") == path), key=lambda item: item["allocatedBytes"], reverse=True)
    files = [item for item in snapshot.get("largest", []) if item.get("parent") == path]
    files.sort(key=lambda item: item["allocatedBytes"], reverse=True)
    shown = sum(child["allocatedBytes"] for child in children) + sum(item["allocatedBytes"] for item in files)
    crumbs, cursor = [], path
    while cursor is not None:
        current = nodes.get(cursor)
        crumbs.append({"path": cursor, "name": current["name"] if current else cursor})
        cursor = current.get("parent") if current else None
    def decorate(row: dict[str, Any], kind: str) -> dict[str, Any]:
        return {**row, "kind": kind, "categoryId": category_for(row["path"]), "description": row.get("description") or describe(row["path"])}
    return {
        "node": decorate(node, "dir"),
        "breadcrumbs": list(reversed(crumbs)),
        "children": [decorate(child, "dir") for child in children],
        "files": [decorate(item, "file") for item in files],
        "otherBytes": max(0, int(node["allocatedBytes"]) - shown),
    }
