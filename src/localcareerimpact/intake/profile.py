"""Typed profile drafting through the resident 30B decision worker."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from localcareerimpact.app.model_runtime import ModelRuntime
from localcareerimpact.agent.prompts import profile_extraction_messages, stage_retry_message
from localcareerimpact.contracts.base import StrictContract
from localcareerimpact.workers.client import WorkerCallError
from localcareerimpact.workers.protocol import (
    GenerateRequest,
    GenerationFailureCategory,
    GenerationMetrics,
    WorkerMessage,
    sanitize_schema_error_path,
)


ShortText = Annotated[str, Field(min_length=1, max_length=500)]
_RETRYABLE_PROFILE_FAILURES = frozenset(
    {"json_parse", "schema_mismatch", "truncated", "duplicate_key", "reasoning_markup"}
)


class CandidateProfileDraft(BaseModel):
    """Visible candidate facts plus concise uncertainty notes, never hidden reasoning."""

    model_config = ConfigDict(extra="forbid", strict=True)

    occupation_candidates: list[ShortText] = Field(max_length=5)
    region: str | None = Field(default=None, max_length=200)
    years_of_experience: float | None = Field(default=None, ge=0, le=80)
    responsibilities: list[ShortText] = Field(default_factory=list, max_length=30)
    skills: list[ShortText] = Field(default_factory=list, max_length=50)
    industry_context: str | None = Field(default=None, max_length=1000)
    goals: list[ShortText] = Field(default_factory=list, max_length=20)
    confidence_notes: list[ShortText] = Field(default_factory=list, max_length=20)

    @field_validator("region", "industry_context")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator(
        "occupation_candidates",
        "responsibilities",
        "skills",
        "goals",
        "confidence_notes",
    )
    @classmethod
    def normalize_text_list(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = value.strip()
            folded = item.casefold()
            if not item or folded in seen:
                continue
            seen.add(folded)
            normalized.append(item)
        return normalized


class ProfileGenerationError(RuntimeError):
    """Raised when the private 30B worker cannot produce the closed profile."""


class ProfileAttemptDiagnostics(StrictContract):
    """Internal profile-stage metadata with no candidate or error-message content."""

    stage: Literal["profile"] = "profile"
    attempt: int = Field(ge=1, le=2)
    status: Literal["started", "complete", "failed"]
    boundary: Literal["request", "generation", "contract"]
    category: GenerationFailureCategory | Literal[
        "invalid_request", "startup_failed", "transport_error", "generation_failed"
    ] | None = None
    metrics: GenerationMetrics | None = None
    schema_error_path: tuple[int | str, ...] = ()
    parse_error_offset: int | None = Field(default=None, ge=0)
    context_codepoints: int | None = Field(default=None, ge=0)

    @field_validator("schema_error_path")
    @classmethod
    def validate_schema_error_path(
        cls, value: tuple[int | str, ...]
    ) -> tuple[int | str, ...]:
        if value != sanitize_schema_error_path(
            value, CandidateProfileDraft.model_json_schema()
        ):
            raise ValueError("profile schema error path contains an unsafe segment")
        return value

    @model_validator(mode="after")
    def validate_diagnostic_fields(self) -> ProfileAttemptDiagnostics:
        if self.status == "failed":
            if self.category is None:
                raise ValueError("failed profile attempts require a category")
        elif (
            self.category is not None
            or self.schema_error_path
            or self.parse_error_offset is not None
        ):
            raise ValueError("failure details require a failed profile attempt")
        if self.status == "started" and self.metrics is not None:
            raise ValueError("started profile attempts cannot have generation metrics")
        if self.schema_error_path and self.category != "schema_mismatch":
            raise ValueError("schema paths require a schema mismatch")
        if (
            self.parse_error_offset is not None
            and self.category not in {"json_parse", "truncated"}
        ):
            raise ValueError("parse offsets require a JSON parse or truncation failure")
        return self


async def draft_candidate_profile(
    runtime: ModelRuntime,
    *,
    request_id: str,
    user_text: str,
    extracted_text: str,
    record_attempt: Callable[[ProfileAttemptDiagnostics], None] | None = None,
) -> CandidateProfileDraft:
    context_codepoints: int | None = None
    retry_message: WorkerMessage | None = None

    def record(
        status: Literal["started", "complete", "failed"],
        boundary: Literal["request", "generation", "contract"],
        **details: object,
    ) -> None:
        if record_attempt is not None:
            record_attempt(
                ProfileAttemptDiagnostics(
                    attempt=attempt,
                    status=status,
                    boundary=boundary,
                    context_codepoints=context_codepoints,
                    **details,
                )
            )

    for attempt in (1, 2):
        context_codepoints = None
        # Cancellation leaves this attempt started; it does not imply failure.
        record("started", "request")
        try:
            response_schema = CandidateProfileDraft.model_json_schema()
            # Explicit empty/null values preserve unknowns without letting the
            # ordered generation grammar skip known candidate fields entirely.
            response_schema["required"] = list(response_schema["properties"])
            messages = profile_extraction_messages(
                user_text=user_text,
                extracted_text=extracted_text,
            )
            if retry_message is not None:
                messages = (*messages, retry_message)
            context_codepoints = len(
                json.dumps(
                    response_schema,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ) + sum(len(message.role) + len(message.content) for message in messages)
            worker_request = GenerateRequest(
                request_id=request_id if attempt == 1 else str(uuid4()),
                role="profile",
                messages=messages,
                response_schema=response_schema,
                max_tokens=1600,
                temperature="0",
                top_p="1",
                repetition_penalty="1",
                seed=5001,
            )
        except ValidationError as exc:
            record("failed", "request", category="invalid_request")
            raise ProfileGenerationError(
                "The local profile model could not produce a valid candidate profile."
            ) from exc

        try:
            envelope = await runtime.generate("qwen30b", worker_request)
        except WorkerCallError as exc:
            schema_error_path = sanitize_schema_error_path(
                exc.schema_error_path, response_schema
            )
            record(
                "failed",
                "generation",
                category=exc.category or exc.code or "transport_error",
                metrics=exc.metrics,
                parse_error_offset=exc.parse_error_offset,
                schema_error_path=schema_error_path,
            )
            if (
                attempt == 1
                and exc.code == "generation_failed"
                and exc.category in _RETRYABLE_PROFILE_FAILURES
            ):
                retry_message = stage_retry_message(
                    category=exc.category,
                    schema_error_path=schema_error_path,
                    parse_error_offset=exc.parse_error_offset,
                )
                continue
            raise ProfileGenerationError(
                "The local profile model could not produce a valid candidate profile."
            ) from exc
        except ValidationError as exc:
            record("failed", "generation", category="schema_mismatch")
            raise ProfileGenerationError(
                "The local profile model could not produce a valid candidate profile."
            ) from exc

        try:
            profile = CandidateProfileDraft.model_validate(envelope.content, strict=True)
        except ValidationError as exc:
            first_error = exc.errors(include_input=False, include_context=False)[0]
            schema_error_path = sanitize_schema_error_path(
                first_error["loc"], response_schema
            )
            record(
                "failed",
                "contract",
                category="schema_mismatch",
                metrics=envelope.metrics,
                schema_error_path=schema_error_path,
            )
            if attempt == 1:
                retry_message = stage_retry_message(
                    category="schema_mismatch",
                    schema_error_path=schema_error_path,
                )
                continue
            raise ProfileGenerationError(
                "The local profile model could not produce a valid candidate profile."
            ) from exc
        record("complete", "contract", metrics=envelope.metrics)
        return profile

    raise ProfileGenerationError(
        "The local profile model could not produce a valid candidate profile."
    )
