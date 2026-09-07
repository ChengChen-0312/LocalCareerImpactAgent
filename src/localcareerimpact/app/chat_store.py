"""Owner-scoped SQLite persistence for chats and ordered messages."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Literal
from uuid import uuid4

from localcareerimpact.intake.profile import CandidateProfileDraft, ProfileAttemptDiagnostics

from .database import (
    ANALYSIS_TOTAL_STAGES, RUN_ERROR_MESSAGES, Database, append_run_message, utc_now_iso,
)
from .schemas import (
    ChatDetailOut,
    ChatMessageOut,
    ChatSummaryOut,
    ConfirmProfileIn,
    MessagePairOut,
    ProfileCardOut,
    ProfileConfirmationOut,
    RunProgressOut,
)


DEFAULT_CHAT_TITLE = "New chat"
ASSISTANT_ACKNOWLEDGEMENT = "Thanks — your message has been saved."
PROFILE_REVIEW_MESSAGE = (
    "I extracted a candidate profile from the material you shared. "
    "Please review and edit it before analysis starts."
)
ANALYSIS_QUEUED_MESSAGE = "Profile confirmed. Your local analysis has been queued."
TITLE_CODEPOINT_LIMIT = 48


class ChatNotFoundError(LookupError):
    """Raised when a chat does not exist for the authenticated owner."""


class ProfileStateError(LookupError):
    """Raised when analysis is requested without the current confirmed profile."""


class ChatStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create_chat(self, owner_username: str) -> ChatSummaryOut:
        chat_id = str(uuid4())
        created_at = utc_now_iso()
        with self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO chats (chat_id, owner_username, title, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, owner_username, DEFAULT_CHAT_TITLE, created_at, created_at),
            )
        return ChatSummaryOut(
            chat_id=chat_id,
            title=DEFAULT_CHAT_TITLE,
            created_at=created_at,
            updated_at=created_at,
        )

    def list_chats(self, owner_username: str) -> list[ChatSummaryOut]:
        with self._database.connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT chat_id, title, created_at, updated_at
                FROM chats
                WHERE owner_username = ?
                ORDER BY updated_at DESC, created_at DESC, chat_id DESC
                """,
                (owner_username,),
            ).fetchall()
        return [self._chat_from_row(row) for row in rows]

    def get_chat(self, owner_username: str, chat_id: str) -> ChatDetailOut:
        with self._database.connect() as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            self._owned_chat_row(connection, owner_username, chat_id)
            for run in connection.execute(
                "SELECT runs.run_id, runs.status, reports.report_id, reports.report_json "
                "FROM runs LEFT JOIN reports ON reports.run_id = runs.run_id "
                "WHERE runs.chat_id = ? ORDER BY runs.created_at, runs.run_id", (chat_id,)
            ).fetchall():
                if run["status"] == "COMPLETE" and run["report_id"]:
                    saved = self._decoded_object(run["report_json"])
                    append_run_message(
                        connection, run["run_id"], kind="report", text=str(saved["title"]),
                        payload={"run_id": run["run_id"], "report_id": run["report_id"]},
                    )
                elif run["status"] in RUN_ERROR_MESSAGES:
                    append_run_message(
                        connection, run["run_id"], kind="error", text=RUN_ERROR_MESSAGES[run["status"]],
                        payload={"run_id": run["run_id"], "code": run["status"]},
                    )
            chat_row = connection.execute(
                """
                SELECT chat_id, title, created_at, updated_at
                FROM chats
                WHERE chat_id = ? AND owner_username = ?
                """,
                (chat_id, owner_username),
            ).fetchone()
            if chat_row is None:
                raise ChatNotFoundError(chat_id)
            message_rows = connection.execute(
                """
                SELECT message_id, chat_id, role, kind, text, sequence,
                       created_at, payload_json
                FROM messages
                WHERE chat_id = ?
                ORDER BY sequence ASC
                """,
                (chat_id,),
            ).fetchall()

        chat = self._chat_from_row(chat_row)
        return ChatDetailOut(
            **chat.model_dump(),
            messages=[self._message_from_row(row) for row in message_rows],
        )

    def require_chat(self, owner_username: str, chat_id: str) -> None:
        """Fail before expensive intake work when the chat is absent or not owned."""

        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM chats WHERE chat_id = ? AND owner_username = ?",
                (chat_id, owner_username),
            ).fetchone()
        if row is None:
            raise ChatNotFoundError(chat_id)

    def record_profile_attempt(
        self,
        owner_username: str,
        chat_id: str,
        request_id: str,
        diagnostic: ProfileAttemptDiagnostics,
    ) -> None:
        """Persist private intake metadata only while the owner still has the chat."""

        # Re-parse even model instances so constructed/nested values cannot bypass
        # the content-free contract at the persistence boundary.
        payload = ProfileAttemptDiagnostics.model_validate_json(
            diagnostic.model_dump_json(), strict=True
        )
        with self._database.connect() as connection:
            written = connection.execute(
                """
                INSERT INTO profile_stage_attempts (
                    chat_id, request_id, attempt, diagnostic_json, updated_at
                )
                SELECT chat_id, ?, ?, ?, ?
                FROM chats
                WHERE chat_id = ? AND owner_username = ?
                ON CONFLICT(request_id, attempt) DO UPDATE SET
                    diagnostic_json = excluded.diagnostic_json,
                    updated_at = excluded.updated_at
                WHERE profile_stage_attempts.chat_id = excluded.chat_id
                  AND EXISTS (
                      SELECT 1 FROM chats
                      WHERE chat_id = profile_stage_attempts.chat_id
                        AND owner_username = ?
                  )
                """,
                (
                    request_id,
                    payload.attempt,
                    payload.model_dump_json(),
                    utc_now_iso(),
                    chat_id,
                    owner_username,
                    owner_username,
                ),
            )
            if written.rowcount != 1:
                raise ChatNotFoundError(chat_id)

    def add_candidate_message(
        self,
        owner_username: str,
        chat_id: str,
        *,
        user_text: str,
        extracted_text: str,
        attachment_types: Sequence[Literal["pdf", "text", "audio"]],
        profile: CandidateProfileDraft,
    ) -> MessagePairOut:
        now = utc_now_iso()
        profile_id = str(uuid4())
        user_message_id = str(uuid4())
        profile_message_id = str(uuid4())
        profile_data = profile.model_dump(mode="json")
        profile_card = self._profile_card(profile_id, False, profile_data)
        display_text = user_text.strip() or "Shared candidate material for profile extraction."
        user_payload: dict[str, object] | None = None
        if attachment_types:
            user_payload = {
                "attachment_count": len(attachment_types),
                "attachment_types": list(attachment_types),
                "extracted_text": extracted_text,
            }

        with self._database.connect() as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            chat_row = self._owned_chat_row(connection, owner_username, chat_id)
            next_sequence = self._next_sequence(connection, chat_id)
            connection.execute(
                """
                INSERT INTO candidate_profiles (
                    profile_id, chat_id, profile_json, confirmed, created_at, updated_at
                ) VALUES (?, ?, ?, 0, ?, ?)
                """,
                (
                    profile_id,
                    chat_id,
                    json.dumps(profile_data, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO messages (
                    message_id, chat_id, role, kind, text, sequence,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        user_message_id,
                        chat_id,
                        "user",
                        "text",
                        display_text,
                        next_sequence,
                        self._encode_payload(user_payload),
                        now,
                    ),
                    (
                        profile_message_id,
                        chat_id,
                        "assistant",
                        "profile",
                        PROFILE_REVIEW_MESSAGE,
                        next_sequence + 1,
                        self._encode_payload(profile_card.model_dump(mode="json")),
                        now,
                    ),
                ),
            )

            title = chat_row["title"]
            if next_sequence == 0 and title == DEFAULT_CHAT_TITLE:
                title_source = (
                    user_text.strip()
                    or next(iter(profile.occupation_candidates), "")
                    or extracted_text.strip()
                    or display_text
                )
                title = self._title_from_text(title_source)
            connection.execute(
                "UPDATE chats SET title = ?, updated_at = ? WHERE chat_id = ?",
                (title, now, chat_id),
            )

        user_message = ChatMessageOut(
            message_id=user_message_id,
            chat_id=chat_id,
            role="user",
            kind="text",
            text=display_text,
            sequence=next_sequence,
            created_at=now,
            payload=user_payload,
        )
        assistant_message = ChatMessageOut(
            message_id=profile_message_id,
            chat_id=chat_id,
            role="assistant",
            kind="profile",
            text=PROFILE_REVIEW_MESSAGE,
            sequence=next_sequence + 1,
            created_at=now,
            payload=profile_card.model_dump(mode="json"),
        )
        return MessagePairOut(
            user_message=user_message,
            assistant_message=assistant_message,
            chat=ChatSummaryOut(
                chat_id=chat_id,
                title=title,
                created_at=chat_row["created_at"],
                updated_at=now,
            ),
        )

    def confirm_profile(
        self,
        owner_username: str,
        chat_id: str,
        confirmation: ConfirmProfileIn,
    ) -> ProfileConfirmationOut:
        now = utc_now_iso()
        with self._database.connect() as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            chat_row = self._owned_chat_row(connection, owner_username, chat_id)
            current_profile = self._current_profile_row(connection, chat_id)
            if current_profile is None or current_profile["profile_id"] != confirmation.profile_id:
                raise ProfileStateError("The profile card is no longer current.")
            if current_profile["confirmed"]:
                raise ProfileStateError("The current profile has already been confirmed.")

            stored_profile = self._decoded_object(current_profile["profile_json"])
            stored_profile.update(
                confirmation.model_dump(mode="json", exclude={"profile_id"})
            )
            profile_card = self._profile_card(
                confirmation.profile_id, True, stored_profile
            )
            connection.execute(
                """
                UPDATE candidate_profiles
                SET profile_json = ?, confirmed = 1, updated_at = ?
                WHERE profile_id = ?
                """,
                (
                    json.dumps(stored_profile, ensure_ascii=False),
                    now,
                    confirmation.profile_id,
                ),
            )
            profile_message = self._update_current_profile_message(
                connection, chat_id, profile_card
            )
            run, progress_message = self._create_analysis_run(
                connection,
                chat_id,
                confirmation.profile_id,
                now,
            )
            connection.execute(
                "UPDATE chats SET updated_at = ? WHERE chat_id = ?",
                (now, chat_id),
            )

        return ProfileConfirmationOut(
            profile_message=profile_message,
            progress_message=progress_message,
            run=run,
            chat=ChatSummaryOut(
                chat_id=chat_id,
                title=chat_row["title"],
                created_at=chat_row["created_at"],
                updated_at=now,
            ),
        )

    def confirm_profile_from_text(
        self,
        owner_username: str,
        chat_id: str,
        confirmation_text: str,
    ) -> MessagePairOut:
        """Confirm only the latest card for an exact, unambiguous text command."""

        now = utc_now_iso()
        user_message_id = str(uuid4())
        with self._database.connect() as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            chat_row = self._owned_chat_row(connection, owner_username, chat_id)
            current_profile = self._current_profile_row(connection, chat_id)
            if current_profile is None:
                raise ProfileStateError("There is no current profile card to confirm.")
            if current_profile["confirmed"]:
                raise ProfileStateError("The current profile has already been confirmed.")

            profile_id = current_profile["profile_id"]
            stored_profile = self._decoded_object(current_profile["profile_json"])
            self._require_usable_occupation(stored_profile)
            profile_card = self._profile_card(profile_id, True, stored_profile)
            connection.execute(
                """
                UPDATE candidate_profiles
                SET confirmed = 1, updated_at = ?
                WHERE profile_id = ?
                """,
                (now, profile_id),
            )
            self._update_current_profile_message(connection, chat_id, profile_card)
            next_sequence = self._next_sequence(connection, chat_id)
            connection.execute(
                """
                INSERT INTO messages (
                    message_id, chat_id, role, kind, text, sequence,
                    payload_json, created_at
                ) VALUES (?, ?, 'user', 'text', ?, ?, NULL, ?)
                """,
                (user_message_id, chat_id, confirmation_text, next_sequence, now),
            )
            run, progress_message = self._create_analysis_run(
                connection,
                chat_id,
                profile_id,
                now,
                sequence=next_sequence + 1,
            )
            connection.execute(
                "UPDATE chats SET updated_at = ? WHERE chat_id = ?",
                (now, chat_id),
            )

        user_message = ChatMessageOut(
            message_id=user_message_id,
            chat_id=chat_id,
            role="user",
            kind="text",
            text=confirmation_text,
            sequence=next_sequence,
            created_at=now,
            payload=None,
        )
        return MessagePairOut(
            user_message=user_message,
            assistant_message=progress_message,
            chat=ChatSummaryOut(
                chat_id=chat_id,
                title=chat_row["title"],
                created_at=chat_row["created_at"],
                updated_at=now,
            ),
        )

    def add_text_message(
        self, owner_username: str, chat_id: str, text: str
    ) -> MessagePairOut:
        now = utc_now_iso()
        user_message_id = str(uuid4())
        assistant_message_id = str(uuid4())

        with self._database.connect() as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            chat_row = connection.execute(
                """
                SELECT chat_id, title, created_at, updated_at
                FROM chats
                WHERE chat_id = ? AND owner_username = ?
                """,
                (chat_id, owner_username),
            ).fetchone()
            if chat_row is None:
                raise ChatNotFoundError(chat_id)

            next_sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), -1) + 1 FROM messages WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()[0]
            connection.executemany(
                """
                INSERT INTO messages (
                    message_id, chat_id, role, kind, text, sequence,
                    payload_json, created_at
                ) VALUES (?, ?, ?, 'text', ?, ?, NULL, ?)
                """,
                (
                    (user_message_id, chat_id, "user", text, next_sequence, now),
                    (
                        assistant_message_id,
                        chat_id,
                        "assistant",
                        ASSISTANT_ACKNOWLEDGEMENT,
                        next_sequence + 1,
                        now,
                    ),
                ),
            )

            title = chat_row["title"]
            if next_sequence == 0 and title == DEFAULT_CHAT_TITLE:
                title = self._title_from_text(text)
            connection.execute(
                "UPDATE chats SET title = ?, updated_at = ? WHERE chat_id = ?",
                (title, now, chat_id),
            )

        user_message = ChatMessageOut(
            message_id=user_message_id,
            chat_id=chat_id,
            role="user",
            kind="text",
            text=text,
            sequence=next_sequence,
            created_at=now,
            payload=None,
        )
        assistant_message = ChatMessageOut(
            message_id=assistant_message_id,
            chat_id=chat_id,
            role="assistant",
            kind="text",
            text=ASSISTANT_ACKNOWLEDGEMENT,
            sequence=next_sequence + 1,
            created_at=now,
            payload=None,
        )
        return MessagePairOut(
            user_message=user_message,
            assistant_message=assistant_message,
            chat=ChatSummaryOut(
                chat_id=chat_id,
                title=title,
                created_at=chat_row["created_at"],
                updated_at=now,
            ),
        )

    @staticmethod
    def _owned_chat_row(
        connection: sqlite3.Connection,
        owner_username: str,
        chat_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT chat_id, title, created_at, updated_at
            FROM chats
            WHERE chat_id = ? AND owner_username = ?
            """,
            (chat_id, owner_username),
        ).fetchone()
        if row is None:
            raise ChatNotFoundError(chat_id)
        return row

    @staticmethod
    def _next_sequence(connection: sqlite3.Connection, chat_id: str) -> int:
        return connection.execute(
            "SELECT COALESCE(MAX(sequence), -1) + 1 FROM messages WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()[0]

    @staticmethod
    def _current_profile_row(
        connection: sqlite3.Connection,
        chat_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT profile_id, profile_json, confirmed, created_at, updated_at
            FROM candidate_profiles
            WHERE chat_id = ?
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (chat_id,),
        ).fetchone()

    @staticmethod
    def _decoded_object(payload_json: str) -> dict[str, object]:
        decoded = json.loads(payload_json)
        if not isinstance(decoded, dict):
            raise ProfileStateError("The current profile is invalid.")
        return decoded

    @staticmethod
    def _require_usable_occupation(profile_data: dict[str, object]) -> None:
        occupations = profile_data.get("occupation_candidates")
        if not isinstance(occupations, list) or not any(
            isinstance(value, str) and value.strip() for value in occupations
        ):
            raise ProfileStateError(
                "Add at least one occupation before confirming the profile."
            )

    @staticmethod
    def _profile_card(
        profile_id: str,
        confirmed: bool,
        profile_data: dict[str, object],
    ) -> ProfileCardOut:
        visible_fields = {
            key: value
            for key, value in profile_data.items()
            if key != "confidence_notes"
        }
        return ProfileCardOut(
            profile_id=profile_id,
            confirmed=confirmed,
            **visible_fields,
        )

    def _update_current_profile_message(
        self,
        connection: sqlite3.Connection,
        chat_id: str,
        profile_card: ProfileCardOut,
    ) -> ChatMessageOut:
        rows = connection.execute(
            """
            SELECT message_id, chat_id, role, kind, text, sequence,
                   created_at, payload_json
            FROM messages
            WHERE chat_id = ? AND kind = 'profile'
            ORDER BY sequence DESC
            """,
            (chat_id,),
        ).fetchall()
        profile_row = next(
            (
                row
                for row in rows
                if self._decoded_object(row["payload_json"]).get("profile_id")
                == profile_card.profile_id
            ),
            None,
        )
        if profile_row is None:
            raise ProfileStateError("The current profile card message is unavailable.")
        payload = profile_card.model_dump(mode="json")
        connection.execute(
            "UPDATE messages SET payload_json = ? WHERE message_id = ?",
            (self._encode_payload(payload), profile_row["message_id"]),
        )
        return ChatMessageOut(
            message_id=profile_row["message_id"],
            chat_id=chat_id,
            role="assistant",
            kind="profile",
            text=profile_row["text"],
            sequence=profile_row["sequence"],
            created_at=profile_row["created_at"],
            payload=payload,
        )

    def _create_analysis_run(
        self,
        connection: sqlite3.Connection,
        chat_id: str,
        profile_id: str,
        now: str,
        *,
        sequence: int | None = None,
    ) -> tuple[RunProgressOut, ChatMessageOut]:
        confirmed = connection.execute(
            """
            SELECT profile_json FROM candidate_profiles
            WHERE profile_id = ? AND chat_id = ? AND confirmed = 1
            """,
            (profile_id, chat_id),
        ).fetchone()
        if confirmed is None:
            raise ProfileStateError("Analysis requires a current confirmed profile.")
        self._require_usable_occupation(self._decoded_object(confirmed["profile_json"]))

        run_id = str(uuid4())
        progress_message_id = str(uuid4())
        if sequence is None:
            sequence = self._next_sequence(connection, chat_id)
        run = RunProgressOut(
            run_id=run_id,
            status="queued",
            current_stage="Waiting for local analysis",
            completed_stages=0,
            total_stages=ANALYSIS_TOTAL_STAGES,
        )
        connection.execute(
            """
            INSERT INTO runs (run_id, chat_id, profile_id, status, created_at, updated_at)
            VALUES (?, ?, ?, 'queued', ?, ?)
            """,
            (run_id, chat_id, profile_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO messages (
                message_id, chat_id, role, kind, text, sequence,
                payload_json, created_at
            ) VALUES (?, ?, 'assistant', 'progress', ?, ?, ?, ?)
            """,
            (
                progress_message_id,
                chat_id,
                ANALYSIS_QUEUED_MESSAGE,
                sequence,
                self._encode_payload(run.model_dump(mode="json")),
                now,
            ),
        )
        progress_message = ChatMessageOut(
            message_id=progress_message_id,
            chat_id=chat_id,
            role="assistant",
            kind="progress",
            text=ANALYSIS_QUEUED_MESSAGE,
            sequence=sequence,
            created_at=now,
            payload=run.model_dump(mode="json"),
        )
        return run, progress_message

    @staticmethod
    def _encode_payload(payload: dict[str, object] | None) -> str | None:
        if payload is None:
            return None
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _title_from_text(text: str) -> str:
        compact = " ".join(text.split())
        if len(compact) <= TITLE_CODEPOINT_LIMIT:
            return compact
        return f"{compact[: TITLE_CODEPOINT_LIMIT - 1]}…"

    @staticmethod
    def _chat_from_row(row: sqlite3.Row) -> ChatSummaryOut:
        return ChatSummaryOut(
            chat_id=row["chat_id"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> ChatMessageOut:
        payload = None
        if row["payload_json"] is not None:
            decoded = json.loads(row["payload_json"])
            if isinstance(decoded, dict):
                payload = decoded
        return ChatMessageOut(
            message_id=row["message_id"],
            chat_id=row["chat_id"],
            role=row["role"],
            kind=row["kind"],
            text=row["text"],
            sequence=row["sequence"],
            created_at=row["created_at"],
            payload=payload,
        )
