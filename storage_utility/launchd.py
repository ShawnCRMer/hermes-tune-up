"""macOS: Hermes background services are launchd LaunchAgents (``hermes gateway install``).

Read-only discovery uses the agent's plist and ``launchctl print``. Only these facts are kept:
label, plist path, program arguments, HERMES_HOME/HOME from EnvironmentVariables, KeepAlive,
the stderr log path, and the job's state/pid/runs/last exit code. Every other environment
entry (it can hold provider keys) is dropped as soon as it is parsed and never returned.
"""
from __future__ import annotations

import os
import plistlib
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

from .childenv import minimal_env

LAUNCHCTL = "/bin/launchctl"
_MAX_PLIST_BYTES = 256 * 1024
_MAX_LOG_TAIL = 64 * 1024
_LABEL = re.compile(r"[A-Za-z0-9._-]{1,200}\Z")
_STATUS_KEYS = {"state", "pid", "runs", "last exit code"}


def domain() -> str:
    return f"gui/{os.getuid()}"


def agents_dir() -> Path:
    return Path(os.environ.get("HOME") or str(Path.home())) / "Library" / "LaunchAgents"


HERMES_PREFIX = "ai.hermes."  # what `hermes gateway install` names its LaunchAgents


def label_matches(label: str, pattern: str) -> bool:
    """``hermes*`` (the Linux unit pattern) matches labels that start with it, or Hermes's own
    ``ai.hermes.*`` labels. The Desktop app's ``application.*`` job is not a Hermes service."""
    stem = pattern.rstrip("*").lower()
    lowered = label.lower()
    return bool(stem) and (lowered.startswith(stem) or lowered.startswith(f"ai.{stem}."))


def _read_regular(path: Path, limit: int, *, tail: bool = False) -> bytes | None:
    """Bounded read of a regular file by descriptor: no symlinks, no FIFOs/devices (never blocks)."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        if not tail and info.st_size > limit:
            return None
        if tail and info.st_size > limit:
            os.lseek(fd, info.st_size - limit, os.SEEK_SET)
        chunks, remaining = [], limit + (0 if tail else 1)
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        return None if not tail and len(data) > limit else data
    except OSError:
        return None
    finally:
        os.close(fd)


def _read_plist(path: Path) -> dict[str, Any] | None:
    raw = _read_regular(path, _MAX_PLIST_BYTES)
    if raw is None:
        return None
    try:
        data = plistlib.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _job_from_plist(path: Path, data: dict[str, Any]) -> dict[str, Any] | None:
    label = data.get("Label")
    if not isinstance(label, str) or not _LABEL.fullmatch(label):
        return None
    args = data.get("ProgramArguments")
    program = " ".join(str(part) for part in args) if isinstance(args, list) else str(data.get("Program") or "")
    environment = data.get("EnvironmentVariables")
    environment = environment if isinstance(environment, dict) else {}
    # Keep only the two settings Tune-up needs; drop the rest unread.
    hermes_home = environment.get("HERMES_HOME") if isinstance(environment.get("HERMES_HOME"), str) else None
    home = environment.get("HOME") if isinstance(environment.get("HOME"), str) else None
    stderr = data.get("StandardErrorPath")
    return {"label": label, "plist": str(path), "program": program[:4000], "hermesHome": hermes_home, "home": home,
            "keepAlive": restarts_after_crash(data.get("KeepAlive")),
            "stderr": stderr if isinstance(stderr, str) else None}


def jobs(pattern: str = "hermes*") -> list[dict[str, Any]]:
    """Hermes LaunchAgents installed for this user, in label order."""
    found: list[dict[str, Any]] = []
    try:
        entries = sorted(agents_dir().iterdir())
    except OSError:
        return found
    for path in entries:
        if path.suffix != ".plist":
            continue
        data = _read_plist(path)
        job = _job_from_plist(path, data) if data else None
        if job and label_matches(job["label"], pattern):
            found.append(job)
    return found


def restarts_after_crash(keep_alive: Any) -> bool:
    """launchd brings the job back after it crashes: KeepAlive true, or a dict that relaunches on a
    failing exit (SuccessfulExit false) or on a crash (Crashed true)."""
    if isinstance(keep_alive, bool):
        return keep_alive
    if isinstance(keep_alive, dict):
        return keep_alive.get("SuccessfulExit") is False or keep_alive.get("Crashed") is True
    return False


def parse_disabled(text: str) -> set[str]:
    """Labels ``launchctl print-disabled`` reports as disabled (older macOS prints ``=> true``)."""
    return {match.group(1) for match in re.finditer(r'"([^"]+)"\s*=>\s*(?:disabled|true)\b', text)}


def disabled_labels() -> set[str]:
    try:
        done = launchctl("print-disabled", domain(), timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return parse_disabled(done.stdout) if done.returncode == 0 else set()


def service_home(job: dict[str, Any]) -> str:
    """HERMES_HOME of a job: its EnvironmentVariables, else ~/.hermes of its HOME (Hermes's default)."""
    if job.get("hermesHome"):
        return str(job["hermesHome"])
    return str(Path(job.get("home") or os.environ.get("HOME") or str(Path.home())) / ".hermes")


def parse_print(text: str) -> dict[str, Any]:
    """Only the top-level state/pid/runs/last exit code of ``launchctl print`` output."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("\t") or line.startswith("\t\t"):
            continue  # nested blocks (environment, arguments, coalitions) are skipped unread
        key, sep, value = line.strip().partition(" = ")
        if sep and key in _STATUS_KEYS and key not in values:
            values[key] = value.strip()
    def number(raw: str | None) -> int | None:
        match = re.match(r"-?\d+", raw or "")
        return int(match.group(0)) if match else None
    return {"loaded": True, "state": values.get("state"), "pid": number(values.get("pid")) or 0,
            "runs": number(values.get("runs")) or 0, "lastExit": number(values.get("last exit code"))}


def status(label: str) -> dict[str, Any]:
    if not _LABEL.fullmatch(label):
        return {"loaded": False, "state": None, "pid": 0, "runs": 0, "lastExit": None}
    try:
        done = subprocess.run([LAUNCHCTL, "print", f"{domain()}/{label}"], capture_output=True, text=True,
                              timeout=10, env=minimal_env(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return {"loaded": None, "state": None, "pid": 0, "runs": 0, "lastExit": None}
    if done.returncode != 0:
        return {"loaded": False, "state": None, "pid": 0, "runs": 0, "lastExit": None}
    return parse_print(done.stdout)


def running(info: dict[str, Any]) -> bool:
    return info.get("state") == "running" and bool(info.get("pid"))


def launchctl(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([LAUNCHCTL, *args], capture_output=True, text=True, timeout=timeout,
                          env=minimal_env(), stdin=subprocess.DEVNULL)


def start(job: dict[str, Any]) -> subprocess.CompletedProcess:
    """Start a stopped job: load it from its plist if launchd forgot it, then kickstart."""
    target = f"{domain()}/{job['label']}"
    if not status(job["label"]).get("loaded"):
        launchctl("enable", target)
        loaded = launchctl("bootstrap", domain(), job["plist"])
        if loaded.returncode != 0 and not status(job["label"]).get("loaded"):
            return loaded
    return launchctl("kickstart", target)


def restart(job: dict[str, Any]) -> subprocess.CompletedProcess:
    return launchctl("kickstart", "-k", f"{domain()}/{job['label']}")


def turn_off(job: dict[str, Any]) -> subprocess.CompletedProcess:
    """Unload and disable (survives log-in); the plist is kept so it can be turned back on."""
    target = f"{domain()}/{job['label']}"
    unloaded = launchctl("bootout", target)
    disabled = launchctl("disable", target)
    gone = status(job["label"]).get("loaded") is False  # unknown (launchctl failed) is not success
    return subprocess.CompletedProcess(unloaded.args, 0 if gone and disabled.returncode == 0 else 1,
                                       unloaded.stdout, (unloaded.stderr or disabled.stderr))


def turn_on_command(job: dict[str, Any]) -> str:
    return f"launchctl enable {domain()}/{job['label']} && launchctl bootstrap {domain()} {job['plist']}"


def last_log(job: dict[str, Any]) -> str | None:
    """Last error-looking line of the job's own stderr log (bounded tail read).

    Only a regular file directly inside ``<its Hermes home>/logs`` is read, never through a link,
    so a plist can't point this at a credential file or a FIFO."""
    path = job.get("stderr")
    if not path or not os.path.isabs(path):
        return None
    logs = os.path.realpath(os.path.join(service_home(job), "logs"))
    if os.path.realpath(os.path.dirname(path)) != logs or os.path.basename(path).startswith(".env"):
        return None
    raw = _read_regular(Path(path), _MAX_LOG_TAIL, tail=True)
    if raw is None:
        return None
    text = raw.decode("utf-8", "replace")
    lines = [line.strip() for line in text.splitlines()[-200:] if line.strip()]
    errors = [line for line in lines if re.search(r"Error\b|Error:|error:|Exception", line)] or \
             [line for line in lines if re.search(r"failed|Failed", line)]
    found = (errors or lines or [None])[-1]
    return found[:300] if found else None


def tunnel_labels() -> list[str]:
    """Running cloudflared/tailscale jobs in this user's launchd domain (labels only)."""
    try:
        out = launchctl("list", timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    labels = []
    for line in out.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].strip() not in {"", "-"} and re.search(r"cloudflared|tailscale", parts[2], re.I):
            labels.append(parts[2].strip())
    return labels
