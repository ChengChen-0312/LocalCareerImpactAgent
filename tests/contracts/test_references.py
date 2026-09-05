import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

try:
    from localcareerimpact.contracts.canonical import canonical_contract_hash, canonical_json_bytes, sha256_uri
    from localcareerimpact.contracts.factpack import Caveat, FactPack, Gap, QueryRecord, TaskEvidence, compute_factpack_hash
    from localcareerimpact.contracts.query import (
        FactToolResult,
        GapToolResult,
        PassageToolResult,
        QueryResultClosureV1,
        QueryResultDigestInput,
        QueryResultHashPreimage,
        validate_query_result_closure,
    )
except ModuleNotFoundError:
    pytest.fail("PHASE0_RED_T03_FACTPACK_CONTRACTS_ABSENT", pytrace=False)

from ._task3_helpers import build_valid_graph, digest


def test_every_tool_payload_hash_and_query_result_hash_recomputes():
    pack, closure, _ = build_valid_graph()
    validate_query_result_closure(closure, pack)
    result = closure.results[0]
    assert result.records[0].kind == "fact"
    assert result.records[-1].kind == "passage"


def test_query_specific_passage_rank_is_contiguous_and_independent_of_aggregate_order():
    pack, closure, _ = build_valid_graph()
    passage = closure.results[0].records[-1]
    changed = passage.model_copy(update={"rrf_rank": 2})
    result = closure.results[0].model_copy(update={"records": (*closure.results[0].records[:-1], changed)})
    bad = closure.model_copy(update={"results": (result,)})
    with pytest.raises(ValueError):
        validate_query_result_closure(bad, pack)


def test_duplicate_typed_records_are_rejected_even_with_equal_content():
    _, closure, _ = build_valid_graph()
    record = closure.results[0].records[0]
    with pytest.raises((ValidationError, ValueError)):
        QueryResultDigestInput(**{**closure.results[0].model_dump(), "records": (record, record)})


def test_query_result_preimage_rejects_result_hash_field():
    pack, closure, _ = build_valid_graph()
    query = pack.queries[0]
    from localcareerimpact.contracts.query import SnapshotBoundQuery

    bound = SnapshotBoundQuery(snapshot_id=pack.snapshot_id, registry_bundle_hash=sha256_uri(canonical_json_bytes(pack.registries)), query=query.normalized_params)
    with pytest.raises((ValidationError, ValueError)):
        QueryResultHashPreimage(bound_query=bound, result=closure.results[0], result_hash=digest("forbidden"))


def test_missing_references_and_duplicate_ids_fail():
    pack, closure, _ = build_valid_graph()
    bad_fact = pack.facts[0].model_copy(update={"method_id": "missing"})
    with pytest.raises(ValueError):
        FactPack.model_validate({**pack.model_dump(), "facts": (bad_fact.model_dump(),)})
    with pytest.raises(ValueError):
        FactPack.model_validate({**pack.model_dump(), "sources": (pack.sources[0].model_dump(), pack.sources[0].model_dump())})


def test_legacy_authority_aliases_are_rejected_everywhere():
    pack, _, _ = build_valid_graph()
    with pytest.raises((ValidationError, ValueError)):
        FactPack.model_validate({**pack.model_dump(), "sources": ({**pack.sources[0].model_dump(), "authority_class": "government"},)})


def test_tampered_payload_hash_and_missing_query_closure_entry_fail():
    pack, closure, _ = build_valid_graph()
    record = closure.results[0].records[0].model_copy(update={"payload_hash": digest("tampered")})
    result = closure.results[0].model_copy(update={"records": (record, *closure.results[0].records[1:])})
    with pytest.raises(ValueError):
        validate_query_result_closure(closure.model_copy(update={"results": (result,)}), pack)
    with pytest.raises(ValueError):
        validate_query_result_closure(closure.model_copy(update={"results": ()}), pack)


def test_crosswalk_source_must_be_a_source_and_provenance_input():
    pack, _, _ = build_valid_graph()
    fact = pack.facts[0].model_copy(update={"crosswalk_quality": "exact", "crosswalk_source_id": "S1", "source_ids": ()})
    with pytest.raises(ValueError):
        FactPack.model_validate({**pack.model_dump(), "facts": (fact.model_dump(),)})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("metric", "missing"),
        ("method_id", "missing"),
        ("caveat_ids", ("missing",)),
    ],
)
def test_fact_references_resolve_inside_same_pack(field, value):
    pack, _, _ = build_valid_graph()
    fact = pack.facts[0].model_copy(update={field: value})
    with pytest.raises(ValueError):
        FactPack.model_validate({**pack.model_dump(), "facts": (fact.model_dump(),)})


def test_gap_tool_result_cannot_cross_query_ownership():
    pack, closure, _ = build_valid_graph()
    scope = pack.facts[0].requested_scope
    gap = Gap(gap_id="G1", query_id="Q1", reason_code="unavailable", requested_scope=scope, answered_scope=None, message_template_id="unavailable-v1")
    result = QueryResultDigestInput(
        query_id="Q2", template_id="get_exposure_v1", requested_scope=scope, answered_scope=None,
        records=(GapToolResult(kind="gap", gap_id="G1", payload_hash=sha256_uri(canonical_json_bytes(gap))),),
    )
    from localcareerimpact.contracts.query import SnapshotBoundQuery

    bound = SnapshotBoundQuery(snapshot_id=pack.snapshot_id, registry_bundle_hash=closure.registry_bundle_hash, query=pack.queries[0].normalized_params)
    query = QueryRecord(query_id="Q2", template_id="get_exposure_v1", normalized_params=pack.queries[0].normalized_params, result_hash=canonical_contract_hash(QueryResultHashPreimage(bound_query=bound, result=result)))
    unhashed = FactPack.model_validate({**pack.model_dump(), "queries": (pack.queries[0], query), "gaps": (gap,)})
    paired_pack = unhashed.model_copy(update={"factpack_hash": compute_factpack_hash(unhashed)})
    paired_closure = QueryResultClosureV1(snapshot_id=pack.snapshot_id, registry_bundle_hash=closure.registry_bundle_hash, results=(closure.results[0], result))
    with pytest.raises(ValueError, match="ownership"):
        validate_query_result_closure(paired_closure, paired_pack)


def test_caveat_target_ids_must_resolve_to_exactly_one_record_type():
    pack, _, _ = build_valid_graph()
    task = TaskEvidence(
        task_id="F1", classification="OSCA", classification_version="v1", occupation_code="261313",
        task_text="Colliding task", source_id="S1", source_file_sha256=pack.sources[0].file_sha256,
        lineage=pack.facts[0].provenance_inputs[0].lineage, caveat_ids=(),
    )
    caveat = Caveat(
        caveat_id="C1", code="ambiguous-ref", severity="required",
        renderer_template_en="Required", renderer_template_zh="必需", applies_to_refs=("F1",),
    )
    with pytest.raises(ValueError, match="ambiguous"):
        FactPack.model_validate({**pack.model_dump(), "tasks": (task,), "caveats": (caveat,)})
