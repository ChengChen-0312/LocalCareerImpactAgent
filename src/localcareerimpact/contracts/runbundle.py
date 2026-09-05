from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping
from weakref import WeakValueDictionary

from pydantic import Field, model_validator

from .canonical import canonical_contract_hash, canonical_json_bytes, sha256_uri
from .factpack import FactPack, SnapshotRegistryView, compute_factpack_hash
from .query import (
    FrozenContract,
    QueryResultClosureV1,
    SnapshotBoundQuery,
    _tupleize,
    bind_evidence_query,
    parse_evidence_query,
    validate_query_result_closure,
)

SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
PROFILE_PATTERN = r"^service-hmac:[0-9a-f]{64}$"
Authority = Literal["DRAFT", "ADVISORY_ONLY", "DECIDER_CANDIDATE"]
_VALIDATION_SEAL = object()


def _unique_sorted_models(values: tuple[Any, ...], attr: str, label: str) -> tuple[Any, ...]:
    identifiers = [getattr(item, attr) for item in values]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError(f"duplicate {label} IDs")
    return tuple(sorted(values, key=lambda item: getattr(item, attr)))


class ConfirmedOccupation(FrozenContract):
    classification: Literal["OSCA", "ANZSCO"]
    classification_version: str
    occupation_code: str
    title: str


class ConfirmedResponsibility(FrozenContract):
    responsibility_id: str
    text: str


class ConfirmedSkill(FrozenContract):
    skill_id: str
    text: str


class UserGoal(FrozenContract):
    goal_id: str
    text: str


class ConfirmedCaseProfile(FrozenContract):
    profile_ref: str = Field(pattern=PROFILE_PATTERN)
    region: str
    confirmed_occupations: tuple[ConfirmedOccupation, ...]
    confirmed_responsibilities: tuple[ConfirmedResponsibility, ...]
    confirmed_skills: tuple[ConfirmedSkill, ...]
    user_goals: tuple[UserGoal, ...]

    @model_validator(mode="after")
    def _canonical(self) -> ConfirmedCaseProfile:
        specifications = (
            ("confirmed_occupations", "occupation_code", "occupation"),
            ("confirmed_responsibilities", "responsibility_id", "responsibility"),
            ("confirmed_skills", "skill_id", "skill"),
            ("user_goals", "goal_id", "goal"),
        )
        updates = {}
        for field_name, attr, label in specifications:
            values = getattr(self, field_name)
            ordered = _unique_sorted_models(values, attr, label)
            if ordered != values:
                updates[field_name] = ordered
        for name, value in updates.items():
            object.__setattr__(self, name, value)
        return self


class EvidenceContextV1(FrozenContract):
    schema_version: Literal["evidence-context.v1"] = "evidence-context.v1"
    run_id: str
    tenant_id: str
    policy_version: str
    confirmed_case_profile: ConfirmedCaseProfile
    fact_pack: FactPack
    query_result_closure_hash: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _factpack_integrity(self) -> EvidenceContextV1:
        if not hmac.compare_digest(self.fact_pack.factpack_hash, compute_factpack_hash(self.fact_pack)):
            raise ValueError("EvidenceContext contains a mismatched FactPack hash")
        return self


@dataclass(frozen=True, slots=True)
class ValidatedFactPackBinding:
    fact_pack: FactPack
    closure: QueryResultClosureV1
    snapshot_registry_view: SnapshotRegistryView
    bound_queries: tuple[tuple[str, SnapshotBoundQuery], ...]
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _VALIDATION_SEAL:
            raise TypeError("ValidatedFactPackBinding can only be created by the validation factory")


class ValidatedEvidenceContext:
    __slots__ = ("_binding", "_canonical_context_bytes", "_context", "__weakref__")

    def __new__(cls, *_args: object, **_kwargs: object) -> ValidatedEvidenceContext:
        raise TypeError("ValidatedEvidenceContext can only be created by validate_evidence_context")

    def __setattr__(self, name: str, value: object) -> None:
        raise TypeError("ValidatedEvidenceContext is immutable")

    @property
    def context(self) -> EvidenceContextV1:
        return self._context

    @property
    def binding(self) -> ValidatedFactPackBinding:
        return self._binding


_FACTORY_VALIDATED_CONTEXTS: WeakValueDictionary[int, ValidatedEvidenceContext] = WeakValueDictionary()


def _new_validated_evidence_context(
    context: EvidenceContextV1,
    binding: ValidatedFactPackBinding,
) -> ValidatedEvidenceContext:
    validated = object.__new__(ValidatedEvidenceContext)
    object.__setattr__(validated, "_context", context)
    object.__setattr__(validated, "_binding", binding)
    object.__setattr__(validated, "_canonical_context_bytes", canonical_json_bytes(context))
    _FACTORY_VALIDATED_CONTEXTS[id(validated)] = validated
    return validated


class RunBundle(FrozenContract):
    run_id: str
    tenant_id: str
    policy_version: str
    confirmed_case_profile: ConfirmedCaseProfile
    fact_pack: FactPack
    authority: Authority

    @model_validator(mode="after")
    def _factpack_integrity(self) -> RunBundle:
        if not hmac.compare_digest(self.fact_pack.factpack_hash, compute_factpack_hash(self.fact_pack)):
            raise ValueError("RunBundle contains a mismatched FactPack hash")
        return self


def validate_factpack_binding(
    pack: FactPack,
    closure: QueryResultClosureV1,
    snapshot_registry_view: SnapshotRegistryView,
) -> ValidatedFactPackBinding:
    if not isinstance(pack, FactPack) or not isinstance(closure, QueryResultClosureV1) or not isinstance(snapshot_registry_view, SnapshotRegistryView):
        raise TypeError("pack, closure, and snapshot registry view must be parsed contracts")
    if not hmac.compare_digest(pack.snapshot_id, snapshot_registry_view.snapshot_id):
        raise ValueError("FactPack snapshot does not match SnapshotRegistryView")
    if canonical_json_bytes(pack.registries) != canonical_json_bytes(snapshot_registry_view.registry_bundle):
        raise ValueError("FactPack registries do not match SnapshotRegistryView")
    validate_query_result_closure(closure, pack)
    bound_queries: list[tuple[str, SnapshotBoundQuery]] = []
    for query_record in pack.queries:
        reparsed = parse_evidence_query(query_record.normalized_params.model_dump())
        bound_queries.append((query_record.query_id, bind_evidence_query(reparsed, snapshot_registry_view)))
    for fact in pack.facts:
        snapshot_registry_view.assert_scope_resolves(fact.requested_scope)
        snapshot_registry_view.assert_scope_resolves(fact.answered_scope)
    for task in pack.tasks:
        snapshot_registry_view.assert_occupation_resolves(task.classification, task.classification_version, (task.occupation_code,))
    for gap in pack.gaps:
        snapshot_registry_view.assert_scope_resolves(gap.requested_scope)
        if gap.answered_scope is not None:
            snapshot_registry_view.assert_scope_resolves(gap.answered_scope)
    for result in closure.results:
        if result.requested_scope is not None:
            snapshot_registry_view.assert_scope_resolves(result.requested_scope)
        if result.answered_scope is not None:
            snapshot_registry_view.assert_scope_resolves(result.answered_scope)
    return ValidatedFactPackBinding(
        fact_pack=pack,
        closure=closure,
        snapshot_registry_view=snapshot_registry_view,
        bound_queries=tuple(bound_queries),
        _seal=_VALIDATION_SEAL,
    )


def validate_evidence_context(
    context: EvidenceContextV1,
    closure: QueryResultClosureV1,
    snapshot_registry_view: SnapshotRegistryView,
) -> ValidatedEvidenceContext:
    if not isinstance(context, EvidenceContextV1):
        raise TypeError("context must be a parsed EvidenceContextV1")
    expected_closure_hash = sha256_uri(canonical_json_bytes(closure))
    if not hmac.compare_digest(context.query_result_closure_hash, expected_closure_hash):
        raise ValueError("query result closure hash mismatch")
    binding = validate_factpack_binding(context.fact_pack, closure, snapshot_registry_view)
    return _new_validated_evidence_context(context, binding)


def parse_evidence_context(payload: Mapping[str, object]) -> EvidenceContextV1:
    if not isinstance(payload, Mapping):
        raise TypeError("EvidenceContext payload must be a mapping")
    return EvidenceContextV1.model_validate(_tupleize(dict(payload)))


def compute_evidence_context_hash(context: EvidenceContextV1 | ValidatedEvidenceContext) -> str:
    wire_context = context.context if isinstance(context, ValidatedEvidenceContext) else context
    return canonical_contract_hash(wire_context)


def project_runbundle(validated_context: ValidatedEvidenceContext, authority: Authority | Any) -> RunBundle:
    if type(validated_context) is not ValidatedEvidenceContext:
        raise TypeError("project_runbundle requires an exact ValidatedEvidenceContext")
    if _FACTORY_VALIDATED_CONTEXTS.get(id(validated_context)) is not validated_context:
        raise TypeError("project_runbundle requires a ValidatedEvidenceContext")
    context = validated_context.context
    binding = validated_context.binding
    if canonical_json_bytes(context) != validated_context._canonical_context_bytes:
        raise ValueError("validated EvidenceContext no longer matches its factory-bound bytes")
    expected_closure_hash = sha256_uri(canonical_json_bytes(binding.closure))
    if not hmac.compare_digest(context.query_result_closure_hash, expected_closure_hash):
        raise ValueError("validated context closure hash no longer matches its binding")
    if canonical_json_bytes(context.fact_pack) != canonical_json_bytes(binding.fact_pack):
        raise ValueError("validated context FactPack no longer matches its binding")
    validate_factpack_binding(context.fact_pack, binding.closure, binding.snapshot_registry_view)
    selected = authority if isinstance(authority, str) else getattr(authority, "authority", None)
    return RunBundle(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        policy_version=context.policy_version,
        confirmed_case_profile=context.confirmed_case_profile,
        fact_pack=context.fact_pack,
        authority=selected,
    )


def parse_runbundle(payload: Mapping[str, object]) -> RunBundle:
    if not isinstance(payload, Mapping):
        raise TypeError("RunBundle payload must be a mapping")
    return RunBundle.model_validate(_tupleize(dict(payload)))


def compute_runbundle_hash(bundle: RunBundle) -> str:
    return canonical_contract_hash(bundle)


__all__ = [
    "Authority", "ConfirmedCaseProfile", "ConfirmedOccupation", "ConfirmedResponsibility", "ConfirmedSkill",
    "EvidenceContextV1", "RunBundle", "UserGoal", "ValidatedEvidenceContext", "ValidatedFactPackBinding",
    "compute_evidence_context_hash", "compute_runbundle_hash", "parse_evidence_context", "parse_runbundle",
    "project_runbundle", "validate_evidence_context", "validate_factpack_binding",
]
