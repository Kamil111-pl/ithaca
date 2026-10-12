import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "./api";
import type { ModelOption, Settings, View } from "./types";

export type Theme = "auto" | "dark" | "light";

interface Prefs {
  theme: Theme;
  reduceTransparency: boolean;
  reduceMotion: boolean;
}

interface Toast {
  id: number;
  text: string;
  error?: boolean;
}

interface AppState {
  view: View;
  setView: (v: View) => void;
  conversationId: string | null;
  openConversation: (id: string | null) => void;
  documentId: string | null;
  openDocument: (id: string | null) => void;
  models: ModelOption[];
  refreshModels: () => Promise<void>;
  settings: Settings;
  saveSettings: (patch: Settings) => Promise<void>;
  prefs: Prefs;
  setPrefs: (p: Partial<Prefs>) => void;
  toast: (text: string, error?: boolean) => void;
  toasts: Toast[];
  sidebarOpen: boolean;
  setSidebarOpen: (v: boolean) => void;
  conversationsVersion: number;
  bumpConversations: () => void;
  modeTint: string;
  setModeTint: (c: string) => void;
}

const Ctx = createContext<AppState | null>(null);

function readPrefs(): Prefs {
  try {
    const raw = localStorage.getItem("ithaca.prefs");
    if (raw) return { theme: "auto", reduceTransparency: false, reduceMotion: false, ...JSON.parse(raw) };
  } catch {
    /* storage unavailable */
  }
  return { theme: "auto", reduceTransparency: false, reduceMotion: false };
}

function readView(): View {
  const hash = window.location.hash.replace("#", "").split("/")[0];
  const views: View[] = ["chat", "documents", "research", "focus", "memory", "settings"];
  return (views.includes(hash as View) ? hash : "chat") as View;
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [view, setViewState] = useState<View>(readView);
  const [conversationId, setConversationId] = useState<string | null>(() => {
    const parts = window.location.hash.replace("#", "").split("/");
    return parts[0] === "chat" && parts[1] ? parts[1] : null;
  });
  const [documentId, setDocumentId] = useState<string | null>(() => {
    const parts = window.location.hash.replace("#", "").split("/");
    return parts[0] === "documents" && parts[1] ? parts[1] : null;
  });
  const [models, setModels] = useState<ModelOption[]>([]);
  const [settings, setSettings] = useState<Settings>({});
  const [prefs, setPrefsState] = useState<Prefs>(readPrefs);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [conversationsVersion, setConversationsVersion] = useState(0);
  const [modeTint, setModeTint] = useState("transparent");

  useEffect(() => {
    const id = view === "chat" ? conversationId : view === "documents" ? documentId : null;
    const hash = `#${view}${id ? `/${id}` : ""}`;
    if (window.location.hash !== hash) history.pushState(null, "", hash);
  }, [view, conversationId, documentId]);

  // Back/forward and typed URLs.
  useEffect(() => {
    const onHash = () => {
      const [v, id] = window.location.hash.replace("#", "").split("/");
      const next = readView();
      setViewState(next);
      if (v === "chat") setConversationId(id || null);
      if (v === "documents") setDocumentId(id || null);
    };
    window.addEventListener("hashchange", onHash);
    window.addEventListener("popstate", onHash);
    return () => {
      window.removeEventListener("hashchange", onHash);
      window.removeEventListener("popstate", onHash);
    };
  }, []);

  useEffect(() => {
    const root = document.documentElement;
    const apply = () => {
      const dark = prefs.theme === "dark" || (prefs.theme === "auto" && window.matchMedia("(prefers-color-scheme: dark)").matches);
      root.dataset.theme = dark ? "dark" : "light";
      document.querySelector('meta[name="theme-color"]')?.setAttribute("content", dark ? "#070707" : "#ffffff");
    };
    apply();
    root.dataset.transparency = prefs.reduceTransparency ? "reduced" : "";
    root.dataset.motion = prefs.reduceMotion ? "reduced" : "";
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, [prefs]);

  useEffect(() => {
    document.documentElement.style.setProperty("--mode-tint", modeTint);
  }, [modeTint]);

  const setPrefs = useCallback((p: Partial<Prefs>) => {
    setPrefsState((prev) => {
      const next = { ...prev, ...p };
      try {
        localStorage.setItem("ithaca.prefs", JSON.stringify(next));
      } catch {
        /* ignore */
      }
      return next;
    });
  }, []);

  const toast = useCallback((text: string, error = false) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, text, error }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), error ? 6000 : 3200);
  }, []);

  const refreshModels = useCallback(async () => {
    try {
      setModels(await api.get<ModelOption[]>("/api/models"));
    } catch {
      /* surfaced elsewhere */
    }
  }, []);

  useEffect(() => {
    refreshModels();
    api.get<Settings>("/api/settings").then(setSettings).catch(() => {});
  }, [refreshModels]);

  const saveSettings = useCallback(
    async (patch: Settings) => {
      try {
        setSettings(await api.put<Settings>("/api/settings", patch));
      } catch (e) {
        toast((e as Error).message, true);
      }
    },
    [toast],
  );

  const setView = useCallback((v: View) => {
    setViewState(v);
    setSidebarOpen(false);
  }, []);
  const openConversation = useCallback((id: string | null) => {
    setConversationId(id);
    setViewState("chat");
    setSidebarOpen(false);
  }, []);
  const openDocument = useCallback((id: string | null) => {
    setDocumentId(id);
    setViewState("documents");
    setSidebarOpen(false);
  }, []);
  const bumpConversations = useCallback(() => setConversationsVersion((v) => v + 1), []);

  const value = useMemo<AppState>(
    () => ({
      view,
      setView,
      conversationId,
      openConversation,
      documentId,
      openDocument,
      models,
      refreshModels,
      settings,
      saveSettings,
      prefs,
      setPrefs,
      toast,
      toasts,
      sidebarOpen,
      setSidebarOpen,
      conversationsVersion,
      bumpConversations,
      modeTint,
      setModeTint,
    }),
    [view, setView, conversationId, openConversation, documentId, openDocument, models, refreshModels, settings, saveSettings, prefs, setPrefs, toast, toasts, sidebarOpen, conversationsVersion, bumpConversations, modeTint],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useApp(): AppState {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useApp outside AppProvider");
  return ctx;
}

export function relTime(ts: number): string {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 7) return `${Math.floor(s / 86400)}d ago`;
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
