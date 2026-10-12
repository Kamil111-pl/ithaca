import { ArrowUp, FileText, Telescope } from "lucide-react";
import { useRef, useState } from "react";
import { Segmented } from "../components/Dialog";
import { Glass } from "../components/Glass";
import { Markdown } from "../components/Markdown";
import { ModelPicker } from "../components/ModelPicker";
import { Orb, type Stage } from "../components/Orb";
import { stream } from "../lib/api";
import { useApp } from "../lib/app";

type Depth = "quick" | "standard" | "deep";
type Step = { key: string; text: string; href?: string; state: "active" | "done" | "fail" };

const PHASES: Record<string, string> = {
  planning: "Planning search queries",
  searching: "Searching the web",
  reading: "Reading sources",
  writing: "Writing the report",
};

const PHASE_STAGE: Record<string, Stage> = { planning: "think", searching: "search", reading: "research", writing: "write" };

export function ResearchView() {
  const { models, openDocument, toast } = useApp();
  const fallback = models.find((m) => m.default && m.kind === "anthropic") ?? models.find((m) => m.default) ?? models[0];
  const [choice, setChoice] = useState<{ provider_id: string; model: string } | null>(null);
  const [question, setQuestion] = useState("");
  const [depth, setDepth] = useState<Depth>("standard");
  const [steps, setSteps] = useState<Step[]>([]);
  const [report, setReport] = useState("");
  const [docId, setDocId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const ctrl = useRef<AbortController | null>(null);

  const providerId = choice?.provider_id ?? fallback?.provider_id ?? null;
  const model = choice?.model ?? fallback?.model ?? null;

  const run = async () => {
    const q = question.trim();
    if (!q || busy) return;
    if (!providerId || !model) return toast("Add a model in Settings first.", true);
    setSteps([]);
    setReport("");
    setDocId(null);
    setBusy(true);
    ctrl.current = new AbortController();
    const push = (s: Step) =>
      setSteps((prev) => [...prev.map((p) => (p.state === "active" && s.key.startsWith("phase") ? { ...p, state: "done" as const } : p)), s]);
    try {
      for await (const ev of stream<Record<string, unknown>>("/api/research", { question: q, depth, provider_id: providerId, model }, ctrl.current.signal)) {
        switch (ev.type) {
          case "phase":
            push({ key: `phase-${ev.phase}`, text: PHASES[String(ev.phase)] ?? String(ev.phase), state: "active" });
            break;
          case "queries":
            push({ key: `q-${Date.now()}`, text: `Queries: ${(ev.queries as string[]).join(" · ")}`, state: "done" });
            break;
          case "candidates":
            push({ key: `c-${Date.now()}`, text: `Found ${ev.count} candidate sources`, state: "done" });
            break;
          case "source":
            push({ key: `s-${ev.index}`, text: `[${ev.index}] ${ev.title || ev.url}`, href: String(ev.url), state: "done" });
            break;
          case "source_failed":
            push({ key: `f-${ev.url}`, text: `Skipped ${new URL(String(ev.url)).hostname} — ${ev.error}`, state: "fail" });
            break;
          case "warning":
            push({ key: `w-${Date.now()}`, text: String(ev.message), state: "fail" });
            break;
          case "delta":
            setReport((r) => r + String(ev.text));
            break;
          case "done":
            setReport(String(ev.report));
            setDocId(String(ev.document_id));
            setSteps((prev) => prev.map((p) => (p.state === "active" ? { ...p, state: "done" } : p)));
            break;
          case "error":
            toast(String(ev.message), true);
            break;
        }
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") toast((e as Error).message, true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="view-scroll">
      <div className="page">
        <h1 className="page-title">Research</h1>
        <p className="page-sub">Ithaca plans queries, reads the sources, and writes a cited report you can keep as a document.</p>
        <Glass
          as="form"
          className="research-box"
          onSubmit={(e) => {
            e.preventDefault();
            run();
          }}
        >
          {busy ? <Orb stage="research" /> : <Telescope size={20} className="faint" />}
          <input placeholder="What do you want to know?" value={question} onChange={(e) => setQuestion(e.target.value)} aria-label="Research question" />
          {busy ? (
            <button type="button" className="btn" onClick={() => ctrl.current?.abort()}>Stop</button>
          ) : (
            <button className="send-btn" type="submit" disabled={!question.trim()} aria-label="Start research">
              <ArrowUp size={20} />
            </button>
          )}
        </Glass>
        <div className="row" style={{ margin: "14px 4px 22px", flexWrap: "wrap" }}>
          <Segmented<Depth>
            value={depth}
            onChange={setDepth}
            options={[
              { value: "quick", label: "Quick" },
              { value: "standard", label: "Standard" },
              { value: "deep", label: "Deep" },
            ]}
          />
          <span className="spacer" />
          <ModelPicker providerId={providerId} model={model} onChange={(m) => setChoice(m)} />
        </div>

        {steps.length > 0 && (
          <Glass className="card" style={{ marginBottom: 16 }}>
            <ul className="timeline">
              {steps.map((s) => (
                <li key={s.key} className={s.state}>
                  {s.state === "active" && busy ? (
                    <Orb stage={PHASE_STAGE[s.key.slice("phase-".length)] ?? "think"} size={16} />
                  ) : (
                    <span className="dot" style={s.state === "fail" ? { background: "var(--danger)" } : undefined} />
                  )}
                  {s.href ? <a href={s.href} target="_blank" rel="noreferrer">{s.text}</a> : <span>{s.text}</span>}
                </li>
              ))}
            </ul>
          </Glass>
        )}

        {report && (
          <Glass className="card" style={{ padding: "26px 30px" }}>
            {docId && (
              <div className="row" style={{ marginBottom: 12 }}>
                <span className="badge ok">Saved</span>
                <span className="spacer" />
                <button className="btn sm" onClick={() => openDocument(docId)}>
                  <FileText size={14} /> Open as document
                </button>
              </div>
            )}
            <Markdown text={report} />
          </Glass>
        )}
      </div>
    </div>
  );
}
