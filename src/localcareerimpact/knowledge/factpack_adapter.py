"""One honest adapter from retrieved local passages to the evidence boundary."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping
from urllib.parse import quote

from localcareerimpact.contracts.canonical import canonical_json_bytes, sha256_uri
from localcareerimpact.contracts.factpack import (
    AuthorityClass,
    FactPack,
    Jurisdiction,
    Passage,
    RegistryBundle,
    SnapshotRegistryView,
    Source,
    compute_factpack_hash,
)
from localcareerimpact.contracts.query import QueryResultClosureV1
from localcareerimpact.contracts.runbundle import (
    ConfirmedCaseProfile,
    EvidenceContextV1,
    ValidatedEvidenceContext,
    validate_evidence_context,
)

from .retrieval import RetrievedChunk


@dataclass(frozen=True, slots=True)
class FactPackAdapterResult:
    fact_pack: FactPack
    closure: QueryResultClosureV1
    registry_view: SnapshotRegistryView


@dataclass(frozen=True, slots=True)
class EvidenceContextAdapterResult:
    fact_pack_result: FactPackAdapterResult
    context: EvidenceContextV1
    validated_context: ValidatedEvidenceContext


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    """Explicit administrator-supplied metadata required by Source/Passage."""

    publisher: str
    release_date: str | None
    licence: str
    jurisdiction: Jurisdiction
    authority_class: AuthorityClass


def adapt_retrieved_chunks(
    retrieved: tuple[RetrievedChunk, ...],
    *,
    source_provenance: Mapping[str, SourceProvenance],
    snapshot_id: str | None = None,
) -> FactPackAdapterResult:
    snapshot_ids = {item.chunk.snapshot_id for item in retrieved}
    if len(snapshot_ids) > 1:
        raise ValueError("Retrieved passages must belong to one immutable snapshot.")
    if snapshot_ids:
        retrieved_snapshot_id = next(iter(snapshot_ids))
        if snapshot_id is not None and snapshot_id != retrieved_snapshot_id:
            raise ValueError("The explicit snapshot does not match the retrieved passages.")
        selected_snapshot_id = retrieved_snapshot_id
    elif snapshot_id is not None:
        selected_snapshot_id = snapshot_id
    else:
        raise ValueError("An explicit snapshot is required for an empty retrieval result.")

    source_by_document: dict[str, Source] = {}
    passages: list[Passage] = []
    for item in retrieved:
        chunk = item.chunk
        indexed_at = _timestamp(chunk.metadata, "indexed_at")
        provenance = source_provenance.get(chunk.document_id)
        if provenance is None:
            raise ValueError(
                "Explicit source provenance is required for every retrieved document."
            )
        source = source_by_document.get(chunk.document_id)
        if source is None:
            source_id = _identifier("source", selected_snapshot_id, chunk.document_id)
            source = Source(
                source_id=source_id,
                title=chunk.title,
                publisher=provenance.publisher,
                source_uri=(
                    f"knowledge://{quote(selected_snapshot_id, safe='')}/"
                    f"{quote(chunk.source_label, safe='')}"
                ),
                release_date=provenance.release_date,
                retrieved_at=indexed_at,
                file_sha256=chunk.content_hash,
                licence=provenance.licence,
                jurisdiction=provenance.jurisdiction,
                authority_class=provenance.authority_class,
            )
            source_by_document[chunk.document_id] = source
        passages.append(
            Passage(
                evidence_id=_identifier(
                    "evidence", selected_snapshot_id, chunk.chunk_id
                ),
                chunk_id=chunk.chunk_id,
                source_id=source.source_id,
                file_sha256=chunk.content_hash,
                chunk_sha256=chunk.chunk_hash,
                page=_page_number(chunk.locator),
                section=chunk.locator,
                release_date=provenance.release_date,
                licence=source.licence,
                jurisdiction=source.jurisdiction,
                authority_class=source.authority_class,
                text=chunk.text,
            )
        )

    registries = RegistryBundle(
        query_templates=(),
        classifications=(),
        metrics=(),
        geographies=(),
        grains=(),
        transforms=(),
        formulas=(),
        message_templates=(),
    )
    registry_hash = sha256_uri(canonical_json_bytes(registries))
    registry_view = SnapshotRegistryView(
        snapshot_id=selected_snapshot_id,
        registry_bundle_hash=registry_hash,
        registry_bundle=registries,
        occupation_codes_by_classification_version=(),
        method_ids=(),
    )
    unhashed = FactPack(
        snapshot_id=selected_snapshot_id,
        factpack_hash=sha256_uri(b"pending-local-passage-pack"),
        queries=(),
        facts=(),
        passages=tuple(passages),
        tasks=(),
        methods=(),
        caveats=(),
        sources=tuple(source_by_document.values()),
        registries=registries,
        gaps=(),
    )
    fact_pack = unhashed.model_copy(
        update={"factpack_hash": compute_factpack_hash(unhashed)}
    )
    closure = QueryResultClosureV1(
        snapshot_id=selected_snapshot_id,
        registry_bundle_hash=registry_hash,
        results=(),
    )
    return FactPackAdapterResult(
        fact_pack=fact_pack,
        closure=closure,
        registry_view=registry_view,
    )


def adapt_evidence_context(
    *,
    run_id: str,
    tenant_id: str,
    policy_version: str,
    confirmed_case_profile: ConfirmedCaseProfile,
    retrieved: tuple[RetrievedChunk, ...],
    source_provenance: Mapping[str, SourceProvenance],
    snapshot_id: str | None = None,
) -> EvidenceContextAdapterResult:
    result = adapt_retrieved_chunks(
        retrieved,
        source_provenance=source_provenance,
        snapshot_id=snapshot_id,
    )
    context = EvidenceContextV1(
        run_id=run_id,
        tenant_id=tenant_id,
        policy_version=policy_version,
        confirmed_case_profile=confirmed_case_profile,
        fact_pack=result.fact_pack,
        query_result_closure_hash=sha256_uri(canonical_json_bytes(result.closure)),
    )
    validated = validate_evidence_context(
        context,
        result.closure,
        result.registry_view,
    )
    return EvidenceContextAdapterResult(
        fact_pack_result=result,
        context=context,
        validated_context=validated,
    )


def _identifier(kind: str, snapshot_id: str, record_id: str) -> str:
    digest = hashlib.sha256(f"{kind}:{snapshot_id}:{record_id}".encode("utf-8")).hexdigest()
    return f"{kind}-{digest}"


def _page_number(locator: str) -> int:
    if locator.startswith("page:"):
        raw = locator.removeprefix("page:").split("#", 1)[0]
        try:
            page = int(raw)
        except ValueError:
            return 1
        return max(1, page)
    return 1


def _timestamp(metadata: dict[str, object], key: str) -> str:
    value = metadata.get(key)
    if not isinstance(value, str):
        raise ValueError(f"Retrieved passage is missing {key} provenance.")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Retrieved passage has invalid {key} provenance.")
    return value


__all__ = [
    "EvidenceContextAdapterResult",
    "FactPackAdapterResult",
    "SourceProvenance",
    "adapt_evidence_context",
    "adapt_retrieved_chunks",
]
