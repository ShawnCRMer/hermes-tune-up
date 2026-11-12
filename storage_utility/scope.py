"""Request-time home and binding scope with fail-closed private state access."""
from __future__ import annotations

import hashlib
try:
    import fcntl
except ImportError:  # pragma: no cover - unsupported platform fails closed
    fcntl = None
import json
import os
import platform
import re
import secrets
import threading
import uuid
from pathlib import Path
from typing import Any, Mapping

from . import hostos
from .contracts import Binding

POLICY_VERSION = "v2-aggregate"
# Other profiles are separate Hermes instances: never inventoried from this one.
TOP_LEVEL_EXCLUDED = frozenset({"profiles"})
# Kept for API compatibility; only other profiles are excluded from inventory.
EXCLUDED_DIR_NAMES = TOP_LEVEL_EXCLUDED
SENSITIVE_DIR_NAMES = frozenset({"secrets", "private_keys", ".ssh", ".gnupg", "credentials"})
SENSITIVE_NAMES = frozenset({".env", ".env.local", ".env.production", "auth.json", "credentials.json", "token.json", "secrets", "private_keys", ".ssh", ".netrc", "vault.db"})
_SENSITIVE_PATTERN = re.compile(
    r"(?:^|[^a-z0-9])(?:api[-_.]?key|api[-_.]?token|auth(?:[-_.]?pool)?|token|credential|secret|"
    r"private[-_.]?key|signing[-_.]?key|encryption[-_.]?key|id[-_.]?rsa|password|passwd|key|keys|"
    r"keychain|keystore)(?:$|[^a-z0-9])"
)
_MACHINE_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_EPOCHS: dict[tuple[str, str], str] = {}
_EPOCHS_LOCK = threading.Lock()


def _opaque(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _directory_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if not all(hasattr(os, name) for name in required):
        raise ValueError("safe no-follow directory primitives are unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW


def _open_directory_no_follow(path: Path) -> int:
    """Open every absolute path component with O_NOFOLLOW and own one fd."""
    flags = _directory_flags()
    absolute = Path(os.path.abspath(os.fspath(path)))
    fd = os.open(absolute.anchor or os.sep, flags)
    try:
        for component in absolute.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _open_state_directory(home_fd: int) -> int:
    flags = _directory_flags()
    try:
        os.mkdir("storage-utility", mode=0o700, dir_fd=home_fd)
    except FileExistsError:
        pass
    try:
        state_fd = os.open("storage-utility", flags, dir_fd=home_fd)
    except OSError as exc:
        raise ValueError("private state directory is unavailable or unsafe") from exc
    try:
        state = os.fstat(state_fd)
        if not stat_is_directory(state.st_mode):
            raise ValueError("private state directory is not a directory")
        return state_fd
    except BaseException:
        os.close(state_fd)
        raise


def stat_is_directory(mode: int) -> bool:
    return (mode & 0o170000) == 0o040000


def _read_machine_id(state_fd: int) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open("machine-id", flags, dir_fd=state_fd)
    except OSError as exc:
        raise ValueError("private machine-id is unavailable or unsafe") from exc
    try:
        metadata = os.fstat(fd)
        if not (metadata.st_mode & 0o170000) == 0o100000 or metadata.st_nlink != 1 or metadata.st_size > 64:
            raise ValueError("private machine-id is unavailable or unsafe")
        chunks: list[bytes] = []
        remaining = 65
        while remaining:
            chunk = os.read(fd, min(remaining, 32))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks).decode("ascii").strip()
        if not _MACHINE_ID_PATTERN.fullmatch(value):
            raise ValueError("private machine-id is unavailable or unsafe")
        return value
    except (UnicodeDecodeError, OSError) as exc:
        raise ValueError("private machine-id is unavailable or unsafe") from exc
    finally:
        os.close(fd)


def _machine_id(state_fd: int) -> str:
    """Read an existing identity or publish one with exclusive creation."""
    if fcntl is None:
        raise ValueError("private state locking is unavailable")
    try:
        fcntl.flock(state_fd, fcntl.LOCK_EX)
    except OSError as exc:
        raise ValueError("private state locking is unavailable") from exc
    try:
        for _ in range(2):
            try:
                return _read_machine_id(state_fd)
            except ValueError as exc:
                try:
                    os.stat("machine-id", dir_fd=state_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                except OSError as stat_error:
                    raise ValueError("private machine-id is unavailable or unsafe") from stat_error
                else:
                    raise exc
            value = secrets.token_hex(16)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open("machine-id", flags, 0o600, dir_fd=state_fd)
            except FileExistsError:
                continue
            except OSError as exc:
                raise ValueError("private machine-id is unavailable or unsafe") from exc
            try:
                payload = (value + "\n").encode("ascii")
                written = os.write(fd, payload)
                if written != len(payload):
                    raise OSError("short private machine-id write")
                os.fchmod(fd, 0o600)
            except OSError as exc:
                raise ValueError("private machine-id could not be published") from exc
            finally:
                os.close(fd)
            return value
        raise ValueError("private machine-id is unavailable or unsafe")
    finally:
        try:
            fcntl.flock(state_fd, fcntl.LOCK_UN)
        except OSError:
            pass


class Scope:
    def __init__(self, home: Path, profile_id: str = "default", *, epoch: str | None = None):
        requested_home = Path(home).expanduser()
        try:
            if requested_home.is_symlink():
                raise ValueError("scoped Hermes home cannot be a symlink")
        except OSError as exc:
            raise ValueError("scoped Hermes home is unavailable") from exc
        self.home = Path(os.path.abspath(os.fspath(requested_home)))
        self.profile_id = str(profile_id or "default")
        home_fd = None
        state_fd = None
        try:
            home_fd = _open_directory_no_follow(self.home)
            root_stat = os.fstat(home_fd)
            if not stat_is_directory(root_stat.st_mode):
                raise ValueError("scoped Hermes home must be an existing directory")
            try:
                # Linux: /proc/self/fd (unchanged). macOS: F_GETPATH. Must name the same folder in
                # every process: scan workers rebuild this scope and must reproduce its binding.
                canonical_home = hostos.fd_path(home_fd)
            except OSError as exc:
                raise ValueError("scoped Hermes home identity is unavailable") from exc
            if not canonical_home or not os.path.isabs(canonical_home):
                raise ValueError("scoped Hermes home identity is unavailable")
            state_fd = _open_state_directory(home_fd)
            machine_id = _machine_id(state_fd)
        except (OSError, ValueError) as exc:
            if isinstance(exc, ValueError):
                raise
            raise ValueError("scoped Hermes home is unavailable") from exc
        finally:
            for fd in (state_fd, home_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
        self.data_dir = self.home / "storage-utility"
        self.machine_id = machine_id
        self.root_device = int(root_stat.st_dev)
        self.root_inode = int(root_stat.st_ino)
        self.root_locator = canonical_home
        self.execution_namespace = _opaque(f"{os.name}:{platform.system()}:{platform.machine()}")
        self.epoch = epoch or self._epoch()
        self.root_set_id = _opaque(json.dumps(
            [self.root_locator, self.root_device, self.root_inode, POLICY_VERSION],
            separators=(",", ":"),
        ))
        self.volume_id = self._volume_id()
        self.binding = Binding(
            self.machine_id, self.execution_namespace, self.profile_id, self.root_set_id, (self.volume_id,), self.epoch
        )

    def _volume_id(self) -> str:
        return _opaque(f"{self.root_device}:{Path(self.root_locator).anchor}")

    def _epoch(self) -> str:
        key = (self.root_locator, self.profile_id)
        with _EPOCHS_LOCK:
            return _EPOCHS.setdefault(key, uuid.uuid4().hex)

    def ensure_binding(self, value: Mapping[str, Any] | Binding) -> bool:
        try:
            incoming = value if isinstance(value, Binding) else Binding.from_value(value)
        except ValueError:
            return False
        return incoming == self.binding

    def relative_label(self, path: Path) -> str:
        try:
            return path.relative_to(self.home).as_posix()
        except ValueError:
            return "(outside scoped home)"

    def excluded(self, name: str, relative: str = "") -> bool:
        """True for paths that are out of scope or secret-looking (never actionable)."""
        parts = Path(relative).parts if relative else (name,)
        if parts and parts[0] in TOP_LEVEL_EXCLUDED:
            return True
        return is_sensitive(relative or name)

    def scope_labels(self) -> dict[str, list[str] | str]:
        return {
            "roots": ["Hermes home"],
            "exclusions": ["other profiles", "symlinks", "special files", "other mounted volumes"],
            "sensitive": "credential/secret-looking files are never read, hashed, sent to AI or offered for deletion",
            "policyVersion": POLICY_VERSION,
        }


def is_sensitive_name(name: str) -> bool:
    normalized = name.lower()
    return normalized in SENSITIVE_NAMES or normalized.startswith(".env") or bool(_SENSITIVE_PATTERN.search(normalized))


def is_sensitive(relative: str) -> bool:
    """Credential/secret-looking file, or anything under a secret-named folder."""
    parts = [part for part in relative.replace("\\", "/").split("/") if part]
    if not parts:
        return False
    if any(part.lower() in SENSITIVE_DIR_NAMES for part in parts[:-1]):
        return True
    return is_sensitive_name(parts[-1])


def scope_from_home(home: Path, profile_id: str = "default") -> Scope:
    return Scope(home, profile_id)
