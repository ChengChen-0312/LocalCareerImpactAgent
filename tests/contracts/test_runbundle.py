import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

try:
    from localcareerimpact.contracts.canonical import canonical_contract_hash, canonical_json_bytes, sha256_uri
    from localcareerimpact.contracts.factpack import FactPack, QueryRecord, compute_factpack_hash
    from localcareerimpact.contracts.query import QueryResultHashPreimage, SnapshotBoundQuery, parse_evidence_query
    from localcareerimpact.contracts.runbundle import (
        RunBundle,
        ValidatedEvidenceContext,
        parse_evidence_context,
        project_runbundle,
        validate_evidence_context,
    )
except ModuleNotFoundError:
    pytest.fail("PHASE0_RED_T03_FACTPACK_CONTRACTS_ABSENT", pytrace=False)

from ._task3_helpers import build_valid_graph, lookup_payload, registry_view


def test_evidence_context_validates_paired_closure_and_has_external_hash():
    pack, closure, context = build_valid_graph()
    validated = validate_evidence_context(context, closure, registry_view())
    parsed = parse_evidence_context(context.model_dump())
    assert isinstance(validated, ValidatedEvidenceContext)
    assert canonical_contract_hash(parsed) == canonical_contract_hash(context)


def test_three_runbundle_projections_differ_only_in_authority():
    _, closure, context = build_valid_graph()
    validated = validate_evidence_context(context, closure, registry_view())
    bundles = [project_runbundle(validated, authority) for authority in ("DRAFT", "ADVISORY_ONLY", "DECIDER_CANDIDATE")]
    dumped = [bundle.model_dump() for bundle in bundles]
    for value in dumped:
        value.pop("authority")
    assert dumped[0] == dumped[1] == dumped[2]


def test_unpaired_evidence_context_cannot_be_projected():
    _, closure, context = build_valid_graph()
    validated = validate_evidence_context(context, closure, registry_view())
    assigned = context
    candidates = (
        assigned,
        context.model_copy(),
        type(context).model_validate(context.model_dump()),
        parse_evidence_context(json.loads(canonical_json_bytes(context))),
    )
    for candidate in candidates:
        with pytest.raises(TypeError):
            project_runbundle(candidate, "DRAFT")
    with pytest.raises((AttributeError, TypeError)):
        validated.context = context.model_copy()


def test_dataclass_replacement_cannot_forge_projection_authority():
    _, closure, context = build_valid_graph()
    validated = validate_evidence_context(context, closure, registry_view())
    bad_context = context.model_copy(update={"query_result_closure_hash": "sha256:" + "f" * 64})
    with pytest.raises(TypeError):
        replace(validated, context=bad_context)


def test_validated_wrapper_cannot_replace_run_tenant_policy_or_profile():
    _, closure, context = build_valid_graph()
    validated = validate_evidence_context(context, closure, registry_view())
    replacements = (
        {"run_id": "other-run"},
        {"tenant_id": "other-tenant"},
        {"policy_version": "other-policy"},
        {"confirmed_case_profile": context.confirmed_case_profile.model_copy(update={"region": "NSW"})},
    )
    for update in replacements:
        substituted = context.model_copy(update=update)
        with pytest.raises(TypeError):
            replace(validated, context=substituted)


def test_readable_seal_cannot_rewrap_substituted_context_for_projection():
    _, closure, context = build_valid_graph()
    validated = validate_evidence_context(context, closure, registry_view())
    substituted = context.model_copy(update={"tenant_id": "other-tenant"})
    with pytest.raises((AttributeError, TypeError, ValueError)):
        forged = ValidatedEvidenceContext(substituted, validated.binding, validated._seal)
        project_runbundle(forged, "DRAFT")


def test_hash_equality_spoofing_subclass_cannot_enter_identity_registry():
    _, closure, context = build_valid_graph()
    validated = validate_evidence_context(context, closure, registry_view())
    substituted = context.model_copy(update={"tenant_id": "spoofed-tenant"})

    class EqualitySpoof(ValidatedEvidenceContext):
        __slots__ = ()

        def __new__(cls):
            return object.__new__(cls)

        def __hash__(self):
            return hash(validated)

        def __eq__(self, other):
            return True

    spoof = EqualitySpoof()
    object.__setattr__(spoof, "_context", substituted)
    object.__setattr__(spoof, "_binding", validated.binding)
    object.__setattr__(spoof, "_canonical_context_bytes", canonical_json_bytes(substituted))
    with pytest.raises(TypeError):
        project_runbundle(spoof, "DRAFT")


def test_runbundle_rejects_mismatched_embedded_hash_and_sidecars():
    _, closure, context = build_valid_graph()
    bundle = project_runbundle(validate_evidence_context(context, closure, registry_view()), "DRAFT")
    bad = bundle.model_dump()
    bad["fact_pack"]["factpack_hash"] = "sha256:" + "f" * 64
    with pytest.raises((ValidationError, ValueError)):
        RunBundle.model_validate(bad)
    for extra in ("stage", "role", "output_kind", "evidence_context_hash", "query_result_closure"):
        with pytest.raises((ValidationError, ValueError)):
            RunBundle.model_validate({**bundle.model_dump(), extra: "forbidden"})


def test_confirmed_profile_is_outside_factpack():
    pack, _, context = build_valid_graph()
    assert "confirmed_case_profile" not in type(pack).model_fields
    assert context.confirmed_case_profile.profile_ref.startswith("service-hmac:")


def test_evidence_context_rejects_wrong_closure_hash_and_wrong_pair():
    _, closure, context = build_valid_graph()
    with pytest.raises(ValueError):
        validate_evidence_context(context.model_copy(update={"query_result_closure_hash": "sha256:" + "0" * 64}), closure, registry_view())
    with pytest.raises(ValueError):
        validate_evidence_context(context, closure.model_copy(update={"snapshot_id": "sha256:" + "1" * 64}), registry_view())


def test_persisted_unknown_occupation_cannot_enter_validated_authority_path():
    pack, closure, context = build_valid_graph()
    unknown = parse_evidence_query(lookup_payload(occupation={"classification": "OSCA", "classification_version": "v1", "codes": ("UNKNOWN",)}))
    direct_bound = SnapshotBoundQuery(snapshot_id=pack.snapshot_id, registry_bundle_hash=closure.registry_bundle_hash, query=unknown)
    result_hash = canonical_contract_hash(QueryResultHashPreimage(bound_query=direct_bound, result=closure.results[0]))
    query = QueryRecord(query_id="Q1", template_id="get_exposure_v1", normalized_params=unknown, result_hash=result_hash)
    unhashed = FactPack.model_validate({**pack.model_dump(), "queries": (query,)})
    bad_pack = unhashed.model_copy(update={"factpack_hash": compute_factpack_hash(unhashed)})
    bad_context = type(context)(**{**context.model_dump(), "fact_pack": bad_pack})
    with pytest.raises(ValueError, match="occupation"):
        validate_evidence_context(bad_context, closure, registry_view())
