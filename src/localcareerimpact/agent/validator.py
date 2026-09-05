"""Deterministic validation applied after the sole final 30B decision."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Literal, TypeAlias

from localcareerimpact.contracts.factpack import FactPack

from .contracts import (
    CareerImpactDraft,
    DraftClaim,
    GroundedNarrative,
    MvpReportV1,
    NarrativeDecision,
    ReportLanguage,
    ReportNarrativePack,
    ResolutionDecisionPack,
    RevisedClaimPack,
    Suggestion,
)
from .language import classify_narrative_language


ValidationCategoryCode: TypeAlias = Literal[
    "REPORT_BINDING",
    "HORIZON_STRUCTURE",
    "LOCAL_REFERENCE",
    "EVIDENCE_REFERENCE",
    "RAG_NUMERIC_GROUNDING",
    "ORIGIN_LINKAGE",
    "NARRATIVE_LINKAGE",
    "CITATION_LINKAGE",
    "SUGGESTION_REFERENCE",
    "SUGGESTION_RESOLUTION",
    "PERSONAL_PROBABILITY",
    "UNSTRUCTURED_NUMERIC",
    "VISIBLE_LANGUAGE",
    "INTERNAL_VALIDATOR",
]
StageValidationCategoryCode: TypeAlias = Literal[
    "DRAFT_BINDING",
    "SUGGESTION_BINDING",
    "CLAIM_BINDING",
    "EVIDENCE_SCOPE",
    "EVIDENCE_REFERENCE",
    "NARRATIVE_REFERENCE",
    "HORIZON_STRUCTURE",
    "PERSONAL_PROBABILITY",
    "VISIBLE_LANGUAGE",
]

_NUMERIC = re.compile(
    r"(?<![\w])(?:\d+(?:\.\d+)?|\.\d+)\s*(?:%|percent|percentage)?", re.I
)
_ZERO_TO_TEN_WORD = r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten)"
_ZERO_TO_TWENTY_WORD = (
    r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)"
)
_WORD_SEPARATOR = r"(?:\s*-\s*|\s+)"
_ENGLISH_SMALL_NUMBER = re.compile(rf"(?i)\b{_ZERO_TO_TEN_WORD}\b")
_ENGLISH_WORD_PERCENT = re.compile(
    rf"(?i)\b{_ZERO_TO_TWENTY_WORD}\s+percent(?:age)?\b"
)
_ENGLISH_WORD_RATIO = re.compile(
    rf"(?i)\b{_ZERO_TO_TEN_WORD}{_WORD_SEPARATOR}"
    rf"(?:in|out\s+of){_WORD_SEPARATOR}{_ZERO_TO_TEN_WORD}\b"
)
_ENGLISH_WORD_FRACTION = re.compile(
    rf"(?i)\b(?:one|two|three|four|five|six|seven|eight|nine|ten)"
    rf"{_WORD_SEPARATOR}(?:half|halves|thirds?|quarters?|fourths?|fifths?|"
    r"sixths?|sevenths?|eighths?|ninths?|tenths?)\b"
)
_CHINESE_NUMBER_EXPRESSION = re.compile(
    r"(?:百分之|[零〇一二两三四五六七八九十百千万\d]+分之)"
    r"[零〇一二两三四五六七八九十百千万\d]+|"
    r"[零〇一二两三四五六七八九十\d]+成"
    r"(?:半|[零〇一二两三四五六七八九])?"
)
_STRUCTURAL_HORIZON = re.compile(
    r"(?i)\b(?:1\s*[-–—]\s*3|3\s*[-–—]\s*5)\s*(?:years?|年)\b|"
    r"(?:1\s*(?:至|到)\s*3|3\s*(?:至|到)\s*5)\s*年"
)
_PERSONAL_JOB_LOSS = re.compile(
    r"(?is)(?:"
    r"\b(?:you|your|yours|i|me|my|mine|she|her|hers|he|him|his|"
    r"this\s+person|that\s+person|the\s+person)\b.{0,160}"
    r"\b(?:job[ -]?loss|los(?:e|ing|t)\s+(?:(?:the|your|my|their|his|her)\s+)?job|"
    r"(?:be(?:come|coming)?\s+)?unemploy(?:ed|ment)?|layoff|laid\s+off|redundan(?:t|cy))\b"
    r"|\b(?:your|my)\s+(?:job|role|position)\b.{0,100}"
    r"\b(?:loss|lost|at\s+risk|eliminat(?:ed|ion)|replac(?:ed|ement))\b"
    r"|\b(?:job[ -]?loss|unemploy(?:ed|ment)?|layoff|laid\s+off|redundan(?:t|cy))\b"
    r".{0,100}\b(?:for|to)\s+"
    r"(?:you|me|her|him|this\s+person|that\s+person|the\s+person)\b"
    r"|\b(?:(?:the|this|that|a|an)\s+)?(?:candidate|user)\b"
    r"(?!\s+(?:survey|study|sample|population|group|cohort|statistics?|data))"
    r".{0,160}\b(?:job[ -]?loss|los(?:e|ing|t)\s+"
    r"(?:(?:the|your|my|their|his|her)\s+)?job|"
    r"unemploy(?:ed|ment)?|layoff|laid\s+off|redundan(?:t|cy))\b"
    r"|\b(?:job[ -]?loss|unemploy(?:ed|ment)?|layoff|laid\s+off|redundan(?:t|cy))\b"
    r".{0,100}\b(?:for|to)\s+(?:(?:the|this|that)\s+)?(?:candidate|user)\b"
    r"|(?:你|您|我|本人|你的|您的|我的).{0,100}"
    r"(?:失业|失去工作|岗位丧失|被裁员|被取代|工作不保|职位不保)"
    r"|(?:失业|失去工作|岗位丧失|被裁员|被取代|工作不保|职位不保)"
    r".{0,80}(?:你|您|我|本人|你的|您的|我的)"
    r"|(?:(?:该|此|这个|这名|这位|一名|一位)(?:候选人|用户)"
    r"(?!\s*(?:调查|调研|研究|样本|群体|总体|统计|数据))|"
    r"(?:候选人|用户)(?:本人|个人|的)).{0,100}"
    r"(?:失业|失去工作|岗位丧失|被裁员|被取代|工作不保|职位不保)"
    r"|(?:失业|失去工作|岗位丧失|被裁员|被取代|工作不保|职位不保)"
    r".{0,80}(?:(?:该|此|这个|这名|这位)(?:候选人|用户)|"
    r"(?:候选人|用户)(?:本人|个人))"
    r")"
)
_POPULATION_NARRATOR = re.compile(
    r"(?is)\bI\s+(?:surveyed|studied|asked|analysed|analyzed|measured|reported)\b"
    r".{0,120}\b(?:candidates?|users?|workers?|employees?|respondents?|people|population)\b"
    r"|我.{0,30}(?:调查|调研|研究|询问|统计).{0,80}"
    r"(?:候选人|用户|员工|劳动者|受访者|人群)"
)
_AGGREGATE_PERSONAL_SCOPE = re.compile(
    r"(?is)\b(?:for|within|across|among)\s+"
    r"(?:(?:your|the|this)\s+)?"
    r"(?:occupation|sector|industry|workforce|field|employers?)\b|"
    r"\b(?:your|the|this)\s+"
    r"(?:occupation|sector|industry|workforce|field)\b|"
    r"(?:针对|对于|在|整个)?(?:你的|您的|该|本)?"
    r"(?:职业|行业|部门|领域|劳动力|雇主群体)"
)
_SURVEY_MEMBER = re.compile(
    r"(?is)\b(?:candidate|user)\b.{0,60}\b(?:in|within|from)\s+"
    r"(?:(?:this|the|a)\s+)?(?:survey|study|sample|cohort|population)\b|"
    r"(?:调查|调研|研究|样本|队列|总体)(?:中|内|里的|中的).{0,20}"
    r"(?:候选人|用户)|(?:候选人|用户).{0,40}"
    r"(?:调查|调研|研究|样本|队列|总体)(?:中|内|里的|中的)"
)
_POPULATION_MARKER = re.compile(
    r"(?is)\b(?:candidates|users|workers|employees|respondents|people|"
    r"employers|occupations|sectors|industries|workforces|populations)\b|"
    r"(?:劳动者|员工群体|候选人群体|用户群体|受访者|人群|雇主|企业群体|总体)"
)
_RATE_STATISTIC_CONTEXT = re.compile(
    r"(?is)\b(?:rate|statistic|share|proportion|average|prevalence|incidence|"
    r"aggregate|across\s+employers|among\s+workers)\b|"
    r"(?:比率|比例|占比|统计|平均|发生率|总体率|跨雇主|雇主之间)"
)
_EXACT_PROBABILITY = re.compile(
    r"(?ix)(?:"
    r"\d+(?:\.\d+)?\s*(?:%|percent(?:age)?)"
    r"|\b(?:one|1)\s*(?:-|\s)in(?:-|\s)(?:two|three|four|five|six|seven|eight|nine|ten|\d+)\b"
    r"|\b\d+\s*/\s*\d+\b"
    r"|\b(?:0?\.\d+|1(?:\.0+)?)\s*(?:probability|chance|risk)\b"
    r"|\b(?:probability|chance|risk)\s*"
    r"(?:is|of|=|:|estimated\s+at|set\s+at)?\s*(?:0?\.\d+|1(?:\.0+)?)\b"
    r"|\b(?:probability|chance|risk)\b.{0,60}\b(?:0?\.\d+|1(?:\.0+)?)\b"
    r"|(?:概率|风险|可能性)\s*(?:为|是|达到|约|:|：)?\s*(?:0?\.\d+|1(?:\.0+)?)"
    r"|(?:百分之|[零〇一二两三四五六七八九十百千万\d]+分之)"
    r"[零〇一二两三四五六七八九十百千万\d]+"
    r"|[零〇一二两三四五六七八九十\d]+成"
    r"(?:半|[零〇一二两三四五六七八九])?"
    r")"
)


@dataclass(frozen=True, slots=True)
class ReportValidationResult:
    errors: tuple[str, ...]
    category_counts: tuple[tuple[ValidationCategoryCode, int], ...]

    @property
    def valid(self) -> bool:
        return not self.errors

    def category_count_map(self) -> dict[str, int]:
        return dict(self.category_counts)


@dataclass(frozen=True, slots=True)
class StageValidationResult:
    """Content-free outcome for one staged semantic contract."""

    errors: tuple[StageValidationCategoryCode, ...]
    error_path: tuple[int | str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    def category_count_map(self) -> dict[str, int]:
        return dict(Counter(self.errors))


def _stage_result(
    errors: list[StageValidationCategoryCode],
) -> StageValidationResult:
    return StageValidationResult(errors=tuple(errors))


def validate_resolution_decision_pack(
    decisions: ResolutionDecisionPack,
    *,
    suggestions: tuple[Suggestion, ...],
    language: ReportLanguage,
) -> StageValidationResult:
    """Validate F1 binding and the visible resolution prose."""

    errors: list[StageValidationCategoryCode] = []
    expected = Counter(item.suggestion_id for item in suggestions)
    actual = Counter(item.suggestion_id for item in decisions.resolutions)
    if expected != actual:
        errors.append("SUGGESTION_BINDING")
    texts = tuple(item.reason for item in decisions.resolutions)
    if contains_personal_job_loss_probability("\n".join(texts)):
        errors.append("PERSONAL_PROBABILITY")
    if texts and not matches_report_language(texts, language):
        errors.append("VISIBLE_LANGUAGE")
    return _stage_result(errors)


def validate_draft_binding(
    draft: CareerImpactDraft,
    *,
    run_id: str,
    snapshot_id: str,
    language: ReportLanguage,
) -> StageValidationResult:
    """Keep the existing draft identity gate inside the recorded attempt."""

    field = next((
        name for name, expected in (
            ("run_id", run_id), ("snapshot_id", snapshot_id), ("language", language),
        ) if getattr(draft, name) != expected
    ), None)
    return StageValidationResult(
        errors=("DRAFT_BINDING",) if field else (),
        error_path=(field,) if field else (),
    )


def validate_revised_claim_pack(
    revised: RevisedClaimPack,
    *,
    draft_claims: tuple[DraftClaim, ...],
    fact_pack: FactPack,
    language: ReportLanguage,
) -> StageValidationResult:
    """Validate F2 identity, evidence scope, and visible claim prose."""

    errors: list[StageValidationCategoryCode] = []
    expected = Counter(item.claim_id for item in draft_claims)
    actual = Counter(item.claim_id for item in revised.claims)
    if expected != actual:
        errors.append("CLAIM_BINDING")
    valid_evidence = fact_pack_record_ids(fact_pack)
    invalid_scope_index = next((
        index for index, item in enumerate(revised.claims)
        if item.basis != "rag" and item.evidence_refs
    ), None)
    if invalid_scope_index is not None:
        errors.append("EVIDENCE_SCOPE")
    if any(
        evidence_ref not in valid_evidence
        for item in revised.claims
        for evidence_ref in item.evidence_refs
    ):
        errors.append("EVIDENCE_REFERENCE")
    texts = tuple(
        value
        for item in revised.claims
        for value in (item.text, item.uncertainty)
    )
    if contains_personal_job_loss_probability("\n".join(texts)):
        errors.append("PERSONAL_PROBABILITY")
    if not matches_report_language(texts, language):
        errors.append("VISIBLE_LANGUAGE")
    return StageValidationResult(
        errors=tuple(errors),
        error_path=("claims", invalid_scope_index, "evidence_refs")
        if errors and errors[0] == "EVIDENCE_SCOPE" else (),
    )


def validate_report_narrative_pack(
    narratives: ReportNarrativePack,
    *,
    revised_claims: tuple[DraftClaim, ...],
    language: ReportLanguage,
) -> StageValidationResult:
    """Validate F3 claim references, exact horizons, and visible prose."""

    errors: list[StageValidationCategoryCode] = []
    valid_claim_ids = {item.claim_id for item in revised_claims}
    decisions = tuple(_narrative_decisions(narratives))
    if any(
        claim_id not in valid_claim_ids
        for decision in decisions
        for claim_id in decision.claim_ids
    ):
        errors.append("NARRATIVE_REFERENCE")
    horizons = Counter(
        item.horizon for item in narratives.sections.horizon_scenarios
    )
    if horizons != Counter(("1-3-years", "3-5-years")):
        errors.append("HORIZON_STRUCTURE")
    texts = (narratives.title, *(item.text for item in decisions))
    if contains_personal_job_loss_probability("\n".join(texts)):
        errors.append("PERSONAL_PROBABILITY")
    if not matches_report_language(texts, language):
        errors.append("VISIBLE_LANGUAGE")
    return _stage_result(errors)


def _narrative_decisions(
    narratives: ReportNarrativePack,
) -> tuple[NarrativeDecision, ...]:
    sections = narratives.sections
    values: list[NarrativeDecision] = [
        sections.occupation_summary.summary,
        sections.opportunities.summary,
        sections.risks_and_uncertainty.summary,
    ]
    for scenario in sections.horizon_scenarios:
        values.extend((
            scenario.narrative(scenario.summary_text),
            scenario.narrative(scenario.uncertainty_text),
        ))
    for row in sections.task_impact_matrix:
        values.extend((row.narrative(row.task_text), row.narrative(row.rationale_text)))
    for action in sections.practical_next_actions:
        values.extend((
            action.narrative(action.action_text), action.narrative(action.rationale_text),
        ))
    return tuple(values)


def _validation_category(error: str) -> ValidationCategoryCode:
    if error.startswith(
        ("schema_version ", "run_id ", "snapshot_id ", "report language ")
    ):
        return "REPORT_BINDING"
    if error.startswith("horizons "):
        return "HORIZON_STRUCTURE"
    if error.startswith("duplicate report citation references:"):
        return "CITATION_LINKAGE"
    if error.startswith("duplicate ") or " refers to unknown claims:" in error:
        return "LOCAL_REFERENCE"
    if " is absent from the frozen FactPack" in error or error.endswith(
        " has no report citation"
    ):
        return "EVIDENCE_REFERENCE"
    if error.startswith("RAG numeric claim ") or error.endswith(
        " has an unlinked numeric RAG assertion"
    ):
        return "RAG_NUMERIC_GROUNDING"
    if error.endswith(" origin does not match its linked claims"):
        return "ORIGIN_LINKAGE"
    if error.endswith(" evidence references do not match its linked claims"):
        return "NARRATIVE_LINKAGE"
    if error.startswith("citation ") or error.startswith("report citations "):
        return "CITATION_LINKAGE"
    if error.startswith("non-RAG claim "):
        return "EVIDENCE_REFERENCE"
    if error.startswith("suggestion ") and error.endswith(
        " refers to unavailable evidence"
    ):
        return "SUGGESTION_REFERENCE"
    if error.startswith(
        ("unresolved reviewer suggestions:", "unexpected or duplicate suggestion resolutions:")
    ):
        return "SUGGESTION_RESOLUTION"
    if error == "unsupported exact personal job-loss probability is forbidden":
        return "PERSONAL_PROBABILITY"
    if error.startswith(
        (
            "report title cannot carry ",
            "suggestion resolution reason cannot carry ",
        )
    ):
        return "UNSTRUCTURED_NUMERIC"
    if error.startswith("visible report text does not match language "):
        return "VISIBLE_LANGUAGE"
    return "INTERNAL_VALIDATOR"


def validate_report(
    report: MvpReportV1,
    *,
    run_id: str,
    snapshot_id: str,
    language: ReportLanguage,
    fact_pack: FactPack,
    suggestions: tuple[Suggestion, ...],
) -> ReportValidationResult:
    errors: list[str] = []
    if report.schema_version != "mvp-report.v1":
        errors.append("schema_version must be exactly mvp-report.v1")
    if report.run_id != run_id:
        errors.append("run_id does not match the analysis run")
    if report.snapshot_id != snapshot_id or fact_pack.snapshot_id != snapshot_id:
        errors.append("snapshot_id does not match the frozen FactPack")
    if report.language != language:
        errors.append(f"report language must be {language}")

    scenario_horizons = Counter(
        scenario.horizon for scenario in report.sections.horizon_scenarios
    )
    if scenario_horizons != Counter(("1-3-years", "3-5-years")):
        errors.append("horizons must contain 1-3-years and 3-5-years exactly once")

    _validate_report_local_integrity(report, errors)

    valid_evidence = fact_pack_record_ids(fact_pack)
    citation_refs = {citation.evidence_ref for citation in report.citations}
    for evidence_ref in sorted(citation_refs - valid_evidence):
        errors.append(f"citation {evidence_ref} is absent from the frozen FactPack")

    claim_by_id = {claim.claim_id: claim for claim in report.claims}
    for claim in report.claims:
        if claim.basis != "rag" and claim.evidence_refs:
            errors.append(
                f"non-RAG claim {claim.claim_id} must not carry evidence references"
            )
        _validate_evidence_refs(
            claim.evidence_refs,
            citation_refs,
            valid_evidence,
            f"claim {claim.claim_id}",
            errors,
        )
        if (
            claim.basis == "rag"
            and (_has_factual_numeric(claim.text) or _has_factual_numeric(claim.uncertainty))
            and not claim.evidence_refs
        ):
            errors.append(f"RAG numeric claim {claim.claim_id} has no evidence reference")

    rag_evidence_refs = {
        evidence_ref
        for claim in report.claims
        if claim.basis == "rag"
        for evidence_ref in claim.evidence_refs
    }
    if citation_refs != rag_evidence_refs:
        errors.append("report citations must exactly match evidence used by RAG claims")
    for citation in report.citations:
        expected_claim_ids = tuple(
            sorted(
                claim.claim_id
                for claim in report.claims
                if claim.basis == "rag"
                and citation.evidence_ref in claim.evidence_refs
            )
        )
        if citation.claim_ids != expected_claim_ids:
            errors.append(
                f"citation {citation.evidence_ref} claim links do not match RAG claims"
            )
        if citation.relevance.text != citation_relevance_text(language):
            errors.append(
                f"citation {citation.evidence_ref} relevance must use fixed localized text"
            )

    for row in report.sections.task_impact_matrix:
        _validate_grounded_narrative(
            row.task,
            claim_ids=row.claim_ids,
            claim_by_id=claim_by_id,
            citation_refs=citation_refs,
            valid_evidence=valid_evidence,
            label=f"task row {row.row_id} task",
            errors=errors,
        )
        _validate_grounded_narrative(
            row.rationale,
            claim_ids=row.claim_ids,
            claim_by_id=claim_by_id,
            citation_refs=citation_refs,
            valid_evidence=valid_evidence,
            label=f"task row {row.row_id} rationale",
            errors=errors,
        )
    _validate_section_narratives(
        report,
        claim_by_id=claim_by_id,
        citation_refs=citation_refs,
        valid_evidence=valid_evidence,
        errors=errors,
    )

    for suggestion in suggestions:
        unknown_refs = set(suggestion.evidence_refs) - valid_evidence
        if unknown_refs:
            errors.append(
                f"suggestion {suggestion.suggestion_id} refers to unavailable evidence"
            )
    expected_resolutions = Counter(item.suggestion_id for item in suggestions)
    actual_resolutions = Counter(
        item.suggestion_id for item in report.suggestion_resolutions
    )
    if expected_resolutions != actual_resolutions:
        missing = sorted((expected_resolutions - actual_resolutions).elements())
        extra = sorted((actual_resolutions - expected_resolutions).elements())
        if missing:
            errors.append("unresolved reviewer suggestions: " + ", ".join(missing))
        if extra:
            errors.append("unexpected or duplicate suggestion resolutions: " + ", ".join(extra))

    visible_texts = _visible_texts(report)
    narrative = "\n".join(visible_texts)
    if contains_personal_job_loss_probability(narrative):
        errors.append("unsupported exact personal job-loss probability is forbidden")
    if _has_factual_numeric(report.title):
        errors.append("report title cannot carry an unstructured numeric assertion")
    if any(
        _has_factual_numeric(item.reason) for item in report.suggestion_resolutions
    ):
        errors.append(
            "suggestion resolution reason cannot carry an unstructured numeric assertion"
        )
    if not matches_report_language(visible_texts, language):
        errors.append(f"visible report text does not match language {language}")
    unique_errors = tuple(dict.fromkeys(errors))
    category_counts = Counter(_validation_category(error) for error in unique_errors)
    return ReportValidationResult(
        errors=unique_errors,
        category_counts=tuple(sorted(category_counts.items())),
    )


def _validate_report_local_integrity(
    report: MvpReportV1,
    errors: list[str],
) -> None:
    claim_ids = tuple(item.claim_id for item in report.claims)
    valid_claim_ids = set(claim_ids)
    duplicate_groups = (
        ("report claim IDs", claim_ids),
        (
            "task row IDs",
            tuple(item.row_id for item in report.sections.task_impact_matrix),
        ),
        (
            "action IDs",
            tuple(item.action_id for item in report.sections.practical_next_actions),
        ),
        (
            "report citation references",
            tuple(item.evidence_ref for item in report.citations),
        ),
    )
    for label, values in duplicate_groups:
        duplicates = sorted(value for value, count in Counter(values).items() if count > 1)
        if duplicates:
            errors.append(f"duplicate {label}: " + ", ".join(duplicates))

    reference_groups: list[tuple[str, tuple[str, ...]]] = [
        ("occupation summary", report.sections.occupation_summary.claim_ids),
        ("opportunities", report.sections.opportunities.claim_ids),
        ("risks and uncertainty", report.sections.risks_and_uncertainty.claim_ids),
    ]
    reference_groups.extend(
        (f"{item.horizon} scenario", item.claim_ids)
        for item in report.sections.horizon_scenarios
    )
    reference_groups.extend(
        (f"task row {item.row_id}", item.claim_ids)
        for item in report.sections.task_impact_matrix
    )
    reference_groups.extend(
        (f"action {item.action_id}", item.claim_ids)
        for item in report.sections.practical_next_actions
    )
    reference_groups.extend(
        (f"citation {item.evidence_ref}", item.claim_ids)
        for item in report.citations
    )
    for label, references in reference_groups:
        unknown = sorted(set(references) - valid_claim_ids)
        if unknown:
            errors.append(f"{label} refers to unknown claims: " + ", ".join(unknown))


def _validate_evidence_refs(
    references: tuple[str, ...],
    citation_refs: set[str],
    valid_evidence: set[str],
    label: str,
    errors: list[str],
) -> None:
    for evidence_ref in references:
        if evidence_ref not in valid_evidence:
            errors.append(f"{label} evidence {evidence_ref} is absent from the frozen FactPack")
        if evidence_ref not in citation_refs:
            errors.append(f"{label} evidence {evidence_ref} has no report citation")


def fact_pack_record_ids(fact_pack: FactPack) -> set[str]:
    """Return every unambiguous record identifier in a frozen FactPack."""

    identifiers: set[str] = set()
    identifiers.update(item.fact_id for item in fact_pack.facts)
    identifiers.update(item.evidence_id for item in fact_pack.passages)
    identifiers.update(item.task_id for item in fact_pack.tasks)
    identifiers.update(item.method_id for item in fact_pack.methods)
    identifiers.update(item.source_id for item in fact_pack.sources)
    return identifiers


def _validate_grounded_narrative(
    narrative: GroundedNarrative,
    *,
    claim_ids: tuple[str, ...],
    claim_by_id: dict[str, object],
    citation_refs: set[str],
    valid_evidence: set[str],
    label: str,
    errors: list[str],
    require_claim_evidence_union: bool = True,
) -> None:
    _validate_evidence_refs(
        narrative.evidence_refs,
        citation_refs,
        valid_evidence,
        label,
        errors,
    )
    linked_claims = [claim_by_id[item] for item in claim_ids if item in claim_by_id]
    if linked_claims and narrative.origin != getattr(linked_claims[0], "basis"):
        errors.append(f"{label} origin does not match its linked claims")
    expected_evidence_refs = tuple(
        sorted(
            {
                evidence_ref
                for claim in linked_claims
                for evidence_ref in getattr(claim, "evidence_refs")
            }
        )
    )
    if require_claim_evidence_union and narrative.evidence_refs != expected_evidence_refs:
        errors.append(f"{label} evidence references do not match its linked claims")
    if (
        narrative.origin == "rag"
        and _has_factual_numeric(narrative.text)
        and not narrative.evidence_refs
    ):
        errors.append(f"{label} has an unlinked numeric RAG assertion")


def _validate_section_narratives(
    report: MvpReportV1,
    *,
    claim_by_id: dict[str, object],
    citation_refs: set[str],
    valid_evidence: set[str],
    errors: list[str],
) -> None:
    sections = (
        ("occupation summary", report.sections.occupation_summary),
        ("opportunities", report.sections.opportunities),
        ("risks and uncertainty", report.sections.risks_and_uncertainty),
    )
    for label, section in sections:
        _validate_grounded_narrative(
            section.summary,
            claim_ids=section.claim_ids,
            claim_by_id=claim_by_id,
            citation_refs=citation_refs,
            valid_evidence=valid_evidence,
            label=label,
            errors=errors,
        )
    for scenario in report.sections.horizon_scenarios:
        for field_name, narrative in (
            ("summary", scenario.summary),
            ("uncertainty", scenario.uncertainty),
        ):
            _validate_grounded_narrative(
                narrative,
                claim_ids=scenario.claim_ids,
                claim_by_id=claim_by_id,
                citation_refs=citation_refs,
                valid_evidence=valid_evidence,
                label=f"{scenario.horizon} {field_name}",
                errors=errors,
            )
    for action in report.sections.practical_next_actions:
        for field_name, narrative in (
            ("action", action.action),
            ("rationale", action.rationale),
        ):
            _validate_grounded_narrative(
                narrative,
                claim_ids=action.claim_ids,
                claim_by_id=claim_by_id,
                citation_refs=citation_refs,
                valid_evidence=valid_evidence,
                label=f"action {action.action_id} {field_name}",
                errors=errors,
            )
    for citation in report.citations:
        _validate_grounded_narrative(
            citation.relevance,
            claim_ids=citation.claim_ids,
            claim_by_id=claim_by_id,
            citation_refs=citation_refs,
            valid_evidence=valid_evidence,
            label=f"citation {citation.evidence_ref} relevance",
            errors=errors,
            require_claim_evidence_union=False,
        )
        if citation.relevance.origin != "rag":
            errors.append(f"citation {citation.evidence_ref} relevance must have RAG origin")
        if citation.relevance.evidence_refs != (citation.evidence_ref,):
            errors.append(
                f"citation {citation.evidence_ref} relevance must link its cited evidence"
            )


def _visible_texts(report: MvpReportV1) -> tuple[str, ...]:
    values: list[str] = [
        report.title,
        report.sections.occupation_summary.summary.text,
        report.sections.opportunities.summary.text,
        report.sections.risks_and_uncertainty.summary.text,
    ]
    for claim in report.claims:
        values.extend((claim.text, claim.uncertainty))
    for scenario in report.sections.horizon_scenarios:
        values.extend((scenario.summary.text, scenario.uncertainty.text))
    for row in report.sections.task_impact_matrix:
        values.extend((row.task.text, row.rationale.text))
    for action in report.sections.practical_next_actions:
        values.extend((action.action.text, action.rationale.text))
    for citation in report.citations:
        values.append(citation.relevance.text)
    for resolution in report.suggestion_resolutions:
        values.append(resolution.reason)
    return tuple(values)


def matches_report_language(
    texts: tuple[str, ...], language: ReportLanguage
) -> bool:
    """Return whether every classifiable visible string matches the selection."""

    classified = tuple(
        detected
        for text in texts
        if (detected := classify_narrative_language(text)) is not None
    )
    return bool(classified) and all(detected == language for detected in classified)


def contains_personal_job_loss_probability(text: str) -> bool:
    """Detect forbidden exact personal job-loss probability sentences."""

    sentences = re.split(r"\n|(?<!\d)\.(?!\d)|[!?;。！？；]+", text)
    return any(
        _PERSONAL_JOB_LOSS.search(sentence)
        and _has_exact_probability(sentence)
        and not _is_aggregate_job_loss_statistic(sentence)
        for sentence in sentences
    )


def _has_exact_probability(text: str) -> bool:
    return any(
        pattern.search(text) is not None
        for pattern in (
            _EXACT_PROBABILITY,
            _ENGLISH_WORD_PERCENT,
            _ENGLISH_WORD_RATIO,
            _ENGLISH_WORD_FRACTION,
        )
    )


def _is_aggregate_job_loss_statistic(text: str) -> bool:
    if _POPULATION_NARRATOR.search(text) or _AGGREGATE_PERSONAL_SCOPE.search(text):
        return True
    if _SURVEY_MEMBER.search(text) and _RATE_STATISTIC_CONTEXT.search(text):
        return True
    aggregate_quantity = any(
        pattern.search(text) is not None
        for pattern in (
            _ENGLISH_WORD_PERCENT,
            _ENGLISH_WORD_RATIO,
            _ENGLISH_WORD_FRACTION,
            _CHINESE_NUMBER_EXPRESSION,
        )
    ) or "%" in text
    return bool(
        _POPULATION_MARKER.search(text)
        and (_RATE_STATISTIC_CONTEXT.search(text) or aggregate_quantity)
    )


def _has_factual_numeric(text: str) -> bool:
    without_horizon = _STRUCTURAL_HORIZON.sub("", text)
    return any(
        pattern.search(without_horizon) is not None
        for pattern in (
            _NUMERIC,
            _ENGLISH_SMALL_NUMBER,
            _ENGLISH_WORD_PERCENT,
            _ENGLISH_WORD_RATIO,
            _ENGLISH_WORD_FRACTION,
            _CHINESE_NUMBER_EXPRESSION,
        )
    )


def citation_relevance_text(language: ReportLanguage) -> str:
    """Return fixed prose without copying source metadata into the report."""

    if language == "zh":
        return "冻结的本地证据支持所关联的 RAG 声明。"
    return "Frozen local evidence supports the linked RAG claims."


__all__ = [
    "ReportValidationResult",
    "StageValidationCategoryCode",
    "validate_draft_binding",
    "StageValidationResult",
    "ValidationCategoryCode",
    "citation_relevance_text",
    "contains_personal_job_loss_probability",
    "fact_pack_record_ids",
    "matches_report_language",
    "validate_report",
    "validate_report_narrative_pack",
    "validate_resolution_decision_pack",
    "validate_revised_claim_pack",
]
