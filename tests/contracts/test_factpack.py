import random
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

try:
    from localcareerimpact.contracts.factpack import Fact, FactPack, compute_factpack_hash, parse_factpack
except ModuleNotFoundError:
    pytest.fail("PHASE0_RED_T03_FACTPACK_CONTRACTS_ABSENT", pytrace=False)

from ._task3_helpers import build_valid_graph, digest


def test_zero_is_available_and_null_is_not_zero():
    pack, _, _ = build_valid_graph()
    assert pack.facts[0].value == "0"
    with pytest.raises((ValidationError, ValueError)):
        Fact(**{**pack.facts[0].model_dump(), "value": None})


@pytest.mark.parametrize("status", ["suppressed", "unavailable", "not_applicable", "quarantined"])
def test_non_available_status_requires_null(status):
    pack, _, _ = build_valid_graph()
    with pytest.raises((ValidationError, ValueError)):
        Fact(**{**pack.facts[0].model_dump(), "availability_status": status})


@pytest.mark.parametrize("resolution", ["ambiguous", "unresolved"])
def test_unresolved_entity_cannot_produce_available_fact(resolution):
    pack, _, _ = build_valid_graph()
    with pytest.raises((ValidationError, ValueError)):
        Fact(**{**pack.facts[0].model_dump(), "entity_resolution_status": resolution})


def test_quarantined_crosswalk_forces_quarantined_availability():
    pack, _, _ = build_valid_graph()
    with pytest.raises((ValidationError, ValueError)):
        Fact(**{**pack.facts[0].model_dump(), "crosswalk_quality": "quarantined", "crosswalk_source_id": "S1"})


def test_factpack_hash_excludes_only_root_hash_and_parse_verifies_it():
    pack, _, _ = build_valid_graph()
    assert parse_factpack(pack.model_dump()).factpack_hash == pack.factpack_hash
    tampered = pack.model_dump()
    tampered["facts"][0]["scope_note"] = "tampered"
    with pytest.raises(ValueError):
        parse_factpack(tampered)


def test_nested_models_and_collections_are_deeply_immutable():
    pack, _, _ = build_valid_graph()
    with pytest.raises((TypeError, ValidationError)):
        pack.facts[0].value = "1"
    with pytest.raises((TypeError, AttributeError)):
        pack.facts.append(pack.facts[0])


def test_reorderable_arrays_canonicalize_and_hash_identically_for_100_permutations():
    pack, _, _ = build_valid_graph()
    payload = pack.model_dump()
    scope = pack.facts[0].requested_scope.model_dump()
    lineage = pack.facts[0].provenance_inputs[0].lineage.model_dump()
    payload["queries"] = (*payload["queries"], {**payload["queries"][0], "query_id": "Q2", "result_hash": digest("result-2")})
    payload["facts"] = (*payload["facts"], {**payload["facts"][0], "fact_id": "F2", "reference_period": "2024"})
    payload["tasks"] = (
        {"task_id": "T1", "classification": "OSCA", "classification_version": "v1", "occupation_code": "261313", "task_text": "Task one", "source_id": "S1", "source_file_sha256": pack.sources[0].file_sha256, "lineage": lineage, "caveat_ids": ()},
        {"task_id": "T2", "classification": "OSCA", "classification_version": "v1", "occupation_code": "261313", "task_text": "Task two", "source_id": "S1", "source_file_sha256": pack.sources[0].file_sha256, "lineage": {**lineage, "lineage_id": "L2"}, "caveat_ids": ()},
    )
    payload["methods"] = (*payload["methods"], {**payload["methods"][0], "method_id": "method-2", "description": "Second method"})
    payload["caveats"] = (
        {"caveat_id": "C1", "code": "first", "severity": "required", "renderer_template_en": "First", "renderer_template_zh": "一", "applies_to_refs": ("F1",)},
        {"caveat_id": "C2", "code": "second", "severity": "contextual", "renderer_template_en": "Second", "renderer_template_zh": "二", "applies_to_refs": ("F2",)},
    )
    payload["sources"] = (*payload["sources"], {**payload["sources"][0], "source_id": "S2", "source_uri": "urn:source:2", "file_sha256": digest("source-2")})
    payload["gaps"] = (
        {"gap_id": "G1", "query_id": "Q1", "reason_code": "unavailable", "requested_scope": scope, "answered_scope": None, "message_template_id": "unavailable-v1"},
        {"gap_id": "G2", "query_id": "Q2", "reason_code": "suppressed", "requested_scope": scope, "answered_scope": None, "message_template_id": "unavailable-v1"},
    )
    registry_additions = {
        "query_templates": {"template_id": "second-template", "schema_hash": digest("template-2")},
        "classifications": {"classification": "ANZSCO", "version": "v2", "schema_hash": digest("class-2")},
        "metrics": {"metric_id": "vacancy_count", "unit_id": "count", "definition": "count"},
        "geographies": {"level": "STE", "code": "VIC", "name": "Victoria"},
        "grains": {"grain_id": "industry", "definition": "industry grain"},
        "transforms": {"transform_id": "second-transform", "code_hash": digest("code-2"), "params_hash": digest("params-2")},
        "formulas": {"formula_id": "second-formula", "version": "v1", "expression": "P1 + 1"},
        "message_templates": {"template_id": "second-message", "en": "Second", "zh": "二"},
    }
    for name, addition in registry_additions.items():
        payload["registries"][name] = (*payload["registries"][name], addition)
    canonical_pack = FactPack.model_validate(payload)
    baseline = compute_factpack_hash(canonical_pack)
    payload = canonical_pack.model_dump()
    rng = random.Random(3)
    reorderable = ("queries", "facts", "tasks", "methods", "caveats", "sources", "gaps")
    for iteration in range(100):
        candidate = {**payload}
        for name in reorderable:
            values = list(payload[name])
            values.reverse() if iteration == 0 else rng.shuffle(values)
            candidate[name] = values
        for name in ("query_templates", "classifications", "metrics", "geographies", "grains", "transforms", "formulas", "message_templates"):
            values = list(payload["registries"][name])
            values.reverse() if iteration == 0 else rng.shuffle(values)
            candidate["registries"] = {**candidate["registries"], name: values}
        parsed = FactPack.model_validate(candidate)
        assert compute_factpack_hash(parsed) == baseline


@pytest.mark.parametrize("extra", ["user_id", "tenant_id", "case_ref", "ui_language", "generated_at", "runtime_clock"])
def test_factpack_forbids_user_and_runtime_fields(extra):
    pack, _, _ = build_valid_graph()
    with pytest.raises((ValidationError, ValueError)):
        FactPack.model_validate({**pack.model_dump(), extra: "forbidden"})


@pytest.mark.parametrize("value", [0, 0.0, "NaN", "Infinity", "not-decimal"])
def test_fact_values_are_strict_decimal_strings(value):
    pack, _, _ = build_valid_graph()
    with pytest.raises((ValidationError, ValueError, TypeError)):
        Fact(**{**pack.facts[0].model_dump(), "value": value})


def test_crosswalk_quality_and_source_are_both_null_or_both_non_null():
    pack, _, _ = build_valid_graph()
    for updates in ({"crosswalk_quality": "exact"}, {"crosswalk_source_id": "S1"}):
        with pytest.raises((ValidationError, ValueError)):
            Fact(**{**pack.facts[0].model_dump(), **updates})


def test_every_fact_has_provenance_and_published_values_have_method_locator_no_derivation():
    pack, _, _ = build_valid_graph()
    fact = pack.facts[0]
    for updates in (
        {"provenance_inputs": ()},
        {"method_id": None},
        {"derivation": {"formula_id": "identity", "formula_version": "v1", "input_refs": ("P1",), "decimal_context": "precision-28", "rounding_mode": "ROUND_HALF_EVEN"}},
        {"provenance_inputs": ({**fact.provenance_inputs[0].model_dump(), "lineage": {**fact.provenance_inputs[0].lineage.model_dump(), "row": None, "cell": None, "page": None}},)},
    ):
        with pytest.raises((ValidationError, ValueError)):
            Fact(**{**fact.model_dump(), **updates})


def test_official_published_modelled_is_valid():
    pack, _, _ = build_valid_graph()
    assert Fact(**{**pack.facts[0].model_dump(), "estimate_nature": "modelled"}).estimate_nature == "modelled"


def test_derived_decimal_formula_recomputes_exactly_without_implicit_quantize():
    pack, _, _ = build_valid_graph()
    fact = pack.facts[0]
    derived = Fact(**{
        **fact.model_dump(),
        "estimate_nature": "derived",
        "method_id": None,
        "value": "0",
        "derivation": {"formula_id": "identity", "formula_version": "v1", "input_refs": ("P1",), "decimal_context": "precision-28", "rounding_mode": "ROUND_HALF_EVEN"},
    })
    assert derived.value == "0"


@pytest.mark.parametrize("expression", ["abs(P1)", "P1 ** 2", "P1[0]", "P1 + 0.5", "P1.real"])
def test_derived_formula_rejects_nodes_outside_the_decimal_ast_subset(expression):
    pack, _, _ = build_valid_graph()
    fact = Fact(**{
        **pack.facts[0].model_dump(), "estimate_nature": "derived", "method_id": None,
        "derivation": {"formula_id": "identity", "formula_version": "v1", "input_refs": ("P1",), "decimal_context": "precision-28", "rounding_mode": "ROUND_HALF_EVEN"},
    })
    registries = pack.registries.model_dump()
    registries["formulas"] = ({**registries["formulas"][0], "expression": expression},)
    with pytest.raises((ValidationError, ValueError)):
        FactPack.model_validate({**pack.model_dump(), "facts": (fact.model_dump(),), "registries": registries})


def test_derived_formula_rejects_variable_set_or_exact_decimal_result_mismatch():
    pack, _, _ = build_valid_graph()
    fact = Fact(**{
        **pack.facts[0].model_dump(), "estimate_nature": "derived", "method_id": None, "value": "1",
        "derivation": {"formula_id": "identity", "formula_version": "v1", "input_refs": ("P1",), "decimal_context": "precision-28", "rounding_mode": "ROUND_HALF_EVEN"},
    })
    with pytest.raises(ValueError):
        FactPack.model_validate({**pack.model_dump(), "facts": (fact.model_dump(),)})
    with pytest.raises((ValidationError, ValueError)):
        Fact(**{**fact.model_dump(), "derivation": {**fact.derivation.model_dump(), "decimal_context": "precision-1000"}})


def test_decimal_context_rounds_the_final_identity_expression():
    pack, _, _ = build_valid_graph()
    provenance = pack.facts[0].provenance_inputs[0].model_copy(update={"input_value": "1.234"})
    fact = Fact(**{
        **pack.facts[0].model_dump(), "estimate_nature": "derived", "method_id": None,
        "value": "1.234", "provenance_inputs": (provenance,),
        "derivation": {"formula_id": "identity", "formula_version": "v1", "input_refs": ("P1",), "decimal_context": "precision-2", "rounding_mode": "ROUND_HALF_EVEN"},
    })
    with pytest.raises(ValueError):
        FactPack.model_validate({**pack.model_dump(), "facts": (fact.model_dump(),)})


def test_transform_chain_preserves_semantic_order():
    pack, _, _ = build_valid_graph()
    transforms = (
        pack.registries.transforms[0],
        type(pack.registries.transforms[0])(transform_id="second-v1", code_hash=pack.registries.transforms[0].code_hash, params_hash=pack.registries.transforms[0].params_hash),
    )
    lineage = type(pack.facts[0].provenance_inputs[0].lineage)(
        **{**pack.facts[0].provenance_inputs[0].lineage.model_dump(), "transform_chain": ("second-v1", "identity-v1")}
    )
    assert lineage.transform_chain == ("second-v1", "identity-v1")
