export type MessageRole = "user" | "assistant" | "system";
export type MessageKind = "text" | "profile" | "progress" | "report" | "error";

export interface Session {
  username: string;
}

export type ModelWorkerStatus = "starting" | "ready" | "failed";

export interface Health {
  database: "ready" | "unavailable";
  qwen30b: ModelWorkerStatus;
  qwen4b: ModelWorkerStatus;
}

export interface ChatSummary {
  chat_id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface ChatMessage {
  message_id: string;
  chat_id: string;
  role: MessageRole;
  kind: MessageKind;
  text: string;
  sequence: number;
  created_at: string;
  payload: Record<string, unknown> | null;
}

export interface ChatList {
  chats: ChatSummary[];
}

export interface ChatDetail extends ChatSummary {
  messages: ChatMessage[];
}

export interface MessagePair {
  user_message: ChatMessage;
  assistant_message: ChatMessage;
  chat: ChatSummary;
}

export interface AttachmentMetadata {
  attachment_id: string;
  attachment_type: "pdf" | "text" | "audio";
  size_bytes: number;
}

export interface ProfileCard {
  profile_id: string;
  confirmed: boolean;
  occupation_candidates: string[];
  region: string | null;
  years_of_experience: number | null;
  responsibilities: string[];
  skills: string[];
  industry_context: string | null;
  goals: string[];
}

export interface RunProgress {
  run_id: string;
  status: "queued" | "running" | "complete" | "failed" | "cancelled";
  current_stage: string | null;
  completed_stages: number;
  total_stages: number;
}

export interface ProfileConfirmationResult {
  profile_message: ChatMessage;
  progress_message: ChatMessage;
  run: RunProgress;
  chat: ChatSummary;
}

export interface ReportSummary {
  report_id: string;
  run_id: string;
  title: string;
  summary: string;
  created_at: string;
}

export type KnowledgeDocumentStatus = "accepted" | "rejected";
export type KnowledgeSnapshotStatus = "building" | "active" | "inactive" | "failed";

export interface KnowledgeDocument {
  source_label: string;
  filename: string;
  source_type: string;
  publisher: string;
  release_date: string | null;
  licence: string;
  jurisdiction: "AU" | "GLOBAL" | "UNKNOWN";
  authority_class: "official" | "peer_reviewed" | "institutional" | "contextual";
  status: KnowledgeDocumentStatus;
  document_id: string | null;
  content_hash: string | null;
  language: string | null;
  size_bytes: number;
  chunk_count: number;
  error_message: string | null;
  updated_at: string;
}

export interface KnowledgeDocumentList {
  documents: KnowledgeDocument[];
}

export interface KnowledgeSnapshot {
  snapshot_id: string;
  status: KnowledgeSnapshotStatus;
  stage: string;
  total_documents: number;
  processed_documents: number;
  total_chunks: number;
  processed_chunks: number;
  failure_source: string | null;
  error_message: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface KnowledgeStatus {
  rebuilding: boolean;
  active_snapshot: KnowledgeSnapshot | null;
  latest_rebuild: KnowledgeSnapshot | null;
}

export interface KnowledgeUploadResult {
  document: KnowledgeDocument;
}

export interface KnowledgeCitation {
  snapshot_id: string;
  chunk_id: string;
  document_id: string;
  title: string;
  source_label: string;
  source_type: string;
  publisher: string;
  release_date: string | null;
  licence: string;
  jurisdiction: "AU" | "GLOBAL" | "UNKNOWN";
  authority_class: "official" | "peer_reviewed" | "institutional" | "contextual";
  locator: string;
  language: string;
  text: string;
  content_hash: string;
  chunk_hash: string;
}
