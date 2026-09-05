import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from .base import StrictContract


def assert_no_float(value: Any) -> None:
    """Reject floats recursively, including non-finite values."""
    if isinstance(value, float):
        raise TypeError("float values are not allowed in canonical contracts")
    if isinstance(value, Mapping):
        for key, item in value.items():
            assert_no_float(key)
            assert_no_float(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_no_float(item)


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON-compatible values as deterministic UTF-8 JSON bytes."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=False)
    assert_no_float(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_uri(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def canonical_contract_hash(value: StrictContract) -> str:
    if not isinstance(value, StrictContract):
        raise TypeError("value must be a parsed StrictContract")
    return sha256_uri(canonical_json_bytes(value))
