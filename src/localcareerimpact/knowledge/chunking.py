"""Deterministic locator-preserving knowledge chunking."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .documents import KnowledgeDocument, KnowledgeSourceType


CHUNK_MAX_CODEPOINTS = 900
CHUNK_OVERLAP_CODEPOINTS = 120
MAX_CHUNKS_PER_SNAPSHOT = 20_000


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    chunk_id: str
    document_id: str
    snapshot_id: str
    chunk_index: int
    locator: str
    title: str
    source_label: str
    source_type: KnowledgeSourceType
    text: str
    language: str
    content_hash: str
    chunk_hash: str


def chunk_document(
    document: KnowledgeDocument,
    snapshot_id: str,
    *,
    maximum_codepoints: int = CHUNK_MAX_CODEPOINTS,
    overlap_codepoints: int = CHUNK_OVERLAP_CODEPOINTS,
) -> tuple[KnowledgeChunk, ...]:
    if maximum_codepoints < 100:
        raise ValueError("maximum_codepoints must be at least 100")
    if overlap_codepoints < 0 or overlap_codepoints >= maximum_codepoints:
        raise ValueError("overlap_codepoints must be non-negative and smaller than the chunk")

    chunks: list[KnowledgeChunk] = []
    for section in document.sections:
        pieces = _split_text(section.text, maximum_codepoints, overlap_codepoints)
        for part_number, text in enumerate(pieces, start=1):
            locator = section.locator
            if len(pieces) > 1:
                locator = f"{locator}#part={part_number}"
            chunk_index = len(chunks)
            chunk_hash = f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"
            identity = json.dumps(
                {
                    "document_id": document.document_id,
                    "locator": locator,
                    "text": text,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            chunk_id = f"chunk-{hashlib.sha256(identity).hexdigest()}"
            chunks.append(
                KnowledgeChunk(
                    chunk_id=chunk_id,
                    document_id=document.document_id,
                    snapshot_id=snapshot_id,
                    chunk_index=chunk_index,
                    locator=locator,
                    title=document.title,
                    source_label=document.source_label,
                    source_type=document.source_type,
                    text=text,
                    language=document.language,
                    content_hash=document.content_hash,
                    chunk_hash=chunk_hash,
                )
            )
    return tuple(chunks)


def _split_text(text: str, maximum: int, overlap: int) -> tuple[str, ...]:
    normalized = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))
    normalized = normalized.strip()
    if not normalized:
        return ()
    if len(normalized) <= maximum:
        return (normalized,)

    pieces: list[str] = []
    start = 0
    while start < len(normalized):
        hard_end = min(start + maximum, len(normalized))
        end = hard_end
        if hard_end < len(normalized):
            minimum_break = start + maximum // 2
            candidates = (
                normalized.rfind("\n\n", minimum_break, hard_end),
                normalized.rfind("\n", minimum_break, hard_end),
                normalized.rfind(". ", minimum_break, hard_end),
                normalized.rfind("。", minimum_break, hard_end),
                normalized.rfind(" ", minimum_break, hard_end),
            )
            selected = max(candidates)
            if selected >= minimum_break:
                end = selected + (1 if normalized[selected] in {"。", "\n"} else 0)
        piece = normalized[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(normalized):
            break
        next_start = max(start + 1, end - overlap)
        while next_start < end and normalized[next_start].isspace():
            next_start += 1
        start = next_start
    return tuple(pieces)


__all__ = [
    "CHUNK_MAX_CODEPOINTS",
    "CHUNK_OVERLAP_CODEPOINTS",
    "MAX_CHUNKS_PER_SNAPSHOT",
    "KnowledgeChunk",
    "chunk_document",
]
