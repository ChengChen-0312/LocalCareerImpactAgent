"""Shared contract primitives for LocalCareerImpactAgent."""

from .base import StrictContract
from .canonical import assert_no_float, canonical_contract_hash, canonical_json_bytes, sha256_uri

__all__ = [
    "StrictContract",
    "assert_no_float",
    "canonical_contract_hash",
    "canonical_json_bytes",
    "sha256_uri",
]
