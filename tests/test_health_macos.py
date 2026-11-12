"""macOS leftovers and fixes against real processes (never the real Hermes home)."""
import json
import os
import subprocess
import time

import pytest

from conftest import macos_only
from storage_utility import health, hostos

pytestmark = macos_only


def _orphan(seconds: int = 300) -> int:
    """A sleep whose parent has exited (re-parented to launchd), like a chat's leftover."""
    out = subprocess.run(["/bin/sh", "-c", f"(exec sleep {seconds}) >/dev/null 2>&1 & echo $!"], capture_output=True, text=True, check=True)
    pid = int(out.stdout.strip())
    import psutil
    deadline = time.monotonic() + 5
    while psutil.Process(pid).ppid() != 1 and time.monotonic() < deadline:
        time.sleep(0.05)
    return pid


def _register(home, rows, name="processes.json"):
    (home / name).write_text(json.dumps(rows))


def _kill(pid):
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass


@pytest.fixture
def scope(home):
    from storage_utility.scope import Scope
    return Scope(home, "test")


def _entry(pid, start=None, command="sleep 300"):
    return {"pid": pid, "host_start_time": hostos.process_start(pid) if start is None else start, "pid_scope": "host",
            "command": command, "session_id": "proc_test", "started_at": time.time()}


def test_detached_registered_process_is_a_leftover(scope, home):
    pid = _orphan()
    try:
        _register(home, [_entry(pid)])
        rows = health.leftovers(scope, min_age=0)
        assert [row["pid"] for row in rows] == [pid]
        assert rows[0]["id"] == f"p{pid}-{hostos.process_start(pid)}" and rows[0]["memoryBytes"] > 0
        assert health.leftovers(scope) == []  # younger than 30 minutes: may still be in use
    finally:
        _kill(pid)


def test_process_still_owned_by_a_hermes_parent_is_not_a_leftover(scope, home):
    child = subprocess.Popen(["sleep", "300"])  # parent is this (stand-in Hermes) process
    try:
        _register(home, [_entry(child.pid)])
        assert health.leftovers(scope, min_age=0) == []
    finally:
        child.kill()
        child.wait()


def test_reused_pid_is_never_reported(scope, home):
    pid = _orphan()
    try:
        _register(home, [_entry(pid, start=hostos.process_start(pid) - 500)])
        assert health.leftovers(scope, min_age=0) == []
        _register(home, [{**_entry(pid), "host_start_time": None}])
        assert health.leftovers(scope, min_age=0) == []
    finally:
        _kill(pid)


def test_ledger_copy_whose_home_is_gone_is_a_strong_leftover(scope, home, tmp_path):
    import psutil
    pid = _orphan()
    try:
        _register(home, [{"pid": pid, "create_time": psutil.Process(pid).create_time(), "hermes_home": str(tmp_path / "deleted-home"),
                          "argv": ["hermes", "dashboard", "--port", "9555"], "purpose": "dashboard"}], "spawn-ledger.json")
        rows = health.leftovers(scope, min_age=0)
        assert rows and rows[0]["strong"] and "no longer exists" in " ".join(rows[0]["why"])
        _register(home, [{"pid": pid, "create_time": psutil.Process(pid).create_time(), "hermes_home": str(home),
                          "argv": ["hermes"], "purpose": "dashboard"}], "spawn-ledger.json")
        assert health.leftovers(scope, min_age=0) == []  # its folder exists: not provably a leftover
    finally:
        _kill(pid)


def test_stop_leftovers_stops_exactly_the_reviewed_process(scope, home, monkeypatch):
    pid = _orphan()
    keep = _orphan()
    try:
        _register(home, [_entry(pid), _entry(keep)])
        monkeypatch.setattr(health, "_disk_cleanup_enabled", lambda: None)
        item = f"p{pid}-{hostos.process_start(pid)}"
        status, body = health.act(scope, {"action": "stop-leftovers", "ids": [item, "p1-123"], "confirmation": {"acknowledged": True}}, min_age=0)
        assert status == 200 and body["ok"] is True
        assert hostos.process_start(pid) is None
        assert hostos.process_start(keep) is not None
        assert any("Skipped p1-123" in line for line in body["done"])
    finally:
        _kill(pid)
        _kill(keep)


def test_fixes_need_confirmation_and_a_current_offer(scope, monkeypatch):
    monkeypatch.setattr(health, "_disk_cleanup_enabled", lambda: None)
    status, body = health.act(scope, {"action": "start", "unit": "ai.hermes.gateway"})
    assert status == 409 and body["error"]["code"] == "confirmation_required"
    status, body = health.act(scope, {"action": "turn-off", "unit": "com.apple.Finder", "confirmation": {"acknowledged": True}})
    assert status == 409 and body["error"]["code"] == "changed"
    status, body = health.act(scope, {"action": "auto-restart", "unit": "ai.hermes.gateway", "confirmation": {"acknowledged": True}})
    assert status in {400, 409} and body.get("ok") is None  # never offered on macOS: Hermes owns its launchd settings


def test_checkup_reports_launchd_services_for_this_home_only(scope, home, tmp_path, monkeypatch):
    import plistlib
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    for label, hermes_home in (("ai.hermes.tuneup-test-mine", str(home)), ("ai.hermes.tuneup-test-other", str(tmp_path / "other"))):
        (agents / f"{label}.plist").write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": ["/bin/sleep", "1"],
                                                                "EnvironmentVariables": {"HERMES_HOME": hermes_home, "TOKEN": "s3cret"}}))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(health, "_disk_cleanup_enabled", lambda: None)
    result = health.checkup(scope, min_age=0)
    assert result["available"] and result["platform"] == "macos"
    assert [row["unit"] for row in result["services"]] == ["ai.hermes.tuneup-test-mine"]
    assert result["services"][0]["active"] == "not loaded"
    down = [card for card in result["cards"] if card["id"] == "down-ai.hermes.tuneup-test-mine"]
    assert down and down[0]["fix"] == {"action": "start", "unit": "ai.hermes.tuneup-test-mine", "label": "Start it"}
    assert "s3cret" not in json.dumps(result)
