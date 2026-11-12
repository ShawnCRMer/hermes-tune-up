"""Review → Trash → restore, and permanent delete, through the HTTP API on a throwaway home.

HOME points at the test folder, so the plugin's Trash (~/.local/share/Trash) is a test Trash too.
"""
import importlib.util
import os
import sys
import time
import types

import pytest

from conftest import ROOT

CANDIDATES = ("your-files",)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    user_home = tmp_path / "user"
    home = user_home / ".hermes"
    (home / "outputs").mkdir(parents=True)
    old = time.time() - 90 * 86400
    for index in range(3):
        path = home / "outputs" / f"old-{index}.bin"
        path.write_bytes(os.urandom(120_000))
        os.utime(path, (old, old))
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    stub = types.ModuleType("hermes_constants")
    stub.get_hermes_home = lambda: str(home)
    monkeypatch.setitem(sys.modules, "hermes_constants", stub)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    spec = importlib.util.spec_from_file_location("tuneup_plugin_api_actions", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix="/api")
    with TestClient(app) as client:
        yield client, home, user_home


def _reviewable(client):
    for category_id in CANDIDATES:
        response = client.get(f"/api/cleanup/{category_id}?fresh=true")
        if response.status_code != 200:
            continue
        body = response.json()
        items = [item for item in body["category"].get("items", []) if item.get("selectable")]
        if body["category"].get("action") == "review" and items:
            return body, category_id, items
    pytest.fail("no reviewable category with selectable items")


def _apply(client, binding, category_id, items, operation):
    plan = client.post("/api/plans", json={"binding": binding, "categoryId": category_id, "operation": operation,
                                          "itemIds": [item["itemId"] for item in items]})
    assert plan.status_code == 200, plan.text
    plan = plan.json()
    plan_id = plan.get("planId") or plan["plan"]["planId"]
    digest = plan.get("digest") or plan["plan"]["digest"]
    applied = client.post(f"/api/plans/{plan_id}/apply", json={"binding": binding, "digest": digest, "idempotencyKey": f"k-{time.time()}",
                                                              "confirmation": {"operation": operation, "acknowledged": True}})
    assert applied.status_code == 200, applied.text
    return applied.json()


def test_trash_then_restore_round_trip(setup):
    client, home, user_home = setup
    body, category_id, items = _reviewable(client)
    assert body["trash"]["available"] is True, body["trash"]
    target = items[0]
    original = home / target["path"] if not os.path.isabs(target["path"]) else None
    result = _apply(client, body["binding"], category_id, [target], "trash")
    assert result.get("appliedCount", 1) >= 1, result
    trash = client.get("/api/trash").json()
    receipts = [row for row in trash["items"] if row.get("status") == "present"]
    assert receipts, trash
    assert any((user_home / ".local/share/Trash/files").iterdir())
    if original is not None:
        assert not original.exists()
    restored = client.post("/api/trash/restore", json={"binding": body["binding"], "receiptIds": [receipts[0]["receiptId"]],
                                                       "confirmation": {"acknowledged": True}})
    assert restored.status_code == 200, restored.text
    if original is not None:
        assert original.exists()


def test_permanent_delete_removes_only_the_reviewed_item(setup):
    client, home, _ = setup
    body, category_id, items = _reviewable(client)
    before = sorted(p.name for p in (home / "outputs").iterdir())
    _apply(client, body["binding"], category_id, [items[0]], "permanent")
    after = sorted(p.name for p in (home / "outputs").iterdir())
    assert len(after) == len(before) - 1 and items[0]["name"] not in after


def test_stale_binding_is_refused(setup):
    client, _, _ = setup
    body, category_id, items = _reviewable(client)
    stale = {**body["binding"], "epoch": "0" * 32}
    response = client.post("/api/plans", json={"binding": stale, "categoryId": category_id, "operation": "trash",
                                               "itemIds": [items[0]["itemId"]]})
    assert response.status_code == 409 and response.json()["error"]["code"] == "stale_scope"
