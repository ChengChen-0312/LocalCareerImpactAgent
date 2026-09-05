"""Small SQLite bootstrap for MVP persistence."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


ANALYSIS_TOTAL_STAGES = 9


SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id TEXT PRIMARY KEY,
    owner_username TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(chat_id, sequence)
);

CREATE TABLE IF NOT EXISTS candidate_profiles (
    profile_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    profile_json TEXT NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_stage_attempts (
    chat_id TEXT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    diagnostic_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(request_id, attempt)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL REFERENCES candidate_profiles(profile_id),
    snapshot_id TEXT,
    response_language TEXT,
    evidence_context_json TEXT,
    suggestions_json TEXT,
    error_code TEXT,
    error_message TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, sequence)
);

CREATE TABLE IF NOT EXISTS run_stage_attempts (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    diagnostic_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(run_id, stage, attempt)
);

CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE CASCADE,
    snapshot_id TEXT,
    language TEXT,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_documents (
    document_id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    title TEXT NOT NULL,
    source_label TEXT NOT NULL,
    source_type TEXT NOT NULL,
    publisher TEXT NOT NULL DEFAULT 'Local operator-provided source',
    release_date TEXT,
    licence TEXT NOT NULL DEFAULT 'unavailable',
    jurisdiction TEXT NOT NULL DEFAULT 'UNKNOWN',
    authority_class TEXT NOT NULL DEFAULT 'contextual',
    language TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    modified_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    total_documents INTEGER NOT NULL DEFAULT 0,
    processed_documents INTEGER NOT NULL DEFAULT 0,
    total_chunks INTEGER NOT NULL DEFAULT 0,
    processed_chunks INTEGER NOT NULL DEFAULT 0,
    failure_source TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS knowledge_source_files (
    source_label TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    source_type TEXT NOT NULL,
    publisher TEXT NOT NULL DEFAULT 'Local operator-provided source',
    release_date TEXT,
    licence TEXT NOT NULL DEFAULT 'unavailable',
    jurisdiction TEXT NOT NULL DEFAULT 'UNKNOWN',
    authority_class TEXT NOT NULL DEFAULT 'contextual',
    status TEXT NOT NULL,
    document_id TEXT,
    content_hash TEXT,
    language TEXT,
    size_bytes INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_snapshot_documents (
    snapshot_id TEXT NOT NULL REFERENCES knowledge_snapshots(snapshot_id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES knowledge_documents(document_id),
    source_label TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, document_id)
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id TEXT NOT NULL,
    document_id TEXT NOT NULL REFERENCES knowledge_documents(document_id),
    snapshot_id TEXT NOT NULL REFERENCES knowledge_snapshots(snapshot_id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    locator TEXT NOT NULL,
    title TEXT NOT NULL,
    source_label TEXT NOT NULL,
    source_type TEXT NOT NULL,
    text TEXT NOT NULL,
    language TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    chunk_hash TEXT NOT NULL,
    embedding_blob BLOB NOT NULL,
    embedding_dimension INTEGER NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(snapshot_id, chunk_id)
);
"""


def utc_now_iso() -> str:
    """Return a canonical UTC timestamp for application-created records."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def update_run_progress_message(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    current_stage: str,
    completed_stages: int,
) -> None:
    """Keep the persisted chat progress card consistent with the run terminal state."""

    rows = connection.execute(
        """
        SELECT messages.message_id, messages.payload_json
        FROM messages
        JOIN runs ON runs.chat_id = messages.chat_id
        WHERE runs.run_id = ? AND messages.kind = 'progress'
        ORDER BY messages.sequence DESC
        """,
        (run_id,),
    ).fetchall()
    for message_id, payload_json in rows:
        try:
            payload = json.loads(payload_json) if payload_json is not None else None
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("run_id") != run_id:
            continue
        payload.update(
            {
                "status": status,
                "current_stage": current_stage,
                "completed_stages": completed_stages,
            }
        )
        connection.execute(
            "UPDATE messages SET payload_json = ? WHERE message_id = ?",
            (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                message_id,
            ),
        )
        return


_SNAPSHOT_ID = re.compile(r"sha256:([0-9a-f]{64})")


def knowledge_snapshot_fts_table(snapshot_id: str) -> str:
    """Return a safe immutable FTS table name for one snapshot."""

    match = _SNAPSHOT_ID.fullmatch(snapshot_id)
    if match is None:
        raise ValueError("Knowledge snapshot IDs must be canonical SHA-256 identifiers.")
    return f"knowledge_chunks_fts_{match.group(1)}"


def ensure_knowledge_snapshot_fts(
    connection: sqlite3.Connection, snapshot_id: str
) -> None:
    """Create a snapshot-local FTS corpus whose BM25 statistics cannot drift."""

    table = knowledge_snapshot_fts_table(snapshot_id)
    connection.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING fts5(
            text,
            title,
            source_label,
            tokenize='unicode61'
        )
        """
    )
    expected = int(
        connection.execute(
            "SELECT COUNT(*) FROM knowledge_chunks WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()[0]
    )
    actual = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    if actual == expected:
        return
    connection.execute(f"DELETE FROM {table}")
    connection.execute(
        f"""
        INSERT INTO {table}(rowid, text, title, source_label)
        SELECT row_id, text, title, source_label
        FROM knowledge_chunks
        WHERE snapshot_id = ?
        ORDER BY row_id
        """,
        (snapshot_id,),
    )


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(SCHEMA)
            self._migrate_pre_rag_schema(connection)
            self._terminalize_interrupted_analysis_runs(connection)
            self._initialize_knowledge_fts(connection)
            connection.execute(
                """
                UPDATE knowledge_snapshots
                SET status = 'failed', stage = 'failed',
                    failure_source = 'application-restart',
                    error_message = 'The knowledge rebuild was interrupted by an application restart.',
                    completed_at = ?
                WHERE status = 'building'
                """,
                (utc_now_iso(),),
            )

    @staticmethod
    def _terminalize_interrupted_analysis_runs(
        connection: sqlite3.Connection,
    ) -> None:
        """Fail closed any analysis left RUNNING by a prior process termination."""

        rows = connection.execute(
            "SELECT run_id FROM runs WHERE status = 'RUNNING' ORDER BY run_id"
        ).fetchall()
        for (run_id_value,) in rows:
            run_id = str(run_id_value)
            now = utc_now_iso()
            message = "The analysis was interrupted by an application restart."
            connection.execute(
                """
                UPDATE runs
                SET status = 'ANALYSIS_INTERRUPTED',
                    error_code = 'ANALYSIS_INTERRUPTED', error_message = ?, updated_at = ?
                WHERE run_id = ? AND status = 'RUNNING'
                """,
                (message, now, run_id),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                continue
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), -1) + 1 FROM run_events WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO run_events (
                    event_id, run_id, sequence, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, 'failed', ?, ?)
                """,
                (
                    str(uuid4()),
                    run_id,
                    sequence,
                    json.dumps(
                        {
                            "status": "failed",
                            "code": "ANALYSIS_INTERRUPTED",
                            "message": message,
                        },
                        separators=(",", ":"),
                    ),
                    now,
                ),
            )
            update_run_progress_message(
                connection,
                run_id,
                status="cancelled",
                current_stage="Analysis interrupted",
                completed_stages=min(sequence, ANALYSIS_TOTAL_STAGES),
            )

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _migrate_pre_rag_schema(self, connection: sqlite3.Connection) -> None:
        """Preserve pre-Task-5 placeholders while installing the snapshot schema."""

        run_columns = self._columns(connection, "runs")
        run_additions = {
            "snapshot_id": "TEXT",
            "response_language": "TEXT",
            "evidence_context_json": "TEXT",
            "suggestions_json": "TEXT",
            "error_code": "TEXT",
            "error_message": "TEXT",
        }
        for column, declaration in run_additions.items():
            if column not in run_columns:
                connection.execute(f"ALTER TABLE runs ADD COLUMN {column} {declaration}")

        report_columns = self._columns(connection, "reports")
        for column in ("snapshot_id", "language"):
            if column not in report_columns:
                connection.execute(f"ALTER TABLE reports ADD COLUMN {column} TEXT")

        snapshot_columns = self._columns(connection, "knowledge_snapshots")
        snapshot_additions = {
            "status": "TEXT NOT NULL DEFAULT 'failed'",
            "stage": "TEXT NOT NULL DEFAULT 'legacy'",
            "total_documents": "INTEGER NOT NULL DEFAULT 0",
            "processed_documents": "INTEGER NOT NULL DEFAULT 0",
            "total_chunks": "INTEGER NOT NULL DEFAULT 0",
            "processed_chunks": "INTEGER NOT NULL DEFAULT 0",
            "failure_source": "TEXT",
            "error_message": "TEXT",
            "completed_at": "TEXT",
        }
        for column, declaration in snapshot_additions.items():
            if column not in snapshot_columns:
                connection.execute(
                    f"ALTER TABLE knowledge_snapshots ADD COLUMN {column} {declaration}"
                )

        document_columns = self._columns(connection, "knowledge_documents")
        document_additions = {
            "title": "TEXT NOT NULL DEFAULT ''",
            "source_label": "TEXT NOT NULL DEFAULT ''",
            "source_type": "TEXT NOT NULL DEFAULT ''",
            "publisher": "TEXT NOT NULL DEFAULT 'Local operator-provided source'",
            "release_date": "TEXT",
            "licence": "TEXT NOT NULL DEFAULT 'unavailable'",
            "jurisdiction": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
            "authority_class": "TEXT NOT NULL DEFAULT 'contextual'",
            "language": "TEXT NOT NULL DEFAULT 'unknown'",
            "size_bytes": "INTEGER NOT NULL DEFAULT 0",
            "modified_at": "TEXT NOT NULL DEFAULT '1970-01-01T00:00:00Z'",
        }
        for column, declaration in document_additions.items():
            if column not in document_columns:
                connection.execute(
                    f"ALTER TABLE knowledge_documents ADD COLUMN {column} {declaration}"
                )

        source_file_columns = self._columns(connection, "knowledge_source_files")
        source_file_additions = {
            "publisher": "TEXT NOT NULL DEFAULT 'Local operator-provided source'",
            "release_date": "TEXT",
            "licence": "TEXT NOT NULL DEFAULT 'unavailable'",
            "jurisdiction": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
            "authority_class": "TEXT NOT NULL DEFAULT 'contextual'",
        }
        for column, declaration in source_file_additions.items():
            if column not in source_file_columns:
                connection.execute(
                    f"ALTER TABLE knowledge_source_files ADD COLUMN {column} {declaration}"
                )

        chunk_columns = self._columns(connection, "knowledge_chunks")
        if "embedding_blob" not in chunk_columns or "row_id" not in chunk_columns:
            legacy_table = "knowledge_chunks_pre_task5"
            if not self._columns(connection, legacy_table):
                connection.execute(
                    f"ALTER TABLE knowledge_chunks RENAME TO {legacy_table}"
                )
            else:
                connection.execute("DROP TABLE knowledge_chunks")
            connection.executescript(
                """
                CREATE TABLE knowledge_chunks (
                    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chunk_id TEXT NOT NULL,
                    document_id TEXT NOT NULL REFERENCES knowledge_documents(document_id),
                    snapshot_id TEXT NOT NULL REFERENCES knowledge_snapshots(snapshot_id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL,
                    locator TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_label TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    text TEXT NOT NULL,
                    language TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    chunk_hash TEXT NOT NULL,
                    embedding_blob BLOB NOT NULL,
                    embedding_dimension INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(snapshot_id, chunk_id)
                );
                """
            )

        connection.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS knowledge_one_active_snapshot
            ON knowledge_snapshots(status) WHERE status = 'active';
            CREATE INDEX IF NOT EXISTS knowledge_chunks_snapshot
            ON knowledge_chunks(snapshot_id, chunk_id);
            CREATE INDEX IF NOT EXISTS knowledge_chunks_document
            ON knowledge_chunks(snapshot_id, document_id);
            """
        )

    @staticmethod
    def _initialize_knowledge_fts(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts USING fts5(
                text,
                title,
                source_label,
                content='knowledge_chunks',
                content_rowid='row_id',
                tokenize='unicode61'
            );

            CREATE TRIGGER IF NOT EXISTS knowledge_chunks_ai AFTER INSERT ON knowledge_chunks BEGIN
                INSERT INTO knowledge_chunks_fts(rowid, text, title, source_label)
                VALUES (new.row_id, new.text, new.title, new.source_label);
            END;

            CREATE TRIGGER IF NOT EXISTS knowledge_chunks_ad AFTER DELETE ON knowledge_chunks BEGIN
                INSERT INTO knowledge_chunks_fts(
                    knowledge_chunks_fts, rowid, text, title, source_label
                ) VALUES (
                    'delete', old.row_id, old.text, old.title, old.source_label
                );
            END;

            CREATE TRIGGER IF NOT EXISTS knowledge_chunks_au AFTER UPDATE ON knowledge_chunks BEGIN
                INSERT INTO knowledge_chunks_fts(
                    knowledge_chunks_fts, rowid, text, title, source_label
                ) VALUES (
                    'delete', old.row_id, old.text, old.title, old.source_label
                );
                INSERT INTO knowledge_chunks_fts(rowid, text, title, source_label)
                VALUES (new.row_id, new.text, new.title, new.source_label);
            END;
            INSERT INTO knowledge_chunks_fts(knowledge_chunks_fts) VALUES('rebuild');
            """
        )
        snapshot_ids = connection.execute(
            "SELECT DISTINCT snapshot_id FROM knowledge_chunks ORDER BY snapshot_id"
        ).fetchall()
        for row in snapshot_ids:
            ensure_knowledge_snapshot_fts(connection, str(row[0]))

    def is_ready(self) -> bool:
        try:
            with self.connect() as connection:
                connection.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return False
        return True
