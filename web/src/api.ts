import type {
  ChatDetail,
  ChatList,
  ChatSummary,
  Health,
  KnowledgeCitation,
  KnowledgeDocumentList,
  KnowledgeStatus,
  KnowledgeUploadResult,
  MessagePair,
  ProfileCard,
  ProfileConfirmationResult,
  Session,
  RunDetail,
  ReportDetail,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "include",
    headers: {
      ...init.headers,
    },
  });

  if (!response.ok) {
    let message = `Request failed (${response.status}).`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (typeof body.detail === "string") {
        message = body.detail;
      }
    } catch {
      // The status remains useful when the server returns a non-JSON response.
    }
    throw new ApiError(message, response.status);
  }

  return (await response.json()) as T;
}

export const api = {
  health: (signal?: AbortSignal) =>
    request<Health>("/api/health", { signal }),

  session: (signal?: AbortSignal) =>
    request<Session>("/api/auth/session", { signal }),

  login: (username: string, password: string, signal?: AbortSignal) =>
    request<Session>("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
      signal,
    }),

  logout: (signal?: AbortSignal) =>
    request<{ ok: boolean }>("/api/auth/logout", { method: "POST", signal }),

  listChats: (signal?: AbortSignal) =>
    request<ChatList>("/api/chats", { signal }),

  createChat: (signal?: AbortSignal) =>
    request<ChatSummary>("/api/chats", { method: "POST", signal }),

  getChat: (chatId: string, signal?: AbortSignal) =>
    request<ChatDetail>(`/api/chats/${encodeURIComponent(chatId)}`, { signal }),

  sendMessage: (
    chatId: string,
    text: string,
    attachments: File[],
    signal?: AbortSignal,
  ) => {
    const form = new FormData();
    form.append("text", text);
    for (const attachment of attachments) {
      form.append("files", attachment);
    }
    return request<MessagePair>(
      `/api/chats/${encodeURIComponent(chatId)}/messages`,
      { method: "POST", body: form, signal },
    );
  },

  confirmProfile: (
    chatId: string,
    profile: ProfileCard,
    signal?: AbortSignal,
  ) => request<ProfileConfirmationResult>(
    `/api/chats/${encodeURIComponent(chatId)}/confirm-profile`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        profile_id: profile.profile_id,
        occupation_candidates: profile.occupation_candidates,
        region: profile.region,
        years_of_experience: profile.years_of_experience,
        responsibilities: profile.responsibilities,
        skills: profile.skills,
        industry_context: profile.industry_context,
        goals: profile.goals,
      }),
      signal,
    },
  ),

  getRun: (runId: string, signal?: AbortSignal) =>
    request<RunDetail>(`/api/runs/${encodeURIComponent(runId)}`, { signal }),

  runEvents: (runId: string, after: number) => new EventSource(
    `/api/runs/${encodeURIComponent(runId)}/events?after=${after}`,
    { withCredentials: true },
  ),

  getReport: (reportId: string, signal?: AbortSignal) =>
    request<ReportDetail>(`/api/reports/${encodeURIComponent(reportId)}`, { signal }),

  listKnowledgeDocuments: (signal?: AbortSignal) =>
    request<KnowledgeDocumentList>("/api/admin/knowledge/documents", { signal }),

  uploadKnowledgeDocument: (file: File, signal?: AbortSignal) => {
    const form = new FormData();
    form.append("file", file);
    return request<KnowledgeUploadResult>("/api/admin/knowledge/upload", {
      method: "POST",
      body: form,
      signal,
    });
  },

  rebuildKnowledge: (signal?: AbortSignal) =>
    request<KnowledgeStatus>("/api/admin/knowledge/rebuild", {
      method: "POST",
      signal,
    }),

  knowledgeStatus: (signal?: AbortSignal) =>
    request<KnowledgeStatus>("/api/admin/knowledge/status", { signal }),

  knowledgeCitation: (
    snapshotId: string,
    chunkId: string,
    signal?: AbortSignal,
  ) => request<KnowledgeCitation>(
    `/api/admin/knowledge/citations/${encodeURIComponent(snapshotId)}/${encodeURIComponent(chunkId)}`,
    { signal },
  ),
};
