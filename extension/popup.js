const LABELS = {
  connected: ["Connected to Ithaca", "ok"],
  connecting: ["Connecting…", ""],
  disconnected: ["Ithaca isn't running", "bad"],
  "no-token": ["Not paired yet", ""],
  "bad-token": ["Pairing code rejected", "bad"],
};

function show(status) {
  const [text, cls] = LABELS[status] || [status, ""];
  document.getElementById("label").textContent = text;
  document.getElementById("status").className = `status ${cls}`;
}

chrome.runtime.sendMessage({ type: "status" }, (r) => show(r ? r.status : "disconnected"));
chrome.storage.local.get(["token"], ({ token }) => {
  if (token) document.getElementById("token").placeholder = "Paired — paste a new code to re-pair";
});

document.getElementById("save").addEventListener("click", async () => {
  const token = document.getElementById("token").value.trim();
  if (token) await chrome.storage.local.set({ token });
  show("connecting");
  chrome.runtime.sendMessage({ type: "reconnect" }, (r) => show(r ? r.status : "disconnected"));
});
