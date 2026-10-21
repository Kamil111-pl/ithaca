const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ---------------------------------------------------------------- theme */

const root = document.documentElement;
const systemDark = matchMedia("(prefers-color-scheme: dark)");
const dark = () => (root.dataset.theme ? root.dataset.theme === "dark" : systemDark.matches);
function syncThemeIcon() {
  const d = dark();
  $("#theme .sun").style.display = $("#theme .moon").style.display = "block";
  $("#theme .sun").classList.toggle("off", !d);
  $("#theme .moon").classList.toggle("off", d);
  $("#theme").setAttribute("aria-label", d ? "Switch to light theme" : "Switch to dark theme");
}
$("#theme").addEventListener("click", () => {
  root.dataset.theme = dark() ? "light" : "dark";
  try { localStorage.setItem("ithaca-theme", root.dataset.theme); } catch {}
  syncThemeIcon();
});
systemDark.addEventListener("change", syncThemeIcon);
syncThemeIcon();

/* ----------------------------------------------------------------- copy */

for (const b of $$("[data-copy]")) {
  b.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); } catch { return; }
    const ic = $(".copy-ic", b);
    if (ic) {
      ic.children[0].classList.add("off");
      ic.children[1].classList.remove("off");
      setTimeout(() => { ic.children[0].classList.remove("off"); ic.children[1].classList.add("off"); }, 1600);
    } else {
      const t = b.textContent;
      b.textContent = "Copied";
      setTimeout(() => (b.textContent = t), 1600);
    }
  });
}

/* ------------------------------------------------------- scroll reveals */

const io = new IntersectionObserver((entries) => {
  for (const e of entries) if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
}, { threshold: 0.12 });
for (const el of $$(".block > *, .stages")) { el.classList.add("reveal"); io.observe(el); }

const tabs = $$(".tab");
const spy = new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    for (const t of tabs) t.classList.toggle("on", t.getAttribute("href") === "#" + e.target.id);
  }
}, { rootMargin: "-45% 0px -50% 0px" });
for (const t of tabs) { const s = $(t.getAttribute("href")); if (s) spy.observe(s); }

/* -------------------------------------------------------- approval mode */

const POLICY = {
  smart: "The default. Reads and safe tools just run. Shell commands, writes outside the workspace, and anything outbound after reading the web stop and ask.",
  always: "Every tool call that changes something waits for your yes — files, shell, email, MCP. Slower, and completely in your hands.",
  never: "For trusted local work: tools run without stopping. The sandbox still applies, and tainted runs still can’t leak data without asking.",
};
const policyText = $("#policy-text");
policyText.textContent = POLICY.smart;
for (const b of $$(".segmented button")) {
  b.addEventListener("click", () => {
    $$(".segmented button").forEach((x) => { x.classList.toggle("on", x === b); x.setAttribute("aria-selected", x === b); });
    policyText.style.opacity = 0;
    setTimeout(() => { policyText.textContent = POLICY[b.dataset.mode]; policyText.style.opacity = 1; }, 180);
  });
}

/* ------------------------------------------------------------- pipeline */

const steps = $$("#pipeline li");
let pipeTimer = 0;
function runPipeline() {
  let i = 0;
  clearInterval(pipeTimer);
  const paint = () => steps.forEach((s, j) => { s.classList.toggle("done", j < i); s.classList.toggle("active", j === i); });
  paint();
  pipeTimer = setInterval(() => {
    i = (i + 1) % (steps.length + 2);
    paint();
  }, 1100);
}
new IntersectionObserver(([e], o) => { if (e.isIntersecting) { runPipeline(); o.disconnect(); } }, { threshold: 0.4 }).observe($("#pipeline"));

/* ----------------------------------------------------------------- demo */

const thread = $("#thread");
const replay = $("#replay");
const el = (html) => { const d = document.createElement("div"); d.innerHTML = html.trim(); return d.firstChild; };
function push(node) {
  thread.appendChild(node);
  while (thread.scrollHeight > thread.clientHeight + 4 && thread.children.length > 1) thread.firstChild.remove();
  return node;
}

const STAGES = {
  think: ["solving", "Thinking"],
  search: ["searching", "Looking things up"],
  act: ["working", "Taking action"],
  approve: ["breathing", "Waiting for your approval"],
  write: ["composing", "Writing"],
};
let activity = null;
function stage(key) {
  const [state, label] = STAGES[key];
  if (!activity) activity = push(el(`<div class="turn"><div class="activity"><ithaca-orb size="64" px="38"></ithaca-orb><span class="activity-label shimmer"></span></div></div>`));
  const orb = $("ithaca-orb", activity);
  orb.setAttribute("speed", key === "approve" ? "0.45" : "1");
  orb.setAttribute("state", state);
  const lbl = $(".activity-label", activity);
  lbl.textContent = label;
  lbl.style.animation = "none"; void lbl.offsetWidth; lbl.style.animation = "";
  thread.appendChild(activity);
}
function endActivity() { activity?.remove(); activity = null; }

function tool(name, arg, badge) {
  push(el(`<div class="turn"><div class="toolcard"><span class="status-dot"></span><span class="name">${name}</span><span class="arg">${arg}</span>${badge ? `<span class="badge">${badge}</span>` : ""}</div></div>`));
}

async function typeInto(node, text) {
  if (reduced) { node.textContent = text; return; }
  for (const w of text.split(/(\s+)/)) { node.textContent += w; await sleep(w.trim() ? 38 : 0); }
}

let runId = 0;
async function play() {
  const id = ++runId;
  const alive = () => id === runId;
  replay.hidden = true;
  thread.innerHTML = "";
  activity = null;

  push(el(`<div class="turn me"><div class="bubble">Read the release notes on our site and email a summary to the team.</div></div>`));
  await sleep(700); if (!alive()) return;
  stage("think"); await sleep(1500); if (!alive()) return;
  stage("search"); await sleep(1200); if (!alive()) return;
  tool("web_fetch", "acme.dev/changelog/2.4", "tainted"); await sleep(1100); if (!alive()) return;
  stage("think"); await sleep(1100); if (!alive()) return;
  stage("approve");

  const card = push(el(`
    <div class="turn"><div class="approval">
      <h4><ithaca-orb state="breathing" size="20" px="18" speed="0.45"></ithaca-orb>Send an email?</h4>
      <p>This run read content from the web, so outbound messages need your approval.</p>
      <pre>to: team@acme.dev
subject: What’s new in 2.4</pre>
      <div class="row"><button class="btn primary">Allow once</button><button class="btn">Deny</button></div>
    </div></div>`));
  thread.appendChild(activity);

  const allowed = await new Promise((resolve) => {
    const [yes, no] = $$(".btn", card);
    yes.onclick = () => resolve(true);
    no.onclick = () => resolve(false);
    setTimeout(() => resolve(true), 9000); // keep the demo moving if no one clicks
  });
  if (!alive()) return;
  $(".approval", card).classList.add("done");
  $$(".btn", card).forEach((b) => (b.disabled = true));
  $("h4", card).lastChild.textContent = allowed ? "Allowed once" : "Denied";

  if (allowed) {
    stage("act"); await sleep(1000); if (!alive()) return;
    tool("send_email", "to: team@acme.dev", "approved"); await sleep(700); if (!alive()) return;
  }
  stage("write"); await sleep(1300); if (!alive()) return;
  endActivity();
  const b = push(el(`<div class="turn"><div class="bubble"></div></div>`)).firstChild;
  await typeInto(b, allowed
    ? "Sent. 2.4 adds offline sync, a faster search index and two security fixes — I kept it to five bullets and linked the full notes."
    : "Okay, nothing was sent. Here’s the summary instead: 2.4 adds offline sync, a faster search index and two security fixes.");
  if (!alive()) return;
  replay.hidden = false;
}
replay.addEventListener("click", play);

let started = false;
new IntersectionObserver(([e]) => {
  if (e.isIntersecting && !started) { started = true; play(); }
}, { threshold: 0.35 }).observe($("#demo"));
