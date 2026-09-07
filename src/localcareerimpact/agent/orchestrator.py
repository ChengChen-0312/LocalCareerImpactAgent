"""Serial 30B -> shared 4B x3 -> 30B orchestration for one frozen run."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, NoReturn, TypeVar
from uuid import uuid4

from pydantic import Field, ValidationError

from localcareerimpact.app.database import (
    ANALYSIS_STAGE_MESSAGES,
    RUN_ERROR_MESSAGES,
    Database,
    append_run_message,
    completed_run_stages,
    update_run_progress_message,
    utc_now_iso,
)
from localcareerimpact.app.model_runtime import ModelRuntime
from localcareerimpact.contracts.base import StrictContract
from localcareerimpact.contracts.factpack import FactPack
from localcareerimpact.contracts.runbundle import (
    ConfirmedCaseProfile,
    ConfirmedResponsibility,
    ConfirmedSkill,
    UserGoal,
)
from localcareerimpact.knowledge import (
    HybridRetriever,
    KnowledgeEmbeddingMismatchError,
    KnowledgeService,
    KnowledgeSnapshotUnavailableError,
    SourceProvenance,
    adapt_evidence_context,
)
from localcareerimpact.knowledge.embeddings import EmbeddingError
from localcareerimpact.workers.client import WorkerCallError, WorkerStartupError
from localcareerimpact.workers.protocol import (
    GenerateRequest,
    GenerationFailureCategory,
    GenerationMetrics,
    ModelKey,
    WorkerMessage,
    sanitize_schema_error_path,
)

from .assembler import ReportAssemblyError, assemble_report
from .contracts import (
    CareerImpactDraft,
    DraftClaim,
    MvpReportV1,
    ReportNarrativePack,
    ReportLanguage,
    ResolutionDecisionPack,
    RevisedClaimPack,
    RunFailureCode,
    Suggestion,
    SuggestionCollection,
)
from .language import select_response_language
from .prompts import (
    boundary_review_messages,
    draft_messages,
    evidence_review_messages,
    report_narrative_messages,
    resolution_messages,
    revised_claim_messages,
    safety_review_messages,
    stage_retry_message,
)
from .validator import (
    ReportValidationResult,
    StageValidationResult,
    StageValidationCategoryCode,
    validate_report,
    validate_draft_binding,
    validate_report_narrative_pack,
    validate_resolution_decision_pack,
    validate_revised_claim_pack,
)


_ContractT = TypeVar("_ContractT", bound=StrictContract)
_TaskT = TypeVar("_TaskT")
LOGGER = logging.getLogger(__name__)


class AnalysisRunNotFoundError(LookupError):
    """Raised when a run does not belong to the authenticated owner."""


class AnalysisRunStateError(RuntimeError):
    """Raised when a run is not in the one executable queued state."""


@dataclass(frozen=True, slots=True)
class OrchestrationResult:
    run_id: str
    status: Literal[
        "COMPLETE",
        "RETRIEVAL_FAILED",
        "MAIN_AGENT_FAILED",
        "REVIEW_INCOMPLETE",
        "VALIDATION_FAILED",
        "ANALYSIS_INTERRUPTED",
        "INTERNAL_FAILED",
    ]
    report_id: str | None
    message: str


@dataclass(frozen=True, slots=True)
class _ClaimedRun:
    run_id: str
    profile_id: str
    profile: dict[str, object]
    language: ReportLanguage


class StageAttemptDiagnostics(StrictContract):
    """Internal, content-free record; independent of report/evidence decoding."""

    stage: Literal[
        "draft", "evidence_review", "boundary_review", "safety_review",
        "resolution_decision", "revised_claims", "report_narratives",
    ]
    attempt: int = Field(ge=1, le=2)
    status: Literal["started", "complete", "failed"]
    boundary: Literal["request", "generation", "contract", "validation"]
    category: GenerationFailureCategory | StageValidationCategoryCode | Literal[
        "invalid_request", "startup_failed", "transport_error", "generation_failed",
        "REVIEW_BINDING",
    ] | None = None
    metrics: GenerationMetrics | None = None
    schema_error_path: tuple[int | str, ...] = ()
    parse_error_offset: int | None = Field(default=None, ge=0)
    context_codepoints: int = Field(ge=0)
    validator_categories: dict[
        StageValidationCategoryCode | Literal["REVIEW_BINDING"], int
    ] = Field(default_factory=dict)
    numeric_digit_pattern: bool | None = None
    numeric_english_pattern: bool | None = None
    numeric_chinese_expression_pattern: bool | None = None
    structural_horizon_adjacent_chinese: bool | None = None
    numeric_after_adjacent_horizon_removal: bool | None = None


class _MainDecisionClient:
    """Sole 30B decision client for the draft and three bounded final stages."""

    def __init__(
        self,
        runtime: ModelRuntime,
        *,
        record_attempt: Callable[[StageAttemptDiagnostics], Awaitable[None]] | None = None,
    ) -> None:
        self._runtime = runtime
        self._record_attempt = record_attempt

    async def draft(
        self,
        messages: tuple[WorkerMessage, ...],
        *,
        run_id: str,
        snapshot_id: str,
        language: ReportLanguage,
        fact_pack: FactPack | None = None,
    ) -> tuple[CareerImpactDraft, int]:
        return await self._run_stage(
            role="draft",
            messages=messages,
            contract_type=CareerImpactDraft,
            response_schema=CareerImpactDraft.generation_schema(
                evidence_ids=(
                    tuple(item.fact_id for item in fact_pack.facts)
                    + tuple(item.evidence_id for item in fact_pack.passages)
                ) if fact_pack is not None else None,
            ),
            max_tokens=4_096,
            initial_temperature="0.2",
            top_p="1",
            seed=6001,
            retry_seed=6001,
            fixed_fields={"run_id": run_id, "snapshot_id": snapshot_id, "language": language},
            validate=lambda value: validate_draft_binding(
                value, run_id=run_id, snapshot_id=snapshot_id, language=language, fact_pack=fact_pack,
            ),
        )

    async def resolution_decision(
        self,
        messages: tuple[WorkerMessage, ...],
        *,
        suggestions: tuple[Suggestion, ...],
        language: ReportLanguage,
    ) -> tuple[ResolutionDecisionPack, int]:
        response_schema = ResolutionDecisionPack.generation_schema()
        response_schema["$defs"]["SuggestionResolution"]["properties"]["reason"].update({
            "maxLength": 96,
            "pattern": r"^[^\d.!?。！？]+[.!?。！？]$",
        })
        resolutions = response_schema["properties"]["resolutions"]
        resolutions.update({
            "minItems": len(suggestions),
            "maxItems": len(suggestions),
            "items": False,
        })
        if suggestions:
            # IDs and ordering are bookkeeping; every accept/reject decision
            # and its reason are still generated by the sole main agent.
            resolutions["prefixItems"] = [
                {
                    "$ref": "#/$defs/SuggestionResolution",
                    "properties": {"suggestion_id": {"const": item.suggestion_id}},
                }
                for item in suggestions
            ]
        return await self._run_stage(
            role="resolution_decision",
            messages=messages,
            contract_type=ResolutionDecisionPack,
            response_schema=response_schema,
            max_tokens=1_536,
            initial_temperature="0.2",
            seed=6101,
            validate=lambda value: validate_resolution_decision_pack(
                value,
                suggestions=suggestions,
                language=language,
            ),
        )

    async def revised_claims(
        self,
        messages: tuple[WorkerMessage, ...],
        *,
        draft_claims: tuple[DraftClaim, ...],
        fact_pack: FactPack,
        language: ReportLanguage,
    ) -> tuple[RevisedClaimPack, int]:
        return await self._run_stage(
            role="revised_claims",
            messages=messages,
            contract_type=RevisedClaimPack,
            response_schema=RevisedClaimPack.generation_schema(
                evidence_ids=(
                    tuple(item.fact_id for item in fact_pack.facts)
                    + tuple(item.evidence_id for item in fact_pack.passages)
                ),
            ),
            max_tokens=3_072,
            initial_temperature="0.2",
            seed=6102,
            validate=lambda value: validate_revised_claim_pack(
                value,
                draft_claims=draft_claims,
                fact_pack=fact_pack,
                language=language,
            ),
        )

    async def report_narratives(
        self,
        messages: tuple[WorkerMessage, ...],
        *,
        revised_claims: tuple[DraftClaim, ...],
        language: ReportLanguage,
    ) -> tuple[ReportNarrativePack, int]:
        return await self._run_stage(
            role="report_narratives",
            messages=messages,
            contract_type=ReportNarrativePack,
            response_schema=ReportNarrativePack.generation_schema(language, revised_claims=revised_claims),
            max_tokens=4_096,
            initial_temperature="0.3",
            seed=6103,
            validate=lambda value: validate_report_narrative_pack(
                value,
                revised_claims=revised_claims,
                language=language,
            ),
        )

    async def _run_stage(
        self,
        *,
        role: Literal["draft", "resolution_decision", "revised_claims", "report_narratives"],
        messages: tuple[WorkerMessage, ...],
        contract_type: type[_ContractT],
        max_tokens: int,
        initial_temperature: str,
        seed: int,
        top_p: str = "0.8",
        retry_seed: int | None = None,
        fixed_fields: dict[str, str] | None = None,
        response_schema: dict[str, object] | None = None,
        validate: Callable[[_ContractT], StageValidationResult],
    ) -> tuple[_ContractT, int]:
        response_schema = response_schema if response_schema is not None else contract_type.model_json_schema()
        # These values are application identity, never a semantic model decision.
        # Constrain the request while retaining the same stored contract and gate.
        for name, value in (fixed_fields or {}).items():
            response_schema["properties"][name]["const"] = value
        last_error: Exception | None = None
        for attempt in (1, 2):
            attempt_messages = messages
            if attempt == 2:
                retry_diagnostics = _safe_stage_retry_diagnostics(
                    last_error,
                    response_schema=response_schema,
                )
                if retry_diagnostics is not None:
                    attempt_messages = (
                        *attempt_messages,
                        stage_retry_message(**retry_diagnostics),
                    )
            diagnostic = StageAttemptDiagnostics(
                stage=role,
                attempt=attempt,
                status="started",
                boundary="request",
                context_codepoints=len(json.dumps(
                    response_schema, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                )) + sum(len(item.role) + len(item.content) for item in attempt_messages),
            )
            if self._record_attempt is not None:
                await self._record_attempt(diagnostic)
            boundary = "request"
            metrics = None
            validation = None
            try:
                request = GenerateRequest(
                    request_id=str(uuid4()),
                    role=role,
                    messages=attempt_messages,
                    response_schema=response_schema,
                    max_tokens=max_tokens,
                    temperature=initial_temperature if attempt == 1 else "0.2",
                    top_p=top_p,
                    repetition_penalty="1.0",
                    seed=seed if attempt == 1 else (
                        retry_seed if retry_seed is not None else seed + 100
                    ),
                    allow_single_json_fence=True,
                )
                boundary = "generation"
                envelope = await self._runtime.generate("qwen30b", request)
                metrics = envelope.metrics
                boundary = "contract"
                value = _parse_contract(contract_type, envelope.content)
                boundary = "validation"
                validation = validate(value)
                if not validation.valid:
                    raise _FocusedStageValidationError(
                        validation.errors[0], validation.error_path,
                    )
            except (
                WorkerCallError,
                ValidationError,
                _FocusedStageValidationError,
            ) as exc:
                last_error = exc
                safe_details = _safe_stage_retry_diagnostics(
                    exc, response_schema=response_schema,
                ) or {}
                # Persist before retry/restart. Never serialize the exception, input,
                # generated content, or Pydantic validation messages.
                if self._record_attempt is not None:
                    await self._record_attempt(diagnostic.model_copy(update={
                        "status": "failed",
                        "boundary": boundary,
                        "category": "invalid_request" if boundary == "request"
                        else _safe_stage_error_category(exc),
                        "metrics": exc.metrics if isinstance(exc, WorkerCallError) else metrics,
                        "schema_error_path": sanitize_schema_error_path(
                            safe_details.get("schema_error_path", ()), response_schema,
                        ),
                        "parse_error_offset": safe_details.get("parse_error_offset"),
                        "validator_categories": validation.category_count_map() if validation else {},
                        "numeric_digit_pattern": validation.numeric_digit_pattern if validation else None,
                        "numeric_english_pattern": validation.numeric_english_pattern if validation else None,
                        "numeric_chinese_expression_pattern": validation.numeric_chinese_expression_pattern if validation else None,
                        "structural_horizon_adjacent_chinese": validation.structural_horizon_adjacent_chinese if validation else None,
                        "numeric_after_adjacent_horizon_removal": validation.numeric_after_adjacent_horizon_removal if validation else None,
                    }))
                if boundary == "request":
                    raise _StageFailure(
                        "MAIN_AGENT_FAILED", f"The 30B {role} context or request is invalid."
                    ) from exc
                LOGGER.warning(
                    "30B %s attempt %s failed (%s).",
                    role,
                    attempt,
                    _safe_stage_error_category(exc),
                )
                if attempt == 1 and _requires_worker_restart(exc):
                    try:
                        await _restart_failed_worker(self._runtime, "qwen30b", exc)
                    except WorkerStartupError as restart_error:
                        last_error = restart_error
                        break
            else:
                if self._record_attempt is not None:
                    await self._record_attempt(diagnostic.model_copy(update={
                        "status": "complete", "boundary": boundary, "metrics": metrics,
                    }))
                return value, attempt
        raise _StageFailure(
            "MAIN_AGENT_FAILED",
            f"The 30B {role} stage failed after its one allowed retry.",
        ) from last_error


class _AdvisoryReviewer:
    """4B client with content-free telemetry and no report persistence capability."""

    def __init__(
        self,
        runtime: ModelRuntime,
        *,
        record_attempt: Callable[[StageAttemptDiagnostics], Awaitable[None]] | None = None,
    ) -> None:
        self._runtime = runtime
        self._record_attempt = record_attempt

    async def review(
        self,
        *,
        role: Literal["evidence_review", "boundary_review", "safety_review"],
        expected_category: Literal["evidence", "reasoning_boundary", "safety_language"],
        expected_id_prefix: Literal["evidence-", "boundary-", "safety-"],
        messages: tuple[WorkerMessage, ...],
        draft_claim_ids: frozenset[str],
        factpack_record_ids: frozenset[str],
        forbidden_suggestion_ids: frozenset[str],
    ) -> tuple[tuple[Suggestion, ...], int]:
        response_schema = SuggestionCollection.model_json_schema()
        properties = response_schema["$defs"]["Suggestion"]["properties"]
        properties["category"]["const"] = expected_category
        # Review output has a fixed 2048-token budget. Bound the generated
        # explanation and reference lists, while the reviewer chooses the
        # relevant issues and the main agent retains the full frozen evidence.
        properties["proposed_correction"].update({
            "maxLength": 180,
            "pattern": r"^(?:[^.!?。！？]|[0-9]+\.[0-9]+)*[.!?。！？]$",
        })
        properties["affected_claim_ids"]["maxItems"] = 3
        properties["evidence_refs"]["maxItems"] = 2
        available_ids = [
            f"{expected_id_prefix}{index}" for index in (1, 2, 3)
            if f"{expected_id_prefix}{index}" not in forbidden_suggestion_ids
        ]
        if available_ids:
            properties["suggestion_id"]["enum"] = available_ids
            # Suggestion identifiers are bookkeeping. Fix their order while the
            # reviewer still chooses whether to give 0..3 suggestions and what
            # each suggestion says or references.
            response_schema["properties"]["suggestions"].update({
                "prefixItems": [
                    {
                        "$ref": "#/$defs/Suggestion",
                        "properties": {"suggestion_id": {"const": identifier}},
                    }
                    for identifier in available_ids
                ],
                "items": False,
                "maxItems": len(available_ids),
            })
        else:
            response_schema["properties"]["suggestions"]["maxItems"] = 0
        for field, allowed in (
            ("affected_claim_ids", draft_claim_ids),
            ("evidence_refs", factpack_record_ids),
        ):
            if allowed:
                properties[field]["items"]["enum"] = sorted(allowed)
            else:
                properties[field]["maxItems"] = 0
        last_error: Exception | None = None
        for attempt in (1, 2):
            attempt_messages = messages
            if attempt == 2:
                retry_diagnostics = _safe_stage_retry_diagnostics(
                    last_error, response_schema=response_schema,
                )
                if retry_diagnostics is not None:
                    attempt_messages = (*messages, stage_retry_message(**retry_diagnostics))
            diagnostic = StageAttemptDiagnostics(
                stage=role,
                attempt=attempt,
                status="started",
                boundary="request",
                context_codepoints=len(json.dumps(
                    response_schema, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                )) + sum(len(item.role) + len(item.content) for item in attempt_messages),
            )
            if self._record_attempt is not None:
                await self._record_attempt(diagnostic)
            boundary = "request"
            metrics = None
            try:
                request = GenerateRequest(
                    request_id=str(uuid4()),
                    role=role,
                    messages=attempt_messages,
                    response_schema=response_schema,
                    max_tokens=2_048,
                    temperature="0",
                    top_p="1",
                    repetition_penalty="1",
                    seed={
                        "evidence_review": 6201,
                        "boundary_review": 6202,
                        "safety_review": 6203,
                    }[role],
                    allow_single_json_fence=True,
                )
                boundary = "generation"
                envelope = await self._runtime.generate("qwen4b", request)
                metrics = envelope.metrics
                boundary = "contract"
                collection = _parse_contract(SuggestionCollection, envelope.content)
                boundary = "validation"
                _validate_suggestion_set(
                    collection.suggestions,
                    expected_category=expected_category,
                    expected_id_prefix=expected_id_prefix,
                    draft_claim_ids=draft_claim_ids,
                    factpack_record_ids=factpack_record_ids,
                    forbidden_suggestion_ids=forbidden_suggestion_ids,
                )
            except (WorkerCallError, ValidationError, ValueError) as exc:
                last_error = exc
                safe_details = _safe_stage_retry_diagnostics(
                    exc, response_schema=response_schema,
                ) or {}
                category = (
                    "invalid_request" if boundary == "request"
                    else "REVIEW_BINDING" if boundary == "validation"
                    else _safe_stage_error_category(exc)
                )
                if self._record_attempt is not None:
                    await self._record_attempt(diagnostic.model_copy(update={
                        "status": "failed",
                        "boundary": boundary,
                        "category": category,
                        "metrics": exc.metrics if isinstance(exc, WorkerCallError) else metrics,
                        "schema_error_path": sanitize_schema_error_path(
                            safe_details.get("schema_error_path", ()), response_schema,
                        ),
                        "parse_error_offset": safe_details.get("parse_error_offset"),
                        "validator_categories": {"REVIEW_BINDING": 1}
                        if category == "REVIEW_BINDING" else {},
                    }))
                if boundary == "request":
                    raise _StageFailure(
                        "REVIEW_INCOMPLETE", f"The {role} context or request is invalid."
                    ) from exc
                LOGGER.warning("%s attempt %s failed (%s).", role, attempt, type(exc).__name__)
                if attempt == 1:
                    try:
                        await _restart_failed_worker(self._runtime, "qwen4b", exc)
                    except WorkerStartupError as restart_error:
                        last_error = restart_error
                        break
            else:
                if self._record_attempt is not None:
                    await self._record_attempt(diagnostic.model_copy(update={
                        "status": "complete", "boundary": boundary, "metrics": metrics,
                    }))
                return collection.suggestions, attempt
        raise _StageFailure(
            "REVIEW_INCOMPLETE",
            f"The {role} role failed after its one allowed retry.",
        ) from last_error


@dataclass(frozen=True, slots=True)
class _ValidationFailureDiagnostics:
    validation: ReportValidationResult

    def event_payload(self) -> dict[str, object]:
        return {
            "evaluated": True,
            "category_counts": self.validation.category_count_map(),
        }


class _FocusedStageValidationError(RuntimeError):
    """Carry one closed deterministic category and structural path into a retry."""

    def __init__(
        self, category: StageValidationCategoryCode, error_path: tuple[int | str, ...] = (),
    ) -> None:
        self.category = category
        self.error_path = error_path
        super().__init__(category)


class _ReviewBindingError(ValueError):
    """Keep a reviewer binding failure usable by the content-free retry path."""

    category = "REVIEW_BINDING"

    def __init__(self, error_path: tuple[int | str, ...]) -> None:
        self.error_path = error_path
        super().__init__(self.category)


class _StageFailure(RuntimeError):
    def __init__(
        self,
        code: RunFailureCode,
        message: str,
        *,
        validation_diagnostics: _ValidationFailureDiagnostics | None = None,
    ) -> None:
        self.code = code
        self.validation_diagnostics = validation_diagnostics
        super().__init__(message)


async def _await_completion_observing_cancellation(
    task: asyncio.Task[_TaskT],
) -> tuple[_TaskT, bool]:
    """Drain an in-scope operation and report cancellation received while waiting."""

    cancellation_received = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancellation_received = True
        except Exception:
            break
    if task.cancelled():
        raise asyncio.CancelledError
    try:
        result = task.result()
    except Exception:
        if cancellation_received:
            raise asyncio.CancelledError from None
        raise
    return result, cancellation_received


async def _restart_failed_worker(
    runtime: ModelRuntime,
    model_key: ModelKey,
    error: Exception,
) -> None:
    """Replace a runtime-failed worker or recover a failed transport process."""

    if isinstance(error, WorkerCallError):
        if error.category == "runtime_error":
            await runtime.restart(model_key)
        else:
            await runtime.restart_if_failed(model_key)


def _requires_worker_restart(error: Exception) -> bool:
    return isinstance(error, WorkerCallError) and not (
        error.code == "generation_failed"
        and error.category not in {None, "runtime_error"}
    )


def _safe_stage_error_category(error: Exception) -> str:
    if isinstance(error, _FocusedStageValidationError):
        return error.category
    if isinstance(error, WorkerCallError):
        return error.category or error.code or "transport_error"
    if isinstance(error, ValidationError):
        return "schema_mismatch"
    return "runtime_error"


def _safe_stage_retry_diagnostics(
    error: Exception | None,
    *,
    response_schema: dict[str, object],
) -> dict[str, object] | None:
    if isinstance(error, (_FocusedStageValidationError, _ReviewBindingError)):
        return {
            "category": error.category,
            "schema_error_path": sanitize_schema_error_path(error.error_path, response_schema),
            "parse_error_offset": None,
        }
    if isinstance(error, ValidationError):
        errors = error.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )
        path = errors[0]["loc"] if errors else ()
        return {
            "category": "schema_mismatch",
            "schema_error_path": sanitize_schema_error_path(path, response_schema),
            "parse_error_offset": None,
        }
    if (
        isinstance(error, WorkerCallError)
        and error.code == "generation_failed"
        and error.category not in {None, "runtime_error"}
    ):
        return {
            "category": error.category,
            "schema_error_path": error.schema_error_path,
            "parse_error_offset": error.parse_error_offset,
        }
    return None


class StarOrchestrator:
    def __init__(
        self,
        database: Database,
        runtime: ModelRuntime,
        knowledge_service: KnowledgeService,
    ) -> None:
        self._database = database
        self._runtime = runtime
        self._knowledge_service = knowledge_service

    async def execute(self, owner_username: str, run_id: str) -> OrchestrationResult:
        async def record_attempt(diagnostic: StageAttemptDiagnostics) -> None:
            operation = asyncio.create_task(asyncio.to_thread(
                self._record_stage_attempt, run_id, diagnostic,
            ))
            _, cancelled = await _await_completion_observing_cancellation(operation)
            if cancelled:
                raise asyncio.CancelledError

        main = _MainDecisionClient(self._runtime, record_attempt=record_attempt)
        reviewer = _AdvisoryReviewer(self._runtime, record_attempt=record_attempt)
        claim_task = asyncio.create_task(
            asyncio.to_thread(self._claim_run, owner_username, run_id)
        )
        try:
            claimed, cancelled_during_claim = await _await_completion_observing_cancellation(
                claim_task
            )
        except Exception:
            raise
        if cancelled_during_claim:
            await self._terminalize_cancelled_run(run_id)
        try:
            await asyncio.to_thread(self._record_event, run_id, "retrieval", {"status": "running"})
            evidence, retrieval_coverage = await self._retrieve_and_bind(claimed, owner_username)
            context = evidence.context
            fact_pack = context.fact_pack
            profile_payload = claimed.profile
            evidence_payload = context.model_dump(mode="json")
            evidence_payload["retrieval_coverage"] = retrieval_coverage

            await asyncio.to_thread(self._record_event, run_id, "draft", {"status": "running"})
            draft, draft_attempt = await main.draft(
                draft_messages(
                    profile=profile_payload,
                    evidence=evidence_payload,
                    run_id=run_id,
                    snapshot_id=fact_pack.snapshot_id,
                    language=claimed.language,
                ),
                run_id=run_id,
                snapshot_id=fact_pack.snapshot_id,
                language=claimed.language,
                fact_pack=fact_pack,
            )
            await asyncio.to_thread(
                self._record_event,
                run_id,
                "draft",
                {"status": "complete", "attempt": draft_attempt},
            )

            draft_payload = draft.model_dump(mode="json")
            draft_claim_ids = frozenset(item.claim_id for item in draft.claims)
            factpack_record_ids = frozenset(_factpack_record_ids(fact_pack))
            review_specs = (
                (
                    "evidence_review",
                    "evidence",
                    "evidence-",
                    evidence_review_messages(draft=draft_payload, evidence=evidence_payload),
                ),
                (
                    "boundary_review",
                    "reasoning_boundary",
                    "boundary-",
                    boundary_review_messages(draft=draft_payload, evidence=evidence_payload),
                ),
                (
                    "safety_review",
                    "safety_language",
                    "safety-",
                    safety_review_messages(
                        draft=draft_payload,
                        evidence=evidence_payload,
                        language=claimed.language,
                    ),
                ),
            )
            all_suggestions: list[Suggestion] = []
            seen_suggestion_ids: set[str] = set()
            for role, category, id_prefix, messages in review_specs:
                await asyncio.to_thread(self._record_event, run_id, role, {"status": "running"})
                suggestions, attempt = await reviewer.review(
                    role=role,
                    expected_category=category,
                    expected_id_prefix=id_prefix,
                    messages=messages,
                    draft_claim_ids=draft_claim_ids,
                    factpack_record_ids=factpack_record_ids,
                    forbidden_suggestion_ids=frozenset(seen_suggestion_ids),
                )
                seen_suggestion_ids.update(item.suggestion_id for item in suggestions)
                all_suggestions.extend(suggestions)
                await asyncio.to_thread(
                    self._record_review,
                    run_id,
                    role,
                    tuple(all_suggestions),
                    suggestions,
                    attempt,
                )

            suggestion_tuple = tuple(all_suggestions)
            await asyncio.to_thread(self._record_event, run_id, "resolution_decision", {"status": "running"})
            decisions, resolution_attempt = await main.resolution_decision(
                resolution_messages(
                    draft=draft_payload,
                    suggestions=suggestion_tuple,
                    evidence=evidence_payload,
                    language=claimed.language,
                ),
                suggestions=suggestion_tuple,
                language=claimed.language,
            )
            await asyncio.to_thread(
                self._record_event,
                run_id,
                "resolution_decision",
                {
                    "status": "complete",
                    "attempt": resolution_attempt,
                    "count": len(decisions.resolutions),
                },
            )

            await asyncio.to_thread(self._record_event, run_id, "revised_claims", {"status": "running"})
            revised_claims, claims_attempt = await main.revised_claims(
                revised_claim_messages(
                    draft=draft_payload,
                    evidence=evidence_payload,
                    suggestions=suggestion_tuple,
                    resolutions=decisions.model_dump(mode="json"),
                    language=claimed.language,
                ),
                draft_claims=draft.claims,
                fact_pack=fact_pack,
                language=claimed.language,
            )
            await asyncio.to_thread(
                self._record_event,
                run_id,
                "revised_claims",
                {
                    "status": "complete",
                    "attempt": claims_attempt,
                    "count": len(revised_claims.claims),
                },
            )

            await asyncio.to_thread(self._record_event, run_id, "report_narratives", {"status": "running"})
            narratives, narratives_attempt = await main.report_narratives(
                report_narrative_messages(
                    revised_claims=revised_claims.model_dump(mode="json"),
                    language=claimed.language,
                ),
                revised_claims=revised_claims.claims,
                language=claimed.language,
            )
            await asyncio.to_thread(
                self._record_event,
                run_id,
                "report_narratives",
                {
                    "status": "complete",
                    "attempt": narratives_attempt,
                    "count": _report_narrative_count(narratives),
                },
            )

            await asyncio.to_thread(self._record_event, run_id, "validation", {"status": "running"})
            try:
                report = assemble_report(
                    run_id=run_id,
                    snapshot_id=fact_pack.snapshot_id,
                    language=claimed.language,
                    decisions=decisions,
                    draft_claims=draft.claims,
                    claims=revised_claims,
                    narratives=narratives,
                    suggestions=suggestion_tuple,
                    fact_pack=fact_pack,
                )
            except ReportAssemblyError as exc:
                raise _StageFailure(
                    "VALIDATION_FAILED",
                    "The staged final decision could not be assembled safely.",
                    validation_diagnostics=_ValidationFailureDiagnostics(
                        validation=exc.validation,
                    ) if exc.validation is not None else None,
                ) from exc
            return await self._validate_and_save(
                claimed=claimed,
                report=report,
                fact_pack=fact_pack,
                suggestions=suggestion_tuple,
            )
        except asyncio.CancelledError:
            await self._terminalize_cancelled_run(run_id)
        except _StageFailure as exc:
            cancelled = await self._terminalize_run(
                run_id,
                exc.code,
                str(exc),
                validation_diagnostics=exc.validation_diagnostics,
            )
            if cancelled:
                raise asyncio.CancelledError
            return OrchestrationResult(
                run_id=run_id,
                status=exc.code,
                report_id=None,
                message=str(exc),
            )
        except Exception as exc:
            cancelled = await self._terminalize_run(
                run_id,
                "INTERNAL_FAILED",
                "The analysis stopped because of an internal persistence or runtime failure.",
            )
            if cancelled:
                raise asyncio.CancelledError from exc
            raise

    async def _terminalize_run(
        self,
        run_id: str,
        code: RunFailureCode,
        message: str,
        *,
        validation_diagnostics: _ValidationFailureDiagnostics | None = None,
    ) -> bool:
        operation = asyncio.create_task(
            asyncio.to_thread(
                self._fail_run,
                run_id,
                code,
                message,
                validation_diagnostics=validation_diagnostics,
            )
        )
        _result, cancellation_received = (
            await _await_completion_observing_cancellation(operation)
        )
        return cancellation_received

    async def _terminalize_cancelled_run(self, run_id: str) -> NoReturn:
        try:
            await self._terminalize_run(
                run_id,
                "ANALYSIS_INTERRUPTED",
                "The analysis was interrupted before completion.",
            )
        except Exception as exc:
            raise asyncio.CancelledError from exc
        raise asyncio.CancelledError

    async def _retrieve_and_bind(self, claimed: _ClaimedRun, owner_username: str):
        try:
            retriever = HybridRetriever(
                self._knowledge_service.repository,
                self._knowledge_service.embedding_client,
            )
            retrieved, coverage = await retriever.retrieve_analysis(
                _retrieval_query(claimed.profile),
                occupation_query=" ".join(
                    item for item in claimed.profile.get("occupation_candidates", [])
                    if isinstance(item, str)
                ),
                run_id=claimed.run_id,
            )
            if not retrieved:
                raise KnowledgeSnapshotUnavailableError(
                    "The frozen snapshot returned no relevant evidence."
                )
            provenance_rows = await asyncio.to_thread(
                self._knowledge_service.repository.source_provenance,
                tuple(item.chunk.document_id for item in retrieved),
            )
            provenance = {
                document_id: SourceProvenance(
                    publisher=value.publisher,
                    release_date=value.release_date,
                    licence=value.licence,
                    jurisdiction=value.jurisdiction,
                    authority_class=value.authority_class,
                )
                for document_id, value in provenance_rows.items()
            }
            evidence = adapt_evidence_context(
                run_id=claimed.run_id,
                tenant_id=f"local-user:{owner_username}",
                policy_version="mvp-report.v1",
                confirmed_case_profile=_evidence_profile(claimed),
                retrieved=retrieved,
                source_provenance=provenance,
            )
        except (
            KnowledgeSnapshotUnavailableError,
            KnowledgeEmbeddingMismatchError,
            EmbeddingError,
            ValueError,
        ) as exc:
            raise _StageFailure(
                "RETRIEVAL_FAILED",
                "The run could not bind and validate a frozen local evidence snapshot.",
            ) from exc

        await asyncio.to_thread(
            self._record_evidence,
            claimed.run_id,
            claimed.language,
            evidence.context.model_dump_json(exclude_none=False),
            len(retrieved),
            evidence.context.fact_pack.snapshot_id,
            coverage,
        )
        return evidence, coverage

    async def _validate_and_save(
        self,
        *,
        claimed: _ClaimedRun,
        report: MvpReportV1,
        fact_pack: FactPack,
        suggestions: tuple[Suggestion, ...],
    ) -> OrchestrationResult:
        validation = validate_report(
            report,
            run_id=claimed.run_id,
            snapshot_id=fact_pack.snapshot_id,
            language=claimed.language,
            fact_pack=fact_pack,
            suggestions=suggestions,
        )
        if not validation.valid:
            raise _StageFailure(
                "VALIDATION_FAILED",
                "The assembled report failed complete deterministic validation.",
                validation_diagnostics=_ValidationFailureDiagnostics(
                    validation=validation,
                ),
            )

        # The persistence capability is deliberately created only after the final 30B
        # decision passes deterministic validation. Review clients cannot receive it.
        from .report_store import ReportStore

        saved = await asyncio.to_thread(ReportStore(self._database).save_report, report)
        return OrchestrationResult(
            run_id=claimed.run_id,
            status="COMPLETE",
            report_id=saved.report_id,
            message="Report assembled, validated, and saved.",
        )

    def _claim_run(self, owner_username: str, run_id: str) -> _ClaimedRun:
        with self._database.connect() as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT runs.run_id, runs.profile_id, runs.status,
                       candidate_profiles.profile_json
                FROM runs
                JOIN chats ON chats.chat_id = runs.chat_id
                JOIN candidate_profiles
                  ON candidate_profiles.profile_id = runs.profile_id
                WHERE runs.run_id = ? AND chats.owner_username = ?
                  AND candidate_profiles.confirmed = 1
                """,
                (run_id, owner_username),
            ).fetchone()
            if row is None:
                raise AnalysisRunNotFoundError(run_id)
            if row["status"] != "queued":
                raise AnalysisRunStateError("Only a queued analysis run can be executed.")
            profile = json.loads(row["profile_json"])
            if not isinstance(profile, dict):
                raise AnalysisRunStateError("The confirmed profile is invalid.")
            progress_rows = connection.execute(
                "SELECT sequence, payload_json FROM messages "
                "WHERE chat_id = (SELECT chat_id FROM runs WHERE run_id = ?) AND kind = 'progress'",
                (run_id,),
            ).fetchall()
            boundary = next((
                int(item["sequence"]) for item in progress_rows
                if isinstance(payload := json.loads(item["payload_json"] or "{}"), dict)
                and payload.get("run_id") == run_id
            ), None)
            if boundary is None:
                raise AnalysisRunStateError("The confirmed analysis message is unavailable.")
            user_rows = connection.execute(
                """
                SELECT messages.text, messages.payload_json
                FROM messages
                JOIN runs ON runs.chat_id = messages.chat_id
                WHERE runs.run_id = ? AND messages.role = 'user'
                  AND messages.sequence < ?
                ORDER BY messages.sequence DESC
                """,
                (run_id, boundary),
            ).fetchall()
            attachment_texts = []
            for item in user_rows:
                payload = json.loads(item["payload_json"] or "{}")
                if (
                    isinstance(payload, dict)
                    and payload.get("attachment_count")
                    and isinstance(extracted := payload.get("extracted_text"), str)
                ):
                    attachment_texts.append(extracted)
                else:
                    attachment_texts.append("")
            language = select_response_language(
                (str(item["text"]) for item in user_rows),
                attachment_texts=attachment_texts,
            )
            connection.execute(
                """
                UPDATE runs
                SET status = 'RUNNING', response_language = ?, updated_at = ?
                WHERE run_id = ? AND status = 'queued'
                """,
                (language, utc_now_iso(), run_id),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise AnalysisRunStateError("The analysis run is already executing.")
        return _ClaimedRun(
            run_id=run_id,
            profile_id=str(row["profile_id"]),
            profile=profile,
            language=language,
        )

    def _record_evidence(
        self,
        run_id: str,
        language: ReportLanguage,
        context_json: str,
        evidence_count: int,
        snapshot_id: str,
        topic_coverage: dict[str, object],
    ) -> None:
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE runs
                SET snapshot_id = ?, response_language = ?, evidence_context_json = ?,
                    updated_at = ?
                WHERE run_id = ? AND status = 'RUNNING' AND snapshot_id = ?
                """,
                (
                    snapshot_id,
                    language,
                    context_json,
                    utc_now_iso(),
                    run_id,
                    snapshot_id,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise AnalysisRunStateError("The run lost its frozen snapshot binding.")
            self._insert_event(
                connection,
                run_id,
                "retrieval",
                {
                    "status": "complete",
                    "snapshot_id": snapshot_id,
                    "evidence_count": evidence_count,
                    "topic_coverage": topic_coverage,
                },
            )

    def _record_review(
        self,
        run_id: str,
        role: str,
        all_suggestions: tuple[Suggestion, ...],
        role_suggestions: tuple[Suggestion, ...],
        attempt: int,
    ) -> None:
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE runs SET suggestions_json = ?, updated_at = ?
                WHERE run_id = ? AND status = 'RUNNING'
                """,
                (
                    json.dumps(
                        [item.model_dump(mode="json") for item in all_suggestions],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    utc_now_iso(),
                    run_id,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise AnalysisRunStateError("The analysis run is no longer running.")
            self._insert_event(
                connection,
                run_id,
                role,
                {
                    "status": "complete",
                    "attempt": attempt,
                    "count": len(role_suggestions),
                },
            )

    def _record_event(
        self, run_id: str, event_type: str, payload: dict[str, object]
    ) -> None:
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None or str(row[0]) != "RUNNING":
                return
            self._insert_event(connection, run_id, event_type, payload)

    def _record_stage_attempt(
        self, run_id: str, diagnostic: StageAttemptDiagnostics,
    ) -> None:
        # Revalidate at the storage boundary; model_copy deliberately skips validation.
        payload = StageAttemptDiagnostics.model_validate_json(diagnostic.model_dump_json())
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO run_stage_attempts
                    (run_id, stage, attempt, diagnostic_json, updated_at)
                SELECT ?, ?, ?, ?, ?
                WHERE EXISTS (SELECT 1 FROM runs WHERE run_id = ? AND status = 'RUNNING')
                ON CONFLICT(run_id, stage, attempt) DO UPDATE SET
                    diagnostic_json = excluded.diagnostic_json,
                    updated_at = excluded.updated_at
                """,
                (run_id, payload.stage, payload.attempt, payload.model_dump_json(),
                 utc_now_iso(), run_id),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise AnalysisRunStateError("The analysis run is no longer running.")

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), -1) + 1 FROM run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO run_events (
                event_id, run_id, sequence, event_type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                run_id,
                sequence,
                event_type,
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                utc_now_iso(),
            ),
        )
        if event_type in ANALYSIS_STAGE_MESSAGES and payload.get("status") in {"running", "complete"}:
            update_run_progress_message(
                connection, run_id, status="running", current_stage=event_type,
                completed_stages=completed_run_stages(connection, run_id),
            )

    def _fail_run(
        self,
        run_id: str,
        code: RunFailureCode,
        message: str,
        *,
        validation_diagnostics: _ValidationFailureDiagnostics | None = None,
    ) -> None:
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE runs
                SET status = ?, error_code = ?, error_message = ?, updated_at = ?
                WHERE run_id = ? AND status = 'RUNNING'
                """,
                (code, code, message, utc_now_iso(), run_id),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                row = connection.execute(
                    "SELECT status FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if row is None or str(row[0]) != "RUNNING":
                    return
                raise AnalysisRunStateError(
                    "The running analysis could not enter a terminal state."
                )
            event_type = "validation" if code == "VALIDATION_FAILED" else "failed"
            event_payload: dict[str, object] = {
                "status": "failed",
                "code": code,
                "message": message,
            }
            if code == "VALIDATION_FAILED":
                event_payload["validator_categories"] = (
                    validation_diagnostics.event_payload()
                    if validation_diagnostics is not None
                    else {
                        "evaluated": False,
                        "category_counts": {},
                    }
                )
            self._insert_event(
                connection,
                run_id,
                event_type,
                event_payload,
            )
            update_run_progress_message(
                connection,
                run_id,
                status="cancelled" if code == "ANALYSIS_INTERRUPTED" else "failed",
                current_stage=code,
                completed_stages=completed_run_stages(connection, run_id),
            )
            append_run_message(
                connection, run_id, kind="error", text=RUN_ERROR_MESSAGES[code],
                payload={"run_id": run_id, "code": code},
            )


def _parse_contract(
    contract_type: type[_ContractT], content: dict[str, object]
) -> _ContractT:
    return contract_type.model_validate_json(
        json.dumps(content, ensure_ascii=False, allow_nan=False),
        strict=True,
    )


def _validate_suggestion_set(
    suggestions: tuple[Suggestion, ...],
    *,
    expected_category: str,
    expected_id_prefix: str,
    draft_claim_ids: frozenset[str],
    factpack_record_ids: frozenset[str],
    forbidden_suggestion_ids: frozenset[str],
) -> None:
    reused_index = next((
        index for index, item in enumerate(suggestions)
        if item.suggestion_id in forbidden_suggestion_ids
    ), None)
    if reused_index is not None:
        raise _ReviewBindingError(("suggestions", reused_index, "suggestion_id"))
    for index, suggestion in enumerate(suggestions):
        if suggestion.category != expected_category:
            raise _ReviewBindingError(("suggestions", index, "category"))
        if not suggestion.suggestion_id.startswith(expected_id_prefix):
            raise _ReviewBindingError(("suggestions", index, "suggestion_id"))
        if not set(suggestion.affected_claim_ids) <= draft_claim_ids:
            raise _ReviewBindingError(("suggestions", index, "affected_claim_ids"))
        if not set(suggestion.evidence_refs) <= factpack_record_ids:
            raise _ReviewBindingError(("suggestions", index, "evidence_refs"))


def _factpack_record_ids(fact_pack: object) -> set[str]:
    identifiers: set[str] = set()
    identifiers.update(item.fact_id for item in fact_pack.facts)
    identifiers.update(item.evidence_id for item in fact_pack.passages)
    identifiers.update(item.task_id for item in fact_pack.tasks)
    identifiers.update(item.method_id for item in fact_pack.methods)
    identifiers.update(item.source_id for item in fact_pack.sources)
    return identifiers


def _report_narrative_count(narratives: ReportNarrativePack) -> int:
    sections = narratives.sections
    return (
        3
        + 2 * len(sections.horizon_scenarios)
        + 2 * len(sections.task_impact_matrix)
        + 2 * len(sections.practical_next_actions)
    )


def _retrieval_query(profile: dict[str, object]) -> str:
    parts: list[str] = []
    # Retrieve occupational evidence using the work itself. Generic tool names,
    # locations and goals can dominate lexical matches in an English corpus even
    # when the confirmed role and duties are in Chinese. Retain them as a fallback
    # for profiles that do not yet name a role or describe any work.
    for keys in (
        ("occupation_candidates", "responsibilities", "industry_context"),
        ("skills", "region", "goals"),
    ):
        for key in keys:
            value = profile.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
            elif isinstance(value, list):
                parts.extend(item.strip() for item in value if isinstance(item, str) and item.strip())
        if parts:
            break
    query = " ".join(dict.fromkeys(parts))[:2_000].strip()
    if not query:
        raise ValueError("The confirmed profile contains no retrievable terms.")
    return query


def _evidence_profile(claimed: _ClaimedRun) -> ConfirmedCaseProfile:
    profile_json = json.dumps(
        claimed.profile,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    profile_digest = hmac.new(secrets.token_bytes(32), profile_json, hashlib.sha256).hexdigest()

    def strings(name: str) -> tuple[str, ...]:
        value = claimed.profile.get(name)
        if not isinstance(value, list):
            return ()
        return tuple(item for item in value if isinstance(item, str) and item.strip())

    # Candidate cards contain occupation titles, not a confirmed OSCA/ANZSCO code.
    # The evidence boundary therefore leaves coded occupations empty instead of
    # fabricating a classification; the complete confirmed card is still supplied to
    # the decision prompts and persisted on the run.
    return ConfirmedCaseProfile(
        profile_ref=f"service-hmac:{profile_digest}",
        region=(
            str(claimed.profile.get("region")).strip()
            if claimed.profile.get("region")
            else "unavailable"
        ),
        confirmed_occupations=(),
        confirmed_responsibilities=tuple(
            ConfirmedResponsibility(
                responsibility_id=f"responsibility-{index:03d}", text=text
            )
            for index, text in enumerate(strings("responsibilities"), start=1)
        ),
        confirmed_skills=tuple(
            ConfirmedSkill(skill_id=f"skill-{index:03d}", text=text)
            for index, text in enumerate(strings("skills"), start=1)
        ),
        user_goals=tuple(
            UserGoal(goal_id=f"goal-{index:03d}", text=text)
            for index, text in enumerate(strings("goals"), start=1)
        ),
    )


__all__ = [
    "AnalysisRunNotFoundError",
    "AnalysisRunStateError",
    "OrchestrationResult",
    "StarOrchestrator",
]
