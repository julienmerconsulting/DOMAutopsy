// DOMAutopsy frontend - vanilla JS, pas de dep
const $ = (sel) => document.querySelector(sel);

const form = $("#runForm");
const submitBtn = $("#submitBtn");
const stopBtn = $("#stopBtn");
const reportBtn = $("#reportBtn");
const clearLogBtn = $("#clearLogBtn");
const logBox = $("#log");
const canvas = $("#screen");
const ctx = canvas.getContext("2d");
const canvasEmpty = $("#canvasEmpty");
const screenStatus = $("#screenStatus");
const logStatus = $("#logStatus");
const formatSelect = $("#output_format");
const runBadge = $("#runBadge");
const runIdLabel = $("#runIdLabel");
const portLabel = $("#portLabel");
const activeRunsBadge = $("#activeRunsBadge");

let currentRunId = null;
let logSocket = null;
let screenSocket = null;
let activeRunsTimer = null;

async function loadFormats() {
  try {
    const resp = await fetch("/api/formats");
    const formats = await resp.json();
    formatSelect.innerHTML = "";
    for (const [key, info] of Object.entries(formats)) {
      const opt = document.createElement("option");
      opt.value = key;
      opt.textContent = `${info.label} (${info.extension})`;
      if (key === "katalon") opt.selected = true;
      formatSelect.appendChild(opt);
    }
  } catch (e) {
    appendLog("ERREUR : impossible de charger les formats : " + e.message, "error");
  }
}

async function refreshActiveRuns() {
  try {
    const resp = await fetch("/api/runs");
    const runs = await resp.json();
    const active = runs.filter(r => r.status === "running").length;
    const total = runs.length;
    activeRunsBadge.textContent = `${active} actif${active > 1 ? "s" : ""} / ${total} total`;
  } catch (e) {
    activeRunsBadge.textContent = "API offline";
  }
}

function appendLog(line, kind) {
  if (!kind) {
    if (/ERROR|❌|FAIL/i.test(line)) kind = "error";
    else if (/WARNING|WARN|\[WARN\]/i.test(line)) kind = "warn";
    else if (/✅|\[OK\]|SUCCESS/i.test(line)) kind = "ok";
    else if (/Step \d+|📍/i.test(line)) kind = "step";
    else kind = "info";
  }
  if (logBox.textContent === "En attente...") logBox.textContent = "";
  const span = document.createElement("span");
  span.className = `l-${kind}`;
  span.textContent = line + "\n";
  logBox.appendChild(span);
  logBox.scrollTop = logBox.scrollHeight;
}

function setStatus(el, state) {
  el.className = "status-dot " + (state || "");
}

function drawFrame(base64Data) {
  const img = new Image();
  img.onload = () => {
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    ctx.drawImage(img, 0, 0);
    canvasEmpty.style.display = "none";
  };
  img.src = "data:image/jpeg;base64," + base64Data;
}

function closeSockets() {
  if (logSocket) { try { logSocket.close(); } catch(e) {} logSocket = null; }
  if (screenSocket) { try { screenSocket.close(); } catch(e) {} screenSocket = null; }
}

async function killCurrentRun() {
  if (!currentRunId) return;
  try {
    await fetch(`/api/run/${currentRunId}`, { method: "DELETE" });
  } catch (e) { /* best effort */ }
}

function setRunIdle() {
  submitBtn.disabled = false;
  submitBtn.textContent = "Lancer le run";
  stopBtn.disabled = true;
}
function setRunActive(runId, port) {
  submitBtn.disabled = true;
  submitBtn.textContent = "Run en cours...";
  stopBtn.disabled = false;
  runBadge.hidden = false;
  runIdLabel.textContent = runId;
  portLabel.textContent = port;
}

function openLogSocket(runId) {
  setStatus(logStatus, "running");
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws/logs/${runId}`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "log") {
      appendLog(msg.line);
    } else if (msg.type === "end") {
      const ok = msg.status === "exit_0";
      setStatus(logStatus, ok ? "ok" : "error");
      appendLog(`\n--- Run termine (${msg.status}) ---`, ok ? "ok" : "error");
      reportBtn.disabled = !ok;
      setRunIdle();
    } else if (msg.type === "error") {
      appendLog("ERREUR WS log: " + msg.message, "error");
    }
  };
  ws.onerror = () => {
    setStatus(logStatus, "error");
    appendLog("ERREUR : WebSocket logs deconnecte", "error");
  };
  ws.onclose = () => {
    if (logStatus.classList.contains("running")) setStatus(logStatus, "");
  };
  return ws;
}

function openScreenSocket(runId) {
  setStatus(screenStatus, "running");
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws/screen/${runId}`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "frame") {
      drawFrame(msg.data);
      setStatus(screenStatus, "ok");
    } else if (msg.type === "error") {
      appendLog("ERREUR WS screen: " + msg.message, "warn");
      setStatus(screenStatus, "error");
    }
  };
  ws.onclose = () => {
    if (screenStatus.classList.contains("running")) setStatus(screenStatus, "");
  };
  return ws;
}

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  closeSockets();
  logBox.textContent = "";
  reportBtn.disabled = true;
  canvasEmpty.style.display = "block";
  canvasEmpty.textContent = "Chromium se lance...";

  const payload = {
    url: $("#url").value.trim(),
    task: $("#task").value.trim(),
    output_format: $("#output_format").value,
    provider: $("#provider").value,
    model: $("#model").value.trim() || null,
    min_wait: parseFloat($("#min_wait").value),
    max_wait: parseFloat($("#max_wait").value),
    network_idle: parseFloat($("#network_idle").value),
    max_steps: parseInt($("#max_steps").value, 10),
    headless: $("#headless").checked,
  };

  try {
    const resp = await fetch("/api/run", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const { run_id, cdp_port } = await resp.json();
    currentRunId = run_id;
    setRunActive(run_id, cdp_port);
    appendLog(`>> Run ${run_id} sur CDP ${cdp_port} (headless=${payload.headless})`, "ok");

    logSocket = openLogSocket(run_id);
    setTimeout(() => {
      screenSocket = openScreenSocket(run_id);
    }, 1500);
    refreshActiveRuns();
  } catch (e) {
    appendLog("ERREUR au lancement : " + e.message, "error");
    setRunIdle();
  }
});

stopBtn.addEventListener("click", async () => {
  if (!currentRunId) return;
  appendLog(`>> Stop demande pour ${currentRunId}`, "warn");
  await killCurrentRun();
  closeSockets();
  setRunIdle();
});

reportBtn.addEventListener("click", () => {
  if (currentRunId) {
    window.open(`/api/report/${currentRunId}`, "_blank");
  }
});

clearLogBtn.addEventListener("click", () => {
  logBox.textContent = "";
});

// Cleanup quand l'utilisateur ferme l'onglet : kill le subprocess pour eviter les zombies
// sendBeacon est synchrone et survit au teardown de la page (fetch normal serait abort)
window.addEventListener("beforeunload", () => {
  if (currentRunId) {
    const url = `/api/run/${currentRunId}`;
    // Pas de DELETE via sendBeacon (juste POST), donc fallback fetch keepalive
    try {
      fetch(url, { method: "DELETE", keepalive: true });
    } catch (e) {}
  }
});

// Refresh badge "runs actifs" toutes les 10s (utile pour voir les runs des AUTRES onglets)
// Pause le polling quand l'onglet est en background pour pas spammer le serveur
activeRunsTimer = setInterval(() => {
  if (!document.hidden) refreshActiveRuns();
}, 10000);
// Refresh immediatement quand l'utilisateur revient sur l'onglet
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshActiveRuns();
});

// Boot
loadFormats();
refreshActiveRuns();
