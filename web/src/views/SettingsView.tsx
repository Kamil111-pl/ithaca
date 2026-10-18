import { AppWindow, Chrome, Copy, Cpu, Download, Eye, Mic, Monitor, FlaskConical, KeyRound, LogOut, Plug, Plus, RefreshCw, ServerCog, Shield, Sparkles, Star, Trash2, Wrench } from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Dialog, Segmented, Switch } from "../components/Dialog";
import { Glass } from "../components/Glass";
import { modelLabel } from "../components/ModelPicker";
import { api } from "../lib/api";
import { useApp, type Theme } from "../lib/app";
import type { Provider } from "../lib/types";

const PRESETS: { name: string; kind: "openai" | "anthropic"; base_url: string; key: boolean; model?: string; hint: string }[] = [
  { name: "Claude", kind: "anthropic", base_url: "", key: true, model: "claude-opus-5-5", hint: "Anthropic API key (or set ANTHROPIC_API_KEY before starting Ithaca)." },
  { name: "Ithaca Local (MLX)", kind: "openai", base_url: "http://127.0.0.1:8080/v1", key: false, model: "ithaca-local", hint: "The distilled on-device model. Start it with ./ithaca model serve." },
  { name: "Ollama", kind: "openai", base_url: "http://127.0.0.1:11434/v1", key: false, hint: "Ollama's OpenAI-compatible endpoint." },
  { name: "LM Studio", kind: "openai", base_url: "http://127.0.0.1:1234/v1", key: false, hint: "Start the local server in LM Studio first." },
  { name: "OpenAI", kind: "openai", base_url: "https://api.openai.com/v1", key: true, hint: "Any OpenAI API key." },
  { name: "OpenRouter", kind: "openai", base_url: "https://openrouter.ai/api/v1", key: true, hint: "Hundreds of models with one key." },
  { name: "Custom", kind: "openai", base_url: "", key: true, hint: "Any OpenAI-compatible /v1 endpoint." },
];

export function SettingsView() {
  return (
    <div className="view-scroll">
      <div className="page">
        <h1 className="page-title">Settings</h1>
        <p className="page-sub">Everything is stored in one SQLite file on your drive.</p>
        <Providers />
        <AgentSettings />
        <SandboxSettings />
        <BrowserSettings />
        <ConnectedApps />
        <ComputerUse />
        <VoiceSettings />
        <WebAndMemory />
        <McpServers />
        <LocalModel />
        <Appearance />
        <Account />
      </div>
    </div>
  );
}

function Providers() {
  const { refreshModels, settings, saveSettings, toast } = useApp();
  const [rows, setRows] = useState<Provider[]>([]);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const load = useCallback(() => api.get<Provider[]>("/api/providers").then(setRows).catch(() => {}), []);
  useEffect(() => {
    load();
  }, [load]);
  const after = () => {
    load();
    refreshModels();
  };

  const probe = async (p: Provider, model: string) => {
    setBusy(`${p.id}:${model}`);
    try {
      const r = await api.post<{ tools: boolean; error: string | null }>(`/api/providers/${p.id}/probe`, { model });
      toast(r.error ? `Probe failed: ${r.error}` : r.tools ? `${modelLabel(model)} can call tools` : `${modelLabel(model)} can't call tools — chat-only`, Boolean(r.error));
      after();
    } finally {
      setBusy(null);
    }
  };

  return (
    <Glass as="section" className="section">
      <h3><Cpu size={18} /> Models</h3>
      <p>Connect Claude, local models, or any OpenAI-compatible server. Ithaca probes each model once to check tool calling instead of guessing from its name.</p>
      {rows.map((p) => (
        <div className="provider" key={p.id}>
          <div className="row">
            {p.kind === "anthropic" ? <Sparkles size={17} /> : <ServerCog size={17} />}
            <b>{p.name}</b>
            <span className="faint mono" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 260 }}>{p.base_url || "api.anthropic.com"}</span>
            <span className="spacer" />
            {p.kind === "anthropic" || p.has_api_key ? <span className={`badge ${p.has_api_key ? "ok" : "warn"}`}>{p.has_api_key ? "key set" : "no key"}</span> : null}
            <button className="icon-btn" title="Refresh model list" aria-label="Refresh model list" onClick={async () => {
              setBusy(p.id);
              try { await api.post(`/api/providers/${p.id}/refresh`); after(); } catch (e) { toast((e as Error).message, true); } finally { setBusy(null); }
            }}>
              <RefreshCw size={16} className={busy === p.id ? "spin" : ""} />
            </button>
            <button className="icon-btn" title="Remove" aria-label="Remove" onClick={async () => { if (confirm(`Remove ${p.name}?`)) { await api.del(`/api/providers/${p.id}`); after(); } }}>
              <Trash2 size={16} />
            </button>
          </div>
          <div style={{ marginTop: 8 }}>
            {p.models.slice(0, 40).map((m) => {
              const isDefault = settings["chat.default_provider_id"] === p.id && settings["chat.default_model"] === m.id;
              return (
                <div className="model-row" key={m.id}>
                  <span style={{ flex: 1 }}>{m.id}</span>
                  <span className={`badge ${m.tools ? "ok" : m.tools === false ? "warn" : ""}`}>{m.tools ? "tools" : m.tools === false ? "chat only" : "untested"}</span>
                  <button
                    className={`btn sm ${(m.vision ?? p.kind === "anthropic") ? "" : "ghost"}`}
                    title="Can this model see images? Needed for computer use."
                    onClick={async () => {
                      const models = p.models.map((x) => (x.id === m.id ? { ...x, vision: !(m.vision ?? p.kind === "anthropic") } : x));
                      await api.patch(`/api/providers/${p.id}`, { models });
                      after();
                    }}
                  >
                    <Eye size={14} /> {(m.vision ?? p.kind === "anthropic") ? "vision" : "no vision"}
                  </button>
                  <button className="btn sm ghost" disabled={busy === `${p.id}:${m.id}`} onClick={() => probe(p, m.id)}>
                    <FlaskConical size={14} /> {busy === `${p.id}:${m.id}` ? "Testing…" : "Test"}
                  </button>
                  <button
                    className={`icon-btn${isDefault ? " on" : ""}`}
                    title={isDefault ? "Default model" : "Make default"}
                    aria-label={isDefault ? "Default model" : "Make default"}
                    onClick={() => saveSettings({ "chat.default_provider_id": p.id, "chat.default_model": m.id }).then(refreshModels)}
                  >
                    <Star size={15} fill={isDefault ? "currentColor" : "none"} />
                  </button>
                </div>
              );
            })}
            {!p.models.length && <div className="faint" style={{ fontSize: 13 }}>No models listed yet — press refresh once the server is running.</div>}
          </div>
        </div>
      ))}
      <button className="btn" onClick={() => setAdding(true)}><Plus size={16} /> Add provider</button>
      {adding && <AddProvider onClose={() => setAdding(false)} onDone={after} />}
    </Glass>
  );
}

function AddProvider({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const { toast } = useApp();
  const [preset, setPreset] = useState(PRESETS[0]);
  const [name, setName] = useState(PRESETS[0].name);
  const [baseUrl, setBaseUrl] = useState(PRESETS[0].base_url);
  const [key, setKey] = useState("");
  const [saving, setSaving] = useState(false);
  const pick = (p: (typeof PRESETS)[number]) => {
    setPreset(p);
    setName(p.name);
    setBaseUrl(p.base_url);
  };
  const save = async () => {
    setSaving(true);
    try {
      const r = await api.post<Provider>("/api/providers", { name, kind: preset.kind, base_url: baseUrl, api_key: key, default_model: preset.model ?? "" });
      if (r.warning) toast(r.warning, true);
      else toast(`${name} connected · ${r.models.length} models`);
      onDone();
      onClose();
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setSaving(false);
    }
  };
  return (
    <Dialog
      title="Add a provider"
      onClose={onClose}
      footer={<><button className="btn ghost" onClick={onClose}>Cancel</button><button className="btn primary" disabled={saving || (preset.kind === "openai" && !baseUrl)} onClick={save}>{saving ? "Connecting…" : "Connect"}</button></>}
    >
      <div className="replies" style={{ justifyContent: "flex-start", marginTop: 0 }}>
        {PRESETS.map((p) => (
          <button key={p.name} className="reply" style={p.name === preset.name ? { background: "var(--bubble-me)", color: "var(--bubble-me-text)" } : undefined} onClick={() => pick(p)}>
            {p.name}
          </button>
        ))}
      </div>
      <p className="muted" style={{ margin: 0, fontSize: 13.5 }}>{preset.hint}</p>
      <label className="field"><span>Name</span><input className="input" value={name} onChange={(e) => setName(e.target.value)} /></label>
      {preset.kind === "openai" && (
        <label className="field"><span>Base URL</span><input className="input mono" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="http://127.0.0.1:8080/v1" /></label>
      )}
      {preset.key && (
        <label className="field">
          <span>API key</span>
          <input className="input mono" type="password" value={key} onChange={(e) => setKey(e.target.value)} placeholder={preset.kind === "anthropic" ? "sk-ant-… (optional if set in env)" : "sk-…"} autoComplete="off" />
          <small>Stored only in your local database.</small>
        </label>
      )}
    </Dialog>
  );
}

function Row({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <div className="set-row">
      <div className="lbl"><b>{title}</b>{hint && <small>{hint}</small>}</div>
      {children}
    </div>
  );
}

function AgentSettings() {
  const { settings, saveSettings } = useApp();
  const [instructions, setInstructions] = useState("");
  useEffect(() => setInstructions(String(settings["agent.custom_instructions"] ?? "")), [settings]);
  return (
    <Glass as="section" className="section">
      <h3><Wrench size={18} /> Agent</h3>
      <p>How Ithaca uses tools and when it asks before acting.</p>
      <Row title="Tool loading" hint="Search keeps prompts small for local models; All sends every tool up front.">
        <Segmented value={String(settings["agent.tool_loading"] ?? "search")} onChange={(v) => saveSettings({ "agent.tool_loading": v })} options={[{ value: "search", label: "On demand" }, { value: "all", label: "All" }]} />
      </Row>
      <Row title="Approvals" hint="Smart asks only when an action is risky (e.g. after reading web content).">
        <Segmented value={String(settings["agent.approval_mode"] ?? "smart")} onChange={(v) => saveSettings({ "agent.approval_mode": v })} options={[{ value: "smart", label: "Smart" }, { value: "always", label: "Always ask" }, { value: "never", label: "Never" }]} />
      </Row>
      <Row title="Reasoning effort" hint="For Claude models. Higher is slower and more thorough.">
        <select className="select" style={{ width: 150 }} value={String(settings["agent.effort"] ?? "medium")} onChange={(e) => saveSettings({ "agent.effort": e.target.value })}>
          {["low", "medium", "high", "xhigh", "max"].map((v) => <option key={v} value={v}>{v}</option>)}
        </select>
      </Row>
      <Row title="Max steps per message">
        <input className="input" type="number" min={1} max={100} style={{ width: 90 }} value={Number(settings["agent.max_rounds"] ?? 24)} onChange={(e) => saveSettings({ "agent.max_rounds": Number(e.target.value) || 24 })} />
      </Row>
      <Row title="Learn from tasks that worked" hint="Reuses the steps of successful tasks for similar requests. See them under Memory.">
        <Switch on={Boolean(settings["agent.learn_procedures"] ?? true)} onChange={(v) => saveSettings({ "agent.learn_procedures": v })} />
      </Row>
      <Row title="Recall memories automatically">
        <Switch on={Boolean(settings["memory.auto_recall"] ?? true)} onChange={(v) => saveSettings({ "memory.auto_recall": v })} />
      </Row>
      <label className="field" style={{ marginTop: 10 }}>
        <span>Standing instructions</span>
        <textarea className="textarea" placeholder="e.g. Answer in British English. I'm a backend engineer; skip basics." value={instructions} onChange={(e) => setInstructions(e.target.value)} onBlur={() => instructions !== settings["agent.custom_instructions"] && saveSettings({ "agent.custom_instructions": instructions })} />
      </label>
    </Glass>
  );
}

function SandboxSettings() {
  const { settings, saveSettings, toast } = useApp();
  const [status, setStatus] = useState<{ backend: string; seatbelt_available: boolean; docker_available: boolean; workspace: string } | null>(null);
  const [out, setOut] = useState("");
  useEffect(() => {
    api.get<typeof status>("/api/sandbox").then(setStatus).catch(() => {});
  }, [settings]);
  return (
    <Glass as="section" className="section">
      <h3><Shield size={18} /> Sandbox</h3>
      <p>Shell commands run in a confined workspace: no access to your home folder or other drives, and no internet unless you allow it.</p>
      <Row title="Active backend" hint={status?.workspace}>
        <span className={`badge ${status?.backend === "off" || status?.backend === "unavailable" ? "danger" : "ok"}`}>{status?.backend ?? "…"}</span>
      </Row>
      <Row title="Mode" hint="Auto prefers macOS Seatbelt, then Docker.">
        <Segmented value={String(settings["sandbox.mode"] ?? "auto")} onChange={(v) => saveSettings({ "sandbox.mode": v })} options={[{ value: "auto", label: "Auto" }, { value: "seatbelt", label: "Seatbelt" }, { value: "docker", label: "Docker" }, { value: "off", label: "Off" }]} />
      </Row>
      <Row title="Allow network in sandbox" hint="Off by default; individual commands can still ask.">
        <Switch on={Boolean(settings["sandbox.allow_network"])} onChange={(v) => saveSettings({ "sandbox.allow_network": v })} />
      </Row>
      <Row title="Command timeout (seconds)">
        <input className="input" type="number" min={5} max={600} style={{ width: 90 }} value={Number(settings["sandbox.timeout_s"] ?? 60)} onChange={(e) => saveSettings({ "sandbox.timeout_s": Number(e.target.value) || 60 })} />
      </Row>
      <button className="btn" onClick={async () => {
        try {
          const r = await api.post<{ output: string; backend: string }>("/api/sandbox/test");
          setOut(`[${r.backend}]\n${r.output}`);
        } catch (e) { toast((e as Error).message, true); }
      }}>Run sandbox self-test</button>
      {out && <div className="out">{out}</div>}
    </Glass>
  );
}

function WebAndMemory() {
  const { settings, saveSettings, models } = useApp();
  const [url, setUrl] = useState("");
  useEffect(() => setUrl(String(settings["web.searxng_url"] ?? "")), [settings]);
  const embedProviders = Array.from(new Map(models.filter((m) => m.kind === "openai").map((m) => [m.provider_id, m.provider])).entries());
  return (
    <Glass as="section" className="section">
      <h3><Plug size={18} /> Web & embeddings</h3>
      <p>Search backend for web tools and research, and the embedding model for memory search.</p>
      <Row title="Search backend">
        <Segmented value={String(settings["web.search_backend"] ?? "duckduckgo")} onChange={(v) => saveSettings({ "web.search_backend": v })} options={[{ value: "duckduckgo", label: "DuckDuckGo" }, { value: "searxng", label: "SearXNG" }]} />
      </Row>
      {settings["web.search_backend"] === "searxng" && (
        <Row title="SearXNG URL">
          <input className="input mono" style={{ width: 260 }} value={url} onChange={(e) => setUrl(e.target.value)} onBlur={() => saveSettings({ "web.searxng_url": url })} placeholder="http://127.0.0.1:8888" />
        </Row>
      )}
      <Row title="Contact for web requests" hint="Optional URL or email added to Ithaca's user-agent. Some sites, like Wikipedia, block fetches without one.">
        <input className="input" style={{ width: 240 }} defaultValue={String(settings["web.contact"] ?? "")} onBlur={(e) => saveSettings({ "web.contact": e.target.value.trim() })} placeholder="you@example.com" />
      </Row>
      <Row title="Embeddings" hint="Built-in hashing needs no download; a real embedding model (e.g. nomic-embed-text via Ollama) understands meaning.">
        <select className="select" style={{ width: 200 }} value={String(settings["embeddings.provider_id"] ?? "")} onChange={(e) => saveSettings({ "embeddings.provider_id": e.target.value })}>
          <option value="">Built-in (hashed)</option>
          {embedProviders.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
        </select>
      </Row>
      {Boolean(settings["embeddings.provider_id"]) && (
        <Row title="Embedding model">
          <input className="input mono" style={{ width: 200 }} defaultValue={String(settings["embeddings.model"] ?? "")} onBlur={(e) => saveSettings({ "embeddings.model": e.target.value })} placeholder="nomic-embed-text" />
        </Row>
      )}
    </Glass>
  );
}

interface McpRow {
  id: string; name: string; command: string; args: string[]; transport: string; url: string; auth: string;
  trusted: boolean; enabled: boolean; connected: boolean; state: string; tools: string[]; error: string | null;
  auth_url: string | null; signed_in: boolean; transport_used?: string | null;
}

interface McpForm {
  name: string; kind: "local" | "remote"; command: string; args: string; url: string;
  transport: "auto" | "http" | "sse"; auth: "none" | "headers" | "oauth"; headerName: string; headerValue: string; trusted: boolean;
}

const EMPTY_MCP: McpForm = { name: "", kind: "remote", command: "npx", args: "-y @modelcontextprotocol/server-filesystem /path", url: "",
  transport: "auto", auth: "none", headerName: "Authorization", headerValue: "", trusted: false };

function McpServers() {
  const { toast } = useApp();
  const [rows, setRows] = useState<McpRow[]>([]);
  const [form, setForm] = useState<McpForm | null>(null);
  const [saving, setSaving] = useState(false);
  const load = useCallback(() => api.get<McpRow[]>("/api/mcp").then(setRows).catch(() => {}), []);
  useEffect(() => {
    load();
  }, [load]);
  // While anything is connecting or waiting for sign-in, keep the list fresh.
  useEffect(() => {
    if (!rows.some((r) => r.state === "connecting" || r.state === "auth_required")) return;
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  }, [rows, load]);

  const signIn = (r: McpRow) => {
    if (r.auth_url) window.open(r.auth_url, "_blank", "noopener");
  };

  const save = async () => {
    if (!form) return;
    setSaving(true);
    try {
      const body = form.kind === "local"
        ? { name: form.name, transport: "stdio", command: form.command,
            args: form.args.match(/(?:[^\s"]+|"[^"]*")+/g)?.map((a) => a.replace(/^"|"$/g, "")) ?? [], trusted: form.trusted }
        : { name: form.name, transport: form.transport, url: form.url, auth: form.auth, trusted: form.trusted,
            headers: form.auth === "headers" && form.headerName ? { [form.headerName]: form.headerValue } : {} };
      const r = await api.post<McpRow & { error?: string }>("/api/mcp", body);
      if (r.state === "auth_required" && r.auth_url) {
        toast("Opening the sign-in page…");
        window.open(r.auth_url, "_blank", "noopener");
      } else if (r.error) toast(`Saved, but it couldn't connect: ${r.error}`, true);
      else if (r.connected) toast(`${form.name} connected · ${r.tools.length} tools`);
      setForm(null);
      load();
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setSaving(false);
    }
  };

  const badge = (r: McpRow) =>
    r.connected ? <span className="badge ok">{r.tools.length} tools</span>
    : r.state === "auth_required" ? <span className="badge warn">needs sign-in</span>
    : r.state === "connecting" ? <span className="badge">connecting…</span>
    : r.error ? <span className="badge danger">error</span>
    : <span className="badge">off</span>;

  return (
    <Glass as="section" className="section">
      <h3><Plug size={18} /> MCP servers</h3>
      <p>Add tools from Model Context Protocol servers — local commands or remote servers by URL (with API keys or OAuth sign-in). Untrusted servers need approval for each call and their output is treated as external content.</p>
      {rows.map((r) => (
        <div className="provider" key={r.id}>
          <div className="row" style={{ flexWrap: "wrap" }}>
            {badge(r)}
            <b>{r.name}</b>
            <span className="badge">{r.transport === "stdio" ? "local" : r.auth === "oauth" ? "remote · OAuth" : r.auth === "headers" ? "remote · key" : "remote"}</span>
            <span className="faint mono" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1, minWidth: 120 }}>
              {r.transport === "stdio" ? `${r.command} ${r.args.join(" ")}` : r.url}
            </span>
            {r.state === "auth_required" && <button className="btn sm primary" onClick={() => signIn(r)}>Sign in</button>}
            {r.auth === "oauth" && r.signed_in && (
              <button className="btn sm ghost" onClick={async () => { await api.post(`/api/mcp/${r.id}/signout`); toast("Signed out"); load(); }}>Sign out</button>
            )}
            <span className="faint" style={{ fontSize: 12.5 }}>Trusted</span>
            <Switch on={r.trusted} onChange={async (v) => { await api.patch(`/api/mcp/${r.id}`, { trusted: v }); load(); }} />
            <button className="icon-btn" title="Reconnect" aria-label="Reconnect" onClick={async () => {
              const s = await api.post<McpRow>(`/api/mcp/${r.id}/connect`);
              if (s.state === "auth_required" && s.auth_url) window.open(s.auth_url, "_blank", "noopener");
              else if (s.error) toast(s.error, true);
              load();
            }}><RefreshCw size={15} /></button>
            <button className="icon-btn" title="Remove" aria-label="Remove" onClick={async () => { await api.del(`/api/mcp/${r.id}`); load(); }}><Trash2 size={15} /></button>
          </div>
          {r.error && <div className="out" style={{ color: "var(--danger)" }}>{r.error}</div>}
        </div>
      ))}
      <button className="btn" onClick={() => setForm({ ...EMPTY_MCP })}><Plus size={16} /> Add server</button>
      {form && (
        <Dialog
          title="Add MCP server"
          onClose={() => setForm(null)}
          footer={<>
            <button className="btn ghost" onClick={() => setForm(null)}>Cancel</button>
            <button className="btn primary" disabled={saving || !form.name || (form.kind === "local" ? !form.command : !form.url)} onClick={save}>
              {saving ? "Connecting…" : form.kind === "remote" && form.auth === "oauth" ? "Add & sign in" : "Add"}
            </button>
          </>}
        >
          <Segmented value={form.kind} onChange={(v) => setForm({ ...form, kind: v })} options={[{ value: "remote", label: "Remote (URL)" }, { value: "local", label: "Local (command)" }]} />
          <label className="field"><span>Name</span><input className="input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder={form.kind === "remote" ? "linear" : "filesystem"} /></label>
          {form.kind === "local" ? (
            <>
              <label className="field"><span>Command</span><input className="input mono" value={form.command} onChange={(e) => setForm({ ...form, command: e.target.value })} /></label>
              <label className="field"><span>Arguments</span><input className="input mono" value={form.args} onChange={(e) => setForm({ ...form, args: e.target.value })} /></label>
            </>
          ) : (
            <>
              <label className="field"><span>Server URL</span><input className="input mono" value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} placeholder="https://mcp.example.com/mcp" /></label>
              <Row title="Transport" hint="Auto tries Streamable HTTP, then the older SSE transport.">
                <Segmented value={form.transport} onChange={(v) => setForm({ ...form, transport: v })} options={[{ value: "auto", label: "Auto" }, { value: "http", label: "HTTP" }, { value: "sse", label: "SSE" }]} />
              </Row>
              <Row title="Authentication" hint={form.auth === "oauth" ? "You'll sign in with the service in a new tab; Ithaca stores and refreshes the tokens." : form.auth === "headers" ? "Sent with every request, e.g. an API key." : "For public servers."}>
                <Segmented value={form.auth} onChange={(v) => setForm({ ...form, auth: v })} options={[{ value: "none", label: "None" }, { value: "headers", label: "API key" }, { value: "oauth", label: "OAuth" }]} />
              </Row>
              {form.auth === "headers" && (
                <div style={{ display: "grid", gridTemplateColumns: "1fr 2fr", gap: 10 }}>
                  <label className="field"><span>Header</span><input className="input mono" value={form.headerName} onChange={(e) => setForm({ ...form, headerName: e.target.value })} /></label>
                  <label className="field"><span>Value</span><input className="input mono" type="password" autoComplete="off" value={form.headerValue} onChange={(e) => setForm({ ...form, headerValue: e.target.value })} placeholder="Bearer …" /></label>
                </div>
              )}
            </>
          )}
          <Row title="Trusted" hint="Skip approvals and don't treat its output as external content."><Switch on={form.trusted} onChange={(v) => setForm({ ...form, trusted: v })} /></Row>
        </Dialog>
      )}
    </Glass>
  );
}

function BrowserSettings() {
  const { settings, saveSettings, toast } = useApp();
  const [st, setSt] = useState<{ connected: boolean; version: string | null; pairing_code: string; extension_path: string } | null>(null);
  const load = useCallback(() => api.get<typeof st>("/api/browser/status").then(setSt).catch(() => {}), []);
  useEffect(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [load]);
  return (
    <Glass as="section" className="section">
      <h3><Chrome size={18} /> Browser</h3>
      <p>Let Ithaca work in <b>your own Chrome</b> — new tabs, your sign-ins (Google Docs, Gmail, …). Chrome shows a “started debugging this browser” banner while Ithaca acts; cancel it to take control back. Ithaca never types passwords.</p>
      <Row title="Chrome extension" hint={st?.connected ? `Connected · v${st.version ?? "?"}` : "Not connected"}>
        <span className={`badge ${st?.connected ? "ok" : "warn"}`}>{st?.connected ? "connected" : "not connected"}</span>
      </Row>
      <Row title="Use" hint="Auto uses your Chrome when the extension is connected, otherwise Ithaca's separate window.">
        <Segmented value={String(settings["browser.target"] ?? "auto")} onChange={(v) => saveSettings({ "browser.target": v })}
          options={[{ value: "auto", label: "Auto" }, { value: "chrome", label: "My Chrome" }, { value: "separate", label: "Separate window" }]} />
      </Row>
      {!st?.connected && (
        <div className="out" style={{ fontFamily: "var(--font)", fontSize: 13.5, lineHeight: 1.6 }}>
          {"1. In Chrome open chrome://extensions and turn on Developer mode (top right).\n2. Click “Load unpacked” and choose:\n     "}{st?.extension_path ?? "…/Ithaca/extension"}{"\n3. Click the Ithaca icon in the toolbar, paste the pairing code below, and press Connect."}
        </div>
      )}
      <Row title="Pairing code" hint="Paste into the extension. Re-pair to revoke the old code.">
        <span className="row">
          <code className="mono" style={{ fontSize: 12 }}>{st?.pairing_code ? `${st.pairing_code.slice(0, 6)}…` : "…"}</code>
          <button className="btn sm" onClick={() => st && navigator.clipboard.writeText(st.pairing_code).then(() => toast("Pairing code copied"))}><Copy size={14} /> Copy</button>
          <button className="btn sm ghost" onClick={async () => { setSt(await api.post("/api/browser/repair")); toast("New code issued — paste it into the extension"); }}>Re-pair</button>
        </span>
      </Row>
    </Glass>
  );
}

function VoiceSettings() {
  const [st, setSt] = useState<{ available: boolean } | null>(null);
  useEffect(() => {
    api.get<{ available: boolean }>("/api/voice/status").then(setSt).catch(() => {});
  }, []);
  return (
    <Glass as="section" className="section">
      <h3><Mic size={18} /> Voice</h3>
      <p>Talk to Ithaca. Speech recognition runs on your Mac (Whisper on Apple Silicon); no audio leaves the machine.</p>
      <Row title="Speech model" hint="model/weights/whisper — Whisper small.en">
        <span className={`badge ${st?.available ? "ok" : "warn"}`}>{st?.available ? "installed" : "not installed — ./ithaca model whisper"}</span>
      </Row>
      <Row title="In the app" hint="Use the microphone button in the chat composer; the speaker button reads replies aloud.">
        <span className="badge">ready</span>
      </Row>
      <p style={{ margin: "14px 0 6px" }}><b>“Hey Ithaca” anywhere on your Mac</b> — run the always-on listener in Terminal:</p>
      <div className="out">{`./ithaca voice

“Hey Ithaca, open Notes and write me an essay on WW2.”
“Hey Ithaca” … (chime) … “what's on my calendar today?”
“Hey Ithaca, open the app.”    “Hey Ithaca, stop.”`}</div>
      <p className="faint" style={{ fontSize: 12.5, marginTop: 10 }}>It opens the task in Ithaca and speaks the answer. macOS asks once for microphone access.</p>
    </Glass>
  );
}

function ComputerUse() {
  const { settings, saveSettings, toast } = useApp();
  const [st, setSt] = useState<{ supported: boolean; screen: boolean; input: boolean } | null>(null);
  const load = () => api.get<typeof st>("/api/computer").then(setSt).catch(() => {});
  useEffect(() => {
    load();
  }, [settings]);
  return (
    <Glass as="section" className="section">
      <h3><Monitor size={18} /> Computer use</h3>
      <p>Let Ithaca see your screen and use the mouse and keyboard to work in any app. It needs a vision model (e.g. Claude) and asks once per task before taking control. Press Stop to take back control.</p>
      <Row title="Enable computer use">
        <Switch on={Boolean(settings["computer.enabled"])} onChange={(v) => saveSettings({ "computer.enabled": v })} />
      </Row>
      <Row title="Screen Recording permission" hint="System Settings → Privacy & Security → Screen Recording">
        <span className={`badge ${st?.screen ? "ok" : "warn"}`}>{st?.screen ? "granted" : "not granted"}</span>
      </Row>
      <Row title="Accessibility permission" hint="System Settings → Privacy & Security → Accessibility (mouse & keyboard)">
        <span className={`badge ${st?.input ? "ok" : "warn"}`}>{st?.input ? "granted" : "not granted"}</span>
      </Row>
      <div className="row">
        <button className="btn" onClick={async () => { setSt(await api.post("/api/computer/request-permissions")); toast("If macOS showed prompts, allow them, then restart Ithaca."); }}>Request permissions</button>
        <button className="btn ghost" onClick={load}>Re-check</button>
      </div>
    </Glass>
  );
}

function LocalModel() {
  const [info, setInfo] = useState<{ url: string; running: boolean; models: string[]; weights: string[] } | null>(null);
  useEffect(() => {
    api.get<typeof info>("/api/local-model").then(setInfo).catch(() => {});
  }, []);
  return (
    <Glass as="section" className="section">
      <h3><Cpu size={18} /> On-device model</h3>
      <p>Ithaca's own small model, fine-tuned and 4-bit quantized with MLX to run on a 16 GB Mac.</p>
      <Row title="Server" hint={info?.url}>
        <span className={`badge ${info?.running ? "ok" : "warn"}`}>{info?.running ? `running · ${info.models.join(", ")}` : "not running"}</span>
      </Row>
      <Row title="Weights on this drive" hint="model/weights/">
        <span className="badge">{info?.weights.length ? info.weights.join(", ") : "none yet"}</span>
      </Row>
      <div className="out">{`# build it (downloads the base model, fine-tunes, quantizes)\n./ithaca model build\n\n# serve it\n./ithaca model serve`}</div>
    </Glass>
  );
}

function Appearance() {
  const { prefs, setPrefs } = useApp();
  return (
    <Glass as="section" className="section">
      <h3><Sparkles size={18} /> Appearance</h3>
      <p>Saved in this browser.</p>
      <Row title="Theme">
        <Segmented<Theme> value={prefs.theme} onChange={(v) => setPrefs({ theme: v })} options={[{ value: "auto", label: "Auto" }, { value: "dark", label: "Dark" }, { value: "light", label: "Light" }]} />
      </Row>
      <Row title="Reduce transparency" hint="Solid surfaces instead of glass."><Switch on={prefs.reduceTransparency} onChange={(v) => setPrefs({ reduceTransparency: v })} /></Row>
      <Row title="Reduce motion"><Switch on={prefs.reduceMotion} onChange={(v) => setPrefs({ reduceMotion: v })} /></Row>
    </Glass>
  );
}

function Account() {
  const { toast } = useApp();
  const [pw, setPw] = useState<{ current: string; next: string } | null>(null);
  return (
    <Glass as="section" className="section">
      <h3><KeyRound size={18} /> Account & data</h3>
      <p>Single-user, local-only. Your data never leaves this machine except for calls to the model providers you configure.</p>
      <div className="row" style={{ flexWrap: "wrap" }}>
        <button className="btn" onClick={() => setPw({ current: "", next: "" })}><KeyRound size={15} /> Change password</button>
        <a className="btn" href="/api/export"><Download size={15} /> Export everything</a>
        <span className="spacer" />
        <button className="btn danger" onClick={async () => { await api.post("/api/auth/logout"); location.reload(); }}><LogOut size={15} /> Sign out</button>
      </div>
      {pw && (
        <Dialog title="Change password" onClose={() => setPw(null)} footer={<><button className="btn ghost" onClick={() => setPw(null)}>Cancel</button><button className="btn primary" onClick={async () => {
          try { await api.post("/api/auth/password", { current: pw.current, new: pw.next }); toast("Password changed"); setPw(null); } catch (e) { toast((e as Error).message, true); }
        }}>Save</button></>}>
          <label className="field"><span>Current password</span><input className="input" type="password" value={pw.current} onChange={(e) => setPw({ ...pw, current: e.target.value })} /></label>
          <label className="field"><span>New password</span><input className="input" type="password" value={pw.next} onChange={(e) => setPw({ ...pw, next: e.target.value })} /><small>At least 8 characters.</small></label>
        </Dialog>
      )}
    </Glass>
  );
}

interface SpotifyStatus {
  configured: boolean;
  connected: boolean;
  account?: { name?: string; product?: string } | null;
  redirect_uri: string;
}

/** Apps Ithaca reaches through their official channels: Spotify's Web API, Apple Shortcuts, app links. */
function ConnectedApps() {
  const { settings, saveSettings, toast } = useApp();
  const [st, setSt] = useState<SpotifyStatus | null>(null);
  const [clientId, setClientId] = useState("");
  const load = useCallback(() => api.get<SpotifyStatus>("/api/integrations/spotify").then(setSt).catch(() => {}), []);
  useEffect(() => {
    load();
    setClientId(String(settings["spotify.client_id"] ?? ""));
  }, [load, settings]);
  useEffect(() => {
    const t = setInterval(load, 4000); // pick up the result of the sign-in tab
    return () => clearInterval(t);
  }, [load]);
  const connect = async () => {
    try {
      if (clientId.trim() !== settings["spotify.client_id"]) await saveSettings({ "spotify.client_id": clientId.trim() });
      const { url } = await api.post<{ url: string }>("/api/integrations/spotify/connect", {});
      window.open(url, "_blank", "noopener");
    } catch (e) {
      toast((e as Error).message, true);
    }
  };
  return (
    <Glass as="section" className="section">
      <h3><AppWindow size={18} /> Connected apps</h3>
      <p>Ithaca works with your apps through the ways they officially support: Spotify's own API, your Apple Shortcuts, and app links.</p>
      <Row title="Spotify" hint={st?.connected ? `Connected${st.account?.name ? ` as ${st.account.name}` : ""}${st.account?.product && st.account.product !== "premium" ? " — playback control needs Premium" : ""}` : "Search and play music on your account."}>
        {st?.connected ? (
          <button className="btn sm ghost" onClick={async () => { await api.del("/api/integrations/spotify"); load(); }}>Disconnect</button>
        ) : (
          <button className="btn sm primary" disabled={!clientId.trim()} onClick={connect}>Connect</button>
        )}
      </Row>
      {!st?.connected && (
        <div style={{ display: "grid", gap: 10, margin: "4px 0 10px" }}>
          <ol className="muted" style={{ margin: 0, paddingLeft: 18, fontSize: 13, display: "grid", gap: 4 }}>
            <li>Go to <a href="https://developer.spotify.com/dashboard" target="_blank" rel="noreferrer">developer.spotify.com/dashboard</a> and create an app (any name; tick “Web API”).</li>
            <li>Add this Redirect URI to the app: <span className="mono">{st?.redirect_uri ?? "http://127.0.0.1:7100/api/integrations/spotify/callback"}</span>{" "}
              <button className="icon-btn" style={{ width: 26, height: 26 }} aria-label="Copy" onClick={() => { navigator.clipboard.writeText(st?.redirect_uri ?? ""); toast("Copied"); }}><Copy size={13} /></button></li>
            <li>Paste the app's Client ID below and press Connect, then sign in on Spotify's page.</li>
          </ol>
          <input className="input" placeholder="Spotify Client ID" value={clientId} onChange={(e) => setClientId(e.target.value)} />
        </div>
      )}
      <Row title="Apple Shortcuts" hint="Ithaca can list and run your Shortcuts — and every app action they can use. Make them in the Shortcuts app.">
        <span className="badge">ready</span>
      </Row>
      <Row title="App links" hint="Opens apps straight at the right screen (spotify:, music:, slack:, notion:, …).">
        <span className="badge">ready</span>
      </Row>
    </Glass>
  );
}
