/**
 * Focus timer — Tomato's layout and behaviour (mode title, big clock in a
 * progress ring that turns wavy during breaks, play/reset/skip button group,
 * "up next", cycle dots, stats), rebuilt in liquid glass instead of
 * Material 3 Expressive: no tonal color fills, just glass, light and a
 * faint mode tint in the backdrop.
 */
import { Pause, Play, RotateCcw, Settings2, SkipForward } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Dialog, Switch } from "../components/Dialog";
import { Glass, Liquid } from "../components/Glass";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import type { FocusStats } from "../lib/types";

type Mode = "focus" | "short_break" | "long_break";

interface FocusConfig {
  focus: number; // minutes
  short_break: number;
  long_break: number;
  cycle: number; // focus sessions before a long break
  autoStart: boolean;
  sound: boolean;
}

interface TimerState {
  mode: Mode;
  remaining: number; // seconds left when paused / at last start
  endsAt: number | null; // epoch ms while running
  startedAt: number | null; // epoch ms of the session's first start
  done: number; // focus sessions completed in the current cycle
  label: string;
}

const DEFAULTS: FocusConfig = { focus: 25, short_break: 5, long_break: 15, cycle: 4, autoStart: false, sound: true };
const TITLES: Record<Mode, string> = { focus: "Focus", short_break: "Short break", long_break: "Long break" };
const TINTS: Record<Mode, string> = {
  focus: "rgba(255, 99, 71, 0.16)",
  short_break: "rgba(79, 209, 197, 0.14)",
  long_break: "rgba(120, 150, 255, 0.14)",
};

function load<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? { ...fallback, ...JSON.parse(raw) } : fallback;
  } catch {
    return fallback;
  }
}
function save(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* ignore */
  }
}

function fmt(sec: number) {
  const s = Math.max(0, Math.ceil(sec));
  const m = Math.floor(s / 60);
  return `${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function chime() {
  try {
    const ctx = new AudioContext();
    const now = ctx.currentTime;
    [660, 880, 1320].forEach((f, i) => {
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.type = "sine";
      o.frequency.value = f;
      g.gain.setValueAtTime(0, now + i * 0.18);
      g.gain.linearRampToValueAtTime(0.18, now + i * 0.18 + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, now + i * 0.18 + 0.9);
      o.connect(g).connect(ctx.destination);
      o.start(now + i * 0.18);
      o.stop(now + i * 0.18 + 1);
    });
    setTimeout(() => ctx.close(), 2000);
  } catch {
    /* audio unavailable */
  }
}

function useFocusTimer(cfg: FocusConfig, onComplete: (s: TimerState, actual: number) => void) {
  const duration = useCallback((m: Mode) => cfg[m] * 60, [cfg]);
  const [state, setState] = useState<TimerState>(() =>
    load<TimerState>("ithaca.focus.state", { mode: "focus", remaining: cfg.focus * 60, endsAt: null, startedAt: null, done: 0, label: "" }),
  );
  const [now, setNow] = useState(Date.now());
  const completing = useRef(false);

  useEffect(() => save("ithaca.focus.state", state), [state]);

  useEffect(() => {
    if (!state.endsAt) return;
    const id = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(id);
  }, [state.endsAt]);

  const left = Math.min(duration(state.mode), state.endsAt ? (state.endsAt - now) / 1000 : state.remaining);

  const nextMode = (s: TimerState): { mode: Mode; done: number } => {
    if (s.mode !== "focus") return { mode: "focus", done: s.mode === "long_break" ? 0 : s.done };
    const done = s.done + 1;
    return { mode: done >= cfg.cycle ? "long_break" : "short_break", done };
  };

  const stateRef = useRef(state);
  stateRef.current = state;

  const advance = useCallback(
    (completed: boolean) => {
      const s = stateRef.current;
      const planned = duration(s.mode);
      const elapsed = s.endsAt ? planned - (s.endsAt - Date.now()) / 1000 : planned - s.remaining;
      // Side effects (logging, chime) happen once, outside the state updater.
      if (s.startedAt && elapsed > 5) onComplete(s, completed ? planned : Math.round(elapsed));
      const nx = nextMode(s);
      const auto = completed && cfg.autoStart;
      const rem = duration(nx.mode);
      setState({
        ...s,
        mode: nx.mode,
        done: s.mode === "focus" && !completed ? s.done : nx.done,
        remaining: rem,
        endsAt: auto ? Date.now() + rem * 1000 : null,
        startedAt: auto ? Date.now() : null,
      });
    },
    // nextMode only reads cfg.cycle
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [cfg, duration, onComplete],
  );

  useEffect(() => {
    if (state.endsAt && left <= 0 && !completing.current) {
      completing.current = true;
      advance(true);
      setTimeout(() => (completing.current = false), 500);
    }
  }, [left, state.endsAt, advance]);

  return {
    state,
    left,
    total: duration(state.mode),
    nextMode: nextMode(state).mode,
    running: Boolean(state.endsAt),
    toggle: () => {
      setNow(Date.now());
      setState((s) =>
        s.endsAt
          ? { ...s, remaining: Math.max(0, (s.endsAt - Date.now()) / 1000), endsAt: null }
          : { ...s, endsAt: Date.now() + s.remaining * 1000, startedAt: s.startedAt ?? Date.now() },
      );
    },
    reset: () => setState((s) => ({ ...s, remaining: duration(s.mode), endsAt: null, startedAt: null })),
    skip: () => advance(false),
    setLabel: (label: string) => setState((s) => ({ ...s, label })),
    setMode: (mode: Mode) => setState((s) => ({ ...s, mode, remaining: duration(mode), endsAt: null, startedAt: null })),
  };
}

/** Ring: a clean arc while focusing, a wavy arc during breaks (Tomato's CircularWavyProgressIndicator). */
function Ring({ progress, wavy }: { progress: number; wavy: boolean }) {
  const [phase, setPhase] = useState(0);
  useEffect(() => {
    if (!wavy) return;
    let raf = 0;
    const tick = (t: number) => {
      setPhase(t / 900);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [wavy]);

  const size = 200;
  const c = size / 2;
  const r = 88;
  const p = Math.min(1, Math.max(0, progress));

  const path = useMemo(() => {
    if (!wavy) return null;
    const steps = 360;
    const end = Math.max(1, Math.round(steps * p));
    let d = "";
    for (let i = 0; i <= end; i++) {
      const a = (i / steps) * Math.PI * 2 - Math.PI / 2;
      const rr = r + Math.sin(a * 12 + phase) * 2.6;
      const x = c + rr * Math.cos(a);
      const y = c + rr * Math.sin(a);
      d += `${i ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`;
    }
    return d;
  }, [wavy, p, phase, c]);

  const circ = 2 * Math.PI * r;
  return (
    <svg viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
      <defs>
        <linearGradient id="ringGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="currentColor" stopOpacity="1" />
          <stop offset="1" stopColor="currentColor" stopOpacity="0.55" />
        </linearGradient>
        <filter id="ringGlow" x="-20%" y="-20%" width="140%" height="140%">
          <feGaussianBlur stdDeviation="2.2" result="b" />
          <feMerge>
            <feMergeNode in="b" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>
      {/* dotted track, after the orbs */}
      <circle cx={c} cy={c} r={r} fill="none" stroke="currentColor" strokeOpacity="0.28" strokeWidth="3" strokeLinecap="round" strokeDasharray="0.01 9" />
      {wavy ? (
        <path d={path ?? ""} fill="none" stroke="url(#ringGrad)" strokeWidth="4" strokeLinecap="round" />
      ) : (
        <circle
          cx={c}
          cy={c}
          r={r}
          fill="none"
          stroke="url(#ringGrad)"
          strokeWidth="4"
          strokeLinecap="round"
          strokeDasharray={`${circ * p} ${circ}`}
          transform={`rotate(-90 ${c} ${c})`}
         
          style={{ transition: "stroke-dasharray .3s linear" }}
        />
      )}
    </svg>
  );
}

export function FocusView() {
  const { setModeTint, toast } = useApp();
  const [cfg, setCfg] = useState<FocusConfig>(() => load("ithaca.focus.config", DEFAULTS));
  const [stats, setStats] = useState<FocusStats | null>(null);
  const [editing, setEditing] = useState(false);

  const refreshStats = useCallback(() => {
    api.get<FocusStats>("/api/focus/stats?days=7").then(setStats).catch(() => {});
  }, []);
  useEffect(refreshStats, [refreshStats]);

  const onComplete = useCallback(
    (s: TimerState, actual: number) => {
      const planned = cfg[s.mode] * 60;
      const completed = actual >= planned;
      if (completed && cfg.sound) chime();
      if (completed && "Notification" in window && Notification.permission === "granted") {
        new Notification(s.mode === "focus" ? "Focus session complete" : "Break's over", { body: s.mode === "focus" ? "Time for a break." : "Back to it." });
      }
      api
        .post("/api/focus/sessions", {
          mode: s.mode,
          label: s.label,
          planned_s: planned,
          actual_s: Math.min(actual, planned),
          completed,
          started_at: (s.startedAt ?? Date.now()) / 1000,
          ended_at: Date.now() / 1000,
        })
        .then(refreshStats)
        .catch((e) => toast((e as Error).message, true));
    },
    [cfg, refreshStats, toast],
  );

  const t = useFocusTimer(cfg, onComplete);
  const mode = t.state.mode;

  useEffect(() => {
    setModeTint(t.running ? TINTS[mode] : TINTS[mode].replace(/0\.1\d\)/, "0.07)"));
    document.title = t.running ? `${fmt(t.left)} · ${TITLES[mode]}` : "Ithaca";
  }, [mode, t.running, t.left, setModeTint]);
  useEffect(() => () => {
    setModeTint("transparent");
    document.title = "Ithaca";
  }, [setModeTint]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).tagName === "INPUT" || (e.target as HTMLElement).tagName === "TEXTAREA") return;
      if (e.code === "Space") {
        e.preventDefault();
        t.toggle();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [t]);

  const progress = 1 - t.left / t.total;
  const ringColor = mode === "focus" ? "var(--text)" : "var(--text-2)";

  const week = useMemo(() => {
    const out: { label: string; minutes: number; today: boolean }[] = [];
    const byDay = new Map(stats?.days.map((d) => [d.day, d.seconds]) ?? []);
    for (let i = 6; i >= 0; i--) {
      const d = new Date(Date.now() - i * 86400000);
      const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      out.push({ label: d.toLocaleDateString(undefined, { weekday: "narrow" }), minutes: Math.round((byDay.get(key) ?? 0) / 60), today: i === 0 });
    }
    return out;
  }, [stats]);
  const maxMin = Math.max(30, ...week.map((w) => w.minutes));

  return (
    <div className="focus">
      <div className="focus-grid">
        <Glass className="focus-stage">
          <div className="row" style={{ width: "100%" }}>
            <span className="spacer" />
            <button className="icon-btn" onClick={() => setEditing(true)} aria-label="Timer settings">
              <Settings2 size={18} />
            </button>
          </div>
          <h2 className={`mode-title ${mode === "focus" ? "focus" : ""}`} style={{ marginTop: -36 }}>
            {TITLES[mode]}
          </h2>
          <input className="focus-label" placeholder="What are you working on?" value={t.state.label} onChange={(e) => t.setLabel(e.target.value)} maxLength={60} />

          <div className="ring-wrap" style={{ color: ringColor }}>
            <div className="ring-face glass thin" />
            <Ring progress={progress} wavy={mode !== "focus"} />
            <div style={{ display: "grid", justifyItems: "center" }}>
              <div className="clock" aria-live="off">{fmt(t.left)}</div>
              <div className="clock-sub">
                Up next · <b>{TITLES[t.nextMode]}</b>
              </div>
            </div>
          </div>

          <div className="cycle-dots" aria-label={`${t.state.done} of ${cfg.cycle} focus sessions done`}>
            {Array.from({ length: cfg.cycle }, (_, i) => <i key={i} className={i < t.state.done ? "on" : ""} />)}
          </div>

          <div className="controls">
            <button className="round-btn glass thin" onClick={t.reset} aria-label="Reset">
              <RotateCcw size={20} />
            </button>
            <Liquid width={132} height={72} radius={36} onClick={t.toggle} title={t.running ? "Pause (space)" : "Start (space)"}>
              <span className="play-inner">
                {t.running ? <Pause size={22} fill="currentColor" /> : <Play size={22} fill="currentColor" />}
                {t.running ? "Pause" : "Start"}
              </span>
            </Liquid>
            <button className="round-btn glass thin" onClick={t.skip} aria-label="Skip">
              <SkipForward size={20} />
            </button>
          </div>

          <div className="segmented glass thin">
            {(["focus", "short_break", "long_break"] as Mode[]).map((m) => (
              <button key={m} className={m === mode ? "on" : ""} onClick={() => t.setMode(m)}>
                {TITLES[m]}
              </button>
            ))}
          </div>
        </Glass>

        <div style={{ display: "grid", gap: 16 }}>
          <Glass className="card">
            <div className="faint" style={{ fontSize: 12.5, fontWeight: 650, textTransform: "uppercase", letterSpacing: ".06em" }}>Today</div>
            <div className="row" style={{ alignItems: "baseline", marginTop: 6 }}>
              <span className="stat-big">{Math.round((stats?.today_seconds ?? 0) / 60)}</span>
              <span className="muted">min focused</span>
            </div>
            <div className="row" style={{ marginTop: 8, gap: 16 }}>
              <span className="muted">{stats?.today_sessions ?? 0} sessions</span>
              <span className="muted">🔥 {stats?.streak ?? 0}-day streak</span>
            </div>
          </Glass>
          <Glass className="card">
            <div className="row">
              <b>This week</b>
              <span className="spacer" />
              <span className="faint">{Math.round((stats?.total_seconds ?? 0) / 60)} min</span>
            </div>
            <div className="bars">
              {week.map((w, i) => (
                <div key={i} className={`bar${w.today ? " today" : ""}`} title={`${w.minutes} min`}>
                  <i style={{ height: `${(w.minutes / maxMin) * 100}%` }} />
                  <span>{w.label}</span>
                </div>
              ))}
            </div>
          </Glass>
          {Boolean(stats?.labels.length) && (
            <Glass className="card">
              <b>Top focus</b>
              <div style={{ display: "grid", gap: 8, marginTop: 10 }}>
                {stats!.labels.map((l) => (
                  <div key={l.label} className="row">
                    <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{l.label}</span>
                    <span className="faint">{Math.round(l.seconds / 60)} min</span>
                  </div>
                ))}
              </div>
            </Glass>
          )}
          <p className="faint" style={{ fontSize: 12.5, margin: "0 6px" }}>
            Space starts and pauses. Ask Ithaca “how did my focus go this week?” for a summary.
          </p>
        </div>
      </div>

      {editing && (
        <FocusSettings
          cfg={cfg}
          onClose={() => setEditing(false)}
          onSave={(c) => {
            setCfg(c);
            save("ithaca.focus.config", c);
            if (!t.running) t.setMode(mode);
            if (c.sound && "Notification" in window && Notification.permission === "default") Notification.requestPermission();
            setEditing(false);
          }}
        />
      )}
    </div>
  );
}

function FocusSettings({ cfg, onSave, onClose }: { cfg: FocusConfig; onSave: (c: FocusConfig) => void; onClose: () => void }) {
  const [c, setC] = useState(cfg);
  const num = (k: "focus" | "short_break" | "long_break" | "cycle", label: string, max: number) => (
    <label className="field">
      <span>{label}</span>
      <input className="input" type="number" min={1} max={max} value={c[k]} onChange={(e) => setC({ ...c, [k]: Math.max(1, Math.min(max, Number(e.target.value) || 1)) })} />
    </label>
  );
  return (
    <Dialog title="Timer" onClose={onClose} footer={<><button className="btn ghost" onClick={onClose}>Cancel</button><button className="btn primary" onClick={() => onSave(c)}>Save</button></>}>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        {num("focus", "Focus (min)", 180)}
        {num("short_break", "Short break (min)", 60)}
        {num("long_break", "Long break (min)", 120)}
        {num("cycle", "Sessions before long break", 12)}
      </div>
      <div className="set-row">
        <div className="lbl"><b>Auto-start next timer</b></div>
        <Switch on={c.autoStart} onChange={(v) => setC({ ...c, autoStart: v })} />
      </div>
      <div className="set-row">
        <div className="lbl"><b>Chime and notification</b></div>
        <Switch on={c.sound} onChange={(v) => setC({ ...c, sound: v })} />
      </div>
    </Dialog>
  );
}
