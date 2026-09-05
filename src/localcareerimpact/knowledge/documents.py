"""Bounded, deterministic loading for retained local knowledge sources."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Literal


KnowledgeSourceType = Literal["pdf", "txt", "csv", "json"]

PDF_MAX_BYTES = 10 * 1024 * 1024
TEXT_MAX_BYTES = 2 * 1024 * 1024
STRUCTURED_MAX_BYTES = 10 * 1024 * 1024
PDF_MAX_PAGES = 30
MAX_EXTRACTED_CODEPOINTS = 2_000_000
CSV_MAX_ROWS = 20_000
CSV_MAX_COLUMNS = 200
JSON_MAX_TOP_LEVEL_ITEMS = 20_000
MAX_RETAINED_FILENAME_BYTES = 240

_POLICIES: dict[str, tuple[KnowledgeSourceType, int]] = {
    ".pdf": ("pdf", PDF_MAX_BYTES),
    ".txt": ("txt", TEXT_MAX_BYTES),
    ".csv": ("csv", STRUCTURED_MAX_BYTES),
    ".json": ("json", STRUCTURED_MAX_BYTES),
}
_UPLOAD_MEDIA_TYPES: dict[str, frozenset[str]] = {
    ".pdf": frozenset({"application/pdf"}),
    ".txt": frozenset({"text/plain"}),
    ".csv": frozenset({"text/csv", "application/csv", "application/vnd.ms-excel"}),
    ".json": frozenset({"application/json", "text/json"}),
}
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class KnowledgeDocumentError(ValueError):
    """Raised when one retained source cannot enter a snapshot."""

    def __init__(self, source_label: str, message: str) -> None:
        super().__init__(message)
        self.source_label = source_label


@dataclass(frozen=True, slots=True)
class DocumentSection:
    locator: str
    text: str


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    document_id: str
    filename: str
    content_hash: str
    title: str
    source_label: str
    source_type: KnowledgeSourceType
    language: str
    size_bytes: int
    modified_at: str
    sections: tuple[DocumentSection, ...]


def discover_knowledge_files(inbox: Path) -> tuple[Path, ...]:
    """Return a stable inventory; hidden upload staging files are ignored."""

    inbox.mkdir(parents=True, exist_ok=True)
    return tuple(
        sorted(
            (
                path
                for path in inbox.rglob("*")
                if (path.is_file() or path.is_symlink())
                and not any(part.startswith(".") for part in path.relative_to(inbox).parts)
            ),
            key=lambda path: path.relative_to(inbox).as_posix(),
        )
    )


def source_label_for(path: Path, inbox: Path) -> str:
    try:
        return path.relative_to(inbox).as_posix()
    except ValueError:
        return path.name


def source_type_for_name(filename: str) -> KnowledgeSourceType:
    extension = Path(filename).suffix.lower()
    policy = _POLICIES.get(extension)
    if policy is None:
        raise KnowledgeDocumentError(
            filename,
            "Knowledge sources must be PDF, UTF-8 TXT, CSV, or JSON files.",
        )
    return policy[0]


def load_document(path: Path, inbox: Path) -> KnowledgeDocument:
    source_label = source_label_for(path, inbox)
    if path.is_symlink():
        raise KnowledgeDocumentError(source_label, "Symbolic-link knowledge sources are not accepted.")
    extension = path.suffix.lower()
    policy = _POLICIES.get(extension)
    if policy is None:
        raise KnowledgeDocumentError(
            source_label,
            "Knowledge sources must be PDF, UTF-8 TXT, CSV, or JSON files.",
        )
    source_type, maximum_bytes = policy
    try:
        with path.open("rb") as handle:
            raw = handle.read(maximum_bytes + 1)
            stat = os.fstat(handle.fileno())
    except OSError as exc:
        raise KnowledgeDocumentError(source_label, "The knowledge source could not be read.") from exc
    if not raw:
        raise KnowledgeDocumentError(source_label, "Knowledge sources must not be empty.")
    if len(raw) > maximum_bytes:
        raise KnowledgeDocumentError(source_label, "The knowledge source exceeds its size limit.")

    if source_type == "pdf":
        sections = _pdf_sections(raw, source_label)
    else:
        text = _decode_utf8(raw, source_label, source_type)
        if source_type == "txt":
            sections = (DocumentSection(locator="section:document", text=text.strip()),)
        elif source_type == "csv":
            sections = _csv_sections(text, source_label)
        else:
            sections = _json_sections(text, source_label)

    sections = tuple(section for section in sections if section.text.strip())
    total_codepoints = sum(len(section.text) for section in sections)
    if not sections:
        raise KnowledgeDocumentError(source_label, "The knowledge source contains no extractable text.")
    if total_codepoints > MAX_EXTRACTED_CODEPOINTS:
        raise KnowledgeDocumentError(source_label, "The extracted knowledge text is too large.")

    hexadecimal_hash = hashlib.sha256(raw).hexdigest()
    content_hash = f"sha256:{hexadecimal_hash}"
    combined_text = "\n".join(section.text for section in sections)
    modified_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat().replace(
        "+00:00", "Z"
    )
    title = " ".join(path.stem.split())[:200] or path.name[:200]
    return KnowledgeDocument(
        document_id=f"doc-{hexadecimal_hash}",
        filename=path.name,
        content_hash=content_hash,
        title=title,
        source_label=source_label,
        source_type=source_type,
        language=_detect_language(combined_text),
        size_bytes=len(raw),
        modified_at=modified_at,
        sections=sections,
    )


def retain_uploaded_source(
    inbox: Path,
    filename: str,
    media_type: str | None,
    stream: BinaryIO,
) -> Path:
    """Persist a validated admin upload in the knowledge inbox, never candidate temp."""

    try:
        encoded_filename = filename.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise KnowledgeDocumentError("upload", "The knowledge filename is invalid.") from exc
    if (
        not filename
        or Path(filename).name != filename
        or _CONTROL_CHARACTERS.search(filename)
        or len(encoded_filename) > MAX_RETAINED_FILENAME_BYTES
    ):
        raise KnowledgeDocumentError("upload", "The knowledge filename is invalid.")
    extension = Path(filename).suffix.lower()
    policy = _POLICIES.get(extension)
    if policy is None:
        raise KnowledgeDocumentError(
            filename,
            "Knowledge uploads must be PDF, UTF-8 TXT, CSV, or JSON files.",
        )
    normalized_media_type = (media_type or "").strip().lower()
    try:
        normalized_media_type.encode("ascii")
    except UnicodeEncodeError as exc:
        raise KnowledgeDocumentError(filename, "The upload media type is invalid.") from exc
    if normalized_media_type not in _UPLOAD_MEDIA_TYPES[extension]:
        raise KnowledgeDocumentError(filename, "The filename and upload media type do not match.")

    inbox.mkdir(parents=True, exist_ok=True)
    maximum_bytes = policy[1]
    temporary_path: Path | None = None
    destination_created = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=inbox, prefix=".upload-", suffix=".part", delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            digest = hashlib.sha256()
            size = 0
            while block := stream.read(1024 * 1024):
                size += len(block)
                if size > maximum_bytes:
                    raise KnowledgeDocumentError(filename, "The knowledge upload exceeds its size limit.")
                digest.update(block)
                handle.write(block)
            handle.flush()
            os.fsync(handle.fileno())
        if size == 0:
            raise KnowledgeDocumentError(filename, "Knowledge uploads must not be empty.")

        content_digest = digest.hexdigest()
        original_destination = inbox / filename
        digest_suffix = f"-{content_digest}{extension}"
        stem_byte_limit = MAX_RETAINED_FILENAME_BYTES - len(
            digest_suffix.encode("utf-8")
        )
        bounded_stem = _truncate_utf8(original_destination.stem, stem_byte_limit)
        generated_destination = inbox / (
            f"{bounded_stem}{digest_suffix}"
        )
        for destination in (original_destination, generated_destination):
            try:
                # Both paths are in the retained inbox. A hard-link claim is atomic and
                # never replaces an existing file, unlike POSIX rename/replace.
                os.link(temporary_path, destination)
            except FileExistsError:
                if destination.is_symlink() or not destination.is_file():
                    raise KnowledgeDocumentError(
                        filename, "The upload destination is not a regular file."
                    )
                if _file_sha256(destination) == content_digest:
                    temporary_path.unlink(missing_ok=True)
                    temporary_path = None
                    load_document(destination, inbox)
                    return destination
                if destination == original_destination:
                    continue
                raise KnowledgeDocumentError(
                    filename, "A retained source has the same generated filename."
                )
            destination_created = True
            temporary_path.unlink()
            temporary_path = None
            load_document(destination, inbox)
            return destination
        raise KnowledgeDocumentError(filename, "The knowledge upload could not be retained.")
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        if destination_created:
            destination.unlink(missing_ok=True)
        raise


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _truncate_utf8(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    return encoded[:maximum_bytes].decode("utf-8", errors="ignore")


def _decode_utf8(raw: bytes, source_label: str, source_type: str) -> str:
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise KnowledgeDocumentError(
            source_label,
            f"{source_type.upper()} knowledge sources must use valid UTF-8 encoding.",
        ) from exc


def _pdf_sections(raw: bytes, source_label: str) -> tuple[DocumentSection, ...]:
    if raw[:5] != b"%PDF-":
        raise KnowledgeDocumentError(source_label, "The PDF knowledge source is not a valid PDF.")
    try:
        import pymupdf
    except ImportError as exc:
        raise KnowledgeDocumentError(source_label, "The local PDF extractor is unavailable.") from exc
    try:
        document = pymupdf.open(stream=raw, filetype="pdf")
    except Exception as exc:
        raise KnowledgeDocumentError(source_label, "The PDF knowledge source could not be opened.") from exc
    try:
        if document.needs_pass:
            raise KnowledgeDocumentError(source_label, "Password-protected PDFs are not accepted.")
        if document.page_count < 1 or document.page_count > PDF_MAX_PAGES:
            raise KnowledgeDocumentError(source_label, "PDF knowledge sources must contain 1 to 30 pages.")
        return tuple(
            DocumentSection(
                locator=f"page:{page_number}",
                text=page.get_text("text", sort=True).strip(),
            )
            for page_number, page in enumerate(document, start=1)
        )
    except KnowledgeDocumentError:
        raise
    except Exception as exc:
        raise KnowledgeDocumentError(
            source_label, "The PDF knowledge source could not be safely extracted."
        ) from exc
    finally:
        document.close()


def _csv_sections(text: str, source_label: str) -> tuple[DocumentSection, ...]:
    try:
        rows = csv.reader(io.StringIO(text, newline=""), strict=True)
        collected: list[DocumentSection] = []
        headers: list[str] | None = None
        for row_number, row in enumerate(rows, start=1):
            if row_number > CSV_MAX_ROWS:
                raise KnowledgeDocumentError(source_label, "CSV knowledge sources have too many rows.")
            if len(row) > CSV_MAX_COLUMNS:
                raise KnowledgeDocumentError(source_label, "CSV knowledge sources have too many columns.")
            normalized = [" ".join(cell.split()) for cell in row]
            if headers is None:
                headers = [value or f"column_{index}" for index, value in enumerate(normalized, start=1)]
                if len(set(headers)) != len(headers):
                    headers = [f"column_{index}" for index in range(1, len(normalized) + 1)]
            pairs = [
                f"{headers[index] if index < len(headers) else f'column_{index + 1}'}: {value}"
                for index, value in enumerate(normalized)
                if value
            ]
            if pairs:
                collected.append(
                    DocumentSection(locator=f"row:{row_number}", text=" | ".join(pairs))
                )
        return tuple(collected)
    except csv.Error as exc:
        raise KnowledgeDocumentError(source_label, "The CSV knowledge source is malformed.") from exc


def _json_sections(text: str, source_label: str) -> tuple[DocumentSection, ...]:
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            if len(value) > JSON_MAX_TOP_LEVEL_ITEMS:
                raise KnowledgeDocumentError(
                    source_label, "The JSON knowledge source has too many sections."
                )
            return tuple(
                DocumentSection(
                    locator=f"section:{key}",
                    text=f"{key}: {_canonical_json_text(item)}",
                )
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            )
        if isinstance(value, list):
            if len(value) > JSON_MAX_TOP_LEVEL_ITEMS:
                raise KnowledgeDocumentError(
                    source_label, "The JSON knowledge source has too many rows."
                )
            return tuple(
                DocumentSection(locator=f"row:{index}", text=_canonical_json_text(item))
                for index, item in enumerate(value, start=1)
            )
        return (
            DocumentSection(locator="section:root", text=_canonical_json_text(value)),
        )
    except KnowledgeDocumentError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise KnowledgeDocumentError(
            source_label, "The JSON knowledge source is malformed."
        ) from exc


def _canonical_json_text(value: object) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _detect_language(text: str) -> str:
    cjk = sum("\u3400" <= character <= "\u9fff" for character in text)
    latin = sum(character.isascii() and character.isalpha() for character in text)
    if cjk == 0 and latin == 0:
        return "unknown"
    return "zh" if cjk * 2 >= latin else "en"


__all__ = [
    "DocumentSection",
    "KnowledgeDocument",
    "KnowledgeDocumentError",
    "KnowledgeSourceType",
    "discover_knowledge_files",
    "load_document",
    "retain_uploaded_source",
    "source_label_for",
    "source_type_for_name",
]
