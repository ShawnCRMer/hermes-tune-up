"""End to end through the HTTP API the Desktop panel uses, on a throwaway Hermes home."""
import importlib.util
import sqlite3
import sys
import time
import types

import pytest

from conftest import ROOT, macos_only


@pytest.fixture
def client(home, monkeypatch):
    stub = types.ModuleType("hermes_constants")
    stub.get_hermes_home = lambda: str(home)
    monkeypatch.setitem(sys.modules, "hermes_constants", stub)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    spec = importlib.util.spec_from_file_location("tuneup_plugin_api", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from storage_utility import health
    monkeypatch.setattr(health, "_disk_cleanup_enabled", lambda: None)
    app = FastAPI()
    app.include_router(module.router, prefix="/api")
    with TestClient(app) as test_client:
        yield test_client


def _binding(client):
    response = client.get("/api/capabilities")
    assert response.status_code == 200, response.text
    return response.json()["binding"]


def test_scan_completes_and_reports_the_home(client, home):
    binding = _binding(client)
    started = client.post("/api/scans", json={"binding": binding})
    assert started.status_code == 200, started.text
    scan_id = started.json()["scanId"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        body = client.get(f"/api/scans/{scan_id}").json()
        if body["state"] not in {"queued", "running"}:
            break
        time.sleep(0.2)
    assert body["state"] == "complete", body.get("errors")
    assert body["coverage"]["complete"] is True and body["coverage"]["visited"] > 0
    duplicates = client.post("/api/diagnostics/duplicates", json={"binding": binding})
    assert duplicates.status_code == 200, duplicates.text


def test_secret_files_never_appear_in_the_scan(client, home):
    binding = _binding(client)
    scan_id = client.post("/api/scans", json={"binding": binding}).json()["scanId"]
    for _ in range(300):
        body = client.get(f"/api/scans/{scan_id}").json()
        if body["state"] not in {"queued", "running"}:
            break
        time.sleep(0.2)
    assert "not-a-real-key" not in str(body)


@macos_only
def test_checkup_and_pack_on_macos(client, home):
    checkup = client.get("/api/checkup").json()
    assert checkup["available"] is True and checkup["platform"] == "macos"
    pack = client.get("/api/compact").json()
    assert pack["available"] is False and "Mac" in pack["reason"]


@macos_only
def test_manual_pack_steps_use_mac_commands(client, home, monkeypatch):
    sqlite3.connect(home / "state.db").close()
    stats = types.ModuleType("hermes_state_dbfile")  # Hermes's own lock-tracked reader, absent outside Hermes
    stats.collect_state_db_stats = lambda path: {"page_size": 4096, "page_count": 50000, "freelist_count": 40000}
    monkeypatch.setitem(sys.modules, "hermes_state_dbfile", stats)
    category = client.get("/api/cleanup/session-db?fresh=true")
    assert category.status_code == 200, category.text
    text = str(category.json())
    assert "lsof" in text and "fuser" not in text and "Quit the Hermes Desktop app" in text
