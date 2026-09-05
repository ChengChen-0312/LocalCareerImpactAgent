"""Manual retained-source knowledge snapshots and local hybrid retrieval."""

from .chunking import KnowledgeChunk, chunk_document
from .documents import KnowledgeDocument, KnowledgeDocumentError, load_document
from .embeddings import (
    EmbeddingClient,
    EmbeddingError,
    EmbeddingResult,
    EmbeddingVector,
)
from .factpack_adapter import (
    SourceProvenance,
    adapt_evidence_context,
    adapt_retrieved_chunks,
)
from .repository import (
    KnowledgeBusyError,
    KnowledgeRepository,
    KnowledgeService,
    KnowledgeSnapshotUnavailableError,
)
from .retrieval import (
    HybridRetriever,
    KnowledgeEmbeddingMismatchError,
    RetrievalScope,
    RetrievedChunk,
)

__all__ = [
    "EmbeddingClient",
    "EmbeddingError",
    "EmbeddingResult",
    "EmbeddingVector",
    "HybridRetriever",
    "KnowledgeBusyError",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "KnowledgeDocumentError",
    "KnowledgeEmbeddingMismatchError",
    "KnowledgeRepository",
    "KnowledgeService",
    "KnowledgeSnapshotUnavailableError",
    "RetrievalScope",
    "RetrievedChunk",
    "SourceProvenance",
    "adapt_evidence_context",
    "adapt_retrieved_chunks",
    "chunk_document",
    "load_document",
]
