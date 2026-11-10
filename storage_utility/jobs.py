"""Bounded one-worker-per-binding metadata scan jobs.

The manager deliberately owns the complete child lifecycle.  Requests and the
watchdog both enter the same lock, so a terminal result cannot race a reap or
an eviction, and an unreaped child remains owned even after its public state
has failed.
"""
from __future__ import annotations

import json
import multiprocessing
import errno
import os
import pickle
import struct
import signal
import subprocess
import sys
import threading
import tempfile
import time
import uuid
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any

from .contracts import Binding, response
from .scan_worker import launch_scan_worker, run_scan
from .scope import Scope


class JobAdmissionError(RuntimeError):
    """A previous worker could not be proven dead, so fail closed."""


@dataclass(frozen=True)
class JobHandle:
    scan_id: str
    binding: dict[str, Any]


@dataclass
class _Job:
    scope: Scope
    process: Any
    connection: Any
    cancel_event: Any
    state: dict[str, Any]
    deadline: float
    cleanup_deadline: float | None = None
    connection_closed: bool = False
    cleanup_started: bool = False
    cleanup_failed: bool = False
    process_closed: bool = False
    cleanup_thread: threading.Thread | None = None
    ipc_buffer: bytearray = dataclass_field(default_factory=bytearray)
    ipc_expected: int | None = None
    deadline_triggered: bool = False
    created_at: float = 0.0
    terminal_at: float | None = None
    startup_child: Any | None = None
    startup_event: threading.Event = dataclass_field(default_factory=threading.Event)
    startup_thread: threading.Thread | None = None
    startup_error: BaseException | None = None
    startup_timed_out: bool = False
    process_started: bool = False
    terminal_observed: bool = False
    exit_observed_at: float | None = None


_TERMINAL = frozenset({"complete", "partial", "cancelled", "failed"})
_MAX_IPC_BYTES = 8 * 1024 * 1024
_PYTHON_EXECUTABLE = sys.executable


def _minimal_spawn_environment() -> dict[str, str]:
    """Return only interpreter/package lookup variables for a scan child."""
    paths: list[str] = []
    for entry in sys.path:
        if entry and entry not in paths:
            paths.append(entry)
    environment = {"PYTHONNOUSERSITE": "1"}
    if paths:
        environment["PYTHONPATH"] = os.pathsep.join(paths)
    return environment


class _PipeConnection:
    """Small parent-side connection for the clean worker subprocess."""

    def __init__(self, fd: int):
        self._fd = fd
        self._closed = False

    def fileno(self) -> int:
        return self._fd

    def close(self) -> None:
        if not self._closed:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._closed = True


class _CancelSignal:
    """Parent-side one-way cancellation signal for the clean child."""

    def __init__(self, fd: int):
        self._fd = fd
        self._sent = False
        self._closed = False

    def set(self) -> None:
        if self._sent or self._closed:
            return
        try:
            os.write(self._fd, b"\\x01")
        except OSError:
            pass
        self._sent = True

    def close(self) -> None:
        if not self._closed:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._closed = True


class _SpawnedProcess:
    """Small owner for an explicitly exec'd child created with ``posix_spawn``."""

    def __init__(self, pid: int):
        self.pid = pid
        self._returncode: int | None = None
        self._reaped = False
        self._closed = False

    def poll(self) -> int | None:
        if self._reaped:
            return self._returncode
        try:
            waited_pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self._reaped = True
            self._returncode = 0
            return self._returncode
        if waited_pid == 0:
            return None
        self._reaped = True
        self._returncode = os.waitstatus_to_exitcode(status)
        return self._returncode

    def is_alive(self) -> bool:
        return self.poll() is None

    def join(self, timeout: float | None = None) -> None:
        if self._reaped:
            return
        if timeout is None:
            try:
                _, status = os.waitpid(self.pid, 0)
            except ChildProcessError:
                self._returncode = 0
            else:
                self._returncode = os.waitstatus_to_exitcode(status)
            self._reaped = True
            return
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline and self.poll() is None:
            time.sleep(min(0.002, max(0.0, deadline - time.monotonic())))
        self.poll()

    def terminate(self) -> None:
        if not self.is_alive():
            return
        try:
            os.killpg(self.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                os.kill(self.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass

    def kill(self) -> None:
        if not self.is_alive():
            return
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                os.kill(self.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass

    def close(self) -> None:
        if not self._reaped:
            return
        self._closed = True


class _SubprocessWorker:
    """Disposable clean-launcher owner for the scan-worker supervisor.

    The request process launches only an explicit, minimal-environment
    same-interpreter exec.  There is no multiprocessing spawn context,
    forkserver, resource tracker, shell, or inherited credential environment.
    The returned PID and process group are owned before scan observation starts.
    """

    def __init__(self, target, args, *, data_path: str, cancel_path: str, ready_path: str, ipc_dir: str):
        self._target = target
        self._args = args
        self._data_path = data_path
        self._cancel_path = cancel_path
        self._ready_path = ready_path
        self._ipc_dir = ipc_dir
        self.pid: int | None = None
        self._returncode: int | None = None
        self._reaped = False
        self._process: Any | None = None

    @staticmethod
    def _target_name(target) -> tuple[str, str]:
        module = getattr(target, "__module__", None)
        qualname = getattr(target, "__qualname__", None)
        if not module or not qualname or "<locals>" in qualname:
            raise TypeError("worker target must be an importable top-level callable")
        return module, qualname

    def start(self) -> None:
        module, qualname = self._target_name(self._target)
        _, home, profile_id, _cancel_event, binding = self._args
        command = [
            _PYTHON_EXECUTABLE,
            "-m",
            "storage_utility.scan_worker",
            "--supervise",
            module,
            qualname,
            str(home),
            str(profile_id),
            json.dumps(binding, sort_keys=True, separators=(",", ":")),
            self._data_path,
            self._cancel_path,
            self._ready_path,
        ]
        environment = _minimal_spawn_environment()
        self.pid = os.posix_spawn(_PYTHON_EXECUTABLE, command, environment)
        self._process = _SpawnedProcess(self.pid)

    def wait_ready(self, timeout: float) -> bool:
        """Wait only for the clean exec boundary, never for scan completion."""
        deadline = time.monotonic() + max(0.0, timeout)
        ready = Path(self._ready_path)
        while time.monotonic() < deadline:
            if ready.exists():
                return True
            if not self.is_alive():
                return False
            time.sleep(0.002)
        return ready.exists()

    def poll(self):
        if self.pid is None or self._reaped:
            return self._returncode
        process = self._process
        if process is None:
            return self._returncode
        if process.is_alive():
            return None
        process.join(timeout=0)
        self._reaped = True
        self._returncode = process.poll() if process.poll() is not None else 0
        return self._returncode

    def is_alive(self) -> bool:
        return self.pid is not None and self.poll() is None

    def join(self, timeout=None) -> None:
        if self.pid is None or self._reaped:
            return
        process = self._process
        if process is None:
            return
        try:
            process.join(timeout=timeout)
        except (AssertionError, ValueError):
            return
        self.poll()

    def terminate(self) -> None:
        pid = self.pid
        if pid is not None and self.is_alive():
            process = self._process
            try:
                os.killpg(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    if process is not None:
                        process.terminate()
                except (OSError, ProcessLookupError, AttributeError):
                    pass

    def kill(self) -> None:
        pid = self.pid
        if pid is not None and self.is_alive():
            process = self._process
            try:
                os.killpg(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                try:
                    if process is not None:
                        process.kill()
                except (OSError, ProcessLookupError, AttributeError):
                    pass

    def close(self) -> None:
        process = self._process
        if process is not None and self.pid is not None and self._reaped:
            try:
                process.close()
            except (OSError, ValueError):
                pass
        for path in (self._data_path, self._cancel_path, self._ready_path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        try:
            os.rmdir(self._ipc_dir)
        except OSError:
            pass


class JobManager:
    def __init__(self, *, scan_deadline_seconds: float = 120.0, cleanup_grace_seconds: float = 2.0):
        self.jobs: dict[str, _Job] = {}
        self.active_by_binding: dict[str, str] = {}
        self.context = multiprocessing.get_context("spawn")
        self.scan_deadline_seconds = min(150.0, max(0.001, float(scan_deadline_seconds)))
        self.cleanup_grace_seconds = min(2.0, max(0.001, float(cleanup_grace_seconds)))
        self.admission_blocked = False
        self._lock = threading.RLock()
        self._watch_stop = threading.Event()
        self._shutdown_requested = threading.Event()
        self._watchdog = threading.Thread(target=self._watchdog_loop, name="storage-scan-watchdog", daemon=True)
        self._watchdog.start()

    @staticmethod
    def _key(binding: Binding | dict[str, Any]) -> str:
        value = binding.as_dict() if isinstance(binding, Binding) else dict(binding)
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _terminal(state: dict[str, Any]) -> bool:
        return state.get("state") in _TERMINAL

    @staticmethod
    def _process_alive(job: _Job) -> bool:
        if job.process_closed or not job.process_started:
            return False
        try:
            return bool(job.process.is_alive())
        except ValueError:
            # multiprocessing.Process.close() intentionally invalidates the
            # sentinel; retain a safe lifecycle read after shutdown/eviction.
            return False

    @staticmethod
    def _startup_alive(job: _Job) -> bool:
        thread = job.startup_thread
        return thread is not None and thread.is_alive()

    @staticmethod
    def _close_process(job: _Job) -> None:
        if job.process_closed:
            return
        try:
            job.process.close()
        except (OSError, ValueError):
            pass
        try:
            job.cancel_event.close()
        except AttributeError:
            pass
        job.process_closed = True

    @staticmethod
    def _close_startup_child(job: _Job) -> None:
        child = job.startup_child
        if child is None:
            return
        try:
            child.close()
        except (OSError, EOFError, ValueError):
            pass
        job.startup_child = None

    def _mark_startup_timeout_locked(self, job: _Job) -> None:
        if job.startup_timed_out:
            return
        job.startup_timed_out = True
        job.cleanup_deadline = job.deadline + self.cleanup_grace_seconds
        self._set_failed_locked(job, "worker_startup_timeout", cleanup_complete=False)
        self.admission_blocked = True

    def _reconcile_startup_locked(self, job: _Job) -> bool:
        """Close startup resources only after the native starter has returned."""
        if not job.startup_event.is_set() or self._startup_alive(job):
            return False
        if job.process_started and self._process_alive(job):
            return False
        if job.process_started and not job.process_closed:
            job.process.join(timeout=0)
        if not job.connection_closed:
            self._close_connection(job)
        if not job.process_closed:
            self._close_process(job)
        job.state["cancel"]["cleanupComplete"] = True
        return True

    def _refresh_admission_locked(self) -> None:
        """Clear a cleanup hold only after every owned child is observed dead."""
        blocked = False
        for job in self.jobs.values():
            if not job.startup_event.is_set() or self._startup_alive(job):
                if job.startup_timed_out or self._shutdown_requested.is_set():
                    blocked = True
                continue
            if not job.process_started:
                self._reconcile_startup_locked(job)
                continue
            if self._process_alive(job):
                if job.cleanup_started or job.cleanup_failed or job.state.get("state") == "failed":
                    blocked = True
                continue
            else:
                self._reconcile_startup_locked(job)
                job.cleanup_failed = False
        self.admission_blocked = blocked

    def start(self, scope: Scope) -> JobHandle:
        with self._lock:
            self._refresh_admission_locked()
            if self.admission_blocked or self._watch_stop.is_set() or self._shutdown_requested.is_set():
                raise JobAdmissionError("worker cleanup is not proven complete")
            key = self._key(scope.binding)
            active_id = self.active_by_binding.get(key)
            if active_id:
                existing = self.jobs.get(active_id)
                if existing:
                    self._poll_locked(existing)
                    if self._process_alive(existing) or not self._terminal(existing.state) or not existing.terminal_observed:
                        return JobHandle(active_id, scope.binding.as_dict())
                    self.active_by_binding.pop(key, None)

            # Admission begins before process startup.  The deadline therefore
            # includes spawn overhead and cannot be extended by a slow launcher.
            admitted_at = time.monotonic()
            ipc_dir = tempfile.mkdtemp(prefix="storage-scan-")
            data_path = os.path.join(ipc_dir, "data")
            cancel_path = os.path.join(ipc_dir, "cancel")
            ready_path = os.path.join(ipc_dir, "ready")
            os.mkfifo(data_path, 0o600)
            os.mkfifo(cancel_path, 0o600)
            parent_fd = os.open(data_path, os.O_RDONLY | os.O_NONBLOCK)
            cancel_write_fd = os.open(cancel_path, os.O_RDWR | os.O_NONBLOCK)
            parent = _PipeConnection(parent_fd)
            cancel_event = _CancelSignal(cancel_write_fd)
            scan_id = uuid.uuid4().hex
            now = _timestamp()
            state = response(
                scope.binding,
                scanId=scan_id,
                state="queued",
                snapshotId=None,
                startedAt=now,
                finishedAt=None,
                cancel={"requested": False, "acknowledged": False, "cleanupComplete": False},
                coverage={"visited": 0, "excluded": 0, "unreadable": 0, "unvisitedKnown": None, "complete": False, "budgetReason": None},
                errors=[], entries=[], totals={"logicalBytes": 0, "allocatedBytes": 0, "verifiedReclaimableBytes": None},
                categories={},
                growth={"status": "unavailable", "logicalDeltaBytes": None, "reason": "scan pending"}, nextCursor=None,
            )
            process = _SubprocessWorker(
                target=run_scan,
                args=(None, str(scope.home), scope.profile_id, cancel_event, scope.binding.as_dict()),
                data_path=data_path,
                cancel_path=cancel_path,
                ready_path=ready_path,
                ipc_dir=ipc_dir,
            )
            job = _Job(scope, process, parent, cancel_event, state, admitted_at + self.scan_deadline_seconds, created_at=admitted_at)
            try:
                os.set_blocking(parent.fileno(), False)
            except (AttributeError, OSError):
                parent.close()
                cancel_event.close()
                process.close()
                raise RuntimeError("worker IPC does not support nonblocking reads")
            self.jobs[scan_id] = job
            self.active_by_binding[key] = scan_id
        # Native process creation is an unbounded OS call on some platforms.
        # Never make the request executor own that call: the starter is itself
        # an explicitly tracked lifecycle resource, and late completion is
        # reconciled by the manager before any process is observed or closed.
        startup_thread = threading.Thread(
            target=self._start_process,
            args=(job,),
            name=f"storage-scan-starter-{scan_id[:8]}",
            daemon=True,
        )
        job.startup_thread = startup_thread
        startup_thread.start()
        startup_thread.join(timeout=max(0.0, job.deadline - time.monotonic()))
        with self._lock:
            if not job.startup_event.is_set():
                self._mark_startup_timeout_locked(job)
                raise JobAdmissionError("worker startup exceeded its bounded deadline")
            if job.startup_error is not None and not job.process_started:
                self._close_connection(job)
                job.cancel_event.close()
                process.close()
                job.state["cancel"]["cleanupComplete"] = True
                job.state["state"] = "failed"
                job.state["errors"] = [{"code": "worker_start_failed", "message": "scan worker could not start", "retryable": True}]
                job.terminal_at = time.monotonic()
                raise RuntimeError("worker startup failed") from job.startup_error
            if job.startup_timed_out:
                self._reap_locked(job, reason="deadline")
                raise JobAdmissionError("worker startup exceeded its bounded deadline")
            return JobHandle(scan_id, scope.binding.as_dict())

    def _start_process(self, job: _Job) -> None:
        """Run the native launch outside the request and lifecycle locks."""
        process = job.process
        error = None
        try:
            process.start()
        except BaseException as exc:
            error = exc
        with self._lock:
            job.process_started = process.pid is not None
            job.startup_error = error
            job.startup_event.set()
            if self._shutdown_requested.is_set() and job.process_started and self._process_alive(job):
                try:
                    process.kill()
                except (OSError, AttributeError):
                    pass
                process.join(timeout=0)
            if not self._shutdown_requested.is_set():
                job.startup_thread = None

    def _close_connection(self, job: _Job) -> None:
        if not job.connection_closed:
            try:
                job.connection.close()
            except (OSError, EOFError):
                pass
            job.connection_closed = True

    def _set_failed_locked(self, job: _Job, code: str, *, cleanup_complete: bool = False) -> None:
        error = {"code": code, "message": "scan worker failed", "retryable": code != "worker_cleanup_failed"}
        if self._terminal(job.state) and job.state["state"] != "failed":
            if not any(existing.get("code") == code for existing in job.state.get("errors", [])):
                job.state["errors"] = [*job.state.get("errors", []), error]
        else:
            job.state["state"] = "failed"
            job.state["errors"] = [error]
            job.terminal_at = time.monotonic()
        job.state["cancel"]["cleanupComplete"] = cleanup_complete

    def _read_message_locked(self, job: _Job) -> dict[str, Any] | None:
        if job.connection_closed:
            return None
        while job.ipc_expected is None:
            if len(job.ipc_buffer) < 4:
                try:
                    chunk = os.read(job.connection.fileno(), 4 - len(job.ipc_buffer))
                except BlockingIOError:
                    return None
                except OSError as exc:
                    if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                        return None
                    self._set_failed_locked(job, "worker_ipc_read_failed", cleanup_complete=False)
                    return None
                if not chunk:
                    return None
                job.ipc_buffer.extend(chunk)
                continue
            size = struct.unpack("!i", job.ipc_buffer[:4])[0]
            header_size = 4
            if size == -1:
                if len(job.ipc_buffer) < 12:
                    try:
                        chunk = os.read(job.connection.fileno(), 12 - len(job.ipc_buffer))
                    except BlockingIOError:
                        return None
                    except OSError as exc:
                        if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                            return None
                        self._set_failed_locked(job, "worker_ipc_read_failed", cleanup_complete=False)
                        return None
                    if not chunk:
                        return None
                    job.ipc_buffer.extend(chunk)
                    continue
                size = struct.unpack("!Q", job.ipc_buffer[4:12])[0]
                header_size = 12
            if size < 0 or size > _MAX_IPC_BYTES:
                self._set_failed_locked(job, "worker_message_too_large", cleanup_complete=False)
                job.ipc_buffer.clear()
                return None
            del job.ipc_buffer[:header_size]
            job.ipc_expected = size
        while len(job.ipc_buffer) < job.ipc_expected:
            try:
                chunk = os.read(job.connection.fileno(), min(64 * 1024, job.ipc_expected - len(job.ipc_buffer)))
            except BlockingIOError:
                return None
            except OSError as exc:
                if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                    return None
                self._set_failed_locked(job, "worker_ipc_read_failed", cleanup_complete=False)
                return None
            if not chunk:
                return None
            job.ipc_buffer.extend(chunk)
        raw = bytes(job.ipc_buffer[:job.ipc_expected])
        del job.ipc_buffer[:job.ipc_expected]
        job.ipc_expected = None
        try:
            message = pickle.loads(raw)
        except (ValueError, pickle.PickleError, TypeError, EOFError):
            self._set_failed_locked(job, "worker_invalid_message", cleanup_complete=False)
            return None
        if not isinstance(message, dict):
            self._set_failed_locked(job, "worker_invalid_message", cleanup_complete=False)
            return None
        return message

    def _accept_message_locked(self, job: _Job, message: dict[str, Any]) -> None:
        # A terminal result (including a terminal failure) is immutable.  A
        # child may have already queued another frame when the first result is
        # observed; accepting it would rewrite the snapshot after callers have
        # seen the terminal state.
        if self._terminal(job.state):
            return
        if not message.get("ok"):
            self._set_failed_locked(job, "worker_failed", cleanup_complete=False)
            return
        result = message.get("result")
        if not isinstance(result, dict) or result.get("binding") != job.scope.binding.as_dict():
            self._set_failed_locked(job, "worker_binding_mismatch", cleanup_complete=False)
            return
        requested = job.state["cancel"]["requested"]
        job.state.update(
            state="cancelled" if requested else ("complete" if result.get("coverage", {}).get("complete") else "partial"),
            snapshotId=job.state["scanId"],
            finishedAt=result.get("finishedAt"), entries=result.get("entries", []),
            coverage=result.get("coverage", {}), errors=result.get("errors", []),
            totals=result.get("totals", {}), growth=result.get("growth", {}),
            categories=result.get("categories", {}),
            tree=result.get("tree", []), largest=result.get("largest", []),
            topLevel=result.get("topLevel", {}), privacy=result.get("privacy", {}),
            elapsedMs=result.get("elapsedMs"),
        )
        job.terminal_at = time.monotonic()

    def _poll_locked(self, job: _Job) -> dict[str, Any]:
        if not job.startup_event.is_set():
            if time.monotonic() >= job.deadline:
                self._mark_startup_timeout_locked(job)
            return job.state
        if not self._terminal(job.state) and time.monotonic() >= job.deadline:
            job.deadline_triggered = True
            self._reap_locked(job, reason="deadline")
            return job.state
        message = None if self._terminal(job.state) else self._read_message_locked(job)
        if message is not None:
            self._accept_message_locked(job, message)
        if not self._process_alive(job):
            if not job.process_closed:
                job.process.join(timeout=0)
            if not self._terminal(job.state):
                if job.exit_observed_at is None:
                    job.exit_observed_at = time.monotonic()
                    return job.state
                if time.monotonic() - job.exit_observed_at < 0.05:
                    return job.state
                self._set_failed_locked(job, "worker_exit", cleanup_complete=True)
                job.terminal_at = time.monotonic()
            else:
                job.state["cancel"]["cleanupComplete"] = True
            self._close_connection(job)
        elif self._terminal(job.state):
            # A child may send its result and then hang.  The result is kept
            # immutable while the manager still enforces cleanup.
            self._reap_locked(job, reason="terminal")
        else:
            job.state["state"] = "running" if job.state["state"] == "queued" else job.state["state"]
        self._refresh_admission_locked()
        self._evict_completed_locked()
        return job.state

    def _watchdog_loop(self) -> None:
        interval = min(0.05, max(0.005, self.scan_deadline_seconds / 10))
        while not self._watch_stop.wait(interval):
            with self._lock:
                for job in list(self.jobs.values()):
                    if not job.startup_event.is_set() and time.monotonic() >= job.deadline:
                        self._mark_startup_timeout_locked(job)
                    elif not self._terminal(job.state) and time.monotonic() >= job.deadline:
                        job.deadline_triggered = True
                        self._reap_locked(job, reason="deadline")
                    elif job.startup_event.is_set() and not self._terminal(job.state):
                        # Drain bounded nonblocking IPC independently of HTTP
                        # polling; a large result otherwise fills the pipe and
                        # the worker hits its deadline while waiting to send.
                        self._poll_locked(job)
                    elif self._process_alive(job) and self._terminal(job.state) and not job.state["cancel"]["cleanupComplete"]:
                        self._reap_locked(job, reason="terminal")
                self._refresh_admission_locked()
                self._evict_completed_locked()

    def _reap_locked(self, job: _Job, *, reason: str) -> bool:
        """Schedule cleanup without holding the manager lock during joins."""
        if not job.startup_event.is_set():
            if reason == "shutdown" or time.monotonic() >= job.deadline:
                self._mark_startup_timeout_locked(job)
            return False
        if not self._process_alive(job):
            return True
        if job.cleanup_started:
            if job.cleanup_thread is not None and job.cleanup_thread.is_alive():
                return False
            if not job.cleanup_failed:
                return False
            # A failed watchdog attempt retains ownership and must not
            # repeatedly consume new budgets.  An explicit shutdown call is
            # the one bounded retry point: it may retry termination with a
            # fresh single budget, while ordinary watchdog polling remains
            # observation-only until the OS child exits.
            if reason != "shutdown":
                return False
            job.cleanup_started = False
            job.cleanup_failed = False
            job.cleanup_deadline = time.monotonic() + self.cleanup_grace_seconds
        was_terminal = self._terminal(job.state)
        deadline_enforced = reason == "deadline" or time.monotonic() >= job.deadline
        if deadline_enforced:
            job.deadline_triggered = True
        job.cleanup_started = True
        if job.cleanup_deadline is None:
            job.cleanup_deadline = time.monotonic() + self.cleanup_grace_seconds
        job.cancel_event.set()
        job.state["cancel"]["requested"] = True
        cleanup_thread = threading.Thread(
            target=self._cleanup_worker,
            args=(job, was_terminal, deadline_enforced),
            name=f"storage-scan-reaper-{job.state['scanId'][:8]}",
            daemon=True,
        )
        job.cleanup_thread = cleanup_thread
        cleanup_thread.start()
        return False

    def _cleanup_worker(self, job: _Job, was_terminal: bool, deadline_enforced: bool) -> None:
        """Reap one child independently of the manager-wide lifecycle lock."""
        cleanup_started_at = time.monotonic()
        cleaned = not self._process_alive(job)
        cleanup_deadline = job.cleanup_deadline or time.monotonic()
        if not cleaned:
            # Reserve most of the single total grace for escalation while
            # keeping every blocking join outside the manager lock.
            cooperative_deadline = min(
                cleanup_deadline,
                cleanup_started_at + self.cleanup_grace_seconds * 0.20,
            )
            remaining = max(0.0, cooperative_deadline - time.monotonic())
            if remaining:
                job.process.join(timeout=remaining)
            cleaned = not self._process_alive(job)
            if not cleaned:
                try:
                    job.process.terminate()
                except (OSError, AttributeError):
                    pass
                terminate_deadline = min(
                    cleanup_deadline,
                    cleanup_started_at + self.cleanup_grace_seconds * 0.45,
                )
                remaining = max(0.0, terminate_deadline - time.monotonic())
                if remaining:
                    job.process.join(timeout=remaining)
                cleaned = not self._process_alive(job)
            if not cleaned:
                try:
                    job.process.kill()
                except (OSError, AttributeError):
                    pass
                remaining = max(0.0, cleanup_deadline - time.monotonic())
                if remaining:
                    job.process.join(timeout=remaining)
                cleaned = not self._process_alive(job)
                if not cleaned:
                    # A SIGKILL can be delivered just as the final bounded
                    # join expires.  A zero-time authoritative read avoids
                    # publishing a false cleanup failure after the child has
                    # already exited, without extending the total budget.
                    job.process.join(timeout=0)
                    cleaned = not self._process_alive(job)
        with self._lock:
            if cleaned:
                if not job.process_closed:
                    job.process.join(timeout=0)
                    self._close_process(job)
                job.state["cancel"].update(acknowledged=True, cleanupComplete=True)
                job.cleanup_failed = False
                job.cleanup_started = True
                if not was_terminal:
                    job.state["finishedAt"] = _timestamp()
                    if deadline_enforced:
                        job.state["state"] = "partial"
                        job.state["coverage"].update(complete=False, unvisitedKnown=None, budgetReason="deadline")
                        job.state["errors"] = [{"code": "deadline", "message": "scan reached its absolute deadline", "retryable": True}]
                    else:
                        job.state["state"] = "cancelled"
                    job.terminal_at = time.monotonic()
                self._close_connection(job)
            else:
                self.admission_blocked = True
                job.cleanup_failed = True
                if not was_terminal:
                    job.state.update(
                        state="failed",
                        finishedAt=_timestamp(),
                        errors=[{"code": "worker_cleanup_failed", "message": "scan worker could not be terminated", "retryable": False}],
                    )
                    job.terminal_at = time.monotonic()
                elif not any(error.get("code") == "worker_cleanup_failed" for error in job.state.get("errors", [])):
                    job.state["errors"] = [
                        *job.state.get("errors", []),
                        {"code": "worker_cleanup_failed", "message": "scan worker could not be terminated", "retryable": False},
                    ]
                job.state["cancel"].update(acknowledged=False, cleanupComplete=False)

    def _evict_completed_locked(self) -> None:
        terminal = [
            job for job in self.jobs.values()
            if self._terminal(job.state) and not self._process_alive(job) and not self._startup_alive(job)
        ]
        # Worker fixtures and future producers may use non-chronological
        # display timestamps.  Retention must follow the manager's observed
        # terminal order rather than comparing producer-controlled strings.
        terminal.sort(key=lambda job: job.terminal_at if job.terminal_at is not None else job.created_at)
        while len(terminal) > 2:
            # The retention bound applies across distinct binding/epoch
            # identities, not only to jobs whose binding map was replaced.
            # Removing an old terminal snapshot is safe; preserve a newer
            # mapping if the binding has since been admitted again.
            old = terminal[0]
            terminal.remove(old)
            old_id = old.state["scanId"]
            old_key = self._key(old.scope.binding)
            self._close_connection(old)
            self.jobs.pop(old_id, None)
            self._close_process(old)
            if self.active_by_binding.get(old_key) == old_id:
                self.active_by_binding.pop(old_key, None)

    def get(self, scan_id: str, binding: Binding | dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            job = self.jobs.get(scan_id)
            if job is None or self._key(job.scope.binding) != self._key(binding):
                return None
            state = self._poll_locked(job)
            if self._terminal(state):
                job.terminal_observed = True
            return state

    def cancel(self, scan_id: str, binding: Binding | dict[str, Any]) -> bool:
        cleanup_thread = None
        with self._lock:
            job = self.jobs.get(scan_id)
            if job is None or self._key(job.scope.binding) != self._key(binding):
                return False
            if not self._terminal(job.state):
                if time.monotonic() >= job.deadline:
                    self._poll_locked(job)
                elif not job.startup_event.is_set():
                    job.state["cancel"]["requested"] = True
                    self._mark_startup_timeout_locked(job)
                else:
                    self._reap_locked(job, reason="cancel")
                    cleanup_thread = job.cleanup_thread
            else:
                self._poll_locked(job)
        if cleanup_thread is not None:
            cleanup_thread.join(timeout=self.cleanup_grace_seconds + 0.05)
        with self._lock:
            job = self.jobs.get(scan_id)
            if job is not None:
                self._refresh_admission_locked()
            return True

    def wait(self, scan_id: str, timeout: float = 10.0) -> dict[str, Any]:
        end = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < end:
            with self._lock:
                job = self.jobs.get(scan_id)
                if job is None:
                    return {}
                state = self._poll_locked(job)
                if self._terminal(state) and state["cancel"].get("cleanupComplete"):
                    job.terminal_observed = True
                    return state
            time.sleep(0.01)
        with self._lock:
            job = self.jobs.get(scan_id)
            if job is None:
                return {}
            state = self._poll_locked(job)
            if self._terminal(state):
                job.terminal_observed = True
            return state

    def process_alive(self, scan_id: str) -> bool:
        with self._lock:
            job = self.jobs.get(scan_id)
            return bool(job and self._process_alive(job))

    def has_live_resources(self) -> bool:
        """Report whether cache eviction would discard any owned lifecycle state."""
        with self._lock:
            return any(
                not job.startup_event.is_set()
                or self._startup_alive(job)
                or self._process_alive(job)
                or (job.cleanup_thread is not None and job.cleanup_thread.is_alive())
                or not job.connection_closed
                or not job.process_closed
                for job in self.jobs.values()
            )

    def shutdown(self) -> None:
        """Stop observation only after every owned child is proven dead."""
        self._shutdown_requested.set()
        with self._lock:
            jobs = list(self.jobs.values())
            for job in jobs:
                if not job.startup_event.is_set():
                    self._mark_startup_timeout_locked(job)
                elif self._startup_alive(job):
                    # The native starter remains the owner until it returns;
                    # closing its endpoints here would lose a late PID.
                    continue
                elif self._process_alive(job):
                    self._reap_locked(job, reason="shutdown")
                else:
                    self._reconcile_startup_locked(job)
            cleanup_deadlines = [job.cleanup_deadline for job in jobs if job.cleanup_deadline is not None]
            shutdown_deadline = max(cleanup_deadlines, default=time.monotonic()) + 0.05

        # Reaper threads perform blocking work outside the lifecycle lock.  A
        # native starter may still be inside an OS launch primitive; shutdown
        # must stop observation without waiting on that uninterruptible call.
        # The starter remains owned by this manager and reconciles any late PID
        # under the lock before it can escape.
        for _ in range(2):
            for job in jobs:
                threads = (job.cleanup_thread,)
                for thread in threads:
                    if thread is None or thread is threading.current_thread() or not thread.is_alive():
                        continue
                    remaining = max(0.0, shutdown_deadline - time.monotonic())
                    if remaining:
                        thread.join(timeout=remaining)

        with self._lock:
            live = []
            for job in jobs:
                if not job.startup_event.is_set() or self._startup_alive(job):
                    live.append(job)
                    continue
                if self._process_alive(job):
                    live.append(job)
                    continue
                if job.cleanup_thread is not None and job.cleanup_thread.is_alive():
                    live.append(job)
                    continue
                if not job.process_closed:
                    self._reconcile_startup_locked(job)
                elif not job.connection_closed:
                    self._close_connection(job)
            self._refresh_admission_locked()
            live = [
                job for job in jobs
                if not job.startup_event.is_set()
                or self._startup_alive(job)
                or self._process_alive(job)
                or (job.cleanup_thread is not None and job.cleanup_thread.is_alive())
            ]
            if live:
                self.admission_blocked = True
                raise JobAdmissionError("shutdown could not prove every worker was reaped")
            self._watch_stop.set()
            self._evict_completed_locked()
        if threading.current_thread() is not self._watchdog:
            self._watchdog.join(timeout=self.cleanup_grace_seconds)
        if self._watchdog.is_alive():
            raise JobAdmissionError("shutdown watchdog did not stop")


def _timestamp() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
