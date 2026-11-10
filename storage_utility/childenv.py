"""Environments for child processes.

Only Hermes's own CLI receives the serving process's full environment (it needs the
profile's configuration). Every other child (systemctl, journalctl, crontab, git, uv,
pnpm, apt-get and this plugin's own helper interpreters) gets an explicit minimal
environment, so provider keys and other secrets in the host environment never reach it.
"""
from __future__ import annotations

import os

_KEEP = ("PATH", "HOME", "LANG", "LC_ALL")
_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def minimal_env(**extra: object) -> dict[str, str]:
    env = {key: os.environ[key] for key in _KEEP if os.environ.get(key)}
    env.setdefault("PATH", _DEFAULT_PATH)
    for key, value in extra.items():
        if value is not None:
            env[key] = str(value)
    return env
