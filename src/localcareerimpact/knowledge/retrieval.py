"""Exact sparse-20/dense-20/RRF-60 hybrid retrieval."""

from __future__ import annotations

import asyncio
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Sequence
from urllib.parse import quote

from .embeddings import EmbeddingClient
from .repository import KnowledgeRepository, StoredKnowledgeChunk


SPARSE_LIMIT = 20
DENSE_LIMIT = 20
FINAL_LIMIT = 8
RRF_K = 60


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    document_ids: frozenset[str] = field(default_factory=frozenset)
    source_labels: frozenset[str] = field(default_factory=frozenset)
    source_types: frozenset[str] = field(default_factory=frozenset)
    languages: frozenset[str] = field(default_factory=frozenset)

    def accepts(self, chunk: StoredKnowledgeChunk) -> bool:
        return (
            (not self.document_ids or chunk.document_id in self.document_ids)
            and (not self.source_labels or chunk.source_label in self.source_labels)
            and (not self.source_types or chunk.source_type in self.source_types)
            and (not self.languages or chunk.language in self.languages)
        )


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    rank: int
    rrf_score: float
    sparse_rank: int | None
    dense_rank: int | None
    chunk: StoredKnowledgeChunk

    @property
    def citation_path(self) -> str:
        return (
            "/knowledge?snapshot="
            f"{quote(self.chunk.snapshot_id, safe='')}"
            "&chunk="
            f"{quote(self.chunk.chunk_id, safe='')}"
        )


def reciprocal_rank_fusion(
    sparse_ids: Sequence[str], dense_ids: Sequence[str], k: int = 60
) -> list[str]:
    scores: dict[str, float] = defaultdict(float)
    for ranking in (sparse_ids, dense_ids):
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] += 1.0 / (k + rank)
    return sorted(scores, key=lambda item: (-scores[item], item))


class HybridRetriever:
    def __init__(
        self, repository: KnowledgeRepository, embedding_client: EmbeddingClient
    ) -> None:
        self._repository = repository
        self._embedding_client = embedding_client

    async def retrieve(
        self,
        query: str,
        *,
        snapshot_id: str | None = None,
        run_id: str | None = None,
        scope: RetrievalScope | None = None,
    ) -> tuple[RetrievedChunk, ...]:
        normalized_query = " ".join(query.split())
        if not normalized_query:
            raise ValueError("A non-empty retrieval query is required.")
        if len(normalized_query) > 2_000:
            raise ValueError("The retrieval query is too long.")
        if snapshot_id is not None and run_id is not None:
            raise ValueError("Choose either an explicit snapshot or a run binding.")
        if run_id is not None:
            selected_snapshot = await asyncio.to_thread(
                self._repository.bind_run_to_active_snapshot, run_id
            )
        elif snapshot_id is not None:
            selected_snapshot = snapshot_id
        else:
            selected_snapshot = await asyncio.to_thread(
                self._repository.active_snapshot_id
            )

        embedding_spec = await asyncio.to_thread(
            self._repository.snapshot_embedding_spec, selected_snapshot
        )
        chunks = await asyncio.to_thread(
            self._repository.snapshot_chunks, selected_snapshot
        )
        chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        sparse_query = _fts_query(normalized_query)
        sparse_ids = await asyncio.to_thread(
            self._repository.sparse_chunk_ids,
            selected_snapshot,
            sparse_query,
            SPARSE_LIMIT,
        )
        query_result = await self._embedding_client.embed((normalized_query,))
        if query_result.model_fingerprint != embedding_spec.model_fingerprint:
            raise KnowledgeEmbeddingMismatchError(
                "The active embedding model does not match the knowledge snapshot."
            )
        query_vector = query_result.vectors[0]
        if query_vector.dimension != embedding_spec.dimension:
            raise KnowledgeEmbeddingMismatchError(
                "The embedding dimension does not match the knowledge snapshot."
            )
        dense_ids = await asyncio.to_thread(
            _dense_chunk_ids,
            query_vector.values(),
            embedding_spec.dimension,
            chunks,
        )

        fused_ids = reciprocal_rank_fusion(sparse_ids, dense_ids, k=RRF_K)
        sparse_ranks = {chunk_id: rank for rank, chunk_id in enumerate(sparse_ids, start=1)}
        dense_ranks = {chunk_id: rank for rank, chunk_id in enumerate(dense_ids, start=1)}
        selected_scope = scope or RetrievalScope()
        seen_hashes: set[str] = set()
        selected: list[RetrievedChunk] = []
        for chunk_id in fused_ids:
            chunk = chunks_by_id.get(chunk_id)
            if chunk is None or not selected_scope.accepts(chunk):
                continue
            if chunk.chunk_hash in seen_hashes:
                continue
            seen_hashes.add(chunk.chunk_hash)
            sparse_rank = sparse_ranks.get(chunk_id)
            dense_rank = dense_ranks.get(chunk_id)
            score = sum(
                1.0 / (RRF_K + rank)
                for rank in (sparse_rank, dense_rank)
                if rank is not None
            )
            selected.append(
                RetrievedChunk(
                    rank=len(selected) + 1,
                    rrf_score=score,
                    sparse_rank=sparse_rank,
                    dense_rank=dense_rank,
                    chunk=chunk,
                )
            )
            if len(selected) == FINAL_LIMIT:
                break
        return tuple(selected)


def _fts_query(text: str) -> str:
    tokens = re.findall(r"[\w]+", text, flags=re.UNICODE)
    if not tokens:
        raise ValueError("The retrieval query contains no searchable terms.")
    unique_tokens = list(dict.fromkeys(tokens))[:64]
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in unique_tokens)


def _embedding_values(chunk: StoredKnowledgeChunk) -> tuple[float, ...]:
    from .embeddings import EmbeddingVector

    return EmbeddingVector(
        blob=chunk.embedding_blob, dimension=chunk.embedding_dimension
    ).values()


class KnowledgeEmbeddingMismatchError(RuntimeError):
    """Raised instead of returning arbitrary evidence for incompatible vectors."""


def _dense_chunk_ids(
    query_values: Sequence[float],
    expected_dimension: int,
    chunks: Sequence[StoredKnowledgeChunk],
) -> tuple[str, ...]:
    if len(query_values) != expected_dimension:
        raise KnowledgeEmbeddingMismatchError(
            "The embedding dimension does not match the knowledge snapshot."
        )
    scored: list[tuple[float, str]] = []
    for chunk in chunks:
        if chunk.embedding_dimension != expected_dimension:
            raise KnowledgeEmbeddingMismatchError(
                "The knowledge snapshot contains incompatible embeddings."
            )
        values = _embedding_values(chunk)
        if len(values) != expected_dimension:
            raise KnowledgeEmbeddingMismatchError(
                "The knowledge snapshot contains invalid embedding data."
            )
        score = _cosine_similarity(query_values, values)
        if not math.isfinite(score):
            raise KnowledgeEmbeddingMismatchError(
                "The knowledge snapshot contains invalid embedding data."
            )
        scored.append((score, chunk.chunk_id))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return tuple(chunk_id for _score, chunk_id in scored[:DENSE_LIMIT])


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise KnowledgeEmbeddingMismatchError(
            "The embedding dimension does not match the knowledge snapshot."
        )
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        raise KnowledgeEmbeddingMismatchError(
            "The knowledge snapshot contains invalid embedding data."
        )
    return dot / (left_norm * right_norm)


__all__ = [
    "DENSE_LIMIT",
    "FINAL_LIMIT",
    "RRF_K",
    "SPARSE_LIMIT",
    "HybridRetriever",
    "KnowledgeEmbeddingMismatchError",
    "RetrievalScope",
    "RetrievedChunk",
    "reciprocal_rank_fusion",
]
