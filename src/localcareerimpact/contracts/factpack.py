from __future__ import annotations

import ast
import hmac
from datetime import date, datetime
from decimal import Context, Decimal, DecimalException, ROUND_HALF_EVEN, localcontext
from typing import Annotated, Any, Literal, Mapping, TypeAlias

from pydantic import Field, field_validator, model_validator

from .canonical import canonical_json_bytes, sha256_uri
from .query import (
    CompareQuery,
    EvidenceQuery,
    ExplainMethodQuery,
    FrozenContract,
    GeographySelector,
    LookupQuery,
    Scope,
    SnapshotBoundQuery,
    TasksQuery,
    TrendQuery,
    _tupleize,
)

SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
AuthorityClass: TypeAlias = Literal["official", "peer_reviewed", "institutional", "contextual"]
Jurisdiction: TypeAlias = Literal["AU", "GLOBAL", "UNKNOWN"]


def _canonical_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("date must be an ISO calendar date") from exc
    if parsed.isoformat() != value:
        raise ValueError("date must use canonical YYYY-MM-DD form")
    return value


def _optional_canonical_date(value: str | None) -> str | None:
    if value is None:
        return None
    return _canonical_date(value)


def _timezone_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timestamp must be ISO/RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value


def _decimal_string(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("numeric values must be decimal strings")
    try:
        parsed = Decimal(value)
    except DecimalException as exc:
        raise ValueError("invalid decimal string") from exc
    if not parsed.is_finite():
        raise ValueError("decimal string must be finite")
    return value


def _unique_sorted(values: tuple[str, ...], label: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if nonempty and not values:
        raise ValueError(f"{label} must not be empty")
    if any(not value or value.strip() != value for value in values):
        raise ValueError(f"{label} values must be canonical non-empty strings")
    if len(set(values)) != len(values):
        raise ValueError(f"duplicate {label}")
    return tuple(sorted(values))


class Lineage(FrozenContract):
    lineage_id: str
    sheet: str | None
    row: str | None
    cell: str | None
    page: str | None
    transform_chain: tuple[str, ...]

    @field_validator("transform_chain")
    @classmethod
    def _transforms(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate transform IDs")
        return value


class ProvenanceInput(FrozenContract):
    input_id: str
    source_id: str
    source_file_sha256: str = Field(pattern=SHA256_PATTERN)
    input_value: str | None
    input_unit: str | None
    lineage: Lineage

    _input_value = field_validator("input_value")(_decimal_string)


class Derivation(FrozenContract):
    formula_id: str
    formula_version: str
    input_refs: tuple[str, ...]
    decimal_context: str = Field(pattern=r"^precision-(?:[1-9][0-9]{0,2})$")
    rounding_mode: Literal["ROUND_HALF_EVEN"]

    @field_validator("input_refs")
    @classmethod
    def _inputs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique_sorted(value, "derivation input references", nonempty=True)


class Fact(FrozenContract):
    fact_id: str
    metric: str
    value: str | None
    unit: str
    source_nature: Literal["official_published", "licensed_published", "curated_context"]
    estimate_nature: Literal["observed", "derived", "modelled"]
    availability_status: Literal["available", "suppressed", "unavailable", "not_applicable", "quarantined"]
    requested_scope: Scope
    answered_scope: Scope
    reference_period: str
    release_date: str
    retrieved_at: str
    crosswalk_quality: Literal["exact", "apportioned", "quarantined"] | None
    entity_resolution_status: Literal["resolved", "ambiguous", "unresolved"]
    crosswalk_source_id: str | None
    source_ids: tuple[str, ...]
    provenance_inputs: tuple[ProvenanceInput, ...]
    derivation: Derivation | None
    method_id: str | None
    caveat_ids: tuple[str, ...]
    scope_note: str | None
    unavailable_reason: str | None

    _value = field_validator("value")(_decimal_string)
    _release = field_validator("release_date")(_canonical_date)
    _retrieved = field_validator("retrieved_at")(_timezone_timestamp)

    @model_validator(mode="after")
    def _fact_invariants(self) -> Fact:
        if self.availability_status == "available" and self.value is None:
            raise ValueError("available fact requires a decimal value")
        if self.availability_status != "available" and self.value is not None:
            raise ValueError("non-available fact requires null value")
        if self.entity_resolution_status != "resolved" and self.availability_status == "available":
            raise ValueError("ambiguous or unresolved entity cannot be available")
        if self.crosswalk_quality == "quarantined" and self.availability_status != "quarantined":
            raise ValueError("quarantined crosswalk forces quarantined availability")
        if (self.crosswalk_quality is None) != (self.crosswalk_source_id is None):
            raise ValueError("crosswalk quality and source must both be null or non-null")
        if not self.provenance_inputs:
            raise ValueError("every fact requires provenance")
        input_ids = [item.input_id for item in self.provenance_inputs]
        if len(set(input_ids)) != len(input_ids):
            raise ValueError("duplicate provenance input IDs")
        if self.estimate_nature == "derived":
            if self.derivation is None or self.availability_status != "available":
                raise ValueError("derived facts require a complete derivation and available value")
        elif self.derivation is not None:
            raise ValueError("only locally derived facts may carry Derivation")
        if self.estimate_nature in ("observed", "modelled") and self.source_nature in ("official_published", "licensed_published"):
            if self.method_id is None:
                raise ValueError("published observed/modelled values require method_id")
            if not any(item.lineage.row is not None or item.lineage.cell is not None or item.lineage.page is not None for item in self.provenance_inputs):
                raise ValueError("published observed/modelled values require a row, cell, or page locator")
        source_ids = _unique_sorted(self.source_ids, "source IDs", nonempty=True)
        caveat_ids = _unique_sorted(self.caveat_ids, "caveat IDs")
        provenance = tuple(sorted(self.provenance_inputs, key=lambda item: (item.lineage.lineage_id, item.input_id)))
        updates: dict[str, Any] = {}
        if source_ids != self.source_ids:
            updates["source_ids"] = source_ids
        if caveat_ids != self.caveat_ids:
            updates["caveat_ids"] = caveat_ids
        if provenance != self.provenance_inputs:
            updates["provenance_inputs"] = provenance
        for name, value in updates.items():
            object.__setattr__(self, name, value)
        return self


class QueryRecord(FrozenContract):
    query_id: str
    template_id: str
    normalized_params: EvidenceQuery
    result_hash: str = Field(pattern=SHA256_PATTERN)


class Passage(FrozenContract):
    evidence_id: str
    chunk_id: str
    source_id: str
    file_sha256: str = Field(pattern=SHA256_PATTERN)
    chunk_sha256: str = Field(pattern=SHA256_PATTERN)
    page: int = Field(ge=1)
    section: str
    release_date: str | None
    licence: str
    jurisdiction: Jurisdiction
    authority_class: AuthorityClass
    text: str

    _release = field_validator("release_date")(_optional_canonical_date)


class TaskEvidence(FrozenContract):
    task_id: str
    classification: Literal["OSCA", "ANZSCO"]
    classification_version: str
    occupation_code: str
    task_text: str
    source_id: str
    source_file_sha256: str = Field(pattern=SHA256_PATTERN)
    lineage: Lineage
    caveat_ids: tuple[str, ...]

    @field_validator("caveat_ids")
    @classmethod
    def _caveats(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique_sorted(value, "caveat IDs")


class Method(FrozenContract):
    method_id: str
    version: str
    description: str
    source_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    caveat_ids: tuple[str, ...]

    @field_validator("source_ids", "evidence_ids", "caveat_ids")
    @classmethod
    def _refs(cls, value: tuple[str, ...], info: Any) -> tuple[str, ...]:
        return _unique_sorted(value, info.field_name)


class Caveat(FrozenContract):
    caveat_id: str
    code: str
    severity: Literal["required", "contextual"]
    renderer_template_en: str
    renderer_template_zh: str
    applies_to_refs: tuple[str, ...]

    @field_validator("applies_to_refs")
    @classmethod
    def _refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique_sorted(value, "caveat application references", nonempty=True)


class Source(FrozenContract):
    source_id: str
    title: str
    publisher: str
    source_uri: str
    release_date: str | None
    retrieved_at: str
    file_sha256: str = Field(pattern=SHA256_PATTERN)
    licence: str
    jurisdiction: Jurisdiction
    authority_class: AuthorityClass

    _release = field_validator("release_date")(_optional_canonical_date)
    _retrieved = field_validator("retrieved_at")(_timezone_timestamp)


class QueryTemplateEntry(FrozenContract):
    template_id: str
    schema_hash: str = Field(pattern=SHA256_PATTERN)


class ClassificationEntry(FrozenContract):
    classification: Literal["OSCA", "ANZSCO"]
    version: str
    schema_hash: str = Field(pattern=SHA256_PATTERN)


class MetricEntry(FrozenContract):
    metric_id: str
    unit_id: str
    definition: str


class GeographyEntry(FrozenContract):
    level: Literal["AUS", "STE", "SA4"]
    code: str
    name: str

    @model_validator(mode="after")
    def _aus(self) -> GeographyEntry:
        if self.level == "AUS" and self.code != "AUS":
            raise ValueError("AUS registry geography requires code AUS")
        return self


class GrainEntry(FrozenContract):
    grain_id: str
    definition: str


class TransformEntry(FrozenContract):
    transform_id: str
    code_hash: str = Field(pattern=SHA256_PATTERN)
    params_hash: str = Field(pattern=SHA256_PATTERN)


class FormulaEntry(FrozenContract):
    formula_id: str
    version: str
    expression: str


class MessageTemplateEntry(FrozenContract):
    template_id: str
    en: str
    zh: str


class RegistryBundle(FrozenContract):
    query_templates: tuple[QueryTemplateEntry, ...]
    classifications: tuple[ClassificationEntry, ...]
    metrics: tuple[MetricEntry, ...]
    geographies: tuple[GeographyEntry, ...]
    grains: tuple[GrainEntry, ...]
    transforms: tuple[TransformEntry, ...]
    formulas: tuple[FormulaEntry, ...]
    message_templates: tuple[MessageTemplateEntry, ...]

    @model_validator(mode="after")
    def _canonical(self) -> RegistryBundle:
        specifications = (
            ("query_templates", lambda x: x.template_id),
            ("classifications", lambda x: (x.classification, x.version)),
            ("metrics", lambda x: x.metric_id),
            ("geographies", lambda x: (x.level, x.code)),
            ("grains", lambda x: x.grain_id),
            ("transforms", lambda x: x.transform_id),
            ("formulas", lambda x: (x.formula_id, x.version)),
            ("message_templates", lambda x: x.template_id),
        )
        updates: dict[str, Any] = {}
        for name, key in specifications:
            values = getattr(self, name)
            keys = [key(item) for item in values]
            if len(set(keys)) != len(keys):
                raise ValueError(f"duplicate registry key in {name}")
            ordered = tuple(sorted(values, key=key))
            if ordered != values:
                updates[name] = ordered
        for name, value in updates.items():
            object.__setattr__(self, name, value)
        return self


class Gap(FrozenContract):
    gap_id: str
    query_id: str
    reason_code: Literal["unsupported_grain", "unavailable", "suppressed", "ambiguous", "quarantined"]
    requested_scope: Scope
    answered_scope: Scope | None
    message_template_id: str


class SnapshotRegistryView(FrozenContract):
    snapshot_id: str = Field(pattern=SHA256_PATTERN)
    registry_bundle_hash: str = Field(pattern=SHA256_PATTERN)
    registry_bundle: RegistryBundle
    occupation_codes_by_classification_version: tuple[tuple[str, str, tuple[str, ...]], ...]
    method_ids: tuple[str, ...]

    @model_validator(mode="after")
    def _integrity(self) -> SnapshotRegistryView:
        expected = sha256_uri(canonical_json_bytes(self.registry_bundle))
        if not hmac.compare_digest(self.registry_bundle_hash, expected):
            raise ValueError("registry bundle hash mismatch")
        seen: set[tuple[str, str]] = set()
        rows = []
        registered_classifications = {(item.classification, item.version) for item in self.registry_bundle.classifications}
        for classification, version, codes in self.occupation_codes_by_classification_version:
            key = (classification, version)
            if key in seen:
                raise ValueError("duplicate occupation-code registry key")
            if key not in registered_classifications:
                raise ValueError("occupation-code classification/version is absent from RegistryBundle")
            seen.add(key)
            rows.append((classification, version, _unique_sorted(codes, "occupation codes", nonempty=True)))
        methods = _unique_sorted(self.method_ids, "method IDs")
        rows_tuple = tuple(sorted(rows))
        updates = {}
        if rows_tuple != self.occupation_codes_by_classification_version:
            updates["occupation_codes_by_classification_version"] = rows_tuple
        if methods != self.method_ids:
            updates["method_ids"] = methods
        for name, value in updates.items():
            object.__setattr__(self, name, value)
        return self

    def assert_query_resolves(self, query: EvidenceQuery) -> None:
        templates = self.registry_bundle
        class_keys = {(item.classification, item.version) for item in templates.classifications}
        metrics = {item.metric_id for item in templates.metrics}
        geographies = {(item.level, item.code) for item in templates.geographies}
        grains = {item.grain_id for item in templates.grains}
        occupations = {(c, v): set(codes) for c, v, codes in self.occupation_codes_by_classification_version}
        if isinstance(query, ExplainMethodQuery):
            if query.method_id not in self.method_ids:
                raise ValueError("unknown method ID")
            return
        key = (query.occupation.classification, query.occupation.classification_version)
        if key not in class_keys or key not in occupations:
            raise ValueError("unknown classification version")
        if not set(query.occupation.codes) <= occupations[key]:
            raise ValueError("unknown occupation code")
        if isinstance(query, (LookupQuery, CompareQuery, TrendQuery)):
            if not set(query.metrics) <= metrics:
                raise ValueError("unknown metric")
            if (query.geography.level, query.geography.code) not in geographies:
                raise ValueError("unknown geography")
            if query.requested_grain not in grains:
                raise ValueError("unknown grain")

    def assert_occupation_resolves(self, classification: str, version: str, codes: tuple[str, ...]) -> None:
        occupations = {(c, v): set(registered_codes) for c, v, registered_codes in self.occupation_codes_by_classification_version}
        key = (classification, version)
        registered_classifications = {(item.classification, item.version) for item in self.registry_bundle.classifications}
        if key not in registered_classifications:
            raise ValueError("unknown occupation classification/version in RegistryBundle")
        if key not in occupations:
            raise ValueError("unknown occupation classification/version")
        if not set(codes) <= occupations[key]:
            raise ValueError("unknown occupation code")

    def assert_scope_resolves(self, scope: Scope) -> None:
        self.assert_occupation_resolves(scope.classification, scope.classification_version, scope.occupation_codes)
        geographies = {(item.level, item.code) for item in self.registry_bundle.geographies}
        grains = {item.grain_id for item in self.registry_bundle.grains}
        if scope.geography_level is not None and (scope.geography_level, scope.geography_code) not in geographies:
            raise ValueError("unknown scope geography")
        if scope.grain not in grains:
            raise ValueError("unknown scope grain")


def _scope_key(scope: Scope) -> bytes:
    return canonical_json_bytes(scope)


def _fact_sort_key(fact: Fact) -> tuple[Any, ...]:
    return (
        fact.metric,
        _scope_key(fact.requested_scope),
        _scope_key(fact.answered_scope),
        fact.reference_period,
        fact.source_nature,
        fact.estimate_nature,
        fact.source_ids,
        tuple(item.lineage.lineage_id for item in fact.provenance_inputs),
        fact.fact_id,
    )


class FactPack(FrozenContract):
    schema_version: Literal["factpack.v1"] = "factpack.v1"
    snapshot_id: str = Field(pattern=SHA256_PATTERN)
    factpack_hash: str = Field(pattern=SHA256_PATTERN)
    queries: tuple[QueryRecord, ...]
    facts: tuple[Fact, ...]
    passages: tuple[Passage, ...]
    tasks: tuple[TaskEvidence, ...]
    methods: tuple[Method, ...]
    caveats: tuple[Caveat, ...]
    sources: tuple[Source, ...]
    registries: RegistryBundle
    gaps: tuple[Gap, ...]

    @model_validator(mode="after")
    def _canonical_and_closed(self) -> FactPack:
        id_specs = (
            ("queries", "query_id"), ("facts", "fact_id"), ("tasks", "task_id"),
            ("methods", "method_id"), ("caveats", "caveat_id"), ("sources", "source_id"), ("gaps", "gap_id"),
        )
        updates: dict[str, Any] = {}
        sorters = {
            "queries": lambda x: x.query_id, "facts": _fact_sort_key, "tasks": lambda x: x.task_id,
            "methods": lambda x: x.method_id, "caveats": lambda x: x.caveat_id,
            "sources": lambda x: x.source_id, "gaps": lambda x: x.gap_id,
        }
        for name, attr in id_specs:
            values = getattr(self, name)
            identifiers = [getattr(item, attr) for item in values]
            if len(set(identifiers)) != len(identifiers):
                raise ValueError(f"duplicate IDs in {name}")
            ordered = tuple(sorted(values, key=sorters[name]))
            if ordered != values:
                updates[name] = ordered
        passage_keys = [(item.evidence_id, item.chunk_id) for item in self.passages]
        if len(set(passage_keys)) != len(passage_keys):
            raise ValueError("duplicate passage evidence/chunk pair")
        semantic_keys = [
            (fact.metric, _scope_key(fact.requested_scope), _scope_key(fact.answered_scope), fact.reference_period, fact.source_nature, fact.estimate_nature)
            for fact in self.facts
        ]
        if len(set(semantic_keys)) != len(semantic_keys):
            raise ValueError("duplicate fact semantic key")
        self._validate_references_and_derivations()
        for name, value in updates.items():
            object.__setattr__(self, name, value)
        return self

    def _validate_references_and_derivations(self) -> None:
        source_by_id = {item.source_id: item for item in self.sources}
        fact_ids = {item.fact_id for item in self.facts}
        task_ids = {item.task_id for item in self.tasks}
        method_by_id = {item.method_id: item for item in self.methods}
        caveat_ids = {item.caveat_id for item in self.caveats}
        passage_ids = {item.evidence_id for item in self.passages}
        query_ids = {item.query_id for item in self.queries}
        gap_ids = {item.gap_id for item in self.gaps}
        template_ids = {item.template_id for item in self.registries.query_templates}
        class_keys = {(item.classification, item.version) for item in self.registries.classifications}
        metric_units = {item.metric_id: item.unit_id for item in self.registries.metrics}
        geography_keys = {(item.level, item.code) for item in self.registries.geographies}
        grains = {item.grain_id for item in self.registries.grains}
        transforms = {item.transform_id for item in self.registries.transforms}
        formulas = {(item.formula_id, item.version): item for item in self.registries.formulas}
        messages = {item.template_id for item in self.registries.message_templates}
        reference_index: dict[str, list[str]] = {}
        for kind, identifiers in (
            ("fact", fact_ids), ("task", task_ids), ("method", set(method_by_id)),
            ("caveat", caveat_ids), ("passage", [item.evidence_id for item in self.passages]),
            ("query", query_ids), ("gap", gap_ids), ("source", set(source_by_id)),
        ):
            for identifier in identifiers:
                reference_index.setdefault(identifier, []).append(kind)
        for identifier, kinds in reference_index.items():
            if len(set(kinds)) > 1:
                raise ValueError(f"ambiguous cross-type record ID: {identifier}")

        def validate_scope(scope: Scope) -> None:
            if (scope.classification, scope.classification_version) not in class_keys:
                raise ValueError("scope classification reference is missing")
            if scope.geography_level is not None and (scope.geography_level, scope.geography_code) not in geography_keys:
                raise ValueError("scope geography reference is missing")
            if scope.grain not in grains:
                raise ValueError("scope grain reference is missing")

        for query in self.queries:
            if query.template_id not in template_ids:
                raise ValueError("query template reference is missing")
            self._validate_query_registry_refs(query.normalized_params, class_keys, set(metric_units), geography_keys, grains, set(method_by_id))
        for fact in self.facts:
            validate_scope(fact.requested_scope)
            validate_scope(fact.answered_scope)
            if fact.metric not in metric_units or metric_units[fact.metric] != fact.unit:
                raise ValueError("fact metric/unit reference is missing")
            if not set(fact.source_ids) <= set(source_by_id):
                raise ValueError("fact source reference is missing")
            if not set(fact.caveat_ids) <= caveat_ids:
                raise ValueError("fact caveat reference is missing")
            if fact.method_id is not None and fact.method_id not in method_by_id:
                raise ValueError("fact method reference is missing")
            provenance_sources = {item.source_id for item in fact.provenance_inputs}
            if not provenance_sources <= set(source_by_id):
                raise ValueError("provenance source reference is missing")
            for item in fact.provenance_inputs:
                source = source_by_id[item.source_id]
                if item.source_file_sha256 != source.file_sha256:
                    raise ValueError("provenance file hash does not match source")
                if not set(item.lineage.transform_chain) <= transforms:
                    raise ValueError("lineage transform reference is missing")
            if fact.crosswalk_source_id is not None:
                if fact.crosswalk_source_id not in fact.source_ids or fact.crosswalk_source_id not in provenance_sources:
                    raise ValueError("crosswalk source must be in fact sources and provenance")
            if fact.derivation is not None:
                formula = formulas.get((fact.derivation.formula_id, fact.derivation.formula_version))
                if formula is None:
                    raise ValueError("derivation formula reference is missing")
                _validate_and_recompute(fact, formula)
        for passage in self.passages:
            source = source_by_id.get(passage.source_id)
            if source is None or passage.file_sha256 != source.file_sha256:
                raise ValueError("passage source reference is missing or inconsistent")
        for task in self.tasks:
            if (task.classification, task.classification_version) not in class_keys:
                raise ValueError("task classification reference is missing")
            source = source_by_id.get(task.source_id)
            if source is None or task.source_file_sha256 != source.file_sha256:
                raise ValueError("task source reference is missing or inconsistent")
            if not set(task.lineage.transform_chain) <= transforms:
                raise ValueError("task lineage transform reference is missing")
            if not set(task.caveat_ids) <= caveat_ids:
                raise ValueError("task caveat reference is missing")
        for method in self.methods:
            if not set(method.source_ids) <= set(source_by_id) or not set(method.evidence_ids) <= passage_ids or not set(method.caveat_ids) <= caveat_ids:
                raise ValueError("method reference is missing")
        for caveat in self.caveats:
            for reference in caveat.applies_to_refs:
                if len(reference_index.get(reference, ())) != 1:
                    raise ValueError("caveat target reference is missing or ambiguous")
        for gap in self.gaps:
            if gap.query_id not in query_ids or gap.message_template_id not in messages:
                raise ValueError("gap reference is missing")
            validate_scope(gap.requested_scope)
            if gap.answered_scope is not None:
                validate_scope(gap.answered_scope)

    @staticmethod
    def _validate_query_registry_refs(query: EvidenceQuery, class_keys: set[tuple[str, str]], metrics: set[str], geographies: set[tuple[str, str]], grains: set[str], methods: set[str]) -> None:
        if isinstance(query, ExplainMethodQuery):
            if query.method_id not in methods:
                raise ValueError("query method reference is missing")
            return
        if (query.occupation.classification, query.occupation.classification_version) not in class_keys:
            raise ValueError("query classification reference is missing")
        if isinstance(query, (LookupQuery, CompareQuery, TrendQuery)):
            if not set(query.metrics) <= metrics:
                raise ValueError("query metric reference is missing")
            if (query.geography.level, query.geography.code) not in geographies:
                raise ValueError("query geography reference is missing")
            if query.requested_grain not in grains:
                raise ValueError("query grain reference is missing")


def _validate_expression_node(node: ast.AST) -> None:
    allowed = (ast.Expression, ast.Name, ast.Load, ast.Constant, ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.UAdd, ast.USub)
    if not isinstance(node, allowed):
        raise ValueError("formula contains a forbidden AST node")
    if isinstance(node, ast.Constant) and (isinstance(node.value, bool) or not isinstance(node.value, int)):
        raise ValueError("formula constants must be integers")
    for child in ast.iter_child_nodes(node):
        _validate_expression_node(child)


def _evaluate_expression(node: ast.AST, values: Mapping[str, Decimal]) -> Decimal:
    if isinstance(node, ast.Expression):
        return _evaluate_expression(node.body, values)
    if isinstance(node, ast.Name):
        return values[node.id]
    if isinstance(node, ast.Constant):
        return Decimal(node.value)
    if isinstance(node, ast.UnaryOp):
        value = _evaluate_expression(node.operand, values)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        left = _evaluate_expression(node.left, values)
        right = _evaluate_expression(node.right, values)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        return left / right
    raise ValueError("forbidden formula node")


def _validate_and_recompute(fact: Fact, formula: FormulaEntry) -> None:
    assert fact.derivation is not None and fact.value is not None
    try:
        tree = ast.parse(formula.expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError("invalid formula expression") from exc
    _validate_expression_node(tree)
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    if names != set(fact.derivation.input_refs):
        raise ValueError("formula variables must exactly equal derivation input_refs")
    inputs = {item.input_id: item for item in fact.provenance_inputs}
    if not set(fact.derivation.input_refs) <= set(inputs):
        raise ValueError("derivation input reference is missing")
    values: dict[str, Decimal] = {}
    for input_id in fact.derivation.input_refs:
        raw = inputs[input_id].input_value
        if raw is None:
            raise ValueError("derived input values must be non-null")
        values[input_id] = Decimal(raw)
    precision = int(fact.derivation.decimal_context.removeprefix("precision-"))
    try:
        with localcontext(Context(prec=precision, rounding=ROUND_HALF_EVEN)) as context:
            result = _evaluate_expression(tree, values)
            result = context.plus(result)
    except (DecimalException, ZeroDivisionError, KeyError) as exc:
        raise ValueError("formula cannot be evaluated") from exc
    if not result.is_finite() or result != Decimal(fact.value):
        raise ValueError("derived value does not equal Decimal recomputation")


def compute_factpack_hash(pack: FactPack) -> str:
    if not isinstance(pack, FactPack):
        raise TypeError("pack must be a parsed FactPack")
    payload = pack.model_dump(mode="json", exclude_none=False, by_alias=True)
    del payload["factpack_hash"]
    return sha256_uri(canonical_json_bytes(payload))


def parse_factpack(payload: Mapping[str, object], *, verify_hash: bool = True) -> FactPack:
    if not isinstance(payload, Mapping):
        raise TypeError("FactPack payload must be a mapping")
    pack = FactPack.model_validate(_tupleize(dict(payload)))
    if verify_hash and not hmac.compare_digest(pack.factpack_hash, compute_factpack_hash(pack)):
        raise ValueError("FactPack hash mismatch")
    return pack


__all__ = [
    "AuthorityClass", "Caveat", "ClassificationEntry", "Derivation", "Fact", "FactPack", "FormulaEntry",
    "Gap", "GeographyEntry", "GrainEntry", "Jurisdiction", "Lineage", "MessageTemplateEntry", "Method", "MetricEntry",
    "Passage", "ProvenanceInput", "QueryRecord", "QueryTemplateEntry", "RegistryBundle", "Scope", "SnapshotBoundQuery",
    "SnapshotRegistryView", "Source", "TaskEvidence", "TransformEntry", "compute_factpack_hash", "parse_factpack",
]
