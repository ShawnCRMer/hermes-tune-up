"""Wire-contract helpers for Storage Utility API v1."""
from dataclasses import dataclass
from typing import Any, Mapping

SCHEMA_VERSION = 1

@dataclass(frozen=True)
class Binding:
    machine_id: str
    execution_namespace: str
    profile_id: str
    root_set_id: str
    volume_ids: tuple[str, ...]
    epoch: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "machineId": self.machine_id,
            "executionNamespace": self.execution_namespace,
            "profileId": self.profile_id,
            "rootSetId": self.root_set_id,
            "volumeIds": list(self.volume_ids),
            "epoch": self.epoch,
        }

    @classmethod
    def from_value(cls, value: Mapping[str, Any]) -> "Binding":
        if not isinstance(value, Mapping):
            raise ValueError("binding must contain all required fields")
        try:
            fields = tuple(value[name] for name in ("machineId", "executionNamespace", "profileId", "rootSetId", "epoch"))
            raw_volumes = value["volumeIds"]
        except (KeyError, TypeError) as exc:
            raise ValueError("binding must contain all required fields") from exc
        if (not all(isinstance(item, str) and item for item in fields)
                or not isinstance(raw_volumes, (list, tuple))
                or not raw_volumes
                or not all(isinstance(item, str) and item for item in raw_volumes)):
            raise ValueError("binding fields must be non-empty strings")
        return cls(fields[0], fields[1], fields[2], fields[3], tuple(raw_volumes), fields[4])


def capability(status: str, reason: str | None = None) -> dict[str, str]:
    if status not in {"available", "unsupported", "denied"}:
        raise ValueError("invalid capability status")
    value = {"status": status}
    if reason:
        value["reason"] = reason
    return value


def response(binding: Binding, **values: Any) -> dict[str, Any]:
    return {"schemaVersion": SCHEMA_VERSION, "binding": binding.as_dict(), **values}


def error_body(code: str, message: str, *, retryable: bool, binding: Binding | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "error": {"code": code, "message": message, "retryable": bool(retryable)},
    }
    if binding is not None:
        body["binding"] = binding.as_dict()
    return body
