"""Run the Hermes-projects scan in a separate low-priority process and keep its result."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import state
from .scope import Scope
from .childenv import minimal_env

LATEST = "projects-latest.json"
STALE_SECONDS = 6 * 3600
_TIMEOUT = 600
_PACKAGE_ROOT = str(Path(__file__).resolve().parents[1])
_BOOT = "import sys; sys.path.insert(0, sys.argv[1]); from storage_utility.projects import build; import json; print(json.dumps(build(sys.argv[2], sys.argv[3])))"
_RUNNING: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def user_home() -> str:
    return str(Path(os.environ.get("HOME") or Path.home()).absolute())


def latest(scope: Scope) -> dict[str, Any] | None:
    value = state.read_json(scope, LATEST, None)
    return value if isinstance(value, dict) and isinstance(value.get("findings"), dict) else None


def status(scope: Scope) -> dict[str, Any]:
    with _LOCK:
        running = _RUNNING.get(str(scope.home))
    saved = latest(scope)
    return {"running": bool(running and running.get("state") == "running"),
            "error": running.get("error") if running else None,
            "finishedAt": saved.get("finishedAt") if saved else None,
            "state": saved.get("state") if saved else None,
            "projects": len(saved.get("projects") or []) if saved else 0}


def _age(saved: dict[str, Any] | None) -> float:
    if not saved or not saved.get("finishedAt"):
        return float("inf")
    try:
        return time.time() - datetime.fromisoformat(saved["finishedAt"].replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("inf")


def start(scope: Scope, *, on_done: Callable[[], None] | None = None, only_if_stale: bool = False) -> bool:
    """Start a scan unless one is running (or, with only_if_stale, the last one is recent)."""
    key = str(scope.home)
    if only_if_stale and _age(latest(scope)) < STALE_SECONDS:
        return False
    with _LOCK:
        if _RUNNING.get(key, {}).get("state") == "running":
            return False
        job = {"state": "running", "startedAt": time.time(), "error": None}
        _RUNNING[key] = job

    def worker() -> None:
        try:
            done = subprocess.run([sys.executable, "-I", "-c", _BOOT, _PACKAGE_ROOT, str(scope.home), user_home()],
                                  capture_output=True, text=True, timeout=_TIMEOUT, stdin=subprocess.DEVNULL, env=minimal_env())
            result = json.loads(done.stdout) if done.returncode == 0 and done.stdout.strip() else None
            if not isinstance(result, dict):
                job["error"] = "the project scan did not finish"
                return
            state.write_json(scope, LATEST, result)
            job["state"] = "done"
        except (OSError, subprocess.SubprocessError, ValueError):
            job["error"] = "the project scan did not finish"
        finally:
            if job["state"] == "running":
                job["state"] = "failed"
            if on_done is not None:
                try:
                    on_done()
                except Exception:
                    pass

    threading.Thread(target=worker, name="storage-projects-scan", daemon=True).start()
    return True
