"""Health checkup on macOS: the same cards as Linux, from launchd and Hermes's own records.

Services are the user's Hermes LaunchAgents (see launchd.py). Leftovers come from Hermes's own
background-process registry (``processes.json``): a process Hermes started for a chat that is
still running, at least MIN_LEFTOVER_AGE old, and no longer a descendant of any Hermes process
(the Hermes that started it exited or restarted). Identity is the PID plus the start time Hermes
recorded, re-checked right before any signal, so a reused PID is never touched. Plus Hermes
copies from the spawn ledger whose Hermes folder no longer exists. Nothing reads another
process's environment.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from . import compaction, launchd, state
from .contracts import error_body, response
from .hostos import ancestry, listening_ports, process_start, psutil_module
from .scope import Scope

_MAX_RECORD_BYTES = 4 * 1024 * 1024


def _health():
    from . import health
    return health


def _read_record(scope: Scope, name: str) -> list[dict[str, Any]]:
    """A Hermes JSON list in the home (processes.json / spawn-ledger.json); bounded, no symlinks."""
    try:
        fd = os.open(str(scope.home / name), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return []
    try:
        info = os.fstat(fd)
        if (info.st_mode & 0o170000) != 0o100000 or info.st_size > _MAX_RECORD_BYTES:
            return []
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = json.loads(handle.read(_MAX_RECORD_BYTES + 1) or b"[]")
    except (OSError, ValueError):
        return []
    finally:
        os.close(fd)
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


STABLE_SECONDS = 120


def _crash_looping(info: dict[str, Any], restarts: int) -> bool:
    """Many launches, a failing last exit, and not up for long right now. launchd's ``runs`` also
    counts manual restarts, so a service that is currently staying up is never called a crash loop."""
    if restarts < _health().CRASH_LOOP_RESTARTS or info.get("lastExit") in (None, 0):
        return False
    if not launchd.running(info):
        return True
    psutil = psutil_module()
    try:
        return psutil is not None and time.time() - psutil.Process(int(info["pid"])).create_time() < STABLE_SECONDS
    except Exception:
        return True


def _belongs(job: dict[str, Any], home: Path) -> bool:
    """Proof the job serves this home: HERMES_HOME set to it, or Hermes's own ``ai.hermes.*`` label
    with no HERMES_HOME (Hermes then uses ~/.hermes). Mirrors Linux, which never infers it alone."""
    if job.get("hermesHome"):
        return compaction._same(job["hermesHome"], home)
    return job.get("label", "").startswith(launchd.HERMES_PREFIX) and compaction._same(launchd.service_home(job), home)


# Hermes's own list of spawn purposes that are Hermes backends (hermes_cli/process_identity.py).
_LEDGER_PURPOSES = frozenset({"serve", "dashboard", "gateway", "mcp-helper"})


def _attached(pid: int, hermes_pids: set[int]) -> bool:
    """True when some ancestor is a Hermes process (errs toward 'attached': never flag a process
    whose parent might still be supervising it)."""
    for row in ancestry(pid):
        if row["pid"] in hermes_pids or "hermes" in row["command"].lower():
            return True
    return False


def _service_rows(scope: Scope, pattern: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    mine, others, rows = [], [], []
    disabled = launchd.disabled_labels()
    for job in launchd.jobs(pattern):
        job = {**job, "status": launchd.status(job["label"]), "disabled": job["label"] in disabled}
        (mine if _belongs(job, scope.home) else others).append(job)
    for job in mine:
        info = job["status"]
        active = ("active" if launchd.running(info) else "turned off" if job["disabled"] else
                  "stopped" if info.get("loaded") else "not loaded")
        rows.append({"unit": job["label"], "label": compaction._describe(job["label"])["label"], "active": active,
                     "sub": info.get("state"), "restarts": max(0, int(info.get("runs") or 0) - 1),
                     "restart": "always" if job["keepAlive"] else "no", "mainPid": int(info.get("pid") or 0),
                     "since": None, "fragment": job["plist"], "execStart": job["program"]})
    return rows, mine, others


def leftovers(scope: Scope, jobs: list[dict[str, Any]] | None = None, *, pattern: str = "hermes*",
              min_age: float | None = None, **_: Any) -> list[dict[str, Any]]:
    health = _health()
    psutil = psutil_module()
    if psutil is None:
        return []
    if jobs is None:
        jobs = [{**job, "status": launchd.status(job["label"])} for job in launchd.jobs(pattern) if _belongs(job, scope.home)]
    min_age = health.MIN_LEFTOVER_AGE if min_age is None else min_age
    now = time.time()
    hermes_pids = {os.getpid(), os.getppid()} | {int(job["status"].get("pid") or 0) for job in jobs} - {0}
    candidates: list[dict[str, Any]] = []
    for entry in _read_record(scope, "processes.json"):
        pid, expected = entry.get("pid"), entry.get("host_start_time")
        if not isinstance(pid, int) or pid <= 1 or pid in hermes_pids or entry.get("pid_scope") not in (None, "", "host"):
            continue
        if entry.get("persist_on_release"):
            continue  # the chat asked for it to outlive the chat
        if not isinstance(expected, int):
            continue  # no recorded identity: a reused PID could not be told apart
        candidates.append({"pid": pid, "start": expected, "command": str(entry.get("command") or ""), "source": "registry"})
    for entry in _read_record(scope, "spawn-ledger.json"):
        pid, home = entry.get("pid"), entry.get("hermes_home")
        if not isinstance(pid, int) or pid <= 1 or pid in hermes_pids or not isinstance(home, str) or not home:
            continue
        if entry.get("purpose") not in _LEDGER_PURPOSES:
            continue
        if os.path.exists(home):
            continue  # a live profile or copy that still has its folder is not provably a leftover
        start = process_start(pid)
        try:
            born = float(entry.get("create_time"))
        except (TypeError, ValueError):
            continue
        if start is None or abs(start / 100 - born) > 1.0:
            continue
        argv = entry.get("argv")
        command = argv if isinstance(argv, str) else " ".join(str(part) for part in argv) if isinstance(argv, list) else ""
        candidates.append({"pid": pid, "start": start, "command": command[:400],
                           "source": "ledger", "home": home})
    found: list[dict[str, Any]] = []
    live = list({row["pid"]: row for row in reversed(candidates)}.values())  # one row per PID, first source wins
    ports = listening_ports(row["pid"] for row in live)
    for row in live:
        pid = row["pid"]
        if process_start(pid) != row["start"]:
            continue  # gone, or the PID now belongs to another program
        try:
            process = psutil.Process(pid)
            age = now - process.create_time()
            memory = int(process.memory_info().rss)
            command = row["command"] or " ".join(process.cmdline())
        except Exception:
            continue
        if age < min_age or (row["source"] == "registry" and _attached(pid, hermes_pids)):
            continue
        if row["source"] == "ledger":
            why = [f"a separate copy of Hermes started {health._human_age(age)} ago", "the Hermes folder it was started for no longer exists"]
            strong = True
        else:
            why = [f"started in the background by a Hermes chat {health._human_age(age)} ago; the Hermes that started it is no longer running it"]
            strong = False
        if ports.get(pid):
            why.append("listening on port " + ", ".join(str(port) for port in ports[pid]))
        found.append({"kind": "process", "id": f"p{pid}-{row['start']}", "pid": pid, "start": row["start"], "unit": "",
                      "command": command[:200], "age": round(age), "ageText": health._human_age(age), "memoryBytes": memory,
                      "ports": ports.get(pid, []), "why": why, "strong": strong})
    found.sort(key=lambda item: (not item["strong"], -item["age"]))
    return found


def checkup(scope: Scope, *, pattern: str = "hermes*", min_age: float | None = None, check_disk_cleanup: bool = True,
            **_: Any) -> dict[str, Any]:
    health = _health()
    services, mine, others = _service_rows(scope, pattern)
    cards: list[dict[str, Any]] = []
    if not mine:
        cards.append({"id": "no-service", "level": "info", "title": "Hermes isn't set up as a background service on this Mac",
                      "text": "Hermes runs only while an app or terminal keeps it open. To have it start at log-in and come back "
                              "after a crash, run `hermes gateway install` in Terminal."})
    for row, job in zip(services, mine):
        core = any(word in row["unit"] for word in health.CORE_WORDS)
        running = row["active"] == "active"
        info = job["status"]
        # A crash-looping launchd job spends most of its time waiting to respawn ("not running"),
        # so the crash loop is recognised first; a core service is never offered "turn off".
        crashing = _crash_looping(info, row["restarts"])
        if job["disabled"] and not running:
            continue  # turned off on purpose (by Tune-up or `launchctl disable`): not a problem to fix
        if crashing and not core:
            cards.append({"id": f"loop-{row['unit']}", "level": "bad", "title": f"{row['label'].capitalize()} keeps crashing and restarting",
                          "text": f"It has restarted {row['restarts']} times. Last message: {launchd.last_log(job) or 'none'}",
                          "fix": {"action": "turn-off", "unit": row["unit"], "label": "Turn it off (can be turned back on)"}})
        elif not running:
            last = launchd.last_log(job)
            cards.append({"id": f"down-{row['unit']}", "level": "bad", "title": f"{row['label'].capitalize()} is not running",
                          "text": "Hermes can't do this part of its job until it is started again." + (f" Last message: {last}" if last else ""),
                          "fix": {"action": "start", "unit": row["unit"], "label": "Start it"}})
        if running and core and row["restart"] != "always":
            cards.append({"id": f"restart-{row['unit']}", "level": "warn", "title": f"{row['label'].capitalize()} won't come back by itself if it stops",
                          "text": "Its launchd settings don't restart it after a crash. Run `hermes gateway install` again to "
                                  "rewrite them the way Hermes sets them up now."})
        if running and "dashboard" in row["unit"]:
            port = health._port_of(row["execStart"], 9119)
            if port and not health._reachable(port):
                cards.append({"id": f"port-{row['unit']}", "level": "bad", "title": "The dashboard is running but not answering",
                              "text": f"Nothing answers on port {port}, so the desktop/phone apps and your web address can't connect.",
                              "fix": {"action": "restart", "unit": row["unit"], "label": "Restart it"}})
    # Other Hermes services on this Mac (another install or profile) that keep crashing.
    for job in others:
        info = job["status"]
        if job["disabled"] and not launchd.running(info):
            continue
        restarts = max(0, int(info.get("runs") or 0) - 1)
        if _crash_looping(info, restarts):
            other = launchd.service_home(job)
            cards.append({"id": f"loop-{job['label']}", "level": "bad", "title": f"{job['label']} keeps crashing and restarting",
                          "text": f"It is not part of this Hermes: it runs for {other}. It has restarted {restarts} times. "
                                  f"Last message: {launchd.last_log(job) or 'none'}",
                          "fix": {"action": "turn-off", "unit": job["label"], "label": "Turn it off (can be turned back on)"}})
    rest = leftovers(scope, mine, pattern=pattern, min_age=min_age)
    for row in rest:
        row.update(health.explain(row, scope))
    if rest:
        total = sum(row.get("memoryBytes") or 0 for row in rest)
        cards.append({"id": "leftovers", "level": "warn", "title": f"{len(rest)} thing{'s' if len(rest) != 1 else ''} left running by agents",
                      "text": "Programs that chats started in the background and never stopped, like test servers, previews and download links. "
                              "Stopping one only ends the program: nothing is deleted."
                              + (f" Together they use about {total // (1024 * 1024)} MB of memory." if total else ""),
                      "items": rest, "fix": {"action": "stop-leftovers", "label": "Review and stop…"}})
    enabled = health._disk_cleanup_enabled() if check_disk_cleanup else None
    if enabled is False:
        cards.append({"id": "disk-cleanup", "level": "info", "title": "Automatic temp-file cleanup is off",
                      "text": "Hermes has a built-in helper that deletes the scratch files chats create (test_…, tmp_…, old scheduled-job output) "
                              "after a few days, without asking. It only touches files it saw being created inside the Hermes folder. It's optional.",
                      "fix": {"action": "enable-disk-cleanup", "label": "Turn it on"}})
    healthy = not any(card["level"] == "bad" for card in cards)
    return {"available": True, "healthy": healthy, "platform": "macos",
            "services": [{k: row[k] for k in ("unit", "label", "active", "restarts", "restart", "since")} for row in services],
            "tunnels": launchd.tunnel_labels(), "cards": cards, "diskCleanup": enabled}


def _alive_identity(pid: int, start: int) -> bool:
    return process_start(pid) == start


def _wait_running(label: str, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if launchd.running(launchd.status(label)):
            return True
        time.sleep(0.5)
    return False


def act(scope: Scope, body: dict[str, Any], **checkup_options: Any) -> tuple[int, dict[str, Any]]:
    health = _health()
    if (body.get("confirmation") or {}).get("acknowledged") is not True:
        return 409, error_body("confirmation_required", "confirm first", retryable=False, binding=scope.binding)
    action = str(body.get("action") or "")
    unit = str(body.get("unit") or "")
    current = checkup(scope, **checkup_options)
    offered = {f"{card['fix'].get('action', '')}:{card['fix'].get('unit', '')}" for card in current.get("cards", []) if card.get("fix")}
    if action in {"start", "restart", "turn-off"} and f"{action}:{unit}" not in offered:
        return 409, error_body("changed", "this problem is no longer there (or changed); check again", retryable=False, binding=scope.binding)
    job = next((row for row in launchd.jobs(checkup_options.get("pattern", "hermes*")) if row["label"] == unit), None)
    if action in {"start", "restart", "turn-off"} and job is None:
        return 409, error_body("changed", "this service is no longer installed; check again", retryable=False, binding=scope.binding)
    done: list[str] = []
    ok = True
    if action == "start":
        result = launchd.start(job)
        ok = _wait_running(unit)
        done.append(f"Started {unit}" if ok else (result.stderr or result.stdout or f"{unit} did not start").strip()[:200])
    elif action == "restart":
        result = launchd.restart(job)
        ok = result.returncode == 0 and _wait_running(unit)
        done.append(f"Restarted {unit}" if ok else (result.stderr or f"{unit} did not come back").strip()[:200])
    elif action == "turn-off":
        result = launchd.turn_off(job)
        ok = result.returncode == 0
        record = state.read_json(scope, "turned-off.json", [])
        record = record if isinstance(record, list) else []
        record.append({"unit": unit, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        state.write_json(scope, "turned-off.json", record)
        done.append(f"Turned off {unit}. To turn it back on: {launchd.turn_on_command(job)}" if ok else (result.stderr or "could not turn it off").strip()[:200])
    elif action == "enable-disk-cleanup":
        argv = health.commands.hermes_argv()
        result = subprocess.run([*(argv or ["false"]), "plugins", "enable", "disk-cleanup"], capture_output=True, text=True, timeout=120)
        ok = result.returncode == 0
        done.append("Automatic temp-file cleanup is on" if ok else (result.stderr or result.stdout).strip()[-200:])
    elif action == "stop-leftovers":
        wanted = body.get("ids")
        if not isinstance(wanted, list) or not wanted:
            return 400, error_body("invalid_selection", "choose what to stop", retryable=False, binding=scope.binding)
        live = {row["id"]: row for row in leftovers(scope, pattern=checkup_options.get("pattern", "hermes*"), min_age=checkup_options.get("min_age"))}
        for item_id in wanted:
            row = live.get(item_id)
            if row is None:
                done.append(f"Skipped {item_id}: it changed or already stopped")
                continue
            pid, start = row["pid"], row["start"]
            if not _alive_identity(pid, start):
                done.append(f"Skipped process {pid}: it already ended")
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                ok = False
                done.append(f"Could not stop process {pid}: not allowed")
                continue
            for _ in range(50):
                if not _alive_identity(pid, start):
                    break
                time.sleep(0.1)
            if _alive_identity(pid, start):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                time.sleep(0.3)
            stopped = not _alive_identity(pid, start)
            ok = ok and stopped
            done.append(f"Stopped {row['command'][:60]}" if stopped else f"Could not stop process {pid}")
    else:
        return 400, error_body("invalid_action", "unknown fix", retryable=False, binding=scope.binding)
    return 200, response(scope.binding, ok=ok, done=done, checkup=checkup(scope, **checkup_options))
