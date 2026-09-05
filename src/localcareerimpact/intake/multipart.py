"""Incremental, directly-to-request-directory multipart candidate intake."""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

from starlette.requests import Request

from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header

from .limits import (
    AUDIO_MAX_BYTES,
    MAX_UPLOAD_COUNT,
    MULTIPART_MAX_BOUNDARY_BYTES,
    MULTIPART_MAX_HEADER_BYTES_PER_PART,
    MULTIPART_MAX_HEADERS_PER_PART,
    MULTIPART_MAX_OVERHEAD_BYTES,
    MULTIPART_MAX_PARTS,
    MULTIPART_MAX_TEXT_BYTES,
    MULTIPART_MAX_TOTAL_BYTES,
    PDF_MAX_BYTES,
    TEXT_MAX_CODEPOINTS,
    TXT_MAX_BYTES,
    UPLOAD_CHUNK_BYTES,
)


AttachmentType = Literal["pdf", "text", "audio"]


class IntakeValidationError(ValueError):
    """Raised for unsupported, malformed, or over-limit candidate input."""


@dataclass(frozen=True, slots=True)
class UploadPolicy:
    attachment_type: AttachmentType
    extension: str
    maximum_bytes: int


@dataclass(frozen=True, slots=True)
class StoredCandidateUpload:
    path: Path
    attachment_type: AttachmentType
    extension: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ParsedCandidateMultipart:
    request_dir: Path
    text: str
    uploads: tuple[StoredCandidateUpload, ...]


_VALID_UPLOAD_PAIRS: dict[tuple[str, str], UploadPolicy] = {}


def _register(
    attachment_type: AttachmentType,
    extension: str,
    maximum_bytes: int,
    *media_types: str,
) -> None:
    for media_type in media_types:
        _VALID_UPLOAD_PAIRS[(extension, media_type)] = UploadPolicy(
            attachment_type=attachment_type,
            extension=extension,
            maximum_bytes=maximum_bytes,
        )


_register("pdf", ".pdf", PDF_MAX_BYTES, "application/pdf")
_register("text", ".txt", TXT_MAX_BYTES, "text/plain")
_register(
    "audio",
    ".wav",
    AUDIO_MAX_BYTES,
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
    "audio/vnd.wave",
)
_register("audio", ".mp3", AUDIO_MAX_BYTES, "audio/mpeg", "audio/mp3")
_register("audio", ".mpeg", AUDIO_MAX_BYTES, "audio/mpeg")
_register("audio", ".m4a", AUDIO_MAX_BYTES, "audio/mp4", "audio/x-m4a")
_register("audio", ".mp4", AUDIO_MAX_BYTES, "audio/mp4")
_register("audio", ".webm", AUDIO_MAX_BYTES, "audio/webm", "video/webm")
_register("audio", ".ogg", AUDIO_MAX_BYTES, "audio/ogg", "application/ogg")
_register("audio", ".oga", AUDIO_MAX_BYTES, "audio/ogg", "application/ogg")
_register("audio", ".opus", AUDIO_MAX_BYTES, "audio/ogg", "audio/opus")
_register("audio", ".flac", AUDIO_MAX_BYTES, "audio/flac", "audio/x-flac")
_register("audio", ".aac", AUDIO_MAX_BYTES, "audio/aac")


class _CandidateMultipartReceiver:
    def __init__(self, request_dir: Path) -> None:
        self.request_dir = request_dir
        self.uploads: list[StoredCandidateUpload] = []
        self.text_bytes: bytes | None = None
        self.complete = False
        self.part_count = 0
        self.payload_bytes = 0
        self._headers: dict[bytes, bytes] = {}
        self._header_name = bytearray()
        self._header_value = bytearray()
        self._header_bytes = 0
        self._policy: UploadPolicy | None = None
        self._file: BinaryIO | None = None
        self._file_path: Path | None = None
        self._part_size = 0
        self._text = bytearray()

    def callbacks(self) -> dict[str, object]:
        return {
            "on_part_begin": self.on_part_begin,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_end": self.on_end,
        }

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def on_part_begin(self) -> None:
        self.part_count += 1
        if self.part_count > MULTIPART_MAX_PARTS:
            raise IntakeValidationError(
                "A message may contain at most 8 attachments and one text field."
            )
        self._headers = {}
        self._header_name.clear()
        self._header_value.clear()
        self._header_bytes = 0
        self._policy = None
        self._file_path = None
        self._part_size = 0
        self._text.clear()

    def _add_header_bytes(self, amount: int) -> None:
        self._header_bytes += amount
        if self._header_bytes > MULTIPART_MAX_HEADER_BYTES_PER_PART:
            raise IntakeValidationError("A multipart part contains oversized headers.")

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        piece = data[start:end]
        self._add_header_bytes(len(piece))
        self._header_name.extend(piece)

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        piece = data[start:end]
        self._add_header_bytes(len(piece))
        self._header_value.extend(piece)

    def on_header_end(self) -> None:
        if len(self._headers) >= MULTIPART_MAX_HEADERS_PER_PART:
            raise IntakeValidationError("A multipart part contains too many headers.")
        name = bytes(self._header_name).lower()
        if not name or name in self._headers:
            raise IntakeValidationError("A multipart part contains invalid headers.")
        self._headers[name] = bytes(self._header_value)
        self._header_name.clear()
        self._header_value.clear()

    def on_headers_finished(self) -> None:
        disposition = self._headers.get(b"content-disposition")
        if disposition is None:
            raise IntakeValidationError("Each multipart part needs Content-Disposition.")
        disposition_type, options = parse_options_header(disposition)
        if disposition_type != b"form-data" or b"name" not in options:
            raise IntakeValidationError("A multipart part has invalid Content-Disposition.")
        field_name = options[b"name"]
        is_file = b"filename" in options
        if is_file:
            if field_name != b"files" or len(self.uploads) >= MAX_UPLOAD_COUNT:
                raise IntakeValidationError(
                    "A message may contain at most 8 candidate attachments."
                )
            try:
                raw_filename = options[b"filename"].decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise IntakeValidationError("An attachment filename is invalid.") from exc
            extension = Path(raw_filename).suffix.lower()
            content_type = self._headers.get(b"content-type", b"")
            try:
                media_type = (
                    content_type.partition(b";")[0]
                    .strip()
                    .lower()
                    .decode("ascii", errors="strict")
                )
            except UnicodeDecodeError as exc:
                raise IntakeValidationError(
                    "Unsupported attachment type or mismatched file extension and media type."
                ) from exc
            policy = _VALID_UPLOAD_PAIRS.get((extension, media_type))
            if policy is None:
                raise IntakeValidationError(
                    "Unsupported attachment type or mismatched file extension and media type."
                )
            self._policy = policy
            self._file_path = self.request_dir / f"candidate-{len(self.uploads)}{policy.extension}"
            self._file = self._file_path.open("xb")
            self._headers = {}
            return

        if field_name != b"text" or self.text_bytes is not None:
            raise IntakeValidationError(
                "Messages accept one text field and up to 8 candidate attachments."
            )
        content_type = self._headers.get(b"content-type")
        if (
            content_type is not None
            and content_type.partition(b";")[0].strip().lower() != b"text/plain"
        ):
            raise IntakeValidationError("The message text part must be plain text.")
        self._headers = {}

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        piece = memoryview(data)[start:end]
        amount = len(piece)
        self.payload_bytes += amount
        self._part_size += amount
        if self._policy is not None:
            if self._part_size > self._policy.maximum_bytes:
                raise IntakeValidationError(
                    f"{self._policy.attachment_type.capitalize()} attachment exceeds "
                    "its size limit."
                )
            if self._file is None:
                raise IntakeValidationError("The attachment stream is invalid.")
            self._file.write(piece)
            return
        if self._part_size > MULTIPART_MAX_TEXT_BYTES:
            raise IntakeValidationError("Message text must not exceed 30,000 Unicode code points.")
        self._text.extend(piece)

    def on_part_end(self) -> None:
        if self._policy is not None:
            if self._file is None or self._file_path is None:
                raise IntakeValidationError("The attachment stream is invalid.")
            self._file.close()
            self._file = None
            if self._part_size == 0:
                self._file_path.unlink(missing_ok=True)
                raise IntakeValidationError("Attachments must not be empty.")
            self.uploads.append(
                StoredCandidateUpload(
                    path=self._file_path,
                    attachment_type=self._policy.attachment_type,
                    extension=self._policy.extension,
                    size_bytes=self._part_size,
                )
            )
            return
        if self.text_bytes is not None:
            raise IntakeValidationError("Messages accept only one text field.")
        self.text_bytes = bytes(self._text)

    def on_end(self) -> None:
        self.complete = True


def _multipart_boundary(request: Request) -> bytes:
    content_type = request.headers.get("content-type", "")
    media_type, options = parse_options_header(content_type)
    boundary = options.get(b"boundary")
    if media_type != b"multipart/form-data" or not boundary:
        raise IntakeValidationError("Messages require multipart form data with a boundary.")
    if len(boundary) > MULTIPART_MAX_BOUNDARY_BYTES:
        raise IntakeValidationError("The multipart boundary is too long.")
    return boundary


@asynccontextmanager
async def stream_candidate_multipart(
    request: Request,
    *,
    request_id: str,
    upload_root: Path,
) -> AsyncIterator[ParsedCandidateMultipart]:
    """Parse one request incrementally and remove its directory on every exit path."""

    request_dir = upload_root / request_id
    receiver = _CandidateMultipartReceiver(request_dir)
    request_dir.mkdir(parents=True, exist_ok=False)
    try:
        boundary = _multipart_boundary(request)
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise IntakeValidationError("Content-Length is invalid.") from exc
            if declared_length < 0 or declared_length > MULTIPART_MAX_TOTAL_BYTES:
                raise IntakeValidationError("The multipart request exceeds its total size limit.")

        parser = MultipartParser(boundary, receiver.callbacks())
        total_received = 0
        async for request_chunk in request.stream():
            total_received += len(request_chunk)
            if total_received > MULTIPART_MAX_TOTAL_BYTES:
                raise IntakeValidationError("The multipart request exceeds its total size limit.")
            for offset in range(0, len(request_chunk), UPLOAD_CHUNK_BYTES):
                parser.write(request_chunk[offset : offset + UPLOAD_CHUNK_BYTES])
            if total_received - receiver.payload_bytes > MULTIPART_MAX_OVERHEAD_BYTES:
                raise IntakeValidationError("The multipart request contains excessive overhead.")
        parser.finalize()
        if not receiver.complete:
            raise IntakeValidationError("The multipart request is incomplete.")
        try:
            text = (receiver.text_bytes or b"").decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise IntakeValidationError("Message text must use valid UTF-8 encoding.") from exc
        if len(text) > TEXT_MAX_CODEPOINTS:
            raise IntakeValidationError("Message text must not exceed 30,000 Unicode code points.")
        yield ParsedCandidateMultipart(
            request_dir=request_dir,
            text=text,
            uploads=tuple(receiver.uploads),
        )
    except MultipartParseError as exc:
        raise IntakeValidationError("The multipart request is malformed.") from exc
    finally:
        receiver.close()
        shutil.rmtree(request_dir, ignore_errors=True)
