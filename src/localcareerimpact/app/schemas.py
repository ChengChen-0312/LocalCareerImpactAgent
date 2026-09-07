"""Closed HTTP response contracts for the MVP chat experience."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatMessageOut(ClosedModel):
    message_id: str
    chat_id: str
    role: Literal["user", "assistant", "system"]
    kind: Literal["text", "profile", "progress", "report", "error"]
    text: str
    sequence: int
    created_at: str
    payload: dict[str, object] | None


class ChatSummaryOut(ClosedModel):
    chat_id: str
    title: str
    created_at: str
    updated_at: str


class ChatListOut(ClosedModel):
    chats: list[ChatSummaryOut]


class ChatDetailOut(ChatSummaryOut):
    messages: list[ChatMessageOut]


class MessagePairOut(ClosedModel):
    user_message: ChatMessageOut
    assistant_message: ChatMessageOut
    chat: ChatSummaryOut


class AttachmentMetadataOut(ClosedModel):
    attachment_id: str
    attachment_type: Literal["pdf", "text", "audio"]
    size_bytes: int = Field(ge=0)


ProfileText = Annotated[str, Field(min_length=1, max_length=500)]


class CandidateProfileFields(ClosedModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    occupation_candidates: list[ProfileText] = Field(max_length=5)
    region: str | None = Field(default=None, max_length=200)
    years_of_experience: float | None = Field(default=None, ge=0, le=80)
    responsibilities: list[ProfileText] = Field(default_factory=list, max_length=30)
    skills: list[ProfileText] = Field(default_factory=list, max_length=50)
    industry_context: str | None = Field(default=None, max_length=1000)
    goals: list[ProfileText] = Field(default_factory=list, max_length=20)

    @field_validator("region", "industry_context")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator(
        "occupation_candidates",
        "responsibilities",
        "skills",
        "goals",
    )
    @classmethod
    def normalize_text_list(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        return normalized


class ProfileCardOut(CandidateProfileFields):
    profile_id: str = Field(min_length=1, max_length=128)
    confirmed: bool


class ConfirmProfileIn(CandidateProfileFields):
    profile_id: str = Field(min_length=1, max_length=128)

    @field_validator("occupation_candidates")
    @classmethod
    def require_occupation_candidate(cls, values: list[str]) -> list[str]:
        if not values:
            raise ValueError("add at least one occupation before confirmation")
        return values


class RunProgressOut(ClosedModel):
    run_id: str
    status: Literal["queued", "running", "complete", "failed", "cancelled"]
    current_stage: str | None
    completed_stages: int = Field(ge=0)
    total_stages: int = Field(ge=0)


class RunExecutionOut(ClosedModel):
    run_id: str
    status: Literal[
        "COMPLETE",
        "RETRIEVAL_FAILED",
        "MAIN_AGENT_FAILED",
        "REVIEW_INCOMPLETE",
        "VALIDATION_FAILED",
        "ANALYSIS_INTERRUPTED",
        "INTERNAL_FAILED",
    ]
    report_id: str | None
    message: str


class ReportSummaryOut(ClosedModel):
    report_id: str
    run_id: str
    title: str
    summary: str
    created_at: str


class ProfileConfirmationOut(ClosedModel):
    profile_message: ChatMessageOut
    progress_message: ChatMessageOut
    run: RunProgressOut
    chat: ChatSummaryOut


class KnowledgeDocumentOut(ClosedModel):
    source_label: str
    filename: str
    source_type: str
    publisher: str
    release_date: str | None
    licence: str
    jurisdiction: Literal["AU", "GLOBAL", "UNKNOWN"]
    authority_class: Literal["official", "peer_reviewed", "institutional", "contextual"]
    status: Literal["accepted", "rejected"]
    document_id: str | None
    content_hash: str | None
    language: str | None
    size_bytes: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    error_message: str | None
    updated_at: str


class KnowledgeDocumentListOut(ClosedModel):
    documents: list[KnowledgeDocumentOut]


class KnowledgeSnapshotOut(ClosedModel):
    snapshot_id: str
    status: Literal["building", "active", "inactive", "failed"]
    stage: str
    total_documents: int = Field(ge=0)
    processed_documents: int = Field(ge=0)
    total_chunks: int = Field(ge=0)
    processed_chunks: int = Field(ge=0)
    failure_source: str | None
    error_message: str | None
    created_at: str
    completed_at: str | None


class KnowledgeStatusOut(ClosedModel):
    rebuilding: bool
    active_snapshot: KnowledgeSnapshotOut | None
    latest_rebuild: KnowledgeSnapshotOut | None


class KnowledgeUploadOut(ClosedModel):
    document: KnowledgeDocumentOut


class KnowledgeCitationOut(ClosedModel):
    snapshot_id: str
    chunk_id: str
    document_id: str
    title: str
    source_label: str
    source_type: str
    publisher: str
    release_date: str | None
    licence: str
    jurisdiction: Literal["AU", "GLOBAL", "UNKNOWN"]
    authority_class: Literal["official", "peer_reviewed", "institutional", "contextual"]
    locator: str
    language: str
    text: str
    content_hash: str
    chunk_hash: str


class RunDetailOut(RunProgressOut):
    report_id: str | None
    error_code: str | None
    message: str
    last_event_id: int = Field(ge=-1)


class RunEventOut(ClosedModel):
    run_id: str
    stage: str
    status: Literal["running", "complete", "failed", "cancelled"]
    message: str
    completed_stages: int = Field(ge=0)
    total_stages: int = Field(ge=0)
    report_id: str | None = None


class ReportCitationDetailOut(ClosedModel):
    evidence_ref: str
    citation_scope: Literal["passage", "source"]
    source_title: str
    publisher: str
    release_date: str | None
    licence: str
    locator: str
    text: str


class ReportDetailOut(ClosedModel):
    report_id: str
    created_at: str
    report: dict[str, object]
    citation_details: list[ReportCitationDetailOut]
