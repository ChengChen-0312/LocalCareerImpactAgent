import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

try:
    from localcareerimpact.contracts.base import StrictContract
except ModuleNotFoundError:
    pytest.fail("PHASE0_RED_T02_CANONICAL_CONTRACTS_ABSENT", pytrace=False)


class Profile(StrictContract):
    name: str
    age: int


def test_unknown_fields_are_rejected():
    with pytest.raises(ValueError):
        Profile(name="Ada", age=37, extra="nope")


def test_string_to_integer_coercion_is_rejected():
    with pytest.raises(ValueError):
        Profile(name="Ada", age="37")


def test_contract_is_immutable():
    profile = Profile(name="Ada", age=37)
    with pytest.raises((TypeError, ValueError)):
        profile.age = 38
