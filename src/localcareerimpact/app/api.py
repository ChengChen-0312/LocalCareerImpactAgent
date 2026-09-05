"""MVP shell HTTP endpoints."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict

from localcareerimpact.agent import (
    AnalysisRunNotFoundError,
    AnalysisRunStateError,
    StarOrchestrator,
)

from localcareerimpact.intake import (
    IntakeDependencyError,
    IntakeProcessingError,
    IntakeValidationError,
    ingest_candidate_message,
    stream_candidate_multipart,
)
from localcareerimpact.intake.profile import ProfileAttemptDiagnostics
from localcareerimpact.knowledge import (
    KnowledgeBusyError,
    KnowledgeDocumentError,
    KnowledgeService,
    KnowledgeSnapshotUnavailableError,
)
from localcareerimpact.knowledge.repository import (
    KnowledgeSnapshotRecord,
    KnowledgeSourceRecord,
    KnowledgeStatus,
)

from .auth import CsvCredentialStore, SessionStore
from .auth import SessionPrincipal
from .chat_store import ChatNotFoundError, ChatStore, ProfileStateError
from .config import AppSettings, ConfigurationError
from .database import Database
from .model_runtime import ModelRuntime
from .schemas import (
    ChatDetailOut,
    ChatListOut,
    ChatSummaryOut,
    ConfirmProfileIn,
    KnowledgeCitationOut,
    KnowledgeDocumentListOut,
    KnowledgeDocumentOut,
    KnowledgeSnapshotOut,
    KnowledgeStatusOut,
    KnowledgeUploadOut,
    MessagePairOut,
    ProfileConfirmationOut,
    RunExecutionOut,
)


SESSION_COOKIE = "lcia_session"
UNAMBIGUOUS_PROFILE_CONFIRMATIONS = frozenset(
    {
        "confirm",
        "confirm profile",
        "confirm and analyse",
        "confirm and analyze",
        "确认",
        "确认资料",
        "确认并分析",
    }
)
router = APIRouter()


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str


def _credential_store(request: Request) -> CsvCredentialStore:
    return request.app.state.credential_store


def _session_store(request: Request) -> SessionStore:
    return request.app.state.session_store


def _chat_store(request: Request) -> ChatStore:
    return request.app.state.chat_store


def _knowledge_service(request: Request) -> KnowledgeService:
    return request.app.state.knowledge_service


def _require_principal(request: Request) -> SessionPrincipal:
    principal = _session_store(request).resolve(request.cookies.get(SESSION_COOKIE))
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required.",
        )
    return principal


def _require_admin(request: Request) -> SessionPrincipal:
    principal = _require_principal(request)
    settings: AppSettings = request.app.state.settings
    if principal.username != settings.admin_username:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access is required.",
        )
    return principal


def _knowledge_document_out(source: KnowledgeSourceRecord) -> KnowledgeDocumentOut:
    return KnowledgeDocumentOut(**asdict(source))


def _knowledge_snapshot_out(
    snapshot: KnowledgeSnapshotRecord | None,
) -> KnowledgeSnapshotOut | None:
    if snapshot is None:
        return None
    return KnowledgeSnapshotOut(**asdict(snapshot))


def _knowledge_status_out(value: KnowledgeStatus) -> KnowledgeStatusOut:
    return KnowledgeStatusOut(
        rebuilding=value.rebuilding,
        active_snapshot=_knowledge_snapshot_out(value.active_snapshot),
        latest_rebuild=_knowledge_snapshot_out(value.latest_rebuild),
    )


@router.post("/api/auth/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict[str, str]:
    try:
        principal = _credential_store(request).authenticate(body.username, body.password)
    except ConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Credential configuration is unavailable.",
        ) from exc

    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )

    token = _session_store(request).create(principal.username)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
    )
    return {"username": principal.username}


@router.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict[str, bool]:
    _session_store(request).revoke(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(key=SESSION_COOKIE, httponly=True, samesite="lax")
    return {"ok": True}


@router.get("/api/auth/session")
def session(request: Request) -> dict[str, str]:
    principal = _require_principal(request)
    return {"username": principal.username}


@router.get("/api/health")
def health(request: Request) -> dict[str, str]:
    database: Database = request.app.state.database
    model_runtime: ModelRuntime = request.app.state.model_runtime
    statuses = model_runtime.statuses
    return {
        "database": "ready" if database.is_ready() else "unavailable",
        "qwen30b": statuses["qwen30b"],
        "qwen4b": statuses["qwen4b"],
    }


@router.get(
    "/api/admin/knowledge/documents",
    response_model=KnowledgeDocumentListOut,
)
def list_knowledge_documents(request: Request) -> KnowledgeDocumentListOut:
    _require_admin(request)
    sources = _knowledge_service(request).repository.list_sources()
    return KnowledgeDocumentListOut(
        documents=[_knowledge_document_out(source) for source in sources]
    )


@router.post(
    "/api/admin/knowledge/upload",
    response_model=KnowledgeUploadOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_knowledge_document(
    request: Request,
    file: Annotated[UploadFile, File()],
) -> KnowledgeUploadOut:
    _require_admin(request)
    try:
        source = await _knowledge_service(request).retain_upload(
            file.filename or "",
            file.content_type,
            file.file,
        )
        return KnowledgeUploadOut(document=_knowledge_document_out(source))
    except KnowledgeDocumentError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    finally:
        await file.close()


@router.post(
    "/api/admin/knowledge/rebuild",
    response_model=KnowledgeStatusOut,
)
async def rebuild_knowledge(request: Request) -> KnowledgeStatusOut:
    _require_admin(request)
    try:
        result = await _knowledge_service(request).rebuild()
    except KnowledgeBusyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return _knowledge_status_out(result)


@router.get(
    "/api/admin/knowledge/status",
    response_model=KnowledgeStatusOut,
)
def knowledge_status(request: Request) -> KnowledgeStatusOut:
    _require_admin(request)
    return _knowledge_status_out(_knowledge_service(request).repository.status())


@router.get(
    "/api/admin/knowledge/citations/{snapshot_id}/{chunk_id}",
    response_model=KnowledgeCitationOut,
)
def resolve_knowledge_citation(
    snapshot_id: str,
    chunk_id: str,
    request: Request,
) -> KnowledgeCitationOut:
    _require_admin(request)
    try:
        chunk = _knowledge_service(request).repository.resolve_chunk(
            snapshot_id, chunk_id
        )
    except KnowledgeSnapshotUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    provenance = _knowledge_service(request).repository.source_provenance(
        (chunk.document_id,)
    )[chunk.document_id]
    return KnowledgeCitationOut(
        snapshot_id=chunk.snapshot_id,
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        title=chunk.title,
        source_label=chunk.source_label,
        source_type=chunk.source_type,
        publisher=provenance.publisher,
        release_date=provenance.release_date,
        licence=provenance.licence,
        jurisdiction=provenance.jurisdiction,
        authority_class=provenance.authority_class,
        locator=chunk.locator,
        language=chunk.language,
        text=chunk.text,
        content_hash=chunk.content_hash,
        chunk_hash=chunk.chunk_hash,
    )


@router.get("/api/chats", response_model=ChatListOut)
def list_chats(request: Request) -> ChatListOut:
    principal = _require_principal(request)
    return ChatListOut(chats=_chat_store(request).list_chats(principal.username))


@router.post(
    "/api/chats",
    response_model=ChatSummaryOut,
    status_code=status.HTTP_201_CREATED,
)
def create_chat(request: Request) -> ChatSummaryOut:
    principal = _require_principal(request)
    return _chat_store(request).create_chat(principal.username)


@router.get("/api/chats/{chat_id}", response_model=ChatDetailOut)
def get_chat(chat_id: str, request: Request) -> ChatDetailOut:
    principal = _require_principal(request)
    try:
        return _chat_store(request).get_chat(principal.username, chat_id)
    except ChatNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat not found.",
        ) from exc


@router.post("/api/chats/{chat_id}/messages", response_model=MessagePairOut)
async def create_message(
    chat_id: str,
    request: Request,
) -> MessagePairOut:
    principal = _require_principal(request)
    request_id = str(uuid4())
    try:
        store = _chat_store(request)
        store.require_chat(principal.username, chat_id)
        settings: AppSettings = request.app.state.settings
        async with stream_candidate_multipart(
            request,
            request_id=request_id,
            upload_root=settings.upload_root,
        ) as candidate_form:
            text = candidate_form.text
            uploads = candidate_form.uploads
            if not text.strip() and not uploads:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="A message or candidate attachment is required.",
                )

            normalized_confirmation = " ".join(text.split()).casefold()
            if not uploads and normalized_confirmation in UNAMBIGUOUS_PROFILE_CONFIRMATIONS:
                return store.confirm_profile_from_text(
                    principal.username,
                    chat_id,
                    text,
                )

            runtime: ModelRuntime = request.app.state.model_runtime

            def record_profile_attempt(diagnostic: ProfileAttemptDiagnostics) -> None:
                store.record_profile_attempt(
                    principal.username, chat_id, request_id, diagnostic
                )

            intake = await ingest_candidate_message(
                request_id,
                uploads,
                request_dir=candidate_form.request_dir,
                user_text=text,
                settings=settings,
                runtime=runtime,
                record_attempt=record_profile_attempt,
            )
            return store.add_candidate_message(
                principal.username,
                chat_id,
                user_text=text,
                extracted_text=intake.extracted_text,
                attachment_types=intake.attachment_types,
                profile=intake.profile,
            )
    except ChatNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat not found.",
        ) from exc
    except ProfileStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except IntakeValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except IntakeDependencyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except IntakeProcessingError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.post(
    "/api/chats/{chat_id}/confirm-profile",
    response_model=ProfileConfirmationOut,
)
def confirm_profile(
    chat_id: str,
    body: ConfirmProfileIn,
    request: Request,
) -> ProfileConfirmationOut:
    principal = _require_principal(request)
    try:
        return _chat_store(request).confirm_profile(
            principal.username,
            chat_id,
            body,
        )
    except ChatNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat not found.",
        ) from exc
    except ProfileStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.post(
    "/api/runs/{run_id}/execute",
    response_model=RunExecutionOut,
)
async def execute_run(run_id: str, request: Request) -> RunExecutionOut:
    """Run one queued analysis inline; Task 7 owns background execution and SSE."""

    principal = _require_principal(request)
    orchestrator = StarOrchestrator(
        request.app.state.database,
        request.app.state.model_runtime,
        request.app.state.knowledge_service,
    )
    try:
        result = await orchestrator.execute(principal.username, run_id)
    except AnalysisRunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Analysis run not found.",
        ) from exc
    except AnalysisRunStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return RunExecutionOut(
        run_id=result.run_id,
        status=result.status,
        report_id=result.report_id,
        message=result.message,
    )
