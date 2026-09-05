"""Bounded, local-only extraction for candidate attachments."""

from __future__ import annotations

import asyncio
import math
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from localcareerimpact.app.config import AppSettings
from localcareerimpact.app.model_runtime import ModelRuntime

from .audio import (
    AudioDependencyError,
    AudioProcessingError,
    AudioValidationError,
    normalize_audio,
    transcribe_audio,
)
from .limits import (
    LOCAL_TOOL_MAX_STDERR_BYTES,
    MAX_UPLOAD_COUNT,
    OCR_MAX_STDOUT_BYTES,
    PDF_MAX_PAGES,
    PDF_MAX_RASTER_HEIGHT_PIXELS,
    PDF_MAX_RASTER_PIXELS,
    PDF_MAX_RASTER_WIDTH_PIXELS,
    PDF_OCR_DPI,
    PDF_OCR_TIMEOUT_SECONDS,
    PDF_TEXT_DENSITY_THRESHOLD,
    TEXT_MAX_CODEPOINTS,
)
from .multipart import AttachmentType, IntakeValidationError, StoredCandidateUpload
from .profile import (
    CandidateProfileDraft,
    ProfileAttemptDiagnostics,
    ProfileGenerationError,
    draft_candidate_profile,
)


class IntakeDependencyError(RuntimeError):
    """Raised when a configured local extraction dependency is unavailable."""


class IntakeProcessingError(RuntimeError):
    """Raised when valid candidate input cannot be processed locally."""


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    extracted_text: str
    attachment_types: tuple[AttachmentType, ...]


@dataclass(frozen=True, slots=True)
class CandidateMessageIntake:
    extracted_text: str
    attachment_types: tuple[AttachmentType, ...]
    profile: CandidateProfileDraft


def _check_text_limit(text: str) -> None:
    if len(text) > TEXT_MAX_CODEPOINTS:
        raise IntakeValidationError(
            "Candidate text must not exceed 30,000 Unicode code points."
        )


def _decode_text(path: Path) -> str:
    try:
        text = path.read_bytes().decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise IntakeValidationError("TXT attachments must use valid UTF-8 encoding.") from exc
    text = text.strip()
    _check_text_limit(text)
    return text


async def _run_tesseract(image_path: Path) -> str:
    executable = shutil.which("tesseract")
    if executable is None:
        raise IntakeDependencyError("The configured local OCR tool is unavailable.")
    process = await asyncio.create_subprocess_exec(
        executable,
        str(image_path),
        "stdout",
        "-l",
        "eng+chi_sim+chi_tra",
        "--dpi",
        str(PDF_OCR_DPI),
        "--psm",
        "6",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def read_bounded(
        stream: asyncio.StreamReader | None, maximum_bytes: int
    ) -> tuple[bytes, bool]:
        if stream is None:
            return b"", False
        collected = bytearray()
        exceeded = False
        while chunk := await stream.read(64 * 1024):
            remaining = maximum_bytes - len(collected)
            if remaining > 0:
                collected.extend(chunk[:remaining])
            if len(chunk) > remaining:
                exceeded = True
        return bytes(collected), exceeded

    stdout_task = asyncio.create_task(read_bounded(process.stdout, OCR_MAX_STDOUT_BYTES))
    stderr_task = asyncio.create_task(
        read_bounded(process.stderr, LOCAL_TOOL_MAX_STDERR_BYTES)
    )
    wait_task = asyncio.create_task(process.wait())
    completion = asyncio.gather(stdout_task, stderr_task, wait_task)

    async def kill_reap_and_drain() -> None:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await asyncio.shield(
            asyncio.gather(
                stdout_task,
                stderr_task,
                wait_task,
                return_exceptions=True,
            )
        )

    try:
        async with asyncio.timeout(PDF_OCR_TIMEOUT_SECONDS):
            (stdout, stdout_exceeded), (_stderr, _stderr_exceeded), _returncode = (
                await asyncio.shield(completion)
            )
    except asyncio.TimeoutError as exc:
        await kill_reap_and_drain()
        raise IntakeProcessingError("Local PDF OCR timed out.") from exc
    except asyncio.CancelledError:
        await kill_reap_and_drain()
        raise
    except BaseException:
        await kill_reap_and_drain()
        raise
    if stdout_exceeded:
        raise IntakeValidationError("Local PDF OCR output exceeds the candidate text limit.")
    if _stderr_exceeded:
        raise IntakeProcessingError("Local PDF OCR returned excessive diagnostic output.")
    if process.returncode != 0:
        raise IntakeProcessingError("Local PDF OCR failed.")
    try:
        return stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise IntakeProcessingError("Local PDF OCR returned invalid text.") from exc


async def _extract_pdf(path: Path, request_dir: Path, file_index: int) -> str:
    try:
        import pymupdf
    except ImportError as exc:
        raise IntakeDependencyError("The configured local PDF extractor is unavailable.") from exc

    with path.open("rb") as source:
        signature = source.read(5)
    if signature != b"%PDF-":
        raise IntakeValidationError("The PDF attachment is not a valid PDF document.")
    try:
        document = pymupdf.open(path)
    except Exception as exc:
        raise IntakeValidationError("The PDF attachment could not be opened.") from exc

    try:
        if document.needs_pass:
            raise IntakeValidationError("Password-protected PDF attachments are not supported.")
        if document.page_count < 1:
            raise IntakeValidationError("The PDF attachment has no pages.")
        if document.page_count > PDF_MAX_PAGES:
            raise IntakeValidationError("PDF attachments must not exceed 30 pages.")

        page_texts: list[str] = []
        for page_index, page in enumerate(document):
            embedded_text = page.get_text("text", sort=True).strip()
            visible_characters = len("".join(embedded_text.split()))
            area = max(float(page.rect.width * page.rect.height), 1.0)
            density = visible_characters / area
            selected_text = embedded_text
            if density < PDF_TEXT_DENSITY_THRESHOLD:
                raster_width = math.ceil(float(page.rect.width) * PDF_OCR_DPI / 72)
                raster_height = math.ceil(float(page.rect.height) * PDF_OCR_DPI / 72)
                if (
                    raster_width <= 0
                    or raster_height <= 0
                    or raster_width > PDF_MAX_RASTER_WIDTH_PIXELS
                    or raster_height > PDF_MAX_RASTER_HEIGHT_PIXELS
                    or raster_width * raster_height > PDF_MAX_RASTER_PIXELS
                ):
                    raise IntakeValidationError(
                        "A PDF page is too large to render safely for local OCR."
                    )
                image_path = request_dir / f"ocr-{file_index}-{page_index}.png"
                try:
                    pixmap = page.get_pixmap(
                        dpi=PDF_OCR_DPI,
                        colorspace=pymupdf.csGRAY,
                        alpha=False,
                    )
                    pixmap.save(image_path)
                    ocr_text = await _run_tesseract(image_path)
                    if ocr_text:
                        selected_text = ocr_text
                finally:
                    image_path.unlink(missing_ok=True)
            if selected_text:
                page_texts.append(selected_text)
            _check_text_limit("\n\n".join(page_texts))
        return "\n\n".join(page_texts).strip()
    except (IntakeValidationError, IntakeDependencyError, IntakeProcessingError):
        raise
    except Exception as exc:
        raise IntakeValidationError("The PDF attachment could not be safely extracted.") from exc
    finally:
        document.close()


async def extract_all(
    request_dir: Path,
    uploads: Sequence[StoredCandidateUpload],
    *,
    settings: AppSettings,
    runtime: ModelRuntime,
) -> ExtractionResult:
    if len(uploads) > MAX_UPLOAD_COUNT:
        raise IntakeValidationError("A message may contain at most 8 attachments.")

    extracted_parts: list[str] = []
    attachment_types: list[AttachmentType] = []
    for index, upload in enumerate(uploads):
        if upload.attachment_type == "text":
            extracted = _decode_text(upload.path)
        elif upload.attachment_type == "pdf":
            extracted = await _extract_pdf(upload.path, request_dir, index)
        else:
            normalized_path = request_dir / f"normalized-{index}.wav"
            try:
                await normalize_audio(upload.path, normalized_path)
                model_paths = settings.load_model_paths()
                extracted = await transcribe_audio(
                    normalized_path,
                    asr_python_path=settings.repository_root
                    / "environments"
                    / "asr"
                    / ".venv"
                    / "bin"
                    / "python",
                    whisper_model_path=model_paths.whisper,
                    metal_operation=runtime.metal_operation,
                )
            except AudioValidationError as exc:
                raise IntakeValidationError(str(exc)) from exc
            except AudioDependencyError as exc:
                raise IntakeDependencyError(str(exc)) from exc
            except AudioProcessingError as exc:
                raise IntakeProcessingError(str(exc)) from exc
            finally:
                normalized_path.unlink(missing_ok=True)

        if extracted:
            extracted_parts.append(extracted)
        attachment_types.append(upload.attachment_type)
        _check_text_limit("\n\n".join(extracted_parts))
    return ExtractionResult(
        extracted_text="\n\n".join(extracted_parts),
        attachment_types=tuple(attachment_types),
    )


async def ingest_candidate_message(
    request_id: str,
    uploads: Sequence[StoredCandidateUpload],
    *,
    request_dir: Path,
    user_text: str,
    settings: AppSettings,
    runtime: ModelRuntime,
    record_attempt: Callable[[ProfileAttemptDiagnostics], None] | None = None,
) -> CandidateMessageIntake:
    """Extract locally stored request artifacts and draft the visible profile."""

    extraction = await extract_all(
        request_dir,
        uploads,
        settings=settings,
        runtime=runtime,
    )
    _check_text_limit(user_text + extraction.extracted_text)
    try:
        profile = await draft_candidate_profile(
            runtime,
            request_id=request_id,
            user_text=user_text,
            extracted_text=extraction.extracted_text,
            record_attempt=record_attempt,
        )
    except ProfileGenerationError as exc:
        raise IntakeProcessingError(str(exc)) from exc
    return CandidateMessageIntake(
        extracted_text=extraction.extracted_text,
        attachment_types=extraction.attachment_types,
        profile=profile,
    )
