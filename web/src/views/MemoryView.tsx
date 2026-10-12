import { Brain, Check, ListChecks, Pencil, Plus, Search, ShieldAlert, Trash2, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Glass } from "../components/Glass";
import { api } from "../lib/api";
import { relTime, useApp } from "../lib/app";
import type { Memory } from "../lib/types";

export function MemoryView() {
  const { toast } = useApp();
  const [items, setItems] = useState<Memory[] | null>(null);
  const [q, setQ] = useState("");
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [editText, setEditText] = useState("");

  const load = () => api.get<Memory[]>(`/api/memories${q ? `?q=${encodeURIComponent(q)}` : ""}`).then(setItems).catch(() => setItems([]));
  useEffect(() => {
    const t = setTimeout(load, q ? 220 : 0);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q]);

  const add = async () => {
    if (!draft.trim()) return;
    try {
      await api.post("/api/memories", { content: draft.trim() });
      setDraft("");
      load();
    } catch (e) {
      toast((e as Error).message, true);
    }
  };

  return (
    <div className="view-scroll">
      <div className="page">
        <h1 className="page-title">Memory</h1>
        <p className="page-sub">What Ithaca remembers about you. Relevant memories are recalled automatically in chat; edit or remove anything.</p>

        <Glass as="form" className="research-box" style={{ marginBottom: 12 }} onSubmit={(e) => { e.preventDefault(); add(); }}>
          <Brain size={19} className="faint" />
          <input placeholder="Teach Ithaca something, e.g. “I prefer metric units”" value={draft} onChange={(e) => setDraft(e.target.value)} />
          <button className="send-btn" disabled={!draft.trim()} aria-label="Add memory"><Plus size={20} /></button>
        </Glass>
        <div className="side-search" style={{ margin: "0 0 18px" }}>
          <Search size={15} />
          <input placeholder="Search memories" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>

        {items?.map((m) => (
          <Glass thin key={m.id} className="memory-item">
            {m.tainted ? <span title="Saved while reading web content — treated as untrusted"><ShieldAlert size={18} color="var(--warn)" /></span> : <Brain size={18} className="faint" />}
            <div className="content">
              {editing === m.id ? (
                <textarea className="textarea" value={editText} onChange={(e) => setEditText(e.target.value)} autoFocus />
              ) : (
                m.content
              )}
              <div className="faint" style={{ fontSize: 12, marginTop: 4 }}>
                {relTime(m.created_at)}
                {m.tainted && " · from a web-tainted chat (editing it marks it trusted)"}
              </div>
            </div>
            {editing === m.id ? (
              <>
                <button className="icon-btn" aria-label="Save" onClick={async () => { await api.patch(`/api/memories/${m.id}`, { content: editText }); setEditing(null); load(); }}><Check size={16} /></button>
                <button className="icon-btn" aria-label="Cancel" onClick={() => setEditing(null)}><X size={16} /></button>
              </>
            ) : (
              <>
                <button className="icon-btn" aria-label="Edit" onClick={() => { setEditing(m.id); setEditText(m.content); }}><Pencil size={16} /></button>
                <button className="icon-btn" aria-label="Delete" onClick={async () => { await api.del(`/api/memories/${m.id}`); load(); }}><Trash2 size={16} /></button>
              </>
            )}
          </Glass>
        ))}
        {items && !items.length && <p className="muted" style={{ textAlign: "center", padding: 30 }}>{q ? "No memories match." : "No memories yet. Ithaca saves useful facts as you chat."}</p>}

        <Procedures />
      </div>
    </div>
  );
}

interface Procedure {
  id: string;
  steps: string;
  requests: string[];
  successes: number;
  failures: number;
  updated_at: number;
}

/** Tool steps Ithaca learned from tasks that worked; shown as examples for similar requests. */
function Procedures() {
  const [items, setItems] = useState<Procedure[] | null>(null);
  const load = () => api.get<Procedure[]>("/api/procedures").then(setItems).catch(() => setItems([]));
  useEffect(() => {
    load();
  }, []);
  if (!items?.length) return null;
  return (
    <>
      <h3 className="page-title" style={{ fontSize: 16, lineHeight: "34px", fontWeight: 400, margin: "28px 0 2px" }}>Learned procedures</h3>
      <p className="page-sub" style={{ marginBottom: 14 }}>Steps from tasks that worked, reused for similar requests. What was typed or written is never kept.</p>
      {items.map((p) => (
        <Glass thin key={p.id} className="memory-item">
          <ListChecks size={18} className="faint" />
          <div className="content">
            <div>“{p.requests[0]}”{p.requests.length > 1 && <span className="faint"> and {p.requests.length - 1} similar</span>}</div>
            <div className="mono faint" style={{ marginTop: 4, wordBreak: "break-word" }}>{p.steps}</div>
            <div className="faint" style={{ fontSize: 12, marginTop: 4 }}>
              Worked {p.successes}×{p.failures ? ` · failed ${p.failures}×` : ""} · {relTime(p.updated_at)}
            </div>
          </div>
          <button className="icon-btn" aria-label="Forget this procedure" onClick={async () => { await api.del(`/api/procedures/${p.id}`); load(); }}><Trash2 size={16} /></button>
        </Glass>
      ))}
    </>
  );
}
