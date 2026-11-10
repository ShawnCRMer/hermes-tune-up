"""Reviewed cleanup of individual files and folders, plus the Hermes Trash.

Flow: the UI selects items from a server-computed cleanup category, the
server freezes an immutable plan (exact paths, identities, sizes, operation,
expiry, digest), the user confirms, and apply revalidates every item through
anchored no-follow descriptors before acting. A folder written to after
review is refused. Trash moves use the FreeDesktop layout on the same volume
and are recorded as receipts, so the Hermes Trash lists only items this
plugin moved and can restore or permanently remove them individually.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from .contracts import error_body, response
from .fsutil import Anchor, identity, measure_fd, open_parent, rename_noreplace, stat_at, volume_free
from .scope import Scope, _open_directory_no_follow
from . import state

PLAN_TTL_SECONDS = 15 * 60
MAX_ITEMS_PER_PLAN = 200
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_RECEIPTS = "trash-receipts.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ----- Trash location ---------------------------------------------------------

def trash_base() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home and os.path.isabs(data_home):
        return Path(data_home) / "Trash"
    return Path(os.environ.get("HOME") or str(Path.home())) / ".local" / "share" / "Trash"


def _open_trash() -> tuple[int, int]:
    base = trash_base()
    for directory in (base / "files", base / "info"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    files_fd = _open_directory_no_follow(base / "files")
    try:
        info_fd = _open_directory_no_follow(base / "info")
    except BaseException:
        os.close(files_fd)
        raise
    uid = os.getuid()
    if os.fstat(files_fd).st_uid != uid or os.fstat(info_fd).st_uid != uid:
        os.close(files_fd)
        os.close(info_fd)
        raise OSError("Trash folders are not owned by this user")
    return files_fd, info_fd


def trash_support(scope: Scope) -> dict[str, Any]:
    if os.name != "posix" or not hasattr(os, "getuid"):
        return {"available": False, "reason": "OS Trash is only supported on Linux/FreeDesktop serving hosts"}
    try:
        files_fd, info_fd = _open_trash()
    except OSError:
        return {"available": False, "reason": "the user Trash folder is unavailable"}
    try:
        if os.fstat(files_fd).st_dev != scope.root_device:
            return {"available": False, "reason": "the Trash is on a different volume from Hermes, so moving there would copy data; use permanent delete"}
        return {"available": True, "reason": None}
    finally:
        os.close(files_fd)
        os.close(info_fd)


# ----- anchors --------------------------------------------------------------------

def _anchor(scope: Scope, item: dict[str, Any]):
    """Where an item's path is resolved: the Hermes home, or a reviewed project folder."""
    root = item.get("root")
    if not root:
        return scope
    return Anchor(root, item.get("rootIdentity"))


def _display_path(item: dict[str, Any]) -> str:
    return f"{item['root'].rstrip('/')}/{item['path']}" if item.get("root") else item["path"]


def _keeper_intact(scope: Scope, item: dict[str, Any]) -> bool:
    """For a duplicate: the copy being kept must still exist with the reviewed identity."""
    keeper = item.get("keeper")
    if not keeper:
        return True
    try:
        anchor = _anchor(scope, {"root": keeper.get("root"), "rootIdentity": keeper.get("rootIdentity")})
        parent, name = open_parent(anchor, keeper["path"])
    except (OSError, KeyError):
        return False
    try:
        current = identity(stat_at(parent, name))
    except OSError:
        return False
    finally:
        os.close(parent)
    expected = keeper.get("identity") or {}
    return (current["device"], current["inode"], current["size"]) == (expected.get("device"), expected.get("inode"), expected.get("size"))


# ----- plans --------------------------------------------------------------------

def _plan_name(plan_id: str) -> str:
    if not isinstance(plan_id, str) or not plan_id.replace("-", "").replace("_", "").isalnum() or len(plan_id) > 64:
        raise OSError("invalid plan id")
    return f"plan-{plan_id}.json"


def _prune_old_plans(scope: Scope) -> None:
    cutoff = time.time() - 86400
    for name in state.list_names(scope, "plan-"):
        record = state.read_json(scope, name, None)
        if not isinstance(record, dict) or float(record.get("expires", 0)) < cutoff:
            state.remove(scope, name)


@state.serialized
def create_plan(scope: Scope, category: dict[str, Any], body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    operation = body.get("operation")
    if operation not in {"trash", "permanent"}:
        return 400, error_body("invalid_operation", "operation must be trash or permanent", retryable=False, binding=scope.binding)
    if operation == "trash" and not trash_support(scope)["available"]:
        return 409, error_body("trash_unavailable", trash_support(scope)["reason"], retryable=False, binding=scope.binding)
    raw = body.get("itemIds")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_ITEMS_PER_PLAN or not all(isinstance(item, str) for item in raw) or len(set(raw)) != len(raw):
        return 400, error_body("invalid_selection", "select between 1 and 200 items", retryable=False, binding=scope.binding)
    by_id = {item["itemId"]: item for item in category.get("items", [])}
    missing = [item for item in raw if item not in by_id]
    blocked = [item for item in raw if item in by_id and not by_id[item].get("selectable")]
    if missing or blocked:
        return 409, error_body("selection_changed", "some selected items changed or are no longer eligible; refresh and review again", retryable=False, binding=scope.binding)
    _prune_old_plans(scope)
    items = [by_id[item] for item in raw]
    plan_id = secrets.token_urlsafe(12)
    expires = time.time() + PLAN_TTL_SECONDS
    if operation == "trash":
        trash_device = None
        try:
            files_fd, info_fd = _open_trash()
            trash_device = os.fstat(files_fd).st_dev
            os.close(files_fd)
            os.close(info_fd)
        except OSError:
            pass
        if any(item.get("identity", {}).get("device") != trash_device for item in items):
            return 409, error_body("trash_unavailable", "some selected items are on a different disk from the Trash; use permanent delete for them", retryable=False, binding=scope.binding)
    public_items = [{
        "itemId": item["itemId"], "path": _display_path(item), "name": item["name"], "kind": item["kind"],
        "allocatedBytes": item["allocatedBytes"], "files": item.get("files"), "modifiedAt": item.get("modifiedAt"),
        "note": item.get("note"),
    } for item in items]
    public = response(
        scope.binding, planId=plan_id, categoryId=category["id"], categoryTitle=category["title"], operation=operation,
        items=public_items, totalBytes=sum(int(item["allocatedBytes"] or 0) for item in items),
        expiresAt=datetime.fromtimestamp(expires, timezone.utc).isoformat().replace("+00:00", "Z"),
        consequence=category.get("consequence"),
        recovery="Moved to the Hermes Trash. You can restore each item, or remove it permanently to free the space." if operation == "trash"
        else "Permanently deleted immediately. This cannot be undone.",
        freesSpaceNow=operation == "permanent",
    )
    public["digest"] = _digest({key: value for key, value in public.items() if key != "digest"})
    record = {"binding": scope.binding.as_dict(), "expires": expires, "public": public,
              "internal": [{"itemId": item["itemId"], "path": item["path"], "identity": item["identity"],
                            "root": item.get("root"), "rootIdentity": item.get("rootIdentity"), "keeper": item.get("keeper"),
                            "modifiedAt": item.get("modifiedAt"), "allocatedBytes": item["allocatedBytes"]} for item in items],
              "progress": {}, "result": None, "idempotency": {}}
    state.write_json(scope, _plan_name(plan_id), record)
    return 200, public


def _claim(parent: int, name: str, expected: dict[str, Any]) -> str:
    """Rename the exact reviewed object to a private name in its own folder."""
    if identity(stat_at(parent, name)) != expected:
        raise _Refused("changed after review")
    claim = ".storage-claim-" + secrets.token_hex(12)
    rename_noreplace(parent, name, parent, claim)
    if identity(stat_at(parent, claim)) != expected:
        try:
            rename_noreplace(parent, claim, parent, name)
        except OSError:
            pass  # keep the unknown object under its claim name rather than destroy it
        raise _Refused("replaced during cleanup")
    return claim


class _Refused(Exception):
    pass


def _changed_since_review(parent: int, name: str, expected: dict[str, Any], reviewed_mtime: Any, device: int) -> bool:
    if expected["kind"] != "dir" or not isinstance(reviewed_mtime, int):
        return False
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (expected["device"], expected["inode"]):
            return True
        current = measure_fd(fd, device, deadline=time.monotonic() + 20)
        return (not current["complete"]) or int(current["modifiedAt"]) > reviewed_mtime
    finally:
        os.close(fd)


def _remove_claim(parent: int, claim: str, kind: str) -> None:
    if kind == "dir":
        shutil.rmtree(claim, dir_fd=parent)
    else:
        os.unlink(claim, dir_fd=parent)


def _trash_claim(scope: Any, parent: int, claim: str, original: str, receipt_id: str, expected: dict[str, Any]) -> dict[str, Any]:
    files_fd, info_fd = _open_trash()
    info_name = f"{receipt_id}.trashinfo"
    info_written = False
    try:
        if os.fstat(files_fd).st_dev != expected["device"]:
            raise _Refused("Trash is on another volume")
        payload = ("[Trash Info]\nPath=" + quote(str(scope.home / original), safe="/") +
                   "\nDeletionDate=" + datetime.now().strftime("%Y-%m-%dT%H:%M:%S") + "\n").encode()
        fd = os.open(info_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=info_fd)
        try:
            os.write(fd, payload)
            os.fsync(fd)
            info_identity = identity(os.fstat(fd))
        finally:
            os.close(fd)
        info_written = True
        rename_noreplace(parent, claim, files_fd, receipt_id)
        trashed = identity(stat_at(files_fd, receipt_id))
        if (trashed["device"], trashed["inode"], trashed["kind"]) != (expected["device"], expected["inode"], expected["kind"]):
            raise OSError("Trash identity mismatch")
        return {"trashedIdentity": trashed, "trashInfoIdentity": info_identity}
    except BaseException:
        if info_written:
            try:
                os.stat(receipt_id, dir_fd=files_fd, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    os.unlink(info_name, dir_fd=info_fd)
                except OSError:
                    pass
        raise
    finally:
        os.close(files_fd)
        os.close(info_fd)


@state.serialized
def apply_plan(scope: Scope, plan_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    try:
        name = _plan_name(plan_id)
    except OSError:
        return 404, error_body("plan_unavailable", "unknown plan", retryable=False, binding=scope.binding)
    record = state.read_json(scope, name, None)
    if not isinstance(record, dict) or record.get("binding") != scope.binding.as_dict():
        return 404, error_body("plan_unavailable", "this plan does not belong to the active Hermes instance", retryable=False, binding=scope.binding)
    public = record["public"]
    if body.get("digest") != public["digest"]:
        return 409, error_body("plan_conflict", "the reviewed plan does not match", retryable=False, binding=scope.binding)
    confirmation = body.get("confirmation")
    if not isinstance(confirmation, dict) or confirmation.get("operation") != public["operation"] or confirmation.get("acknowledged") is not True:
        return 409, error_body("confirmation_required", "confirm the exact operation first", retryable=False, binding=scope.binding)
    key = body.get("idempotencyKey")
    if not isinstance(key, str) or not key or len(key) > 128:
        return 400, error_body("invalid_idempotency_key", "an idempotency key is required", retryable=False, binding=scope.binding)
    if key in record.get("idempotency", {}):
        return 200, record["idempotency"][key]
    if record.get("result") is not None:
        return 409, error_body("already_applied", "this plan was already applied", retryable=False, binding=scope.binding)
    if time.time() >= float(record["expires"]):
        return 410, error_body("plan_expired", "this review expired; preview again", retryable=False, binding=scope.binding)

    free_before = volume_free(scope)
    receipts = _read_receipts(scope)
    outcomes = []
    for index, item in enumerate(record["internal"]):
        receipt_id = f"hermes-storage-{plan_id}-{index}"
        progress = record["progress"].get(item["itemId"])
        outcome = {"itemId": item["itemId"], "path": _display_path(item), "allocatedBytes": item["allocatedBytes"], "status": "unknown"}
        if progress and progress.get("phase") == "settled":
            outcomes.append(progress["outcome"])
            continue
        if progress and progress.get("phase") == "prepared":
            outcome.update(_reconcile(scope, item, progress, receipts, receipt_id))
        else:
            outcome.update(_apply_one(scope, record, name, item, public["operation"], receipt_id, receipts))
        state.write_json(scope, _RECEIPTS, receipts)
        record["progress"][item["itemId"]] = {"phase": "settled", "outcome": outcome}
        state.write_json(scope, name, record)
        outcomes.append(outcome)
    free_after = volume_free(scope)
    applied = [row for row in outcomes if row["status"] == "applied"]
    result = response(
        scope.binding, planId=plan_id, operation=public["operation"], outcomes=outcomes,
        appliedCount=len(applied), refusedCount=len(outcomes) - len(applied),
        bytesHandled=sum(int(row["allocatedBytes"] or 0) for row in applied),
        volumeFreeDelta=(free_after - free_before) if free_before is not None and free_after is not None else None,
    )
    record["result"] = result
    record["idempotency"][key] = result
    state.write_json(scope, name, record)
    return 200, result


def _apply_one(scope: Scope, record: dict, plan_name: str, item: dict, operation: str, receipt_id: str, receipts: list) -> dict[str, Any]:
    expected = item["identity"]
    try:
        anchor = _anchor(scope, item)
        parent, name = open_parent(anchor, item["path"])
    except OSError:
        return {"status": "refused", "reason": "its folder is missing, was replaced, or is no longer a plain folder"}
    if not _keeper_intact(scope, item):
        os.close(parent)
        return {"status": "refused", "reason": "the copy being kept changed or is gone, so this copy was left in place"}
    try:
        try:
            current = identity(stat_at(parent, name))
        except FileNotFoundError:
            return {"status": "refused", "reason": "already gone"}
        if current != expected:
            return {"status": "refused", "reason": "changed after review"}
        try:
            if _changed_since_review(parent, name, expected, item.get("modifiedAt"), anchor.root_device):
                return {"status": "refused", "reason": "something was written inside this folder after review"}
        except OSError:
            return {"status": "refused", "reason": "could not re-check the folder"}
        try:
            claim = _claim(parent, name, expected)
        except _Refused as reason:
            return {"status": "refused", "reason": str(reason)}
        except OSError:
            return {"status": "failed", "reason": "the item could not be moved safely"}
        record["progress"][item["itemId"]] = {"phase": "prepared", "claim": claim, "operation": operation}
        state.write_json(scope, plan_name, record)
        try:
            if operation == "trash":
                details = _trash_claim(anchor, parent, claim, item["path"], receipt_id, expected)
                receipts.append({"receiptId": receipt_id, "originalPath": item["path"], "kind": expected["kind"],
                                 "root": item.get("root"), "rootIdentity": item.get("rootIdentity"),
                                 "displayPath": _display_path(item),
                                 "allocatedBytes": item["allocatedBytes"], "trashedAt": _now(), **details})
                return {"status": "applied", "receiptId": receipt_id}
            _remove_claim(parent, claim, expected["kind"])
            return {"status": "applied"}
        except (_Refused, OSError) as error:
            try:
                rename_noreplace(parent, claim, parent, name)
                return {"status": "failed", "reason": f"{error if isinstance(error, _Refused) else 'operation failed'}; item left in place"}
            except OSError:
                return {"status": "failed", "reason": f"operation failed; item kept as {claim} in its folder"}
    finally:
        os.close(parent)


def _reconcile(scope: Scope, item: dict, progress: dict, receipts: list, receipt_id: str) -> dict[str, Any]:
    """Settle an item whose earlier apply was interrupted mid-action."""
    expected = item["identity"]
    if progress.get("operation") == "trash":
        try:
            files_fd, info_fd = _open_trash()
            try:
                trashed = identity(stat_at(files_fd, receipt_id))
            finally:
                os.close(files_fd)
                os.close(info_fd)
            if (trashed["device"], trashed["inode"]) == (expected["device"], expected["inode"]):
                if not any(row.get("receiptId") == receipt_id for row in receipts):
                    receipts.append({"receiptId": receipt_id, "originalPath": item["path"], "kind": expected["kind"],
                                     "root": item.get("root"), "rootIdentity": item.get("rootIdentity"), "displayPath": _display_path(item),
                                     "allocatedBytes": item["allocatedBytes"], "trashedAt": _now(), "trashedIdentity": trashed,
                                     "trashInfoIdentity": None, "reconciled": True})
                return {"status": "applied", "receiptId": receipt_id, "reconciled": True}
        except OSError:
            pass
    try:
        parent, name = open_parent(_anchor(scope, item), item["path"])
    except OSError:
        return {"status": "unknown", "reason": "interrupted; outcome could not be confirmed"}
    try:
        claim = progress.get("claim")
        try:
            claimed = identity(stat_at(parent, claim)) if isinstance(claim, str) else None
        except OSError:
            claimed = None
        if claimed == expected:
            try:
                rename_noreplace(parent, claim, parent, name)
                return {"status": "refused", "reason": "interrupted before completion; item restored in place"}
            except OSError:
                return {"status": "unknown", "reason": f"interrupted; item kept as {claim}"}
        return {"status": "unknown", "reason": "interrupted; outcome could not be confirmed"}
    finally:
        os.close(parent)


# ----- Hermes Trash -------------------------------------------------------------

def _read_receipts(scope: Scope) -> list[dict[str, Any]]:
    value = state.read_json(scope, _RECEIPTS, [])
    return [row for row in value if isinstance(row, dict) and isinstance(row.get("receiptId"), str)] if isinstance(value, list) else []


def _receipt_status(files_fd: int | None, row: dict[str, Any]) -> str:
    if files_fd is None:
        return "unavailable"
    name = row["receiptId"]
    if Path(name).name != name or name.startswith("."):
        return "unsafe"
    try:
        current = identity(stat_at(files_fd, name))
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unavailable"
    expected = row.get("trashedIdentity") or {}
    if (current["device"], current["inode"], current["kind"]) == (expected.get("device"), expected.get("inode"), expected.get("kind")):
        return "present"
    return "replaced"


@state.serialized
def list_trash(scope: Scope) -> dict[str, Any]:
    receipts = _read_receipts(scope)
    try:
        files_fd, info_fd = _open_trash()
    except OSError:
        files_fd = info_fd = None
    try:
        items = [{**row, "status": _receipt_status(files_fd, row)} for row in receipts]
    finally:
        if files_fd is not None:
            os.close(files_fd)
            os.close(info_fd)
    present = [row for row in items if row["status"] == "present"]
    return response(scope.binding, items=items, presentBytes=sum(int(row.get("allocatedBytes") or 0) for row in present),
                    support=trash_support(scope),
                    note="Only items this Storage plugin moved are listed. Your other Trash contents are never shown or emptied.")


def _selected_receipts(scope: Scope, body: dict[str, Any]) -> tuple[list[dict], list[dict]] | None:
    ids = body.get("receiptIds")
    if not isinstance(ids, list) or not ids or len(ids) > 200 or not all(isinstance(item, str) for item in ids):
        return None
    receipts = _read_receipts(scope)
    wanted = set(ids)
    return [row for row in receipts if row["receiptId"] in wanted], receipts


def _trash_operation(scope: Scope, body: dict[str, Any], operation: Callable[[int, int, dict], dict]) -> tuple[int, dict[str, Any]]:
    if body.get("confirmation", {}).get("acknowledged") is not True:
        return 409, error_body("confirmation_required", "confirm first", retryable=False, binding=scope.binding)
    selection = _selected_receipts(scope, body)
    if selection is None:
        return 400, error_body("invalid_selection", "select Hermes Trash items", retryable=False, binding=scope.binding)
    selected, receipts = selection
    try:
        files_fd, info_fd = _open_trash()
    except OSError:
        return 503, error_body("trash_unavailable", "the Trash folder is unavailable", retryable=True, binding=scope.binding)
    free_before = volume_free(scope)
    outcomes = []
    done: set[str] = set()
    try:
        for row in selected:
            status = _receipt_status(files_fd, row)
            if status == "missing":
                outcomes.append({"receiptId": row["receiptId"], "status": "missing", "reason": "no longer in the Trash"})
                done.add(row["receiptId"])
                continue
            if status != "present":
                outcomes.append({"receiptId": row["receiptId"], "status": "refused", "reason": "the Trash entry was replaced or is unsafe"})
                continue
            try:
                outcome = operation(files_fd, info_fd, row)
            except (_Refused, OSError) as error:
                outcome = {"status": "refused" if isinstance(error, _Refused) else "failed", "reason": str(error) if isinstance(error, _Refused) else "operation failed"}
            outcome["receiptId"] = row["receiptId"]
            outcomes.append(outcome)
            if outcome["status"] == "applied":
                done.add(row["receiptId"])
                _remove_trashinfo(info_fd, row)
    finally:
        os.close(files_fd)
        os.close(info_fd)
    remaining = [row for row in receipts if row["receiptId"] not in done]
    state.write_json(scope, _RECEIPTS, remaining)
    free_after = volume_free(scope)
    return 200, response(scope.binding, outcomes=outcomes,
                         volumeFreeDelta=(free_after - free_before) if free_before is not None and free_after is not None else None)


def _remove_trashinfo(info_fd: int, row: dict[str, Any]) -> None:
    name = f"{row['receiptId']}.trashinfo"
    try:
        current = identity(stat_at(info_fd, name))
        expected = row.get("trashInfoIdentity")
        if expected is None or (current["device"], current["inode"]) == (expected.get("device"), expected.get("inode")):
            os.unlink(name, dir_fd=info_fd)
    except OSError:
        pass


@state.serialized
def remove_from_trash(scope: Scope, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    def operation(files_fd: int, info_fd: int, row: dict) -> dict:
        expected = row["trashedIdentity"]
        claim = _claim(files_fd, row["receiptId"], identity(stat_at(files_fd, row["receiptId"])))
        if identity(stat_at(files_fd, claim))["inode"] != expected["inode"]:
            raise _Refused("replaced during removal")
        _remove_claim(files_fd, claim, expected["kind"])
        return {"status": "applied"}
    return _trash_operation(scope, body, operation)


@state.serialized
def restore_from_trash(scope: Scope, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    def operation(files_fd: int, info_fd: int, row: dict) -> dict:
        try:
            parent, name = open_parent(_anchor(scope, row), row["originalPath"])
        except OSError:
            raise _Refused("its original folder no longer exists or was replaced")
        try:
            try:
                stat_at(parent, name)
                raise _Refused("something new already exists at the original location")
            except FileNotFoundError:
                pass
            if os.fstat(parent).st_dev != os.fstat(files_fd).st_dev:
                raise _Refused("the original folder is on another volume")
            rename_noreplace(files_fd, row["receiptId"], parent, name)
            return {"status": "applied", "restoredPath": row.get("displayPath") or row["originalPath"]}
        finally:
            os.close(parent)
    return _trash_operation(scope, body, operation)
