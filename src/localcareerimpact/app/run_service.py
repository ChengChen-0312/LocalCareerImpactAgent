"""Application-owned analysis tasks and owner-scoped public read models."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from uuid import uuid4

from localcareerimpact.agent import (
    AnalysisRunNotFoundError,
    AnalysisRunStateError,
    StarOrchestrator,
)
from localcareerimpact.agent.contracts import MvpReportV1
from localcareerimpact.contracts.runbundle import EvidenceContextV1
from localcareerimpact.knowledge import KnowledgeService

from .database import (
    ANALYSIS_STAGE_MESSAGES,
    ANALYSIS_TOTAL_STAGES,
    RUN_ERROR_MESSAGES,
    Database,
    append_run_message,
    completed_run_stages,
    update_run_progress_message,
    utc_now_iso,
)
from .model_runtime import ModelRuntime
from .schemas import (
    ReportCitationDetailOut,
    ReportDetailOut,
    RunDetailOut,
    RunEventOut,
)


class RunService:
    """Own each run independently of HTTP and SSE connection lifetimes."""

    def __init__(self, database: Database, runtime: ModelRuntime, knowledge: KnowledgeService) -> None:
        self._database = database
        self._orchestrator = StarOrchestrator(database, runtime, knowledge)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._dispatcher: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def stopping(self) -> bool:
        return self._stopping

    async def start(self) -> None:
        # A committed confirmation is authorization to execute even if its HTTP
        # response was lost. Database.initialize already terminalizes old RUNNING runs.
        for owner, run_id in await asyncio.to_thread(self._queued_runs):
            self.schedule(owner, run_id)
        self._dispatcher = asyncio.create_task(self._dispatch_queued(), name="analysis-dispatcher")

    async def _dispatch_queued(self) -> None:
        # Also recover a confirmation whose request was cancelled between its
        # SQLite commit and explicit schedule call. This never replays RUNNING work.
        while not self._stopping:
            await asyncio.sleep(1)
            try:
                queued = await asyncio.to_thread(self._queued_runs)
            except sqlite3.Error:
                continue
            for owner, run_id in queued:
                if self._stopping:
                    return
                self.schedule(owner, run_id)

    def schedule(self, owner: str, run_id: str) -> None:
        """Called on the app event loop after the owner-scoped confirmation commits."""
        if self._stopping:
            raise AnalysisRunStateError("The application is stopping. Please reconnect after it restarts.")
        if run_id in self._tasks:
            return
        task = asyncio.create_task(self._execute(owner, run_id), name=f"analysis:{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda finished: self._forget(run_id, finished))

    def _forget(self, run_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(run_id) is task:
            self._tasks.pop(run_id, None)
        if not task.cancelled():
            task.exception()  # Retrieve failures; never log exception text or model data.

    async def _execute(self, owner: str, run_id: str) -> None:
        try:
            await self._orchestrator.execute(owner, run_id)
        except asyncio.CancelledError:
            raise
        except AnalysisRunNotFoundError:
            return
        except AnalysisRunStateError:
            # A duplicate request can observe an already claimed or terminal run.
            await asyncio.to_thread(self._fail_queued_run, run_id, "INTERNAL_FAILED")
        except Exception:
            # The orchestrator terminalizes failures after claiming; this also covers
            # a failure before the claim without leaving a queued card indefinitely.
            await asyncio.to_thread(self._fail_queued_run, run_id, "INTERNAL_FAILED")

    async def stop(self) -> None:
        self._stopping = True
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            await asyncio.gather(self._dispatcher, return_exceptions=True)
        held = tuple(self._tasks.items())
        for _, task in held:
            task.cancel()
        if held:
            await asyncio.gather(*(task for _, task in held), return_exceptions=True)
        # A task cancelled before its coroutine begins never reaches execute's handler.
        for run_id, _ in held:
            await asyncio.to_thread(self._fail_queued_run, run_id, "ANALYSIS_INTERRUPTED")

    def _queued_runs(self) -> list[tuple[str, str]]:
        with self._database.connect() as connection:
            return [(str(owner), str(run_id)) for owner, run_id in connection.execute(
                "SELECT chats.owner_username, runs.run_id FROM runs "
                "JOIN chats ON chats.chat_id = runs.chat_id "
                "WHERE runs.status = 'queued' ORDER BY runs.created_at, runs.run_id"
            )]

    def _fail_queued_run(self, run_id: str, code: str) -> None:
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now_iso()
            updated = connection.execute(
                "UPDATE runs SET status = ?, error_code = ?, error_message = ?, updated_at = ? "
                "WHERE run_id = ? AND status = 'queued'",
                (code, code, RUN_ERROR_MESSAGES[code], now, run_id),
            )
            if updated.rowcount != 1:
                return
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), -1) + 1 FROM run_events WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO run_events (event_id, run_id, sequence, event_type, payload_json, created_at) "
                "VALUES (?, ?, ?, 'failed', ?, ?)",
                (str(uuid4()), run_id, sequence, json.dumps({"status": "failed", "code": code}), now),
            )
            update_run_progress_message(
                connection, run_id,
                status="cancelled" if code == "ANALYSIS_INTERRUPTED" else "failed",
                current_stage=code, completed_stages=completed_run_stages(connection, run_id),
            )
            append_run_message(
                connection, run_id, kind="error", text=RUN_ERROR_MESSAGES[code],
                payload={"run_id": run_id, "code": code},
            )

    @staticmethod
    def _owned_run(connection: sqlite3.Connection, owner: str, run_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT runs.*, reports.report_id FROM runs "
            "JOIN chats ON chats.chat_id = runs.chat_id "
            "LEFT JOIN reports ON reports.run_id = runs.run_id "
            "WHERE runs.run_id = ? AND chats.owner_username = ?", (run_id, owner)
        ).fetchone()
        if row is None:
            raise AnalysisRunNotFoundError(run_id)
        return row

    @staticmethod
    def _public_events(connection: sqlite3.Connection, run_id: str) -> list[tuple[int, RunEventOut]]:
        events: list[tuple[int, RunEventOut]] = []
        completed: set[str] = set()
        for row in connection.execute(
            "SELECT sequence, event_type, payload_json FROM run_events "
            "WHERE run_id = ? ORDER BY sequence", (run_id,)
        ):
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            stage = str(row["event_type"])
            state = payload.get("status")
            if stage in ANALYSIS_STAGE_MESSAGES and state in {"running", "complete"}:
                if state == "complete":
                    completed.add(stage)
                report_id = payload.get("report_id") if stage == "validation" and state == "complete" else None
                message = "Analysis complete." if report_id else ANALYSIS_STAGE_MESSAGES[stage]
            elif state == "failed" and payload.get("code") in RUN_ERROR_MESSAGES:
                code = str(payload["code"])
                state = "cancelled" if code == "ANALYSIS_INTERRUPTED" else "failed"
                message = RUN_ERROR_MESSAGES[code]
                report_id = None
            else:
                continue
            events.append((int(row["sequence"]), RunEventOut(
                run_id=run_id, stage=stage, status=state, message=message,
                completed_stages=len(completed), total_stages=ANALYSIS_TOTAL_STAGES,
                report_id=report_id if isinstance(report_id, str) else None,
            )))
        return events

    def get_events(self, owner: str, run_id: str, after: int) -> tuple[list[tuple[int, RunEventOut]], bool]:
        with self._database.connect() as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            run = self._owned_run(connection, owner, run_id)
            events = self._public_events(connection, run_id)
            terminal = run["status"] not in {"queued", "RUNNING"}
        return [item for item in events if item[0] > after], terminal

    def get_run(self, owner: str, run_id: str) -> RunDetailOut:
        with self._database.connect() as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            run = self._owned_run(connection, owner, run_id)
            events = self._public_events(connection, run_id)
        state = str(run["status"])
        public_state = {"queued": "queued", "RUNNING": "running", "COMPLETE": "complete", "ANALYSIS_INTERRUPTED": "cancelled"}.get(state, "failed")
        latest = events[-1] if events else None
        message = RUN_ERROR_MESSAGES.get(state, "Analysis complete." if state == "COMPLETE" else "Waiting for local analysis")
        if state == "RUNNING" and latest:
            message = latest[1].message
        return RunDetailOut(
            run_id=run_id, status=public_state,
            current_stage=latest[1].stage if latest else None,
            completed_stages=latest[1].completed_stages if latest else 0,
            total_stages=ANALYSIS_TOTAL_STAGES, report_id=run["report_id"],
            error_code=run["error_code"], message=message,
            last_event_id=latest[0] if latest else -1,
        )

    def get_report(self, owner: str, report_id: str) -> ReportDetailOut:
        with self._database.connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT reports.report_json, reports.created_at, runs.evidence_context_json "
                "FROM reports JOIN runs ON runs.run_id = reports.run_id "
                "JOIN chats ON chats.chat_id = runs.chat_id "
                "WHERE reports.report_id = ? AND chats.owner_username = ? AND runs.status = 'COMPLETE'",
                (report_id, owner),
            ).fetchone()
        if row is None:
            raise AnalysisRunNotFoundError(report_id)
        report = MvpReportV1.model_validate_json(row["report_json"])
        evidence = EvidenceContextV1.model_validate_json(row["evidence_context_json"])
        if report.run_id != evidence.run_id or report.snapshot_id != evidence.fact_pack.snapshot_id:
            raise AnalysisRunStateError("The saved report's evidence is unavailable.")
        passages = {item.evidence_id: item for item in evidence.fact_pack.passages}
        sources = {item.source_id: item for item in evidence.fact_pack.sources}
        details: list[ReportCitationDetailOut] = []
        for citation in report.citations:
            passage = passages.get(citation.evidence_ref)
            source = sources.get(passage.source_id) if passage else sources.get(citation.evidence_ref)
            if source is None:
                raise AnalysisRunStateError("The saved report's citation is unavailable.")
            # A source-level citation does not select any particular passage. Expose
            # its frozen metadata without inventing a quoted span or page locator.
            details.append(ReportCitationDetailOut(
                evidence_ref=citation.evidence_ref,
                citation_scope="passage" if passage else "source",
                source_title=source.title,
                publisher=source.publisher, release_date=source.release_date,
                licence=source.licence,
                locator=passage.section if passage else source.source_uri,
                text=passage.text if passage else "",
            ))
        return ReportDetailOut(
            report_id=report_id, created_at=row["created_at"],
            report=report.model_dump(mode="json"), citation_details=details,
        )
