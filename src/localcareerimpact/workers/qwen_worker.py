"""Single-model, stdin/stdout JSON-lines worker for a resident Qwen model."""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from localcareerimpact.app.config import ConfigurationError, ModelPaths
from localcareerimpact.workers.protocol import (
    ErrorEnvelope,
    GenerateRequest,
    GenerationFailureCategory,
    GenerationMetrics,
    ModelKey,
    ReadyEnvelope,
    ResultEnvelope,
    sanitize_schema_error_path,
)


LOGGER = logging.getLogger("localcareerimpact.qwen_worker")
_REFERENCE_KEYWORDS = frozenset({"$ref", "$dynamicRef", "$recursiveRef"})


class _ReasoningMarkupError(ValueError):
    pass


class _DuplicateKeyError(ValueError):
    pass


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one resident local Qwen worker.")
    parser.add_argument("--model-key", required=True, choices=("qwen30b", "qwen4b"))
    parser.add_argument("--config", required=True, type=Path)
    return parser.parse_args()


def _emit(envelope: ReadyEnvelope | ResultEnvelope | ErrorEnvelope) -> None:
    sys.stdout.write(envelope.model_dump_json() + "\n")
    sys.stdout.flush()


def _safe_error(
    model_key: ModelKey,
    code: Literal["invalid_request", "generation_failed", "startup_failed"],
    message: str,
    request_id: str | None = None,
    *,
    category: GenerationFailureCategory | None = None,
    metrics: GenerationMetrics | None = None,
    parse_error_offset: int | None = None,
    schema_error_path: tuple[int | str, ...] = (),
) -> ErrorEnvelope:
    return ErrorEnvelope(
        request_id=request_id,
        model_key=model_key,
        code=code,
        message=message,
        category=category,
        metrics=metrics,
        parse_error_offset=parse_error_offset,
        schema_error_path=schema_error_path,
    )


def _prompt_for(request: GenerateRequest, processor: Any, model: Any) -> str:
    from mlx_vlm import apply_chat_template

    contract = (
        f"Private worker role: {request.role}. Return exactly one JSON object matching "
        "the supplied JSON Schema. Do not include markdown, hidden reasoning, chain of "
        "thought, or commentary. JSON Schema: "
        f"{json.dumps(request.response_schema, ensure_ascii=False, sort_keys=True)}"
    )
    messages = [message.model_dump() for message in request.messages]
    if messages[0]["role"] == "system":
        messages[0]["content"] = f"{messages[0]['content']}\n\n{contract}"
    else:
        messages.insert(0, {"role": "system", "content": contract})
    return apply_chat_template(processor, model.config, messages)


def _json_object_from_generation(
    text: str,
    response_schema: dict[str, object],
    *,
    allow_single_json_fence: bool = False,
) -> dict[str, object]:
    from jsonschema.validators import validator_for
    from referencing import Registry

    visible = text.strip()
    lowered = visible.lower()
    if lowered.startswith("<think>"):
        closing_index = lowered.find("</think>", len("<think>"))
        if closing_index < 0:
            raise _ReasoningMarkupError
        reasoning = lowered[len("<think>") : closing_index]
        if "<think" in reasoning or "</think" in reasoning:
            raise _ReasoningMarkupError
        visible = visible[closing_index + len("</think>") :].strip()
        lowered = visible.lower()

    if "<think" in lowered or "</think" in lowered:
        raise _ReasoningMarkupError

    lines = visible.splitlines()
    if (
        allow_single_json_fence
        and len(lines) >= 3
        and lines[0].strip().lower() in {"```", "```json"}
        and lines[-1].strip() == "```"
    ):
        fenced_value = "\n".join(lines[1:-1]).strip()
        if not fenced_value or "```" in fenced_value:
            raise ValueError("model output contains an invalid JSON fence")
        visible = fenced_value

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant is forbidden: {value}")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateKeyError
            result[key] = value
        return result

    value = json.loads(
        visible,
        parse_constant=reject_constant,
        object_pairs_hook=reject_duplicate_keys,
    )
    if not isinstance(value, dict):
        raise ValueError("model output must be one JSON object")

    def reject_non_finite(candidate: object) -> None:
        if isinstance(candidate, float) and not math.isfinite(candidate):
            raise ValueError("non-finite JSON number is forbidden")
        if isinstance(candidate, dict):
            for child in candidate.values():
                reject_non_finite(child)
        elif isinstance(candidate, list):
            for child in candidate:
                reject_non_finite(child)

    def require_offline_schema(candidate: object) -> None:
        if isinstance(candidate, dict):
            for key, child in candidate.items():
                if key in _REFERENCE_KEYWORDS:
                    if not isinstance(child, str) or not (
                        child == "" or child.startswith("#")
                    ):
                        raise ValueError("external JSON Schema reference is forbidden")
                elif key == "$schema":
                    if not isinstance(child, str) or validator_for(
                        {"$schema": child},
                        default=None,
                    ) is None:
                        raise ValueError("unsupported JSON Schema dialect")
                require_offline_schema(child)
        elif isinstance(candidate, list):
            for child in candidate:
                require_offline_schema(child)

    reject_non_finite(value)
    require_offline_schema(response_schema)
    validator_class = validator_for(response_schema)
    validator_class.check_schema(response_schema)
    validator_class(response_schema, registry=Registry()).validate(value)
    return value


def _generation_metrics(generation: Any, request: GenerateRequest) -> GenerationMetrics:
    return GenerationMetrics(
        prompt_tokens=generation.prompt_tokens,
        generation_tokens=generation.generation_tokens,
        total_tokens=generation.total_tokens,
        generation_limit=request.max_tokens,
        reached_generation_limit=generation.generation_tokens >= request.max_tokens,
    )


def _failure_diagnostics(
    exc: Exception,
    metrics: GenerationMetrics | None,
    response_schema: dict[str, object],
) -> tuple[GenerationFailureCategory, int | None, tuple[int | str, ...]]:
    from jsonschema import ValidationError as JsonSchemaValidationError

    if isinstance(exc, _ReasoningMarkupError):
        category: GenerationFailureCategory = "reasoning_markup"
    elif isinstance(exc, _DuplicateKeyError):
        category = "duplicate_key"
    elif isinstance(exc, json.JSONDecodeError):
        category = (
            "truncated"
            if metrics is not None and metrics.reached_generation_limit
            else "json_parse"
        )
    elif isinstance(exc, JsonSchemaValidationError):
        category = "schema_mismatch"
    else:
        category = "runtime_error"

    parse_error_offset = exc.pos if isinstance(exc, json.JSONDecodeError) else None
    schema_error_path: tuple[int | str, ...] = ()
    if isinstance(exc, JsonSchemaValidationError):
        path = list(exc.absolute_path)
        # JSON Schema locates a missing required field at its parent object.
        # Name the missing schema-owned field without copying model content.
        if exc.validator == "required" and isinstance(exc.instance, dict):
            missing = next((
                name for name in exc.validator_value if name not in exc.instance
            ), None)
            if missing is not None:
                path.append(missing)
        schema_error_path = sanitize_schema_error_path(path, response_schema)
    return category, parse_error_offset, schema_error_path


def _generate(
    request: GenerateRequest,
    model: Any,
    processor: Any,
) -> tuple[str, GenerationMetrics]:
    import mlx.core as mx
    from mlx_vlm import generate

    prompt = _prompt_for(request, processor, model)
    mx.random.seed(request.seed)
    generation = generate(
        model,
        processor,
        prompt,
        verbose=False,
        max_tokens=request.max_tokens,
        temperature=float(request.temperature),
        top_p=float(request.top_p),
        repetition_penalty=float(request.repetition_penalty),
    )
    return generation.text, _generation_metrics(generation, request)


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    args = _parse_args()
    model_key: ModelKey = args.model_key

    try:
        model_paths = ModelPaths.from_file(args.config, validate_all_paths=False)
        model_path = model_paths.qwen_path(model_key)
        from mlx_vlm import load

        LOGGER.info("Loading configured %s model from local storage.", model_key)
        model, processor = load(
            str(model_path),
            lazy=False,
            local_files_only=model_paths.local_files_only,
            trust_remote_code=False,
        )
    except ConfigurationError:
        LOGGER.exception("The configured %s model directory is unavailable.", model_key)
        _emit(
            _safe_error(
                model_key,
                "startup_failed",
                "configured model directory is unavailable",
            )
        )
        return 1
    except Exception:
        LOGGER.exception("The configured %s model failed to load.", model_key)
        _emit(
            _safe_error(
                model_key,
                "startup_failed",
                "configured local model failed to load",
            )
        )
        return 1

    _emit(ReadyEnvelope(model_key=model_key))
    LOGGER.info("Configured %s model is resident and ready.", model_key)

    for line in sys.stdin:
        request_id: str | None = None
        try:
            request = GenerateRequest.model_validate_json(line, strict=True)
            request_id = request.request_id
        except ValidationError:
            LOGGER.warning("Rejected an invalid generate request for %s.", model_key)
            _emit(_safe_error(model_key, "invalid_request", "invalid generate request"))
            continue

        metrics: GenerationMetrics | None = None
        try:
            generated_text, metrics = _generate(request, model, processor)
            content = _json_object_from_generation(
                generated_text,
                request.response_schema,
                allow_single_json_fence=request.allow_single_json_fence,
            )
            _emit(
                ResultEnvelope(
                    request_id=request.request_id,
                    content=content,
                    metrics=metrics,
                )
            )
        except Exception as exc:
            category, parse_error_offset, schema_error_path = _failure_diagnostics(
                exc,
                metrics,
                request.response_schema,
            )
            LOGGER.warning(
                "Generation failed for %s request %s (%s).",
                model_key,
                request_id,
                type(exc).__name__,
            )
            _emit(
                _safe_error(
                    model_key,
                    "generation_failed",
                    "local model generation failed",
                    request_id=request_id,
                    category=category,
                    metrics=metrics,
                    parse_error_offset=parse_error_offset,
                    schema_error_path=schema_error_path,
                )
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
