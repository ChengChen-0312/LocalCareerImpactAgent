import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

try:
    from localcareerimpact.contracts.canonical import canonical_json_bytes, sha256_uri
    from localcareerimpact.contracts.factpack import SnapshotRegistryView
    from localcareerimpact.contracts.query import bind_evidence_query, parse_evidence_query
except ModuleNotFoundError:
    pytest.fail("PHASE0_RED_T03_FACTPACK_CONTRACTS_ABSENT", pytrace=False)

from ._task3_helpers import digest, lookup_payload, registry_bundle, registry_view


@pytest.mark.parametrize(
    ("intent", "updates"),
    [
        ("lookup", {}),
        ("compare", {"occupation": {"classification": "OSCA", "classification_version": "v1", "codes": ("261313", "261314")}}),
        ("trend", {"period": {"mode": "range", "exact": None, "from": "2020", "to": "2025", "as_of": "2026-01-01"}}),
        ("tasks", {"metrics": (), "geography": None, "period": None, "requested_grain": None}),
        ("explain_method", {"occupation": None, "metrics": (), "geography": None, "period": None, "requested_grain": None, "method_id": "method-1"}),
    ],
)
def test_five_intents_accept_only_their_required_matrix(intent, updates):
    assert parse_evidence_query(lookup_payload(intent=intent, **updates)).intent == intent


@pytest.mark.parametrize(
    "payload",
    [
        lookup_payload(snapshot_id=digest("snapshot")),
        lookup_payload(registry_bundle_hash=digest("registry")),
        lookup_payload(raw_alias="developer"),
        lookup_payload(ui_language="zh"),
        lookup_payload(occupation=None),
        lookup_payload(intent="compare"),
        lookup_payload(intent="tasks"),
        lookup_payload(intent="explain_method"),
    ],
)
def test_query_rejects_extra_and_matrix_violations(payload):
    with pytest.raises((ValidationError, ValueError)):
        parse_evidence_query(payload)


@pytest.mark.parametrize(
    "period",
    [
        {"mode": "latest_common", "exact": "2025", "from": None, "to": None, "as_of": "2026-01-01"},
        {"mode": "exact", "exact": None, "from": None, "to": None, "as_of": "2026-01-01"},
        {"mode": "range", "exact": None, "from": "2025", "to": "2020", "as_of": "2026-01-01"},
    ],
)
def test_period_cross_field_rules(period):
    with pytest.raises((ValidationError, ValueError)):
        parse_evidence_query(lookup_payload(period=period))


def test_binding_resolves_snapshot_registry_without_extending_query_bytes():
    query = parse_evidence_query(lookup_payload())
    before = canonical_json_bytes(query)
    bound = bind_evidence_query(query, registry_view())
    assert canonical_json_bytes(bound.query) == before
    assert set(query.model_dump()) == {"intent", "occupation", "metrics", "geography", "period", "requested_grain", "method_id", "evidence_mode"}


def test_canonical_query_uses_the_approved_from_field_name():
    query = parse_evidence_query(lookup_payload(period={"mode": "range", "exact": None, "from": "2020", "to": "2025", "as_of": "2026-01-01"}))
    period = json.loads(canonical_json_bytes(query))["period"]
    assert "from" in period
    assert "from_" not in period


def test_every_public_json_serializer_uses_from_and_parser_rejects_internal_alias():
    query = parse_evidence_query(lookup_payload(period={"mode": "range", "exact": None, "from": "2020", "to": "2025", "as_of": "2026-01-01"}))
    assert "\"from\":" in query.model_dump_json()
    assert "\"from_\":" not in query.model_dump_json()
    bad = lookup_payload(period={"mode": "range", "exact": None, "from_": "2020", "to": "2025", "as_of": "2026-01-01"})
    with pytest.raises((ValidationError, ValueError)):
        parse_evidence_query(bad)


def test_binding_requires_the_real_snapshot_registry_view():
    class DuckSnapshot:
        snapshot_id = digest("snapshot")
        registry_bundle_hash = digest("registry")

        def assert_query_resolves(self, query):
            return None

    with pytest.raises(TypeError):
        bind_evidence_query(parse_evidence_query(lookup_payload()), DuckSnapshot())


@pytest.mark.parametrize(
    "change",
    [
        {"occupation": {"classification": "OSCA", "classification_version": "old", "codes": ("261313",)}},
        {"occupation": {"classification": "OSCA", "classification_version": "v1", "codes": ("unknown",)}},
        {"metrics": ("unknown",)},
        {"geography": {"level": "STE", "code": "XX"}},
        {"requested_grain": "unknown"},
    ],
)
def test_binding_rejects_unknown_registry_ids(change):
    with pytest.raises(ValueError):
        bind_evidence_query(parse_evidence_query(lookup_payload(**change)), registry_view())


def test_snapshot_registry_recomputes_its_registry_hash():
    registries = registry_bundle()
    with pytest.raises((ValidationError, ValueError)):
        SnapshotRegistryView(
            snapshot_id=digest("snapshot"),
            registry_bundle_hash=digest("wrong"),
            registry_bundle=registries,
            occupation_codes_by_classification_version=(("OSCA", "v1", ("261313",)),),
            method_ids=("method-1",),
        )


def test_snapshot_registry_rejects_occupation_map_key_absent_from_registry_bundle():
    registries = registry_bundle()
    with pytest.raises((ValidationError, ValueError), match="classification"):
        SnapshotRegistryView(
            snapshot_id=digest("snapshot"),
            registry_bundle_hash=sha256_uri(canonical_json_bytes(registries)),
            registry_bundle=registries,
            occupation_codes_by_classification_version=(("ANZSCO", "not-in-bundle", ("999999",)),),
            method_ids=("method-1",),
        )


@pytest.mark.parametrize(
    ("geography", "valid"),
    [
        ({"level": "AUS", "code": "AUS"}, True),
        ({"level": "AUS", "code": "VIC"}, False),
        ({"level": "STE", "code": "VIC"}, False),
        ({"level": "SA4", "code": "206"}, False),
        ({"level": None, "code": None}, False),
    ],
)
def test_geography_pairs_are_closed_and_snapshot_resolved(geography, valid):
    if valid:
        query = parse_evidence_query(lookup_payload(geography=geography))
        bind_evidence_query(query, registry_view())
    else:
        with pytest.raises((ValidationError, ValueError)):
            query = parse_evidence_query(lookup_payload(geography=geography))
            bind_evidence_query(query, registry_view())


@pytest.mark.parametrize(
    "period",
    [
        {"mode": "latest_common", "exact": None, "from": None, "to": None, "as_of": "2026-01-01"},
        {"mode": "exact", "exact": "2025-12", "from": None, "to": None, "as_of": "2026-01-01"},
        {"mode": "range", "exact": None, "from": "2020", "to": "2025", "as_of": "2026-01-01"},
    ],
)
def test_each_period_mode_accepts_exactly_its_fields(period):
    assert parse_evidence_query(lookup_payload(period=period)).period.mode == period["mode"]
