import {
  ArrowUp,
  Brain,
  ChevronRight,
  FileText,
  Globe,
  MoreHorizontal,
  Pencil,
  Pin,
  PinOff,
  Plus,
  Search,
  ShieldAlert,
  ShieldCheck,
  Square,
  Terminal,
  Trash2,
  Wrench,
  X,
  Mic,
  Volume2,
  VolumeX,
} from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactElement } from "react";
import { Dialog } from "../components/Dialog";
import { Markdown } from "../components/Markdown";
import { useMenu } from "../components/Menu";
import { ModelPicker, modelLabel } from "../components/ModelPicker";
import { Orb, STAGE_LABEL, toolStage } from "../components/Orb";
import { api, stream } from "../lib/api";
import { useApp } from "../lib/app";
import type { Conversation, ConversationDetail, Item, RunEvent, ToolCall } from "../lib/types";
import { activityStage, applyEvent, emptyLive, type LiveState } from "./chatState";
import { speak, startRecording, transcribe, type Recorder } from "../lib/voice";

/* ------------------------------------------------------------- sidebar */

export function ConversationList() {
  const { conversationId, openConversation, conversationsVersion, bumpConversations, toast } = useApp();
  const [items, setItems] = useState<Conversation[]>([]);
  const [q, setQ] = useState("");
  const [renaming, setRenaming] = useState<Conversation | null>(null);
  const menu = useMenu();

  useEffect(() => {
    const t = setTimeout(() => {
      api.get<Conversation[]>(`/api/conversations${q ? `?q=${encodeURIComponent(q)}` : ""}`).then(setItems).catch(() => {});
    }, q ? 200 : 0);
    return () => clearTimeout(t);
  }, [q, conversationsVersion]);

  const groups: { label: string; list: Conversation[] }[] = [];
  const now = Date.now() / 1000;
  const pinned = items.filter((c) => c.pinned);
  const rest = items.filter((c) => !c.pinned);
  if (pinned.length) groups.push({ label: "Pinned", list: pinned });
  const today = rest.filter((c) => now - c.updated_at < 86400);
  const week = rest.filter((c) => now - c.updated_at >= 86400 && now - c.updated_at < 7 * 86400);
  const older = rest.filter((c) => now - c.updated_at >= 7 * 86400);
  if (today.length) groups.push({ label: "Today", list: today });
  if (week.length) groups.push({ label: "This week", list: week });
  if (older.length) groups.push({ label: "Earlier", list: older });

  const showMenu = (el: HTMLElement | { x: number; y: number }, c: Conversation) =>
    menu.open(el, [
      { label: c.pinned ? "Unpin" : "Pin", icon: c.pinned ? <PinOff size={18} /> : <Pin size={18} />, onSelect: async () => { await api.patch(`/api/conversations/${c.id}`, { pinned: !c.pinned }); bumpConversations(); } },
      { label: "Rename", icon: <Pencil size={18} />, onSelect: () => setRenaming(c) },
      { separator: true },
      {
        label: "Delete",
        danger: true,
        icon: <Trash2 size={18} />,
        onSelect: async () => {
          await api.del(`/api/conversations/${c.id}`);
          if (c.id === conversationId) openConversation(null);
          bumpConversations();
          toast("Chat deleted");
        },
      },
    ]);

  return (
    <>
      <div className="side-actions">
        <div className="side-search">
          <Search size={15} />
          <input placeholder="Search chats" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search chats" />
        </div>
        <button className="icon-btn on" style={{ width: 38, height: 38 }} onClick={() => openConversation(null)} aria-label="New chat" title="New chat (⌘K)">
          <Plus size={18} />
        </button>
      </div>
      <div className="side-list">
        {groups.map((g) => (
          <div key={g.label}>
            <div className="side-group">{g.label}</div>
            {g.list.map((c) => (
              <button
                key={c.id}
                className={`side-item${c.id === conversationId ? " on" : ""}`}
                onClick={() => openConversation(c.id)}
                onContextMenu={(e) => { e.preventDefault(); showMenu({ x: e.clientX, y: e.clientY }, c); }}
              >
                <span className="t">{c.title}</span>
                <span
                  className="more icon-btn"
                  style={{ width: 26, height: 26 }}
                  role="button"
                  aria-label="Chat options"
                  onClick={(e) => { e.stopPropagation(); showMenu(e.currentTarget, c); }}
                >
                  <MoreHorizontal size={16} />
                </span>
              </button>
            ))}
          </div>
        ))}
        {!items.length && <div className="side-empty">{q ? "No chats match." : "Your conversations will appear here."}</div>}
      </div>
      {menu.node}
      {renaming && <RenameDialog conv={renaming} onClose={() => setRenaming(null)} onDone={bumpConversations} />}
    </>
  );
}

function RenameDialog({ conv, onClose, onDone }: { conv: Conversation; onClose: () => void; onDone: () => void }) {
  const [title, setTitle] = useState(conv.title);
  const save = async () => {
    await api.patch(`/api/conversations/${conv.id}`, { title: title.trim() || conv.title });
    onDone();
    onClose();
  };
  return (
    <Dialog title="Rename chat" onClose={onClose} footer={<><button className="btn ghost" onClick={onClose}>Cancel</button><button className="btn primary" onClick={save}>Save</button></>}>
      <input className="input" autoFocus value={title} onChange={(e) => setTitle(e.target.value)} onKeyDown={(e) => e.key === "Enter" && save()} />
    </Dialog>
  );
}

/* ---------------------------------------------------------------- chat */

const SUGGESTIONS = [
  "Plan my week around three deep-work blocks",
  "Research the best e-ink tablets for note taking",
  "Write a Python script that renames my photos by date",
  "What do you remember about me?",
];

export function ChatView() {
  const { conversationId, openConversation, models, bumpConversations, toast, refreshModels } = useApp();
  const [conv, setConv] = useState<ConversationDetail | null>(null);
  const [live, setLive] = useState<LiveState>(emptyLive());
  const [draft, setDraft] = useState("");
  const [pendingModel, setPendingModel] = useState<{ provider_id: string; model: string } | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const threadRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  // Set when send() creates a conversation, so selecting it doesn't reload
  // (and abort) the stream that just started.
  const skipNextLoad = useRef<string | null>(null);

  const consume = useCallback(async (gen: AsyncGenerator<RunEvent>) => {
    try {
      for await (const ev of gen) {
        setLive((s) => applyEvent(s, ev));
        if (ev.type === "user_message" || ev.type === "run_finished") {
          bumpConversations();
          // The server titles a chat from its first message; pick that up.
          const cid = window.location.hash.split("/")[1];
          if (cid) api.get<ConversationDetail>(`/api/conversations/${cid}`).then((c) => setConv((prev) => (prev && prev.id === c.id ? { ...prev, title: c.title } : prev))).catch(() => {});
        }
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        setLive((s) => ({ ...s, running: false, items: [...s.items, { kind: "error", key: `e${Date.now()}`, message: (e as Error).message }] }));
      }
    }
  }, [bumpConversations]);

  // Load the conversation, and re-attach to a run that is still going.
  useEffect(() => {
    if (conversationId && skipNextLoad.current === conversationId) {
      skipNextLoad.current = null;
      return;
    }
    abortRef.current?.abort();
    setPendingModel(null);
    if (!conversationId) {
      setConv(null);
      setLive(emptyLive());
      return;
    }
    let cancelled = false;
    api
      .get<ConversationDetail>(`/api/conversations/${conversationId}`)
      .then((c) => {
        if (cancelled) return;
        setConv(c);
        const runId = c.active_run_id;
        const items = (c.items as (Item & { run_id?: string })[]).map((it, i) => ({ ...it, key: `ev-${(it as { event_id?: number }).event_id ?? i}` })) as (Item & { run_id?: string })[];
        if (runId) {
          setLive({ ...emptyLive(items.filter((it) => it.run_id !== runId)), tainted: c.tainted, running: true, runId });
          const ctrl = new AbortController();
          abortRef.current = ctrl;
          consume(stream<RunEvent>(`/api/runs/${runId}/stream`, undefined, ctrl.signal, "GET"));
        } else {
          setLive({ ...emptyLive(items), tainted: c.tainted });
        }
        stick.current = true;
      })
      .catch((e) => {
        toast((e as Error).message, true);
        openConversation(null);
      });
    return () => {
      cancelled = true;
    };
  }, [conversationId, consume, toast, openConversation]);

  useLayoutEffect(() => {
    const el = threadRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [live.items, live.status]);

  const fallbackModel = models.find((m) => m.default && m.kind === "anthropic") ?? models.find((m) => m.default) ?? models[0];
  const activeProvider = pendingModel?.provider_id ?? conv?.provider_id ?? fallbackModel?.provider_id ?? null;
  const activeModel = pendingModel?.model ?? conv?.model ?? fallbackModel?.model ?? null;

  const send = async (text?: string) => {
    const content = (text ?? draft).trim();
    if (!content || live.running) return;
    if (!activeProvider || !activeModel) {
      toast("Add a model provider in Settings first.", true);
      return;
    }
    setDraft("");
    let cid = conversationId;
    if (!cid) {
      const created = await api.post<Conversation>("/api/conversations", { provider_id: activeProvider, model: activeModel });
      cid = created.id;
      setConv({ ...created, items: [], active_run_id: null, tainted: false });
      // Switch the URL/sidebar without reloading the empty thread.
      history.replaceState(null, "", `#chat/${cid}`);
      skipNextLoad.current = cid;
      openConversation(cid);
    }
    stick.current = true;
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setLive((s) => ({ ...s, running: true }));
    consume(stream<RunEvent>(`/api/conversations/${cid}/messages`, { content, provider_id: activeProvider, model: activeModel }, ctrl.signal));
  };

  // ---- voice: mic input and spoken replies
  const [recording, setRecording] = useState<"idle" | "listening" | "transcribing">("idle");
  const [level, setLevel] = useState(0);
  const recorder = useRef<Recorder | null>(null);
  const [speakReplies, setSpeakReplies] = useState(() => {
    try {
      return localStorage.getItem("ithaca.speak") === "1";
    } catch {
      return false;
    }
  });
  const finishRecording = useCallback(async () => {
    const rec = recorder.current;
    recorder.current = null;
    if (!rec) return;
    setRecording("transcribing");
    try {
      const text = (await transcribe(await rec.stop())).trim();
      if (text) sendRef.current(text);
      else toast("Didn't catch that — try again.");
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setRecording("idle");
      setLevel(0);
    }
  }, [toast]);
  const toggleMic = async () => {
    if (recording === "listening") return finishRecording();
    if (recording !== "idle") return;
    try {
      recorder.current = await startRecording(() => void finishRecording(), (rms) => setLevel(rms));
      setRecording("listening");
    } catch (e) {
      toast(`Microphone unavailable: ${(e as Error).message}`, true);
    }
  };
  const wasRunning = useRef(false);
  useEffect(() => {
    if (wasRunning.current && !live.running && speakReplies) {
      const last = [...live.items].reverse().find((i) => i.kind === "assistant" && i.content.trim());
      if (last && last.kind === "assistant") speak(last.content);
    }
    wasRunning.current = live.running;
  }, [live.running, live.items, speakReplies]);

  const sendRef = useRef(send);
  sendRef.current = send;

  const stop = async () => {
    if (live.runId) await api.post(`/api/runs/${live.runId}/cancel`);
  };

  const decide = async (approvalId: string, approve: boolean) => {
    try {
      await api.post(`/api/approvals/${approvalId}`, { approve });
    } catch (e) {
      toast((e as Error).message, true);
    }
  };

  const changeModel = async (m: { provider_id: string; model: string }) => {
    setPendingModel(m);
    if (conversationId) {
      await api.patch(`/api/conversations/${conversationId}`, { provider_id: m.provider_id, model: m.model });
      setConv((c) => (c ? { ...c, provider_id: m.provider_id, model: m.model } : c));
    }
    refreshModels();
  };

  const detachDoc = async () => {
    if (!conversationId) return;
    const c = await api.patch<Conversation>(`/api/conversations/${conversationId}`, { clear_document: true });
    setConv((prev) => (prev ? { ...prev, ...c } : prev));
  };

  const empty = live.items.length === 0;
  const stage = activityStage(live);

  return (
    <div className="view" style={{ position: "relative" }}>
      <div className="chat-head">
        <span className="chat-title">{conv?.title ?? "New chat"}</span>
        {conv?.document_id && (
          <span className="chip" title="This document is shared with the assistant each turn">
            <FileText size={14} /> Document attached
            <button className="icon-btn" style={{ width: 20, height: 20 }} onClick={detachDoc} aria-label="Detach document"><X size={12} /></button>
          </span>
        )}
        <span className="spacer" />
        {live.tainted ? (
          <span className="chip warn" title="This chat has read web or third-party content. Risky actions now need your approval.">
            <ShieldAlert size={14} /> External content
          </span>
        ) : (
          <span className="chip" title="Risky actions are gated by the runtime, not by the model">
            <ShieldCheck size={14} /> Protected
          </span>
        )}
      </div>

      <div
        className="thread"
        ref={threadRef}
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }}
      >
        <div className="thread-inner">
          {empty ? (
            <div className="hero">
              <div>
                <div className="hero-orb">
                  <Orb stage="connect" size={64} />
                </div>
                <h1>Hello, I'm Ithaca.</h1>
                <p>Your private AI workspace, running on your Mac.</p>
                <div className="replies" style={{ justifyContent: "center", maxWidth: 620, margin: "28px auto 0" }}>
                  {SUGGESTIONS.map((s, i) => (
                    <button key={s} className="reply" style={{ animationDelay: `${120 + i * 70}ms` }} onClick={() => send(s)}>
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          ) : (
            <Thread items={live.items} live={live} onDecide={decide} />
          )}
          {stage && (
            <div className="turn">
              <div className={`activity${stage === "write" ? " compact" : ""}`} data-stage={stage} role="status" aria-live="polite">
                <Orb stage={stage} size={stage === "write" ? 20 : 44} />
                <span className="activity-label" key={stage}>{stage === "recover" && live.status ? live.status : STAGE_LABEL[stage]}</span>
              </div>
            </div>
          )}
        </div>
      </div>

      <div className="composer-dock">
        {live.status && <div className="status-line">{live.status}</div>}
        <form
          className="glass composer"
          onSubmit={(e) => {
            e.preventDefault();
            send();
          }}
        >
          <AutoTextarea
            value={draft}
            onChange={setDraft}
            onSubmit={() => send()}
            placeholder={live.running ? "Ithaca is working…" : "Message Ithaca"}
          />
          <div className="composer-bar">
            <ModelPicker providerId={activeProvider} model={activeModel} onChange={changeModel} />
            <span className="spacer" />
            <button
              type="button"
              className={`icon-btn${speakReplies ? " on" : ""}`}
              title={speakReplies ? "Speaking replies — click to mute" : "Speak replies aloud"}
              aria-label="Speak replies"
              onClick={() => {
                const next = !speakReplies;
                setSpeakReplies(next);
                if (!next) speechSynthesis?.cancel();
                try {
                  localStorage.setItem("ithaca.speak", next ? "1" : "0");
                } catch {
                  /* ignore */
                }
              }}
            >
              {speakReplies ? <Volume2 size={18} /> : <VolumeX size={18} />}
            </button>
            <button
              type="button"
              className={`mic-btn ${recording}`}
              style={{ ["--lvl" as string]: Math.min(1, level * 12).toFixed(2) }}
              title={recording === "listening" ? "Listening… click to finish" : "Speak a message"}
              aria-label="Voice input"
              disabled={live.running || recording === "transcribing"}
              onClick={toggleMic}
            >
              {recording === "transcribing" ? <Orb stage="listen" size={18} /> : <Mic size={18} />}
            </button>
            <button
              className="send-btn"
              type={live.running ? "button" : "submit"}
              onClick={live.running ? (e) => (e.preventDefault(), stop()) : undefined}
              disabled={!live.running && !draft.trim()}
              aria-label={live.running ? "Stop" : "Send"}
              title={live.running ? "Stop" : "Send"}
            >
              <span className="swap">
                <ArrowUp size={19} strokeWidth={2.2} className={live.running ? "off" : ""} />
                <Square size={13} fill="currentColor" className={live.running ? "" : "off"} />
              </span>
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function AutoTextarea({ value, onChange, onSubmit, placeholder }: { value: string; onChange: (v: string) => void; onSubmit: () => void; placeholder: string }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const fit = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
    el.style.overflowY = el.scrollHeight > 240 ? "auto" : "hidden";
  }, []);
  useLayoutEffect(fit, [value, fit]);
  // Re-fit when the width changes (window resize, sidebar, late font load).
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let w = el.clientWidth;
    const ro = new ResizeObserver(() => {
      if (el.clientWidth !== w) {
        w = el.clientWidth;
        fit();
      }
    });
    ro.observe(el);
    document.fonts?.ready.then(fit);
    return () => ro.disconnect();
  }, [fit]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "l") {
        e.preventDefault();
        ref.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    ref.current?.focus();
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return (
    <textarea
      ref={ref}
      rows={1}
      value={value}
      placeholder={placeholder}
      aria-label="Message"
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
          e.preventDefault();
          onSubmit();
        }
      }}
    />
  );
}

/* -------------------------------------------------------------- thread */

function Thread({ items, live, onDecide }: { items: Item[]; live: LiveState; onDecide: (id: string, ok: boolean) => void }) {
  const results = new Map<string, Extract<Item, { kind: "tool_result" }>>();
  const approvals = new Map<string, Extract<Item, { kind: "approval" }>>();
  for (const it of items) {
    if (it.kind === "tool_result") results.set(it.tool_call_id, it);
    if (it.kind === "approval") approvals.set(it.tool_call_id, it);
  }
  const out: ReactElement[] = [];
  for (const it of items) {
    if (it.kind === "user") {
      out.push(
        <div className="turn me" key={it.key}>
          <div className="bubble">{it.content}</div>
        </div>,
      );
    } else if (it.kind === "assistant") {
      const hasText = it.content.trim().length > 0;
      if (!hasText && !it.tool_calls.length && !it.thinking.trim()) continue;
      out.push(
        <div className={`turn${hasText ? "" : " tools-only"}`} key={it.key}>
          {it.thinking.trim() && <Thinking text={it.thinking} streaming={Boolean(it.streaming) && !hasText} />}
          {hasText && (
            <div className="bubble">
              <Markdown text={it.content} />
            </div>
          )}
          {it.tool_calls.map((c) => (
            <div key={c.id} style={{ display: "contents" }}>
              <ToolCard call={c} result={results.get(c.id)} running={live.runningTools.has(c.id)} progress={live.progress[c.id]} />
              {approvals.get(c.id) && <Approval item={approvals.get(c.id)!} onDecide={onDecide} />}
            </div>
          ))}
          {!it.streaming && it.model && hasText && !it.tool_calls.length && <div className="turn-meta">{modelLabel(it.model)}</div>}
        </div>,
      );
    } else if (it.kind === "error") {
      out.push(<div className="notice error" key={it.key}>{it.message}</div>);
    } else if (it.kind === "notice") {
      out.push(<div className="notice" key={it.key}>{it.text}</div>);
    }
  }
  return <>{out}</>;
}

function Thinking({ text, streaming }: { text: string; streaming: boolean }) {
  return (
    <details className="thinking" open={streaming}>
      <summary>
        {streaming ? <Orb stage="think" size={16} /> : <Brain size={14} />} <span className={streaming ? "shimmer" : undefined}>{streaming ? "Thinking" : "Reasoning"}</span>
      </summary>
      <div className="body">{text}</div>
    </details>
  );
}

const TOOL_META: Record<string, { label: string; icon: ReactElement }> = {
  tool_search: { label: "Loading tools", icon: <Wrench size={15} /> },
  run_command: { label: "Ran a command", icon: <Terminal size={15} /> },
  web_search: { label: "Searched the web", icon: <Globe size={15} /> },
  web_fetch: { label: "Read a page", icon: <Globe size={15} /> },
  deep_research: { label: "Deep research", icon: <Search size={15} /> },
  memory_save: { label: "Saved to memory", icon: <Brain size={15} /> },
  memory_search: { label: "Checked memory", icon: <Brain size={15} /> },
  create_document: { label: "Created a document", icon: <FileText size={15} /> },
  edit_document: { label: "Edited a document", icon: <FileText size={15} /> },
  read_document: { label: "Read a document", icon: <FileText size={15} /> },
  open_app: { label: "Opened an app", icon: <Terminal size={15} /> },
  notes_create: { label: "Wrote a note in Notes", icon: <FileText size={15} /> },
  reminders_add: { label: "Added a reminder", icon: <Pin size={15} /> },
  open_in_app: { label: "Opened a file", icon: <FileText size={15} /> },
  run_applescript: { label: "Ran an AppleScript", icon: <Terminal size={15} /> },
  computer_screenshot: { label: "Looked at the screen", icon: <Search size={15} /> },
  open_url: { label: "Opened a website", icon: <Globe size={15} /> },
  browser_open: { label: "Browser: opened", icon: <Globe size={15} /> },
  browser_search: { label: "Browser: searched", icon: <Globe size={15} /> },
  browser_read: { label: "Browser: read the page", icon: <Globe size={15} /> },
  browser_click: { label: "Browser: clicked", icon: <Globe size={15} /> },
  browser_type: { label: "Browser: typed", icon: <Globe size={15} /> },
  browser_press: { label: "Browser: pressed a key", icon: <Globe size={15} /> },
  browser_scroll: { label: "Browser: scrolled", icon: <Globe size={15} /> },
  browser_back: { label: "Browser: went back", icon: <Globe size={15} /> },
  browser_tabs: { label: "Browser: tabs", icon: <Globe size={15} /> },
  browser_screenshot: { label: "Browser: screenshot", icon: <Globe size={15} /> },
  browser_type_here: { label: "Browser: typed", icon: <Globe size={15} /> },
  computer_click: { label: "Clicked", icon: <Wrench size={15} /> },
  computer_type: { label: "Typed", icon: <Wrench size={15} /> },
  computer_key: { label: "Pressed keys", icon: <Wrench size={15} /> },
  computer_scroll: { label: "Scrolled", icon: <Wrench size={15} /> },
};

function argSummary(call: ToolCall): string {
  const a = call.arguments ?? {};
  if (call.name === "computer_click" || call.name === "computer_scroll") return `(${a.x}, ${a.y})`;
  if (call.name === "browser_click") return `[${a.ref}]`;
  if (call.name === "browser_type") return `[${a.ref}] ${String(a.text ?? "").slice(0, 80)}`;
  const v = a.command ?? a.query ?? a.url ?? a.path ?? a.question ?? a.title ?? a.content ?? a.id ?? a.text ?? a.keys ?? a.name;
  return v === undefined ? "" : String(v).slice(0, 120);
}

function ToolCard({ call, result, running, progress }: { call: ToolCall; result?: Extract<Item, { kind: "tool_result" }>; running: boolean; progress?: string[] }) {
  const { openDocument } = useApp();
  const [open, setOpen] = useState(false);
  const meta = TOOL_META[call.name] ?? { label: call.name.replace(/^mcp__/, "").replace(/__/g, " · ").replace(/_/g, " "), icon: <Wrench size={15} /> };
  const dot = running || !result ? "run" : result.is_error ? "err" : "ok";
  const docId = result?.data && typeof result.data.document_id === "string" ? (result.data.document_id as string) : null;
  return (
    <div className="toolcard">
      <button className="toolcard-head" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        {dot === "run" ? <Orb stage={toolStage(call.name)} size={16} /> : <span className={`status-dot ${dot}`} />}
        {meta.icon}
        <span className="name">{meta.label}</span>
        <span className="arg">{argSummary(call)}</span>
        {result?.untrusted && <span className="badge warn">external</span>}
        {result?.duration_ms !== undefined && <span className="faint" style={{ fontSize: 11.5 }}>{(result.duration_ms / 1000).toFixed(1)}s</span>}
        <ChevronRight size={14} style={{ transform: open ? "rotate(90deg)" : undefined, transition: "transform .2s" }} />
      </button>
      {progress && running && (
        <div className="toolcard-body">
          <ul className="progress-list">{progress.slice(-6).map((p, i) => <li key={i}>{p}</li>)}</ul>
        </div>
      )}
      {result?.image && (
        <div className="toolcard-body">
          <a href={result.image} target="_blank" rel="noreferrer">
            <img src={result.image} alt="Screenshot" className="shot" />
          </a>
        </div>
      )}
      {docId && (
        <div className="toolcard-body">
          <button className="btn sm" onClick={() => openDocument(docId)}>
            <FileText size={14} /> Open document
          </button>
        </div>
      )}
      {open && (
        <div className="toolcard-body">
          <pre>{JSON.stringify(call.arguments, null, 2)}</pre>
          {result && <pre style={{ color: result.is_error ? "var(--danger)" : undefined }}>{result.display || "(no output)"}</pre>}
        </div>
      )}
    </div>
  );
}

function Approval({ item, onDecide }: { item: Extract<Item, { kind: "approval" }>; onDecide: (id: string, ok: boolean) => void }) {
  const pending = item.status === "pending";
  return (
    <div className="glass approval">
      <h4>
        <ShieldAlert size={17} /> {pending ? "Allow this action?" : item.status === "approved" ? "Allowed" : "Declined"}
      </h4>
      <p>{item.reason}</p>
      <pre>
        {item.name}
        {"\n"}
        {JSON.stringify(item.arguments, null, 2)}
      </pre>
      {pending && (
        <div className="row">
          <button className="btn primary" onClick={() => onDecide(item.approval_id, true)}>Allow once</button>
          <button className="btn" onClick={() => onDecide(item.approval_id, false)}>Deny</button>
        </div>
      )}
    </div>
  );
}
