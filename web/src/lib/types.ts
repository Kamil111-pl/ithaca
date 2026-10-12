export type View = "chat" | "documents" | "research" | "focus" | "memory" | "settings";

export interface Conversation {
  id: string;
  title: string;
  provider_id: string | null;
  model: string | null;
  document_id: string | null;
  pinned: number;
  created_at: number;
  updated_at: number;
}

export interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export type Item =
  | { kind: "user"; key: string; content: string }
  | {
      kind: "assistant";
      key: string;
      content: string;
      thinking: string;
      tool_calls: ToolCall[];
      model?: string | null;
      streaming?: boolean;
      stop_reason?: string | null;
    }
  | {
      kind: "tool_result";
      key: string;
      tool_call_id: string;
      name: string;
      display: string;
      is_error: boolean;
      untrusted: boolean;
      data?: Record<string, unknown> | null;
      duration_ms?: number;
      image?: string | null;
    }
  | {
      kind: "approval";
      key: string;
      approval_id: string;
      tool_call_id: string;
      name: string;
      arguments: Record<string, unknown>;
      reason: string;
      status: "pending" | "approved" | "denied";
    }
  | { kind: "error"; key: string; message: string }
  | { kind: "notice"; key: string; text: string };

export interface ConversationDetail extends Conversation {
  items: (Item & { event_id?: number })[];
  active_run_id: string | null;
  tainted: boolean;
}

export interface ModelOption {
  provider_id: string;
  provider: string;
  kind: "openai" | "anthropic";
  model: string;
  tools: boolean | null;
  context: number | null;
  vision?: boolean;
  default: boolean;
}

export interface ProviderModel {
  id: string;
  tools: boolean | null;
  vision?: boolean | null;
  context: number | null;
}

export interface Provider {
  id: string;
  name: string;
  kind: "openai" | "anthropic";
  base_url: string;
  default_model: string;
  models: ProviderModel[];
  has_api_key: boolean;
  warning?: string;
}

export interface Doc {
  id: string;
  title: string;
  content: string;
  created_at: number;
  updated_at: number;
}

export interface DocSummary {
  id: string;
  title: string;
  preview?: string;
  snippet?: string;
  updated_at: number;
}

export interface Memory {
  id: string;
  content: string;
  tags: string[];
  tainted: boolean;
  created_at: number;
  score?: number;
}

export type Settings = Record<string, string | number | boolean>;

export interface FocusStats {
  days: { day: string; seconds: number; completed: number }[];
  today_seconds: number;
  today_sessions: number;
  total_seconds: number;
  streak: number;
  labels: { label: string; seconds: number }[];
}

export type RunEvent = { type: string; run_id?: string; [k: string]: unknown };
