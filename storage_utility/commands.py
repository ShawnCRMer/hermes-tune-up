"""Allow-listed Hermes maintenance commands, run with their owners' own safety rules.

The plugin never builds a shell command or accepts free-form arguments. Each
command is a fixed argv chosen server-side; the only parameter is a validated
day count for session pruning. Commands run one at a time per Hermes home in
a background thread, with before/after measurements of the folders they own
and of the volume's free space.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .fsutil import measure, volume_free
from .scope import Scope

SESSION_PRUNE_DAYS = (30, 60, 90, 180, 365)
_OUTPUT_LIMIT = 6000
_RUN_TIMEOUT = 1800
_PREVIEW_TIMEOUT = 180
_PREVIEW_TTL = 900


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def hermes_boot_argv(root: str, python: str | None = None) -> list[str]:
    """Start Hermes from its source root exactly the way Hermes relaunches itself (isolated
    interpreter, root on sys.path). `python -m hermes_cli.main` is NOT enough: the serving
    interpreter only finds hermes_cli because Hermes put its root on sys.path at startup."""
    boot = ("import sys, runpy; sys.path.insert(0, %r); "
            "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)") % root
    return [python or sys.executable, "-I", "-c", boot]


def hermes_argv() -> list[str] | None:
    """Invoke the same Hermes installation that is serving this plugin."""
    spec = importlib.util.find_spec("hermes_cli")
    if spec is not None and spec.origin and sys.executable:
        return hermes_boot_argv(str(Path(spec.origin).resolve().parents[1]))
    found = shutil.which("hermes")
    return [found] if found else None


def uv_executable() -> str | None:
    candidates = [Path(sys.executable).parent / "uv", Path(sys.prefix) / "bin" / "uv"]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    found = shutil.which("uv")
    if found:
        return found
    try:  # Hermes's own source tree ships uv in its venv.
        import hermes_cli
        tree = Path(hermes_cli.__file__).resolve().parents[1]
        candidate = tree / "venv" / "bin" / "uv"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    except Exception:
        pass
    return None


@dataclass(frozen=True)
class CommandSpec:
    command_id: str
    label: str
    display: str
    description: str
    consequence: str
    destructive: bool
    measure: tuple[str, ...]
    build: Callable[[Scope, dict[str, Any]], list[str] | None]
    preview: Callable[[Scope, dict[str, Any]], list[str] | None] | None = None
    # Allowed values per parameter: a fixed tuple, or a function of the scope computed
    # server-side (e.g. repos Hermes itself registered). Never free-form input.
    params: dict[str, Any] = field(default_factory=dict)
    parse_preview: Callable[[str], dict[str, Any]] | None = None


def _hermes(*args: str) -> Callable[[Scope, dict[str, Any]], list[str] | None]:
    def build(scope: Scope, params: dict[str, Any]) -> list[str] | None:
        base = hermes_argv()
        return None if base is None else [*base, *args]
    return build


def _uv_prune(scope: Scope, params: dict[str, Any]) -> list[str] | None:
    uv = uv_executable()
    return None if uv is None else [uv, "cache", "prune", "--cache-dir", str(scope.home / "cache" / "uv")]


def _prune_sessions(dry_run: bool):
    def build(scope: Scope, params: dict[str, Any]) -> list[str] | None:
        base = hermes_argv()
        days = int(params["days"])
        if base is None:
            return None
        return [*base, "sessions", "prune", "--older-than", str(days), "--dry-run" if dry_run else "--yes"]
    return build


def _worktree_prune(dry_run: bool):
    def build(scope: Scope, params: dict[str, Any]) -> list[str] | None:
        base = hermes_argv()
        if base is None:
            return None
        return [*base, "worktree", "prune", "--repo", str(params["repo"]), "--json", *(["--dry-run"] if dry_run else [])]
    return build


def _worktree_repos(scope: Scope) -> tuple:
    from .discover import worktree_repos
    return tuple(row["repo"] for row in worktree_repos(scope))


def _parse_worktree_plan(raw: str) -> dict[str, Any]:
    start = raw.find("{")
    try:
        data = json.loads(raw[start:]) if start >= 0 else {}
    except ValueError:
        return {"matchCount": None}
    planned, kept = [], []
    for row in data.get("sweep") or []:
        if not isinstance(row, dict):
            continue
        action = str(row.get("action") or "")
        entry = {"name": row.get("name"), "action": action, "reason": row.get("reason")}
        (kept if action.startswith("skip") or action in {"keep", ""} else planned).append(entry)
    sample = [f"{row['name']}: {row['action'].replace('-', ' ')} ({row['reason']})" for row in planned][:12]
    sample += [f"{row['name']}: kept ({row['reason']})" for row in kept][:12 - len(sample) if len(sample) < 12 else 0]
    return {"matchCount": len(planned), "kept": len(kept), "sample": sample, "unit": "worktree"}


def _root_tool(*argv: str) -> Callable[[Scope, dict[str, Any]], list[str] | None]:
    """A system tool that needs administrator rights on the Hermes computer."""
    def build(scope: Scope, params: dict[str, Any]) -> list[str] | None:
        if not shutil.which(argv[0]) or not hasattr(os, "geteuid") or os.geteuid() != 0:
            return None
        return [shutil.which(argv[0]) or argv[0], *argv[1:]]
    return build


def _pnpm_prune(scope: Scope, params: dict[str, Any]) -> list[str] | None:
    pnpm = shutil.which("pnpm")
    return None if pnpm is None else [pnpm, "store", "prune"]


def git_repos(scope: Scope) -> tuple:
    """Code projects Hermes made (from the project scan) whose history can be packed right now."""
    from . import project_jobs
    saved = project_jobs.latest(scope) or {}
    repos = []
    for row in saved.get("projects") or []:
        root = row.get("root")
        if not root or not row.get("isRepo") or row.get("active"):
            continue
        git = Path(root) / ".git"
        if git.is_dir() and not (git / "gc.pid").exists() and not (git / "index.lock").exists():
            repos.append(root)
    return tuple(sorted(repos))


def _git_gc(scope: Scope, params: dict[str, Any]) -> list[str] | None:
    git = shutil.which("git")
    return None if git is None else [git, "-C", str(params["repo"]), "gc", "--quiet"]


SPECS: dict[str, CommandSpec] = {
    spec.command_id: spec for spec in (
        CommandSpec(
            "uv-cache-prune", "Prune package cache", "uv cache prune",
            "Removes downloaded Python packages that no installed environment uses.",
            "Nothing you made is lost. Packages are downloaded again if a future install needs them.",
            False, ("cache/uv",), _uv_prune,
        ),
        CommandSpec(
            "pm-gc", "Remove unused runtime versions", "hermes pm gc",
            "Hermes's package manager deletes store entries and old dependency generations that nothing references any more.",
            "Current environments are kept. Rolling back to an older Hermes version may need a re-download.",
            False, ("installs", "tools"), _hermes("pm", "gc"),
        ),
        CommandSpec(
            "checkpoints-prune", "Prune old checkpoints", "hermes checkpoints prune -f",
            "Deletes rollback history for projects idle over 7 days or whose folder no longer exists, then caps the store at 500 MB.",
            "You can no longer /rollback those older edits. Recent rollback history is kept.",
            True, ("checkpoints",), _hermes("checkpoints", "prune", "-f"),
        ),
        CommandSpec(
            "checkpoints-clear", "Delete all checkpoints", "hermes checkpoints clear -f",
            "Deletes the entire checkpoint store.",
            "All /rollback history for every project is permanently lost.",
            True, ("checkpoints",), _hermes("checkpoints", "clear", "-f"),
        ),
        CommandSpec(
            "sessions-prune", "Delete old sessions", "hermes sessions prune --older-than <days> --yes",
            "Permanently deletes ended conversations last active before the cutoff. Pinned, archived and still-open sessions are skipped.",
            "Those conversations, their messages and search entries are permanently deleted. Disk space is only returned after the session database is compacted.",
            True, (), _prune_sessions(False), _prune_sessions(True), {"days": SESSION_PRUNE_DAYS},
        ),
        CommandSpec(
            "journal-vacuum", "Shrink system activity logs", "journalctl --vacuum-size=200M",
            "Linux's own log keeps a record of what every program did. This keeps the newest 200 MB and removes older entries.",
            "Older log entries can no longer be looked at for troubleshooting. Recent ones stay.",
            False, (), _root_tool("journalctl", "--vacuum-size=200M"),
        ),
        CommandSpec(
            "apt-clean", "Remove saved Linux updates", "apt-get clean",
            "Removes copies of system updates that are already installed.",
            "Nothing you'll notice. If a package is reinstalled it is downloaded again.",
            False, (), _root_tool("apt-get", "clean"),
        ),
        CommandSpec(
            "pnpm-prune", "Prune unused JavaScript downloads", "pnpm store prune",
            "Removes packages from pnpm's shared download store that no project uses any more.",
            "Nothing you'll notice. They download again if a project needs them.",
            False, (), _pnpm_prune,
        ),
        CommandSpec(
            "git-gc", "Pack project history", "git -C <repo> gc",
            "Git's own housekeeping: packs a code project's history into fewer, smaller files. Every file, commit and branch stays.",
            "Nothing changes in the project. It may run for a few minutes on large projects.",
            False, (), _git_gc, None, {"repo": git_repos},
        ),
        CommandSpec(
            "worktree-prune", "Prune leftover worktrees", "hermes worktree prune --repo <repo>",
            "Hermes's own worktree cleanup for one repository: removes stale agent worktrees and merged branches. Its safety gates keep anything with uncommitted or unpushed work (archiving first where it applies) and leave Kanban task trees to Kanban.",
            "Removed worktrees are deleted from disk; work Hermes judges unsafe to lose is archived or kept.",
            True, (), _worktree_prune(False), _worktree_prune(True), {"repo": _worktree_repos}, _parse_worktree_plan,
        ),
    )
}


def allowed_values(scope: Scope, spec: CommandSpec) -> dict[str, tuple]:
    return {name: (allowed(scope) if callable(allowed) else tuple(allowed)) for name, allowed in spec.params.items()}


def validate_params(spec: CommandSpec, raw: Any, scope: Scope | None = None) -> dict[str, Any] | None:
    params = raw if isinstance(raw, dict) else {}
    result: dict[str, Any] = {}
    choices = allowed_values(scope, spec) if scope is not None else {k: v for k, v in spec.params.items() if not callable(v)}
    if len(choices) != len(spec.params):
        return None
    for name, allowed in choices.items():
        value = params.get(name)
        if isinstance(value, bool) or value not in allowed:
            return None
        result[name] = value
    if set(params) - set(spec.params):
        return None
    return result


def display(spec: CommandSpec, params: dict[str, Any]) -> str:
    text = spec.display
    for name, value in params.items():
        text = text.replace(f"<{name}>", str(value))
    return text


def availability(scope: Scope, spec: CommandSpec) -> tuple[bool, str | None]:
    choices = allowed_values(scope, spec)
    if any(not values for values in choices.values()):
        return False, "nothing to run this on yet"
    sample = {name: values[0] for name, values in choices.items()}
    if spec.build(scope, sample) is None:
        return False, "the required executable is not available on the serving host"
    return True, None


def _environment(scope: Scope) -> dict[str, str]:
    env = dict(os.environ)
    env["HERMES_HOME"] = str(scope.home)
    env["UV_CACHE_DIR"] = str(scope.home / "cache" / "uv")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _tail(text: str) -> str:
    text = text.replace("\r", "\n")
    lines = [line for line in text.splitlines() if "python-dotenv could not parse" not in line]
    joined = "\n".join(lines).strip()
    return joined[-_OUTPUT_LIMIT:]


def _measure_paths(scope: Scope, paths: tuple[str, ...]) -> int | None:
    total = 0
    for relative in paths:
        result = measure(scope, relative, budget_seconds=30)
        if result is None:
            continue
        if not result["complete"]:
            return None
        total += int(result["allocatedBytes"])
    return total


# ----- previews (dry runs) ---------------------------------------------------

_PREVIEWS: dict[str, dict[str, Any]] = {}
_PREVIEWS_LOCK = threading.Lock()
_MATCH = re.compile(r"(\d+)\s+session\(s\)\s+match")
_OPEN = re.compile(r"(\d+)\s+open sessions also match")


def preview(scope: Scope, command_id: str, raw_params: Any) -> tuple[int, dict[str, Any]]:
    spec = SPECS.get(command_id)
    if spec is None or spec.preview is None:
        return 404, {"code": "no_preview", "message": "this command has no preview"}
    params = validate_params(spec, raw_params, scope)
    if params is None:
        return 400, {"code": "invalid_params", "message": "unsupported parameters"}
    argv = spec.preview(scope, params)
    if argv is None:
        return 503, {"code": "unavailable", "message": "the required executable is not available"}
    try:
        completed = subprocess.run(argv, env=_environment(scope), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=_PREVIEW_TIMEOUT, text=True, errors="replace")
    except subprocess.TimeoutExpired:
        return 504, {"code": "preview_timeout", "message": "the dry run took too long"}
    raw = (completed.stdout or "").replace("\r", "\n")
    output = _tail(raw)
    if completed.returncode != 0:
        return 502, {"code": "preview_failed", "message": "the dry run failed", "output": output}
    # Parse the FULL output: the summary line precedes a listing that can exceed the display tail.
    extra: dict[str, Any] = {}
    if spec.parse_preview is not None:
        parsed = spec.parse_preview(raw)
        count, sample, skipped = parsed.get("matchCount"), parsed.get("sample") or [], None
        extra = {key: value for key, value in parsed.items() if key not in {"matchCount", "sample"}}
    else:
        match = _MATCH.search(raw)
        skipped = _OPEN.search(raw)
        count = int(match.group(1)) if match else (0 if "No sessions" in raw or "0 session" in raw else None)
        sample = [line.strip() for line in raw.splitlines() if re.match(r"^\s+\S+\s+\d{4}-\d{2}-\d{2}", line)][:12]
        extra = {"unit": "conversation"}
    token = secrets.token_urlsafe(16)
    record = {"token": token, "home": str(scope.home), "commandId": command_id, "params": params,
              "count": count, "expires": time.time() + _PREVIEW_TTL}
    with _PREVIEWS_LOCK:
        for key in [key for key, value in _PREVIEWS.items() if value["expires"] < time.time()]:
            _PREVIEWS.pop(key, None)
        _PREVIEWS[token] = record
    return 200, {"previewToken": token, "commandId": command_id, "params": params, "matchCount": count,
                 "skippedOpen": int(skipped.group(1)) if skipped else 0, "sample": sample,
                 "display": display(spec, params), **extra}


def _consume_preview(scope: Scope, command_id: str, params: dict[str, Any], token: Any) -> bool:
    with _PREVIEWS_LOCK:
        record = _PREVIEWS.get(token) if isinstance(token, str) else None
        if (record is None or record["expires"] < time.time() or record["home"] != str(scope.home)
                or record["commandId"] != command_id or record["params"] != params):
            return False
        _PREVIEWS.pop(token, None)
        return True


# ----- background jobs -------------------------------------------------------

_JOBS: dict[str, dict[str, Any]] = {}
_RUNNING: dict[str, str] = {}
_JOBS_LOCK = threading.Lock()


def start(scope: Scope, command_id: str, raw_params: Any, preview_token: Any, on_finish: Callable[[], None] | None = None) -> tuple[int, dict[str, Any]]:
    spec = SPECS.get(command_id)
    if spec is None:
        return 404, {"code": "unknown_command", "message": "unknown maintenance command"}
    params = validate_params(spec, raw_params, scope)
    if params is None:
        return 400, {"code": "invalid_params", "message": "unsupported parameters"}
    if spec.preview is not None and not _consume_preview(scope, command_id, params, preview_token):
        return 409, {"code": "preview_required", "message": "run the preview again and review it before running this command"}
    argv = spec.build(scope, params)
    if argv is None:
        return 503, {"code": "unavailable", "message": "the required executable is not available on the serving host"}
    home = str(scope.home)
    job_id = secrets.token_urlsafe(12)
    job = {"jobId": job_id, "commandId": command_id, "label": spec.label, "params": params, "state": "running",
           "startedAt": _now(), "finishedAt": None, "exitCode": None, "output": "", "before": None, "after": None, "home": home}
    with _JOBS_LOCK:
        if home in _RUNNING:
            return 409, {"code": "busy", "message": "another maintenance command is still running", "jobId": _RUNNING[home]}
        _RUNNING[home] = job_id
        _JOBS[job_id] = job
        if len(_JOBS) > 40:
            for key in [key for key, value in _JOBS.items() if value["state"] != "running"][:-20]:
                _JOBS.pop(key, None)

    def worker() -> None:
        try:
            job["before"] = {"ownedBytes": _measure_paths(scope, spec.measure) if spec.measure else None, "volumeFreeBytes": volume_free(scope)}
            try:
                completed = subprocess.run(argv, env=_environment(scope), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, timeout=_RUN_TIMEOUT, text=True, errors="replace")
                job["exitCode"] = completed.returncode
                job["output"] = _tail(completed.stdout or "")
                job["state"] = "succeeded" if completed.returncode == 0 else "failed"
            except subprocess.TimeoutExpired as error:
                job["output"] = _tail(error.stdout if isinstance(error.stdout, str) else "")
                job["state"] = "failed"
                job["exitCode"] = None
                job["output"] += "\n(stopped waiting after 30 minutes)"
            except OSError as error:
                job["state"] = "failed"
                job["output"] = f"could not start: {error.strerror or type(error).__name__}"
            job["after"] = {"ownedBytes": _measure_paths(scope, spec.measure) if spec.measure else None, "volumeFreeBytes": volume_free(scope)}
        finally:
            job["finishedAt"] = _now()
            with _JOBS_LOCK:
                if _RUNNING.get(home) == job_id:
                    _RUNNING.pop(home, None)
            if on_finish is not None:
                try:
                    on_finish()
                except Exception:
                    pass

    threading.Thread(target=worker, name=f"storage-command-{command_id}", daemon=True).start()
    return 200, public_job(job)


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in job.items() if key != "home"}
    before, after = job.get("before") or {}, job.get("after") or {}
    if before.get("ownedBytes") is not None and after.get("ownedBytes") is not None:
        result["ownedBytesRemoved"] = max(0, before["ownedBytes"] - after["ownedBytes"])
    if before.get("volumeFreeBytes") is not None and after.get("volumeFreeBytes") is not None:
        result["volumeFreeDelta"] = after["volumeFreeBytes"] - before["volumeFreeBytes"]
    return result


def get_job(scope: Scope, job_id: str) -> dict[str, Any] | None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None or job["home"] != str(scope.home):
            return None
        return public_job(dict(job))


def running_job(scope: Scope) -> dict[str, Any] | None:
    with _JOBS_LOCK:
        job_id = _RUNNING.get(str(scope.home))
        job = _JOBS.get(job_id) if job_id else None
        return public_job(dict(job)) if job else None
