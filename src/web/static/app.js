/* eslint-disable no-console */

const $ = (id) => document.getElementById(id);

const state = {
  ws: null,
  reconnectTimer: null,
  running: false,
};

function setStatus(kind, label) {
  const pill = $("status-pill");
  pill.className = `pill ${kind}`;
  pill.textContent = label;
}

function setRunning(yes) {
  state.running = yes;
  $("run-btn").disabled = yes;
  $("cancel-btn").disabled = !yes;
}

function timelineEl() { return $("timeline"); }

function appendEntry(html, classes = "entry") {
  const el = document.createElement("div");
  el.className = classes;
  el.innerHTML = html;
  timelineEl().appendChild(el);
  timelineEl().scrollTop = timelineEl().scrollHeight;
  return el;
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fmtArgs(obj) {
  if (!obj || Object.keys(obj).length === 0) return "";
  try {
    return JSON.stringify(obj, null, 0)
      .replace(/^{/, "")
      .replace(/}$/, "");
  } catch {
    return String(obj);
  }
}

function clearTimeline() {
  timelineEl().innerHTML = "";
}

function showAskBar(question) {
  $("ask-question").textContent = question;
  $("ask-bar").classList.remove("hidden");
  $("ask-input").value = "";
  $("ask-input").focus();
  setStatus("waiting", "waiting for you");
}

function hideAskBar() {
  $("ask-bar").classList.add("hidden");
}

function renderScratchpad(entries) {
  const body = $("scratchpad-body");
  if (!entries || Object.keys(entries).length === 0) {
    body.innerHTML = '<div class="scratchpad-empty">empty</div>';
    return;
  }
  const rows = Object.entries(entries).map(([k, v]) =>
    `<div class="scratchpad-entry"><span class="scratchpad-key">${escapeHtml(k)}</span><span class="scratchpad-val">${escapeHtml(v)}</span></div>`
  );
  body.innerHTML = rows.join("");
}

// --- Event handlers ---------------------------------------------------------

function handleEvent(msg) {
  switch (msg.type) {
    case "AgentStarted":
      clearTimeline();
      setRunning(true);
      setStatus("running", "running");
      appendEntry(`<div class="panel-title">task</div>${escapeHtml(msg.task)}`, "entry task");
      break;
    case "LLMRequestStarted":
      appendEntry(`<span style="color:#6e7681">[step ${msg.step}] thinking…</span>`, "entry usage");
      break;
    case "LLMRequestCompleted": {
      const cumulative = (msg.cumulative_cost_usd ?? 0).toFixed(4);
      const partialMark = msg.cost_partial ? "+?" : "";
      $("cost-meter").textContent = `$${cumulative}${partialMark}`;
      const stepStr = msg.cost_usd === null || msg.cost_usd === undefined
        ? "?"
        : `$${msg.cost_usd.toFixed(4)}`;
      appendEntry(
        `<span>[step ${msg.step}] in=${msg.input_tokens} out=${msg.output_tokens} ` +
        `cache_r=${msg.cache_read_tokens} cache_w=${msg.cache_creation_tokens} ` +
        `step=${stepStr} total=$${cumulative}${partialMark}</span>`,
        "entry usage"
      );
      break;
    }
    case "AgentThinking":
      if (msg.text && msg.text.trim()) {
        appendEntry(escapeHtml(msg.text), "entry thinking");
      }
      break;
    case "ToolCallStarted":
      appendEntry(
        `<span class="arrow">→</span><span class="name">${escapeHtml(msg.tool)}</span>` +
        `<span class="args">(${escapeHtml(fmtArgs(msg.args))})</span>`,
        "entry tool-call"
      );
      break;
    case "ToolCallCompleted":
      appendEntry(
        `${msg.is_error ? "✗" : "✓"} ${escapeHtml(msg.result_summary)}`,
        `entry tool-result ${msg.is_error ? "err" : "ok"}`
      );
      break;
    case "ScratchpadUpdated":
      renderScratchpad(msg.entries);
      break;
    case "NeedsUserInput":
      showAskBar(msg.question);
      break;
    case "TaskCompleted":
      setRunning(false);
      setStatus("done", "done");
      hideAskBar();
      appendEntry(
        `<div class="panel-title">done</div>${escapeHtml(msg.report)}`,
        "entry panel done"
      );
      break;
    case "TaskFailed":
      setRunning(false);
      setStatus("failed", "failed");
      hideAskBar();
      appendEntry(
        `<div class="panel-title">failed</div>${escapeHtml(msg.reason)}`,
        "entry panel failed"
      );
      break;
    case "Error":
      appendEntry(
        `<div class="panel-title">error</div>${escapeHtml(msg.reason)}`,
        "entry panel failed"
      );
      break;
    case "pong":
      break;
    default:
      console.warn("unknown event", msg);
  }
}

// --- WebSocket connection ---------------------------------------------------

function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/ws`;
  const ws = new WebSocket(url);
  state.ws = ws;

  ws.onopen = () => {
    if (!state.running) setStatus("idle", "idle");
  };
  ws.onmessage = (ev) => {
    try {
      handleEvent(JSON.parse(ev.data));
    } catch (e) {
      console.error("bad event", ev.data, e);
    }
  };
  ws.onclose = () => {
    setStatus("failed", "disconnected");
    setRunning(false);
    state.ws = null;
    state.reconnectTimer = setTimeout(connect, 1500);
  };
  ws.onerror = (e) => console.error("ws error", e);
}

function send(obj) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(obj));
  }
}

// --- noVNC iframe -----------------------------------------------------------

async function bootNoVnc() {
  let url = "http://localhost:6080/vnc.html?autoconnect=1&resize=remote";
  try {
    const res = await fetch("/health");
    const data = await res.json();
    if (data.novnc_url) url = data.novnc_url;
  } catch {
    // fall through to default
  }
  $("vnc-iframe").src = url;
  $("novnc-link").href = url;
}

// --- wire up UI -------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  bootNoVnc();
  connect();

  $("run-btn").addEventListener("click", () => {
    const task = $("task-input").value.trim();
    if (!task) return;
    send({ type: "run", task });
  });

  $("cancel-btn").addEventListener("click", () => {
    send({ type: "cancel" });
  });

  $("task-input").addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      $("run-btn").click();
    }
  });

  $("ask-submit").addEventListener("click", () => {
    const answer = $("ask-input").value;
    if (!answer.trim()) return;
    send({ type: "answer", answer });
    hideAskBar();
    setStatus("running", "running");
  });

  $("ask-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      $("ask-submit").click();
    }
  });
});
