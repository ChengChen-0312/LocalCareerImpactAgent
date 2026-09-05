from __future__ import annotations

from localcareerimpact.contracts.canonical import canonical_contract_hash, canonical_json_bytes, sha256_uri
from localcareerimpact.contracts.factpack import (
    Caveat,
    ClassificationEntry,
    Fact,
    FactPack,
    FormulaEntry,
    GeographyEntry,
    GrainEntry,
    Lineage,
    MessageTemplateEntry,
    Method,
    MetricEntry,
    Passage,
    ProvenanceInput,
    QueryRecord,
    QueryTemplateEntry,
    RegistryBundle,
    Scope,
    SnapshotRegistryView,
    Source,
    TransformEntry,
    compute_factpack_hash,
)
from localcareerimpact.contracts.query import (
    FactToolResult,
    MethodToolResult,
    PassageToolResult,
    QueryResultClosureV1,
    QueryResultDigestInput,
    QueryResultHashPreimage,
    bind_evidence_query,
    parse_evidence_query,
)
from localcareerimpact.contracts.runbundle import ConfirmedCaseProfile, EvidenceContextV1


def digest(label: str) -> str:
    return sha256_uri(label.encode())


def lookup_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "intent": "lookup",
        "occupation": {
            "classification": "OSCA",
            "classification_version": "v1",
            "codes": ("261313",),
        },
        "metrics": ("automation_score",),
        "geography": {"level": "AUS", "code": "AUS"},
        "period": {"mode": "latest_common", "exact": None, "from": None, "to": None, "as_of": "2026-01-01"},
        "requested_grain": "occupation",
        "method_id": None,
        "evidence_mode": "strict",
    }
    payload.update(updates)
    return payload


def registry_bundle() -> RegistryBundle:
    return RegistryBundle(
        query_templates=(QueryTemplateEntry(template_id="get_exposure_v1", schema_hash=digest("template")),),
        classifications=(ClassificationEntry(classification="OSCA", version="v1", schema_hash=digest("class")),),
        metrics=(MetricEntry(metric_id="automation_score", unit_id="ratio_0_1", definition="fixed"),),
        geographies=(GeographyEntry(level="AUS", code="AUS", name="Australia"),),
        grains=(GrainEntry(grain_id="occupation", definition="occupation grain"),),
        transforms=(TransformEntry(transform_id="identity-v1", code_hash=digest("code"), params_hash=digest("params")),),
        formulas=(FormulaEntry(formula_id="identity", version="v1", expression="P1"),),
        message_templates=(MessageTemplateEntry(template_id="unavailable-v1", en="Unavailable", zh="不可用"),),
    )


def registry_view() -> SnapshotRegistryView:
    registries = registry_bundle()
    return SnapshotRegistryView(
        snapshot_id=digest("snapshot"),
        registry_bundle_hash=sha256_uri(canonical_json_bytes(registries)),
        registry_bundle=registries,
        occupation_codes_by_classification_version=(("OSCA", "v1", ("261313", "261314")),),
        method_ids=("method-1",),
    )


def build_valid_graph() -> tuple[FactPack, QueryResultClosureV1, EvidenceContextV1]:
    view = registry_view()
    query = parse_evidence_query(lookup_payload())
    bound = bind_evidence_query(query, view)
    scope = Scope(
        classification="OSCA",
        classification_version="v1",
        occupation_codes=("261313",),
        geography_level="AUS",
        geography_code="AUS",
        grain="occupation",
    )
    source = Source(
        source_id="S1",
        title="Official source",
        publisher="Publisher",
        source_uri="urn:source:1",
        release_date="2025-12-01",
        retrieved_at="2026-01-01T00:00:00Z",
        file_sha256=digest("source-file"),
        licence="open",
        jurisdiction="AU",
        authority_class="official",
    )
    passage = Passage(
        evidence_id="D1",
        chunk_id="chunk-1",
        source_id="S1",
        file_sha256=source.file_sha256,
        chunk_sha256=digest("chunk"),
        page=1,
        section="Method",
        release_date="2025-12-01",
        licence="open",
        jurisdiction="AU",
        authority_class="official",
        text="Untrusted evidence text.",
    )
    method = Method(
        method_id="method-1",
        version="v1",
        description="Published method",
        source_ids=("S1",),
        evidence_ids=("D1",),
        caveat_ids=(),
    )
    fact = Fact(
        fact_id="F1",
        metric="automation_score",
        value="0",
        unit="ratio_0_1",
        source_nature="official_published",
        estimate_nature="observed",
        availability_status="available",
        requested_scope=scope,
        answered_scope=scope,
        reference_period="2025",
        release_date="2025-12-01",
        retrieved_at="2026-01-01T00:00:00Z",
        crosswalk_quality=None,
        entity_resolution_status="resolved",
        crosswalk_source_id=None,
        source_ids=("S1",),
        provenance_inputs=(
            ProvenanceInput(
                input_id="P1",
                source_id="S1",
                source_file_sha256=source.file_sha256,
                input_value="0",
                input_unit="ratio_0_1",
                lineage=Lineage(lineage_id="L1", sheet="Sheet1", row="2", cell="B2", page=None, transform_chain=("identity-v1",)),
            ),
        ),
        derivation=None,
        method_id="method-1",
        caveat_ids=(),
        scope_note=None,
        unavailable_reason=None,
    )
    result = QueryResultDigestInput(
        query_id="Q1",
        template_id="get_exposure_v1",
        requested_scope=scope,
        answered_scope=scope,
        records=(
            PassageToolResult(kind="passage", evidence_id="D1", chunk_id="chunk-1", rrf_rank=1, payload_hash=sha256_uri(canonical_json_bytes(passage))),
            MethodToolResult(kind="method", method_id="method-1", payload_hash=sha256_uri(canonical_json_bytes(method))),
            FactToolResult(kind="fact", fact_id="F1", payload_hash=sha256_uri(canonical_json_bytes(fact))),
        ),
    )
    result_hash = canonical_contract_hash(QueryResultHashPreimage(bound_query=bound, result=result))
    query_record = QueryRecord(query_id="Q1", template_id="get_exposure_v1", normalized_params=query, result_hash=result_hash)
    unhashed = FactPack(
        snapshot_id=view.snapshot_id,
        factpack_hash=digest("placeholder"),
        queries=(query_record,),
        facts=(fact,),
        passages=(passage,),
        tasks=(),
        methods=(method,),
        caveats=(),
        sources=(source,),
        registries=view.registry_bundle,
        gaps=(),
    )
    pack = unhashed.model_copy(update={"factpack_hash": compute_factpack_hash(unhashed)})
    closure = QueryResultClosureV1(
        snapshot_id=view.snapshot_id,
        registry_bundle_hash=view.registry_bundle_hash,
        results=(result,),
    )
    profile = ConfirmedCaseProfile(
        profile_ref="service-hmac:" + "a" * 64,
        region="VIC",
        confirmed_occupations=(),
        confirmed_responsibilities=(),
        confirmed_skills=(),
        user_goals=(),
    )
    context = EvidenceContextV1(
        run_id="run-1",
        tenant_id="tenant-1",
        policy_version="policy-v1",
        confirmed_case_profile=profile,
        fact_pack=pack,
        query_result_closure_hash=sha256_uri(canonical_json_bytes(closure)),
    )
    return pack, closure, context
