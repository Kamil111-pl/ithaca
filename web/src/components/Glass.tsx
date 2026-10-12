import type { CSSProperties, HTMLAttributes, ReactNode } from "react";

type GlassProps = HTMLAttributes<HTMLDivElement> & { thin?: boolean; as?: "div" | "section" | "aside" | "form" };

/** A surface: translucent fill outlined by a 1px inset ring. */
export function Glass({ thin, className = "", as = "div", children, ...rest }: GlassProps) {
  const Tag = as as "div";
  return (
    <Tag className={`glass${thin ? " thin" : ""} ${className}`} {...rest}>
      {children}
    </Tag>
  );
}

interface LiquidProps {
  width: number;
  height: number;
  radius?: number;
  children: ReactNode;
  onClick?: () => void;
  className?: string;
  style?: CSSProperties;
  title?: string;
}

/**
 * Floating pill in the thinking-orbs language: translucent fill, a 1px inset
 * ring and a faint inner glow (formerly refractive liquid glass).
 */
export function Liquid({ width, height, radius = 999, children, onClick, className = "", style, title }: LiquidProps) {
  return (
    <div className={`lg-anchor ${className}`} style={{ width, height, ...style }} title={title}>
      <div
        className="lg-pill"
        role={onClick ? "button" : undefined}
        tabIndex={onClick ? 0 : undefined}
        onClick={onClick}
        onKeyDown={onClick ? (e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), onClick()) : undefined}
        style={{ width, height, borderRadius: radius }}
      >
        {children}
      </div>
    </div>
  );
}
