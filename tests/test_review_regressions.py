"""Regressions for the independent review of the macOS port."""
import json
import os
import plistlib
import subprocess
import time

import pytest

from conftest import macos_only
from storage_utility import health, hostos, launchd, projects


@pytest.fixture
def scope(home):
    from storage_utility.scope import Scope
    return Scope(home, "test")


def _orphan():
    out = subprocess.run(["/bin/sh", "-c", "(exec sleep 300) >/dev/null 2>&1 & echo $!"], capture_output=True, text=True, check=True)
    pid = int(out.stdout.strip())
    import psutil
    deadline = time.monotonic() + 5
    while psutil.Process(pid).ppid() != 1 and time.monotonic() < deadline:
        time.sleep(0.05)
    return pid


@macos_only
def test_in_use_check_fails_closed_on_macos(monkeypatch):
    """psutil cannot list open directory descriptors on macOS, so 'not in use' is never claimed."""
    with pytest.raises(projects.Blind):
        projects.in_use_paths(os.path.expanduser("~"))


@macos_only
def test_ledger_argv_string_is_shown_as_is(scope, home, tmp_path):
    import psutil
    pid = _orphan()
    try:
        (home / "spawn-ledger.json").write_text(json.dumps([{
            "pid": pid, "create_time": psutil.Process(pid).create_time(), "hermes_home": str(tmp_path / "gone"),
            "argv": "hermes gateway run --port 9555", "purpose": "gateway"}]))
        rows = health.leftovers(scope, min_age=0)
        assert rows and rows[0]["command"] == "hermes gateway run --port 9555"
    finally:
        os.kill(pid, 9)


@macos_only
def test_ledger_rows_outside_reapable_purposes_are_ignored(scope, home, tmp_path):
    import psutil
    pid = _orphan()
    try:
        (home / "spawn-ledger.json").write_text(json.dumps([{
            "pid": pid, "create_time": psutil.Process(pid).create_time(), "hermes_home": str(tmp_path / "gone"),
            "argv": "git commit-graph write", "purpose": "git commit-graph"}]))
        assert health.leftovers(scope, min_age=0) == []
    finally:
        os.kill(pid, 9)


@macos_only
def test_persist_on_release_processes_are_not_leftovers(scope, home):
    pid = _orphan()
    try:
        (home / "processes.json").write_text(json.dumps([{"pid": pid, "host_start_time": hostos.process_start(pid), "pid_scope": "host",
                                                          "command": "sleep 300", "persist_on_release": True}]))
        assert health.leftovers(scope, min_age=0) == []
    finally:
        os.kill(pid, 9)


@macos_only
def test_a_turned_off_service_is_not_offered_start_again(scope, home, tmp_path, monkeypatch):
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    label = "ai.hermes.tuneup-test-off"
    (agents / f"{label}.plist").write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": ["/bin/sleep", "1"],
                                                            "EnvironmentVariables": {"HERMES_HOME": str(home)}}))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(health, "_disk_cleanup_enabled", lambda: None)
    monkeypatch.setattr(launchd, "disabled_labels", lambda: {label})
    result = health.checkup(scope, min_age=0)
    assert not any(card.get("fix", {}).get("action") == "start" for card in result["cards"])
    assert result["services"][0]["active"] == "turned off" and result["healthy"] is True


def test_disabled_labels_parser():
    text = '\tdisabled services = {\n\t\t"ai.hermes.a" => disabled\n\t\t"ai.hermes.b" => enabled\n\t\t"ai.hermes.c" => true\n\t}\n'
    assert launchd.parse_disabled(text) == {"ai.hermes.a", "ai.hermes.c"}


def test_label_matching_needs_a_hermes_label():
    assert not launchd.label_matches("com.acme.hermesmail", "hermes*")
    assert launchd.label_matches("ai.hermes.gateway", "hermes*")


def test_default_home_only_counts_for_hermes_own_labels(tmp_path):
    from pathlib import Path
    from storage_utility import health_macos
    mine = Path(tmp_path / ".hermes")
    assert health_macos._belongs({"label": "ai.hermes.gateway", "hermesHome": None, "home": str(tmp_path)}, mine)
    assert not health_macos._belongs({"label": "hermes-thing", "hermesHome": None, "home": str(tmp_path)}, mine)
    assert health_macos._belongs({"label": "hermes-thing", "hermesHome": str(mine), "home": None}, mine)


def test_keepalive_only_counts_when_it_restarts_after_a_crash():
    def job(value):
        data = {"Label": "ai.hermes.x", "ProgramArguments": ["/bin/true"]}
        if value is not None:
            data["KeepAlive"] = value
        return data
    from pathlib import Path
    assert launchd._job_from_plist(Path("/x.plist"), job(True))["keepAlive"] is True
    assert launchd._job_from_plist(Path("/x.plist"), job({"SuccessfulExit": False}))["keepAlive"] is True
    assert launchd._job_from_plist(Path("/x.plist"), job({"Crashed": True}))["keepAlive"] is True
    assert launchd._job_from_plist(Path("/x.plist"), job({"SuccessfulExit": True}))["keepAlive"] is False
    assert launchd._job_from_plist(Path("/x.plist"), job(None))["keepAlive"] is False


def test_last_log_refuses_links_fifos_and_files_outside_the_hermes_logs(tmp_path):
    home = tmp_path / ".hermes"
    (home / "logs").mkdir(parents=True)
    secret = home / ".env"
    secret.write_text("API_KEY=sk-live-secret\n")
    link = home / "logs" / "err.log"
    link.symlink_to(secret)
    job = {"stderr": str(link), "hermesHome": str(home)}
    assert launchd.last_log(job) is None
    assert launchd.last_log({"stderr": str(secret), "hermesHome": str(home)}) is None
    fifo = home / "logs" / "pipe.log"
    os.mkfifo(fifo)
    assert launchd.last_log({"stderr": str(fifo), "hermesHome": str(home)}) is None  # must not hang
    real = home / "logs" / "gateway.error.log"
    real.write_text("ok\nValueError: boom\n")
    assert launchd.last_log({"stderr": str(real), "hermesHome": str(home)}) == "ValueError: boom"


def test_read_plist_refuses_symlinks_and_oversize(tmp_path):
    target = tmp_path / "real.plist"
    target.write_bytes(plistlib.dumps({"Label": "ai.hermes.x"}))
    link = tmp_path / "link.plist"
    link.symlink_to(target)
    assert launchd._read_plist(target) == {"Label": "ai.hermes.x"}
    assert launchd._read_plist(link) is None
    big = tmp_path / "big.plist"
    big.write_bytes(plistlib.dumps({"Label": "ai.hermes.x", "Pad": "x" * (300 * 1024)}))
    assert launchd._read_plist(big) is None


def test_turn_off_does_not_claim_success_when_status_is_unknown(monkeypatch):
    calls = []
    monkeypatch.setattr(launchd, "launchctl", lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(launchd, "status", lambda label: {"loaded": None})
    assert launchd.turn_off({"label": "ai.hermes.x", "plist": "/x"}).returncode != 0


@macos_only
def test_service_text_drops_plist_environment(tmp_path, monkeypatch):
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.x.plist").write_bytes(plistlib.dumps({"Label": "com.x", "ProgramArguments": ["/Users/me/proj/run.sh"],
                                                         "EnvironmentVariables": {"TOKEN": "s3cret"}}))
    monkeypatch.setenv("HOME", str(tmp_path))
    texts = dict(projects.service_text())
    assert "/Users/me/proj/run.sh" in texts["com.x.plist"] and "s3cret" not in texts["com.x.plist"]
