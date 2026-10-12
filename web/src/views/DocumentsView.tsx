import { Check, Columns2, Eye, FilePlus2, MessageSquare, MoreHorizontal, PenLine, Search, Sparkles, Trash2, Wand2, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Glass } from "../components/Glass";
import { Markdown } from "../components/Markdown";
import { Orb, STAGE_LABEL } from "../components/Orb";
import { useMenu } from "../components/Menu";
import { api, stream } from "../lib/api";
import { relTime, useApp } from "../lib/app";
import type { Conversation, Doc, DocSummary } from "../lib/types";

export function DocumentList() {
  const { documentId, openDocument } = useApp();
  const [docs, setDocs] = useState<DocSummary[]>([]);
  const [q, setQ] = useState("");
  const version = useDocsVersion();

  useEffect(() => {
    const t = setTimeout(() => {
      api.get<DocSummary[]>(`/api/documents${q ? `?q=${encodeURIComponent(q)}` : ""}`).then(setDocs).catch(() => {});
    }, q ? 200 : 0);
    return () => clearTimeout(t);
  }, [q, version]);

  const create = async () => {
    const d = await api.post<Doc>("/api/documents", { title: "Untitled", content: "" });
    bumpDocs();
    openDocument(d.id);
  };

  return (
    <>
      <div className="side-actions">
        <div className="side-search">
          <Search size={15} />
          <input placeholder="Search documents" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search documents" />
        </div>
        <button className="icon-btn on" style={{ width: 38, height: 38 }} onClick={create} aria-label="New document">
          <FilePlus2 size={18} />
        </button>
      </div>
      <div className="side-list">
        <button className={`side-item${!documentId ? " on" : ""}`} onClick={() => openDocument(null)}>
          <span className="t">All documents</span>
        </button>
        <div className="side-group">Documents</div>
        {docs.map((d) => (
          <button key={d.id} className={`side-item${d.id === documentId ? " on" : ""}`} onClick={() => openDocument(d.id)}>
            <span className="t">{d.title}</span>
            <span className="faint" style={{ fontSize: 11.5 }}>{relTime(d.updated_at)}</span>
          </button>
        ))}
        {!docs.length && <div className="side-empty">{q ? "Nothing matches." : "No documents yet."}</div>}
      </div>
    </>
  );
}

/* tiny cross-component refresh signal */
let docsVersion = 0;
const listeners = new Set<(v: number) => void>();
function bumpDocs() {
  docsVersion++;
  listeners.forEach((l) => l(docsVersion));
}
function useDocsVersion() {
  const [v, setV] = useState(docsVersion);
  useEffect(() => {
    listeners.add(setV);
    return () => void listeners.delete(setV);
  }, []);
  return v;
}

export function DocumentsView() {
  const { documentId } = useApp();
  return documentId ? <Editor key={documentId} id={documentId} /> : <Library />;
}

function Library() {
  const { openDocument } = useApp();
  const [docs, setDocs] = useState<DocSummary[] | null>(null);
  const version = useDocsVersion();
  useEffect(() => {
    api.get<DocSummary[]>("/api/documents").then(setDocs).catch(() => setDocs([]));
  }, [version]);
  const create = async () => {
    const d = await api.post<Doc>("/api/documents", { title: "Untitled", content: "" });
    bumpDocs();
    openDocument(d.id);
  };
  return (
    <div className="view-scroll">
      <div className="page" style={{ maxWidth: 1080 }}>
        <div className="row">
          <h1 className="page-title">Documents</h1>
          <span className="spacer" />
          <button className="btn primary" onClick={create}>
            <FilePlus2 size={16} /> New document
          </button>
        </div>
        <p className="page-sub">Drafts, notes and research reports. Ithaca can read and edit them when you ask.</p>
        <div className="doc-grid">
          {docs?.map((d) => (
            <button key={d.id} className="glass doc-card" onClick={() => openDocument(d.id)}>
              <h4>{d.title}</h4>
              <p>{(d.preview ?? "").replace(/[#*_>`]/g, "") || "Empty document"}</p>
              <span className="spacer" />
              <span className="faint" style={{ fontSize: 12 }}>Edited {relTime(d.updated_at)}</span>
            </button>
          ))}
        </div>
        {docs && !docs.length && (
          <Glass className="card" style={{ textAlign: "center", padding: 40 }}>
            <PenLine size={28} />
            <h3 style={{ margin: "10px 0 4px" }}>Nothing here yet</h3>
            <p className="muted" style={{ margin: 0 }}>Create a document, or ask Ithaca to draft one for you.</p>
          </Glass>
        )}
      </div>
    </div>
  );
}

const QUICK = ["Improve clarity and flow", "Fix grammar and spelling", "Make it more concise", "Continue writing", "Turn into bullet points"];

function Editor({ id }: { id: string }) {
  const { openDocument, openConversation, toast, bumpConversations } = useApp();
  const [doc, setDoc] = useState<Doc | null>(null);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [mode, setMode] = useState<"write" | "split" | "read">(() => (localStorage.getItem("ithaca.docmode") as "write") || "split");
  const [assist, setAssist] = useState(false);
  const [saved, setSaved] = useState(true);
  const menu = useMenu();
  const ta = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    api.get<Doc>(`/api/documents/${id}`).then((d) => {
      setDoc(d);
      setTitle(d.title);
      setContent(d.content);
    }).catch(() => openDocument(null));
  }, [id, openDocument]);

  // Debounced autosave.
  useEffect(() => {
    if (!doc || (title === doc.title && content === doc.content)) return;
    setSaved(false);
    const t = setTimeout(async () => {
      try {
        const d = await api.patch<Doc>(`/api/documents/${id}`, { title, content });
        setDoc(d);
        setSaved(true);
        bumpDocs();
      } catch (e) {
        toast((e as Error).message, true);
      }
    }, 600);
    return () => clearTimeout(t);
  }, [title, content, doc, id, toast]);

  useEffect(() => {
    try {
      localStorage.setItem("ithaca.docmode", mode);
    } catch {
      /* ignore */
    }
  }, [mode]);

  const chatAbout = async () => {
    const c = await api.post<Conversation>("/api/conversations", { title: `About: ${title}`, document_id: id });
    bumpConversations();
    openConversation(c.id);
  };

  const selection = () => {
    const el = ta.current;
    if (!el || el.selectionStart === el.selectionEnd) return null;
    return { start: el.selectionStart, end: el.selectionEnd, text: content.slice(el.selectionStart, el.selectionEnd) };
  };

  if (!doc) return <div className="view" />;
  const split = mode === "split";
  return (
    <div className="doc-editor">
      <div className="doc-toolbar">
        <input className="doc-title" value={title} onChange={(e) => setTitle(e.target.value)} aria-label="Title" />
        <span className="faint" style={{ fontSize: 12.5 }}>{saved ? "Saved" : "Saving…"}</span>
        <div className="segmented glass thin">
          <button className={mode === "write" ? "on" : ""} onClick={() => setMode("write")} title="Write"><PenLine size={15} /></button>
          <button className={mode === "split" ? "on" : ""} onClick={() => setMode("split")} title="Split"><Columns2 size={15} /></button>
          <button className={mode === "read" ? "on" : ""} onClick={() => setMode("read")} title="Preview"><Eye size={15} /></button>
        </div>
        <button className={`btn${assist ? " primary" : ""}`} onClick={() => setAssist((a) => !a)}>
          <Sparkles size={15} /> Assist
        </button>
        <button className="btn" onClick={chatAbout}>
          <MessageSquare size={15} /> Chat about it
        </button>
        <button
          className="icon-btn"
          aria-label="More"
          onClick={(e) =>
            menu.open(e.currentTarget, [
              { label: "Copy Markdown", icon: <Check size={18} />, onSelect: () => navigator.clipboard?.writeText(content).then(() => toast("Copied")) },
              {
                label: "Download .md",
                icon: <FilePlus2 size={18} />,
                onSelect: () => {
                  const a = document.createElement("a");
                  a.href = URL.createObjectURL(new Blob([content], { type: "text/markdown" }));
                  a.download = `${title || "document"}.md`;
                  a.click();
                },
              },
              { separator: true },
              {
                label: "Delete document",
                danger: true,
                icon: <Trash2 size={18} />,
                onSelect: async () => {
                  if (!confirm(`Delete “${title}”? This can't be undone.`)) return;
                  await api.del(`/api/documents/${id}`);
                  bumpDocs();
                  openDocument(null);
                },
              },
            ])
          }
        >
          <MoreHorizontal size={18} />
        </button>
      </div>
      <div className={`doc-body${split ? " split" : ""}${assist ? " assist" : ""}`}>
        {mode !== "read" && (
          <Glass className="doc-pane">
            <textarea ref={ta} value={content} onChange={(e) => setContent(e.target.value)} placeholder="Start writing in Markdown…" spellCheck />
          </Glass>
        )}
        {mode !== "write" && (
          <Glass className="doc-pane">
            <Markdown text={content || "*Nothing to preview yet.*"} />
          </Glass>
        )}
        {assist && (
          <AssistPanel
            docId={id}
            getSelection={selection}
            onApply={(text, sel) => {
              setContent((c) => (sel ? c.slice(0, sel.start) + text + c.slice(sel.end) : text));
              toast("Applied");
            }}
            onClose={() => setAssist(false)}
          />
        )}
      </div>
      {menu.node}
    </div>
  );
}

function AssistPanel({
  docId,
  getSelection,
  onApply,
  onClose,
}: {
  docId: string;
  getSelection: () => { start: number; end: number; text: string } | null;
  onApply: (text: string, sel: { start: number; end: number } | null) => void;
  onClose: () => void;
}) {
  const { toast } = useApp();
  const [instruction, setInstruction] = useState("");
  const [out, setOut] = useState("");
  const [busy, setBusy] = useState(false);
  const [sel, setSel] = useState<{ start: number; end: number; text: string } | null>(null);
  const ctrl = useRef<AbortController | null>(null);

  const run = useCallback(
    async (text: string) => {
      const s = getSelection();
      setSel(s);
      setOut("");
      setBusy(true);
      ctrl.current = new AbortController();
      try {
        for await (const ev of stream<{ type: string; text?: string; message?: string }>(
          `/api/documents/${docId}/assist`,
          { instruction: text, selection: s?.text ?? null },
          ctrl.current.signal,
        )) {
          if (ev.type === "text") setOut((o) => o + (ev.text ?? ""));
          if (ev.type === "error") toast(ev.message ?? "Assist failed", true);
        }
      } catch (e) {
        if ((e as Error).name !== "AbortError") toast((e as Error).message, true);
      } finally {
        setBusy(false);
      }
    },
    [docId, getSelection, toast],
  );

  return (
    <Glass className="doc-pane assist-panel">
      <div className="row">
        <Wand2 size={17} />
        <b>Assist</b>
        <span className="spacer" />
        <button className="icon-btn" onClick={onClose} aria-label="Close"><X size={16} /></button>
      </div>
      <p className="faint" style={{ margin: 0, fontSize: 12.5 }}>Select text in the editor to revise just that part; otherwise the whole document is revised.</p>
      <div className="replies" style={{ justifyContent: "flex-start", marginTop: 0 }}>
        {QUICK.map((q) => (
          <button key={q} className="reply" disabled={busy} onClick={() => run(q)}>{q}</button>
        ))}
      </div>
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          if (instruction.trim()) run(instruction.trim());
        }}
      >
        <input className="input" placeholder="Or describe a change…" value={instruction} onChange={(e) => setInstruction(e.target.value)} />
      </form>
      <div className="assist-out">
        {out ? (
          <>
            <Markdown text={out} />
            {busy && (
              <div className="activity compact">
                <Orb stage="write" />
                <span className="activity-label">{STAGE_LABEL.write}</span>
              </div>
            )}
          </>
        ) : busy ? (
          <div className="activity">
            <Orb stage="think" />
            <span className="activity-label">{STAGE_LABEL.think}</span>
          </div>
        ) : (
          <span className="faint">Suggestions appear here.</span>
        )}
      </div>
      <div className="row">
        {busy ? (
          <button className="btn" onClick={() => ctrl.current?.abort()}>Stop</button>
        ) : (
          <>
            <button className="btn primary" disabled={!out} onClick={() => { onApply(out.trim(), sel); setOut(""); }}>
              <Check size={15} /> {sel ? "Replace selection" : "Replace document"}
            </button>
            <button className="btn ghost" disabled={!out} onClick={() => setOut("")}>Discard</button>
          </>
        )}
      </div>
    </Glass>
  );
}
