"""Exact sparse-20/dense-20/RRF-60 hybrid retrieval."""

from __future__ import annotations

import asyncio
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Sequence
from urllib.parse import quote

from .embeddings import EmbeddingClient
from .repository import KnowledgeRepository, StoredKnowledgeChunk


SPARSE_LIMIT = 20
DENSE_LIMIT = 20
FINAL_LIMIT = 8
RRF_K = 60
ANALYSIS_TOPIC_LIMITS = {"occupations": 4, "ai-impact": 2, "labour-market": 2}


def analysis_topic(source_label: str) -> str:
    """Operator-assigned directory names, not inferred authority or relevance."""

    return next((part for part in PurePosixPath(source_label).parts[:-1]
                 if part in ANALYSIS_TOPIC_LIMITS), "unclassified")


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
        limit: int = FINAL_LIMIT,
    ) -> tuple[RetrievedChunk, ...]:
        if not 1 <= limit <= FINAL_LIMIT:
            raise ValueError("The retrieval limit must be between one and eight.")
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
        selected_scope = scope or RetrievalScope()
        if scope is not None:
            chunks = tuple(chunk for chunk in chunks if selected_scope.accepts(chunk))
            if not chunks:
                return ()
        chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        sparse_query = _fts_query(normalized_query)
        sparse_ids = await asyncio.to_thread(
            self._repository.sparse_chunk_ids,
            selected_snapshot,
            sparse_query,
            SPARSE_LIMIT,
            **({"document_ids": tuple(dict.fromkeys(chunk.document_id for chunk in chunks))}
               if scope is not None else {}),
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
            if len(selected) == limit:
                break
        return tuple(selected)

    async def retrieve_analysis(
        self, query: str, *, occupation_query: str | None = None,
        run_id: str | None = None, snapshot_id: str | None = None,
    ) -> tuple[tuple[RetrievedChunk, ...], dict[str, object]]:
        """Keep task, AI and labour context in one frozen, eight-passage budget."""

        query = " ".join(query.split())
        if not query or len(query) > 2_000:
            raise ValueError("An analysis retrieval query must contain one to 2000 characters.")
        if run_id is not None and snapshot_id is not None:
            raise ValueError("Choose either an explicit snapshot or a run binding.")
        if run_id is not None:
            snapshot_id = await asyncio.to_thread(self._repository.bind_run_to_active_snapshot, run_id)
        elif snapshot_id is None:
            snapshot_id = await asyncio.to_thread(self._repository.active_snapshot_id)
        chunks = await asyncio.to_thread(self._repository.snapshot_chunks, snapshot_id)
        documents: dict[str, set[str]] = {key: set() for key in (*ANALYSIS_TOPIC_LIMITS, "unclassified")}
        for chunk in chunks:
            documents[analysis_topic(chunk.source_label)].add(chunk.document_id)
        if not any(documents[key] for key in ANALYSIS_TOPIC_LIMITS):
            found = await self.retrieve(query, snapshot_id=snapshot_id)
            return found, {"mode": "unclassified", "unclassified_count": len(found)}

        role_query = " ".join((occupation_query or query).split())[:2_000] or query
        if not re.search(r"\w", role_query, flags=re.UNICODE):
            role_query = query
        # Ordinary manual uploads still participate in the occupational query.
        documents["occupations"].update(documents["unclassified"])
        selected: list[RetrievedChunk] = []
        seen_hashes: set[str] = set()
        counts: dict[str, int] = {}
        for topic, budget in ANALYSIS_TOPIC_LIMITS.items():
            found = await self.retrieve(
                role_query, snapshot_id=snapshot_id,
                scope=RetrievalScope(document_ids=frozenset(documents[topic])),
                limit=budget,
            ) if documents[topic] else ()
            if topic == "occupations" and query != role_query and documents[topic]:
                task_matches = await self.retrieve(
                    query, snapshot_id=snapshot_id,
                    scope=RetrievalScope(document_ids=frozenset(documents[topic])), limit=budget,
                )
                # Keep two role matches, then task context, and fill duplicates
                # from the remaining role matches without exceeding four.
                found = (*found[:2], *task_matches, *found[2:])
            counts[topic] = 0
            for item in found:
                if item.chunk.chunk_hash in seen_hashes:
                    continue
                seen_hashes.add(item.chunk.chunk_hash)
                selected.append(replace(item, rank=len(selected) + 1))
                counts[topic] += 1
                if counts[topic] == budget:
                    break
        source_counts = {topic: sum(analysis_topic(item.chunk.source_label) == topic
                                    for item in selected) for topic in ANALYSIS_TOPIC_LIMITS}
        return tuple(selected), {
            "mode": "topic_directories", "topic_counts": source_counts,
            "query_counts": counts,
            "unclassified_count": sum(analysis_topic(item.chunk.source_label) == "unclassified"
                                      for item in selected),
            "missing_topics": [topic for topic, count in source_counts.items() if not count],
            "meaning": "Directory coverage only; relevance and support must still be checked against each passage.",
        }


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
