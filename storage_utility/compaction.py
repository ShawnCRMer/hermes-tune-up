"""Plan and launch the packing job for state.db (see compact_job.py), and report its result.

Only services PROVEN to serve this Hermes home are ever paused:
  * a service whose process or settings say HERMES_HOME=<this home>, or that holds this state.db;
  * a watcher/timer whose triggered unit serves this home or restarts one of those services.
Anything else holding the file (a program not run as a service) blocks the job up front.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import commands, state
from .contracts import error_body, response
from .scope import Scope

STATUS = "compact-status.json"
PLAN = "compact-plan.json"
UNIT_PREFIX = "hermes-storage-compact"
THROUGHPUT_BYTES_PER_SECOND = 40 * 1024 * 1024  # conservative; measured much faster on test copies


def _show(unit: str, *props: str) -> dict[str, str]:
    try:
        out = subprocess.run(["systemctl", "show", unit, *[f"-p{prop}" for prop in props]],
                             capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    values = {}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        values[key] = value
    return values


def _env_home(text: str) -> str | None:
    match = re.search(r"(?:^|\s)HERMES_HOME=(\S+)", text)
    return match.group(1).strip("'\"") if match else None


def _same(path: str | None, home: Path) -> bool:
    if not path:
        return False
    try:
        return os.path.realpath(path) == os.path.realpath(home)
    except OSError:
        return False


def _service_home(unit: str) -> str | None:
    """HERMES_HOME of a running service: its process first, then its settings and env files."""
    props = _show(unit, "MainPID", "Environment", "EnvironmentFiles", "ExecStart")
    pid = props.get("MainPID", "0")
    if pid.isdigit() and pid != "0":
        try:
            for entry in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
                if entry.startswith(b"HERMES_HOME="):
                    return entry.split(b"=", 1)[1].decode(errors="replace")
        except OSError:
            pass
    found = _env_home(props.get("Environment", ""))
    if found:
        return found
    for raw in re.findall(r"(\S+) \(ignore_errors=", props.get("EnvironmentFiles", "")):
        try:
            text = Path(raw).read_text(errors="replace")[:65536]
        except OSError:
            continue
        found = _env_home(text.replace("\n", " "))
        if found:
            return found
    return None


def _script_mentions(unit: str, home: Path) -> bool:
    """Does the unit's start script (or an env file it loads with `. file` / `source file`) name this home?"""
    exec_start = _show(unit, "ExecStart").get("ExecStart", "")
    match = re.search(r"path=(\S+)", exec_start)
    if not match:
        return False
    texts = []
    try:
        script = Path(match.group(1))
        if script.is_file() and script.stat().st_size < 262144:
            text = script.read_text(errors="replace")
            texts.append(text)
            for sourced in re.findall(r"(?:^|[;\s])(?:\.|source)\s+(/[^\s;]+)", text, flags=re.M):
                candidate = Path(sourced)
                if candidate.is_file() and candidate.stat().st_size < 65536:
                    texts.append(candidate.read_text(errors="replace"))
    except OSError:
        return False
    target = os.path.realpath(home)
    for text in texts:
        for found in re.findall(r"HERMES_HOME=[\"']?([^\s\"']+)", text):
            if _same(found, home):
                return True
    return False


def _active(unit: str) -> bool:
    try:
        return subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10).stdout.strip() in {"active", "activating", "reloading"}
    except (OSError, subprocess.TimeoutExpired):
        return True  # unknown: assume busy, never interrupt


def _units(pattern: str) -> list[tuple[str, str]]:
    try:
        out = subprocess.run(["systemctl", "list-units", "--all", "--plain", "--no-legend", pattern],
                             capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].endswith((".service", ".path", ".timer")):
            rows.append((parts[0], parts[2]))
    return rows


def _cgroup_unit(pid: int) -> str | None:
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return None
    match = re.search(r"/([^/\n]+\.service)", text)
    return match.group(1) if match else None


def select_units(home: Path, *, pattern: str = "hermes*") -> dict[str, Any]:
    from .compact_job import holders
    db = str(home / "state.db")
    running = [(name, active) for name, active in _units(pattern) if active in {"active", "activating", "reloading"}]
    holding = holders(db)
    holder_units = {row["pid"]: _cgroup_unit(row["pid"]) for row in holding}
    services = []
    for name, _ in running:
        if not name.endswith(".service") or name.startswith(UNIT_PREFIX):
            continue
        # Only a service PROVEN to serve this home. Holding the file is not proof: a test or second
        # Hermes can run inside another service's process group. Such holders block the job instead.
        if _same(_service_home(name), home):
            services.append(name)
    watchers, actions = [], []
    for name, _ in running:
        if not name.endswith((".path", ".timer")):
            continue
        triggered = _show(name, "Triggers").get("Triggers", "").split()
        for target in triggered:
            text = json.dumps(_show(target, "ExecStart", "ExecStartPre", "Environment"))
            if _same(_service_home(target), home) or any(service in text for service in services) or _script_mentions(target, home):
                watchers.append(name)
                # Also cancel anything the watcher already queued (e.g. "restart the web chat" in 5s).
                actions.extend(unit for unit in triggered if unit not in services and unit not in actions)
                break
    loose = [row for row in holding if holder_units.get(row["pid"]) not in services]
    return {"stopOrder": watchers + actions + services, "startOrder": list(reversed(services)) + watchers, "actions": actions,
            "services": services, "watchers": watchers, "looseHolders": loose}


def _facts(db: Path) -> dict[str, Any] | None:
    """Read-only, in a subprocess: closing an fd to the live DB in-process would drop our own SQLite locks."""
    code = ("import json, sys; sys.path.insert(0, sys.argv[2]); from storage_utility.compact_job import db_facts; "
            "print(json.dumps(db_facts(sys.argv[1])))")
    try:
        out = subprocess.run([sys.executable, "-c", code, str(db), str(Path(__file__).resolve().parents[1])],
                             capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _availability(home: Path) -> str | None:
    if not sys.platform.startswith("linux") or not shutil.which("systemd-run") or not os.path.isdir("/run/systemd/system"):
        return "This computer doesn't run Hermes as background services, so Storage can't pause it safely. Use the steps instead."
    if os.geteuid() != 0:
        return "Hermes isn't running as an administrator here, so Storage can't pause its services. Use the steps instead."
    if commands.hermes_argv() is None:
        return "Storage can't find the Hermes command on this computer."
    return None


def plan(scope: Scope, *, pattern: str = "hermes*") -> dict[str, Any]:
    home = scope.home
    db = home / "state.db"
    blocked = _availability(home)
    units = select_units(home, pattern=pattern) if blocked is None else {"stopOrder": [], "startOrder": [], "services": [], "watchers": [], "looseHolders": []}
    facts = _facts(db) if db.is_file() else None
    if blocked is None and units["looseHolders"]:
        names = ", ".join(f"{row['command'][:60]} (PID {row['pid']})" for row in units["looseHolders"][:3])
        blocked = f"A program that isn't a Hermes service has the file open: {names}. Close it first, or use the steps."
    busy = [name for name in units.get("actions", []) if _active(name)]
    if blocked is None and busy:
        blocked = f"{', '.join(_describe(name)['label'] for name in busy).capitalize()} is running right now. Try again when it has finished."
    if blocked is None and not units["services"]:
        blocked = "Storage couldn't find the Hermes services for this Hermes, so it can't pause them safely. Use the steps instead."
    estimate = None
    if facts:
        estimate = max(1, round((facts["liveBytes"] / THROUGHPUT_BYTES_PER_SECOND + 60) / 60))
    free = None
    try:
        info = os.statvfs(home)
        free = info.f_bavail * info.f_frsize
    except OSError:
        pass
    need = facts["liveBytes"] * 2 + 256 * 1024 * 1024 if facts else None
    if blocked is None and need is not None and free is not None and free < need:
        blocked = "There isn't enough free disk space to pack safely right now. Free up some space first."
    return {"available": blocked is None, "reason": blocked, "emptyBytes": facts["emptyBytes"] if facts else None,
            "liveBytes": facts["liveBytes"] if facts else None, "estimateMinutes": estimate,
            "pauses": [_describe(name) for name in units["watchers"] + units["services"]], **units, "last": status(scope)}


def _describe(unit: str) -> dict[str, str]:
    known = (("auto-restart", "the update watcher"), ("backup", "the nightly backup"), ("gateway", "Hermes itself (chats, agents, bots, scheduled jobs)"),
             ("dashboard", "this screen and the desktop/phone apps"), ("webui", "the web chat"))
    label = next((text for key, text in known if key in unit), unit)
    return {"unit": unit, "label": label}


def launch(scope: Scope, body: dict[str, Any], *, pattern: str = "hermes*", unit_prefix: str = UNIT_PREFIX,
           optimize_argv: list[str] | None = None, extra: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    if (body.get("confirmation") or {}).get("acknowledged") is not True:
        return 409, error_body("confirmation_required", "confirm first", retryable=False, binding=scope.binding)
    current = status(scope)
    if current and current.get("state") == "running":
        return 409, error_body("busy", "packing is already running", retryable=True, binding=scope.binding)
    ready = plan(scope, pattern=pattern)
    if not ready["available"]:
        return 409, error_body("unavailable", ready["reason"], retryable=False, binding=scope.binding)
    reviewed = body.get("units")
    if reviewed is not None and reviewed != ready["stopOrder"]:
        return 409, error_body("plan_changed", "what needs pausing changed since you looked; review again", retryable=False, binding=scope.binding)
    job_id = secrets.token_hex(6)
    status_file = scope.home / "storage-utility" / STATUS
    plan_file = scope.home / "storage-utility" / PLAN
    record = {"jobId": job_id, "db": str(scope.home / "state.db"), "hermesHome": str(scope.home),
              "stopOrder": ready["stopOrder"], "startOrder": ready["startOrder"],
              "optimizeArgv": optimize_argv or [*(commands.hermes_argv() or []), "sessions", "optimize"],
              "statusFile": str(status_file), "timeoutSeconds": 7200, **(extra or {})}
    state.write_json(scope, PLAN, record)
    state.write_json(scope, STATUS, {"state": "running", "jobId": job_id, "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                     "steps": [], "phase": "Starting"})
    job = str(Path(__file__).with_name("compact_job.py"))
    unit = f"{unit_prefix}-{job_id}"
    argv = ["systemd-run", f"--unit={unit}", "--collect", "--quiet",
            f"--description=Storage: pack Hermes chat history ({scope.home})",
            f"--property=ExecStopPost={sys.executable} {job} --restore {plan_file}",
            sys.executable, job, str(plan_file)]
    try:
        started = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        state.write_json(scope, STATUS, {"state": "failed", "jobId": job_id, "steps": [{"text": f"Could not start: {error}"}]})
        return 503, error_body("unavailable", "could not start the packing job", retryable=True, binding=scope.binding)
    if started.returncode != 0:
        state.write_json(scope, STATUS, {"state": "failed", "jobId": job_id, "steps": [{"text": started.stderr.strip()[:300]}]})
        return 503, error_body("unavailable", "could not start the packing job", retryable=True, binding=scope.binding)
    return 200, response(scope.binding, jobId=job_id, unit=unit, estimateMinutes=ready["estimateMinutes"], pauses=ready["pauses"])


def status(scope: Scope) -> dict[str, Any] | None:
    value = state.read_json(scope, STATUS, None)
    return value if isinstance(value, dict) else None
