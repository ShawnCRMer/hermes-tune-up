"""Pack Hermes's chat-history file (state.db) safely, from OUTSIDE Hermes.

Runs as its own short-lived systemd unit (started by Storage with systemd-run), so it
keeps going while it stops the Hermes services that hold the file. Standard library only.

    python compact_job.py PLAN.json            run the job
    python compact_job.py --restore PLAN.json  start again whatever this job stopped
                                               (wired as ExecStopPost, so it also runs
                                               if the job crashes or is killed)

Order, always the same:
  1. stop watchers first, then services (only units the plan proved serve this Hermes)
  2. wait for every process to let go of the file; if anything still holds it -> stop, change nothing
  3. check free disk space; if too little -> stop, change nothing
  4. run Hermes's own `hermes sessions optimize` (SQLite VACUUM: writes a packed copy and
     swaps it in atomically; an interruption leaves the original intact)
  5. ALWAYS start again exactly the units that were running before (also on failure)
  6. verify: same number of conversations and messages, SQLite quick_check ok, file smaller
Progress and the result are written to the status file the plan names.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HOLDER_WAIT_SECONDS = 45
START_RETRIES = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Log:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.data = self._read() or {"steps": [], "state": "running", "startedAt": _now()}

    def _read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        os.replace(tmp, self.path)

    def step(self, text: str, **extra) -> None:
        self.data["steps"].append({"at": _now(), "text": text, **extra})
        self.data["phase"] = text
        self.save()
        print(text, flush=True)


def _minimal_env() -> dict[str, str]:
    """Non-Hermes children get only PATH, HOME and locale (this file also runs standalone)."""
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL") if os.environ.get(key)}
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    return env


def systemctl(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=timeout, env=_minimal_env())


def is_active(unit: str) -> bool:
    return systemctl("is-active", unit, timeout=20).stdout.strip() in {"active", "activating", "reloading"}


def holders(db: str) -> list[dict]:
    targets = {db, db + "-wal", db + "-shm", db + "-journal"}
    me = {os.getpid(), os.getppid()}
    found = []
    for pid in (entry for entry in os.listdir("/proc") if entry.isdigit()):
        if int(pid) in me:
            continue
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(f"/proc/{pid}/fd/{fd}") in targets:
                    try:
                        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()[:160]
                    except OSError:
                        command = "?"
                    found.append({"pid": int(pid), "command": command})
                    break
            except OSError:
                continue
    return found


def db_facts(db: str) -> dict:
    """Read-only facts; the connection is closed before anything else touches the file."""
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        page = connection.execute("PRAGMA page_size").fetchone()[0]
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
        free = connection.execute("PRAGMA freelist_count").fetchone()[0]
        counts = {}
        for table in ("sessions", "messages"):
            try:
                counts[table] = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                counts[table] = None
        return {"pageBytes": page * pages, "emptyBytes": page * free, "liveBytes": page * (pages - free), "counts": counts}
    finally:
        connection.close()


def quick_check(db: str) -> str:
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        return str(connection.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        connection.close()


def file_bytes(db: str) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.stat(db + suffix).st_blocks * 512
        except OSError:
            pass
    return total


def restore(plan: dict, log: Log) -> bool:
    """Start again exactly what this job stopped: services first, watchers last. Idempotent."""
    stopped = log.data.get("stoppedUnits") or []
    if log.data.get("restored") or not stopped:
        log.data["restored"] = True
        log.save()
        return True
    order = [unit for unit in plan["startOrder"] if unit in stopped] + [unit for unit in stopped if unit not in plan["startOrder"]]
    failed = []
    for unit in order:
        for attempt in range(START_RETRIES):
            systemctl("start", unit)
            time.sleep(1 + attempt)
            if is_active(unit):
                break
        else:
            failed.append(unit)
    log.data["restored"] = not failed
    log.data["restartFailed"] = failed
    log.step("Started everything again" if not failed else f"Could not start again: {', '.join(failed)}", failed=failed)
    return not failed


def run(plan: dict) -> int:
    log = Log(plan["statusFile"])
    db = plan["db"]
    log.data.update(state="running", jobId=plan["jobId"], plannedUnits=plan["stopOrder"])
    log.save()
    outcome = "failed"
    try:
        if not os.path.isfile(db):
            log.step("The chat history file was not found; nothing was changed")
            outcome = "nothing"
            return 2
        before = db_facts(db)
        log.data["before"] = {**before, "fileBytes": file_bytes(db)}
        log.step("Checked the file", **{"emptyBytes": before["emptyBytes"]})

        # 1. stop watchers first, then services; remember only what was actually running
        stopped = []
        for unit in plan["stopOrder"]:
            if unit in plan["startOrder"]:
                if is_active(unit):
                    stopped.append(unit)
                    log.data["stoppedUnits"] = stopped
                    log.save()
                    systemctl("stop", unit)
            elif is_active(unit):
                # A watcher's action is mid-run (e.g. the nightly backup): never cut it off.
                log.step(f"{unit} is running right now, so nothing was changed; try again when it has finished", busy=unit)
                outcome = "busy"
                return 8
            else:
                systemctl("stop", unit)  # a queued action: cancel it, never start it again
        log.step(f"Paused {len(stopped)} Hermes service(s)", units=stopped)

        # 2. nothing may hold the file
        deadline = time.monotonic() + HOLDER_WAIT_SECONDS
        still = holders(db)
        while still and time.monotonic() < deadline:
            time.sleep(1)
            still = holders(db)
        if still:
            log.data["blockers"] = still
            log.step("Something else is still using the file, so nothing was changed", blockers=still)
            outcome = "blocked"
            return 3

        # 3. room for the packed copy (its live data) plus its journal, with a margin
        free = os.statvfs(os.path.dirname(db))
        free_bytes = free.f_bavail * free.f_frsize
        need = before["liveBytes"] * 2 + 256 * 1024 * 1024
        need = max(need, int(plan.get("minFreeBytes") or 0))
        if free_bytes < need:
            log.step("Not enough free disk space to pack safely, so nothing was changed", needBytes=need, freeBytes=free_bytes)
            outcome = "no-space"
            return 4

        # 4. Hermes's own command
        log.step("Packing the chat history file (Hermes is paused)")
        env = {**os.environ, "HERMES_HOME": plan["hermesHome"]}
        started = time.monotonic()
        try:
            result = subprocess.run(plan["optimizeArgv"], env=env, capture_output=True, text=True,
                                    timeout=int(plan.get("timeoutSeconds", 7200)), stdin=subprocess.DEVNULL)
            log.data["optimizeOutput"] = (result.stdout + result.stderr)[-3000:]
            log.data["optimizeSeconds"] = round(time.monotonic() - started, 1)
            if result.returncode != 0:
                log.step(f"Hermes's packing command failed (exit {result.returncode}); the file is unchanged or fully packed, never half-done")
                outcome = "failed"
                return 5
        except subprocess.TimeoutExpired:
            log.step("Packing took too long and was stopped; SQLite keeps the original file intact")
            outcome = "failed"
            return 6

        # 6 (before restart, while nothing writes): verify
        log.step("Checking that every conversation is still there")
        after = db_facts(db)
        check = quick_check(db)
        log.data["after"] = {**after, "fileBytes": file_bytes(db), "quickCheck": check}
        same = after["counts"] == before["counts"]
        if check != "ok" or not same:
            log.step("Packed, but the check afterwards found a difference; see details", quickCheck=check,
                     countsBefore=before["counts"], countsAfter=after["counts"])
            outcome = "check-failed"
            return 7
        log.step("Packed and checked: every conversation and message is still there")
        outcome = "done"
        return 0
    except Exception as error:  # noqa: BLE001 - always fall through to restore
        log.step(f"Unexpected problem: {type(error).__name__}: {error}")
        outcome = "failed"
        return 9
    finally:
        restored = restore(plan, log)
        log.data["state"] = outcome if restored else f"{outcome}-restart-failed"
        log.data["finishedAt"] = _now()
        before, after = log.data.get("before") or {}, log.data.get("after") or {}
        if before.get("fileBytes") is not None and after.get("fileBytes") is not None:
            log.data["savedBytes"] = max(0, before["fileBytes"] - after["fileBytes"])
        log.save()


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--restore":
        plan = json.loads(Path(sys.argv[2]).read_text())
        log = Log(plan["statusFile"])
        ok = restore(plan, log)
        if log.data.get("state") == "running":  # the job itself died mid-way
            log.data["state"] = "interrupted" if ok else "interrupted-restart-failed"
            log.data["finishedAt"] = _now()
            log.step("The packing job was interrupted; SQLite keeps the original file intact")
        return 0 if ok else 1
    plan = json.loads(Path(sys.argv[1]).read_text())
    return run(plan)


if __name__ == "__main__":
    sys.exit(main())
