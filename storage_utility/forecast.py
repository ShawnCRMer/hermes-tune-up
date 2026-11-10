"""Drive-fill forecast: "at this rate the drive is full in about N days".

Samples how full the drive is, at most once an hour and only while Tune-up is being used
(no background service), keeps 60 days, and fits a straight line through the last 14 days.
Says nothing until there is at least a day of history, and never extrapolates a shrinking
or flat drive into a scary number.
"""
from __future__ import annotations

import time
from typing import Any

from . import state
from .scope import Scope

FILE = "capacity-history.json"
MIN_GAP = 3600
KEEP = 60 * 86400
WINDOW = 14 * 86400
MIN_SPAN = 86400


def record(scope: Scope, volume: dict[str, Any] | None) -> None:
    if not volume or volume.get("totalBytes") is None or volume.get("availableBytes") is None:
        return
    now = time.time()
    rows = state.read_json(scope, FILE, [])
    rows = [row for row in rows if isinstance(row, dict) and now - float(row.get("at", 0)) < KEEP] if isinstance(rows, list) else []
    if rows and now - float(rows[-1]["at"]) < MIN_GAP:
        return
    rows.append({"at": now, "free": int(volume["availableBytes"]), "total": int(volume["totalBytes"])})
    state.write_json(scope, FILE, rows)


def forecast(scope: Scope, volume: dict[str, Any] | None) -> dict[str, Any]:
    rows = state.read_json(scope, FILE, [])
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    now = time.time()
    recent = [row for row in rows if now - float(row["at"]) <= WINDOW]
    if len(recent) < 3 or float(recent[-1]["at"]) - float(recent[0]["at"]) < MIN_SPAN:
        have = (float(recent[-1]["at"]) - float(recent[0]["at"])) / 3600 if len(recent) > 1 else 0
        return {"status": "learning", "hoursNeeded": max(1, round(24 - have))}
    xs = [float(row["at"]) for row in recent]
    ys = [float(row["total"] - row["free"]) for row in recent]  # bytes used
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    spread = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / spread if spread else 0.0  # bytes per second
    per_day = slope * 86400
    free = float((volume or {}).get("availableBytes") or recent[-1]["free"])
    if per_day <= 50 * 1024 * 1024:  # under ~50 MB/day: effectively steady
        return {"status": "steady", "perDay": per_day}
    return {"status": "growing", "perDay": per_day, "days": round(free / per_day)}
