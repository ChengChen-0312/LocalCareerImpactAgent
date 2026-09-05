"""Pure mechanical assembly from staged main-agent decisions to the MVP report."""

from __future__ import annotations

from localcareerimpact.contracts.factpack import FactPack

from .contracts import (
    ActionItem,
    Citation,
    DraftClaim,
    GroundedNarrative,
    HorizonScenario,
    MvpReportV1,
    NarrativeDecision,
    NarrativeSection,
    ReportLanguage,
    ReportNarrativePack,
    ReportSections,
    ResolutionDecisionPack,
    RevisedClaimPack,
    Suggestion,
    TaskImpactRow,
)
from .validator import (
    citation_relevance_text,
    validate_report,
    validate_report_narrative_pack,
    validate_resolution_decision_pack,
    validate_revised_claim_pack,
)


class ReportAssemblyError(RuntimeError):
    """A content-free failure at the deterministic assembly boundary."""


_ASSEMBLY_ERROR = "report assembly failed"


def assemble_report(
    *,
    run_id: str,
    snapshot_id: str,
    language: ReportLanguage,
    decisions: ResolutionDecisionPack,
    draft_claims: tuple[DraftClaim, ...],
    claims: RevisedClaimPack,
    narratives: ReportNarrativePack,
    suggestions: tuple[Suggestion, ...],
    fact_pack: FactPack,
) -> MvpReportV1:
    """Assemble only when F2 exactly preserves the original draft claim IDs."""

    try:
        if fact_pack.snapshot_id != snapshot_id:
            raise ReportAssemblyError(_ASSEMBLY_ERROR)
        if not validate_resolution_decision_pack(
            decisions,
            suggestions=suggestions,
            language=language,
        ).valid:
            raise ReportAssemblyError(_ASSEMBLY_ERROR)
        if not validate_revised_claim_pack(
            claims,
            draft_claims=draft_claims,
            fact_pack=fact_pack,
            language=language,
        ).valid:
            raise ReportAssemblyError(_ASSEMBLY_ERROR)
        if not validate_report_narrative_pack(
            narratives,
            revised_claims=claims.claims,
            language=language,
        ).valid:
            raise ReportAssemblyError(_ASSEMBLY_ERROR)

        claim_by_id = {item.claim_id: item for item in claims.claims}

        def grounded(decision: NarrativeDecision) -> GroundedNarrative:
            linked_claims = tuple(claim_by_id[item] for item in decision.claim_ids)
            return GroundedNarrative(
                text=decision.text,
                origin=linked_claims[0].basis,
                evidence_refs=tuple(
                    sorted(
                        {
                            evidence_ref
                            for claim in linked_claims
                            for evidence_ref in claim.evidence_refs
                        }
                    )
                ),
            )

        semantic = narratives.sections
        report_sections = ReportSections(
            occupation_summary=NarrativeSection(
                summary=grounded(semantic.occupation_summary.summary),
                claim_ids=semantic.occupation_summary.summary.claim_ids,
            ),
            horizon_scenarios=tuple(
                HorizonScenario(
                    horizon=item.horizon,
                    impact_band=item.impact_band,
                    summary=grounded(item.narrative(item.summary_text)),
                    claim_ids=item.claim_ids,
                    uncertainty=grounded(item.narrative(item.uncertainty_text)),
                )
                for item in semantic.horizon_scenarios
            ),
            task_impact_matrix=tuple(
                TaskImpactRow(
                    row_id=f"task-{index:03d}",
                    task=grounded(item.narrative(item.task_text)),
                    horizon=item.horizon,
                    automation=item.automation,
                    augmentation=item.augmentation,
                    human_led=item.human_led,
                    rationale=grounded(item.narrative(item.rationale_text)),
                    claim_ids=item.claim_ids,
                )
                for index, item in enumerate(semantic.task_impact_matrix, start=1)
            ),
            opportunities=NarrativeSection(
                summary=grounded(semantic.opportunities.summary),
                claim_ids=semantic.opportunities.summary.claim_ids,
            ),
            risks_and_uncertainty=NarrativeSection(
                summary=grounded(semantic.risks_and_uncertainty.summary),
                claim_ids=semantic.risks_and_uncertainty.summary.claim_ids,
            ),
            practical_next_actions=tuple(
                ActionItem(
                    action_id=f"action-{index:03d}",
                    action=grounded(item.narrative(item.action_text)),
                    rationale=grounded(item.narrative(item.rationale_text)),
                    claim_ids=item.claim_ids,
                )
                for index, item in enumerate(
                    semantic.practical_next_actions, start=1
                )
            ),
        )
        rag_evidence_refs = sorted(
            {
                evidence_ref
                for claim in claims.claims
                if claim.basis == "rag"
                for evidence_ref in claim.evidence_refs
            }
        )
        citations = tuple(
            Citation(
                evidence_ref=evidence_ref,
                claim_ids=tuple(
                    sorted(
                        claim.claim_id
                        for claim in claims.claims
                        if claim.basis == "rag"
                        and evidence_ref in claim.evidence_refs
                    )
                ),
                relevance=GroundedNarrative(
                    text=citation_relevance_text(language),
                    origin="rag",
                    evidence_refs=(evidence_ref,),
                ),
            )
            for evidence_ref in rag_evidence_refs
        )
        report = MvpReportV1(
            schema_version="mvp-report.v1",
            run_id=run_id,
            snapshot_id=snapshot_id,
            language=language,
            title=narratives.title,
            overall_impact_band=claims.overall_impact_band,
            claims=claims.claims,
            sections=report_sections,
            citations=citations,
            suggestion_resolutions=decisions.resolutions,
        )
        result = validate_report(
            report,
            run_id=run_id,
            snapshot_id=snapshot_id,
            language=language,
            fact_pack=fact_pack,
            suggestions=suggestions,
        )
        if not result.valid:
            raise ReportAssemblyError(_ASSEMBLY_ERROR)
        return report
    except ReportAssemblyError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ReportAssemblyError(_ASSEMBLY_ERROR) from exc


__all__ = ["ReportAssemblyError", "assemble_report"]
