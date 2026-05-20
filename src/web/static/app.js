/* eslint-disable no-console */
/* Browser Assistant — frontend
   Группирует события по шагам, прячет технические метрики, переключает тему,
   управляет ask-модалкой и settings. Совместим с теми же событиями, что
   шлёт сервер (см. src/agent/events.py). */

const $ = (id) => document.getElementById(id);

const MAX_COST_BUDGET = 2.0; // визуальный кэп — соответствует дефолту MAX_COST_USD

const TOOL_ICONS = {
  observe: "👁",
  navigate: "🧭",
  click: "🖱",
  type: "⌨",
  press_key: "⏎",
  select: "▾",
  scroll: "↕",
  wait_for: "⏳",
  go_back: "←",
  go_forward: "→",
  extract: "📤",
  remember: "💾",
  recall: "🔎",
  ask_user: "❓",
  done: "✅",
};

const TOOL_HUMAN = {
  observe: "осмотр страницы",
  navigate: "переход по URL",
  click: "клик",
  type: "ввод текста",
  press_key: "нажатие клавиши",
  select: "выбор в <select>",
  scroll: "прокрутка",
  wait_for: "ожидание",
  go_back: "назад в истории",
  go_forward: "вперёд в истории",
  extract: "извлечение данных",
  remember: "запомнить",
  recall: "вспомнить",
  ask_user: "вопрос пользователю",
  done: "финал",
};

const RESULT_PREVIEW_LIMIT = 240;

const state = {
  ws: null,
  reconnectTimer: null,
  running: false,
  steps: new Map(),       // step → { el, callsEl, calls: Map<idx, {el, resultEl}> }
  pendingCalls: [],       // queue: tool calls awaiting their step container
  currentStep: null,
  toolCount: 0,
  options: {
    showDetails: true,
    showThinking: true,
    autoscroll: true,
    notifyDone: false,
  },
};

// ============================================================
// Settings persistence
// ============================================================

function loadOptions() {
  try {
    const raw = localStorage.getItem("ba-options");
    if (raw) Object.assign(state.options, JSON.parse(raw));
  } catch {}
  const theme = localStorage.getItem("ba-theme") || "dark";
  document.documentElement.dataset.theme = theme;
  $("theme-btn").textContent = theme === "dark" ? "☀" : "🌙";
  $("opt-show-details").checked = state.options.showDetails;
  $("opt-show-thinking").checked = state.options.showThinking;
  $("opt-autoscroll").checked = state.options.autoscroll;
  $("opt-notify-done").checked = state.options.notifyDone;
  applyOptions();
}

function saveOptions() {
  localStorage.setItem("ba-options", JSON.stringify(state.options));
}

function applyOptions() {
  document.querySelectorAll(".usage-row").forEach((el) => {
    el.classList.toggle("hidden", !state.options.showDetails);
  });
  document.querySelectorAll(".thinking").forEach((el) => {
    el.style.display = state.options.showThinking ? "" : "none";
  });
}

// ============================================================
// Status / cost meters
// ============================================================

function setStatus(kind, label) {
  const pill = $("status-pill");
  pill.className = `pill ${kind}`;
  pill.querySelector(".pill-text").textContent = label;
  $("info-state").textContent = label;
}

function setRunning(yes) {
  state.running = yes;
  $("run-btn").disabled = yes;
  $("cancel-btn").disabled = !yes;
}

function updateCost(value, partial) {
  const text = `$${value.toFixed(4)}${partial ? "+?" : ""}`;
  $("cost-meter").textContent = text;
  $("info-cost").textContent = text;
  const pct = Math.min(100, (value / MAX_COST_BUDGET) * 100);
  const bar = $("cost-bar-fill");
  bar.style.width = `${pct}%`;
  bar.classList.toggle("warn", pct >= 60 && pct < 90);
  bar.classList.toggle("danger", pct >= 90);
}

// ============================================================
// Welcome / timeline switching
// ============================================================

function showWelcome() {
  $("welcome").hidden = false;
  $("timeline").hidden = true;
}
function hideWelcome() {
  $("welcome").hidden = true;
  $("timeline").hidden = false;
}

function clearTimeline() {
  $("timeline").innerHTML = "";
  state.steps.clear();
  state.pendingCalls = [];
  state.currentStep = null;
  state.toolCount = 0;
  $("info-steps").textContent = "0";
  $("info-tools").textContent = "0";
  // Сбросить scratchpad — иначе записи с прошлой задачи висят.
  const body = $("scratchpad-body");
  body.innerHTML = `<div class="scratchpad-empty">пока пусто — агент сохранит сюда то, что нужно помнить между шагами</div>`;
  $("scratchpad-count").textContent = "0";
}

function resetForNewRun() {
  clearTimeline();
  hideWelcome();
  hideAskBar();
  updateCost(0, false);
}

// ============================================================
// Helpers
// ============================================================

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function el(tag, className, html) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (html !== undefined) e.innerHTML = html;
  return e;
}

function scrollTimeline() {
  if (!state.options.autoscroll) return;
  const t = $("timeline");
  t.scrollTop = t.scrollHeight;
}

function formatArg(value) {
  if (typeof value === "string") {
    const truncated = value.length > 80 ? value.slice(0, 77) + "…" : value;
    return `"${escapeHtml(truncated)}"`;
  }
  if (value === null || typeof value !== "object") return escapeHtml(String(value));
  try {
    const s = JSON.stringify(value);
    return escapeHtml(s.length > 80 ? s.slice(0, 77) + "…" : s);
  } catch {
    return escapeHtml(String(value));
  }
}

function formatArgs(obj) {
  if (!obj || Object.keys(obj).length === 0) return "";
  return Object.entries(obj)
    .map(([k, v]) => `<span class="arg-key">${escapeHtml(k)}</span>=${formatArg(v)}`)
    .join(", ");
}

function humanSummaryForCall(name, args) {
  const a = args || {};
  switch (name) {
    case "navigate": return a.url || "";
    case "click": return `элемент ${a.element_id ?? "?"}`;
    case "type": return `«${(a.text ?? "").slice(0, 40)}»${a.submit ? " ⏎" : ""}`;
    case "press_key": return a.key ?? "";
    case "select": return `${a.element_id ?? "?"} → ${a.value ?? ""}`;
    case "scroll": return a.direction ?? "";
    case "wait_for": return `${a.condition ?? ""}${a.value ? ` (${a.value})` : ""}`;
    case "extract": return (a.instruction ?? "").slice(0, 60);
    case "remember": return `${a.key ?? "?"} = ${(a.value ?? "").slice(0, 40)}`;
    case "recall": return a.key ?? "";
    case "ask_user": return (a.question ?? "").slice(0, 60);
    case "done": return (a.report ?? "").slice(0, 60);
    default: return "";
  }
}

// ============================================================
// Step containers
// ============================================================

function ensureStepContainer(stepNum) {
  if (state.steps.has(stepNum)) return state.steps.get(stepNum);

  // Mark previous step inactive
  if (state.currentStep && state.steps.has(state.currentStep)) {
    state.steps.get(state.currentStep).el.classList.remove("active");
  }

  const wrapper = el("div", "step active");
  const head = el("button", "step-head");
  head.type = "button";
  const numBadge = el("span", "step-num", `#${stepNum}`);
  const summary = el("span", "step-summary");
  summary.innerHTML = `<span class="step-empty">шаг ${stepNum} — думаю…</span>`;
  const meta = el("span", "step-meta");
  const chevron = el("span", "step-chevron", "▾");
  head.append(numBadge, summary, meta, chevron);

  const body = el("div", "step-body");

  head.addEventListener("click", () => wrapper.classList.toggle("collapsed"));

  wrapper.append(head, body);
  $("timeline").appendChild(wrapper);

  const ctx = {
    el: wrapper,
    headEl: head,
    summaryEl: summary,
    metaEl: meta,
    bodyEl: body,
    calls: [],          // [{name, args, resultEl}]
    hasThinking: false,
  };
  state.steps.set(stepNum, ctx);
  state.currentStep = stepNum;
  $("info-steps").textContent = String(stepNum);
  scrollTimeline();
  return ctx;
}

function updateStepSummary(step) {
  if (!step.calls.length && !step.hasThinking) return;
  if (step.calls.length === 0) {
    step.summaryEl.innerHTML = `<span class="step-empty">шаг — размышление</span>`;
    return;
  }
  const parts = step.calls.map((c) => {
    const icon = TOOL_ICONS[c.name] || "•";
    const hint = humanSummaryForCall(c.name, c.args);
    return `${icon} ${escapeHtml(c.name)}${hint ? ` <span style="color:var(--text-faint)">${escapeHtml(hint)}</span>` : ""}`;
  });
  step.summaryEl.innerHTML = parts.join(" · ");
}

// ============================================================
// Event handlers
// ============================================================

function handleAgentStarted(msg) {
  // Лента и scratchpad уже сброшены в resetForNewRun (вызывается при клике
  // Run), здесь только переключаем статус и рисуем баннер задачи.
  resetForNewRun();
  setRunning(true);
  setStatus("running", "выполняется");

  const banner = el("div", "task-banner");
  banner.innerHTML = `
    <div class="task-banner-label">задача</div>
    <div>${escapeHtml(msg.task)}</div>
  `;
  $("timeline").appendChild(banner);
}

function handleLLMStarted(msg) {
  ensureStepContainer(msg.step);
}

function handleLLMCompleted(msg) {
  const ctx = ensureStepContainer(msg.step);
  const cumulative = msg.cumulative_cost_usd ?? 0;
  const partial = !!msg.cost_partial;
  updateCost(cumulative, partial);

  const stepStr = msg.cost_usd == null ? "?" : `$${msg.cost_usd.toFixed(4)}`;
  const row = el(
    "div",
    "usage-row" + (state.options.showDetails ? "" : " hidden"),
    `↻ in=${msg.input_tokens} · out=${msg.output_tokens} · cache_r=${msg.cache_read_tokens} · cache_w=${msg.cache_creation_tokens} · step=${stepStr}`
  );
  ctx.bodyEl.appendChild(row);
  ctx.metaEl.textContent = stepStr;
  scrollTimeline();
}

function handleAgentThinking(msg) {
  if (!msg.text || !msg.text.trim()) return;
  const step = state.currentStep ? state.steps.get(state.currentStep) : null;
  if (!step) return;
  const e = el("div", "thinking", escapeHtml(msg.text));
  if (!state.options.showThinking) e.style.display = "none";
  step.bodyEl.appendChild(e);
  step.hasThinking = true;
  updateStepSummary(step);
  scrollTimeline();
}

function handleToolStarted(msg) {
  const step = state.currentStep ? state.steps.get(state.currentStep) : ensureStepContainer(1);
  state.toolCount++;
  $("info-tools").textContent = String(state.toolCount);

  const wrap = el("div", "tool-call");
  const icon = TOOL_ICONS[msg.tool] || "•";
  const human = TOOL_HUMAN[msg.tool] || "";
  wrap.innerHTML = `
    <div class="tool-call-icon" title="${escapeHtml(human)}">${icon}</div>
    <div class="tool-call-body">
      <div><span class="tool-call-name">${escapeHtml(msg.tool)}</span><span class="tool-call-args">(${formatArgs(msg.args)})</span></div>
      <div class="tool-call-result pending">выполняется…</div>
    </div>
  `;
  const resultEl = wrap.querySelector(".tool-call-result");
  step.bodyEl.appendChild(wrap);
  step.calls.push({ name: msg.tool, args: msg.args, resultEl });
  updateStepSummary(step);
  scrollTimeline();
}

function handleToolCompleted(msg) {
  const step = state.currentStep ? state.steps.get(state.currentStep) : null;
  if (!step) return;

  // find the last call for this tool name without a final result
  let call = null;
  for (let i = step.calls.length - 1; i >= 0; i--) {
    const c = step.calls[i];
    if (c.name === msg.tool && c.resultEl.classList.contains("pending")) {
      call = c;
      break;
    }
  }
  if (!call) return;

  call.resultEl.classList.remove("pending");
  call.resultEl.classList.toggle("ok", !msg.is_error);
  call.resultEl.classList.toggle("err", !!msg.is_error);

  const summary = String(msg.result_summary ?? "");
  const prefix = msg.is_error ? "✗ " : "✓ ";
  if (summary.length > RESULT_PREVIEW_LIMIT) {
    const short = summary.slice(0, RESULT_PREVIEW_LIMIT);
    call.resultEl.innerHTML = `${escapeHtml(prefix + short)}…<button class="show-more">показать целиком</button>`;
    const btn = call.resultEl.querySelector(".show-more");
    btn.addEventListener("click", () => {
      call.resultEl.textContent = prefix + summary;
    });
  } else {
    call.resultEl.textContent = prefix + summary;
  }
  scrollTimeline();
}

function handleScratchpad(msg) {
  const entries = msg.entries || {};
  const keys = Object.keys(entries);
  $("scratchpad-count").textContent = String(keys.length);
  const body = $("scratchpad-body");
  if (keys.length === 0) {
    body.innerHTML = `<div class="scratchpad-empty">пока пусто — агент сохранит сюда то, что нужно помнить между шагами</div>`;
    return;
  }
  body.innerHTML = "";
  keys.forEach((k) => {
    const entry = el("div", "scratchpad-entry");
    entry.innerHTML = `
      <span class="scratchpad-key">${escapeHtml(k)}</span>
      <span class="scratchpad-val">${escapeHtml(entries[k])}</span>
    `;
    body.appendChild(entry);
  });
}

function handleNeedsInput(msg) {
  showAskBar(msg.question);
}

function deactivateCurrentStep() {
  if (state.currentStep && state.steps.has(state.currentStep)) {
    const s = state.steps.get(state.currentStep);
    s.el.classList.remove("active");
    s.el.classList.add("completed");
  }
}

function handleTaskCompleted(msg) {
  setRunning(false);
  setStatus("done", "готово");
  hideAskBar();
  deactivateCurrentStep();
  const panel = el("div", "panel done");
  panel.innerHTML = `
    <div class="panel-title">✅ задача выполнена</div>
    <div class="panel-body">${escapeHtml(msg.report)}</div>
  `;
  $("timeline").appendChild(panel);
  scrollTimeline();
  if (state.options.notifyDone) playBeep();
}

function handleTaskFailed(msg) {
  setRunning(false);
  setStatus("failed", "ошибка");
  hideAskBar();
  deactivateCurrentStep();
  const panel = el("div", "panel failed");
  panel.innerHTML = `
    <div class="panel-title">⚠ задача не выполнена</div>
    <div class="panel-body">${escapeHtml(msg.reason)}</div>
  `;
  $("timeline").appendChild(panel);
  scrollTimeline();
  if (state.options.notifyDone) playBeep();
}

function handleError(msg) {
  const panel = el("div", "panel error");
  panel.innerHTML = `
    <div class="panel-title">⚠ ошибка</div>
    <div class="panel-body">${escapeHtml(msg.reason)}</div>
  `;
  $("timeline").hidden = false;
  $("welcome").hidden = true;
  $("timeline").appendChild(panel);
  scrollTimeline();
}

function handleEvent(msg) {
  switch (msg.type) {
    case "AgentStarted": return handleAgentStarted(msg);
    case "LLMRequestStarted": return handleLLMStarted(msg);
    case "LLMRequestCompleted": return handleLLMCompleted(msg);
    case "AgentThinking": return handleAgentThinking(msg);
    case "ToolCallStarted": return handleToolStarted(msg);
    case "ToolCallCompleted": return handleToolCompleted(msg);
    case "ScratchpadUpdated": return handleScratchpad(msg);
    case "NeedsUserInput": return handleNeedsInput(msg);
    case "TaskCompleted": return handleTaskCompleted(msg);
    case "TaskFailed": return handleTaskFailed(msg);
    case "Error": return handleError(msg);
    case "pong": return;
    default:
      console.warn("unknown event", msg);
  }
}

// ============================================================
// Ask bar
// ============================================================

function showAskBar(question) {
  $("ask-question").textContent = question;
  $("ask-bar").classList.remove("hidden");
  const inp = $("ask-input");
  inp.value = "";
  inp.focus();
  setStatus("waiting", "ждёт вашего ответа");
}

function hideAskBar() {
  $("ask-bar").classList.add("hidden");
}

// ============================================================
// Notifications
// ============================================================

function playBeep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain); gain.connect(ctx.destination);
    osc.type = "sine";
    osc.frequency.value = 720;
    gain.gain.value = 0.05;
    osc.start();
    setTimeout(() => { osc.stop(); ctx.close(); }, 180);
  } catch {}
}

// ============================================================
// WebSocket
// ============================================================

function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/ws`;
  const ws = new WebSocket(url);
  state.ws = ws;

  ws.onopen = () => {
    if (!state.running) setStatus("idle", "готов");
  };
  ws.onmessage = (ev) => {
    try {
      handleEvent(JSON.parse(ev.data));
    } catch (e) {
      console.error("bad event", ev.data, e);
    }
  };
  ws.onclose = () => {
    setStatus("failed", "разрыв соединения");
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

// ============================================================
// noVNC iframe
// ============================================================

async function bootNoVnc() {
  let url = "http://localhost:6080/vnc.html?autoconnect=1&resize=remote";
  try {
    const res = await fetch("/health");
    const data = await res.json();
    if (data.novnc_url) url = data.novnc_url;
  } catch {}
  $("info-novnc").textContent = url;

  const iframe = $("vnc-iframe");
  const loading = $("vnc-loading");
  iframe.addEventListener("load", () => {
    setTimeout(() => loading.classList.add("hidden"), 200);
  });
  iframe.src = url;
  $("novnc-link").href = url;
}

// ============================================================
// Wire up UI
// ============================================================

document.addEventListener("DOMContentLoaded", () => {
  loadOptions();
  bootNoVnc();
  connect();
  setStatus("idle", "готов");

  // --- composer ---
  $("run-btn").addEventListener("click", () => {
    const task = $("task-input").value.trim();
    if (!task) {
      $("task-input").focus();
      return;
    }
    // Чистим ленту сразу, чтобы ошибки до AgentStarted (например,
    // невалидный API-ключ при Settings.load) не накладывались на
    // содержимое прошлой задачи.
    resetForNewRun();
    setStatus("running", "запуск…");
    send({ type: "run", task });
  });

  $("cancel-btn").addEventListener("click", () => send({ type: "cancel" }));

  $("clear-btn").addEventListener("click", () => {
    if (state.running) return;
    clearTimeline();
    showWelcome();
    setStatus("idle", "готов");
    updateCost(0, false);
  });

  $("task-input").addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      $("run-btn").click();
    }
  });

  // --- example cards ---
  document.querySelectorAll(".example-card").forEach((card) => {
    card.addEventListener("click", () => {
      const t = card.dataset.task || "";
      $("task-input").value = t;
      $("task-input").focus();
      $("task-input").scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });

  // --- ask bar ---
  $("ask-submit").addEventListener("click", () => {
    const answer = $("ask-input").value;
    if (!answer.trim()) return;
    send({ type: "answer", answer });
    hideAskBar();
    setStatus("running", "выполняется");
  });
  $("ask-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      $("ask-submit").click();
    }
  });

  // --- theme toggle ---
  $("theme-btn").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("ba-theme", next);
    $("theme-btn").textContent = next === "dark" ? "☀" : "🌙";
  });

  // --- settings modal ---
  $("settings-btn").addEventListener("click", () => {
    $("settings-modal").classList.remove("hidden");
  });
  document.querySelectorAll('[data-close="settings"]').forEach((b) =>
    b.addEventListener("click", () => $("settings-modal").classList.add("hidden"))
  );
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      $("settings-modal").classList.add("hidden");
    }
  });

  $("opt-show-details").addEventListener("change", (e) => {
    state.options.showDetails = e.target.checked; saveOptions(); applyOptions();
  });
  $("opt-show-thinking").addEventListener("change", (e) => {
    state.options.showThinking = e.target.checked; saveOptions(); applyOptions();
  });
  $("opt-autoscroll").addEventListener("change", (e) => {
    state.options.autoscroll = e.target.checked; saveOptions();
  });
  $("opt-notify-done").addEventListener("change", (e) => {
    state.options.notifyDone = e.target.checked; saveOptions();
  });

  // --- scratchpad collapse ---
  $("scratchpad-toggle").addEventListener("click", () => {
    document.querySelector(".scratchpad").classList.toggle("collapsed");
  });

  // --- vnc reload ---
  $("vnc-reload").addEventListener("click", () => {
    const iframe = $("vnc-iframe");
    const src = iframe.src;
    $("vnc-loading").classList.remove("hidden");
    iframe.src = "about:blank";
    setTimeout(() => { iframe.src = src; }, 80);
  });
});
