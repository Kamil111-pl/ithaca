import { Brain, FileText, Focus, Menu as MenuIcon, MessageCircle, Settings, Telescope } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { Glass } from "./components/Glass";
import { Orb } from "./components/Orb";
import { api } from "./lib/api";
import { AppProvider, useApp } from "./lib/app";
import type { View } from "./lib/types";
import { AuthView } from "./views/AuthView";
import { ChatView, ConversationList } from "./views/ChatView";
import { DocumentList, DocumentsView } from "./views/DocumentsView";
import { FocusView } from "./views/FocusView";
import { MemoryView } from "./views/MemoryView";
import { ResearchView } from "./views/ResearchView";
import { SettingsView } from "./views/SettingsView";

const TABS: { id: View; label: string; icon: ReactNode }[] = [
  { id: "chat", label: "Chat", icon: <MessageCircle size={15} /> },
  { id: "documents", label: "Docs", icon: <FileText size={15} /> },
  { id: "research", label: "Research", icon: <Telescope size={15} /> },
  { id: "focus", label: "Focus", icon: <Focus size={15} /> },
  { id: "memory", label: "Memory", icon: <Brain size={15} /> },
  { id: "settings", label: "Settings", icon: <Settings size={15} /> },
];

export default function App() {
  const [auth, setAuth] = useState<{ configured: boolean; signed_in: boolean } | null>(null);
  const check = () => api.get<{ configured: boolean; signed_in: boolean }>("/api/auth/status").then(setAuth).catch(() => setAuth({ configured: true, signed_in: false }));
  useEffect(() => {
    check();
    const onUnauthorized = () => setAuth((a) => (a ? { ...a, signed_in: false } : a));
    window.addEventListener("ithaca:unauthorized", onUnauthorized);
    return () => window.removeEventListener("ithaca:unauthorized", onUnauthorized);
  }, []);

  return (
    <>
      <Backdrop />
      {auth === null ? null : auth.signed_in ? (
        <AppProvider>
          <Shell />
        </AppProvider>
      ) : (
        <AuthView configured={auth.configured} onDone={check} />
      )}
    </>
  );
}

function Backdrop() {
  return (
    <div className="backdrop" aria-hidden="true" />
  );
}

function Shell() {
  const { view, setView, sidebarOpen, setSidebarOpen, openConversation, toasts } = useApp();
  const hasSidebar = view === "chat" || view === "documents";

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      if (e.key.toLowerCase() === "k") {
        e.preventDefault();
        openConversation(null);
      }
      const n = Number(e.key);
      if (n >= 1 && n <= TABS.length) {
        e.preventDefault();
        setView(TABS[n - 1].id);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openConversation, setView]);

  return (
    <div className={`app${hasSidebar ? "" : " no-side"}`}>
      {hasSidebar && (
        <Glass as="aside" className={`sidebar${sidebarOpen ? " open" : ""}`}>
          <div className="brand">
            <div className="brand-mark">
              <Orb stage="connect" size={24} />
            </div>
            <span className="brand-name">Ithaca</span>
          </div>
          {view === "chat" ? <ConversationList /> : <DocumentList />}
        </Glass>
      )}
      <main className="main">
        <div className="tabbar-wrap">
          {hasSidebar && (
            <button className="icon-btn mobile-only" style={{ position: "absolute", left: 10, top: 10 }} onClick={() => setSidebarOpen(!sidebarOpen)} aria-label="Toggle sidebar">
              <MenuIcon size={20} />
            </button>
          )}
          <TabBar />
        </div>
        {view === "chat" && <ChatView />}
        {view === "documents" && <DocumentsView />}
        {view === "research" && <ResearchView />}
        {view === "focus" && <FocusView />}
        {view === "memory" && <MemoryView />}
        {view === "settings" && <SettingsView />}
      </main>
      {sidebarOpen && <div className="scrim mobile-only" style={{ zIndex: 70, display: "block" }} onClick={() => setSidebarOpen(false)} />}
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast${t.error ? " error" : ""}`}>{t.text}</div>
        ))}
      </div>
    </div>
  );
}

/** Floating tab group, as in the thinking-orbs demo. */
function TabBar() {
  const { view, setView } = useApp();
  return (
    <div className="tabbar" role="tablist">
      {TABS.map((t, i) => (
        <button
          key={t.id}
          role="tab"
          aria-selected={view === t.id}
          className={`tab${view === t.id ? " on" : ""}`}
          onClick={() => setView(t.id)}
          title={`${t.label} (⌘${i + 1})`}
        >
          {t.icon}
          <span className="lbl">{t.label}</span>
        </button>
      ))}
    </div>
  );
}
