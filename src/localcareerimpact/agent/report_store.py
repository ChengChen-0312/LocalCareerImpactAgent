"""Final-report persistence; this capability is held only by the final branch."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from uuid import uuid4

from localcareerimpact.app.database import (
    ANALYSIS_TOTAL_STAGES,
    Database,
    append_run_message,
    update_run_progress_message,
    utc_now_iso,
)

from .contracts import MvpReportV1


class ReportPersistenceError(RuntimeError):
    """Raised when a report cannot be atomically bound to its run."""


@dataclass(frozen=True, slots=True)
class SavedReport:
    report_id: str
    run_id: str
    created_at: str


class ReportStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def save_report(self, report: MvpReportV1) -> SavedReport:
        """Atomically save one validated report and complete its frozen run."""

        report_id = str(uuid4())
        created_at = utc_now_iso()
        with self._database.connect() as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT snapshot_id, status FROM runs WHERE run_id = ?",
                (report.run_id,),
            ).fetchone()
            if (
                run is None
                or run["status"] != "RUNNING"
                or run["snapshot_id"] != report.snapshot_id
            ):
                raise ReportPersistenceError(
                    "The final report does not match a running frozen analysis run."
                )
            if connection.execute(
                "SELECT 1 FROM reports WHERE run_id = ?", (report.run_id,)
            ).fetchone() is not None:
                raise ReportPersistenceError("The analysis run already has a report.")

            connection.execute(
                """
                INSERT INTO reports (
                    report_id, run_id, snapshot_id, language, report_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    report.run_id,
                    report.snapshot_id,
                    report.language,
                    report.model_dump_json(exclude_none=False),
                    created_at,
                ),
            )
            event_sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), -1) + 1 FROM run_events WHERE run_id = ?",
                    (report.run_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO run_events (
                    event_id, run_id, sequence, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, 'validation', ?, ?)
                """,
                (
                    str(uuid4()),
                    report.run_id,
                    event_sequence,
                    json.dumps(
                        {"status": "complete", "report_id": report_id},
                        separators=(",", ":"),
                    ),
                    created_at,
                ),
            )
            connection.execute(
                """
                UPDATE runs
                SET status = 'COMPLETE', error_code = NULL, error_message = NULL,
                    updated_at = ?
                WHERE run_id = ? AND status = 'RUNNING'
                """,
                (created_at, report.run_id),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise ReportPersistenceError("The report run could not be completed.")
            update_run_progress_message(
                connection,
                report.run_id,
                status="complete",
                current_stage="Analysis complete",
                completed_stages=ANALYSIS_TOTAL_STAGES,
            )
            append_run_message(
                connection, report.run_id, kind="report", text=report.title,
                payload={"run_id": report.run_id, "report_id": report_id},
            )
        return SavedReport(
            report_id=report_id,
            run_id=report.run_id,
            created_at=created_at,
        )


__all__ = ["ReportPersistenceError", "ReportStore", "SavedReport"]
