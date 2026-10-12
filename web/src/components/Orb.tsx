/* Thinking orbs: one animated orb per stage of an agent run, so the user can
   tell at a glance whether Ithaca is reasoning, looking things up, acting,
   waiting on them, or writing the answer. Drawn by thinking-orbs
   (github.com/Jakubantalik/thinking-orbs), which follows <html data-theme>. */

import { ThinkingOrb, type OrbState } from "thinking-orbs";
import { useApp } from "../lib/app";

export type Stage = "connect" | "think" | "search" | "research" | "act" | "create" | "approve" | "write" | "recover" | "listen";

export const STAGE_LABEL: Record<Stage, string> = {
  connect: "Getting started",
  think: "Thinking",
  search: "Looking things up",
  research: "Researching",
  act: "Taking action",
  create: "Writing it down",
  approve: "Waiting for your approval",
  write: "Writing",
  recover: "Reconnecting",
  listen: "Transcribing",
};

const STAGE_ORB: Record<Stage, { state: OrbState; speed?: number }> = {
  connect: { state: "breathing" }, // a ring slowly morphing
  think: { state: "solving" }, // bands scramble, then click back solved
  search: { state: "searching" }, // a scan meridian sweeps a dotted globe
  research: { state: "weaving" }, // strands from many sources plait together
  act: { state: "working" }, // particles on tilted orbits
  create: { state: "shaping" }, // an outline takes shape
  approve: { state: "breathing", speed: 0.45 }, // holding, slower than connect
  write: { state: "composing" }, // an undulating sash, like text flowing
  recover: { state: "connecting" }, // a constellation re-wiring itself
  listen: { state: "listening" }, // a waveform rolls through the rings
};

const TOOL_STAGE: Record<string, Stage> = {
  deep_research: "research",
  web_search: "search",
  web_fetch: "search",
  memory_search: "search",
  read_document: "search",
  tool_search: "search",
  browser_read: "search",
  browser_search: "search",
  browser_tabs: "search",
  browser_screenshot: "search",
  computer_screenshot: "search",
  create_document: "create",
  edit_document: "create",
  memory_save: "create",
  notes_create: "create",
  reminders_add: "create",
};

export function toolStage(name: string): Stage {
  return TOOL_STAGE[name] ?? "act";
}

/** `size` is the displayed size in px; the library's 20px preset is used up to 32px, its 64px preset above. */
export function Orb({ stage, size = 20 }: { stage: Stage; size?: number }) {
  const { prefs } = useApp();
  const { state, speed } = STAGE_ORB[stage];
  return (
    <ThinkingOrb
      className="think-orb"
      state={state}
      size={size > 32 ? 64 : 20}
      speed={speed}
      paused={prefs.reduceMotion}
      aria-hidden
      style={{ width: size, height: size, flex: "none" }}
    />
  );
}
