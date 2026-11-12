"""Health checkup: is this Hermes healthy and reachable, and did agents leave things running?

Everything here is read-only except the explicit fixes at the bottom, each of which the user
confirms, is re-checked against the live system right before it acts, and only ever touches:
  * services PROVEN to serve this Hermes home (see compaction.select_units), or a Hermes
    service that is crash-looping (turned off, never deleted; can be turned back on);
  * processes that were left behind: detached from the Hermes service that started them,
    or temporary services an agent started from the Hermes/user folders.
"""
from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from . import chatlink, commands, compaction, hostos, state
from .childenv import minimal_env
from .contracts import error_body, response
from .scope import Scope

CORE_WORDS = ("gateway", "dashboard")
MIN_LEFTOVER_AGE = 30 * 60        # younger background work may still be in use by a running chat
CRASH_LOOP_RESTARTS = 10
_CLK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
DROPIN = "tune-up-restart.conf"


def _show(unit: str, *props: str) -> dict[str, str]:
    return compaction._show(unit, *props)


def _boot_time() -> float:
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except OSError:
        pass
    return time.time()


def _stat(pid: int) -> dict[str, Any] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    rest = raw[raw.rfind(")") + 2:].split()
    return {"ppid": int(rest[1]), "start": int(rest[19]), "rssPages": int(rest[21])}


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def _environ_home(pid: int) -> str | None:
    """HERMES_HOME named on a process's own command line (e.g. `env HERMES_HOME=... cmd`).
    /proc/<pid>/environ is never read: it holds provider keys."""
    match = re.search(r"(?:^|\s)HERMES_HOME=(\S+)", _cmdline(pid))
    return match.group(1).strip("'\"") if match else None


def _listening_ports() -> dict[int, list[int]]:
    """pid -> TCP ports it listens on (from /proc/net/tcp* + each process's sockets)."""
    inodes: dict[str, int] = {}
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) > 9 and parts[3] == "0A":  # LISTEN
                inodes[parts[9]] = int(parts[1].split(":")[1], 16)
    owners: dict[int, list[int]] = {}
    if not inodes:
        return owners
    for pid in (entry for entry in os.listdir("/proc") if entry.isdigit()):
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if target.startswith("socket:["):
                port = inodes.get(target[8:-1])
                if port:
                    owners.setdefault(int(pid), []).append(port)
    return owners


def _cgroup_pids(unit: str) -> list[int]:
    group = _show(unit, "ControlGroup").get("ControlGroup")
    if not group:
        return []
    try:
        return [int(line) for line in Path(f"/sys/fs/cgroup{group}/cgroup.procs").read_text().split()]
    except (OSError, ValueError):
        return []


def _human_age(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)} minutes"
    if seconds < 86400 * 2:
        return f"{int(seconds // 3600)} hours"
    return f"{int(seconds // 86400)} days"


# ----- checks -----------------------------------------------------------------------------

def _service_rows(scope: Scope, pattern: str = "hermes*") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    units = compaction.select_units(scope.home, pattern=pattern)
    rows = []
    for name in units["services"]:
        props = _show(name, "ActiveState", "SubState", "NRestarts", "Restart", "MainPID", "ActiveEnterTimestamp", "FragmentPath", "ExecStart")
        rows.append({"unit": name, "label": compaction._describe(name)["label"], "active": props.get("ActiveState"),
                     "sub": props.get("SubState"), "restarts": int(props.get("NRestarts") or 0), "restart": props.get("Restart"),
                     "mainPid": int(props.get("MainPID") or 0), "since": props.get("ActiveEnterTimestamp"),
                     "fragment": props.get("FragmentPath"), "execStart": props.get("ExecStart", "")})
    return rows, units


def _port_of(exec_start: str, default: int | None) -> int | None:
    match = re.search(r"--port[ =](\d+)", exec_start)
    return int(match.group(1)) if match else default


def _reachable(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def _last_log(unit: str) -> str | None:
    try:
        # The program's own output (not systemd's "Failed with result…" bookkeeping lines).
        out = subprocess.run(["journalctl", f"_SYSTEMD_UNIT={unit}", "-n", "40", "--no-pager", "-o", "cat"], capture_output=True, text=True, timeout=10, env=minimal_env()).stdout
        if not out.strip():
            out = subprocess.run(["journalctl", "-u", unit, "-n", "20", "--no-pager", "-o", "cat"], capture_output=True, text=True, timeout=10, env=minimal_env()).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    errors = [line for line in lines if re.search(r"Error\b|Error:|error:|Exception", line)] or \
             [line for line in lines if re.search(r"failed|Failed", line) and "Failed with result" not in line]
    return (errors or lines or [None])[-1]


def leftovers(scope: Scope, units: dict[str, Any] | None = None, *, pattern: str = "hermes*", transient: str = "*.service",
              min_age: float | None = None) -> list[dict[str, Any]]:
    """Background processes agents left behind, with the evidence for each."""
    if hostos.IS_MACOS:
        from . import health_macos
        return health_macos.leftovers(scope, pattern=pattern, min_age=min_age)
    units = units or compaction.select_units(scope.home, pattern=pattern)
    min_age = MIN_LEFTOVER_AGE if min_age is None else min_age
    now = time.time()
    boot = _boot_time()
    ports = _listening_ports()
    me = {os.getpid(), os.getppid()}
    found: list[dict[str, Any]] = []
    for unit in units["services"]:
        main = int(_show(unit, "MainPID").get("MainPID") or 0)
        pids = _cgroup_pids(unit)
        stats = {pid: _stat(pid) for pid in pids}
        def attached(pid: int) -> bool:
            seen = 0
            while pid and pid != 1 and seen < 64:
                if pid == main:
                    return True
                info = stats.get(pid) or _stat(pid)
                if not info:
                    return False
                pid, seen = info["ppid"], seen + 1
            return False
        for pid, info in stats.items():
            if not info or pid == main or pid in me or attached(pid):
                continue
            parent = stats.get(info["ppid"])
            if parent is not None and not attached(info["ppid"]) and info["ppid"] != main:
                continue  # report only the top of each detached tree
            age = now - (boot + info["start"] / _CLK)
            if age < min_age:
                continue
            command = _cmdline(pid)
            home = _environ_home(pid)
            why = [f"left running by a Hermes chat {_human_age(age)} ago; the chat that started it has finished"]
            strong = False
            if home and not compaction._same(home, scope.home):
                if not os.path.isdir(home):
                    why.append("the Hermes folder it was started for no longer exists")
                else:
                    why.append("it belongs to a different (test) copy of Hermes")
                strong = True
            if ports.get(pid):
                why.append("listening on port " + ", ".join(str(port) for port in sorted(set(ports[pid]))))
            found.append({"kind": "process", "id": f"p{pid}-{info['start']}", "pid": pid, "start": info["start"], "unit": unit,
                          "command": command[:200], "age": round(age), "ageText": _human_age(age),
                          "memoryBytes": info["rssPages"] * os.sysconf("SC_PAGE_SIZE"), "ports": sorted(set(ports.get(pid, []))),
                          "why": why, "strong": strong})
    # Temporary services an agent started (systemd-run) that run code from the Hermes or user folders.
    for name, active in dict.fromkeys(compaction._units(transient)):
        if active not in {"active", "activating"} or name.startswith(compaction.UNIT_PREFIX) or name in units["services"]:
            continue
        props = _show(name, "Transient", "ExecStart", "ActiveEnterTimestampMonotonic", "MainPID", "InvocationID", "Description")
        if props.get("Transient") != "yes":
            continue
        exec_start = props.get("ExecStart", "")
        if str(scope.home) not in exec_start and str(Path.home()) + "/" not in exec_start:
            continue
        try:
            since = float(props.get("ActiveEnterTimestampMonotonic") or 0) / 1e6
            age = time.monotonic() - since if since else 0
        except ValueError:
            age = 0
        if age < min_age:
            continue
        pid = int(props.get("MainPID") or 0)
        why = [f"a temporary service an agent started {_human_age(age)} ago"]
        if ports.get(pid):
            why.append("listening on port " + ", ".join(str(port) for port in sorted(set(ports[pid]))))
        path = re.search(r"argv\[\]=([^;]+)", exec_start)
        found.append({"kind": "unit", "id": f"u{name}-{props.get('InvocationID', '')[:12]}", "unit": name,
                      "invocation": props.get("InvocationID"), "command": (path.group(1).strip() if path else exec_start)[:200],
                      "description": props.get("Description"), "age": round(age), "ageText": _human_age(age),
                      "ports": sorted(set(ports.get(pid, []))), "why": why, "strong": False})
    found.sort(key=lambda row: (not row["strong"], -row["age"]))
    return found


# ----- plain-language explanation of a leftover ------------------------------------------

def _script_text(command: str) -> str:
    """The command line only. Scripts it names are not opened (they can hold credentials)."""
    return command


def _served_folder(text: str) -> str | None:
    for pattern in (r"--directory[ =](\S+)", r"\bcd\s+(\S+)\s*(?:&&|;|\n)", r"\bserve\s+(/\S+)"):
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip("'\"")
    return None


def explain(row: dict[str, Any], scope: Scope) -> dict[str, Any]:
    text = _script_text(row.get("command") or "")
    port = row["ports"][0] if row.get("ports") else None
    lowered = text.lower()
    advice = "Probably not needed. Stopping it only ends the program; no files are deleted, and the chat can start it again."
    verdict = "probably-not-needed"
    if any("no longer exists" in why or "different (test) copy" in why for why in row.get("why", [])):
        what = "A leftover from a test copy of Hermes."
        advice = "Not needed. The test copy it belonged to is gone. Stopping it deletes nothing."
    elif "tailscale serve" in lowered or "http.server" in lowered or re.search(r"\b(serve|static)\b", lowered) and port:
        folder = _served_folder(text)
        what = "A small web server that shares " + (f"the files in {folder.replace(str(Path.home()), '~')}" if folder else "some files") + (f" at port {port}" if port else "") + "."
        if "tailscale serve" in lowered:
            what += " It is also reachable from your other devices through Tailscale."
        advice = "Only needed while you still use that link (for example to download something from it). If you're done with it, stop it. No files are deleted."
    elif re.search(r"\b(vite|next dev|npm run dev|pnpm dev|webpack serve|astro dev|expo start|flask run|uvicorn|streamlit)\b", lowered):
        what = "A live preview of an app being built" + (f" at port {port}" if port else "") + "."
        advice = "Only needed while you're looking at that preview. Stopping it deletes nothing; the project can be previewed again any time."
    elif re.search(r"\b(tail -f|inotifywait|watchman|--watch|nodemon|entr)\b", lowered):
        what = "A watcher that reacts when files change."
        advice = "Usually left over from a finished task. Stopping it deletes nothing."
    elif re.search(r"\b(cloudflared|ngrok|localtunnel|ssh -[NR])\b", lowered):
        what = "A tunnel that makes something on this computer reachable from the internet."
        advice = "Check first: if it isn't your main Cloudflare tunnel for Hermes, it's probably a leftover. Stopping it deletes nothing."
        verdict = "check-first"
    elif re.search(r"(^|\s|/)sleep\s+\d+", lowered) and not port:
        what = "A placeholder that only waits and does nothing else."
    elif "hermes" in lowered and ("gateway" in lowered or "dashboard" in lowered):
        what = "An extra copy of Hermes (" + ("gateway" if "gateway" in lowered else "dashboard") + ")" + (f" on port {port}" if port else "") + ", probably started for a test."
        advice = "Check first: your real Hermes is listed under Hermes services above and is not this one. Extra copies use memory and can get in each other's way."
        verdict = "check-first"
    else:
        what = "A program a chat started in the background" + (f", listening on port {port}" if port else "") + "."
        advice = "Check first if you don't recognise it. Stopping it deletes nothing."
        verdict = "check-first"
    chat = None
    conn = chatlink._connect(scope)
    if conn is not None:
        try:
            needles = [row.get("unit") or ""] if row.get("kind") == "unit" else []
            needles += [Path(token).name for token in (row.get("command") or "").split() if "/" in token and len(Path(token).name) >= 8]
            for needle in [n.removesuffix(".service") for n in needles if n]:
                session_id = chatlink._by_mention(conn, needle)
                if session_id:
                    chat = chatlink._session(conn, session_id)
                    break
        finally:
            conn.close()
    return {"what": what, "advice": advice, "verdict": verdict, "chat": chat}


def _disk_cleanup_enabled() -> bool | None:
    argv = commands.hermes_argv()
    if argv is None:
        return None
    try:
        out = subprocess.run([*argv, "plugins", "list", "--json"], capture_output=True, text=True, timeout=60).stdout
        import json
        start = out.find("[") if out.find("[") >= 0 and (out.find("{") < 0 or out.find("[") < out.find("{")) else out.find("{")
        data = json.loads(out[start:])
    except Exception:
        return None
    rows = data if isinstance(data, list) else data.get("plugins", [])
    for row in rows:
        if row.get("name") == "disk-cleanup":
            return bool(row.get("enabled"))
    return None


def checkup(scope: Scope, *, pattern: str = "hermes*", transient: str = "*.service", min_age: float | None = None,
            check_disk_cleanup: bool = True) -> dict[str, Any]:
    if hostos.IS_MACOS:
        from . import health_macos
        return health_macos.checkup(scope, pattern=pattern, min_age=min_age, check_disk_cleanup=check_disk_cleanup)
    if not os.path.isdir("/run/systemd/system"):
        return {"available": False, "reason": "This computer doesn't run Hermes as background services, so there's nothing to check here.", "cards": []}
    services, units = _service_rows(scope, pattern)
    cards: list[dict[str, Any]] = []
    for row in services:
        core = any(word in row["unit"] for word in CORE_WORDS)
        running = row["active"] == "active"
        if not running:
            cards.append({"id": f"down-{row['unit']}", "level": "bad", "title": f"{row['label'].capitalize()} is not running",
                          "text": "Hermes can't do this part of its job until it is started again." + (f" Last message: {_last_log(row['unit'])}" if _last_log(row["unit"]) else ""),
                          "fix": {"action": "start", "unit": row["unit"], "label": "Start it"}})
        elif row["restarts"] >= CRASH_LOOP_RESTARTS and not core:
            cards.append({"id": f"loop-{row['unit']}", "level": "bad", "title": f"{row['label'].capitalize()} keeps crashing and restarting",
                          "text": f"It has restarted {row['restarts']} times. Last message: {_last_log(row['unit']) or 'none'}",
                          "fix": {"action": "turn-off", "unit": row["unit"], "label": "Turn it off (can be turned back on)"}})
        if running and core and row["restart"] != "always":
            cards.append({"id": f"restart-{row['unit']}", "level": "warn", "title": f"{row['label'].capitalize()} won't come back by itself if it stops",
                          "text": "If it exits, Hermes stays down until someone starts it again. Make it restart automatically, like the gateway.",
                          "fix": {"action": "auto-restart", "unit": row["unit"], "label": "Make it restart automatically"}})
        if running and "dashboard" in row["unit"]:
            port = _port_of(row["execStart"], 9119)
            if port and not _reachable(port):
                cards.append({"id": f"port-{row['unit']}", "level": "bad", "title": "The dashboard is running but not answering",
                              "text": f"Nothing answers on port {port}, so the desktop/phone apps and your web address can't connect.",
                              "fix": {"action": "restart", "unit": row["unit"], "label": "Restart it"}})
    # Other Hermes services on this computer that are crash-looping (e.g. an old one an update broke).
    for name, active in compaction._units(pattern):
        if name in units["services"] or not name.endswith(".service"):
            continue
        props = _show(name, "ActiveState", "SubState", "NRestarts", "UnitFileState")
        restarts = int(props.get("NRestarts") or 0)
        gave_up = props.get("ActiveState") == "failed" and restarts >= 3 and props.get("UnitFileState") == "enabled"
        not_staying_up = props.get("SubState") != "running"
        if (restarts >= CRASH_LOOP_RESTARTS and not_staying_up) or restarts >= CRASH_LOOP_RESTARTS * 5 or gave_up:
            other = compaction._service_home(name)
            belongs = compaction._same(other, scope.home)
            whose = ("It runs for this Hermes." if belongs else
                     f"It is not part of this Hermes: it runs for {other}." if other else
                     "It is not part of this Hermes (it belongs to another install or profile).")
            cards.append({"id": f"loop-{name}", "level": "bad", "title": f"{name} keeps crashing and restarting",
                          "text": f"{whose} It has restarted {props.get('NRestarts', '?')} times and fills the system log. Last message: {_last_log(name) or 'none'}",
                          "fix": {"action": "turn-off", "unit": name, "label": "Turn it off (can be turned back on)"}})
    tunnels = [name for name, active in compaction._units("cloudflared*") + compaction._units("tailscaled*") if active == "active" and name.endswith(".service")]
    rest = leftovers(scope, units, transient=transient, min_age=min_age)
    for row in rest:
        row.update(explain(row, scope))
    if rest:
        total = sum(row.get("memoryBytes") or 0 for row in rest)
        cards.append({"id": "leftovers", "level": "warn", "title": f"{len(rest)} thing{'s' if len(rest) != 1 else ''} left running by agents",
                      "text": "Programs that chats started in the background and never stopped, like test servers, previews and download links. "
                              "Stopping one only ends the program: nothing is deleted."
                              + (f" Together they use about {total // (1024 * 1024)} MB of memory." if total else ""),
                      "items": rest, "fix": {"action": "stop-leftovers", "label": "Review and stop…"}})
    enabled = _disk_cleanup_enabled() if check_disk_cleanup else None
    if enabled is False:
        cards.append({"id": "disk-cleanup", "level": "info", "title": "Automatic temp-file cleanup is off",
                      "text": "Hermes has a built-in helper that deletes the scratch files chats create (test_…, tmp_…, old scheduled-job output) "
                              "after a few days, without asking. It only touches files it saw being created inside the Hermes folder. It's optional.",
                      "fix": {"action": "enable-disk-cleanup", "label": "Turn it on"}})
    healthy = not any(card["level"] == "bad" for card in cards)
    return {"available": True, "healthy": healthy, "services": [{k: row[k] for k in ("unit", "label", "active", "restarts", "restart", "since")} for row in services],
            "tunnels": tunnels, "cards": cards, "diskCleanup": enabled}


# ----- fixes (each confirmed, each re-checked against the live system) ----------------------

def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=120, env=minimal_env())


def _alive_identity(pid: int, start: int) -> bool:
    info = _stat(pid)
    return bool(info) and info["start"] == start


def act(scope: Scope, body: dict[str, Any], **checkup_options: Any) -> tuple[int, dict[str, Any]]:
    if hostos.IS_MACOS:
        from . import health_macos
        return health_macos.act(scope, body, **checkup_options)
    if (body.get("confirmation") or {}).get("acknowledged") is not True:
        return 409, error_body("confirmation_required", "confirm first", retryable=False, binding=scope.binding)
    action = str(body.get("action") or "")
    unit = str(body.get("unit") or "")
    current = checkup(scope, **checkup_options)
    offered = {f"{card['fix'].get('action', '')}:{card['fix'].get('unit', '')}" for card in current.get("cards", []) if card.get("fix")}
    if action in {"start", "restart", "turn-off", "auto-restart"} and f"{action}:{unit}" not in offered:
        return 409, error_body("changed", "this problem is no longer there (or changed); check again", retryable=False, binding=scope.binding)
    done: list[str] = []
    if action in {"start", "restart"}:
        result = _systemctl(action, unit)
        ok = result.returncode == 0
        done.append(f"{'Started' if action == 'start' else 'Restarted'} {unit}" if ok else result.stderr.strip()[:200])
    elif action == "turn-off":
        result = _systemctl("disable", "--now", unit)
        _systemctl("reset-failed", unit)
        ok = result.returncode == 0
        record = state.read_json(scope, "turned-off.json", [])
        record = record if isinstance(record, list) else []
        record.append({"unit": unit, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        state.write_json(scope, "turned-off.json", record)
        done.append(f"Turned off {unit}. To turn it back on: systemctl enable --now {unit}" if ok else result.stderr.strip()[:200])
    elif action == "auto-restart":
        fragment = _show(unit, "FragmentPath").get("FragmentPath")
        if not fragment:
            return 409, error_body("unavailable", "can't find this service's settings file", retryable=False, binding=scope.binding)
        folder = Path(fragment).parent / f"{unit}.d"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / DROPIN).write_text("# Added by Tune-up at the user's request: bring the service back whenever it exits.\n"
                                     "# `systemctl stop` still stops it. Delete this file to undo.\n[Service]\nRestart=always\nRestartSec=5\n")
        result = _systemctl("daemon-reload")
        ok = result.returncode == 0 and _show(unit, "Restart").get("Restart") == "always"
        done.append(f"{unit} now restarts automatically (setting saved in {folder / DROPIN})" if ok else "could not apply the setting")
    elif action == "enable-disk-cleanup":
        argv = commands.hermes_argv()
        result = subprocess.run([*(argv or ["false"]), "plugins", "enable", "disk-cleanup"], capture_output=True, text=True, timeout=120)
        ok = result.returncode == 0
        done.append("Automatic temp-file cleanup is on" if ok else (result.stderr or result.stdout).strip()[-200:])
    elif action == "stop-leftovers":
        wanted = body.get("ids")
        if not isinstance(wanted, list) or not wanted:
            return 400, error_body("invalid_selection", "choose what to stop", retryable=False, binding=scope.binding)
        live = {row["id"]: row for row in leftovers(scope, pattern=checkup_options.get("pattern", "hermes*"),
                                                     transient=checkup_options.get("transient", "*.service"), min_age=checkup_options.get("min_age"))}
        ok = True
        for item_id in wanted:
            row = live.get(item_id)
            if row is None:
                done.append(f"Skipped {item_id}: it changed or already stopped")
                continue
            if row["kind"] == "unit":
                result = _systemctl("stop", row["unit"])
                done.append(f"Stopped {row['description'] or row['unit']}" if result.returncode == 0 else f"Could not stop {row['unit']}")
                continue
            pid, start = row["pid"], row["start"]
            if not _alive_identity(pid, start):
                done.append(f"Skipped process {pid}: it already ended")
                continue
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                if not _alive_identity(pid, start):
                    break
                time.sleep(0.1)
            if _alive_identity(pid, start):
                os.kill(pid, signal.SIGKILL)
                time.sleep(0.3)
            stopped = not _alive_identity(pid, start)
            ok = ok and stopped
            done.append(f"Stopped {row['command'][:60]}" if stopped else f"Could not stop process {pid}")
    else:
        return 400, error_body("invalid_action", "unknown fix", retryable=False, binding=scope.binding)
    return 200, response(scope.binding, ok=ok, done=done, checkup=checkup(scope, **checkup_options))
