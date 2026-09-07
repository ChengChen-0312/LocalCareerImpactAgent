"""Immutable SQLite knowledge snapshots and manual rebuild orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, NoReturn, Sequence, TypeVar
from uuid import uuid4

from localcareerimpact.app.config import AppSettings
from localcareerimpact.app.database import (
    Database,
    ensure_knowledge_snapshot_fts,
    knowledge_snapshot_fts_table,
    utc_now_iso,
)
from localcareerimpact.app.model_runtime import ModelRuntime

from .chunking import MAX_CHUNKS_PER_SNAPSHOT, KnowledgeChunk, chunk_document
from .documents import (
    KnowledgeDocument,
    KnowledgeDocumentError,
    discover_knowledge_files,
    load_document,
    retain_uploaded_source,
    source_label_for,
)
from .embeddings import EmbeddingClient, EmbeddingError, EmbeddingVector


SourceStatus = Literal["accepted", "rejected"]
SnapshotStatus = Literal["building", "active", "inactive", "failed"]
_T = TypeVar("_T")
LOCAL_SOURCE_PUBLISHER = "Local operator-provided source"
UNAVAILABLE_LICENCE = "unavailable"


class KnowledgeBusyError(RuntimeError):
    """Raised when an administrator starts a second rebuild."""


class KnowledgeSnapshotUnavailableError(LookupError):
    """Raised when retrieval cannot bind to the requested immutable snapshot."""


@dataclass(frozen=True, slots=True)
class KnowledgeSourceRecord:
    source_label: str
    filename: str
    source_type: str
    publisher: str
    release_date: str | None
    licence: str
    jurisdiction: Literal["AU", "GLOBAL", "UNKNOWN"]
    authority_class: Literal["official", "peer_reviewed", "institutional", "contextual"]
    status: SourceStatus
    document_id: str | None
    content_hash: str | None
    language: str | None
    size_bytes: int
    chunk_count: int
    error_message: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class KnowledgeSnapshotRecord:
    snapshot_id: str
    status: SnapshotStatus
    stage: str
    total_documents: int
    processed_documents: int
    total_chunks: int
    processed_chunks: int
    failure_source: str | None
    error_message: str | None
    created_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class KnowledgeStatus:
    active_snapshot: KnowledgeSnapshotRecord | None
    latest_rebuild: KnowledgeSnapshotRecord | None

    @property
    def rebuilding(self) -> bool:
        return self.latest_rebuild is not None and self.latest_rebuild.status == "building"


@dataclass(frozen=True, slots=True)
class StoredKnowledgeChunk:
    chunk_id: str
    document_id: str
    snapshot_id: str
    chunk_index: int
    locator: str
    title: str
    source_label: str
    source_type: str
    text: str
    language: str
    content_hash: str
    chunk_hash: str
    embedding_blob: bytes
    embedding_dimension: int
    metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class SnapshotEmbeddingSpec:
    model_fingerprint: str
    dimension: int


@dataclass(frozen=True, slots=True)
class StoredSourceProvenance:
    document_id: str
    publisher: str
    release_date: str | None
    licence: str
    jurisdiction: Literal["AU", "GLOBAL", "UNKNOWN"]
    authority_class: Literal["official", "peer_reviewed", "institutional", "contextual"]


async def _await_completion_observing_cancellation(
    task: asyncio.Task[_T],
) -> tuple[_T, bool]:
    """Drain a task and report whether its caller was cancelled while waiting."""

    cancellation_received = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancellation_received = True
        except Exception:
            break
    if task.cancelled():
        raise asyncio.CancelledError
    try:
        result = task.result()
    except Exception:
        if cancellation_received:
            raise asyncio.CancelledError from None
        raise
    return result, cancellation_received


async def _await_cancellation_safe(task: asyncio.Task[_T]) -> _T:
    """Drain a task despite repeated cancellation, then propagate cancellation."""

    result, cancellation_received = await _await_completion_observing_cancellation(task)
    if cancellation_received:
        raise asyncio.CancelledError
    return result


class KnowledgeRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create_building_snapshot(self, total_documents: int) -> str:
        random_material = f"{uuid4()}:{utc_now_iso()}".encode("utf-8")
        snapshot_id = f"sha256:{hashlib.sha256(random_material).hexdigest()}"
        now = utc_now_iso()
        with self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO knowledge_snapshots (
                    snapshot_id, status, stage, manifest_json, total_documents,
                    processed_documents, total_chunks, processed_chunks,
                    created_at
                ) VALUES (?, 'building', 'reading', '{}', ?, 0, 0, 0, ?)
                """,
                (snapshot_id, total_documents, now),
            )
        return snapshot_id

    def update_progress(
        self,
        snapshot_id: str,
        *,
        stage: str,
        processed_documents: int | None = None,
        total_chunks: int | None = None,
        processed_chunks: int | None = None,
    ) -> None:
        assignments = ["stage = ?"]
        values: list[object] = [stage]
        for column, value in (
            ("processed_documents", processed_documents),
            ("total_chunks", total_chunks),
            ("processed_chunks", processed_chunks),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                values.append(value)
        values.append(snapshot_id)
        with self._database.connect() as connection:
            connection.execute(
                f"UPDATE knowledge_snapshots SET {', '.join(assignments)} "
                "WHERE snapshot_id = ? AND status = 'building'",
                values,
            )

    def replace_source_inventory(self, sources: Sequence[KnowledgeSourceRecord]) -> None:
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM knowledge_source_files")
            connection.executemany(
                """
                INSERT INTO knowledge_source_files (
                    source_label, filename, source_type, publisher, release_date,
                    licence, jurisdiction, authority_class, status, document_id,
                    content_hash, language, size_bytes, chunk_count,
                    error_message, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [self._source_values(source) for source in sources],
            )

    def record_source(self, source: KnowledgeSourceRecord) -> None:
        with self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO knowledge_source_files (
                    source_label, filename, source_type, publisher, release_date,
                    licence, jurisdiction, authority_class, status, document_id,
                    content_hash, language, size_bytes, chunk_count,
                    error_message, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_label) DO UPDATE SET
                    filename = excluded.filename,
                    source_type = excluded.source_type,
                    publisher = excluded.publisher,
                    release_date = excluded.release_date,
                    licence = excluded.licence,
                    jurisdiction = excluded.jurisdiction,
                    authority_class = excluded.authority_class,
                    status = excluded.status,
                    document_id = excluded.document_id,
                    content_hash = excluded.content_hash,
                    language = excluded.language,
                    size_bytes = excluded.size_bytes,
                    chunk_count = excluded.chunk_count,
                    error_message = excluded.error_message,
                    updated_at = excluded.updated_at
                """,
                self._source_values(source),
            )

    @staticmethod
    def _source_values(source: KnowledgeSourceRecord) -> tuple[object, ...]:
        return (
            source.source_label,
            source.filename,
            source.source_type,
            source.publisher,
            source.release_date,
            source.licence,
            source.jurisdiction,
            source.authority_class,
            source.status,
            source.document_id,
            source.content_hash,
            source.language,
            source.size_bytes,
            source.chunk_count,
            source.error_message,
            source.updated_at,
        )

    def activate_snapshot(
        self,
        snapshot_id: str,
        documents: Sequence[KnowledgeDocument],
        chunks: Sequence[KnowledgeChunk],
        embeddings: Sequence[EmbeddingVector],
        manifest: dict[str, object],
    ) -> None:
        if len(chunks) != len(embeddings):
            raise ValueError("each knowledge chunk requires exactly one embedding")
        if not chunks:
            raise ValueError("a knowledge snapshot requires at least one chunk")
        dimensions = {embedding.dimension for embedding in embeddings}
        if len(dimensions) != 1:
            raise ValueError("knowledge embeddings must have one dimension")
        dimension = next(iter(dimensions))
        embedding_manifest = manifest.get("embedding")
        if (
            not isinstance(embedding_manifest, dict)
            or not isinstance(embedding_manifest.get("model_fingerprint"), str)
            or embedding_manifest.get("dimension") != dimension
        ):
            raise ValueError("the snapshot manifest must bind its embedding model and dimension")

        now = utc_now_iso()
        document_by_id = {document.document_id: document for document in documents}
        with self._database.connect() as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            snapshot = connection.execute(
                "SELECT status FROM knowledge_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if snapshot is None or snapshot["status"] != "building":
                raise KnowledgeSnapshotUnavailableError("The building snapshot is unavailable.")

            for document in documents:
                connection.execute(
                    """
                    INSERT INTO knowledge_documents (
                        document_id, filename, content_hash, title, source_label,
                        source_type, publisher, release_date, licence, jurisdiction,
                        authority_class, language, size_bytes, modified_at,
                        metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(document_id) DO NOTHING
                    """,
                    (
                        document.document_id,
                        document.filename,
                        document.content_hash,
                        document.title,
                        document.source_label,
                        document.source_type,
                        LOCAL_SOURCE_PUBLISHER,
                        None,
                        UNAVAILABLE_LICENCE,
                        "UNKNOWN",
                        "contextual",
                        document.language,
                        document.size_bytes,
                        document.modified_at,
                        json.dumps({}, separators=(",", ":")),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO knowledge_snapshot_documents (
                        snapshot_id, document_id, source_label
                    ) VALUES (?, ?, ?)
                    """,
                    (snapshot_id, document.document_id, document.source_label),
                )

            connection.executemany(
                """
                INSERT INTO knowledge_chunks (
                    chunk_id, document_id, snapshot_id, chunk_index, locator,
                    title, source_label, source_type, text, language,
                    content_hash, chunk_hash, embedding_blob,
                    embedding_dimension, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        chunk.chunk_id,
                        chunk.document_id,
                        snapshot_id,
                        chunk.chunk_index,
                        chunk.locator,
                        chunk.title,
                        chunk.source_label,
                        chunk.source_type,
                        chunk.text,
                        chunk.language,
                        chunk.content_hash,
                        chunk.chunk_hash,
                        embedding.blob,
                        embedding.dimension,
                        json.dumps(
                            {
                                "filename": document_by_id[chunk.document_id].filename,
                                "indexed_at": now,
                                "modified_at": document_by_id[chunk.document_id].modified_at,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        now,
                    )
                    for chunk, embedding in zip(chunks, embeddings, strict=True)
                ),
            )
            ensure_knowledge_snapshot_fts(connection, snapshot_id)
            connection.execute(
                "UPDATE knowledge_snapshots SET status = 'inactive' WHERE status = 'active'"
            )
            connection.execute(
                """
                UPDATE knowledge_snapshots
                SET status = 'active', stage = 'complete', manifest_json = ?,
                    processed_documents = total_documents,
                    total_chunks = ?, processed_chunks = ?, completed_at = ?,
                    failure_source = NULL, error_message = NULL
                WHERE snapshot_id = ? AND status = 'building'
                """,
                (
                    json.dumps(
                        manifest,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    len(chunks),
                    len(chunks),
                    now,
                    snapshot_id,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise KnowledgeSnapshotUnavailableError("The snapshot activation was not committed.")

    def fail_snapshot(self, snapshot_id: str, *, source: str, message: str) -> None:
        with self._database.connect() as connection:
            connection.execute(
                """
                UPDATE knowledge_snapshots
                SET status = 'failed', stage = 'failed', failure_source = ?,
                    error_message = ?, completed_at = ?
                WHERE snapshot_id = ? AND status = 'building'
                """,
                (source, message, utc_now_iso(), snapshot_id),
            )

    def list_sources(self) -> tuple[KnowledgeSourceRecord, ...]:
        with self._database.connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT source_label, filename, source_type, status, document_id,
                       publisher, release_date, licence, jurisdiction,
                       authority_class, content_hash, language, size_bytes, chunk_count,
                       error_message, updated_at
                FROM knowledge_source_files
                ORDER BY CASE status WHEN 'rejected' THEN 0 ELSE 1 END,
                         source_label ASC
                """
            ).fetchall()
        return tuple(self._source_from_row(row) for row in rows)

    def status(self) -> KnowledgeStatus:
        with self._database.connect() as connection:
            connection.row_factory = sqlite3.Row
            active = connection.execute(
                """
                SELECT * FROM knowledge_snapshots
                WHERE status = 'active'
                ORDER BY rowid DESC LIMIT 1
                """
            ).fetchone()
            latest = connection.execute(
                "SELECT * FROM knowledge_snapshots ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return KnowledgeStatus(
            active_snapshot=None if active is None else self._snapshot_from_row(active),
            latest_rebuild=None if latest is None else self._snapshot_from_row(latest),
        )

    def active_snapshot_id(self) -> str:
        status = self.status()
        if status.active_snapshot is None:
            raise KnowledgeSnapshotUnavailableError("No active knowledge snapshot is available.")
        return status.active_snapshot.snapshot_id

    def bind_run_to_active_snapshot(self, run_id: str) -> str:
        """Pin once, before retrieval, so later rebuilds cannot move a run."""

        with self._database.connect() as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT snapshot_id FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KnowledgeSnapshotUnavailableError("The analysis run does not exist.")
            if run["snapshot_id"] is not None:
                return str(run["snapshot_id"])
            active = connection.execute(
                "SELECT snapshot_id FROM knowledge_snapshots WHERE status = 'active'"
            ).fetchone()
            if active is None:
                raise KnowledgeSnapshotUnavailableError("No active knowledge snapshot is available.")
            snapshot_id = str(active["snapshot_id"])
            connection.execute(
                "UPDATE runs SET snapshot_id = ?, updated_at = ? WHERE run_id = ? AND snapshot_id IS NULL",
                (snapshot_id, utc_now_iso(), run_id),
            )
            return snapshot_id

    def snapshot_embedding_spec(self, snapshot_id: str) -> SnapshotEmbeddingSpec:
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT manifest_json
                FROM knowledge_snapshots
                WHERE snapshot_id = ? AND status IN ('active', 'inactive')
                """,
                (snapshot_id,),
            ).fetchone()
        if row is None:
            raise KnowledgeSnapshotUnavailableError(
                "The requested knowledge snapshot is unavailable."
            )
        try:
            manifest = json.loads(row[0])
            embedding = manifest["embedding"]
            fingerprint = embedding["model_fingerprint"]
            dimension = embedding["dimension"]
            if (
                not isinstance(fingerprint, str)
                or not fingerprint.startswith("sha256:")
                or len(fingerprint) != 71
                or not isinstance(dimension, int)
                or isinstance(dimension, bool)
                or dimension < 1
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise KnowledgeSnapshotUnavailableError(
                "The knowledge snapshot has no valid embedding identity."
            ) from exc
        return SnapshotEmbeddingSpec(
            model_fingerprint=fingerprint,
            dimension=dimension,
        )

    def sparse_chunk_ids(
        self, snapshot_id: str, query: str, limit: int = 20,
        *, document_ids: Sequence[str] | None = None,
    ) -> tuple[str, ...]:
        table = knowledge_snapshot_fts_table(snapshot_id)
        document_filter = ""
        parameters: list[object] = [query, snapshot_id]
        if document_ids is not None:
            if not document_ids:
                return ()
            unique_ids = tuple(dict.fromkeys(document_ids))
            document_filter = " AND chunks.document_id IN (" + ",".join("?" for _ in unique_ids) + ")"
            parameters.extend(unique_ids)
        parameters.append(limit)
        with self._database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT chunks.chunk_id
                FROM {table} AS search
                JOIN knowledge_chunks AS chunks ON chunks.row_id = search.rowid
                WHERE {table} MATCH ? AND chunks.snapshot_id = ? {document_filter}
                ORDER BY bm25({table}), chunks.chunk_id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def snapshot_chunks(self, snapshot_id: str) -> tuple[StoredKnowledgeChunk, ...]:
        with self._database.connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT chunk_id, document_id, snapshot_id, chunk_index, locator,
                       title, source_label, source_type, text, language,
                       content_hash, chunk_hash, embedding_blob,
                       embedding_dimension, metadata_json
                FROM knowledge_chunks
                WHERE snapshot_id = ?
                ORDER BY chunk_id
                """,
                (snapshot_id,),
            ).fetchall()
        if not rows:
            raise KnowledgeSnapshotUnavailableError("The requested knowledge snapshot is unavailable.")
        return tuple(self._chunk_from_row(row) for row in rows)

    def source_provenance(
        self, document_ids: Sequence[str]
    ) -> dict[str, StoredSourceProvenance]:
        unique_ids = tuple(dict.fromkeys(document_ids))
        if not unique_ids:
            return {}
        placeholders = ",".join("?" for _item in unique_ids)
        with self._database.connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"""
                SELECT document_id, publisher, release_date, licence,
                       jurisdiction, authority_class
                FROM knowledge_documents
                WHERE document_id IN ({placeholders})
                ORDER BY document_id
                """,
                unique_ids,
            ).fetchall()
        if len(rows) != len(unique_ids):
            raise KnowledgeSnapshotUnavailableError(
                "The selected knowledge source provenance is unavailable."
            )
        return {
            str(row["document_id"]): StoredSourceProvenance(
                document_id=str(row["document_id"]),
                publisher=str(row["publisher"]),
                release_date=row["release_date"],
                licence=str(row["licence"]),
                jurisdiction=row["jurisdiction"],
                authority_class=row["authority_class"],
            )
            for row in rows
        }

    def resolve_chunk(self, snapshot_id: str, chunk_id: str) -> StoredKnowledgeChunk:
        with self._database.connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT chunk_id, document_id, snapshot_id, chunk_index, locator,
                       title, source_label, source_type, text, language,
                       content_hash, chunk_hash, embedding_blob,
                       embedding_dimension, metadata_json
                FROM knowledge_chunks
                WHERE snapshot_id = ? AND chunk_id = ?
                """,
                (snapshot_id, chunk_id),
            ).fetchone()
        if row is None:
            raise KnowledgeSnapshotUnavailableError("The citation locator is unavailable.")
        return self._chunk_from_row(row)

    @staticmethod
    def _source_from_row(row: sqlite3.Row) -> KnowledgeSourceRecord:
        return KnowledgeSourceRecord(
            source_label=str(row["source_label"]),
            filename=str(row["filename"]),
            source_type=str(row["source_type"]),
            publisher=str(row["publisher"]),
            release_date=row["release_date"],
            licence=str(row["licence"]),
            jurisdiction=row["jurisdiction"],
            authority_class=row["authority_class"],
            status=row["status"],
            document_id=row["document_id"],
            content_hash=row["content_hash"],
            language=row["language"],
            size_bytes=int(row["size_bytes"]),
            chunk_count=int(row["chunk_count"]),
            error_message=row["error_message"],
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> KnowledgeSnapshotRecord:
        return KnowledgeSnapshotRecord(
            snapshot_id=str(row["snapshot_id"]),
            status=row["status"],
            stage=str(row["stage"]),
            total_documents=int(row["total_documents"]),
            processed_documents=int(row["processed_documents"]),
            total_chunks=int(row["total_chunks"]),
            processed_chunks=int(row["processed_chunks"]),
            failure_source=row["failure_source"],
            error_message=row["error_message"],
            created_at=str(row["created_at"]),
            completed_at=row["completed_at"],
        )

    @staticmethod
    def _chunk_from_row(row: sqlite3.Row) -> StoredKnowledgeChunk:
        decoded = json.loads(row["metadata_json"])
        metadata = decoded if isinstance(decoded, dict) else {}
        return StoredKnowledgeChunk(
            chunk_id=str(row["chunk_id"]),
            document_id=str(row["document_id"]),
            snapshot_id=str(row["snapshot_id"]),
            chunk_index=int(row["chunk_index"]),
            locator=str(row["locator"]),
            title=str(row["title"]),
            source_label=str(row["source_label"]),
            source_type=str(row["source_type"]),
            text=str(row["text"]),
            language=str(row["language"]),
            content_hash=str(row["content_hash"]),
            chunk_hash=str(row["chunk_hash"]),
            embedding_blob=bytes(row["embedding_blob"]),
            embedding_dimension=int(row["embedding_dimension"]),
            metadata=metadata,
        )


class KnowledgeService:
    def __init__(self, database: Database, settings: AppSettings, runtime: ModelRuntime) -> None:
        self.repository = KnowledgeRepository(database)
        model_paths = settings.load_model_paths()
        self._settings = settings
        self._embedding_model_path = model_paths.embedding
        self.embedding_client = EmbeddingClient(
            python_path=settings.repository_root
            / "environments"
            / "embedding"
            / ".venv"
            / "bin"
            / "python",
            model_path=model_paths.embedding,
            repository_root=settings.repository_root,
            runtime=runtime,
        )
        self._mutation_lock = asyncio.Lock()

    async def retain_upload(
        self,
        filename: str,
        media_type: str | None,
        stream: BinaryIO,
    ) -> KnowledgeSourceRecord:
        async with self._mutation_lock:
            operation = asyncio.create_task(
                asyncio.to_thread(
                    self._retain_upload_sync,
                    filename,
                    media_type,
                    stream,
                )
            )
            return await _await_cancellation_safe(operation)

    def _retain_upload_sync(
        self,
        filename: str,
        media_type: str | None,
        stream: BinaryIO,
    ) -> KnowledgeSourceRecord:
        path = retain_uploaded_source(
            self._settings.knowledge_inbox,
            filename,
            media_type,
            stream,
        )
        document = load_document(path, self._settings.knowledge_inbox)
        chunks = chunk_document(document, "sha256:" + "0" * 64)
        source = self._accepted_source(document, len(chunks))
        self.repository.record_source(source)
        return source

    async def rebuild(self) -> KnowledgeStatus:
        async with self._mutation_lock:
            paths = await asyncio.to_thread(
                discover_knowledge_files, self._settings.knowledge_inbox
            )
            snapshot_id = await self._create_building_snapshot(len(paths))
            failure_source = "knowledge-inbox"
            try:
                documents: list[KnowledgeDocument] = []
                document_by_id: dict[str, KnowledgeDocument] = {}
                chunks: list[KnowledgeChunk] = []
                sources: list[KnowledgeSourceRecord] = []
                rejected: list[KnowledgeDocumentError] = []
                for index, path in enumerate(paths, start=1):
                    source_label = source_label_for(path, self._settings.knowledge_inbox)
                    failure_source = source_label
                    try:
                        document = await asyncio.to_thread(
                            load_document, path, self._settings.knowledge_inbox
                        )
                        if document.document_id not in document_by_id:
                            document_chunks = list(chunk_document(document, snapshot_id))
                            if not document_chunks:
                                raise KnowledgeDocumentError(
                                    source_label,
                                    "The knowledge source produced no usable chunks.",
                                )
                            document_by_id[document.document_id] = document
                            documents.append(document)
                            chunks.extend(document_chunks)
                        else:
                            original = document_by_id[document.document_id]
                            sources.append(
                                self._accepted_source(
                                    document,
                                    0,
                                    note=(
                                        "Duplicate content; this source reuses the chunks from "
                                        f"{original.source_label}."
                                    ),
                                )
                            )
                            await asyncio.to_thread(
                                self.repository.update_progress,
                                snapshot_id,
                                stage="reading",
                                processed_documents=index,
                            )
                            continue
                        sources.append(self._accepted_source(document, len(document_chunks)))
                    except KnowledgeDocumentError as exc:
                        rejected.append(exc)
                        sources.append(self._rejected_source(path, source_label, str(exc)))
                    await asyncio.to_thread(
                        self.repository.update_progress,
                        snapshot_id,
                        stage="reading",
                        processed_documents=index,
                    )

                await asyncio.to_thread(self.repository.replace_source_inventory, sources)
                if not paths:
                    raise KnowledgeDocumentError(
                        "knowledge-inbox", "No retained knowledge sources are available."
                    )
                if rejected:
                    raise rejected[0]
                if len(chunks) > MAX_CHUNKS_PER_SNAPSHOT:
                    raise KnowledgeDocumentError(
                        "knowledge-inbox", "The knowledge snapshot contains too many chunks."
                    )

                failure_source = "embedding"
                await asyncio.to_thread(
                    self.repository.update_progress,
                    snapshot_id,
                    stage="embedding",
                    total_chunks=len(chunks),
                )
                embedding_result = await self.embedding_client.embed(
                    [chunk.text for chunk in chunks]
                )
                embeddings = embedding_result.vectors
                await asyncio.to_thread(
                    self.repository.update_progress,
                    snapshot_id,
                    stage="committing",
                    processed_chunks=len(embeddings),
                )
                manifest = {
                    "embedding": {
                        "dimension": embeddings[0].dimension,
                        "model_fingerprint": embedding_result.model_fingerprint,
                        "model_snapshot": self._embedding_model_path.name,
                    },
                    "sources": [
                        {
                            "content_hash": source.content_hash,
                            "document_id": source.document_id,
                            "source_label": source.source_label,
                            "publisher": source.publisher,
                            "release_date": source.release_date,
                            "licence": source.licence,
                            "jurisdiction": source.jurisdiction,
                            "authority_class": source.authority_class,
                        }
                        for source in sorted(sources, key=lambda item: item.source_label)
                        if source.status == "accepted"
                    ],
                }
                activation = asyncio.create_task(
                    asyncio.to_thread(
                        self.repository.activate_snapshot,
                        snapshot_id,
                        documents,
                        chunks,
                        embeddings,
                        manifest,
                    )
                )
                # Activation is the linearized point of no return. Its transaction
                # reaches a terminal state before cancellation handling can inspect it.
                await _await_cancellation_safe(activation)
            except asyncio.CancelledError:
                await self._fail_snapshot_and_cancel(
                    snapshot_id,
                    source=failure_source,
                )
            except KnowledgeDocumentError as exc:
                await asyncio.to_thread(
                    self.repository.fail_snapshot,
                    snapshot_id,
                    source=exc.source_label,
                    message=str(exc),
                )
            except EmbeddingError as exc:
                await asyncio.to_thread(
                    self.repository.fail_snapshot,
                    snapshot_id,
                    source="embedding",
                    message=str(exc),
                )
            except Exception:
                await asyncio.to_thread(
                    self.repository.fail_snapshot,
                    snapshot_id,
                    source=failure_source,
                    message="The knowledge rebuild failed safely.",
                )
            return await asyncio.to_thread(self.repository.status)

    async def _create_building_snapshot(self, total_documents: int) -> str:
        creation = asyncio.create_task(
            asyncio.to_thread(
                self.repository.create_building_snapshot,
                total_documents,
            )
        )
        snapshot_id, cancellation_received = (
            await _await_completion_observing_cancellation(creation)
        )
        if cancellation_received:
            await self._fail_snapshot_and_cancel(
                snapshot_id,
                source="knowledge-inbox",
            )
        return snapshot_id

    async def _fail_snapshot_and_cancel(
        self,
        snapshot_id: str,
        *,
        source: str,
    ) -> NoReturn:
        failure = asyncio.create_task(
            asyncio.to_thread(
                self.repository.fail_snapshot,
                snapshot_id,
                source=source,
                message="The knowledge rebuild was cancelled.",
            )
        )
        try:
            await _await_cancellation_safe(failure)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise asyncio.CancelledError from exc
        raise asyncio.CancelledError

    @staticmethod
    def _accepted_source(
        document: KnowledgeDocument,
        chunk_count: int,
        *,
        note: str | None = None,
    ) -> KnowledgeSourceRecord:
        return KnowledgeSourceRecord(
            source_label=document.source_label,
            filename=document.filename,
            source_type=document.source_type,
            publisher=LOCAL_SOURCE_PUBLISHER,
            release_date=None,
            licence=UNAVAILABLE_LICENCE,
            jurisdiction="UNKNOWN",
            authority_class="contextual",
            status="accepted",
            document_id=document.document_id,
            content_hash=document.content_hash,
            language=document.language,
            size_bytes=document.size_bytes,
            chunk_count=chunk_count,
            error_message=note,
            updated_at=utc_now_iso(),
        )

    @staticmethod
    def _rejected_source(
        path: Path, source_label: str, message: str
    ) -> KnowledgeSourceRecord:
        try:
            size_bytes = path.lstat().st_size
        except OSError:
            size_bytes = 0
        source_type = path.suffix.lower().removeprefix(".") or "unknown"
        return KnowledgeSourceRecord(
            source_label=source_label,
            filename=path.name,
            source_type=source_type,
            publisher=LOCAL_SOURCE_PUBLISHER,
            release_date=None,
            licence=UNAVAILABLE_LICENCE,
            jurisdiction="UNKNOWN",
            authority_class="contextual",
            status="rejected",
            document_id=None,
            content_hash=None,
            language=None,
            size_bytes=size_bytes,
            chunk_count=0,
            error_message=message,
            updated_at=utc_now_iso(),
        )


__all__ = [
    "KnowledgeBusyError",
    "KnowledgeRepository",
    "KnowledgeService",
    "KnowledgeSnapshotRecord",
    "KnowledgeSnapshotUnavailableError",
    "KnowledgeSourceRecord",
    "KnowledgeStatus",
    "LOCAL_SOURCE_PUBLISHER",
    "SnapshotEmbeddingSpec",
    "StoredKnowledgeChunk",
    "StoredSourceProvenance",
    "UNAVAILABLE_LICENCE",
]
