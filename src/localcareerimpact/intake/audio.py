"""Offline-only audio validation, normalization, and local Whisper ASR."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .limits import (
    ASR_MAX_STDOUT_BYTES,
    ASR_TIMEOUT_SECONDS,
    AUDIO_MAX_DURATION_SECONDS,
    AUDIO_TOOL_TIMEOUT_SECONDS,
    LOCAL_TOOL_MAX_STDERR_BYTES,
    LOCAL_TOOL_MAX_STDOUT_BYTES,
    NORMALIZED_AUDIO_MAX_BYTES,
)


FFMPEG_PATH = Path("/opt/homebrew/bin/ffmpeg")
FFPROBE_PATH = Path("/opt/homebrew/bin/ffprobe")
ASR_RESULT_PREFIX = "LOCALCAREERIMPACT_ASR_RESULT="


class AudioValidationError(ValueError):
    """Raised when an uploaded audio file is invalid or outside policy."""


class AudioDependencyError(RuntimeError):
    """Raised when a required local audio dependency is unavailable."""


class AudioProcessingError(RuntimeError):
    """Raised when a valid local audio operation cannot complete."""


async def _run_process(
    *args: str,
    timeout_seconds: float,
    environment: dict[str, str] | None = None,
    stdout_limit: int = LOCAL_TOOL_MAX_STDOUT_BYTES,
    output_path: Path | None = None,
    output_byte_limit: int | None = None,
) -> tuple[bytes, bytes, int]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
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

    async def monitor_output() -> bool:
        if output_path is None or output_byte_limit is None:
            return False
        while process.returncode is None:
            try:
                if output_path.stat().st_size > output_byte_limit:
                    return True
            except FileNotFoundError:
                pass
            await asyncio.sleep(0.025)
        try:
            return output_path.stat().st_size > output_byte_limit
        except FileNotFoundError:
            return False

    stdout_task = asyncio.create_task(read_bounded(process.stdout, stdout_limit))
    stderr_task = asyncio.create_task(
        read_bounded(process.stderr, LOCAL_TOOL_MAX_STDERR_BYTES)
    )
    wait_task = asyncio.create_task(process.wait())
    process_completion = asyncio.gather(stdout_task, stderr_task, wait_task)
    output_monitor = asyncio.create_task(monitor_output())

    async def supervise() -> tuple[bytes, bytes, int, bool, bool, bool]:
        done, _pending = await asyncio.wait(
            {process_completion, output_monitor},
            return_when=asyncio.FIRST_COMPLETED,
        )
        output_exceeded = False
        if output_monitor in done:
            output_exceeded = output_monitor.result()
            if output_exceeded and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        (stdout, stdout_exceeded), (stderr, stderr_exceeded), returncode = (
            await process_completion
        )
        if not output_monitor.done():
            output_exceeded = await output_monitor
        else:
            output_exceeded = output_exceeded or output_monitor.result()
        return (
            stdout,
            stderr,
            returncode,
            stdout_exceeded,
            stderr_exceeded,
            output_exceeded,
        )

    supervision = asyncio.create_task(supervise())

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
                output_monitor,
                supervision,
                return_exceptions=True,
            )
        )

    try:
        async with asyncio.timeout(timeout_seconds):
            (
                stdout,
                stderr,
                returncode,
                stdout_exceeded,
                stderr_exceeded,
                output_exceeded,
            ) = await asyncio.shield(supervision)
    except asyncio.TimeoutError as exc:
        await kill_reap_and_drain()
        raise AudioProcessingError("Local audio processing timed out.") from exc
    except asyncio.CancelledError:
        await kill_reap_and_drain()
        raise
    except BaseException:
        await kill_reap_and_drain()
        raise
    if output_exceeded:
        raise AudioValidationError("Normalized audio exceeds its decoded size limit.")
    if stdout_exceeded or stderr_exceeded:
        raise AudioProcessingError("A local audio tool returned excessive output.")
    return stdout, stderr, returncode


def _require_audio_tools() -> None:
    if not FFMPEG_PATH.is_file() or not os.access(FFMPEG_PATH, os.X_OK):
        raise AudioDependencyError("The configured local media tool is unavailable.")
    if not FFPROBE_PATH.is_file() or not os.access(FFPROBE_PATH, os.X_OK):
        raise AudioDependencyError("The configured local media probe is unavailable.")


async def _duration_seconds(audio_path: Path) -> Decimal:
    stdout, _stderr, returncode = await _run_process(
        str(FFPROBE_PATH),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(audio_path),
        timeout_seconds=AUDIO_TOOL_TIMEOUT_SECONDS,
    )
    if returncode != 0:
        raise AudioValidationError("The audio file could not be decoded.")
    try:
        payload = json.loads(stdout)
        duration = Decimal(str(payload["format"]["duration"]))
    except (KeyError, TypeError, json.JSONDecodeError, InvalidOperation) as exc:
        raise AudioValidationError("The audio duration could not be determined.") from exc
    if not duration.is_finite() or duration <= 0:
        raise AudioValidationError("The audio duration must be greater than zero.")
    if duration > AUDIO_MAX_DURATION_SECONDS:
        raise AudioValidationError("Audio must not exceed 15 minutes.")
    return duration


async def normalize_audio(source_path: Path, normalized_path: Path) -> None:
    """Create a metadata-free mono 16 kHz PCM WAV after duration validation."""

    _require_audio_tools()
    await _duration_seconds(source_path)
    _stdout, _stderr, returncode = await _run_process(
        str(FFMPEG_PATH),
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(source_path),
        "-t",
        str(AUDIO_MAX_DURATION_SECONDS),
        "-map_metadata",
        "-1",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(normalized_path),
        timeout_seconds=AUDIO_TOOL_TIMEOUT_SECONDS,
        output_path=normalized_path,
        output_byte_limit=NORMALIZED_AUDIO_MAX_BYTES,
    )
    if returncode != 0 or not normalized_path.is_file():
        raise AudioValidationError("The audio file could not be normalized.")
    await _duration_seconds(normalized_path)


_ASR_SCRIPT = r"""
import json
import sys
from pathlib import Path

import mlx_whisper

audio_path = Path(sys.argv[1]).resolve()
model_path = Path(sys.argv[2]).resolve()
if not audio_path.is_file() or not model_path.is_dir():
    raise SystemExit(2)
result = mlx_whisper.transcribe(
    str(audio_path),
    path_or_hf_repo=str(model_path),
    verbose=False,
    word_timestamps=False,
)
text = result.get("text")
if not isinstance(text, str):
    raise SystemExit(3)
print("LOCALCAREERIMPACT_ASR_RESULT=" + json.dumps({"text": text}, ensure_ascii=False))
"""


async def transcribe_audio(
    normalized_path: Path,
    *,
    asr_python_path: Path,
    whisper_model_path: Path,
    metal_operation: Callable[[], AbstractAsyncContextManager[None]],
) -> str:
    """Transcribe one normalized file with configured local weights under Metal lock."""

    if not asr_python_path.is_file() or not os.access(asr_python_path, os.X_OK):
        raise AudioDependencyError("The configured local ASR environment is unavailable.")
    if not whisper_model_path.is_dir():
        raise AudioDependencyError("The configured local Whisper model is unavailable.")

    environment = os.environ.copy()
    environment.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    async with metal_operation():
        stdout, _stderr, returncode = await _run_process(
            str(asr_python_path),
            "-c",
            _ASR_SCRIPT,
            str(normalized_path),
            str(whisper_model_path),
            timeout_seconds=ASR_TIMEOUT_SECONDS,
            environment=environment,
            stdout_limit=ASR_MAX_STDOUT_BYTES,
        )
    if returncode != 0:
        raise AudioProcessingError("Local speech recognition failed.")

    result_line = next(
        (
            line[len(ASR_RESULT_PREFIX) :]
            for line in reversed(stdout.decode("utf-8", errors="replace").splitlines())
            if line.startswith(ASR_RESULT_PREFIX)
        ),
        None,
    )
    if result_line is None:
        raise AudioProcessingError("Local speech recognition returned no result.")
    try:
        payload = json.loads(result_line)
        transcript = payload["text"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise AudioProcessingError("Local speech recognition returned an invalid result.") from exc
    if not isinstance(transcript, str) or not transcript.strip():
        raise AudioValidationError("The audio did not contain transcribable speech.")
    return transcript.strip()
