"""Cheap filesystem capacity reads; this module never traverses directories."""
from __future__ import annotations

import os
import platform
from pathlib import Path
import shutil
from datetime import datetime, timezone

from .contracts import response
from .scope import Scope


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_capacity(scope: Scope) -> dict:
    errors: list[dict[str, str]] = []
    try:
        usage = shutil.disk_usage(scope.home)
        total, free = int(usage.total), int(usage.free)
        try:
            stats = os.statvfs(scope.home)
            available = max(0, int(stats.f_bavail) * int(stats.f_frsize))
        except (AttributeError, OSError, ValueError):
            available = None
            errors.append({"code": "available_capacity_unknown", "message": "available capacity is unavailable"})
        status = "available" if available is not None else "partial"
    except (OSError, ValueError):
        status = "unknown"
        total = free = available = None
        errors.append({"code": "capacity_unavailable", "message": "filesystem capacity is unavailable"})
    return response(
        scope.binding,
        sampledAt=_timestamp(),
        volumes=[{
            "volumeId": scope.volume_id,
            "status": status,
            "totalBytes": total,
            "freeBytes": free,
            "availableBytes": available,
            "accounting": "filesystem",
        }],
        errors=errors,
    )


def _plugin_version() -> str | None:
    try:
        text = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip().strip("'\"") or None
    return None


def backend_identity(scope: Scope) -> dict[str, str | None]:
    """Which computer is being cleaned: the one Hermes runs on, never the viewing device."""
    import socket
    host = socket.gethostname() or ""
    return {"label": "Hermes serving backend", "os": platform.system() or os.name,
            "host": host.split(".")[0] or None, "version": _plugin_version()}
