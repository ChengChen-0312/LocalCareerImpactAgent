import { useEffect, useRef, useState } from "react";

import { ApiError, api } from "./api";
import type { RunDetail, RunEvent } from "./types";

const STAGES = [
  ["retrieval", "Finding local evidence"],
  ["draft", "Preparing the draft"],
  ["evidence_review", "Checking evidence"],
  ["boundary_review", "Checking reasoning boundaries"],
  ["safety_review", "Checking safety and language"],
  ["resolution_decision", "Deciding reviewer suggestions"],
  ["revised_claims", "Revising the analysis"],
  ["report_narratives", "Writing the report"],
  ["validation", "Validating and saving"],
] as const;

function terminal(run: RunDetail): boolean {
  return run.status === "complete" || run.status === "failed" || run.status === "cancelled";
}

export function RunProgressCard({
  runId,
  onSettled,
  onUnauthorized,
}: {
  runId: string;
  onSettled: () => void;
  onUnauthorized: () => void;
}) {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [connection, setConnection] = useState("Connecting to analysis…");
  const callbacks = useRef({ onSettled, onUnauthorized });
  callbacks.current = { onSettled, onUnauthorized };

  useEffect(() => {
    let active = true;
    let source: EventSource | null = null;
    let retryTimer: number | null = null;
    let lastEventId = -1;
    let settled = false;
    let fetching = false;
    let requestController: AbortController | null = null;

    function clearRetry() {
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      retryTimer = null;
    }

    function finish() {
      source?.close();
      source = null;
      clearRetry();
      setConnection("");
      if (!settled) {
        settled = true;
        callbacks.current.onSettled();
      }
    }

    function retrySnapshot() {
      if (!active || settled || retryTimer !== null) return;
      retryTimer = window.setTimeout(() => {
        retryTimer = null;
        void refresh();
      }, 5_000);
    }

    function connect() {
      if (!active || settled || source !== null) return;
      source = api.runEvents(runId, lastEventId);
      source.onopen = () => {
        if (!active) return;
        setConnection("");
      };
      source.addEventListener("progress", (rawEvent) => {
        if (!active || settled) return;
        const message = rawEvent as MessageEvent<string>;
        const eventId = Number(message.lastEventId);
        if (!Number.isInteger(eventId) || eventId <= lastEventId) return;
        let event: RunEvent;
        try {
          event = JSON.parse(message.data) as RunEvent;
        } catch {
          setConnection("Reconnecting to analysis…");
          retrySnapshot();
          return;
        }
        if (
          event === null || typeof event !== "object" || event.run_id !== runId ||
          !["running", "complete", "failed", "cancelled"].includes(event.status) ||
          typeof event.stage !== "string" || typeof event.message !== "string" ||
          !Number.isInteger(event.completed_stages) || !Number.isInteger(event.total_stages)
        ) {
          retrySnapshot();
          return;
        }
        lastEventId = eventId;
        const isComplete = event.stage === "validation" && event.status === "complete";
        const status = isComplete ? "complete"
          : event.status === "failed" || event.status === "cancelled" ? event.status : "running";
        setRun((previous) => ({
          run_id: runId,
          status,
          current_stage: event.stage,
          completed_stages: event.completed_stages,
          total_stages: event.total_stages,
          report_id: event.report_id,
          error_code: previous?.error_code ?? null,
          message: event.message,
          last_event_id: eventId,
        }));
        if (status === "complete" || status === "failed" || status === "cancelled") finish();
      });
      source.onerror = () => {
        if (!active || settled) return;
        setConnection("Reconnecting to analysis. Your run continues on this computer.");
        // EventSource replays persisted events with Last-Event-ID. The snapshot also
        // detects a terminal run or an expired session when a stream cannot reopen.
        retrySnapshot();
      };
    }

    async function refresh() {
      if (!active || fetching || settled) return;
      fetching = true;
      const controller = new AbortController();
      requestController = controller;
      const timeout = window.setTimeout(() => controller.abort(), 8_000);
      try {
        const detail = await api.getRun(runId, controller.signal);
        if (!active || settled) return;
        if (detail.last_event_id >= lastEventId) {
          lastEventId = detail.last_event_id;
          setRun(detail);
        }
        if (terminal(detail)) finish();
        else {
          connect();
        }
      } catch (error) {
        if (!active || settled) return;
        if (error instanceof ApiError && error.status === 401) {
          settled = true;
          source?.close();
          callbacks.current.onUnauthorized();
          return;
        }
        if (error instanceof ApiError && error.status === 404) {
          settled = true;
          source?.close();
          setConnection("This analysis is unavailable. Start a new conversation to try again.");
          return;
        }
        setConnection("Unable to reconnect yet. Your saved progress will return when the connection recovers.");
        retrySnapshot();
      } finally {
        window.clearTimeout(timeout);
        if (requestController === controller) requestController = null;
        fetching = false;
        // A quiet or repeatedly reopened stream must not starve the HTTP fallback.
        retrySnapshot();
      }
    }

    function resume() {
      if (document.visibilityState === "visible") void refresh();
    }
    window.addEventListener("online", resume);
    document.addEventListener("visibilitychange", resume);
    setRun(null);
    setConnection("Connecting to analysis…");
    void refresh();
    return () => {
      active = false;
      window.removeEventListener("online", resume);
      document.removeEventListener("visibilitychange", resume);
      requestController?.abort();
      source?.close();
      clearRetry();
    };
  }, [runId]);

  const completed = Math.min(run?.completed_stages ?? 0, STAGES.length);
  const heading = run?.status === "complete" ? "Analysis complete"
    : run?.status === "failed" ? "Analysis could not finish"
      : run?.status === "cancelled" ? "Analysis interrupted"
        : run?.status === "queued" ? "Analysis queued" : "Analysis in progress";

  return (
    <details className={`analysis-progress ${run?.status ?? "queued"}`} open>
      <summary>
        <strong>{heading}</strong>
        <span>{completed} / {STAGES.length}</span>
      </summary>
      <progress max={STAGES.length} value={completed} aria-label="Completed analysis stages" />
      {run?.message && <p className="progress-message">{run.message}</p>}
      {connection && <p className="progress-connection" role="status">{connection}</p>}
      <ol className="analysis-stage-list">
        {STAGES.map(([stage, label], index) => {
          const done = index < completed;
          const current = run?.current_stage === stage && !done;
          return (
            <li key={stage} className={done ? "done" : current ? "current" : ""}>
              <span aria-hidden="true">{done ? "✓" : current ? "•" : "○"}</span>
              {label}
              <span className="sr-only">{done ? " complete" : current ? " current" : " pending"}</span>
            </li>
          );
        })}
      </ol>
      {(run?.status === "failed" || run?.status === "cancelled") && (
        <p className="analysis-recovery">No final report was saved. Add or correct your information and confirm a new profile to try again.</p>
      )}
    </details>
  );
}
