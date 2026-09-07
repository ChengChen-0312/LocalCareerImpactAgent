import { ChangeEvent, FormEvent, useEffect, useRef, useState } from "react";

import { ApiError, api } from "./api";
import { ReportCard } from "./ReportCard";
import { RunProgressCard } from "./RunProgressCard";
import type {
  ChatMessage,
  ChatSummary,
  Health,
  KnowledgeCitation,
  KnowledgeDocument,
  KnowledgeStatus,
  ProfileCard,
} from "./types";

type RequestStatus = "checking" | "idle" | "loading" | "sending" | "error";
type HealthConnectionStatus = "checking" | "connected" | "unavailable";

const ADMIN_USERNAME = "admin";
const HEALTH_POLL_INTERVAL_MS = 5_000;
const HEALTH_REQUEST_TIMEOUT_MS = 4_000;
const KNOWLEDGE_ACTIVE_POLL_INTERVAL_MS = 1_000;
const KNOWLEDGE_IDLE_POLL_INTERVAL_MS = 5_000;
const PDF_MAX_BYTES = 10 * 1024 * 1024;
const TXT_MAX_BYTES = 2 * 1024 * 1024;
const AUDIO_MAX_BYTES = 25 * 1024 * 1024;
const RECORDING_MAX_MILLISECONDS = 15 * 60 * 1_000;
const MAX_ATTACHMENTS = 8;
const KNOWLEDGE_ACCEPT = ".pdf,application/pdf,.txt,text/plain,.csv,text/csv,.json,application/json";
const STARTING_MODEL_HEALTH: Health = {
  database: "unavailable",
  qwen30b: "starting",
  qwen4b: "starting",
};

function shortTime(timestamp: string): string {
  return new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(timestamp));
}

type AttachmentKind = "document" | "audio" | "recording";

interface QueuedAttachment {
  id: string;
  file: File;
  kind: AttachmentKind;
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function profileFromPayload(payload: Record<string, unknown> | null): ProfileCard | null {
  if (
    payload === null ||
    typeof payload.profile_id !== "string" ||
    typeof payload.confirmed !== "boolean" ||
    !isStringArray(payload.occupation_candidates) ||
    (payload.region !== null && typeof payload.region !== "string") ||
    (payload.years_of_experience !== null && typeof payload.years_of_experience !== "number") ||
    !isStringArray(payload.responsibilities) ||
    !isStringArray(payload.skills) ||
    (payload.industry_context !== null && typeof payload.industry_context !== "string") ||
    !isStringArray(payload.goals)
  ) {
    return null;
  }
  return {
    profile_id: payload.profile_id,
    confirmed: payload.confirmed,
    occupation_candidates: payload.occupation_candidates,
    region: payload.region,
    years_of_experience: payload.years_of_experience,
    responsibilities: payload.responsibilities,
    skills: payload.skills,
    industry_context: payload.industry_context,
    goals: payload.goals,
  };
}

function mergeChatMessages(current: ChatMessage[], incoming: ChatMessage[]): ChatMessage[] {
  const merged = new Map(current.map((message) => [message.message_id, message]));
  const progressRank: Record<string, number> = { queued: 0, running: 1, complete: 2, failed: 2, cancelled: 2 };
  const stages = ["retrieval", "draft", "evidence_review", "boundary_review", "safety_review",
    "resolution_decision", "revised_claims", "report_narratives", "validation"];
  for (const message of incoming) {
    const existing = merged.get(message.message_id);
    // A response can arrive after a newer terminal refresh has already included it.
    // Confirmation and progress only move forward; message text is otherwise immutable.
    if (existing?.kind === "profile" && existing.payload?.confirmed === true &&
      message.payload?.confirmed !== true) continue;
    if (existing?.kind === "progress" && message.kind === "progress") {
      const oldStatus = typeof existing.payload?.status === "string" ? existing.payload.status : "queued";
      const newStatus = typeof message.payload?.status === "string" ? message.payload.status : "queued";
      const oldCount = typeof existing.payload?.completed_stages === "number" ? existing.payload.completed_stages : 0;
      const newCount = typeof message.payload?.completed_stages === "number" ? message.payload.completed_stages : 0;
      const oldStage = stages.indexOf(String(existing.payload?.current_stage));
      const newStage = stages.indexOf(String(message.payload?.current_stage));
      if ((progressRank[oldStatus] ?? 0) > (progressRank[newStatus] ?? 0) ||
        (progressRank[oldStatus] === progressRank[newStatus] && (oldCount > newCount ||
          (oldCount === newCount && newStage >= 0 && oldStage > newStage)))) continue;
    }
    merged.set(message.message_id, message);
  }
  return [...merged.values()].sort((left, right) => left.sequence - right.sequence);
}

function listFromEditor(value: string): string[] {
  return value
    .split("\n")
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
}

function formatElapsed(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

function ProfileEditor({
  profile,
  disabled,
  current,
  onConfirm,
}: {
  profile: ProfileCard;
  disabled: boolean;
  current: boolean;
  onConfirm: (profile: ProfileCard) => void;
}) {
  const [draft, setDraft] = useState(profile);

  useEffect(() => {
    setDraft(profile);
  }, [profile.profile_id, profile.confirmed]);

  function setList(
    field: "occupation_candidates" | "responsibilities" | "skills" | "goals",
    value: string,
  ) {
    setDraft((existing) => ({ ...existing, [field]: listFromEditor(value) }));
  }

  return (
    <section className={`profile-card${profile.confirmed ? " confirmed" : ""}`}>
      <div className="profile-card-heading">
        <strong>Candidate profile</strong>
        <span>{profile.confirmed ? "Confirmed" : current ? "Ready for review" : "Superseded"}</span>
      </div>
      <div className="profile-fields">
        <label className="profile-wide">
          Occupation candidates
          <textarea
            value={draft.occupation_candidates.join("\n")}
            onChange={(event) => setList("occupation_candidates", event.target.value)}
            disabled={profile.confirmed || !current}
            rows={2}
          />
        </label>
        <label>
          Region
          <input
            value={draft.region ?? ""}
            onChange={(event) => setDraft((existing) => ({
              ...existing,
              region: event.target.value || null,
            }))}
            disabled={profile.confirmed || !current}
          />
        </label>
        <label>
          Years of experience
          <input
            type="number"
            min="0"
            max="80"
            step="0.5"
            value={draft.years_of_experience ?? ""}
            onChange={(event) => setDraft((existing) => ({
              ...existing,
              years_of_experience: event.target.value === "" ? null : Number(event.target.value),
            }))}
            disabled={profile.confirmed || !current}
          />
        </label>
        <label className="profile-wide">
          Responsibilities
          <textarea
            value={draft.responsibilities.join("\n")}
            onChange={(event) => setList("responsibilities", event.target.value)}
            disabled={profile.confirmed || !current}
            rows={3}
          />
        </label>
        <label className="profile-wide">
          Skills
          <textarea
            value={draft.skills.join("\n")}
            onChange={(event) => setList("skills", event.target.value)}
            disabled={profile.confirmed || !current}
            rows={3}
          />
        </label>
        <label className="profile-wide">
          Industry context
          <textarea
            value={draft.industry_context ?? ""}
            onChange={(event) => setDraft((existing) => ({
              ...existing,
              industry_context: event.target.value || null,
            }))}
            disabled={profile.confirmed || !current}
            rows={2}
          />
        </label>
        <label className="profile-wide">
          Goals
          <textarea
            value={draft.goals.join("\n")}
            onChange={(event) => setList("goals", event.target.value)}
            disabled={profile.confirmed || !current}
            rows={2}
          />
        </label>
      </div>
      {!profile.confirmed && current && (
        <button
          type="button"
          className="confirm-profile-button"
          disabled={disabled || draft.occupation_candidates.length === 0}
          onClick={() => onConfirm(draft)}
        >
          Confirm and analyse
        </button>
      )}
    </section>
  );
}

function KnowledgeWorkspace({
  username,
  onLogout,
}: {
  username: string;
  onLogout: () => void;
}) {
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [knowledgeStatus, setKnowledgeStatus] = useState<KnowledgeStatus | null>(null);
  const [citation, setCitation] = useState<KnowledgeCitation | null>(null);
  const [busy, setBusy] = useState<"upload" | "rebuild" | null>(null);
  const [errorMessage, setErrorMessage] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const mountedRef = useRef(true);
  const documentsIdentityRef = useRef(0);
  const statusIdentityRef = useRef(0);
  const statusControllerRef = useRef<AbortController | null>(null);
  const serverRebuildingRef = useRef(false);
  const controllersRef = useRef(new Set<AbortController>());

  function trackedController(): AbortController {
    const controller = new AbortController();
    controllersRef.current.add(controller);
    return controller;
  }

  function finishController(controller: AbortController) {
    controllersRef.current.delete(controller);
  }

  function isAbortError(error: unknown): boolean {
    return error instanceof DOMException && error.name === "AbortError";
  }

  function showError(error: unknown) {
    if (!mountedRef.current || isAbortError(error)) return;
    setErrorMessage(error instanceof Error ? error.message : "The knowledge request failed.");
  }

  async function refreshDocuments() {
    const identity = documentsIdentityRef.current + 1;
    documentsIdentityRef.current = identity;
    const controller = trackedController();
    try {
      const documentResult = await api.listKnowledgeDocuments(controller.signal);
      if (!mountedRef.current || documentsIdentityRef.current !== identity) return;
      setDocuments(documentResult.documents);
    } catch (error) {
      showError(error);
    } finally {
      finishController(controller);
    }
  }

  async function refreshStatus() {
    const identity = statusIdentityRef.current + 1;
    statusIdentityRef.current = identity;
    statusControllerRef.current?.abort();
    const controller = trackedController();
    statusControllerRef.current = controller;
    try {
      const result = await api.knowledgeStatus(controller.signal);
      if (!mountedRef.current || statusIdentityRef.current !== identity) return;
      const rebuildFinished = serverRebuildingRef.current && !result.rebuilding;
      serverRebuildingRef.current = result.rebuilding;
      setKnowledgeStatus(result);
      if (rebuildFinished) void refreshDocuments();
    } catch (error) {
      showError(error);
    } finally {
      if (statusControllerRef.current === controller) {
        statusControllerRef.current = null;
      }
      finishController(controller);
    }
  }

  async function refresh() {
    await Promise.all([refreshStatus(), refreshDocuments()]);
  }

  useEffect(() => {
    mountedRef.current = true;
    void refresh();
    const parameters = new URLSearchParams(window.location.search);
    const snapshotId = parameters.get("snapshot");
    const chunkId = parameters.get("chunk");
    if (snapshotId && chunkId) {
      const controller = trackedController();
      void api.knowledgeCitation(snapshotId, chunkId, controller.signal)
        .then((result) => {
          if (mountedRef.current) setCitation(result);
        })
        .catch(showError)
        .finally(() => finishController(controller));
    }
    return () => {
      mountedRef.current = false;
      documentsIdentityRef.current += 1;
      statusIdentityRef.current += 1;
      statusControllerRef.current = null;
      for (const controller of controllersRef.current) controller.abort();
      controllersRef.current.clear();
    };
  }, []);

  const rebuilding = busy === "rebuild" || knowledgeStatus?.rebuilding === true;
  const mutationsDisabled = busy !== null || knowledgeStatus === null || rebuilding;

  useEffect(() => {
    const interval = rebuilding
      ? KNOWLEDGE_ACTIVE_POLL_INTERVAL_MS
      : KNOWLEDGE_IDLE_POLL_INTERVAL_MS;
    const timer = window.setInterval(() => void refreshStatus(), interval);
    return () => window.clearInterval(timer);
  }, [rebuilding]);

  async function handleUpload(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file || mutationsDisabled) return;
    setBusy("upload");
    setErrorMessage("");
    const controller = trackedController();
    try {
      await api.uploadKnowledgeDocument(file, controller.signal);
      if (!mountedRef.current) return;
      await refresh();
    } catch (error) {
      showError(error);
    } finally {
      finishController(controller);
      if (mountedRef.current) setBusy(null);
    }
  }

  async function handleRebuild() {
    if (mutationsDisabled) return;
    setBusy("rebuild");
    setErrorMessage("");
    const controller = trackedController();
    try {
      const result = await api.rebuildKnowledge(controller.signal);
      if (!mountedRef.current) return;
      statusIdentityRef.current += 1;
      statusControllerRef.current?.abort();
      statusControllerRef.current = null;
      serverRebuildingRef.current = result.rebuilding;
      setKnowledgeStatus(result);
      await refresh();
    } catch (error) {
      showError(error);
    } finally {
      finishController(controller);
      if (mountedRef.current) setBusy(null);
    }
  }

  const latest = knowledgeStatus?.latest_rebuild ?? null;
  const active = knowledgeStatus?.active_snapshot ?? null;
  const acceptedCount = documents.filter((document) => document.status === "accepted").length;
  const rejectedCount = documents.filter((document) => document.status === "rejected").length;

  return (
    <div className="app-shell knowledge-shell">
      <aside className="sidebar">
        <div className="sidebar-top">
          <div className="wordmark"><span>LC</span> Career Impact</div>
          <a className="new-chat-button knowledge-back" href="/">
            <span aria-hidden="true">←</span> Back to chats
          </a>
        </div>
        <div className="knowledge-sidebar-note">
          Sources are retained locally. Rebuilds happen only when an administrator starts one.
        </div>
        <div className="sidebar-footer">
          <div className="account-row">
            <div className="avatar">{username.slice(0, 1).toUpperCase()}</div>
            <div><strong>{username}</strong><span>Administrator</span></div>
            <button onClick={onLogout} aria-label="Sign out" title="Sign out">↗</button>
          </div>
        </div>
      </aside>

      <main className="knowledge-panel">
        <header className="knowledge-header">
          <div>
            <p className="eyebrow">Administrator · local sources</p>
            <h1>Knowledge Base</h1>
            <p>Upload retained evidence, then manually publish one immutable retrieval snapshot.</p>
          </div>
          <div className="knowledge-actions">
            <input
              ref={inputRef}
              type="file"
              accept={KNOWLEDGE_ACCEPT}
              hidden
              onChange={(event) => void handleUpload(event)}
            />
            <button
              className="secondary-button"
              onClick={() => inputRef.current?.click()}
              disabled={mutationsDisabled}
            >{busy === "upload" ? "Uploading…" : "Upload source"}</button>
            <button
              className="primary-button knowledge-rebuild"
              onClick={() => void handleRebuild()}
              disabled={mutationsDisabled}
            >{rebuilding ? "Rebuilding…" : "Rebuild snapshot"}</button>
          </div>
        </header>

        {errorMessage && <p className="knowledge-error" role="alert">{errorMessage}</p>}

        <section className="knowledge-summary" aria-label="Knowledge status">
          <article>
            <span>Active snapshot</span>
            <strong>{active ? active.snapshot_id.slice(7, 19) : "None"}</strong>
            <small>{active ? `${active.total_documents} sources · ${active.total_chunks} chunks` : "Build a snapshot before retrieval"}</small>
          </article>
          <article>
            <span>Accepted</span>
            <strong>{acceptedCount}</strong>
            <small>PDF, UTF-8 TXT, CSV, and JSON</small>
          </article>
          <article className={rejectedCount > 0 ? "summary-warning" : ""}>
            <span>Rejected</span>
            <strong>{rejectedCount}</strong>
            <small>{rejectedCount > 0 ? "Resolve before the next rebuild" : "No rejected sources"}</small>
          </article>
        </section>

        {latest && (
          <section className={`rebuild-card ${latest.status}`} aria-live="polite">
            <div>
              <span className="status-badge">{latest.status}</span>
              <strong>Latest rebuild · {latest.stage}</strong>
            </div>
            <p>
              {latest.processed_documents}/{latest.total_documents} sources · {latest.processed_chunks}/{latest.total_chunks} chunks
            </p>
            {latest.error_message && (
              <p className="rebuild-failure">
                {latest.failure_source ? `${latest.failure_source}: ` : ""}{latest.error_message}
              </p>
            )}
          </section>
        )}

        {citation && (
          <section className="citation-inspector" aria-labelledby="citation-title">
            <div>
              <p className="eyebrow">Resolved snapshot citation</p>
              <h2 id="citation-title">{citation.title}</h2>
              <span>{citation.source_label} · {citation.locator}</span>
              <span>
                {citation.publisher} · release {citation.release_date ?? "unavailable"} · {citation.licence} · {citation.jurisdiction} · {citation.authority_class}
              </span>
            </div>
            <pre>{citation.text}</pre>
          </section>
        )}

        <section className="knowledge-documents" aria-labelledby="knowledge-documents-title">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Retained inbox</p>
              <h2 id="knowledge-documents-title">Documents</h2>
            </div>
            <button className="text-button" onClick={() => void refresh()} disabled={busy !== null}>Refresh</button>
          </div>
          {documents.length === 0 ? (
            <div className="knowledge-empty">No knowledge sources have been uploaded or scanned yet.</div>
          ) : (
            <div className="document-table">
              {documents.map((document) => (
                <article key={document.source_label} className={`document-row ${document.status}`}>
                  <span className="document-type">{document.source_type.toUpperCase()}</span>
                  <div>
                    <strong>{document.filename}</strong>
                    <small>{document.source_label} · {(document.size_bytes / 1024).toFixed(1)} KiB · {document.chunk_count} chunks</small>
                    <small>{document.publisher} · release {document.release_date ?? "unavailable"} · {document.jurisdiction} · {document.authority_class}</small>
                    {document.error_message && <p>{document.error_message}</p>}
                  </div>
                  <span className="document-status">{document.status}</span>
                </article>
              ))}
            </div>
          )}
        </section>
      </main>
    </div>
  );
}

function App() {
  const [username, setUsername] = useState<string | null>(null);
  const [selectedChatId, setSelectedChatId] = useState<string | null>(null);
  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [composerText, setComposerText] = useState("");
  const [attachments, setAttachments] = useState<QueuedAttachment[]>([]);
  const [recording, setRecording] = useState(false);
  const [recordingElapsed, setRecordingElapsed] = useState(0);
  const [requestStatus, setRequestStatus] = useState<RequestStatus>("checking");
  const [errorMessage, setErrorMessage] = useState("");
  const [modelHealth, setModelHealth] = useState<Health>(STARTING_MODEL_HEALTH);
  const [healthConnection, setHealthConnection] =
    useState<HealthConnectionStatus>("checking");
  const streamEndRef = useRef<HTMLDivElement>(null);
  const documentInputRef = useRef<HTMLInputElement>(null);
  const audioInputRef = useRef<HTMLInputElement>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const recordingChunksRef = useRef<Blob[]>([]);
  const recordingStartedAtRef = useRef(0);
  const recordingIntervalRef = useRef<number | null>(null);
  const recordingTimeoutRef = useRef<number | null>(null);
  const discardRecordingRef = useRef(false);
  const recordingIdentityRef = useRef(0);
  const microphoneRequestRef = useRef(false);
  const selectedChatIdRef = useRef<string | null>(null);
  const requestStatusRef = useRef<RequestStatus>("checking");
  const sessionGenerationRef = useRef(0);
  const selectionRequestRef = useRef(0);
  const detailControllerRef = useRef<AbortController | null>(null);
  const healthControllerRef = useRef<AbortController | null>(null);
  const healthPollTimerRef = useRef<number | null>(null);
  const healthRequestRef = useRef(0);
  const inFlightControllersRef = useRef(new Set<AbortController>());
  const composerCodePoints = Array.from(composerText).length;
  const actionBusy = requestStatus === "checking" || requestStatus === "loading" || requestStatus === "sending";
  const currentProfileMessageId = [...messages]
    .reverse()
    .find((message) => message.kind === "profile")?.message_id ?? null;

  useEffect(() => {
    const generation = sessionGenerationRef.current;
    void restoreSession(generation);
    startModelHealthPolling(generation);
    return () => {
      stopModelHealthPolling();
      invalidateInFlight();
      stopRecording(true);
    };
  }, []);

  useEffect(() => {
    streamEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  function setStatus(nextStatus: RequestStatus) {
    requestStatusRef.current = nextStatus;
    setRequestStatus(nextStatus);
  }

  function setSelectedChat(chatId: string | null) {
    selectedChatIdRef.current = chatId;
    setSelectedChatId(chatId);
  }

  function startRequest(): AbortController {
    const controller = new AbortController();
    inFlightControllersRef.current.add(controller);
    return controller;
  }

  function finishRequest(controller: AbortController) {
    inFlightControllersRef.current.delete(controller);
  }

  function invalidateSelection() {
    selectionRequestRef.current += 1;
    const controller = detailControllerRef.current;
    if (controller) {
      controller.abort();
      inFlightControllersRef.current.delete(controller);
      detailControllerRef.current = null;
    }
  }

  function invalidateInFlight(): number {
    sessionGenerationRef.current += 1;
    selectionRequestRef.current += 1;
    for (const controller of inFlightControllersRef.current) {
      controller.abort();
    }
    inFlightControllersRef.current.clear();
    detailControllerRef.current = null;
    return sessionGenerationRef.current;
  }

  function changeUser(nextUsername: string | null, nextStatus: RequestStatus): number {
    const generation = invalidateInFlight();
    startModelHealthPolling(generation);
    setUsername(nextUsername);
    setSelectedChat(null);
    setChats([]);
    setMessages([]);
    setComposerText("");
    setAttachments([]);
    stopRecording(true);
    setErrorMessage("");
    setStatus(nextStatus);
    return generation;
  }

  function isCurrentSession(generation: number): boolean {
    return sessionGenerationRef.current === generation;
  }

  function isAbortError(error: unknown): boolean {
    return error instanceof DOMException && error.name === "AbortError";
  }

  function isActionBusy(): boolean {
    const status = requestStatusRef.current;
    return status === "checking" || status === "loading" || status === "sending";
  }

  function clearRecordingTimers() {
    if (recordingIntervalRef.current !== null) {
      window.clearInterval(recordingIntervalRef.current);
      recordingIntervalRef.current = null;
    }
    if (recordingTimeoutRef.current !== null) {
      window.clearTimeout(recordingTimeoutRef.current);
      recordingTimeoutRef.current = null;
    }
  }

  function releaseMediaStream() {
    mediaStreamRef.current?.getTracks().forEach((track) => track.stop());
    mediaStreamRef.current = null;
  }

  function stopRecording(discard: boolean) {
    const recorder = mediaRecorderRef.current;
    discardRecordingRef.current = discard;
    clearRecordingTimers();
    if (discard) {
      recordingIdentityRef.current += 1;
      recordingChunksRef.current = [];
      setRecording(false);
      setRecordingElapsed(0);
    }
    if (recorder && recorder.state !== "inactive") {
      recorder.stop();
    } else {
      releaseMediaStream();
      mediaRecorderRef.current = null;
      if (!discard) setRecording(false);
    }
  }

  async function startRecording() {
    if (
      isActionBusy() ||
      recording ||
      microphoneRequestRef.current ||
      mediaRecorderRef.current !== null
    ) return;
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setErrorMessage("Microphone recording is not supported by this browser.");
      return;
    }
    setErrorMessage("");
    const generation = sessionGenerationRef.current;
    const originChatId = selectedChatIdRef.current;
    microphoneRequestRef.current = true;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (
        !isCurrentSession(generation) ||
        selectedChatIdRef.current !== originChatId
      ) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      mediaStreamRef.current = stream;
      const preferredMimeTypes = [
        "audio/webm;codecs=opus",
        "audio/mp4",
        "audio/ogg;codecs=opus",
      ];
      const mimeType = preferredMimeTypes.find((candidate) =>
        MediaRecorder.isTypeSupported(candidate),
      );
      const recorder = mimeType
        ? new MediaRecorder(stream, { mimeType })
        : new MediaRecorder(stream);
      const recordingIdentity = recordingIdentityRef.current + 1;
      recordingIdentityRef.current = recordingIdentity;
      mediaRecorderRef.current = recorder;
      recordingChunksRef.current = [];
      discardRecordingRef.current = false;
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) recordingChunksRef.current.push(event.data);
      };
      recorder.onstop = () => {
        const chunks = recordingChunksRef.current;
        const discard = discardRecordingRef.current;
        recordingChunksRef.current = [];
        mediaRecorderRef.current = null;
        releaseMediaStream();
        clearRecordingTimers();
        if (discard || recordingIdentityRef.current !== recordingIdentity) return;

        const recordedMimeType = recorder.mimeType || "audio/webm";
        const baseMimeType = recordedMimeType.split(";", 1)[0];
        const extension = baseMimeType === "audio/mp4"
          ? "m4a"
          : baseMimeType === "audio/ogg"
            ? "ogg"
            : "webm";
        const blob = new Blob(chunks, { type: baseMimeType });
        if (blob.size === 0) {
          setErrorMessage("The recording was empty and was discarded.");
        } else if (blob.size > AUDIO_MAX_BYTES) {
          setErrorMessage("Recorded audio must not exceed 25 MiB.");
        } else {
          const file = new File([blob], `browser-recording.${extension}`, {
            type: baseMimeType,
          });
          setAttachments((existing) => {
            if (existing.length >= MAX_ATTACHMENTS) {
              setErrorMessage("A message may contain at most 8 attachments.");
              return existing;
            }
            return [
              ...existing,
              { id: crypto.randomUUID(), file, kind: "recording" },
            ];
          });
        }
        setRecording(false);
        setRecordingElapsed(0);
      };
      recorder.onerror = () => {
        setErrorMessage("The browser could not complete the recording.");
        stopRecording(true);
      };
      recorder.start(1_000);
      recordingStartedAtRef.current = Date.now();
      setRecordingElapsed(0);
      setRecording(true);
      recordingIntervalRef.current = window.setInterval(() => {
        setRecordingElapsed(
          Math.min(
            15 * 60,
            Math.floor((Date.now() - recordingStartedAtRef.current) / 1_000),
          ),
        );
      }, 500);
      recordingTimeoutRef.current = window.setTimeout(
        () => stopRecording(false),
        RECORDING_MAX_MILLISECONDS,
      );
    } catch (error) {
      mediaRecorderRef.current = null;
      recordingChunksRef.current = [];
      clearRecordingTimers();
      setRecording(false);
      setRecordingElapsed(0);
      const message = error instanceof DOMException && error.name === "NotAllowedError"
        ? "Microphone permission was not granted."
        : "The microphone could not be opened.";
      setErrorMessage(message);
      releaseMediaStream();
    } finally {
      microphoneRequestRef.current = false;
    }
  }

  function queueAttachments(event: ChangeEvent<HTMLInputElement>, kind: AttachmentKind) {
    const selected = Array.from(event.target.files ?? []);
    event.target.value = "";
    if (selected.length === 0) return;
    setErrorMessage("");
    const accepted: QueuedAttachment[] = [];
    for (const file of selected) {
      const extension = file.name.toLowerCase().slice(file.name.lastIndexOf("."));
      const maximumBytes = kind === "document"
        ? extension === ".txt" ? TXT_MAX_BYTES : PDF_MAX_BYTES
        : AUDIO_MAX_BYTES;
      if (file.size > maximumBytes) {
        setErrorMessage(
          kind === "document"
            ? "A selected document exceeds its size limit."
            : "Audio must not exceed 25 MiB.",
        );
        continue;
      }
      accepted.push({ id: crypto.randomUUID(), file, kind });
    }
    setAttachments((existing) => {
      const available = Math.max(0, MAX_ATTACHMENTS - existing.length);
      if (accepted.length > available) {
        setErrorMessage("A message may contain at most 8 attachments.");
      }
      return [...existing, ...accepted.slice(0, available)];
    });
  }

  function removeAttachment(id: string) {
    setAttachments((existing) => existing.filter((item) => item.id !== id));
  }

  function stopModelHealthPolling() {
    if (healthPollTimerRef.current !== null) {
      window.clearTimeout(healthPollTimerRef.current);
      healthPollTimerRef.current = null;
    }
    healthRequestRef.current += 1;
    const controller = healthControllerRef.current;
    healthControllerRef.current = null;
    if (controller) {
      controller.abort();
      inFlightControllersRef.current.delete(controller);
    }
  }

  function startModelHealthPolling(generation: number) {
    stopModelHealthPolling();
    setHealthConnection("checking");
    void loadModelHealth(generation);
  }

  async function loadModelHealth(generation: number) {
    if (!isCurrentSession(generation) || healthControllerRef.current !== null) return;
    const requestIdentity = healthRequestRef.current + 1;
    healthRequestRef.current = requestIdentity;
    const controller = startRequest();
    healthControllerRef.current = controller;
    let requestTimedOut = false;
    const timeout = window.setTimeout(() => {
      requestTimedOut = true;
      controller.abort();
    }, HEALTH_REQUEST_TIMEOUT_MS);
    try {
      const health = await api.health(controller.signal);
      if (
        !isCurrentSession(generation) ||
        healthRequestRef.current !== requestIdentity
      ) return;
      setModelHealth(health);
      setHealthConnection("connected");
    } catch (error) {
      if (
        !isCurrentSession(generation) ||
        healthRequestRef.current !== requestIdentity ||
        (isAbortError(error) && !requestTimedOut)
      ) return;
      setHealthConnection("unavailable");
    } finally {
      window.clearTimeout(timeout);
      finishRequest(controller);
      if (healthControllerRef.current === controller) {
        healthControllerRef.current = null;
      }
      if (
        isCurrentSession(generation) &&
        healthRequestRef.current === requestIdentity
      ) {
        healthPollTimerRef.current = window.setTimeout(
          () => void loadModelHealth(generation),
          HEALTH_POLL_INTERVAL_MS,
        );
      }
    }
  }

  function handleRequestError(
    error: unknown,
    generation: number,
    selectionRequest?: number,
    originChatId?: string,
  ) {
    if (
      isAbortError(error) ||
      !isCurrentSession(generation) ||
      (selectionRequest !== undefined && selectionRequestRef.current !== selectionRequest) ||
      (originChatId !== undefined && selectedChatIdRef.current !== originChatId)
    ) {
      return;
    }
    const message = error instanceof Error ? error.message : "Something went wrong.";
    if (error instanceof ApiError && error.status === 401) {
      changeUser(null, "error");
      setErrorMessage(message);
      return;
    }
    setErrorMessage(message);
    setStatus("error");
  }

  async function restoreSession(generation: number) {
    const controller = startRequest();
    try {
      const session = await api.session(controller.signal);
      if (!isCurrentSession(generation)) return;
      finishRequest(controller);
      const activeGeneration = changeUser(session.username, "loading");
      await loadChats(undefined, activeGeneration);
    } catch (error) {
      if (!isCurrentSession(generation) || isAbortError(error)) return;
      if (error instanceof ApiError && error.status === 401) {
        changeUser(null, "idle");
      } else {
        setErrorMessage(error instanceof Error ? error.message : "Unable to connect.");
        setStatus("idle");
      }
    } finally {
      finishRequest(controller);
    }
  }

  async function loadChats(preferredChatId: string | undefined, generation: number) {
    if (!isCurrentSession(generation)) return;
    setStatus("loading");
    const controller = startRequest();
    try {
      const result = await api.listChats(controller.signal);
      if (!isCurrentSession(generation)) return;
      setChats(result.chats);
      const chatId = preferredChatId ?? result.chats[0]?.chat_id ?? null;
      setSelectedChat(chatId);
      if (chatId) {
        finishRequest(controller);
        await loadChatDetail(chatId, generation);
      } else {
        setMessages([]);
        setStatus("idle");
      }
    } catch (error) {
      handleRequestError(error, generation);
    } finally {
      finishRequest(controller);
    }
  }

  async function loadChatDetail(chatId: string, generation: number) {
    invalidateSelection();
    const selectionRequest = selectionRequestRef.current;
    const controller = startRequest();
    detailControllerRef.current = controller;
    setStatus("loading");
    try {
      const detail = await api.getChat(chatId, controller.signal);
      if (
        !isCurrentSession(generation) ||
        selectionRequestRef.current !== selectionRequest ||
        selectedChatIdRef.current !== chatId
      ) return;
      setMessages(detail.messages);
      setStatus("idle");
    } catch (error) {
      handleRequestError(error, generation, selectionRequest, chatId);
    } finally {
      finishRequest(controller);
      if (detailControllerRef.current === controller) {
        detailControllerRef.current = null;
      }
    }
  }

  async function refreshRunResult(chatId: string, generation: number) {
    if (!isCurrentSession(generation) || selectedChatIdRef.current !== chatId) return;
    const selection = selectionRequestRef.current;
    const controller = startRequest();
    try {
      const detail = await api.getChat(chatId, controller.signal);
      if (!isCurrentSession(generation) || selectedChatIdRef.current !== chatId ||
        selectionRequestRef.current !== selection) return;
      setMessages((current) => mergeChatMessages(current, detail.messages));
      setChats((current) => current.map((chat) => chat.chat_id === chatId
        ? { chat_id: detail.chat_id, title: detail.title, created_at: detail.created_at, updated_at: detail.updated_at }
        : chat));
    } catch (error) {
      if (isAbortError(error) || !isCurrentSession(generation) ||
        selectedChatIdRef.current !== chatId || selectionRequestRef.current !== selection) return;
      if (error instanceof ApiError && error.status === 401) {
        handleRequestError(error, generation, selection, chatId);
      } else {
        // This background refresh must not release a newer send/confirm's busy state.
        setErrorMessage("The latest analysis could not be loaded. Reconnect and refresh the conversation.");
      }
    } finally {
      finishRequest(controller);
    }
  }

  async function handleLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isActionBusy()) return;
    const formElement = event.currentTarget;
    const generation = changeUser(null, "loading");
    const form = new FormData(formElement);
    const controller = startRequest();
    try {
      const session = await api.login(
        String(form.get("username") ?? ""),
        String(form.get("password") ?? ""),
        controller.signal,
      );
      if (!isCurrentSession(generation)) return;
      finishRequest(controller);
      const activeGeneration = changeUser(session.username, "loading");
      formElement.reset();
      await loadChats(undefined, activeGeneration);
    } catch (error) {
      handleRequestError(error, generation);
    } finally {
      finishRequest(controller);
    }
  }

  async function handleLogout() {
    const generation = changeUser(null, "loading");
    const controller = startRequest();
    try {
      await api.logout(controller.signal);
    } catch (error) {
      if (isCurrentSession(generation) && !isAbortError(error)) {
        setErrorMessage(error instanceof Error ? error.message : "Unable to sign out cleanly.");
        setStatus("error");
      }
    } finally {
      finishRequest(controller);
      if (isCurrentSession(generation) && requestStatusRef.current === "loading") {
        setStatus("idle");
      }
    }
  }

  async function createNewChat(preserveDraft = false): Promise<ChatSummary | null> {
    if (isActionBusy()) return null;
    if (!preserveDraft) {
      stopRecording(true);
      setAttachments([]);
    }
    const generation = sessionGenerationRef.current;
    setErrorMessage("");
    setStatus("loading");
    const controller = startRequest();
    try {
      const chat = await api.createChat(controller.signal);
      if (!isCurrentSession(generation)) return null;
      invalidateSelection();
      setChats((current) => [chat, ...current]);
      setSelectedChat(chat.chat_id);
      setMessages([]);
      setStatus("idle");
      return chat;
    } catch (error) {
      handleRequestError(error, generation);
      return null;
    } finally {
      finishRequest(controller);
    }
  }

  async function selectChat(chatId: string) {
    if (chatId === selectedChatIdRef.current || requestStatusRef.current === "sending") return;
    const generation = sessionGenerationRef.current;
    setErrorMessage("");
    stopRecording(true);
    setAttachments([]);
    setSelectedChat(chatId);
    setMessages([]);
    await loadChatDetail(chatId, generation);
  }

  async function handleSend(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = composerText;
    if (
      (!text.trim() && attachments.length === 0) ||
      Array.from(text).length > 30_000 ||
      isActionBusy() ||
      recording
    ) return;

    setErrorMessage("");
    let chatId = selectedChatIdRef.current;
    if (!chatId) {
      const chat = await createNewChat(true);
      if (!chat) return;
      chatId = chat.chat_id;
    }

    const generation = sessionGenerationRef.current;
    const originChatId = chatId;
    const outgoingAttachments = attachments.map((attachment) => attachment.file);
    setStatus("sending");
    const controller = startRequest();
    try {
      const result = await api.sendMessage(
        originChatId,
        text,
        outgoingAttachments,
        controller.signal,
      );
      if (!isCurrentSession(generation) || selectedChatIdRef.current !== originChatId) return;
      setMessages((current) => {
        const currentMessages = result.assistant_message.kind === "progress"
          ? current.map((message) => {
              if (message.message_id !== currentProfileMessageId || message.payload === null) {
                return message;
              }
              return { ...message, payload: { ...message.payload, confirmed: true } };
            })
          : current;
        return mergeChatMessages(currentMessages, [result.user_message, result.assistant_message]);
      });
      setChats((current) => [
        result.chat,
        ...current.filter((chat) => chat.chat_id !== result.chat.chat_id),
      ]);
      setComposerText("");
      setAttachments([]);
      setStatus("idle");
    } catch (error) {
      handleRequestError(error, generation, undefined, originChatId);
    } finally {
      finishRequest(controller);
    }
  }

  async function handleConfirmProfile(profile: ProfileCard) {
    if (isActionBusy()) return;
    const chatId = selectedChatIdRef.current;
    if (!chatId) return;
    const generation = sessionGenerationRef.current;
    const originChatId = chatId;
    setErrorMessage("");
    setStatus("sending");
    const controller = startRequest();
    try {
      const result = await api.confirmProfile(originChatId, profile, controller.signal);
      if (!isCurrentSession(generation) || selectedChatIdRef.current !== originChatId) return;
      setMessages((current) => mergeChatMessages(current, [result.profile_message, result.progress_message]));
      setChats((current) => [
        result.chat,
        ...current.filter((chat) => chat.chat_id !== result.chat.chat_id),
      ]);
      setStatus("idle");
    } catch (error) {
      handleRequestError(error, generation, undefined, originChatId);
    } finally {
      finishRequest(controller);
    }
  }

  if (username === null) {
    return (
      <main className="login-page">
        <section className="login-card" aria-labelledby="login-title">
          <div className="brand-mark" aria-hidden="true">LC</div>
          <p className="eyebrow">Private · Local-first</p>
          <h1 id="login-title">Local Career Impact</h1>
          <p className="login-intro">
            Explore how AI may reshape your work with locally processed evidence.
          </p>
          <form className="login-form" onSubmit={handleLogin}>
            <label>
              Username
              <input name="username" autoComplete="username" required autoFocus />
            </label>
            <label>
              Password
              <input name="password" type="password" autoComplete="current-password" required />
            </label>
            {errorMessage && <p className="form-error" role="alert">{errorMessage}</p>}
            <button className="primary-button" disabled={actionBusy}>
              {requestStatus === "loading" ? "Signing in…" : "Continue"}
            </button>
          </form>
          <p className="privacy-note">Your conversations stay on this machine.</p>
        </section>
      </main>
    );
  }

  if (username === ADMIN_USERNAME && window.location.pathname === "/knowledge") {
    return (
      <KnowledgeWorkspace
        username={username}
        onLogout={() => void handleLogout()}
      />
    );
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-top">
          <div className="wordmark"><span>LC</span> Career Impact</div>
          <button className="new-chat-button" onClick={() => void createNewChat()} disabled={actionBusy}>
            <span aria-hidden="true">＋</span> New chat
          </button>
        </div>

        <nav className="conversation-list" aria-label="Conversations">
          <p className="nav-label">Conversations</p>
          {chats.length === 0 && <p className="empty-list">No conversations yet</p>}
          {chats.map((chat) => (
            <button
              key={chat.chat_id}
              className={chat.chat_id === selectedChatId ? "conversation active" : "conversation"}
              onClick={() => void selectChat(chat.chat_id)}
              disabled={requestStatus === "sending"}
            >
              <span>{chat.title}</span>
              <time dateTime={chat.updated_at}>{shortTime(chat.updated_at)}</time>
            </button>
          ))}
        </nav>

        <div className="sidebar-footer">
          {username === ADMIN_USERNAME && <a href="/knowledge">Knowledge Base</a>}
          <div className="account-row">
            <div className="avatar">{username.slice(0, 1).toUpperCase()}</div>
            <div><strong>{username}</strong><span>Local account</span></div>
            <button onClick={() => void handleLogout()} aria-label="Sign out" title="Sign out">↗</button>
          </div>
        </div>
      </aside>

      <main className="chat-panel">
        <header className="chat-header">
          <div>
            <p className="eyebrow">Evidence-guided conversation</p>
            <h1>{chats.find((chat) => chat.chat_id === selectedChatId)?.title ?? "New conversation"}</h1>
          </div>
          <div
            className="readiness"
            aria-label={
              `Model connection ${healthConnection}; Qwen 30B ${modelHealth.qwen30b}; ` +
              `Qwen 4B ${modelHealth.qwen4b}`
            }
            title={
              `Connection: ${healthConnection}; Qwen 30B: ${modelHealth.qwen30b}; ` +
              `Qwen 4B: ${modelHealth.qwen4b}`
            }
          >
            <span aria-hidden="true" />
            {healthConnection === "unavailable" && "Connection unavailable · last known "}
            {healthConnection === "checking" && "Checking · "}
            30B: {modelHealth.qwen30b} · 4B: {modelHealth.qwen4b}
          </div>
        </header>

        <section className="message-stream" aria-live="polite" aria-busy={requestStatus === "loading"}>
          {requestStatus === "loading" && messages.length === 0 ? (
            <div className="loading-state">Loading conversation…</div>
          ) : messages.length === 0 ? (
            <div className="welcome-state">
              <div className="welcome-icon" aria-hidden="true">✦</div>
              <h2>What would you like to explore?</h2>
              <p>
                Share your role, responsibilities, or career goals. Your conversation is saved locally.
              </p>
              <div className="prompt-grid">
                <button onClick={() => setComposerText("How might AI change my current responsibilities?")}>How might AI change my role?</button>
                <button onClick={() => setComposerText("Which skills should I build over the next three years?")}>Which skills should I build?</button>
              </div>
            </div>
          ) : (
            messages.map((message) => {
              const profile = message.kind === "profile"
                ? profileFromPayload(message.payload)
                : null;
              const messageGeneration = sessionGenerationRef.current;
              const runId = typeof message.payload?.run_id === "string" ? message.payload.run_id : null;
              const reportId = typeof message.payload?.report_id === "string" ? message.payload.report_id : null;
              const expireSession = () => {
                if (isCurrentSession(messageGeneration) && selectedChatIdRef.current === message.chat_id) {
                  changeUser(null, "idle");
                  setErrorMessage("Your session expired. Sign in again to reopen your saved analysis.");
                }
              };
              const extractedText = message.payload && typeof message.payload.extracted_text === "string"
                ? message.payload.extracted_text
                : "";
              const attachmentCount = message.payload && typeof message.payload.attachment_count === "number"
                ? message.payload.attachment_count
                : 0;
              return (
                <article key={`${username}:${message.chat_id}:${message.message_id}`} className={`message ${message.role}${message.kind === "report" ? " report-message" : ""}`}>
                  <div className="message-avatar" aria-hidden="true">
                    {message.role === "user" ? username.slice(0, 1).toUpperCase() : "✦"}
                  </div>
                  <div className="message-body">
                    <div className="message-meta">
                      <strong>{message.role === "user" ? "You" : "Career Impact"}</strong>
                      <time dateTime={message.created_at}>{shortTime(message.created_at)}</time>
                    </div>
                    {message.kind !== "progress" && message.kind !== "report" && <p>{message.text}</p>}
                    {attachmentCount > 0 && (
                      <div className="attachment-summary">
                        {attachmentCount} candidate attachment{attachmentCount === 1 ? "" : "s"} processed locally
                      </div>
                    )}
                    {extractedText && (
                      <details className="extracted-text">
                        <summary>View locally extracted text</summary>
                        <pre>{extractedText}</pre>
                      </details>
                    )}
                    {profile && (
                      <ProfileEditor
                        profile={profile}
                        disabled={actionBusy}
                        current={message.message_id === currentProfileMessageId}
                        onConfirm={(editedProfile) => void handleConfirmProfile(editedProfile)}
                      />
                    )}
                    {message.kind === "progress" && runId && (
                      <RunProgressCard runId={runId}
                        onSettled={() => void refreshRunResult(message.chat_id, messageGeneration)}
                        onUnauthorized={expireSession} />
                    )}
                    {message.kind === "report" && reportId && (
                      <ReportCard reportId={reportId} onUnauthorized={expireSession} />
                    )}
                    {(message.kind === "progress" && !runId || message.kind === "report" && !reportId) && <p>{message.text}</p>}
                  </div>
                </article>
              );
            })
          )}
          <div ref={streamEndRef} />
        </section>

        <footer className="composer-dock">
          {errorMessage && <div className="request-error" role="alert"><p>{errorMessage}</p>
            {selectedChatId && <button type="button" className="text-button" disabled={actionBusy}
              onClick={() => void loadChatDetail(selectedChatId, sessionGenerationRef.current)}>Refresh conversation</button>}
          </div>}
          {recording && (
            <div className="recording-bar" role="status">
              <span><i aria-hidden="true" /> Recording {formatElapsed(recordingElapsed)} / 15:00</span>
              <div>
                <button type="button" onClick={() => stopRecording(false)}>Stop</button>
                <button type="button" onClick={() => stopRecording(true)}>Discard</button>
              </div>
            </div>
          )}
          {attachments.length > 0 && (
            <div className="attachment-queue" aria-label="Queued attachments">
              {attachments.map((attachment, index) => (
                <span key={attachment.id}>
                  {attachment.kind === "document"
                    ? "Document"
                    : attachment.kind === "recording"
                      ? "Recording"
                      : "Audio"} {index + 1} · {(attachment.file.size / 1024 / 1024).toFixed(1)} MiB
                  <button
                    type="button"
                    onClick={() => removeAttachment(attachment.id)}
                    aria-label={`Discard attachment ${index + 1}`}
                  >×</button>
                </span>
              ))}
            </div>
          )}
          <form className="composer" onSubmit={handleSend}>
            <textarea
              aria-label="Message"
              placeholder="Describe your role, experience, or goals…"
              value={composerText}
              onChange={(event) => setComposerText(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
              rows={1}
            />
            <div className="composer-actions">
              <div className="attachment-controls" aria-label="Candidate attachments">
                <input
                  ref={documentInputRef}
                  type="file"
                  accept=".pdf,application/pdf,.txt,text/plain"
                  multiple
                  hidden
                  onChange={(event) => queueAttachments(event, "document")}
                />
                <input
                  ref={audioInputRef}
                  type="file"
                  accept="audio/*,.webm,.m4a,.mp3,.wav,.ogg,.flac,.aac"
                  multiple
                  hidden
                  onChange={(event) => queueAttachments(event, "audio")}
                />
                <button
                  type="button"
                  onClick={() => documentInputRef.current?.click()}
                  disabled={actionBusy || recording || attachments.length >= MAX_ATTACHMENTS}
                  title="Attach PDF or UTF-8 TXT"
                >＋ PDF / TXT</button>
                <button
                  type="button"
                  onClick={() => audioInputRef.current?.click()}
                  disabled={actionBusy || recording || attachments.length >= MAX_ATTACHMENTS}
                  title="Attach audio up to 15 minutes"
                >♪ Audio</button>
                <button
                  type="button"
                  onClick={() => void startRecording()}
                  disabled={actionBusy || recording || attachments.length >= MAX_ATTACHMENTS}
                  title="Record audio up to 15 minutes"
                >◉ Mic</button>
              </div>
              <div className="send-group">
                <span>{composerCodePoints.toLocaleString()} / 30,000</span>
                <button
                  className="send-button"
                  aria-label="Send message"
                  disabled={
                    (!composerText.trim() && attachments.length === 0) ||
                    composerCodePoints > 30_000 ||
                    actionBusy ||
                    recording
                  }
                >
                  {requestStatus === "sending" ? "…" : "↑"}
                </button>
              </div>
            </div>
          </form>
          <p className="disclaimer">Scenario guidance, not a prediction of your personal future.</p>
        </footer>
      </main>
    </div>
  );
}

export default App;
