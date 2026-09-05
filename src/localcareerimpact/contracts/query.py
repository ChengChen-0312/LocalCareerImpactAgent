from __future__ import annotations

import hmac
from datetime import date
from typing import Annotated, Any, Literal, Mapping, TypeAlias

from pydantic import Field, TypeAdapter, field_validator, model_validator

from .base import StrictContract
from .canonical import canonical_contract_hash, canonical_json_bytes, sha256_uri

SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"


def _tupleize(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_tupleize(item) for item in value)
    if isinstance(value, dict):
        return {key: _tupleize(item) for key, item in value.items()}
    return value


class FrozenContract(StrictContract):
    @model_validator(mode="before")
    @classmethod
    def _freeze_lists(cls, value: Any) -> Any:
        return _tupleize(value)

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Use public contract aliases by default, including nested models."""
        kwargs.setdefault("by_alias", True)
        return super().model_dump(*args, **kwargs)

    def model_dump_json(self, *args: Any, **kwargs: Any) -> str:
        kwargs.setdefault("by_alias", True)
        return super().model_dump_json(*args, **kwargs)


def _nonempty(value: str) -> str:
    if not value or value.strip() != value:
        raise ValueError("value must be a non-empty canonical string")
    return value


def _canonical_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("date must be an ISO calendar date") from exc
    if parsed.isoformat() != value:
        raise ValueError("date must use canonical YYYY-MM-DD form")
    return value


class OccupationSelector(FrozenContract):
    classification: Literal["OSCA", "ANZSCO"]
    classification_version: str
    codes: tuple[str, ...]

    _version = field_validator("classification_version")(_nonempty)

    @field_validator("codes")
    @classmethod
    def _codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not code or code.strip() != code for code in value):
            raise ValueError("occupation codes must be non-empty canonical strings")
        if len(set(value)) != len(value):
            raise ValueError("duplicate occupation codes")
        return tuple(sorted(value))


class GeographySelector(FrozenContract):
    level: Literal["AUS", "STE", "SA4"]
    code: str

    @model_validator(mode="after")
    def _pair(self) -> GeographySelector:
        _nonempty(self.code)
        if self.level == "AUS" and self.code != "AUS":
            raise ValueError("AUS geography requires code AUS")
        return self


class PeriodSelector(FrozenContract):
    mode: Literal["latest_common", "exact", "range"]
    exact: str | None
    from_: str | None = Field(alias="from", serialization_alias="from")
    to: str | None
    as_of: str

    @model_validator(mode="after")
    def _period_matrix(self) -> PeriodSelector:
        _canonical_date(self.as_of)
        for value in (self.exact, self.from_, self.to):
            if value is not None:
                _nonempty(value)
        if self.mode == "latest_common" and (self.exact is not None or self.from_ is not None or self.to is not None):
            raise ValueError("latest_common sets only as_of")
        if self.mode == "exact" and (self.exact is None or self.from_ is not None or self.to is not None):
            raise ValueError("exact sets exact and as_of only")
        if self.mode == "range" and (self.exact is not None or self.from_ is None or self.to is None):
            raise ValueError("range sets from, to, and as_of only")
        if self.mode == "range" and self.from_ > self.to:
            raise ValueError("period range must be ordered")
        return self


class _ScopedQuery(FrozenContract):
    occupation: OccupationSelector
    metrics: tuple[str, ...]
    geography: GeographySelector
    period: PeriodSelector
    requested_grain: str
    method_id: None
    evidence_mode: Literal["strict"]

    @field_validator("metrics")
    @classmethod
    def _metrics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("metrics must be non-empty and unique")
        for item in value:
            _nonempty(item)
        return tuple(sorted(value))

    _grain = field_validator("requested_grain")(_nonempty)


class LookupQuery(_ScopedQuery):
    intent: Literal["lookup"]

    @model_validator(mode="after")
    def _one_code(self) -> LookupQuery:
        if len(self.occupation.codes) != 1:
            raise ValueError("lookup requires exactly one occupation code")
        return self


class CompareQuery(_ScopedQuery):
    intent: Literal["compare"]

    @model_validator(mode="after")
    def _code_count(self) -> CompareQuery:
        if not 2 <= len(self.occupation.codes) <= 5:
            raise ValueError("compare requires two to five occupation codes")
        return self


class TrendQuery(_ScopedQuery):
    intent: Literal["trend"]

    @model_validator(mode="after")
    def _trend_matrix(self) -> TrendQuery:
        if len(self.occupation.codes) != 1 or self.period.mode != "range":
            raise ValueError("trend requires one occupation and a range period")
        return self


class TasksQuery(FrozenContract):
    intent: Literal["tasks"]
    occupation: OccupationSelector
    metrics: tuple[str, ...]
    geography: None
    period: None
    requested_grain: None
    method_id: None
    evidence_mode: Literal["strict"]

    @model_validator(mode="after")
    def _tasks_matrix(self) -> TasksQuery:
        if len(self.occupation.codes) != 1 or self.metrics:
            raise ValueError("tasks requires one occupation and no metrics")
        return self


class ExplainMethodQuery(FrozenContract):
    intent: Literal["explain_method"]
    occupation: None
    metrics: tuple[str, ...]
    geography: None
    period: None
    requested_grain: None
    method_id: str
    evidence_mode: Literal["strict"]

    @model_validator(mode="after")
    def _method_matrix(self) -> ExplainMethodQuery:
        if self.metrics:
            raise ValueError("explain_method forbids metrics")
        _nonempty(self.method_id)
        return self


EvidenceQuery: TypeAlias = Annotated[
    LookupQuery | CompareQuery | TrendQuery | TasksQuery | ExplainMethodQuery,
    Field(discriminator="intent"),
]
_EVIDENCE_QUERY_ADAPTER = TypeAdapter(EvidenceQuery)


def parse_evidence_query(payload: Mapping[str, object]) -> EvidenceQuery:
    if not isinstance(payload, Mapping):
        raise TypeError("EvidenceQuery payload must be a mapping")
    return _EVIDENCE_QUERY_ADAPTER.validate_python(_tupleize(dict(payload)))


class Scope(FrozenContract):
    classification: Literal["OSCA", "ANZSCO"]
    classification_version: str
    occupation_codes: tuple[str, ...]
    geography_level: Literal["AUS", "STE", "SA4"] | None
    geography_code: str | None
    grain: str

    @model_validator(mode="after")
    def _scope(self) -> Scope:
        _nonempty(self.classification_version)
        _nonempty(self.grain)
        if not self.occupation_codes or len(set(self.occupation_codes)) != len(self.occupation_codes):
            raise ValueError("scope occupation codes must be non-empty and unique")
        if (self.geography_level is None) != (self.geography_code is None):
            raise ValueError("geography level and code must both be null or non-null")
        if self.geography_level == "AUS" and self.geography_code != "AUS":
            raise ValueError("AUS scope requires code AUS")
        object.__setattr__(self, "occupation_codes", tuple(sorted(self.occupation_codes)))
        return self


class SnapshotBoundQuery(FrozenContract):
    snapshot_id: str = Field(pattern=SHA256_PATTERN)
    registry_bundle_hash: str = Field(pattern=SHA256_PATTERN)
    query: EvidenceQuery


def bind_evidence_query(query: EvidenceQuery, snapshot: Any) -> SnapshotBoundQuery:
    from .factpack import SnapshotRegistryView

    if not isinstance(query, (LookupQuery, CompareQuery, TrendQuery, TasksQuery, ExplainMethodQuery)):
        raise TypeError("query must be a parsed EvidenceQuery")
    if not isinstance(snapshot, SnapshotRegistryView):
        raise TypeError("snapshot must be a parsed SnapshotRegistryView")
    snapshot.assert_query_resolves(query)
    return SnapshotBoundQuery(
        snapshot_id=snapshot.snapshot_id,
        registry_bundle_hash=snapshot.registry_bundle_hash,
        query=query,
    )


class FactToolResult(FrozenContract):
    kind: Literal["fact"]
    fact_id: str
    payload_hash: str = Field(pattern=SHA256_PATTERN)


class TaskToolResult(FrozenContract):
    kind: Literal["task"]
    task_id: str
    payload_hash: str = Field(pattern=SHA256_PATTERN)


class MethodToolResult(FrozenContract):
    kind: Literal["method"]
    method_id: str
    payload_hash: str = Field(pattern=SHA256_PATTERN)


class GapToolResult(FrozenContract):
    kind: Literal["gap"]
    gap_id: str
    payload_hash: str = Field(pattern=SHA256_PATTERN)


class PassageToolResult(FrozenContract):
    kind: Literal["passage"]
    evidence_id: str
    chunk_id: str
    rrf_rank: int = Field(ge=1)
    payload_hash: str = Field(pattern=SHA256_PATTERN)


ToolResultRecord: TypeAlias = Annotated[
    FactToolResult | TaskToolResult | MethodToolResult | GapToolResult | PassageToolResult,
    Field(discriminator="kind"),
]


def _record_identity(record: ToolResultRecord) -> tuple[str, ...]:
    if isinstance(record, FactToolResult):
        return (record.kind, record.fact_id)
    if isinstance(record, TaskToolResult):
        return (record.kind, record.task_id)
    if isinstance(record, MethodToolResult):
        return (record.kind, record.method_id)
    if isinstance(record, GapToolResult):
        return (record.kind, record.gap_id)
    return (record.kind, record.evidence_id, record.chunk_id)


def _record_sort_key(record: ToolResultRecord) -> tuple[Any, ...]:
    priority = {"fact": 0, "task": 1, "method": 2, "gap": 3, "passage": 4}
    if isinstance(record, PassageToolResult):
        return (priority[record.kind], record.rrf_rank, record.chunk_id, record.evidence_id)
    return (priority[record.kind], *_record_identity(record)[1:])


class QueryResultDigestInput(FrozenContract):
    query_id: str
    template_id: str
    requested_scope: Scope | None
    answered_scope: Scope | None
    records: tuple[ToolResultRecord, ...]

    @model_validator(mode="after")
    def _canonical_records(self) -> QueryResultDigestInput:
        identities = [_record_identity(record) for record in self.records]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate typed tool result record")
        passages = [record for record in self.records if isinstance(record, PassageToolResult)]
        ranks = sorted(record.rrf_rank for record in passages)
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("passage ranks must be contiguous within a query")
        ordered = tuple(sorted(self.records, key=_record_sort_key))
        if ordered != self.records:
            object.__setattr__(self, "records", ordered)
        return self


class QueryResultHashPreimage(FrozenContract):
    bound_query: SnapshotBoundQuery
    result: QueryResultDigestInput


class QueryResultClosureV1(FrozenContract):
    schema_version: Literal["query-result-closure.v1"] = "query-result-closure.v1"
    snapshot_id: str = Field(pattern=SHA256_PATTERN)
    registry_bundle_hash: str = Field(pattern=SHA256_PATTERN)
    results: tuple[QueryResultDigestInput, ...]

    @model_validator(mode="after")
    def _results(self) -> QueryResultClosureV1:
        ids = [result.query_id for result in self.results]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate closure query IDs")
        ordered = tuple(sorted(self.results, key=lambda item: item.query_id))
        if ordered != self.results:
            object.__setattr__(self, "results", ordered)
        return self


def parse_query_result_closure(payload: Mapping[str, object], pack: Any) -> QueryResultClosureV1:
    closure = QueryResultClosureV1.model_validate(_tupleize(dict(payload)))
    validate_query_result_closure(closure, pack)
    return closure


def validate_query_result_closure(closure: QueryResultClosureV1, pack: Any) -> None:
    registry_hash = sha256_uri(canonical_json_bytes(pack.registries))
    if not hmac.compare_digest(closure.snapshot_id, pack.snapshot_id):
        raise ValueError("closure snapshot does not match FactPack")
    if not hmac.compare_digest(closure.registry_bundle_hash, registry_hash):
        raise ValueError("closure registry hash does not match FactPack")
    query_by_id = {query.query_id: query for query in pack.queries}
    result_by_id = {result.query_id: result for result in closure.results}
    if set(query_by_id) != set(result_by_id):
        raise ValueError("closure must contain exactly one result per FactPack query")
    records_by_kind = {
        "fact": {record.fact_id: record for record in pack.facts},
        "task": {record.task_id: record for record in pack.tasks},
        "method": {record.method_id: record for record in pack.methods},
        "gap": {record.gap_id: record for record in pack.gaps},
    }
    passages = {(record.evidence_id, record.chunk_id): record for record in pack.passages}
    if len(passages) != len(pack.passages):
        raise ValueError("passage evidence/chunk pairs must be unique")
    for query_id, query_record in query_by_id.items():
        result = result_by_id[query_id]
        if result.template_id != query_record.template_id:
            raise ValueError("query template mismatch")
        for tool_record in result.records:
            if isinstance(tool_record, PassageToolResult):
                referenced = passages.get((tool_record.evidence_id, tool_record.chunk_id))
            else:
                identifier = _record_identity(tool_record)[1]
                referenced = records_by_kind[tool_record.kind].get(identifier)
            if referenced is None:
                raise ValueError("tool result record reference is missing")
            if isinstance(tool_record, GapToolResult) and referenced.query_id != query_id:
                raise ValueError("gap ownership does not match query result")
            expected = sha256_uri(canonical_json_bytes(referenced))
            if not hmac.compare_digest(tool_record.payload_hash, expected):
                raise ValueError("tool result payload hash mismatch")
        bound = SnapshotBoundQuery(
            snapshot_id=pack.snapshot_id,
            registry_bundle_hash=registry_hash,
            query=query_record.normalized_params,
        )
        expected_result_hash = canonical_contract_hash(QueryResultHashPreimage(bound_query=bound, result=result))
        if not hmac.compare_digest(query_record.result_hash, expected_result_hash):
            raise ValueError("query result hash mismatch")


__all__ = [
    "CompareQuery", "EvidenceQuery", "ExplainMethodQuery", "FactToolResult", "FrozenContract",
    "GapToolResult", "GeographySelector", "LookupQuery", "MethodToolResult", "OccupationSelector",
    "PassageToolResult", "PeriodSelector", "QueryResultClosureV1", "QueryResultDigestInput",
    "QueryResultHashPreimage", "Scope", "SnapshotBoundQuery", "TaskToolResult", "TasksQuery",
    "ToolResultRecord", "TrendQuery", "bind_evidence_query", "parse_evidence_query",
    "parse_query_result_closure", "validate_query_result_closure",
]
