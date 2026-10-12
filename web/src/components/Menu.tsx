import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

export interface MenuEntry {
  label?: string;
  icon?: ReactNode;
  sub?: string;
  danger?: boolean;
  onSelect?: () => void;
  separator?: boolean;
  heading?: string;
  active?: boolean;
}

/** Liquid glass context menu (the Figma "Like / Reply / Share" panel). */
export function useMenu() {
  const [state, setState] = useState<{ x: number; y: number; items: MenuEntry[] } | null>(null);
  const open = useCallback((anchor: HTMLElement | { x: number; y: number }, items: MenuEntry[]) => {
    if (anchor instanceof HTMLElement) {
      const r = anchor.getBoundingClientRect();
      setState({ x: r.left, y: r.bottom + 6, items });
    } else {
      setState({ ...anchor, items });
    }
  }, []);
  const close = useCallback(() => setState(null), []);
  const node = state ? <MenuLayer {...state} onClose={close} /> : null;
  return { open, close, node };
}

function MenuLayer({ x, y, items, onClose }: { x: number; y: number; items: MenuEntry[]; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ x, y });

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const nx = Math.max(8, Math.min(x, window.innerWidth - r.width - 8));
    const ny = y + r.height > window.innerHeight - 8 ? Math.max(8, y - r.height - 44) : y;
    setPos({ x: nx, y: ny });
    el.querySelector<HTMLButtonElement>(".menu-item")?.focus();
  }, [x, y]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const btns = Array.from(ref.current?.querySelectorAll<HTMLButtonElement>(".menu-item") ?? []);
        const i = btns.indexOf(document.activeElement as HTMLButtonElement);
        const next = e.key === "ArrowDown" ? (i + 1) % btns.length : (i - 1 + btns.length) % btns.length;
        btns[next]?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return createPortal(
    <div className="menu-layer" onMouseDown={onClose} onContextMenu={(e) => { e.preventDefault(); onClose(); }}>
      <div ref={ref} className="glass menu" style={{ left: pos.x, top: pos.y }} onMouseDown={(e) => e.stopPropagation()} role="menu">
        {items.map((it, i) =>
          it.separator ? (
            <div key={i} className="menu-sep" />
          ) : it.heading ? (
            <div key={i} className="menu-head">{it.heading}</div>
          ) : (
            <button
              key={i}
              role="menuitem"
              className={`menu-item${it.danger ? " danger" : ""}`}
              style={it.active ? { fontWeight: 650 } : undefined}
              onClick={() => {
                onClose();
                it.onSelect?.();
              }}
            >
              {it.icon}
              <span>{it.label}</span>
              {it.sub && <span className="sub">{it.sub}</span>}
            </button>
          ),
        )}
      </div>
    </div>,
    document.body,
  );
}
