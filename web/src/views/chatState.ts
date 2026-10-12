import { toolStage, type Stage } from "../components/Orb";
import type { Item, RunEvent, ToolCall } from "../lib/types";

let seq = 0;
const key = () => `live-${Date.now()}-${seq++}`;

export interface LiveState {
  items: Item[];
  running: boolean;
  runId: string | null;
  status: string | null;
  progress: Record<string, string[]>; // tool_call_id -> progress lines
  runningTools: Set<string>;
  tainted: boolean;
}

export const emptyLive = (items: Item[] = []): LiveState => ({
  items,
  running: false,
  runId: null,
  status: null,
  progress: {},
  runningTools: new Set(),
  tainted: false,
});

function lastAssistantIndex(items: Item[]): number {
  for (let i = items.length - 1; i >= 0; i--) if (items[i].kind === "assistant") return i;
  return -1;
}

function progressLine(ev: RunEvent): string | null {
  switch (ev.type) {
    case "research_progress": {
      const t = ev as Record<string, unknown>;
      if (t.phase) return `${String(t.phase)[0].toUpperCase()}${String(t.phase).slice(1)}…`;
      if (t.queries) return `Queries: ${(t.queries as string[]).join(" · ")}`;
      if (t.url && t.title !== undefined) return `Read: ${t.title || t.url}`;
      if (t.url && t.error) return `Skipped ${t.url} (${t.error})`;
      if (t.count !== undefined) return `${t.count} candidate sources`;
      if (t.message) return String(t.message);
      return null;
    }
    default:
      return null;
  }
}

export function applyEvent(state: LiveState, ev: RunEvent): LiveState {
  const items = state.items;
  switch (ev.type) {
    case "user_message":
      return { ...state, running: true, runId: (ev.run_id as string) ?? state.runId, items: [...items, { kind: "user", key: key(), content: String(ev.content ?? "") }] };
    case "run_started":
      return { ...state, running: true, runId: (ev.run_id as string) ?? state.runId };
    case "assistant_start":
      return {
        ...state,
        status: null,
        items: [...items, { kind: "assistant", key: key(), content: "", thinking: "", tool_calls: [], streaming: true }],
      };
    case "text":
    case "thinking": {
      const i = lastAssistantIndex(items);
      if (i < 0) return state;
      const a = items[i] as Extract<Item, { kind: "assistant" }>;
      const next = ev.type === "text" ? { ...a, content: a.content + String(ev.text) } : { ...a, thinking: a.thinking + String(ev.text) };
      const copy = items.slice();
      copy[i] = next;
      return { ...state, items: copy };
    }
    case "assistant_end": {
      const i = lastAssistantIndex(items);
      if (i < 0) return state;
      const copy = items.slice();
      copy[i] = { ...(items[i] as Extract<Item, { kind: "assistant" }>), streaming: false, stop_reason: (ev.stop_reason as string) ?? null };
      return { ...state, items: copy };
    }
    case "tool_start": {
      const i = lastAssistantIndex(items);
      const call: ToolCall = { id: String(ev.tool_call_id), name: String(ev.name), arguments: (ev.arguments as Record<string, unknown>) ?? {} };
      const copy = items.slice();
      if (i >= 0) {
        const a = items[i] as Extract<Item, { kind: "assistant" }>;
        if (!a.tool_calls.some((c) => c.id === call.id)) copy[i] = { ...a, tool_calls: [...a.tool_calls, call] };
      }
      const runningTools = new Set(state.runningTools);
      runningTools.add(call.id);
      return { ...state, items: copy, runningTools };
    }
    case "tool_result": {
      const runningTools = new Set(state.runningTools);
      runningTools.delete(String(ev.tool_call_id));
      return {
        ...state,
        runningTools,
        tainted: state.tainted || Boolean(ev.tainted),
        items: [
          ...items,
          {
            kind: "tool_result",
            key: key(),
            tool_call_id: String(ev.tool_call_id),
            name: String(ev.name),
            display: String(ev.display ?? ""),
            is_error: Boolean(ev.is_error),
            untrusted: Boolean(ev.untrusted),
            data: (ev.data as Record<string, unknown>) ?? null,
            duration_ms: ev.duration_ms as number,
            image: (ev.image as string) ?? null,
          },
        ],
      };
    }
    case "approval_requested":
      return {
        ...state,
        items: [
          ...items,
          {
            kind: "approval",
            key: key(),
            approval_id: String(ev.approval_id),
            tool_call_id: String(ev.tool_call_id),
            name: String(ev.name),
            arguments: (ev.arguments as Record<string, unknown>) ?? {},
            reason: String(ev.reason ?? ""),
            status: "pending",
          },
        ],
      };
    case "approval_decided":
      return {
        ...state,
        items: items.map((it) => (it.kind === "approval" && it.approval_id === ev.approval_id ? { ...it, status: ev.approved ? "approved" : "denied" } : it)),
      };
    case "status":
      return { ...state, status: String(ev.message ?? "") };
    case "research_progress": {
      const running = [...state.runningTools];
      const target = running[running.length - 1];
      const line = progressLine(ev);
      if (!target || !line) return state;
      return { ...state, progress: { ...state.progress, [target]: [...(state.progress[target] ?? []), line] } };
    }
    case "run_finished": {
      const extra: Item[] = [];
      if (ev.status === "error" && ev.error) extra.push({ kind: "error", key: key(), message: String(ev.error) });
      if (ev.status === "cancelled") extra.push({ kind: "notice", key: key(), text: "Stopped." });
      return {
        ...state,
        running: false,
        status: null,
        runningTools: new Set(),
        items: [...items.map((it) => (it.kind === "assistant" && it.streaming ? { ...it, streaming: false } : it)), ...extra],
      };
    }
    default:
      return state;
  }
}

/** Which stage of reasoning/action the current run is in, for the thinking orb. */
export function activityStage(state: LiveState): Stage | null {
  if (!state.running) return null;
  if (state.status && /retry|crash|restart/i.test(state.status)) return "recover";
  const items = state.items;
  if (items.some((it) => it.kind === "approval" && it.status === "pending")) return "approve";
  const running = [...state.runningTools];
  if (running.length) {
    const id = running[running.length - 1];
    for (let i = items.length - 1; i >= 0; i--) {
      const it = items[i];
      const call = it.kind === "assistant" ? it.tool_calls.find((c) => c.id === id) : undefined;
      if (call) return toolStage(call.name);
    }
    return "act";
  }
  const last = items[items.length - 1];
  if (!last || last.kind === "user") return "connect";
  if (last.kind === "assistant" && last.streaming && last.content.trim()) return "write";
  return "think";
}
