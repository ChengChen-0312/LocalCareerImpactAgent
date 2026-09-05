"""Closed semantic contracts for the reviewed MVP report workflow."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import AfterValidator, ConfigDict, Field, field_validator, model_validator

from localcareerimpact.contracts.base import StrictContract
from localcareerimpact.contracts.factpack import SHA256_PATTERN


ImpactBand: TypeAlias = Literal["low", "medium", "medium-high", "high"]
ReportHorizon: TypeAlias = Literal["1-3-years", "3-5-years"]
ReportLanguage: TypeAlias = Literal["en", "zh"]
StatementOrigin: TypeAlias = Literal[
    "profile", "rag", "reasoned_scenario", "recommendation"
]
RunFailureCode: TypeAlias = Literal[
    "RETRIEVAL_FAILED",
    "MAIN_AGENT_FAILED",
    "REVIEW_INCOMPLETE",
    "VALIDATION_FAILED",
    "ANALYSIS_INTERRUPTED",
    "INTERNAL_FAILED",
]


def _strip_required(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("required text must contain a non-whitespace character")
    return stripped


Identifier = Annotated[
    str, Field(min_length=1, max_length=128), AfterValidator(_strip_required)
]
ShortText = Annotated[
    str, Field(min_length=1, max_length=500), AfterValidator(_strip_required)
]
NarrativeText = Annotated[
    str, Field(min_length=1, max_length=4_000), AfterValidator(_strip_required)
]


def _unique(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if len(set(values)) != len(values):
        raise ValueError(f"duplicate {label}")
    return values


class GroundedNarrative(StrictContract):
    text: NarrativeText
    origin: StatementOrigin
    evidence_refs: tuple[Identifier, ...]

    @field_validator("evidence_refs")
    @classmethod
    def unique_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "grounded narrative evidence references")


class DraftClaim(StrictContract):
    claim_id: Identifier
    text: NarrativeText
    basis: Literal["profile", "rag", "reasoned_scenario", "recommendation"]
    horizon: ReportHorizon | None
    impact_band: ImpactBand | None
    evidence_refs: tuple[Identifier, ...]
    uncertainty: ShortText

    @field_validator("evidence_refs")
    @classmethod
    def unique_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "claim evidence references")


class TaskImpactRow(StrictContract):
    """`claim_ids[0]` is primary for both narratives; the rest are support."""

    row_id: Identifier
    task: GroundedNarrative
    horizon: ReportHorizon
    automation: ImpactBand
    augmentation: ImpactBand
    human_led: ImpactBand
    rationale: GroundedNarrative
    claim_ids: tuple[Identifier, ...] = Field(min_length=1)

    @field_validator("claim_ids")
    @classmethod
    def unique_references(
        cls, value: tuple[str, ...], info: object
    ) -> tuple[str, ...]:
        field_name = getattr(info, "field_name", "references")
        return _unique(value, field_name)


class Citation(StrictContract):
    evidence_ref: Identifier
    claim_ids: tuple[Identifier, ...] = Field(min_length=1)
    relevance: GroundedNarrative

    @field_validator("claim_ids")
    @classmethod
    def unique_claim_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "citation claim references")


class Suggestion(StrictContract):
    suggestion_id: Identifier
    category: Literal["evidence", "reasoning_boundary", "safety_language"]
    severity: Literal["required", "recommended"]
    affected_claim_ids: tuple[Identifier, ...]
    evidence_refs: tuple[Identifier, ...]
    proposed_correction: NarrativeText

    @field_validator("affected_claim_ids", "evidence_refs")
    @classmethod
    def unique_references(
        cls, value: tuple[str, ...], info: object
    ) -> tuple[str, ...]:
        field_name = getattr(info, "field_name", "references")
        return _unique(value, field_name)


class SuggestionCollection(StrictContract):
    """JSON-object envelope required by the worker protocol; callers receive the tuple."""

    suggestions: tuple[Suggestion, ...] = Field(max_length=3)

    @model_validator(mode="after")
    def unique_suggestion_ids(self) -> SuggestionCollection:
        _unique(
            tuple(item.suggestion_id for item in self.suggestions),
            "suggestion IDs",
        )
        return self


class SuggestionResolution(StrictContract):
    suggestion_id: Identifier
    decision: Literal["accepted", "rejected"]
    reason: ShortText


class ResolutionDecisionPack(StrictContract):
    """The sole main-agent decision for each bounded reviewer suggestion."""

    resolutions: tuple[SuggestionResolution, ...]

    @model_validator(mode="after")
    def unique_resolution_ids(self) -> ResolutionDecisionPack:
        _unique(
            tuple(item.suggestion_id for item in self.resolutions),
            "suggestion resolution IDs",
        )
        return self


class RevisedClaim(DraftClaim):
    """Expose F2's existing evidence-scope gate in the worker JSON Schema too.

    The focused validator still enforces this rule after contract decoding.
    Keeping one object schema preserves the precise evidence_refs error path.
    """

    model_config = ConfigDict(json_schema_extra={
        "if": {
            "properties": {"basis": {"enum": [
                "profile", "reasoned_scenario", "recommendation",
            ]}},
            "required": ["basis"],
        },
        "then": {"properties": {"evidence_refs": {"maxItems": 0}}},
    })
    evidence_refs: tuple[Identifier, ...] = Field(
        description="Use [] unless basis is rag; only rag may cite frozen evidence IDs.",
    )


class RevisedClaimPack(StrictContract):
    """Main-agent claim semantics without report identity or citation objects."""

    overall_impact_band: ImpactBand
    claims: tuple[RevisedClaim, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_claim_ids(self) -> RevisedClaimPack:
        _unique(tuple(item.claim_id for item in self.claims), "revised claim IDs")
        return self


class ClaimSelection(StrictContract):
    """One explicit main-agent binding, reusable for both texts in a pair."""

    primary_claim_id: Identifier
    supporting_claim_ids: tuple[Identifier, ...] = ()

    @field_validator("supporting_claim_ids")
    @classmethod
    def unique_supporting_claim_ids(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        return _unique(value, "supporting claim references")

    @model_validator(mode="after")
    def primary_is_not_supporting(self) -> ClaimSelection:
        if self.primary_claim_id in self.supporting_claim_ids:
            raise ValueError("primary claim cannot also be a supporting claim")
        return self

    @property
    def claim_ids(self) -> tuple[str, ...]:
        """Return primary first, followed by supporting claims in model order."""

        return (self.primary_claim_id, *self.supporting_claim_ids)

    def narrative(self, text: str) -> NarrativeDecision:
        """Attach prose without selecting, merging or reordering references."""
        return NarrativeDecision(
            text=text,
            primary_claim_id=self.primary_claim_id,
            supporting_claim_ids=self.supporting_claim_ids,
        )


class NarrativeDecision(ClaimSelection):
    """Standalone visible prose plus its explicit claim selection."""

    text: NarrativeText


class SemanticNarrativeSection(StrictContract):
    summary: NarrativeDecision


class SemanticHorizonScenario(ClaimSelection):
    horizon: ReportHorizon
    impact_band: ImpactBand
    summary_text: NarrativeText
    uncertainty_text: NarrativeText


class SemanticTaskImpactRow(ClaimSelection):
    task_text: NarrativeText
    horizon: ReportHorizon
    automation: ImpactBand
    augmentation: ImpactBand
    human_led: ImpactBand
    rationale_text: NarrativeText


class SemanticActionItem(ClaimSelection):
    action_text: NarrativeText
    rationale_text: NarrativeText


class SemanticReportSections(StrictContract):
    occupation_summary: SemanticNarrativeSection
    horizon_scenarios: tuple[SemanticHorizonScenario, ...] = Field(
        min_length=2, max_length=2
    )
    task_impact_matrix: tuple[SemanticTaskImpactRow, ...] = Field(min_length=1)
    opportunities: SemanticNarrativeSection
    risks_and_uncertainty: SemanticNarrativeSection
    practical_next_actions: tuple[SemanticActionItem, ...] = Field(min_length=1)


class ReportNarrativePack(StrictContract):
    """Main-agent visible prose without mechanical report bookkeeping."""

    title: ShortText
    sections: SemanticReportSections


class NarrativeSection(StrictContract):
    """`claim_ids[0]` is primary; remaining IDs are supporting claims."""

    summary: GroundedNarrative
    claim_ids: tuple[Identifier, ...] = Field(min_length=1)

    @field_validator("claim_ids")
    @classmethod
    def unique_claim_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "section claim references")


class HorizonScenario(StrictContract):
    """`claim_ids[0]` is primary for both narratives; the rest are support."""

    horizon: ReportHorizon
    impact_band: ImpactBand
    summary: GroundedNarrative
    claim_ids: tuple[Identifier, ...] = Field(min_length=1)
    uncertainty: GroundedNarrative

    @field_validator("claim_ids")
    @classmethod
    def unique_claim_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "horizon claim references")


class ActionItem(StrictContract):
    """`claim_ids[0]` is primary for both narratives; the rest are support."""

    action_id: Identifier
    action: GroundedNarrative
    rationale: GroundedNarrative
    claim_ids: tuple[Identifier, ...] = Field(min_length=1)

    @field_validator("claim_ids")
    @classmethod
    def unique_claim_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "action claim references")


class ReportSections(StrictContract):
    occupation_summary: NarrativeSection
    horizon_scenarios: tuple[HorizonScenario, ...] = Field(min_length=2, max_length=2)
    task_impact_matrix: tuple[TaskImpactRow, ...] = Field(min_length=1)
    opportunities: NarrativeSection
    risks_and_uncertainty: NarrativeSection
    practical_next_actions: tuple[ActionItem, ...] = Field(min_length=1)


class CareerImpactDraft(StrictContract):
    schema_version: Literal["mvp-draft.v1"]
    run_id: Identifier
    snapshot_id: str = Field(pattern=SHA256_PATTERN)
    language: ReportLanguage
    overall_impact_band: ImpactBand
    claims: tuple[DraftClaim, ...] = Field(min_length=1)
    task_impacts: tuple[TaskImpactRow, ...] = Field(min_length=1)
    uncertainties: tuple[ShortText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_local_ids(self) -> CareerImpactDraft:
        claim_ids = tuple(item.claim_id for item in self.claims)
        _unique(claim_ids, "draft claim IDs")
        _unique(tuple(item.row_id for item in self.task_impacts), "draft task row IDs")
        if any(
            not set(row.claim_ids) <= set(claim_ids) for row in self.task_impacts
        ):
            raise ValueError("draft task row refers to an unknown claim")
        return self


class MvpReportV1(StrictContract):
    schema_version: Literal["mvp-report.v1"]
    run_id: Identifier
    snapshot_id: str = Field(pattern=SHA256_PATTERN)
    language: ReportLanguage
    title: ShortText
    overall_impact_band: ImpactBand
    claims: tuple[DraftClaim, ...] = Field(min_length=1)
    sections: ReportSections
    citations: tuple[Citation, ...]
    suggestion_resolutions: tuple[SuggestionResolution, ...]


__all__ = [
    "ActionItem",
    "CareerImpactDraft",
    "Citation",
    "ClaimSelection",
    "DraftClaim",
    "GroundedNarrative",
    "HorizonScenario",
    "ImpactBand",
    "MvpReportV1",
    "NarrativeDecision",
    "NarrativeSection",
    "ReportNarrativePack",
    "ReportHorizon",
    "ReportLanguage",
    "ReportSections",
    "ResolutionDecisionPack",
    "RevisedClaimPack",
    "RunFailureCode",
    "SemanticActionItem",
    "SemanticHorizonScenario",
    "SemanticNarrativeSection",
    "SemanticReportSections",
    "SemanticTaskImpactRow",
    "Suggestion",
    "SuggestionCollection",
    "SuggestionResolution",
    "StatementOrigin",
    "TaskImpactRow",
]
