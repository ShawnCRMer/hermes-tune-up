"""Opt-in (TUNEUP_LIVE_LAUNCHD=1): real launchd jobs in this user's gui domain.

Loads two throwaway LaunchAgents (a /bin/sleep and a job that exits 3 forever), drives the
Checkup fixes against them, and always unloads and re-enables them afterwards.
"""
import os
import plistlib
import subprocess
import time

import pytest

from conftest import macos_only
from storage_utility import health, launchd

pytestmark = [macos_only, pytest.mark.skipif(os.environ.get("TUNEUP_LIVE_LAUNCHD") != "1", reason="opt-in live launchd test")]

SLEEPER = "ai.hermes.tuneup-selftest-sleeper"
CRASHER = "ai.hermes.tuneup-selftest-crasher"


@pytest.fixture
def agents(home, tmp_path, monkeypatch):
    folder = tmp_path / "Library" / "LaunchAgents"
    folder.mkdir(parents=True)
    jobs = {
        SLEEPER: {"Label": SLEEPER, "ProgramArguments": ["/bin/sleep", "600"], "KeepAlive": True,
                  "EnvironmentVariables": {"HERMES_HOME": str(home), "FAKE_API_KEY": "s3cret-value"}},
        CRASHER: {"Label": CRASHER, "ProgramArguments": ["/bin/sh", "-c", "echo 'RuntimeError: selftest crash' >&2; exit 3"],
                  "KeepAlive": True, "RunAtLoad": True, "ThrottleInterval": 1, "StandardErrorPath": str(home / "logs" / "crasher.err"),
                  "EnvironmentVariables": {"HERMES_HOME": str(home)}},
    }
    for label, data in jobs.items():
        (folder / f"{label}.plist").write_bytes(plistlib.dumps(data))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(health, "_disk_cleanup_enabled", lambda: None)
    yield folder
    for label in jobs:
        subprocess.run([launchd.LAUNCHCTL, "bootout", f"{launchd.domain()}/{label}"], capture_output=True)
    # Only the crasher was disabled (by the fix under test); undo that so its label can load again.
    subprocess.run([launchd.LAUNCHCTL, "enable", f"{launchd.domain()}/{CRASHER}"], capture_output=True)
    for label in jobs:
        assert not launchd.status(label)["loaded"], f"{label} was left loaded"


def _card(result, card_id):
    return next((card for card in result["cards"] if card["id"] == card_id), None)


def test_start_a_stopped_service_and_turn_off_a_crash_loop(scope_factory, agents, monkeypatch):
    scope = scope_factory()
    monkeypatch.setattr(health, "CRASH_LOOP_RESTARTS", 3)
    # 1. A Hermes service that is not running is offered "Start it", and the fix really starts it.
    result = health.checkup(scope, min_age=0)
    assert _card(result, f"down-{SLEEPER}")["fix"]["action"] == "start"
    status, body = health.act(scope, {"action": "start", "unit": SLEEPER, "confirmation": {"acknowledged": True}}, min_age=0)
    assert status == 200 and body["ok"], body
    info = launchd.status(SLEEPER)
    assert launchd.running(info) and info["pid"] > 1
    assert "s3cret-value" not in str(body)
    # 2. A crash-looping service is offered "Turn it off", which unloads and disables it.
    subprocess.run([launchd.LAUNCHCTL, "bootstrap", launchd.domain(), str(agents / f"{CRASHER}.plist")], check=True, capture_output=True)
    deadline = time.monotonic() + 60
    card = None
    while time.monotonic() < deadline and card is None:
        time.sleep(1)
        card = _card(health.checkup(scope, min_age=0), f"loop-{CRASHER}")
    assert card is not None, launchd.status(CRASHER)
    assert "selftest crash" in card["text"]
    status, body = health.act(scope, {"action": "turn-off", "unit": CRASHER, "confirmation": {"acknowledged": True}}, min_age=0)
    assert status == 200 and body["ok"], body
    assert not launchd.status(CRASHER)["loaded"]
    assert "launchctl enable" in body["done"][0]
    disabled = subprocess.run([launchd.LAUNCHCTL, "print-disabled", launchd.domain()], capture_output=True, text=True).stdout
    assert f'"{CRASHER}" => disabled' in disabled or f'"{CRASHER}" => true' in disabled


@pytest.fixture
def scope_factory(home):
    from storage_utility.scope import Scope
    return lambda: Scope(home, "live")
