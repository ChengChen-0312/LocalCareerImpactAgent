"""Strict JSON-lines protocol shared by the app and Qwen subprocesses."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, TypeAdapter, field_validator, model_validator

from localcareerimpact.contracts.base import StrictContract


ModelKey: TypeAlias = Literal["qwen30b", "qwen4b"]
GenerationFailureCategory: TypeAlias = Literal[
    "runtime_error",
    "truncated",
    "reasoning_markup",
    "json_parse",
    "duplicate_key",
    "schema_mismatch",
]
MAX_GENERATE_CONTEXT_CODEPOINTS = 60_000
WorkerRole: TypeAlias = Literal[
    "profile",
    "draft",
    "evidence_review",
    "boundary_review",
    "safety_review",
    "resolution_decision",
    "revised_claims",
    "report_narratives",
]
_SCHEMA_ERROR_PATH_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def is_safe_schema_error_path_identifier(value: object) -> bool:
    return isinstance(value, str) and bool(
        _SCHEMA_ERROR_PATH_IDENTIFIER.fullmatch(value)
    )


def is_safe_schema_error_path_segment(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    return value == "<field>" or is_safe_schema_error_path_identifier(value)


def _schema_error_path_identifiers(
    response_schema: dict[str, object],
) -> frozenset[str]:
    identifiers: set[str] = set()
    pending: list[object] = [response_schema]
    while pending:
        candidate = pending.pop()
        if isinstance(candidate, dict):
            properties = candidate.get("properties")
            if isinstance(properties, dict):
                identifiers.update(
                    key
                    for key in properties
                    if is_safe_schema_error_path_identifier(key)
                )
            pending.extend(candidate.values())
        elif isinstance(candidate, list):
            pending.extend(candidate)
    return frozenset(identifiers)


def sanitize_schema_error_path(
    path: Iterable[object],
    response_schema: dict[str, object],
) -> tuple[int | str, ...]:
    known_identifiers = _schema_error_path_identifiers(response_schema)
    sanitized: list[int | str] = []
    for segment in path:
        if isinstance(segment, int) and is_safe_schema_error_path_segment(segment):
            sanitized.append(segment)
        elif (
            isinstance(segment, str)
            and segment in known_identifiers
            and is_safe_schema_error_path_identifier(segment)
        ):
            sanitized.append(segment)
        else:
            sanitized.append("<field>")
    return tuple(sanitized)


class WorkerMessage(StrictContract):
    role: Literal["system", "user", "assistant"]
    content: str


class GenerateRequest(StrictContract):
    kind: Literal["generate"] = "generate"
    request_id: str = Field(min_length=1, max_length=128)
    role: WorkerRole
    messages: tuple[WorkerMessage, ...] = Field(min_length=1)
    response_schema: dict[str, object]
    max_tokens: int = Field(ge=1, le=8192)
    temperature: str
    top_p: str
    repetition_penalty: str
    seed: int
    allow_single_json_fence: bool = False

    @field_validator("temperature")
    @classmethod
    def validate_temperature(cls, value: str) -> str:
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("temperature must be a decimal string") from exc
        if not parsed.is_finite() or parsed < 0 or parsed > 2:
            raise ValueError("temperature must be between 0 and 2")
        return value

    @field_validator("top_p")
    @classmethod
    def validate_top_p(cls, value: str) -> str:
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("top_p must be a decimal string") from exc
        if not parsed.is_finite() or parsed <= 0 or parsed > 1:
            raise ValueError("top_p must be greater than 0 and at most 1")
        return value

    @field_validator("repetition_penalty")
    @classmethod
    def validate_repetition_penalty(cls, value: str) -> str:
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("repetition_penalty must be a decimal string") from exc
        if not parsed.is_finite() or parsed < 0:
            raise ValueError("repetition_penalty must be non-negative")
        return value

    @model_validator(mode="after")
    def validate_context_and_transport_mode(self) -> GenerateRequest:
        if self.allow_single_json_fence and self.role == "profile":
            raise ValueError("profile generation requires strict bare JSON")
        schema_text = json.dumps(
            self.response_schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        context_size = len(schema_text) + sum(
            len(message.role) + len(message.content) for message in self.messages
        )
        if context_size > MAX_GENERATE_CONTEXT_CODEPOINTS:
            raise ValueError(
                "aggregate worker prompt and schema exceed the context code-point budget"
            )
        return self


class ReadyEnvelope(StrictContract):
    kind: Literal["ready"] = "ready"
    model_key: ModelKey


class GenerationMetrics(StrictContract):
    prompt_tokens: int = Field(ge=0)
    generation_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    generation_limit: int = Field(ge=1, le=8192)
    reached_generation_limit: bool


class ResultEnvelope(StrictContract):
    kind: Literal["result"] = "result"
    request_id: str
    content: dict[str, object]
    metrics: GenerationMetrics


class ErrorEnvelope(StrictContract):
    kind: Literal["error"] = "error"
    request_id: str | None
    model_key: ModelKey
    code: Literal["invalid_request", "generation_failed", "startup_failed"]
    message: str
    category: GenerationFailureCategory | None = None
    metrics: GenerationMetrics | None = None
    parse_error_offset: int | None = Field(default=None, ge=0)
    schema_error_path: tuple[int | str, ...] = ()

    @field_validator("schema_error_path")
    @classmethod
    def validate_schema_error_path(
        cls,
        value: tuple[int | str, ...],
    ) -> tuple[int | str, ...]:
        if not all(is_safe_schema_error_path_segment(segment) for segment in value):
            raise ValueError("schema error path contains an unsafe segment")
        return value

    @model_validator(mode="after")
    def validate_diagnostic_fields(self) -> ErrorEnvelope:
        if self.code != "generation_failed":
            if (
                self.category is not None
                or self.metrics is not None
                or self.parse_error_offset is not None
                or self.schema_error_path
            ):
                raise ValueError(
                    "diagnostic fields are reserved for generation failures"
                )
            return self

        if self.category is None:
            raise ValueError("generation failures require a category")
        if (
            self.parse_error_offset is not None
            and self.category not in {"json_parse", "truncated"}
        ):
            raise ValueError(
                "parse error offsets require a JSON parse or truncation failure"
            )
        if self.schema_error_path and self.category != "schema_mismatch":
            raise ValueError("schema error paths require a schema mismatch")
        return self


WorkerEnvelope = Annotated[
    ReadyEnvelope | ResultEnvelope | ErrorEnvelope,
    Field(discriminator="kind"),
]
WORKER_ENVELOPE_ADAPTER = TypeAdapter(WorkerEnvelope)


def parse_worker_envelope(line: bytes | str) -> WorkerEnvelope:
    return WORKER_ENVELOPE_ADAPTER.validate_json(line, strict=True)
