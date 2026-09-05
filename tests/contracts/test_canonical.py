import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

try:
    from localcareerimpact.contracts.base import StrictContract
    from localcareerimpact.contracts.canonical import (
        assert_no_float,
        canonical_contract_hash,
        canonical_json_bytes,
        sha256_uri,
    )
except ModuleNotFoundError:
    pytest.fail("PHASE0_RED_T02_CANONICAL_CONTRACTS_ABSENT", pytrace=False)


class Payload(StrictContract):
    text: str
    values: list[int] | None = None
    metadata: dict[str, object] | None = None


def test_utf8_is_preserved_and_json_is_compact_sorted_and_has_no_newline():
    payload = Payload(text="你好", metadata={"z": True, "a": "é"})
    assert canonical_json_bytes(payload) == '{"metadata":{"a":"é","z":true},"text":"你好","values":null}'.encode("utf-8")


def test_duplicate_logical_content_has_duplicate_canonical_bytes_and_hash():
    first = Payload(text="same", metadata={"b": 2, "a": 1})
    second = Payload(text="same", metadata={"a": 1, "b": 2})
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert canonical_contract_hash(first) == canonical_contract_hash(second)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 1.5])
def test_float_values_are_rejected(value):
    with pytest.raises((TypeError, ValueError)):
        assert_no_float({"value": value})
    with pytest.raises((TypeError, ValueError)):
        canonical_json_bytes({"value": value})


def test_sha256_uri_is_lowercase_exact_format():
    payload = b"hello"
    assert sha256_uri(payload) == "sha256:" + hashlib.sha256(payload).hexdigest()
    assert len(sha256_uri(payload)) == len("sha256:") + 64


def test_contract_hash_is_hash_of_canonical_json_bytes():
    payload = Payload(text="hash me", values=[1, 2, 3])
    assert canonical_contract_hash(payload) == sha256_uri(canonical_json_bytes(payload))


def test_contract_hash_requires_a_strict_contract():
    with pytest.raises(TypeError):
        canonical_contract_hash({"text": "not parsed"})


def test_canonical_json_bytes_rejects_non_contract_float_and_nonfinite_json():
    with pytest.raises((TypeError, ValueError)):
        canonical_json_bytes({"value": 0.25})
    with pytest.raises((TypeError, ValueError)):
        json.dumps({"value": float("nan")}, allow_nan=False)
