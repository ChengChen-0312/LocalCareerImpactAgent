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
    "UNSTRUCTURED_NUMERIC",
    "RAG_NUMERIC_GROUNDING",
]

_NUMERIC = re.compile(
    r"(?<![\w])(?:\d+(?:\.\d+)?|\.\d+)\s*(?:%|percent|percentage)?", re.I
)
_CLASSIFICATION_MARKER = re.compile(
    r"(?<![A-Za-z])(?:ANZSCO|OSCA)(?![A-Za-z])", re.I
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
    numeric_digit_pattern: bool | None = None
    numeric_english_pattern: bool | None = None
    numeric_chinese_expression_pattern: bool | None = None
    structural_horizon_adjacent_chinese: bool | None = None
    numeric_after_adjacent_horizon_removal: bool | None = None

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
    numeric_index = next((
        index for index, text in enumerate(texts) if _has_factual_numeric(text)
    ), None)
    if numeric_index is not None:
        errors.append("UNSTRUCTURED_NUMERIC")
    if errors and errors[0] == "UNSTRUCTURED_NUMERIC" and numeric_index is not None:
        return StageValidationResult(
            errors=tuple(errors),
            error_path=("resolutions", numeric_index, "reason"),
            **_numeric_pattern_diagnostics(texts[numeric_index]),
        )
    return _stage_result(errors)


def validate_draft_binding(
    draft: CareerImpactDraft,
    *,
    run_id: str,
    snapshot_id: str,
    language: ReportLanguage,
    fact_pack: FactPack | None = None,
) -> StageValidationResult:
    """Check draft identity and claim roles inside the existing bounded attempt."""

    field = next((
        name for name, expected in (
            ("run_id", run_id), ("snapshot_id", snapshot_id), ("language", language),
        ) if getattr(draft, name) != expected
    ), None)
    if field:
        return StageValidationResult(errors=("DRAFT_BINDING",), error_path=(field,))
    issues = _claim_attribution_issues(draft.claims, fact_pack=fact_pack)
    return StageValidationResult(
        errors=tuple(category for category, _ in issues),
        error_path=issues[0][1] if issues else (),
    )


def _claim_attribution_issues(
    claims: tuple[DraftClaim, ...],
    *,
    fact_pack: FactPack | None,
) -> list[tuple[StageValidationCategoryCode, tuple[int | str, ...]]]:
    """Reject role/evidence mismatches; never rewrite the model's decision."""

    issues: list[tuple[StageValidationCategoryCode, tuple[int | str, ...]]] = []
    content_ids = (
        {item.fact_id for item in fact_pack.facts}
        | {item.evidence_id for item in fact_pack.passages}
    ) if fact_pack is not None else None
    valid_ids = fact_pack_record_ids(fact_pack) if fact_pack is not None else None
    cited_classifications = (
        {
            item.evidence_id: {
                match.group().upper() for match in _CLASSIFICATION_MARKER.finditer(item.text)
            }
            for item in fact_pack.passages
        } | {
            item.fact_id: {item.answered_scope.classification}
            for item in fact_pack.facts
        }
    ) if fact_pack is not None else {}
    for index, claim in enumerate(claims):
        if claim.basis == "profile":
            issues.append(("CLAIM_BINDING", ("claims", index, "basis")))
        if claim.basis in {"rag", "profile"}:
            for field in ("horizon", "impact_band"):
                if getattr(claim, field) is not None:
                    issues.append(("CLAIM_BINDING", ("claims", index, field)))
        if claim.basis == "reasoned_scenario" and claim.horizon not in {
            "1-3-years", "3-5-years",
        }:
            issues.append(("HORIZON_STRUCTURE", ("claims", index, "horizon")))
        if claim.basis != "rag" and claim.evidence_refs:
            issues.append(("EVIDENCE_SCOPE", ("claims", index, "evidence_refs")))
        elif claim.basis == "rag" and (
            len(claim.evidence_refs) != 1
            or content_ids is not None and not content_ids.intersection(claim.evidence_refs)
        ):
            issues.append(("EVIDENCE_REFERENCE", ("claims", index, "evidence_refs")))
        if valid_ids is not None:
            for ref_index, evidence_ref in enumerate(claim.evidence_refs):
                if evidence_ref not in valid_ids or (
                    claim.basis == "rag" and content_ids is not None
                    and evidence_ref not in content_ids
                ):
                    issues.append((
                        "EVIDENCE_REFERENCE", ("claims", index, "evidence_refs", ref_index),
                    ))
        if claim.basis == "rag" and len(claim.evidence_refs) == 1:
            allowed = cited_classifications.get(claim.evidence_refs[0])
            if allowed is not None:
                for field in ("text", "uncertainty"):
                    stated = {
                        match.group().upper()
                        for match in _CLASSIFICATION_MARKER.finditer(getattr(claim, field))
                    }
                    if not stated.issubset(allowed):
                        issues.append(("EVIDENCE_REFERENCE", ("claims", index, field)))

    if not any(claim.basis == "rag" for claim in claims):
        issues.append(("CLAIM_BINDING", ("claims",)))
    if not any(claim.basis == "recommendation" for claim in claims):
        issues.append(("CLAIM_BINDING", ("claims",)))
    for horizon in ("1-3-years", "3-5-years"):
        if not any(
            claim.basis == "reasoned_scenario" and claim.horizon == horizon
            for claim in claims
        ):
            issues.append(("HORIZON_STRUCTURE", ("claims",)))
    return issues


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
    attribution = _claim_attribution_issues(revised.claims, fact_pack=fact_pack)
    first_path = ("claims",) if errors else attribution[0][1] if attribution else ()
    errors.extend(category for category, _ in attribution)
    texts = tuple(
        value
        for item in revised.claims
        for value in (item.text, item.uncertainty)
    )
    if contains_personal_job_loss_probability("\n".join(texts)):
        errors.append("PERSONAL_PROBABILITY")
    if not matches_report_language(texts, language):
        errors.append("VISIBLE_LANGUAGE")
    if any(
        item.basis == "rag"
        and (_has_factual_numeric(item.text) or _has_factual_numeric(item.uncertainty))
        and not item.evidence_refs
        for item in revised.claims
    ):
        errors.append("RAG_NUMERIC_GROUNDING")
    return StageValidationResult(
        errors=tuple(errors),
        error_path=first_path,
    )


def validate_report_narrative_pack(
    narratives: ReportNarrativePack,
    *,
    revised_claims: tuple[DraftClaim, ...],
    language: ReportLanguage,
) -> StageValidationResult:
    """Validate F3 claim references, exact horizons, and visible prose."""

    errors: list[StageValidationCategoryCode] = []
    claim_by_id = {item.claim_id: item for item in revised_claims}
    decisions = tuple(_narrative_decisions(narratives))
    attribution = _narrative_attribution_issues(narratives, claim_by_id)
    errors.extend(category for category, _ in attribution)
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
    if _has_factual_numeric(narratives.title):
        errors.append("UNSTRUCTURED_NUMERIC")
    for decision in decisions:
        primary_claim = claim_by_id.get(decision.primary_claim_id)
        if (
            primary_claim is not None
            and primary_claim.basis == "rag"
            and _has_factual_numeric(decision.text)
            and not any(
                claim_by_id[claim_id].evidence_refs
                for claim_id in decision.claim_ids
                if claim_id in claim_by_id
            )
        ):
            errors.append("RAG_NUMERIC_GROUNDING")
            break
    return StageValidationResult(
        errors=tuple(errors), error_path=attribution[0][1] if attribution else (),
    )


def _narrative_attribution_issues(
    narratives: ReportNarrativePack | MvpReportV1,
    claim_by_id: dict[str, DraftClaim],
) -> list[tuple[StageValidationCategoryCode, tuple[int | str, ...]]]:
    issues: list[tuple[StageValidationCategoryCode, tuple[int | str, ...]]] = []
    sections = narratives.sections
    semantic = isinstance(narratives, ReportNarrativePack)
    selections = [
        (
            section.summary if semantic else section,
            ("sections", name, "summary") if semantic else ("sections", name),
            allowed_bases, None,
        )
        for name, section, allowed_bases in (
            ("occupation_summary", sections.occupation_summary, {"rag"}),
            ("opportunities", sections.opportunities, {"reasoned_scenario", "recommendation"}),
            ("risks_and_uncertainty", sections.risks_and_uncertainty, {"reasoned_scenario"}),
        )
    ]
    for field in ("horizon_scenarios", "task_impact_matrix", "practical_next_actions"):
        for index, item in enumerate(getattr(sections, field)):
            scenario = field != "practical_next_actions"
            selections.append((
                item, ("sections", field, index),
                {"reasoned_scenario"} if scenario else {"recommendation"},
                item.horizon if scenario else None,
            ))
    for selection, path, allowed_bases, horizon in selections:
        primary_id = selection.primary_claim_id if semantic else selection.claim_ids[0]
        primary_path = (*path, "primary_claim_id") if semantic else (*path, "claim_ids", 0)
        supporting_ids = selection.supporting_claim_ids if semantic else selection.claim_ids[1:]
        if path[1] == "occupation_summary" and supporting_ids:
            support_path = (*path, "supporting_claim_ids") if semantic else (*path, "claim_ids", 1)
            issues.append(("NARRATIVE_REFERENCE", support_path))
        primary = claim_by_id.get(primary_id)
        if primary is None or allowed_bases is not None and primary.basis not in allowed_bases:
            issues.append(("NARRATIVE_REFERENCE", primary_path))
        elif horizon is not None and primary.horizon != horizon:
            issues.append(("HORIZON_STRUCTURE", primary_path))
        for index, claim_id in enumerate(supporting_ids):
            if claim_id not in claim_by_id:
                ref_path = (*path, "supporting_claim_ids", index) if semantic else (*path, "claim_ids", index + 1)
                issues.append(("NARRATIVE_REFERENCE", ref_path))
    if (
        sections.risks_and_uncertainty.summary.text.strip()
        == sections.occupation_summary.summary.text.strip()
    ):
        issues.append(("NARRATIVE_REFERENCE", ("sections", "risks_and_uncertainty", "summary", "text")))
    return issues


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
    if error.startswith(("claim attribution ", "narrative attribution ")):
        if " HORIZON_STRUCTURE " in error:
            return "HORIZON_STRUCTURE"
        if " EVIDENCE_" in error:
            return "EVIDENCE_REFERENCE"
        return "ORIGIN_LINKAGE" if error.startswith("claim ") else "NARRATIVE_LINKAGE"
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
    for category, path in _claim_attribution_issues(report.claims, fact_pack=fact_pack):
        errors.append(f"claim attribution {category} at {'.'.join(map(str, path))}")
    for category, path in _narrative_attribution_issues(report, claim_by_id):
        errors.append(f"narrative attribution {category} at {'.'.join(map(str, path))}")
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


_DIAGNOSTIC_HORIZON_CANDIDATE = re.compile(
    r"(?i)(?<!\d)(?:(?:1\s*[-–—]\s*3|3\s*[-–—]\s*5)\s*(?:years?|年)|"
    r"(?:1\s*(?:至|到)\s*3|3\s*(?:至|到)\s*5)\s*年)"
)
_DIAGNOSTIC_HAN_CHARACTER = re.compile(r"[\u3400-\u9fff]")


def _numeric_pattern_diagnostics(text: str) -> dict[str, bool]:
    """Describe the existing numeric rejection without retaining matched content.

    The independent horizon flag records adjacency only. It neither changes the
    validator's horizon subtraction nor proves why any real output was rejected.
    """

    without_horizon = _STRUCTURAL_HORIZON.sub("", text)
    def adjacent_to_han(match: re.Match[str]) -> bool:
        return (
            (match.start() > 0 and _DIAGNOSTIC_HAN_CHARACTER.fullmatch(text[match.start() - 1]) is not None)
            or (match.end() < len(text) and _DIAGNOSTIC_HAN_CHARACTER.fullmatch(text[match.end()]) is not None)
        )

    adjacent_han = any(
        adjacent_to_han(match) for match in _DIAGNOSTIC_HORIZON_CANDIDATE.finditer(text)
    )
    diagnostic_without_adjacent_horizon = _DIAGNOSTIC_HORIZON_CANDIDATE.sub(
        lambda match: "" if adjacent_to_han(match) else match.group(0), text,
    )
    return {
        "numeric_digit_pattern": _NUMERIC.search(without_horizon) is not None,
        "numeric_english_pattern": any(
            pattern.search(without_horizon) is not None
            for pattern in (
                _ENGLISH_SMALL_NUMBER, _ENGLISH_WORD_PERCENT,
                _ENGLISH_WORD_RATIO, _ENGLISH_WORD_FRACTION,
            )
        ),
        "numeric_chinese_expression_pattern": _CHINESE_NUMBER_EXPRESSION.search(without_horizon) is not None,
        "structural_horizon_adjacent_chinese": adjacent_han,
        "numeric_after_adjacent_horizon_removal": _has_factual_numeric(diagnostic_without_adjacent_horizon),
    }


def citation_relevance_text(language: ReportLanguage) -> str:
    """Return fixed prose without copying source metadata into the report."""

    if language == "zh":
        return "所关联声明引用的冻结本地证据。"
    return "Frozen local evidence cited by the linked claims."


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
