// Ithaca Browser Bridge — connects this Chrome to the Ithaca app running on
// this Mac and carries out browser actions the assistant requests.
//
// Safety:
// - Only talks to Ithaca on 127.0.0.1, authenticated with a pairing token.
// - Never acts on Ithaca's own pages, chrome:// pages, or other extensions.
// - Refuses to type into password fields.
// - Clicks/typing go through the Chrome debugger, which shows Chrome's
//   "started debugging this browser" banner; cancelling it stops control.

const DEFAULT_URL = "ws://127.0.0.1:7100/api/browser-ext/ws";
let ws = null;
let status = "disconnected";
let agentTabId = null;
const attached = new Set();

async function config() {
  const { token, url } = await chrome.storage.local.get(["token", "url"]);
  return { token, url: url || DEFAULT_URL };
}

// Exactly one connection attempt at a time. Several triggers (startup, the
// keepalive alarm, the heartbeat) can fire together; without this guard each
// opened its own socket and they kept replacing each other on the server.
let connecting = null;
let backoff = 1000;

function connect() {
  if (connecting) return connecting;
  if (ws && ws.readyState <= 1) return Promise.resolve();
  connecting = (async () => {
    try {
      const { token, url } = await config();
      if (!token) {
        status = "no-token";
        return;
      }
      if (ws && ws.readyState <= 1) return;
      let sock;
      try {
        sock = new WebSocket(`${url}?token=${encodeURIComponent(token)}`);
      } catch {
        status = "disconnected";
        return;
      }
      ws = sock;
      status = "connecting";
      await new Promise((resolve) => {
        sock.onopen = () => {
          if (ws !== sock) return resolve();
          status = "connected";
          backoff = 1000;
          sock.send(JSON.stringify({ type: "hello", version: chrome.runtime.getManifest().version }));
          resolve();
        };
        sock.onclose = (ev) => {
          // Only the current socket may change the state (an old socket closing
          // must never wipe out a newer, healthy one).
          if (ws === sock) {
            ws = null;
            status = ev.code === 4401 ? "bad-token" : "disconnected";
            if (ev.code !== 4401) setTimeout(connect, backoff + Math.random() * 500);
            backoff = Math.min(backoff * 2, 15000);
          }
          resolve();
        };
        sock.onerror = () => {};
        sock.onmessage = (ev) => onMessage(sock, ev);
      });
    } finally {
      connecting = null;
    }
  })();
  return connecting;
}

async function onMessage(sock, ev) {
  let msg;
  try {
    msg = JSON.parse(ev.data);
  } catch {
    return;
  }
  if (msg.type === "pong") return;
  if (msg.type === "reload") {
    chrome.runtime.reload();  // Ithaca has a newer version of this extension on disk
    return;
  }
  if (msg.type !== "call") return;
  let reply;
  try {
    reply = { id: msg.id, ok: true, result: await handle(msg.method, msg.params || {}) };
  } catch (e) {
    reply = { id: msg.id, ok: false, error: String((e && e.message) || e) };
  }
  // Answer on the socket the call arrived on.
  if (sock.readyState === 1) sock.send(JSON.stringify({ type: "result", ...reply }));
}

// Keep the service worker and the socket alive.
setInterval(() => {
  if (ws && ws.readyState === 1) ws.send('{"type":"ping"}');
  else connect();
}, 20000);
chrome.alarms.create("ithaca-keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => connect());
chrome.runtime.onStartup.addListener(() => connect());
chrome.runtime.onInstalled.addListener(() => connect());
chrome.debugger.onDetach.addListener((src) => attached.delete(src.tabId));
connect();

chrome.runtime.onMessage.addListener((m, _sender, send) => {
  if (m.type === "status") send({ status });
  if (m.type === "reconnect") {
    const old = ws;
    ws = null;
    if (old) old.close();
    connect().then(() => setTimeout(() => send({ status }), 300));
    return true;
  }
});

// ------------------------------------------------------------------ helpers

function ithacaOrigins() {
  return ["http://127.0.0.1:7100", "http://localhost:7100"];
}

function guard(tab) {
  const url = tab.url || tab.pendingUrl || "";
  if (ithacaOrigins().some((o) => url.startsWith(o))) throw new Error("Ithaca doesn't act on its own pages.");
  if (/^(chrome|edge|about|chrome-extension|devtools):/.test(url)) throw new Error("Chrome doesn't allow extensions to act on this page.");
  if (url.startsWith("https://chromewebstore.google.com")) throw new Error("Chrome doesn't allow extensions to act on the Web Store.");
}

async function agentTab() {
  if (agentTabId !== null) {
    try {
      return await chrome.tabs.get(agentTabId);
    } catch {
      agentTabId = null;
    }
  }
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab) throw new Error("No browser tab is open.");
  agentTabId = tab.id;
  return tab;
}

async function waitLoad(tabId, timeout = 15000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const t = await chrome.tabs.get(tabId);
    if (t.status === "complete") break;
    await new Promise((r) => setTimeout(r, 200));
  }
  await new Promise((r) => setTimeout(r, 500));
}

async function attach(tabId) {
  if (attached.has(tabId)) return;
  await chrome.debugger.attach({ tabId }, "1.3");
  attached.add(tabId);
}

function cdp(tabId, method, params = {}) {
  return chrome.debugger.sendCommand({ tabId }, method, params);
}

// Runs inside the page: number the interactive elements and collect text.
function snapshotFn() {
  const sel =
    "a[href],button,input,textarea,select,summary,[role=button],[role=link],[role=tab],[role=menuitem]," +
    "[role=checkbox],[role=radio],[role=switch],[role=textbox],[role=combobox],[role=option],[contenteditable=true]";
  document.querySelectorAll("[data-ithaca-ref]").forEach((e) => e.removeAttribute("data-ithaca-ref"));
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || st.visibility === "hidden" || st.display === "none") continue;
    if (r.bottom < -200 || r.top > innerHeight * 2.5) continue;
    n += 1;
    el.setAttribute("data-ithaca-ref", String(n));
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute("role") || (tag === "a" ? "link" : tag === "input" ? el.type || "text" : tag);
    const name = (el.getAttribute("aria-label") || el.innerText || el.value || el.getAttribute("title") || el.getAttribute("alt") || "")
      .replace(/\s+/g, " ").trim().slice(0, 80);
    let extra = "";
    const ph = el.getAttribute("placeholder");
    if (ph) extra += ` placeholder="${ph.slice(0, 40)}"`;
    if (tag === "a") {
      try {
        extra += ` → ${new URL(el.href).hostname}`;
      } catch {}
    }
    if (el.disabled) extra += " (disabled)";
    out.push(`[${n}] ${role} "${name}"${extra}`);
    if (n >= 150) break;
  }
  const text = (document.body ? document.body.innerText : "").replace(/\n{3,}/g, "\n\n").slice(0, 6000);
  return { title: document.title, url: location.href, elements: out, text };
}

async function snapshot(tabId, note = "") {
  const tab = await chrome.tabs.get(tabId);
  guard(tab);
  const [res] = await chrome.scripting.executeScript({ target: { tabId }, func: snapshotFn });
  const tabs = await chrome.tabs.query({});
  return { ...res.result, note, tabs: tabs.length };
}

async function locate(tabId, ref) {
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    args: [ref],
    func: (ref) => {
      const el = document.querySelector(`[data-ithaca-ref="${ref}"]`);
      if (!el) return null;
      el.scrollIntoView({ block: "center", inline: "center" });
      const r = el.getBoundingClientRect();
      const tag = el.tagName.toLowerCase();
      return {
        x: r.left + r.width / 2,
        y: r.top + r.height / 2,
        password: tag === "input" && (el.type || "").toLowerCase() === "password",
        field: tag === "input" || tag === "textarea",
      };
    },
  });
  if (!res.result) throw new Error(`No element [${ref}] on the page; read the page again for fresh numbers.`);
  return res.result;
}

async function click(tabId, x, y) {
  await attach(tabId);
  await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
  await cdp(tabId, "Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
  await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
}

const KEYS = {
  enter: ["Enter", "Enter", 13, "\r"], tab: ["Tab", "Tab", 9], escape: ["Escape", "Escape", 27], esc: ["Escape", "Escape", 27],
  backspace: ["Backspace", "Backspace", 8], delete: ["Delete", "Delete", 46], space: [" ", "Space", 32, " "],
  arrowup: ["ArrowUp", "ArrowUp", 38], arrowdown: ["ArrowDown", "ArrowDown", 40], arrowleft: ["ArrowLeft", "ArrowLeft", 37],
  arrowright: ["ArrowRight", "ArrowRight", 39], up: ["ArrowUp", "ArrowUp", 38], down: ["ArrowDown", "ArrowDown", 40],
  pagedown: ["PageDown", "PageDown", 34], pageup: ["PageUp", "PageUp", 33], home: ["Home", "Home", 36], end: ["End", "End", 35],
};

async function press(tabId, combo) {
  await attach(tabId);
  const parts = combo.split("+").map((p) => p.trim().toLowerCase()).filter(Boolean);
  let modifiers = 0;
  let key = null;
  for (const p of parts) {
    if (p === "alt" || p === "option") modifiers |= 1;
    else if (p === "ctrl" || p === "control") modifiers |= 2;
    else if (p === "meta" || p === "cmd" || p === "command") modifiers |= 4;
    else if (p === "shift") modifiers |= 8;
    else key = p;
  }
  let def = KEYS[key];
  if (!def && key && key.length === 1) {
    const upper = key.toUpperCase();
    def = [key, /[a-z]/.test(key) ? `Key${upper}` : `Digit${key}`, upper.charCodeAt(0), modifiers ? undefined : key];
  }
  if (!def) throw new Error(`Unknown key "${combo}".`);
  const [k, code, vk, text] = def;
  const base = { key: k, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers };
  await cdp(tabId, "Input.dispatchKeyEvent", { type: text ? "keyDown" : "rawKeyDown", ...base, ...(text ? { text } : {}) });
  await cdp(tabId, "Input.dispatchKeyEvent", { type: "keyUp", ...base });
}

const settle = (ms = 800) => new Promise((r) => setTimeout(r, ms));

// ------------------------------------------------------------------ commands

async function handle(method, p) {
  if (method === "ping") return { ok: true };

  if (method === "open") {
    let tab;
    if (p.new_tab !== false || agentTabId === null) {
      tab = await chrome.tabs.create({ url: p.url, active: true });
    } else {
      tab = await chrome.tabs.update((await agentTab()).id, { url: p.url, active: true });
    }
    agentTabId = tab.id;
    await chrome.windows.update(tab.windowId, { focused: true });
    await waitLoad(tab.id);
    return snapshot(tab.id);
  }

  const tab = await agentTab();
  guard(tab);

  switch (method) {
    case "snapshot":
      return snapshot(tab.id);
    case "click": {
      const target = await locate(tab.id, p.ref);
      const before = new Set((await chrome.tabs.query({})).map((t) => t.id));
      await click(tab.id, target.x, target.y);
      await settle();
      const opened = (await chrome.tabs.query({})).find((t) => !before.has(t.id) && t.openerTabId === tab.id);
      if (opened) {
        agentTabId = opened.id;
        await waitLoad(opened.id);
        return snapshot(opened.id, `Clicked [${p.ref}]; it opened a new tab.\n`);
      }
      await waitLoad(tab.id, 6000);
      return snapshot(tab.id, `Clicked [${p.ref}].\n`);
    }
    case "type": {
      const target = await locate(tab.id, p.ref);
      if (target.password) throw new Error("Ithaca never types passwords. Ask the user to type it in the browser themselves.");
      await click(tab.id, target.x, target.y);
      if (target.field && p.replace !== false) await press(tab.id, "Meta+A");
      await cdp(tab.id, "Input.insertText", { text: p.text });
      if (p.submit) {
        await press(tab.id, "Enter");
        await settle(1200);
        await waitLoad(tab.id, 8000);
      }
      return snapshot(tab.id, `Typed into [${p.ref}]${p.submit ? " and pressed Enter" : ""}.\n`);
    }
    case "type_here": {
      // Type at the current cursor (e.g. into a Google Doc after clicking in it).
      await attach(tab.id);
      await cdp(tab.id, "Input.insertText", { text: p.text });
      await settle(500);
      return snapshot(tab.id, `Typed ${p.text.length} characters.\n`);
    }
    case "press":
      await press(tab.id, p.key);
      await settle(600);
      return snapshot(tab.id, `Pressed ${p.key}.\n`);
    case "scroll":
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        args: [p.direction === "up" ? -1 : 1],
        func: (d) => window.scrollBy(0, d * innerHeight * 0.8),
      });
      await settle(400);
      return snapshot(tab.id, `Scrolled ${p.direction}.\n`);
    case "back":
      await chrome.tabs.goBack(tab.id);
      await waitLoad(tab.id);
      return snapshot(tab.id, "Went back.\n");
    case "tabs": {
      const tabs = await chrome.tabs.query({});
      if (p.index !== undefined && p.index !== null) {
        const t = tabs[p.index];
        if (!t) throw new Error(`There are ${tabs.length} tabs.`);
        guard(t);
        agentTabId = t.id;
        await chrome.tabs.update(t.id, { active: true });
        await chrome.windows.update(t.windowId, { focused: true });
        return snapshot(t.id, `Switched to tab ${p.index}.\n`);
      }
      return { list: tabs.map((t, i) => `${i}: ${t.title} — ${t.url}${t.id === agentTabId ? "  (Ithaca's tab)" : ""}`) };
    }
    case "screenshot": {
      await chrome.tabs.update(tab.id, { active: true });
      const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: 70 });
      return { image: dataUrl.split(",")[1], url: tab.url };
    }
    case "release":
      for (const id of [...attached]) {
        try {
          await chrome.debugger.detach({ tabId: id });
        } catch {}
      }
      attached.clear();
      return { ok: true };
    default:
      throw new Error(`Unknown command ${method}`);
  }
}
