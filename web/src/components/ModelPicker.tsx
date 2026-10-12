import { Check, ChevronDown, Cpu, Sparkles, Wrench } from "lucide-react";
import { useApp } from "../lib/app";
import type { ModelOption } from "../lib/types";
import { useMenu } from "./Menu";

export function modelLabel(model: string | null | undefined): string {
  if (!model) return "Choose a model";
  const short = model.split("/").pop() ?? model;
  return short.replace(/^claude-/, "Claude ").replace(/-(\d)-(\d)$/, " $1.$2");
}

export function ModelPicker({ providerId, model, onChange }: { providerId: string | null; model: string | null; onChange: (m: ModelOption) => void }) {
  const { models, setView } = useApp();
  const menu = useMenu();
  const grouped = new Map<string, ModelOption[]>();
  for (const m of models) grouped.set(m.provider, [...(grouped.get(m.provider) ?? []), m]);

  return (
    <>
      <button
        className="chip"
        onClick={(e) => {
          const items = [] as Parameters<typeof menu.open>[1];
          for (const [provider, list] of grouped) {
            items.push({ heading: provider });
            for (const m of list.slice(0, 30)) {
              const on = m.provider_id === providerId && m.model === model;
              items.push({
                label: modelLabel(m.model),
                icon: on ? <Check size={16} /> : m.kind === "anthropic" ? <Sparkles size={16} /> : <Cpu size={16} />,
                sub: m.tools === false ? "chat only" : m.tools ? "tools" : undefined,
                active: on,
                onSelect: () => onChange(m),
              });
            }
          }
          if (items.length) items.push({ separator: true });
          items.push({ label: "Manage models…", icon: <Wrench size={16} />, onSelect: () => setView("settings") });
          menu.open(e.currentTarget, items);
        }}
      >
        {modelLabel(model)}
        <ChevronDown size={14} />
      </button>
      {menu.node}
    </>
  );
}
