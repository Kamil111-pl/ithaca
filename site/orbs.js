/* <ithaca-orb state="searching" size="64" px="320"> — the app's thinking orbs
   (github.com/Jakubantalik/thinking-orbs, MIT) drawn with the library's own
   engine, no React. `size` picks the tuned preset (20 / 32 / 64); `px` is the
   displayed size, so a 64-preset orb can be drawn large for a hero. */

import { resolvePreset, MODE_FRAMES, paintFrame } from "./vendor/thinking-orbs/engine.es.js";

const reduced = matchMedia("(prefers-reduced-motion: reduce)");
const systemDark = matchMedia("(prefers-color-scheme: dark)");

function isDark() {
  const t = document.documentElement.dataset.theme;
  if (t === "dark") return true;
  if (t === "light") return false;
  return systemDark.matches;
}

const live = new Set();
let raf = 0;
function tick() {
  const now = performance.now() / 1000;
  for (const orb of live) orb.draw(now);
  raf = live.size ? requestAnimationFrame(tick) : 0;
}
function wake() {
  if (!raf && live.size && !reduced.matches) raf = requestAnimationFrame(tick);
}

class IthacaOrb extends HTMLElement {
  static observedAttributes = ["state", "size", "px", "speed"];

  connectedCallback() {
    if (!this.canvas) {
      this.canvas = document.createElement("canvas");
      this.canvas.setAttribute("aria-hidden", "true");
      this.appendChild(this.canvas);
    }
    this.setup();
    this.io = new IntersectionObserver(([e]) => {
      if (e.isIntersecting) { live.add(this); wake(); }
      else live.delete(this);
    }, { rootMargin: "80px" });
    this.io.observe(this);
  }

  disconnectedCallback() {
    this.io?.disconnect();
    live.delete(this);
  }

  attributeChangedCallback() {
    if (this.canvas) this.setup();
  }

  setup() {
    const preset = Number(this.getAttribute("size") || 64);
    const px = Number(this.getAttribute("px") || preset);
    const dpr = Math.min(2, devicePixelRatio || 1);
    const { mode, speed, opts } = resolvePreset(this.getAttribute("state") || "working", preset);
    this.px = px;
    this.dpr = dpr;
    this.fn = MODE_FRAMES[mode];
    this.opts = opts;
    this.speed = speed * Number(this.getAttribute("speed") || 1);
    this.canvas.width = Math.round(px * dpr);
    this.canvas.height = Math.round(px * dpr);
    this.canvas.style.width = this.canvas.style.height = px + "px";
    this.ctx = this.canvas.getContext("2d");
    this.draw(reduced.matches ? 0.6 / this.speed : performance.now() / 1000);
  }

  draw(now) {
    const { ctx, px, dpr } = this;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, px, px);
    paintFrame(ctx, this.fn(px, now * this.speed, this.opts), isDark());
  }
}

customElements.define("ithaca-orb", IthacaOrb);

// Repaint still orbs when the theme changes.
const repaint = () => document.querySelectorAll("ithaca-orb").forEach((o) => o.canvas && o.setup());
systemDark.addEventListener("change", repaint);
new MutationObserver(repaint).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
