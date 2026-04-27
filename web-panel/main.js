/* ================================================================
   CoreBot Control Panel — vanilla SPA
   - Хеш-роутинг (Dashboard / Accounts / Dialogs / Logs / Settings)
   - JWT в localStorage, перезапрос токена не реализован (простая login-форма)
   - SSE-канал для live-сообщений из corebot.db
   ================================================================ */

const API = window.location.origin.replace(/\/$/, "");
const TOKEN_KEY = "cb.access_token";
const USER_KEY = "cb.user";

const state = {
  token: localStorage.getItem(TOKEN_KEY) || "",
  user: safeJSON(localStorage.getItem(USER_KEY)) || null,
  route: "dashboard",
  routeParams: {},
  sse: null,
  sseStatus: "offline",
  cache: {
    accounts: null,
    accountById: new Map(),
  },
  current: {
    accountId: null,
    peerId: null,
    messageMaxId: 0,
  },
  dialogs: {
    accountSearch: "",
    leftTab: "accounts", // accounts | groups
    selectedGroupId: null, // null = all
    selectedGroupName: "Все",
    groupAccountIds: null, // Set<int> | null
  },
  accounts: {
    q: "",
    sort: "id_desc",
  },
  listeners: {},
};

function safeJSON(s) { try { return s ? JSON.parse(s) : null; } catch { return null; } }
function fmtDate(s) {
  if (!s) return "—";
  try {
    const d = (s instanceof Date) ? s : new Date(s);
    if (Number.isNaN(d.getTime())) return s;
    return d.toLocaleString("ru-RU", { hour12: false });
  } catch { return s; }
}
function fmtRelative(s) {
  if (!s) return "—";
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return s;
  const diff = Math.floor((Date.now() - d.getTime()) / 1000);
  if (diff < 60) return `${diff} с назад`;
  if (diff < 3600) return `${Math.floor(diff / 60)} мин назад`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} ч назад`;
  return d.toLocaleDateString("ru-RU");
}
function escapeHTML(s) {
  if (s === null || s === undefined) return "";
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
function $(sel, root = document) { return root.querySelector(sel); }
function $$(sel, root = document) { return Array.from(root.querySelectorAll(sel)); }
function isReadOnlyRole() { return (state.user?.role || "") === "tenant_viewer"; }

function toast(message, kind = "info", ttl = 3000) {
  const el = $("#toast");
  if (!el) return;
  el.textContent = message;
  el.className = "fixed bottom-4 right-4 z-50 max-w-md px-4 py-3 rounded-lg shadow-lg border text-sm show " + (
    kind === "error" ? "border-rose-700 bg-rose-900/80 text-rose-100" :
    kind === "success" ? "border-emerald-700 bg-emerald-900/80 text-emerald-100" :
    "border-ink-600 bg-ink-800 text-slate-100"
  );
  setTimeout(() => { el.classList.remove("show"); el.classList.add("hidden"); }, ttl);
}

/* ----------------------------- API helpers ----------------------------- */

async function api(path, { method = "GET", body, raw = false } = {}) {
  const url = path.startsWith("http") ? path : API + path;
  const headers = { "Accept": "application/json" };
  if (state.token) headers["Authorization"] = `Bearer ${state.token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const r = await fetch(url, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (r.status === 401) {
    handleLogout(true);
    throw new Error("Unauthorized");
  }
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try {
      const data = await r.json();
      if (data && data.detail) detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch {}
    throw new Error(detail);
  }
  if (raw || r.status === 204) return null;
  return r.json();
}

/* ----------------------------- Login flow ------------------------------ */

async function loginFlow(ev) {
  ev?.preventDefault();
  const username = $("#username").value.trim();
  const password = $("#password").value;
  const msg = $("#loginMsg");
  msg.classList.add("hidden");
  try {
    const r = await fetch(API + "/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (!r.ok) {
      msg.textContent = r.status === 401 ? "Неверный логин или пароль" : `Ошибка входа: HTTP ${r.status}`;
      msg.classList.remove("hidden");
      return;
    }
    const data = await r.json();
    state.token = data.access_token;
    try {
      const me = await fetchMeByToken(state.token);
      state.user = me || { username };
    } catch {
      state.user = { username };
    }
    localStorage.setItem(TOKEN_KEY, state.token);
    localStorage.setItem(USER_KEY, JSON.stringify(state.user));
    await enterApp();
  } catch (e) {
    msg.textContent = "Ошибка сети";
    msg.classList.remove("hidden");
  }
}

function handleLogout(silent = false) {
  state.token = "";
  state.user = null;
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
  closeSSE();
  const app = $("#appShell");
  const lg = $("#loginScreen");
  app.classList.add("hidden");
  app.style.display = "none";
  lg.classList.remove("hidden");
  lg.classList.add("flex");
  lg.style.display = "flex";
  if (!silent) toast("Сессия завершена");
}

async function enterApp() {
  const app = $("#appShell");
  const lg = $("#loginScreen");
  // Гасим экран логина и его inline style="display:flex" — иначе он
  // остаётся в потоке и виден при прокрутке над приложением.
  lg.classList.add("hidden");
  lg.classList.remove("flex");
  lg.style.display = "none";
  app.classList.remove("hidden");
  app.style.display = "flex";
  if (!state.user?.role) {
    try {
      const me = await fetchMeByToken(state.token);
      if (me) {
        state.user = me;
        localStorage.setItem(USER_KEY, JSON.stringify(state.user));
      }
    } catch {}
  }
  const roleSuffix = state.user?.role ? ` (${state.user.role})` : "";
  $("#userBadge").textContent = (state.user?.username || "—") + roleSuffix;
  openSSE();
  navigate(window.location.hash || "#/dashboard");
}

async function fetchMeByToken(token) {
  if (!token) return null;
  const r = await fetch(API + "/auth/me", {
    method: "GET",
    headers: {
      "Accept": "application/json",
      "Authorization": `Bearer ${token}`,
    },
  });
  if (!r.ok) return null;
  return r.json();
}

/* ------------------------------- SSE live ------------------------------ */

function openSSE() {
  closeSSE();
  if (!state.token) return;
  try {
    const url = `${API}/business/stream?token=${encodeURIComponent(state.token)}`;
    const es = new EventSource(url);
    es.addEventListener("hello", () => setSSE("online"));
    es.addEventListener("ping", () => setSSE("online"));
    es.addEventListener("message", (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      onLiveMessage(msg);
    });
    es.onerror = () => {
      setSSE("offline");
    };
    state.sse = es;
  } catch (e) {
    console.warn("SSE failed", e);
  }
}
function closeSSE() {
  try { state.sse?.close(); } catch {}
  state.sse = null;
  setSSE("offline");
}
function setSSE(s) {
  state.sseStatus = s;
  const el = $("#sseStatus");
  if (!el) return;
  el.innerHTML = s === "online"
    ? '<span class="live-dot"></span> live'
    : '<span class="text-slate-500">offline</span>';
}

function onLiveMessage(msg) {
  if (state.route === "dialogs" &&
      state.current.accountId === msg.account_id &&
      state.current.peerId === msg.peer_user_id) {
    // Если это assistant — он мог соответствовать строке очереди.
    // Перерисовываем диалог целиком, чтобы убрать placeholder из outbound_queue.
    if (msg.role === "assistant") {
      loadDialogMessages(msg.account_id, msg.peer_user_id);
    } else {
      appendMessageToChat(msg);
    }
  }
  if (state.route === "dashboard") {
    incLiveCounter();
    try { state.listeners.dashboardLive?.(msg); } catch {}
  }
  if (state.route === "accounts") {
    bumpAccountActivity(msg.account_id);
  }
}

/* ------------------------------- Router -------------------------------- */

function navigate(hash) {
  const path = (hash || "#/dashboard").replace(/^#/, "").split("?")[0];
  const segments = path.split("/").filter(Boolean);
  const route = segments[0] || "dashboard";
  state.route = route;
  state.routeParams = { segments };

  $$("#sideNav .nav-link").forEach(a => {
    a.classList.toggle("active", a.dataset.route === route);
  });
  const pageRoot = $("#pageRoot");
  if (pageRoot) {
    pageRoot.classList.toggle("dialogs-no-scroll", route === "dialogs");
  }

  switch (route) {
    case "dashboard": return renderDashboard();
    case "accounts":  return renderAccounts(segments[1]);
    case "dialogs":   return renderDialogs(segments[1], segments[2]);
    case "queue":     return renderQueue();
    case "mailings":  return renderMailings(segments[1]);
    case "clients":   return renderClients(segments[1]);
    case "groups":    return renderGroups(segments[1]);
    case "proxies":   return renderProxies(segments[1]);
    case "archive":   return renderArchive();
    case "logs":      return renderLogs();
    case "settings":  return renderSettings();
    default: return renderNotFound();
  }
}
/* ------------------------------ Queue view ----------------------------- */

async function renderQueue() {
  setHeader("Очередь", "Ручные исходящие из веба (outbound_queue)");
  const readOnly = isReadOnlyRole();
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full space-y-4">
      <div class="card">
        <div class="flex items-center gap-3 text-sm flex-wrap">
          <label class="text-slate-400">Статус:</label>
          <select id="qStatus" class="bg-ink-800 border border-ink-600 rounded-md px-2 py-1 text-slate-100">
            <option value="">все</option>
            <option value="pending">pending</option>
            <option value="sending">sending</option>
            <option value="sent">sent</option>
            <option value="failed">failed</option>
            <option value="cancelled">cancelled</option>
          </select>
          <input id="qAccount" type="number" min="1" placeholder="account_id"
                 class="w-32 bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100" />
          <input id="qPeer" type="number" min="1" placeholder="peer_user_id"
                 class="w-40 bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100" />
          <input id="qLimit" type="number" min="1" max="1000" value="200"
                 class="w-24 bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100" />
          <button id="qApply" class="px-3 py-1 rounded bg-accent-600 hover:bg-accent-500 text-white">Применить</button>
          <button id="qBulkRetry" class="px-3 py-1 rounded bg-emerald-700 hover:bg-emerald-600 text-white ${readOnly ? "opacity-50 cursor-not-allowed" : ""}" ${readOnly ? "disabled" : ""}>↻ Retry выбранных</button>
          <button id="qBulkCancel" class="px-3 py-1 rounded bg-rose-700 hover:bg-rose-600 text-white ${readOnly ? "opacity-50 cursor-not-allowed" : ""}" ${readOnly ? "disabled" : ""}>✕ Cancel выбранных</button>
          <span id="qCount" class="text-xs text-slate-500 ml-auto"></span>
        </div>
      </div>
      <div class="card p-0 overflow-hidden">
        <table class="cb-table">
          <thead>
            <tr>
              <th><input id="qSelectAll" type="checkbox" class="rounded border-ink-600 bg-ink-800" ${readOnly ? "disabled" : ""} /></th>
              <th>ID</th><th>Аккаунт</th><th>Peer</th><th>Текст</th><th>Статус</th><th>Attempts</th><th>Ошибка</th><th>Время</th><th></th>
            </tr>
          </thead>
          <tbody id="qBody"><tr><td colspan="10" class="text-center text-slate-500 py-8">Загрузка…</td></tr></tbody>
        </table>
      </div>
    </div>
  `;
  $("#qApply").addEventListener("click", loadQueueList);
  $("#qBulkRetry").addEventListener("click", () => runQueueBulk("retry"));
  $("#qBulkCancel").addEventListener("click", () => runQueueBulk("cancel"));
  $("#qSelectAll").addEventListener("change", (ev) => {
    $$("#qBody input[type='checkbox'][data-qid]").forEach((c) => { c.checked = !!ev.currentTarget.checked; });
  });
  await loadQueueList();
}

function queueStatusPill(status) {
  return _queueStatusBadge(status);
}

async function loadQueueList() {
  try {
    const params = new URLSearchParams();
    const st = ($("#qStatus")?.value || "").trim();
    const acc = ($("#qAccount")?.value || "").trim();
    const peer = ($("#qPeer")?.value || "").trim();
    const limit = ($("#qLimit")?.value || "200").trim();
    if (st) params.set("status", st);
    if (acc) params.set("account_id", acc);
    if (peer) params.set("peer_user_id", peer);
    params.set("limit", limit || "200");
    const list = await api(`/business/queue?${params.toString()}`);
    const tbody = $("#qBody");
    $("#qCount").textContent = `найдено: ${list.length}`;
    if (!list.length) {
      tbody.innerHTML = `<tr><td colspan="10" class="text-center text-slate-500 py-8">Очередь пуста по фильтру.</td></tr>`;
      return;
    }
    const readOnly = isReadOnlyRole();
    tbody.innerHTML = list.map((q) => `
      <tr>
        <td><input type="checkbox" data-qid="${q.queue_id}" class="rounded border-ink-600 bg-ink-800" ${readOnly ? "disabled" : ""} /></td>
        <td class="text-slate-500">#${q.queue_id}</td>
        <td><a href="#/dialogs/${q.account_id}/${q.peer_user_id}" class="text-slate-100 hover:text-accent-500">${escapeHTML(q.account_title)}</a></td>
        <td class="text-xs text-slate-300">${q.client_username ? '@' + escapeHTML(q.client_username) : q.peer_user_id}</td>
        <td class="max-w-[360px] truncate text-slate-200" title="${escapeHTML(q.text || "")}">${escapeHTML(q.text || "")}</td>
        <td>${queueStatusPill(q.status)}</td>
        <td class="text-slate-300">${q.attempts ?? 0}</td>
        <td class="max-w-[260px] truncate text-xs text-rose-300" title="${escapeHTML(q.error || "")}">${escapeHTML(q.error || "—")}</td>
        <td class="text-xs text-slate-400">${fmtDate(q.created_at)}</td>
        <td class="text-right whitespace-nowrap">
          <button data-q-act="retry" data-qid="${q.queue_id}" class="px-2 py-1 rounded bg-emerald-700 hover:bg-emerald-600 text-xs ${readOnly ? "opacity-50 cursor-not-allowed" : ""}" ${readOnly ? "disabled" : ""}>↻</button>
          <button data-q-act="cancel" data-qid="${q.queue_id}" class="px-2 py-1 rounded bg-rose-700 hover:bg-rose-600 text-xs ml-1 ${readOnly ? "opacity-50 cursor-not-allowed" : ""}" ${readOnly ? "disabled" : ""}>✕</button>
        </td>
      </tr>
    `).join("");
    if (!readOnly) {
      tbody.querySelectorAll("button[data-q-act]").forEach((b) => {
        b.addEventListener("click", () => onQueueItemAction(Number(b.dataset.qid), b.dataset.qAct));
      });
    }
  } catch (e) {
    toast(`Очередь: ${e.message}`, "error");
  }
}

async function onQueueItemAction(queueId, action) {
  if (!queueId || !action) return;
  if (isReadOnlyRole()) {
    toast("Роль read-only: действие запрещено", "error");
    return;
  }
  try {
    if (action === "retry") {
      await api(`/business/queue/${queueId}/retry`, { method: "POST" });
    } else if (action === "cancel") {
      await api(`/business/queue/${queueId}/cancel`, { method: "POST", raw: true });
    }
    await loadQueueList();
  } catch (e) {
    toast(`Очередь ${action}: ${e.message}`, "error");
  }
}

async function runQueueBulk(action) {
  if (isReadOnlyRole()) {
    toast("Роль read-only: массовые действия запрещены", "error");
    return;
  }
  const ids = $$("#qBody input[type='checkbox'][data-qid]:checked")
    .map((c) => Number(c.dataset.qid))
    .filter((n) => Number.isFinite(n) && n > 0);
  if (!ids.length) {
    toast("Отметьте элементы очереди", "info");
    return;
  }
  try {
    const r = await api("/business/queue/bulk", {
      method: "POST",
      body: { action, queue_ids: ids },
    });
    toast(`Bulk ${action}: updated=${r.updated}, skipped=${r.skipped}`, "success");
    await loadQueueList();
  } catch (e) {
    toast(`Bulk ${action}: ${e.message}`, "error");
  }
}


window.addEventListener("hashchange", () => navigate(window.location.hash));

/* --------------------------- Dashboard view ---------------------------- */

let liveCounter = 0;
function incLiveCounter() {
  liveCounter += 1;
  const el = $("#dashLive");
  if (el) el.textContent = String(liveCounter);
}

async function renderDashboard() {
  setHeader("Дашборд", "Бизнес-метрики бота: рассылки, клиенты, классы, активность");
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full space-y-6">
      <!-- KPI ряд 1: Аккаунты + диалоги -->
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
        ${kpi("Аккаунты", "kpiAccTotal", "—", "всего")}
        ${kpi("AI активны", "kpiAccAI", "—", "auto-режим", "text-emerald-300")}
        ${kpi("MANUAL", "kpiAccManual", "—", "оператор отвечает", "text-amber-300")}
        ${kpi("Авторизованы", "kpiAccAuth", "—", "Telegram OK")}
      </div>
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
        ${kpi("Диалоги всего", "kpiDialogs", "—", "уникальных пар (acc, peer)")}
        ${kpi("Диалоги 24ч", "kpiDialogs24", "—", "активных за сутки")}
        ${kpi("Клиенты", "kpiClients", "—", "записей в БД")}
        ${kpi("Live-стрим", "dashLive", String(liveCounter), "событий с входа")}
      </div>

      <!-- Сообщения -->
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
        ${kpi("Входящие 24ч", "kpiMsgIn", "—", "от клиентов", "text-sky-300")}
        ${kpi("Исходящие AI 24ч", "kpiMsgOut", "—", "ответил нейрочат", "text-violet-300")}
        ${kpi("Ручные 24ч", "kpiManualSent", "—", "из веб-панели")}
        ${kpi("Очередь pending/failed", "kpiManualQueue", "—", "ждут отправки / не доставлены", "text-amber-300")}
      </div>

      <!-- Рассылки KPI -->
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
        ${kpi("Рассылки RUNNING", "kpiMailRun", "—", "идут сейчас", "text-emerald-300")}
        ${kpi("Рассылки PAUSED", "kpiMailPause", "—", "ждут продолжения", "text-amber-300")}
        ${kpi("Рассылки 24ч завершено", "kpiMailDone", "—", "")}
        ${kpi("Сообщ. рассылок 24ч", "kpiMailSent", "—", "успех / ошибки см. ниже")}
      </div>

      <!-- Графики и активные рассылки -->
      <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div class="card lg:col-span-2">
          <div class="flex items-center justify-between mb-3">
            <h3 class="font-semibold">Активность за 24 часа</h3>
            <div class="text-xs text-slate-400 flex items-center gap-3">
              <span class="flex items-center gap-1"><span class="ts-dot ts-in"></span> входящие</span>
              <span class="flex items-center gap-1"><span class="ts-dot ts-out"></span> AI ответ</span>
              <span class="flex items-center gap-1"><span class="ts-dot ts-mn"></span> ручные</span>
            </div>
          </div>
          <div id="dashTimeseries" class="ts-chart">…</div>
        </div>
        <div class="card">
          <h3 class="font-semibold mb-3">Активные рассылки</h3>
          <div id="dashMailings" class="space-y-2 text-sm text-slate-400">…</div>
        </div>
      </div>

      <!-- Классы клиентов и топ-аккаунты -->
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div class="card">
          <h3 class="font-semibold mb-3">Распределение классов клиентов</h3>
          <div id="dashClasses" class="space-y-2 text-sm">…</div>
        </div>
        <div class="card">
          <h3 class="font-semibold mb-3">Топ-аккаунты по активности (24ч)</h3>
          <table class="cb-table">
            <thead><tr><th>Аккаунт</th><th>Режим</th><th class="text-right">In</th><th class="text-right">Out</th></tr></thead>
            <tbody id="dashTopAccounts"><tr><td colspan="4" class="text-slate-500 text-center py-4">…</td></tr></tbody>
          </table>
        </div>
      </div>

      <!-- Recent live + последние сообщения -->
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div class="card">
          <div class="flex items-center justify-between mb-3">
            <h3 class="font-semibold">Live-поток сообщений</h3>
            <span class="text-xs text-slate-400">после входа в панель</span>
          </div>
          <div id="dashRecent" class="space-y-2 text-sm text-slate-400">Подождите событий…</div>
        </div>
        <div class="card">
          <h3 class="font-semibold mb-3">Последние сообщения (БД)</h3>
          <div id="dashRecentDb" class="space-y-2 text-sm text-slate-400">…</div>
        </div>
      </div>
    </div>
  `;

  await Promise.all([
    loadDashSummary(),
    loadDashTimeseries(),
    loadDashClasses(),
    loadDashTopAccounts(),
    loadDashMailings(),
    loadDashRecentDb(),
  ]);

  // Live-feed
  const recentEl = $("#dashRecent");
  const recent = [];
  state.listeners.dashboardLive = (msg) => {
    recent.unshift(msg);
    if (recent.length > 30) recent.length = 30;
    recentEl.innerHTML = recent.map(m => `
      <div class="flex items-start gap-2">
        <span class="pill ${m.role === 'assistant' ? 'pill-blue' : 'pill-gray'}">${escapeHTML(m.role)}</span>
        <div class="min-w-0 flex-1">
          <div class="truncate text-slate-200">${escapeHTML(m.content || "")}</div>
          <div class="text-[11px] text-slate-500">acc#${m.account_id} ↔ ${m.peer_user_id} · ${fmtRelative(m.created_at)}</div>
        </div>
      </div>
    `).join("");
  };
}

function kpi(label, id, val, sub = "", colorClass = "text-white") {
  return `
    <div class="card">
      <div class="text-[11px] uppercase tracking-wide text-slate-400">${escapeHTML(label)}</div>
      <div id="${id}" class="text-2xl font-semibold mt-1 ${colorClass}">${val}</div>
      ${sub ? `<div class="text-[11px] text-slate-500 mt-1">${escapeHTML(sub)}</div>` : ""}
    </div>
  `;
}

async function loadDashSummary() {
  try {
    const s = await api("/business/dashboard/summary");
    $("#kpiAccTotal").textContent = s.accounts_total;
    $("#kpiAccAI").textContent = s.accounts_ai;
    $("#kpiAccManual").textContent = s.accounts_manual;
    $("#kpiAccAuth").textContent = s.accounts_authorized;
    $("#kpiDialogs").textContent = s.dialogs_total;
    $("#kpiDialogs24").textContent = s.dialogs_24h;
    $("#kpiClients").textContent = s.clients_total;
    $("#kpiMsgIn").textContent = s.messages_in_24h;
    $("#kpiMsgOut").textContent = s.messages_out_24h;
    $("#kpiManualSent").textContent = s.manual_sent_24h;
    $("#kpiManualQueue").textContent = `${s.manual_pending} / ${s.manual_failed}`;
    $("#kpiMailRun").textContent = s.mailings_running;
    $("#kpiMailPause").textContent = s.mailings_paused;
    $("#kpiMailDone").textContent = s.mailings_completed_24h;
    $("#kpiMailSent").textContent = `${s.mailing_sent_24h} ✓ / ${s.mailing_failed_24h} ✕`;
  } catch (e) { toast(`summary: ${e.message}`, "error"); }
}

async function loadDashTimeseries() {
  const el = $("#dashTimeseries");
  if (!el) return;
  try {
    const points = await api("/business/dashboard/timeseries?hours=24");
    if (!points.length) { el.textContent = "Нет данных за период."; return; }
    const max = points.reduce((m, p) =>
      Math.max(m, (p.messages_in || 0) + (p.messages_out || 0) + (p.manual_sent || 0)), 1);
    const cols = points.map(p => {
      const total = (p.messages_in || 0) + (p.messages_out || 0) + (p.manual_sent || 0);
      const h = Math.max(2, Math.round((total / max) * 100));
      const inH  = Math.round(((p.messages_in || 0) / Math.max(1, total)) * h);
      const outH = Math.round(((p.messages_out || 0) / Math.max(1, total)) * h);
      const mnH  = h - inH - outH;
      const tip = `${p.ts}\n← ${p.messages_in} · → ${p.messages_out} · ✋ ${p.manual_sent}`;
      const hourLabel = (p.ts || "").slice(11, 13);
      return `
        <div class="ts-col" title="${escapeHTML(tip)}">
          <div class="ts-bar-stack" style="height:${h}%">
            <div class="ts-bar ts-mn"  style="height:${mnH}%"></div>
            <div class="ts-bar ts-out" style="height:${outH}%"></div>
            <div class="ts-bar ts-in"  style="height:${inH}%"></div>
          </div>
          <div class="ts-x">${escapeHTML(hourLabel)}</div>
        </div>`;
    }).join("");
    el.innerHTML = cols;
  } catch (e) { el.textContent = `Ошибка: ${e.message}`; }
}

async function loadDashClasses() {
  const el = $("#dashClasses");
  if (!el) return;
  try {
    const items = await api("/business/dashboard/class_distribution");
    const max = items.reduce((m, it) => Math.max(m, it.clients || 0), 1);
    el.innerHTML = items.map(it => {
      const w = Math.round(((it.clients || 0) / max) * 100);
      return `
        <div>
          <div class="flex items-center justify-between text-xs">
            <span class="text-slate-300">${escapeHTML(it.class_key)}</span>
            <span class="text-slate-500">клиентов: <b class="text-slate-200">${it.clients}</b> · событий: ${it.events}</span>
          </div>
          <div class="cb-bar mt-1"><div class="cb-bar-fill cls-${escapeHTML(it.class_key)}" style="width:${w}%"></div></div>
        </div>
      `;
    }).join("") || `<div class="text-slate-500">Нет данных по классам.</div>`;
  } catch (e) { el.innerHTML = `<div class="text-rose-400">${escapeHTML(e.message)}</div>`; }
}

async function loadDashTopAccounts() {
  const el = $("#dashTopAccounts");
  if (!el) return;
  try {
    const list = await api("/business/dashboard/top_accounts?hours=24&limit=10");
    if (!list.length) { el.innerHTML = `<tr><td colspan="4" class="text-slate-500 text-center py-3">Нет активности.</td></tr>`; return; }
    el.innerHTML = list.map(a => `
      <tr>
        <td><a href="#/dialogs/${a.account_id}" class="text-slate-200 hover:text-accent-500">${escapeHTML(a.title)}</a></td>
        <td>${a.ai_mode === "MANUAL"
          ? '<span class="pill pill-amber">MANUAL</span>'
          : '<span class="pill pill-green">AI</span>'}</td>
        <td class="text-right text-sky-300">${a.messages_in}</td>
        <td class="text-right text-violet-300">${a.messages_out}</td>
      </tr>
    `).join("");
  } catch (e) { el.innerHTML = `<tr><td colspan="4" class="text-rose-400 text-center py-3">${escapeHTML(e.message)}</td></tr>`; }
}

async function loadDashMailings() {
  const el = $("#dashMailings");
  if (!el) return;
  try {
    const list = await api("/business/dashboard/mailings_active");
    if (!list.length) { el.innerHTML = `<div class="text-slate-500">Нет активных рассылок.</div>`; return; }
    el.innerHTML = list.map(m => {
      const pct = m.total ? Math.round((m.sent / m.total) * 100) : 0;
      const pill = m.status === "running"
        ? '<span class="pill pill-green">RUNNING</span>'
        : '<span class="pill pill-amber">PAUSED</span>';
      return `
        <a href="#/mailings/${m.id}" class="block hover:bg-ink-800/40 -mx-2 px-2 py-2 rounded">
          <div class="flex items-center justify-between gap-2">
            <div class="text-slate-200 truncate">${escapeHTML(m.name)}</div>
            ${pill}
          </div>
          <div class="text-[11px] text-slate-500 mt-1">отправлено ${m.sent}/${m.total} · ошибок ${m.failed}</div>
          <div class="cb-bar mt-1"><div class="cb-bar-fill" style="width:${pct}%"></div></div>
        </a>`;
    }).join("");
  } catch (e) { el.innerHTML = `<div class="text-rose-400">${escapeHTML(e.message)}</div>`; }
}

async function loadDashRecentDb() {
  const el = $("#dashRecentDb");
  if (!el) return;
  try {
    const list = await api("/business/dashboard/recent_messages?limit=20");
    if (!list.length) { el.innerHTML = `<div class="text-slate-500">Сообщений нет.</div>`; return; }
    el.innerHTML = list.map(m => `
      <div class="flex items-start gap-2">
        <span class="pill ${m.role === 'assistant' ? 'pill-blue' : 'pill-gray'}">${escapeHTML(m.role)}</span>
        <div class="min-w-0 flex-1">
          <div class="truncate text-slate-200">${escapeHTML(m.content || "")}</div>
          <div class="text-[11px] text-slate-500"><a href="#/dialogs/${m.account_id}/${m.peer_user_id}" class="hover:text-accent-500">${escapeHTML(m.account_title)} ↔ ${escapeHTML(m.peer_title)}</a> · ${fmtRelative(m.created_at)}</div>
        </div>
      </div>`).join("");
  } catch (e) { el.innerHTML = `<div class="text-rose-400">${escapeHTML(e.message)}</div>`; }
}

/* ---------------------------- Accounts view ---------------------------- */

async function renderAccounts(accountIdStr) {
  const accountId = accountIdStr ? Number(accountIdStr) : null;
  setHeader(
    "Аккаунты",
    accountId
      ? `Редактор #${accountId}`
      : "Состояние аккаунтов и режим автоответа (AI / Manual)",
  );
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full">
      <div class="card mb-4">
        <div class="flex items-center justify-between mb-3">
          <h3 class="font-semibold text-slate-100">Новый аккаунт (быстрое создание)</h3>
          <span class="text-xs text-slate-500">Tdata/.session всё ещё импортируются в Telegram-боте</span>
        </div>
        <form id="accountCreateForm" class="grid grid-cols-1 md:grid-cols-4 gap-3 text-sm">
          <label class="block">
            <span class="text-slate-400 text-xs">Телефон *</span>
            <input name="phone" required placeholder="+79990001122"
                   class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">Название в боте (list_label)</span>
            <input name="list_label" placeholder="sales-01"
                   class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">Session name (опц.)</span>
            <input name="session_name" placeholder="web_7999..."
                   class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 font-mono" />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">Username (опц.)</span>
            <input name="username" placeholder="username"
                   class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">Имя</span>
            <input name="first_name"
                   class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">Фамилия</span>
            <input name="last_name"
                   class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">Membership</span>
            <select name="membership" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
              <option value="TEST">TEST</option>
              <option value="READY">READY</option>
              <option value="WARMUP">WARMUP</option>
            </select>
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">Режим</span>
            <select name="ai_mode" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
              <option value="MANUAL">MANUAL</option>
              <option value="AI_ACTIVE">AI_ACTIVE</option>
            </select>
          </label>
          <div class="md:col-span-4 flex items-center gap-3">
            <button type="submit" class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white">Создать аккаунт</button>
            <span id="accCreateMsg" class="text-xs text-slate-400"></span>
          </div>
        </form>
      </div>
      <div class="card overflow-hidden p-0">
        <div class="px-4 py-3 border-b border-ink-700 flex items-center gap-3 text-sm flex-wrap">
          <input id="accSearch" placeholder="Поиск: list_label / @username / phone / #id"
                 value="${escapeHTML(state.accounts?.q || "")}"
                 class="px-3 py-1.5 rounded bg-ink-800 border border-ink-600 text-slate-100 w-80 max-w-full" />
          <select id="accSort" class="bg-ink-800 border border-ink-600 rounded px-2 py-1.5 text-slate-100">
            <option value="id_desc" ${(state.accounts?.sort || "id_desc") === "id_desc" ? "selected" : ""}>Сначала новые (#id ↓)</option>
            <option value="id_asc" ${(state.accounts?.sort || "id_desc") === "id_asc" ? "selected" : ""}>Сначала старые (#id ↑)</option>
            <option value="dialogs_desc" ${(state.accounts?.sort || "id_desc") === "dialogs_desc" ? "selected" : ""}>По диалогам (больше → меньше)</option>
            <option value="last_dialog_desc" ${(state.accounts?.sort || "id_desc") === "last_dialog_desc" ? "selected" : ""}>Как в мессенджере (последний диалог)</option>
            <option value="label_asc" ${(state.accounts?.sort || "id_desc") === "label_asc" ? "selected" : ""}>По названию (A→Я)</option>
          </select>
          <span id="accCount" class="text-slate-500 text-xs ml-auto"></span>
        </div>
        <table class="cb-table">
          <thead>
            <tr>
              <th>ID</th><th>Аккаунт</th><th>Статус</th><th>Membership</th>
              <th>Режим AI</th><th class="text-right">Диалоги</th><th class="text-right">В очереди</th>
              <th>Последняя активность</th><th></th>
            </tr>
          </thead>
          <tbody id="accountsBody">
            <tr><td colspan="9" class="text-center text-slate-500 py-8">Загрузка…</td></tr>
          </tbody>
        </table>
      </div>
      <div id="accountEditorWrap" class="${accountId ? '' : 'hidden'} mt-4 card">
        <div id="accountEditor">${accountId ? "Загрузка…" : ""}</div>
      </div>
    </div>
  `;
  $("#accountCreateForm")?.addEventListener("submit", onCreateAccountSubmit);
  $("#accSearch")?.addEventListener("input", (ev) => {
    state.accounts.q = (ev.currentTarget.value || "").toString();
    refreshAccountsTable();
  });
  $("#accSort")?.addEventListener("change", (ev) => {
    state.accounts.sort = (ev.currentTarget.value || "id_desc").toString();
    refreshAccountsTable();
  });
  await refreshAccountsTable();
  if (accountId) await loadAccountEditor(accountId);
}

async function onCreateAccountSubmit(ev) {
  ev.preventDefault();
  const fd = new FormData(ev.currentTarget);
  const body = {
    phone: (fd.get("phone") || "").toString().trim(),
    list_label: (fd.get("list_label") || "").toString().trim(),
    session_name: (fd.get("session_name") || "").toString().trim(),
    username: (fd.get("username") || "").toString().trim(),
    first_name: (fd.get("first_name") || "").toString().trim(),
    last_name: (fd.get("last_name") || "").toString().trim(),
    membership: (fd.get("membership") || "TEST").toString(),
    ai_mode: (fd.get("ai_mode") || "MANUAL").toString(),
    status: "inactive",
  };
  const out = $("#accCreateMsg");
  out.textContent = "…";
  try {
    const created = await api("/business/accounts", { method: "POST", body });
    toast(`Аккаунт #${created.id} создан`, "success");
    out.textContent = `ok (#${created.id})`;
    out.className = "text-xs text-emerald-300";
    ev.currentTarget.reset();
    await refreshAccountsTable();
    window.location.hash = `#/accounts/${created.id}`;
  } catch (e) {
    out.textContent = e.message;
    out.className = "text-xs text-rose-400";
  }
}

async function refreshAccountsTable() {
  try {
    const list = await api("/business/accounts");
    state.cache.accounts = list;
    state.cache.accountById = new Map(list.map(a => [a.id, a]));
    const q = (state.accounts?.q || "").trim().toLowerCase();
    const sortMode = (state.accounts?.sort || "id_desc").toLowerCase();
    let rows = list.filter((a) => {
      if (!q) return true;
      const hay = [
        a.list_label || "",
        a.username || "",
        a.phone || "",
        `#${a.id}`,
      ].join(" ").toLowerCase();
      return hay.includes(q);
    });
    const byDateDesc = (x, y) => {
      const xt = x ? Date.parse(x) : NaN;
      const yt = y ? Date.parse(y) : NaN;
      if (Number.isNaN(xt) && Number.isNaN(yt)) return 0;
      if (Number.isNaN(xt)) return 1;
      if (Number.isNaN(yt)) return -1;
      return yt - xt;
    };
    rows.sort((a, b) => {
      if (sortMode === "id_asc") return a.id - b.id;
      if (sortMode === "dialogs_desc") return (b.dialogs_count || 0) - (a.dialogs_count || 0);
      if (sortMode === "last_dialog_desc") {
        const cmp = byDateDesc(a.last_dialog_at, b.last_dialog_at);
        return cmp || (b.id - a.id);
      }
      if (sortMode === "label_asc") {
        const at = (a.list_label || a.username || a.phone || `#${a.id}`).toLowerCase();
        const bt = (b.list_label || b.username || b.phone || `#${b.id}`).toLowerCase();
        return at.localeCompare(bt, "ru");
      }
      return b.id - a.id;
    });
    const accCount = $("#accCount");
    if (accCount) accCount.textContent = `показано: ${rows.length} / ${list.length}`;

    const tbody = $("#accountsBody");
    if (!tbody) return;
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="text-center text-slate-500 py-8">Нет аккаунтов в БД.</td></tr>`;
      return;
    }
    tbody.innerHTML = rows.map(a => {
      const title = escapeHTML(a.list_label || a.username || a.phone || `#${a.id}`);
      const fullName = [a.first_name, a.last_name].filter(Boolean).join(" ");
      const statusPill = accountStatusPill(a.status);
      const modePill = a.ai_mode === "MANUAL"
        ? `<span class="pill pill-amber">MANUAL</span>`
        : `<span class="pill pill-green">AI_ACTIVE</span>`;
      const toggleLabel = a.ai_mode === "MANUAL" ? "→ AI_ACTIVE" : "→ MANUAL";
      return `
        <tr data-account-id="${a.id}">
          <td class="text-slate-500">#${a.id}</td>
          <td>
            <div class="font-medium text-slate-100">${title}</div>
            <div class="text-xs text-slate-500">${escapeHTML(fullName) || "—"} · ${escapeHTML(a.username || "")}${a.phone ? ' · ' + escapeHTML(a.phone) : ''}</div>
          </td>
          <td>${statusPill}</td>
          <td><span class="pill pill-gray">${escapeHTML(a.membership || "—")}</span></td>
          <td>${modePill}</td>
          <td class="text-right text-slate-300">${a.dialogs_count}</td>
          <td class="text-right ${a.pending_outbound > 0 ? 'text-amber-400 font-medium' : 'text-slate-300'}">${a.pending_outbound}</td>
          <td class="text-slate-400 text-xs">${fmtRelative(a.last_activity)}</td>
          <td class="text-right whitespace-nowrap">
            <button data-act="goto-dialogs" class="px-2 py-1 rounded bg-ink-700 hover:bg-ink-600 text-xs mr-1">💬 Диалоги</button>
            <button data-act="edit" class="px-2 py-1 rounded bg-ink-700 hover:bg-ink-600 text-xs mr-1">✎ Изменить</button>
            <button data-act="toggle-mode" class="px-2 py-1 rounded ${a.ai_mode === 'MANUAL' ? 'bg-emerald-700 hover:bg-emerald-600' : 'bg-amber-700 hover:bg-amber-600'} text-xs">${toggleLabel}</button>
          </td>
        </tr>
      `;
    }).join("");

    tbody.querySelectorAll("tr[data-account-id]").forEach(tr => {
      const accountId = Number(tr.dataset.accountId);
      tr.querySelector('[data-act="goto-dialogs"]').addEventListener("click", () => {
        window.location.hash = `#/dialogs/${accountId}`;
      });
      tr.querySelector('[data-act="edit"]').addEventListener("click", () => {
        window.location.hash = `#/accounts/${accountId}`;
      });
      tr.querySelector('[data-act="toggle-mode"]').addEventListener("click", async (ev) => {
        const btn = ev.currentTarget;
        btn.disabled = true; btn.textContent = "…";
        try {
          const acc = state.cache.accountById.get(accountId);
          const next = acc.ai_mode === "MANUAL" ? "AI_ACTIVE" : "MANUAL";
          await api(`/business/accounts/${accountId}/mode`, { method: "POST", body: { mode: next } });
          toast(`Аккаунт #${accountId} → ${next}`, "success");
          await refreshAccountsTable();
        } catch (e) {
          toast(`Не удалось переключить: ${e.message}`, "error");
          btn.disabled = false;
        }
      });
    });
  } catch (e) {
    toast(`Ошибка загрузки аккаунтов: ${e.message}`, "error");
  }
}

function accountStatusPill(s) {
  if (!s) return `<span class="pill pill-gray">—</span>`;
  const map = {
    active: "pill-green",
    inactive: "pill-gray",
    flood_wait: "pill-amber",
    banned: "pill-red",
    error: "pill-red",
    spam_blocked: "pill-red",
  };
  return `<span class="pill ${map[s] || "pill-gray"}">${escapeHTML(s)}</span>`;
}

function bumpAccountActivity(accountId) {
  const tr = document.querySelector(`tr[data-account-id="${accountId}"]`);
  if (!tr) return;
  tr.classList.add("ring-1", "ring-accent-500", "transition");
  setTimeout(() => tr.classList.remove("ring-1", "ring-accent-500"), 1200);
}

async function loadAccountEditor(accountId) {
  const wrap = $("#accountEditorWrap");
  const el = $("#accountEditor");
  if (!wrap || !el) return;
  wrap.classList.remove("hidden");
  el.innerHTML = `<div class="text-slate-500 text-sm">Загрузка…</div>`;
  try {
    const [acc, groups, proxies] = await Promise.all([
      api(`/business/accounts/${accountId}`),
      api(`/business/groups`),
      api(`/business/proxies`),
    ]);
    const groupsCheckboxes = (groups || []).map(g => `
      <label class="flex items-center gap-2 text-sm">
        <input type="checkbox" name="group_${g.id}" ${(acc.group_ids || []).includes(g.id) ? "checked" : ""}
               class="rounded border-ink-600 bg-ink-800" />
        <span>${escapeHTML(g.name)}</span>
        <span class="text-xs text-slate-500">(${g.accounts_count})</span>
      </label>
    `).join("") || `<span class="text-slate-500 text-xs">Групп ещё нет — создайте в разделе «Группы».</span>`;
    const proxyOptions = [
      `<option value="0" ${!acc.proxy_id ? "selected" : ""}>— без прокси —</option>`,
      ...(proxies || []).map(p => `
        <option value="${p.id}" ${acc.proxy_id === p.id ? "selected" : ""}>
          ${escapeHTML(p.name)} (${escapeHTML(p.host)}:${p.port}) ${p.is_working ? "✓" : "✗"}
        </option>
      `),
    ].join("");
    el.innerHTML = `
      <div class="flex items-center justify-between mb-4 gap-3">
        <div>
          <h3 class="font-semibold text-white">Редактирование #${acc.id} — ${escapeHTML(acc.list_label || acc.username || acc.phone || "")}</h3>
          <p class="text-xs text-slate-500 mt-1">phone: ${escapeHTML(acc.phone || "—")} · username: ${escapeHTML(acc.username || "—")} · отправлено: ${acc.messages_sent} / today ${acc.messages_today}</p>
        </div>
        <div class="flex gap-2">
          <button id="accCloseBtn" class="px-3 py-2 rounded-md bg-ink-700 hover:bg-ink-600 text-sm">Закрыть</button>
          <button id="accDeleteBtn" class="px-3 py-2 rounded-md bg-rose-700 hover:bg-rose-600 text-white text-sm">🗑 Удалить</button>
        </div>
      </div>
      <form id="accountEditForm" class="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
        <label class="block">
          <span class="text-slate-400 text-xs">Подпись в списке (list_label)</span>
          <input name="list_label" value="${escapeHTML(acc.list_label || "")}"
                 class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
        </label>
        <label class="block">
          <span class="text-slate-400 text-xs">Membership</span>
          <select name="membership" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
            ${["READY","WARMUP","TEST"].map(v => `<option value="${v}" ${acc.membership === v ? "selected" : ""}>${v}</option>`).join("")}
          </select>
        </label>
        <label class="block">
          <span class="text-slate-400 text-xs">Имя (Telegram first_name)</span>
          <input name="first_name" value="${escapeHTML(acc.first_name || "")}"
                 class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
        </label>
        <label class="block">
          <span class="text-slate-400 text-xs">Фамилия (last_name)</span>
          <input name="last_name" value="${escapeHTML(acc.last_name || "")}"
                 class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
        </label>
        <label class="block">
          <span class="text-slate-400 text-xs">Bio</span>
          <textarea name="bio" rows="2" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">${escapeHTML(acc.bio || "")}</textarea>
        </label>
        <label class="block">
          <span class="text-slate-400 text-xs">Теги (CSV, например ,USA,Main,)</span>
          <input name="tags" value="${escapeHTML(acc.tags || "")}"
                 class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
        </label>
        <label class="block">
          <span class="text-slate-400 text-xs">Дневной лимит сообщений</span>
          <input name="daily_limit" type="number" min="0" max="10000" value="${acc.daily_limit}"
                 class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
        </label>
        <label class="block">
          <span class="text-slate-400 text-xs">Статус</span>
          <select name="status" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
            ${["active","inactive","banned","flood_wait","error","spam_blocked"].map(v => `<option value="${v}" ${acc.status === v ? "selected" : ""}>${v}</option>`).join("")}
          </select>
        </label>
        <label class="flex items-center gap-2 mt-6 text-slate-300">
          <input name="warmup_enabled" type="checkbox" ${acc.warmup_enabled ? "checked" : ""} class="rounded border-ink-600 bg-ink-800" />
          Прогрев включён
        </label>
        <label class="block">
          <span class="text-slate-400 text-xs">Профиль прогрева</span>
          <input name="warmup_profile" value="${escapeHTML(acc.warmup_profile || "safe")}"
                 class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
        </label>
        <label class="block col-span-full">
          <span class="text-slate-400 text-xs">Прокси</span>
          <select name="proxy_id" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
            ${proxyOptions}
          </select>
        </label>
        <div class="col-span-full">
          <span class="text-slate-400 text-xs">Группы</span>
          <div class="mt-2 grid grid-cols-2 md:grid-cols-3 gap-2">${groupsCheckboxes}</div>
        </div>
        <div class="col-span-full flex items-center gap-3 mt-2">
          <button type="submit" class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white">Сохранить</button>
          <a href="#/dialogs/${acc.id}" class="text-sm text-slate-400 hover:text-slate-200">→ Открыть диалоги</a>
          <span id="accSaveMsg" class="text-xs text-slate-400"></span>
        </div>
      </form>
    `;

    $("#accCloseBtn").addEventListener("click", () => {
      window.location.hash = "#/accounts";
    });

    $("#accDeleteBtn").addEventListener("click", async () => {
      if (!confirm(`Удалить аккаунт #${acc.id} полностью? Это удалит все его диалоги, сообщения и записи.`)) return;
      try {
        await api(`/business/accounts/${acc.id}`, { method: "DELETE" });
        toast(`Аккаунт #${acc.id} удалён`, "success");
        window.location.hash = "#/accounts";
      } catch (e) {
        toast(`Ошибка удаления: ${e.message}`, "error");
      }
    });

    $("#accountEditForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const fd = new FormData(ev.currentTarget);
      const body = {
        list_label: (fd.get("list_label") || "").toString(),
        first_name: (fd.get("first_name") || "").toString(),
        last_name: (fd.get("last_name") || "").toString(),
        bio: (fd.get("bio") || "").toString(),
        tags: (fd.get("tags") || "").toString(),
        membership: (fd.get("membership") || "READY").toString(),
        status: (fd.get("status") || "active").toString(),
        daily_limit: Number(fd.get("daily_limit") || 0),
        warmup_enabled: !!fd.get("warmup_enabled"),
        warmup_profile: (fd.get("warmup_profile") || "").toString(),
        proxy_id: Number(fd.get("proxy_id") || 0),
      };
      const groupIds = [];
      ev.currentTarget.querySelectorAll('input[type="checkbox"][name^="group_"]').forEach(c => {
        if (c.checked) groupIds.push(Number(c.name.slice(6)));
      });
      body.group_ids = groupIds;

      const out = $("#accSaveMsg");
      out.textContent = "…";
      try {
        await api(`/business/accounts/${acc.id}`, { method: "PATCH", body });
        toast("Сохранено", "success");
        out.textContent = "ok";
        out.className = "text-xs text-emerald-300";
        await refreshAccountsTable();
        await loadAccountEditor(acc.id);
      } catch (e) {
        out.textContent = e.message;
        out.className = "text-xs text-rose-400";
      }
    });
  } catch (e) {
    el.innerHTML = `<div class="text-rose-400">${escapeHTML(e.message)}</div>`;
  }
}

/* ----------------------------- Dialogs view ---------------------------- */

async function renderDialogs(accountIdStr, peerStr) {
  setHeader("Диалоги", "Live-просмотр переписок и ручные ответы");
  const accountId = accountIdStr ? Number(accountIdStr) : null;
  const peerId = peerStr ? Number(peerStr) : null;
  state.current.accountId = accountId;
  state.current.peerId = peerId;
  state.current.messageMaxId = 0;

  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="grid grid-cols-12 h-full min-h-0 overflow-hidden">
      <aside class="col-span-3 min-w-0 min-h-0 border-r border-ink-700 flex flex-col overflow-hidden">
        <div class="px-4 py-3 border-b border-ink-700 flex items-center justify-between gap-2">
          <h3 class="font-medium text-slate-200 text-sm">Диалоги</h3>
          <button id="dlgAccountsRefresh" class="text-xs text-slate-400 hover:text-slate-200">⟳</button>
        </div>
        <div class="px-3 py-2 border-b border-ink-700 flex items-center gap-2">
          <button id="dlgTabAccounts" class="px-2 py-1 rounded text-xs ${state.dialogs.leftTab === 'accounts' ? 'bg-accent-600 text-white' : 'bg-ink-800 text-slate-300 hover:bg-ink-700'}">Аккаунты</button>
          <button id="dlgTabGroups" class="px-2 py-1 rounded text-xs ${state.dialogs.leftTab === 'groups' ? 'bg-accent-600 text-white' : 'bg-ink-800 text-slate-300 hover:bg-ink-700'}">Группы</button>
        </div>
        <div class="px-3 py-2 border-b border-ink-700">
          <input
            id="dlgAccountsSearch"
            type="text"
            placeholder="Поиск по названию аккаунта…"
            value="${escapeHTML(state.dialogs?.accountSearch || '')}"
            class="w-full bg-ink-800 border border-ink-700 rounded px-2 py-1.5 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:border-accent-500 ${state.dialogs.leftTab === 'accounts' ? '' : 'hidden'}"
          />
        </div>
        <div id="dlgAccountsList" class="flex-1 overflow-y-auto cb-scroll ${state.dialogs.leftTab === 'accounts' ? '' : 'hidden'}">
          <div class="p-4 text-slate-500 text-sm">Загрузка…</div>
        </div>
        <div id="dlgGroupsList" class="flex-1 overflow-y-auto cb-scroll ${state.dialogs.leftTab === 'groups' ? '' : 'hidden'}">
          <div class="p-4 text-slate-500 text-sm">Загрузка…</div>
        </div>
      </aside>
      <section class="col-span-${peerId ? '4' : '9'} min-w-0 min-h-0 border-r border-ink-700 flex flex-col overflow-hidden">
        <div class="px-4 py-3 border-b border-ink-700 flex items-center justify-between">
          <h3 id="dlgListTitle" class="font-medium text-slate-200 text-sm">${accountId ? `Диалоги аккаунта #${accountId}` : 'Выберите аккаунт'}</h3>
          <button id="dlgListRefresh" class="text-xs text-slate-400 hover:text-slate-200">⟳</button>
        </div>
        <div id="dlgList" class="flex-1 overflow-y-auto cb-scroll">
          ${accountId ? '<div class="p-4 text-slate-500 text-sm">Загрузка…</div>' : '<div class="p-4 text-slate-500 text-sm">Слева выберите аккаунт.</div>'}
        </div>
      </section>
      ${peerId ? `
        <section class="col-span-5 min-w-0 min-h-0 flex flex-col overflow-hidden">
          <div class="px-4 py-3 border-b border-ink-700 flex items-center justify-between gap-2">
            <div class="min-w-0">
              <h3 class="font-medium text-slate-200 text-sm truncate" id="dlgChatTitle">Диалог с ${peerId}</h3>
              <div class="text-xs text-slate-500" id="dlgChatSub">—</div>
            </div>
            <div class="flex items-center gap-2">
              <button id="dlgDeleteBtn" class="text-xs px-2 py-1 rounded bg-rose-900/40 hover:bg-rose-800 text-rose-200 border border-rose-700/50">🗑 Удалить</button>
            </div>
          </div>
          <div id="dlgMessages" class="flex-1 overflow-y-auto cb-scroll p-4 space-y-2 bg-ink-950/40">
            <div class="text-slate-500 text-sm text-center">Загрузка…</div>
          </div>
          <div id="dlgComposer" class="border-t border-ink-700 p-3"></div>
        </section>
      ` : ''}
    </div>
  `;

  $("#dlgAccountsRefresh").addEventListener("click", () => loadDialogsAccounts(accountId, peerId, { force: true }));
  $("#dlgTabAccounts")?.addEventListener("click", () => switchDialogsLeftTab("accounts", accountId));
  $("#dlgTabGroups")?.addEventListener("click", () => switchDialogsLeftTab("groups", accountId));
  const searchInput = $("#dlgAccountsSearch");
  if (searchInput) {
    searchInput.addEventListener("input", (ev) => {
      state.dialogs.accountSearch = ev.target.value || "";
      paintDialogsAccounts(state.cache.accounts || [], accountId);
    });
    // Курсор в конец, чтобы при перерисовке не «прыгал».
    requestAnimationFrame(() => {
      const v = searchInput.value;
      try { searchInput.setSelectionRange(v.length, v.length); } catch (_) {}
    });
  }
  if ($("#dlgListRefresh") && accountId) {
    $("#dlgListRefresh").addEventListener("click", () => loadDialogsList(accountId, peerId));
  }
  if (peerId) {
    $("#dlgDeleteBtn").addEventListener("click", () => deleteCurrentDialog(accountId, peerId));
  }

  // force=true: при заходе в раздел всегда тянем актуальные last_dialog_at,
  // чтобы порядок «как в мессенджере» отражал свежие сообщения.
  await loadDialogsAccounts(accountId, peerId, { force: true });
  await loadDialogsGroups();
  if (accountId) await loadDialogsList(accountId, peerId);
  if (accountId && peerId) await loadDialogMessages(accountId, peerId);
}

function switchDialogsLeftTab(tab, activeAccountId) {
  state.dialogs.leftTab = tab === "groups" ? "groups" : "accounts";
  const accList = $("#dlgAccountsList");
  const grpList = $("#dlgGroupsList");
  const inp = $("#dlgAccountsSearch");
  const tAcc = $("#dlgTabAccounts");
  const tGrp = $("#dlgTabGroups");
  if (accList) accList.classList.toggle("hidden", state.dialogs.leftTab !== "accounts");
  if (grpList) grpList.classList.toggle("hidden", state.dialogs.leftTab !== "groups");
  if (inp) inp.classList.toggle("hidden", state.dialogs.leftTab !== "accounts");
  if (tAcc) tAcc.className = `px-2 py-1 rounded text-xs ${state.dialogs.leftTab === "accounts" ? "bg-accent-600 text-white" : "bg-ink-800 text-slate-300 hover:bg-ink-700"}`;
  if (tGrp) tGrp.className = `px-2 py-1 rounded text-xs ${state.dialogs.leftTab === "groups" ? "bg-accent-600 text-white" : "bg-ink-800 text-slate-300 hover:bg-ink-700"}`;
  if (state.dialogs.leftTab === "accounts") {
    paintDialogsAccounts(state.cache.accounts || [], activeAccountId);
  }
}

async function loadDialogsAccounts(activeAccountId, activePeerId, opts = {}) {
  const el = $("#dlgAccountsList");
  if (!el) return;
  try {
    // Всегда тянем свежий список, чтобы last_dialog_at был актуальным
    // и порядок «как в мессенджере» не врал. Кэш используем только
    // для синхронных перерисовок при наборе текста в поиске.
    const list = (!opts.force && state.cache.accounts)
      ? state.cache.accounts
      : await api("/business/accounts");
    state.cache.accounts = list;
    state.cache.accountById = new Map(list.map(a => [a.id, a]));
    paintDialogsAccounts(list, activeAccountId);
  } catch (e) {
    el.innerHTML = `<div class="p-4 text-rose-400 text-sm">Ошибка: ${escapeHTML(e.message)}</div>`;
  }
}

function paintDialogsAccounts(list, activeAccountId) {
  const el = $("#dlgAccountsList");
  if (!el) return;
  if (!list.length) {
    el.innerHTML = `<div class="p-4 text-slate-500 text-sm">Нет аккаунтов.</div>`;
    return;
  }

  let scoped = list.slice();
  if (state.dialogs.selectedGroupId !== null && state.dialogs.groupAccountIds instanceof Set) {
    scoped = scoped.filter((a) => state.dialogs.groupAccountIds.has(Number(a.id)));
  }

  const q = (state.dialogs.accountSearch || "").trim().toLowerCase();
  const filtered = q
    ? scoped.filter(a => {
        // Поиск по «имени аккаунта внутри бота» (list_label) — приоритет,
        // плюс fallback на username/phone, чтобы пользователь
        // мог найти безымянные аккаунты.
        const hay = [a.list_label, a.username, a.phone, `#${a.id}`]
          .filter(Boolean).join(" ").toLowerCase();
        return hay.includes(q);
      })
    : scoped.slice();

  // Сортировка «как в Telegram»: сначала свежие диалоги.
  // Аккаунты без диалогов — в самом низу, среди них стабильный порядок по id.
  filtered.sort((a, b) => {
    const ta = a.last_dialog_at ? Date.parse(a.last_dialog_at) : 0;
    const tb = b.last_dialog_at ? Date.parse(b.last_dialog_at) : 0;
    if (tb !== ta) return tb - ta;
    return (a.id || 0) - (b.id || 0);
  });

  if (!filtered.length) {
    el.innerHTML = `<div class="p-4 text-slate-500 text-sm">Ничего не найдено.</div>`;
    return;
  }

  el.innerHTML = filtered.map(a => {
    // Имя «как в боте»: list_label, fallback на username/phone/#id —
    // именно по нему ведётся поиск.
    const internalTitle = a.list_label || a.username || a.phone || `#${a.id}`;
    // Имя «как в Telegram»: first_name + last_name, либо @username.
    const tgFull = [a.first_name, a.last_name].filter(Boolean).join(" ").trim();
    const tgHandle = a.username ? `@${a.username}` : "";
    let tgLabel = tgFull || tgHandle;
    if (tgLabel && tgLabel === internalTitle) tgLabel = "";
    if (tgLabel && tgFull && tgHandle && tgLabel === tgFull && a.username && a.username !== a.list_label) {
      tgLabel = `${tgFull} · ${tgHandle}`;
    }

    const isActive = a.id === activeAccountId;
    const modePill = a.ai_mode === "MANUAL"
      ? '<span class="pill pill-amber">M</span>'
      : '<span class="pill pill-green">AI</span>';
    const pendingBadge = a.pending_outbound > 0
      ? `<span class="pill pill-amber">↑${a.pending_outbound}</span>` : '';
    const lastWhen = a.last_dialog_at ? fmtRelative(a.last_dialog_at) : '';
    return `
      <a href="#/dialogs/${a.id}" class="block px-4 py-2 border-b border-ink-700 ${isActive ? 'bg-ink-800' : 'hover:bg-ink-800/60'}">
        <div class="flex items-center justify-between gap-2">
          <div class="min-w-0 text-sm text-slate-100 truncate">
            ${escapeHTML(internalTitle)}
            ${tgLabel ? `<span class="text-[11px] text-slate-400 ml-1">· ${escapeHTML(tgLabel)}</span>` : ''}
          </div>
          <div class="flex items-center gap-1 shrink-0">${modePill}${pendingBadge}</div>
        </div>
        <div class="flex items-center justify-between gap-2 mt-0.5">
          <div class="text-[11px] text-slate-500 truncate">диалогов: ${a.dialogs_count}</div>
          <div class="text-[11px] text-slate-500 shrink-0">${lastWhen}</div>
        </div>
      </a>
    `;
  }).join("");
}

async function loadDialogsGroups() {
  const el = $("#dlgGroupsList");
  if (!el) return;
  try {
    const groups = await api("/business/groups");
    const selected = state.dialogs.selectedGroupId;
    const rows = [
      { id: null, name: "Все", accounts_count: state.cache.accounts?.length || 0 },
      ...(groups || []),
    ];
    el.innerHTML = rows.map((g) => {
      const isActive = selected === g.id;
      return `
        <button data-dlg-group-id="${g.id === null ? "all" : g.id}"
          class="w-full text-left block px-4 py-2 border-b border-ink-700 ${isActive ? 'bg-ink-800' : 'hover:bg-ink-800/60'}">
          <div class="flex items-center justify-between gap-2">
            <div class="text-sm text-slate-100 truncate">${escapeHTML(g.name)}</div>
            <span class="text-[11px] text-slate-500">${g.accounts_count ?? 0}</span>
          </div>
        </button>
      `;
    }).join("");
    el.querySelectorAll("button[data-dlg-group-id]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const raw = btn.dataset.dlgGroupId;
        if (raw === "all") {
          state.dialogs.selectedGroupId = null;
          state.dialogs.selectedGroupName = "Все";
          state.dialogs.groupAccountIds = null;
        } else {
          const gid = Number(raw);
          const groupRows = await api(`/business/groups/${gid}/accounts`);
          state.dialogs.selectedGroupId = gid;
          state.dialogs.selectedGroupName = (groups.find((x) => x.id === gid)?.name || `#${gid}`);
          state.dialogs.groupAccountIds = new Set((groupRows || []).map((x) => Number(x.id)));
        }
        paintDialogsAccounts(state.cache.accounts || [], state.current.accountId);
        loadDialogsGroups();
        switchDialogsLeftTab("accounts", state.current.accountId);
      });
    });
  } catch (e) {
    el.innerHTML = `<div class="p-4 text-rose-400 text-sm">Ошибка: ${escapeHTML(e.message)}</div>`;
  }
}

async function loadDialogsList(accountId, activePeerId) {
  const el = $("#dlgList");
  if (!el) return;
  try {
    const list = await api(`/business/accounts/${accountId}/dialogs?limit=200`);
    if (!list.length) {
      el.innerHTML = `<div class="p-4 text-slate-500 text-sm">У этого аккаунта пока нет диалогов в нейрочате.</div>`;
      return;
    }
    el.innerHTML = list.map(d => {
      const title = d.client_username ? `@${d.client_username}` : `id ${d.peer_user_id}`;
      const isActive = d.peer_user_id === activePeerId;
      const lastWho = d.last_role === "assistant" ? "Бот" : "Клиент";
      return `
        <a href="#/dialogs/${accountId}/${d.peer_user_id}" class="block px-4 py-3 border-b border-ink-700 ${isActive ? 'bg-ink-800' : 'hover:bg-ink-800/60'}">
          <div class="flex items-center justify-between gap-2">
            <div class="text-sm font-medium text-slate-100 truncate">${escapeHTML(title)}</div>
            <div class="text-[11px] text-slate-500 shrink-0">${fmtRelative(d.last_message_at)}</div>
          </div>
          <div class="text-xs text-slate-400 truncate mt-0.5"><span class="text-slate-500">${lastWho}:</span> ${escapeHTML(d.last_message || "")}</div>
          <div class="text-[11px] text-slate-500 mt-1">${d.messages_count} сообщ.</div>
        </a>
      `;
    }).join("");
  } catch (e) {
    el.innerHTML = `<div class="p-4 text-rose-400 text-sm">Ошибка: ${escapeHTML(e.message)}</div>`;
  }
}

async function loadDialogMessages(accountId, peerId) {
  const el = $("#dlgMessages");
  if (!el) return;
  try {
    const list = await api(`/business/accounts/${accountId}/dialogs/${peerId}/messages?limit=200`);
    // Важно: сбрасываем дедуп-курсор ПЕРЕД повторным рендером, иначе
    // appendMessageToChat() выкинет все «старые» сообщения как уже виденные
    // и в ленте останутся только новые/queue. messageMaxId обновляется
    // внутри appendMessageToChat (он сам берёт max).
    state.current.messageMaxId = 0;
    if (!list.length) {
      el.innerHTML = `<div class="text-slate-500 text-sm text-center">Сообщений пока нет.</div>`;
    } else {
      el.innerHTML = "";
      list.forEach(appendMessageToChat);
    }
    renderComposer(accountId, peerId);
    el.scrollTop = el.scrollHeight;
  } catch (e) {
    el.innerHTML = `<div class="text-rose-400 text-sm text-center">${escapeHTML(e.message)}</div>`;
  }
}

function _queueStatusBadge(status) {
  switch (status) {
    case "pending":   return '<span class="qpill qpill-pend">в очереди</span>';
    case "sending":   return '<span class="qpill qpill-send">отправляется</span>';
    case "failed":    return '<span class="qpill qpill-fail">не доставлено</span>';
    case "cancelled": return '<span class="qpill qpill-cncl">отменено</span>';
    case "sent":      return '<span class="qpill qpill-ok">отправлено</span>';
    default:          return `<span class="qpill">${escapeHTML(status || "?")}</span>`;
  }
}

function appendMessageToChat(msg) {
  const el = $("#dlgMessages");
  if (!el) return;
  // Дубли только по реальным neuro-сообщениям (положительный id).
  if (msg.source !== "queue" && (msg.id || 0) > 0 && msg.id <= state.current.messageMaxId) return;
  if (msg.source !== "queue") {
    state.current.messageMaxId = Math.max(state.current.messageMaxId, msg.id || 0);
  }

  const isAssistant = msg.role === "assistant";
  const isQueue = msg.source === "queue";
  const wrap = document.createElement("div");
  wrap.className = "flex flex-col " + (isAssistant ? "items-end" : "items-start");

  let metaExtra = "";
  let bubbleCls = isAssistant ? "bubble bubble-asst" : "bubble bubble-user";

  if (isQueue) {
    bubbleCls += " bubble-queue qstatus-" + escapeHTML(msg.queue_status || "pending");
    metaExtra = ` · ${_queueStatusBadge(msg.queue_status)}`;
    if (msg.queue_attempts) metaExtra += ` · попытка ${msg.queue_attempts}`;
    if (msg.queue_error) {
      metaExtra += ` · <span class="text-rose-400" title="${escapeHTML(msg.queue_error)}">${escapeHTML(msg.queue_error.slice(0, 60))}</span>`;
    }
  }

  let actions = "";
  if (isQueue) {
    if (msg.queue_status === "failed" || msg.queue_status === "cancelled") {
      actions += `<button class="qbtn qbtn-retry" data-action="retry" data-qid="${msg.queue_id}">Повторить</button>`;
    }
    if (msg.queue_status === "pending" || msg.queue_status === "failed") {
      actions += `<button class="qbtn qbtn-cancel" data-action="cancel" data-qid="${msg.queue_id}">Отменить</button>`;
    }
  }

  wrap.innerHTML = `
    <div class="${bubbleCls}">${escapeHTML(msg.content || "")}</div>
    <div class="bubble-meta">${isAssistant ? "бот" : "клиент"} · ${fmtDate(msg.created_at)}${metaExtra}</div>
    ${actions ? `<div class="bubble-actions">${actions}</div>` : ""}
  `;

  if (actions) {
    wrap.querySelectorAll("button[data-action]").forEach((b) => {
      b.addEventListener("click", () => onQueueAction(b.dataset.action, parseInt(b.dataset.qid, 10)));
    });
  }

  el.appendChild(wrap);
  el.scrollTop = el.scrollHeight;
}

async function onQueueAction(action, queueId) {
  if (!queueId) return;
  try {
    if (action === "retry") {
      await api(`/business/queue/${queueId}/retry`, { method: "POST" });
      toast("Поставлено на повторную отправку", "success", 1500);
    } else if (action === "cancel") {
      if (!confirm("Отменить отправку этого сообщения?")) return;
      await api(`/business/queue/${queueId}/cancel`, { method: "POST", raw: true });
      toast("Отменено", "success", 1500);
    }
    if (state.current.accountId && state.current.peerId) {
      loadDialogMessages(state.current.accountId, state.current.peerId);
    }
  } catch (e) {
    toast(`Ошибка: ${e.message}`, "error");
  }
}

function renderComposer(accountId, peerId) {
  const el = $("#dlgComposer");
  if (!el) return;
  const acc = state.cache.accountById.get(accountId);
  const isManual = acc?.ai_mode === "MANUAL";
  const hint = isManual
    ? "Аккаунт в MANUAL — сообщения клиенту шлёте только вы."
    : "Аккаунт в AI_ACTIVE — ручное сообщение перехватит инициативу, ИИ продолжит видеть его в истории.";
  el.innerHTML = `
    <form id="dlgSendForm" class="flex items-end gap-2">
      <textarea id="dlgInput" rows="2" maxlength="4000" placeholder="Введите ответ от имени аккаунта…"
        class="flex-1 resize-none px-3 py-2 rounded-lg bg-ink-800 border border-ink-600 text-sm text-slate-100 focus:border-accent-500 focus:outline-none focus:ring-1 focus:ring-accent-500"></textarea>
      <button class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white text-sm font-medium" type="submit">Отправить</button>
    </form>
    <div class="text-[11px] text-slate-500 mt-1">${escapeHTML(hint)}</div>
  `;
  $("#dlgSendForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const ta = $("#dlgInput");
    const text = ta.value.trim();
    if (!text) return;
    try {
      await api(`/business/accounts/${accountId}/dialogs/${peerId}/send`, {
        method: "POST",
        body: { text },
      });
      ta.value = "";
      toast("Сообщение поставлено в очередь", "success", 1500);
      // Сразу подтягиваем, чтобы placeholder появился в ленте.
      loadDialogMessages(accountId, peerId);
    } catch (e) {
      toast(`Не удалось отправить: ${e.message}`, "error");
    }
  });
}

async function deleteCurrentDialog(accountId, peerId) {
  if (!confirm("Удалить диалог? Все сообщения и связанные исходящие будут вычищены из БД.")) return;
  try {
    await api(`/business/accounts/${accountId}/dialogs/${peerId}`, { method: "DELETE", raw: true });
    toast("Диалог удалён", "success");
    window.location.hash = `#/dialogs/${accountId}`;
  } catch (e) {
    toast(`Ошибка удаления: ${e.message}`, "error");
  }
}

/* ----------------------------- Mailings view --------------------------- */

async function renderMailings(idStr) {
  const id = idStr ? Number(idStr) : null;
  setHeader("Рассылки", id ? `Рассылка #${id}` : "Список и управление кампаниями");
  const root = $("#pageRoot");
  if (!id) {
    root.innerHTML = `
      <div class="p-6 cb-scroll overflow-y-auto h-full space-y-4">
        <div class="card">
          <div class="flex items-center justify-between mb-3">
            <h3 class="font-semibold text-slate-100">Новая рассылка</h3>
            <span class="text-xs text-slate-500">Создаётся как draft, дальше — настройка в карточке</span>
          </div>
          <form id="mailCreateForm" class="grid grid-cols-1 md:grid-cols-4 gap-3 text-sm">
            <label class="block md:col-span-2">
              <span class="text-slate-400 text-xs">Название *</span>
              <input name="name" required placeholder="Новая кампания"
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">Аудитория</span>
              <select name="audience_mode" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
                <option value="classes">classes</option>
                <option value="test">test</option>
                <option value="all">all</option>
              </select>
            </label>
            <label class="flex items-center gap-2 mt-6 text-slate-300">
              <input name="neurochat_enabled" type="checkbox" class="rounded border-ink-600 bg-ink-800" />
              Нейрочат включён
            </label>
            <label class="block md:col-span-4">
              <span class="text-slate-400 text-xs">Первое сообщение (можно пустое)</span>
              <textarea name="message_text" rows="2"
                        class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100"></textarea>
            </label>
            <div class="md:col-span-4 flex items-center gap-3">
              <button type="submit" class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white">Создать рассылку</button>
              <span id="mailCreateMsg" class="text-xs text-slate-400"></span>
            </div>
          </form>
        </div>
        <div class="flex items-center gap-3 text-sm">
          <label class="text-slate-400">Статус:</label>
          <select id="mailFilter" class="bg-ink-800 border border-ink-600 rounded-md px-2 py-1 text-slate-100">
            <option value="">все</option>
            <option value="running">running</option>
            <option value="paused">paused</option>
            <option value="completed">completed</option>
            <option value="draft">draft</option>
            <option value="cancelled">cancelled</option>
            <option value="error">error</option>
          </select>
          <button id="mailRefresh" class="px-3 py-1 rounded bg-accent-600 hover:bg-accent-500 text-white text-sm">⟳ Обновить</button>
        </div>
        <div class="card p-0 overflow-hidden">
          <table class="cb-table">
            <thead><tr>
              <th>ID</th><th>Название</th><th>Статус</th>
              <th class="text-right">Отправлено</th><th class="text-right">Ошибок</th>
              <th>Аудитория</th><th>AI</th><th>Создана</th><th></th>
            </tr></thead>
            <tbody id="mailBody"><tr><td colspan="9" class="text-center text-slate-500 py-8">Загрузка…</td></tr></tbody>
          </table>
        </div>
      </div>
    `;
    $("#mailCreateForm")?.addEventListener("submit", onCreateMailingSubmit);
    $("#mailFilter").addEventListener("change", loadMailingsList);
    $("#mailRefresh").addEventListener("click", loadMailingsList);
    await loadMailingsList();
    return;
  }
  // detail
  root.innerHTML = `<div id="mailDetail" class="p-6 cb-scroll overflow-y-auto h-full">Загрузка…</div>`;
  await loadMailingDetail(id);
}

async function onCreateMailingSubmit(ev) {
  ev.preventDefault();
  const fd = new FormData(ev.currentTarget);
  const body = {
    name: (fd.get("name") || "").toString().trim(),
    message_text: (fd.get("message_text") || "").toString(),
    audience_mode: (fd.get("audience_mode") || "classes").toString(),
    neurochat_enabled: !!fd.get("neurochat_enabled"),
  };
  const out = $("#mailCreateMsg");
  out.textContent = "…";
  try {
    const created = await api("/business/mailings", { method: "POST", body });
    toast(`Рассылка #${created.id} создана`, "success");
    out.textContent = `ok (#${created.id})`;
    out.className = "text-xs text-emerald-300";
    ev.currentTarget.reset();
    await loadMailingsList();
    window.location.hash = `#/mailings/${created.id}`;
  } catch (e) {
    out.textContent = e.message;
    out.className = "text-xs text-rose-400";
  }
}

async function loadMailingsList() {
  try {
    const filter = ($("#mailFilter")?.value || "").trim();
    const qs = filter ? `?status=${encodeURIComponent(filter)}` : "";
    const list = await api(`/business/mailings${qs}`);
    const tbody = $("#mailBody");
    if (!list.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="text-center text-slate-500 py-8">Нет рассылок.</td></tr>`;
      return;
    }
    tbody.innerHTML = list.map(m => `
      <tr>
        <td class="text-slate-500">#${m.id}</td>
        <td><a href="#/mailings/${m.id}" class="text-slate-100 hover:text-accent-500">${escapeHTML(m.name)}</a></td>
        <td>${mailingStatusPill(m.status)}</td>
        <td class="text-right text-slate-300">${m.sent}/${m.total || "—"}</td>
        <td class="text-right ${m.failed ? "text-rose-300" : "text-slate-400"}">${m.failed}</td>
        <td class="text-xs text-slate-400">${escapeHTML(m.audience_mode || "—")}</td>
        <td>${m.neurochat_enabled ? '<span class="pill pill-blue">on</span>' : '<span class="pill pill-gray">off</span>'}</td>
        <td class="text-xs text-slate-400">${fmtDate(m.created_at)}</td>
        <td class="text-right">
          ${mailingActionButtons(m)}
        </td>
      </tr>
    `).join("");
    tbody.querySelectorAll("button[data-mail-act]").forEach(b => {
      b.addEventListener("click", () => onMailingAction(Number(b.dataset.mid), b.dataset.mailAct));
    });
  } catch (e) { toast(`Рассылки: ${e.message}`, "error"); }
}

function mailingStatusPill(s) {
  const map = {
    running: "pill-green", paused: "pill-amber", draft: "pill-gray",
    pending: "pill-gray", completed: "pill-blue", cancelled: "pill-gray", error: "pill-red",
  };
  return `<span class="pill ${map[s] || "pill-gray"}">${escapeHTML(s)}</span>`;
}

function mailingActionButtons(m) {
  const buttons = [];
  if (m.status === "running") {
    buttons.push(`<button class="px-2 py-1 rounded bg-amber-700 hover:bg-amber-600 text-xs" data-mail-act="pause" data-mid="${m.id}">⏸ Пауза</button>`);
    buttons.push(`<button class="px-2 py-1 rounded bg-rose-700 hover:bg-rose-600 text-xs ml-1" data-mail-act="stop" data-mid="${m.id}">⏹ Стоп</button>`);
  } else if (m.status === "paused") {
    buttons.push(`<button class="px-2 py-1 rounded bg-emerald-700 hover:bg-emerald-600 text-xs" data-mail-act="start" data-mid="${m.id}">▶ Запуск</button>`);
    buttons.push(`<button class="px-2 py-1 rounded bg-rose-700 hover:bg-rose-600 text-xs ml-1" data-mail-act="stop" data-mid="${m.id}">⏹ Стоп</button>`);
  } else {
    buttons.push(`<button class="px-2 py-1 rounded bg-emerald-700 hover:bg-emerald-600 text-xs" data-mail-act="start" data-mid="${m.id}">▶ Запуск</button>`);
  }
  return buttons.join("");
}

async function onMailingAction(mailingId, action) {
  if (!mailingId || !action) return;
  if (action === "stop" && !confirm(`Остановить рассылку #${mailingId}?`)) return;
  try {
    const r = await api(`/business/mailings/${mailingId}/${action}`, { method: "POST" });
    if (r.status === "queued") {
      toast(`Команда ${action} поставлена в очередь`, "success", 1500);
    } else {
      toast(`${action}: ${r.detail || r.status}`, "info", 1500);
    }
    setTimeout(() => loadMailingsList(), 1500);
  } catch (e) { toast(`Ошибка ${action}: ${e.message}`, "error"); }
}

async function loadMailingDetail(id) {
  const root = $("#mailDetail");
  try {
    const [m, groups] = await Promise.all([
      api(`/business/mailings/${id}`),
      api(`/business/groups`).catch(() => []),
    ]);
    const editLocked = m.status === "running";
    const groupOptions = [
      `<option value="0" ${!m.target_group_id ? "selected" : ""}>— все группы —</option>`,
      ...(groups || []).map(g => `<option value="${g.id}" ${m.target_group_id === g.id ? "selected" : ""}>${escapeHTML(g.name)}</option>`),
    ].join("");
    root.innerHTML = `
      <div class="space-y-4 max-w-5xl">
        <div class="flex items-center gap-3">
          <a href="#/mailings" class="text-sm text-slate-400 hover:text-slate-200">← К списку</a>
          ${mailingStatusPill(m.status)}
          <h2 class="text-lg text-slate-100 font-semibold">${escapeHTML(m.name)}</h2>
          <div class="ml-auto flex gap-1">${mailingActionButtons(m)}</div>
        </div>

        <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
          ${kpi("Отправлено", "mdSent", String(m.sent), `из ${m.total || "—"}`)}
          ${kpi("Ошибок", "mdFail", String(m.failed), "", "text-rose-300")}
          ${kpi("Старт", "mdStart", fmtDate(m.started_at) || "—", "")}
          ${kpi("Финиш", "mdEnd", fmtDate(m.completed_at) || "—", "")}
        </div>

        <!--
          ВАЖНО: настройки рассылки и настройки нейрочата разнесены
          в две независимые формы с собственными submit-кнопками,
          чтобы по визуалу и UX совпадать с разделением «рассылка vs нейрочат».
          Бэкенд использует один и тот же PATCH /business/mailings/{id}
          и принимает любой подмножественный набор полей.
        -->
        <div class="card">
          <div class="flex items-center justify-between mb-3">
            <h3 class="font-semibold">Настройки рассылки</h3>
            ${editLocked
              ? `<span class="text-xs text-amber-400">RUNNING — поставьте на паузу для редактирования</span>`
              : `<span class="text-xs text-slate-500">Первое сообщение, аудитория, лимиты и задержки</span>`}
          </div>
          <form id="mailEditForm" class="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm" ${editLocked ? "data-locked=1" : ""}>
            <label class="block md:col-span-3">
              <span class="text-slate-400 text-xs">Название</span>
              <input name="name" value="${escapeHTML(m.name || "")}" ${editLocked ? "disabled" : ""}
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block md:col-span-3">
              <span class="text-slate-400 text-xs">Текст основного сообщения</span>
              <textarea name="message_text" rows="4" ${editLocked ? "disabled" : ""}
                class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 font-mono text-xs">${escapeHTML(m.message_text || "")}</textarea>
            </label>
            <label class="block md:col-span-3">
              <span class="text-slate-400 text-xs">Варианты сообщения (по одному в строке)</span>
              <textarea name="message_variants" rows="3" ${editLocked ? "disabled" : ""}
                class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 font-mono text-xs">${escapeHTML((m.message_variants || []).join("\n"))}</textarea>
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">Между сообщениями (с)</span>
              <input name="delay_between_messages" type="number" step="0.1" min="0" max="600" value="${m.delay_between_messages}" ${editLocked ? "disabled" : ""}
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">Между аккаунтами (с)</span>
              <input name="delay_between_accounts" type="number" step="0.1" min="0" max="600" value="${m.delay_between_accounts}" ${editLocked ? "disabled" : ""}
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">Дневной лимит</span>
              <input name="daily_limit" type="number" min="0" max="10000" value="${m.daily_limit}" ${editLocked ? "disabled" : ""}
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">В пакете</span>
              <input name="messages_per_batch" type="number" min="0" max="10000" value="${m.messages_per_batch}" ${editLocked ? "disabled" : ""}
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">Между пакетами (с)</span>
              <input name="batch_delay" type="number" step="0.1" min="0" max="86400" value="${m.batch_delay}" ${editLocked ? "disabled" : ""}
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">Auto stop (ч), 0 = нет</span>
              <input name="auto_stop_hours" type="number" step="0.5" min="0" max="720" value="${m.auto_stop_hours ?? 0}" ${editLocked ? "disabled" : ""}
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">Целевая группа</span>
              <select name="target_group_id" ${editLocked ? "disabled" : ""}
                class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">${groupOptions}</select>
            </label>
            <label class="block md:col-span-2">
              <span class="text-slate-400 text-xs">Community link (для {link})</span>
              <input name="community_link" value="${escapeHTML(m.community_link || "")}" ${editLocked ? "disabled" : ""}
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">Аудитория</span>
              <select name="audience_mode" ${editLocked ? "disabled" : ""}
                class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
                ${["classes","test","all"].map(v => `<option value="${v}" ${m.audience_mode === v ? "selected" : ""}>${v}</option>`).join("")}
              </select>
            </label>
            <div class="md:col-span-3 flex items-center gap-3">
              <button type="submit" ${editLocked ? "disabled" : ""}
                class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white disabled:opacity-50 disabled:cursor-not-allowed">Сохранить рассылку</button>
              <span id="mailEditMsg" class="text-xs text-slate-400"></span>
            </div>
          </form>
        </div>

        <div class="card">
          <div class="flex items-center justify-between mb-3">
            <h3 class="font-semibold">Настройки нейрочата</h3>
            ${editLocked
              ? `<span class="text-xs text-amber-400">RUNNING — поставьте на паузу для редактирования</span>`
              : `<span class="text-xs text-slate-500">Модель, sampling и system-промпт</span>`}
          </div>
          <form id="neuroEditForm" class="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm mb-5" ${editLocked ? "data-locked=1" : ""}>
            <label class="flex items-center gap-2 md:col-span-1 mt-6 text-slate-300">
              <input name="neurochat_enabled" type="checkbox" ${m.neurochat_enabled ? "checked" : ""} ${editLocked ? "disabled" : ""}
                     class="rounded border-ink-600 bg-ink-800" />
              Нейрочат включён
            </label>
            <label class="block md:col-span-2">
              <span class="text-slate-400 text-xs">Модель OpenRouter (например, openai/gpt-4o-mini)</span>
              <input name="neuro_model" value="${escapeHTML(m.neuro_model || "")}" ${editLocked ? "disabled" : ""}
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 font-mono" />
            </label>
            <label class="block md:col-span-3">
              <span class="text-slate-400 text-xs">Sampling overrides (JSON: temperature/top_p/max_tokens/…)</span>
              <textarea name="neuro_sampling_json" rows="2" ${editLocked ? "disabled" : ""}
                class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 font-mono text-xs">${escapeHTML(m.neuro_sampling_json || "{}")}</textarea>
            </label>
            <div class="md:col-span-3 flex items-center gap-3">
              <button type="submit" ${editLocked ? "disabled" : ""}
                class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white disabled:opacity-50 disabled:cursor-not-allowed">Сохранить нейрочат</button>
              <span id="neuroEditMsg" class="text-xs text-slate-400"></span>
            </div>
          </form>

          <div class="border-t border-ink-700 pt-4">
            <div class="flex items-center justify-between mb-2">
              <h4 class="font-medium text-slate-200">System-промпт</h4>
              <div id="mailPromptStatus" class="text-xs text-slate-500">…</div>
            </div>
            <p class="text-xs text-slate-500 mb-3">Сохраняется в <code>data/neuro/mailings/${id}/system.txt</code>. Если файла нет — используется значение из <code>DEFAULT_NEURO_SYSTEM_PROMPT</code>. Доступные плейсхолдеры см. в боте.</p>
            <textarea id="mailPromptText" rows="14"
              class="w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 font-mono text-xs">Загрузка…</textarea>
            <div class="flex items-center gap-3 mt-3">
              <button id="mailPromptSave" class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white text-sm">Сохранить промпт</button>
              <button id="mailPromptReset" class="px-3 py-2 rounded-lg bg-ink-700 hover:bg-ink-600 text-slate-200 text-sm">Сбросить к DEFAULT</button>
              <span id="mailPromptMsg" class="text-xs text-slate-400"></span>
            </div>
          </div>
        </div>
      </div>
    `;
    root.querySelectorAll("button[data-mail-act]").forEach(b => {
      b.addEventListener("click", () => onMailingAction(Number(b.dataset.mid), b.dataset.mailAct));
    });
    if (!editLocked) {
      $("#mailEditForm").addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const fd = new FormData(ev.currentTarget);
        const variantsRaw = (fd.get("message_variants") || "").toString();
        const variants = variantsRaw.split("\n").map(s => s.trim()).filter(Boolean);
        const auto = Number(fd.get("auto_stop_hours") || 0);
        const body = {
          name: (fd.get("name") || "").toString(),
          message_text: (fd.get("message_text") || "").toString(),
          message_variants: variants,
          delay_between_messages: Number(fd.get("delay_between_messages") || 0),
          delay_between_accounts: Number(fd.get("delay_between_accounts") || 0),
          daily_limit: Number(fd.get("daily_limit") || 0),
          messages_per_batch: Number(fd.get("messages_per_batch") || 0),
          batch_delay: Number(fd.get("batch_delay") || 0),
          auto_stop_hours: auto > 0 ? auto : 0,
          target_group_id: Number(fd.get("target_group_id") || 0),
          community_link: (fd.get("community_link") || "").toString(),
          audience_mode: (fd.get("audience_mode") || "classes").toString(),
        };
        const out = $("#mailEditMsg");
        out.textContent = "…";
        try {
          await api(`/business/mailings/${id}`, { method: "PATCH", body });
          toast("Рассылка сохранена", "success");
          out.textContent = "ok";
          out.className = "text-xs text-emerald-300";
          await loadMailingDetail(id);
        } catch (e) {
          out.textContent = e.message;
          out.className = "text-xs text-rose-400";
        }
      });

      $("#neuroEditForm").addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const fd = new FormData(ev.currentTarget);
        const body = {
          neurochat_enabled: !!fd.get("neurochat_enabled"),
          neuro_model: (fd.get("neuro_model") || "").toString(),
          neuro_sampling_json: (fd.get("neuro_sampling_json") || "{}").toString(),
        };
        const out = $("#neuroEditMsg");
        out.textContent = "…";
        try {
          await api(`/business/mailings/${id}`, { method: "PATCH", body });
          toast("Нейрочат сохранён", "success");
          out.textContent = "ok";
          out.className = "text-xs text-emerald-300";
          await loadMailingDetail(id);
        } catch (e) {
          out.textContent = e.message;
          out.className = "text-xs text-rose-400";
        }
      });
    }
    await fetchMailingPromptText(id);
    bindMailingPromptHandlers(id);
  } catch (e) { root.innerHTML = `<div class="text-rose-400">${escapeHTML(e.message)}</div>`; }
}

async function fetchMailingPromptText(id) {
  const ta = $("#mailPromptText");
  const status = $("#mailPromptStatus");
  if (!ta) return;
  try {
    const r = await api(`/business/mailings/${id}/prompt`);
    ta.value = r.text || "";
    status.textContent = r.has_custom_file ? "файл задан" : "по умолчанию (.env)";
    status.className = `text-xs ${r.has_custom_file ? "text-emerald-300" : "text-slate-500"}`;
  } catch (e) {
    ta.value = "";
    status.textContent = `ошибка: ${e.message}`;
    status.className = "text-xs text-rose-400";
  }
}

function bindMailingPromptHandlers(id) {
  $("#mailPromptSave").addEventListener("click", async () => {
    const ta = $("#mailPromptText");
    const out = $("#mailPromptMsg");
    if (!ta) return;
    out.textContent = "…";
    try {
      await api(`/business/mailings/${id}/prompt`, {
        method: "PUT",
        body: { text: ta.value },
      });
      toast("Промпт сохранён", "success");
      out.textContent = "ok";
      out.className = "text-xs text-emerald-300";
      await fetchMailingPromptText(id);
    } catch (e) {
      out.textContent = e.message;
      out.className = "text-xs text-rose-400";
    }
  });
  $("#mailPromptReset").addEventListener("click", async () => {
    if (!confirm("Удалить файл system.txt и вернуться к промпту по умолчанию?")) return;
    try {
      await api(`/business/mailings/${id}/prompt`, { method: "DELETE" });
      toast("Сброшено", "success");
      await fetchMailingPromptText(id);
    } catch (e) { toast(e.message, "error"); }
  });
}

/* ----------------------------- Clients view ---------------------------- */

async function renderClients(idStr) {
  const id = idStr ? Number(idStr) : null;
  setHeader("Клиенты", id ? `Клиент #${id}` : "База контактов и фильтр по классам");
  const root = $("#pageRoot");
  if (!id) {
    root.innerHTML = `
      <div class="p-6 cb-scroll overflow-y-auto h-full space-y-4">
        <div class="flex items-center gap-3 text-sm flex-wrap">
          <input id="clQ" placeholder="поиск по @username" class="px-3 py-1.5 rounded bg-ink-800 border border-ink-600 text-slate-100 w-64" />
          <select id="clClass" class="bg-ink-800 border border-ink-600 rounded px-2 py-1.5 text-slate-100">
            <option value="">все классы</option>
            <option value="accept">accept</option>
            <option value="alive">alive</option>
            <option value="pulse">pulse</option>
            <option value="dead">dead</option>
            <option value="bl">bl</option>
            <option value="decline">decline</option>
            <option value="stop">stop</option>
            <option value="send_link">send_link</option>
          </select>
          <button id="clApply" class="px-3 py-1.5 rounded bg-accent-600 hover:bg-accent-500 text-white">Применить</button>
          <span class="text-slate-500 text-xs ml-auto" id="clCount"></span>
        </div>
        <div class="card p-0 overflow-hidden">
          <table class="cb-table">
            <thead><tr>
              <th>ID</th><th>Username</th><th>TG ID</th><th>Статус</th>
              <th>Классы</th><th>Добавлен</th><th>Контакт</th><th></th>
            </tr></thead>
            <tbody id="clBody"><tr><td colspan="8" class="text-center text-slate-500 py-8">Загрузка…</td></tr></tbody>
          </table>
        </div>
      </div>
    `;
    $("#clApply").addEventListener("click", loadClientsList);
    $("#clQ").addEventListener("keydown", (e) => { if (e.key === "Enter") loadClientsList(); });
    await loadClientsList();
    return;
  }
  root.innerHTML = `<div id="clDetail" class="p-6 cb-scroll overflow-y-auto h-full">Загрузка…</div>`;
  await loadClientDetail(id);
}

async function loadClientsList() {
  try {
    const q = ($("#clQ")?.value || "").trim();
    const cls = ($("#clClass")?.value || "").trim();
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (cls) params.set("class_key", cls);
    params.set("limit", "100");
    const list = await api(`/business/clients?${params.toString()}`);
    const tbody = $("#clBody");
    $("#clCount").textContent = `найдено: ${list.length}`;
    if (!list.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="text-center text-slate-500 py-8">Нет клиентов под фильтр.</td></tr>`;
      return;
    }
    tbody.innerHTML = list.map(c => {
      const classes = c.classes.map(x => `<span class="pill cls-${escapeHTML(x.class_key)} pill-gray" title="${x.count}">${escapeHTML(x.class_key)}:${x.count}</span>`).join(" ") || `<span class="text-slate-500">—</span>`;
      return `
        <tr>
          <td class="text-slate-500">#${c.id}</td>
          <td><a href="#/clients/${c.id}" class="text-slate-100 hover:text-accent-500">@${escapeHTML(c.username)}</a></td>
          <td class="text-xs text-slate-400">${c.telegram_user_id ?? "—"}</td>
          <td>${escapeHTML(c.status)}</td>
          <td>${classes}</td>
          <td class="text-xs text-slate-400">${fmtDate(c.added_at)}</td>
          <td class="text-xs text-slate-400">${fmtRelative(c.last_contacted_at)}</td>
          <td class="text-right">
            <button data-cl-del="${c.id}" class="px-2 py-1 rounded bg-rose-900/40 hover:bg-rose-800 text-xs text-rose-200 border border-rose-700/50">🗑</button>
          </td>
        </tr>`;
    }).join("");
    tbody.querySelectorAll("button[data-cl-del]").forEach(b => {
      b.addEventListener("click", () => deleteClient(Number(b.dataset.clDel)));
    });
  } catch (e) { toast(`Клиенты: ${e.message}`, "error"); }
}

async function deleteClient(id) {
  if (!confirm(`Удалить клиента #${id}? Каскадно удалит классы/теги/события (NeuroChat — отдельно).`)) return;
  try {
    await api(`/business/clients/${id}`, { method: "DELETE", raw: true });
    toast(`Клиент #${id} удалён`, "success");
    loadClientsList();
  } catch (e) { toast(`Ошибка: ${e.message}`, "error"); }
}

async function loadClientDetail(id) {
  const root = $("#clDetail");
  try {
    const [c, interactions] = await Promise.all([
      api(`/business/clients/${id}`),
      api(`/business/clients/${id}/interactions?limit=100`),
    ]);
    root.innerHTML = `
      <div class="space-y-4 max-w-4xl">
        <div class="flex items-center gap-3">
          <a href="#/clients" class="text-sm text-slate-400 hover:text-slate-200">← К списку</a>
          <h2 class="text-lg text-slate-100 font-semibold">@${escapeHTML(c.username)}</h2>
          <div class="text-xs text-slate-500">tg_id ${c.telegram_user_id ?? "—"} · ${escapeHTML(c.status)}</div>
        </div>

        <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
          ${kpi("Добавлен", "cdAdd", fmtDate(c.added_at), "")}
          ${kpi("Послед. контакт", "cdLast", fmtDate(c.last_contacted_at) || "—", "")}
          ${kpi("Событий", "cdInter", String(c.interactions_count), "client_interactions")}
          ${kpi("Классов", "cdCls", String(c.classes.length), "счётчики")}
        </div>

        <div class="card">
          <h3 class="font-semibold mb-2">Классы</h3>
          <div id="cdClassList" class="flex flex-wrap gap-2 mb-3">
            ${c.classes.map(x => `
              <span class="pill cls-${escapeHTML(x.class_key)} pill-gray flex items-center gap-1">
                ${escapeHTML(x.class_key)}:${x.count}
                <button data-cl-cdec="${escapeHTML(x.class_key)}" class="text-rose-300">−</button>
                <button data-cl-cinc="${escapeHTML(x.class_key)}" class="text-emerald-300">+</button>
              </span>`).join("") || `<span class="text-slate-500">—</span>`}
          </div>
          <form id="cdAddForm" class="flex items-center gap-2 text-sm">
            <input id="cdNewKey" placeholder="новый класс" class="px-3 py-1.5 rounded bg-ink-800 border border-ink-600 text-slate-100" />
            <input id="cdNewVal" type="number" value="1" min="1" max="9999" class="px-3 py-1.5 rounded bg-ink-800 border border-ink-600 text-slate-100 w-24" />
            <button class="px-3 py-1.5 rounded bg-accent-600 hover:bg-accent-500 text-white">Добавить</button>
          </form>
        </div>

        ${c.tags?.length ? `
          <div class="card">
            <h3 class="font-semibold mb-2">Теги</h3>
            <div class="flex flex-wrap gap-2">
              ${c.tags.map(t => `<span class="pill pill-gray">${escapeHTML(t)}</span>`).join("")}
            </div>
          </div>` : ""}

        <div class="card">
          <h3 class="font-semibold mb-2">Последние взаимодействия (${interactions.length})</h3>
          ${interactions.length ? `
            <div class="space-y-2 text-sm">
              ${interactions.map(it => `
                <div class="flex items-start gap-2 border-b border-ink-700/60 pb-2">
                  <span class="pill ${it.direction === 'out' ? 'pill-blue' : it.direction === 'in' ? 'pill-gray' : 'pill-amber'}">${escapeHTML(it.direction)}</span>
                  <div class="min-w-0 flex-1">
                    <div class="text-slate-300 truncate">${escapeHTML(it.kind)}${it.body ? ': ' + escapeHTML(it.body.slice(0, 200)) : ''}</div>
                    <div class="text-[11px] text-slate-500">acc#${it.account_id ?? "—"} · mailing#${it.mailing_id ?? "—"} · ${fmtDate(it.created_at)}</div>
                  </div>
                </div>`).join("")}
            </div>` : `<div class="text-slate-500">Событий нет.</div>`}
        </div>
      </div>
    `;

    root.querySelectorAll("button[data-cl-cdec]").forEach(b => {
      b.addEventListener("click", () => bumpClientClass(id, b.dataset.clCdec, -1));
    });
    root.querySelectorAll("button[data-cl-cinc]").forEach(b => {
      b.addEventListener("click", () => bumpClientClass(id, b.dataset.clCinc, +1));
    });
    $("#cdAddForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const key = $("#cdNewKey").value.trim();
      const val = Math.max(1, Number($("#cdNewVal").value || 1));
      if (!key) return;
      try {
        await api(`/business/clients/${id}/class`, { method: "POST", body: { class_key: key, set_value: val } });
        loadClientDetail(id);
      } catch (e) { toast(e.message, "error"); }
    });
  } catch (e) { root.innerHTML = `<div class="text-rose-400">${escapeHTML(e.message)}</div>`; }
}

async function bumpClientClass(clientId, key, delta) {
  try {
    await api(`/business/clients/${clientId}/class`, { method: "POST", body: { class_key: key, delta } });
    loadClientDetail(clientId);
  } catch (e) { toast(e.message, "error"); }
}

/* ------------------------------ Archive view --------------------------- */

async function renderArchive() {
  setHeader("Архив", "Восстановление мягко-удалённых диалогов");
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full space-y-4 max-w-4xl">
      <div class="card">
        <p class="text-sm text-slate-400 mb-3">При cleanup в режиме <b>archive</b> (по умолчанию в Настройках) сообщения и события переносятся в таблицы <code>*_archive</code>. Здесь можно восстановить переписки полностью или по фильтру.</p>
        <div class="flex items-center gap-3 text-sm">
          <label class="text-slate-400">Аккаунт ID:</label>
          <input id="arAcc" type="number" min="1" class="bg-ink-800 border border-ink-600 rounded px-2 py-1.5 text-slate-100 w-32" />
          <button id="arRefresh" class="px-3 py-1.5 rounded bg-accent-600 hover:bg-accent-500 text-white">Показать архив</button>
        </div>
      </div>
      <div class="card p-0 overflow-hidden">
        <table class="cb-table">
          <thead><tr>
            <th>Аккаунт</th><th>Peer</th><th class="text-right">Сообщений</th><th>Архивирован</th><th></th>
          </tr></thead>
          <tbody id="arBody"><tr><td colspan="5" class="text-center text-slate-500 py-8">Введите фильтр и нажмите «Показать архив».</td></tr></tbody>
        </table>
      </div>
    </div>
  `;
  $("#arRefresh").addEventListener("click", loadArchive);
}

async function loadArchive() {
  const tbody = $("#arBody");
  if (!tbody) return;
  const acc = $("#arAcc").value;
  const params = new URLSearchParams();
  if (acc) params.set("account_id", String(acc));
  params.set("limit", "200");
  try {
    const list = await api(`/business/archive/dialogs?${params.toString()}`);
    if (!list.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="text-center text-slate-500 py-8">Архив пуст.</td></tr>`;
      return;
    }
    tbody.innerHTML = list.map(d => `
      <tr>
        <td>${escapeHTML(d.account_title || `#${d.account_id}`)}</td>
        <td>${d.client_username ? '@' + escapeHTML(d.client_username) : d.peer_user_id}</td>
        <td class="text-right text-slate-300">${d.messages_count}</td>
        <td class="text-xs text-slate-400">${fmtDate(d.last_archived_at)}</td>
        <td class="text-right">
          <button class="px-2 py-1 rounded bg-emerald-700 hover:bg-emerald-600 text-xs"
            data-restore-acc="${d.account_id}" data-restore-peer="${d.peer_user_id}">↶ Восстановить</button>
        </td>
      </tr>
    `).join("");
    tbody.querySelectorAll("button[data-restore-acc]").forEach(b => {
      b.addEventListener("click", () => restoreArchive(Number(b.dataset.restoreAcc), Number(b.dataset.restorePeer)));
    });
  } catch (e) { toast(`Архив: ${e.message}`, "error"); }
}

async function restoreArchive(accountId, peerId) {
  if (!confirm(`Восстановить архивный диалог acc=${accountId} peer=${peerId}?`)) return;
  try {
    const r = await api(`/business/archive/restore`, {
      method: "POST",
      body: { account_id: accountId, peer_user_id: peerId },
    });
    toast(`Восстановлено: сообщений ${r.messages_restored}, событий ${r.interactions_restored}`, "success");
    loadArchive();
  } catch (e) { toast(`Ошибка: ${e.message}`, "error"); }
}

/* ------------------------------- Logs view ----------------------------- */

async function renderLogs() {
  setHeader("Логи", "События телеметрии (ingest от агентов бота)");
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full space-y-4">
      <div class="flex items-center gap-3 text-sm flex-wrap">
        <label class="text-slate-400">Канал:</label>
        <select id="logsChannel" class="bg-ink-800 border border-ink-600 rounded-md px-2 py-1 text-slate-100">
          <option value="ingest">ingest (dashboard/logs)</option>
          <option value="openrouter">openrouter (file log)</option>
        </select>
        <label class="text-slate-400">Уровень:</label>
        <select id="logsLevel" class="bg-ink-800 border border-ink-600 rounded-md px-2 py-1 text-slate-100">
          <option value="">все</option>
          <option value="info">info</option>
          <option value="warning">warning</option>
          <option value="error">error</option>
          <option value="critical">critical</option>
        </select>
        <label class="text-slate-400">Лимит:</label>
        <input id="logsLimit" type="number" value="100" min="1" max="500" class="w-20 bg-ink-800 border border-ink-600 rounded-md px-2 py-1 text-slate-100" />
        <input id="logsProvider" placeholder="provider (напр. openai)" class="hidden w-44 bg-ink-800 border border-ink-600 rounded-md px-2 py-1 text-slate-100" />
        <input id="logsModel" placeholder="model contains" class="hidden w-56 bg-ink-800 border border-ink-600 rounded-md px-2 py-1 text-slate-100" />
        <input id="logsPromptId" placeholder="prompt_id" class="hidden w-36 bg-ink-800 border border-ink-600 rounded-md px-2 py-1 text-slate-100" />
        <input id="logsQ" placeholder="поиск в сообщении" class="hidden w-56 bg-ink-800 border border-ink-600 rounded-md px-2 py-1 text-slate-100" />
        <button id="logsApply" class="px-3 py-1 rounded bg-accent-600 hover:bg-accent-500 text-white">Применить</button>
      </div>
      <div class="card p-0 overflow-hidden">
        <table class="cb-table">
          <thead><tr id="logsHeadRow"><th>Время</th><th>Уровень</th><th>Категория</th><th>Сообщение</th></tr></thead>
          <tbody id="logsBody"><tr><td colspan="4" class="text-center text-slate-500 py-8">Загрузка…</td></tr></tbody>
        </table>
      </div>
    </div>
  `;
  const toggleMode = () => {
    const ch = ($("#logsChannel")?.value || "ingest");
    const openrouter = ch === "openrouter";
    ["#logsProvider", "#logsModel", "#logsPromptId", "#logsQ"].forEach(sel => {
      const el = $(sel);
      if (!el) return;
      el.classList.toggle("hidden", !openrouter);
    });
    const head = $("#logsHeadRow");
    if (head) {
      head.innerHTML = openrouter
        ? "<th>Время</th><th>Уровень</th><th>Provider/Model</th><th>Prompt</th><th>Сообщение</th>"
        : "<th>Время</th><th>Уровень</th><th>Категория</th><th>Сообщение</th>";
    }
  };
  $("#logsChannel").addEventListener("change", () => { toggleMode(); loadLogs(); });
  $("#logsApply").addEventListener("click", () => loadLogs());
  toggleMode();
  await loadLogs();
}

async function loadLogs() {
  const channel = ($("#logsChannel")?.value || "ingest").trim();
  const level = $("#logsLevel").value;
  const limit = $("#logsLimit").value || 100;
  try {
    let list = [];
    if (channel === "openrouter") {
      const provider = ($("#logsProvider")?.value || "").trim();
      const model = ($("#logsModel")?.value || "").trim();
      const promptId = ($("#logsPromptId")?.value || "").trim();
      const q = ($("#logsQ")?.value || "").trim();
      const qs = new URLSearchParams();
      qs.set("limit", String(limit));
      if (level) qs.set("level", level);
      if (provider) qs.set("provider", provider);
      if (model) qs.set("model", model);
      if (promptId) qs.set("prompt_id", promptId);
      if (q) qs.set("q", q);
      list = await api(`/dashboard/logs/openrouter?${qs.toString()}`);
    } else {
      const qs = `limit=${encodeURIComponent(limit)}` + (level ? `&level=${encodeURIComponent(level)}` : "");
      list = await api(`/dashboard/logs?${qs}`);
    }
    const tbody = $("#logsBody");
    if (!list.length) {
      tbody.innerHTML = `<tr><td colspan="4" class="text-center text-slate-500 py-8">Нет записей.</td></tr>`;
      return;
    }
    if (channel === "openrouter") {
      tbody.innerHTML = list.map(l => `
        <tr>
          <td class="text-xs text-slate-400 whitespace-nowrap">${fmtDate(l.created_at)}</td>
          <td>${logLevelPill(l.level)}</td>
          <td class="text-slate-300">
            <span class="text-slate-400">${escapeHTML(l.provider || "—")}</span>
            <span class="text-slate-500">/</span>
            <span class="text-slate-200">${escapeHTML(l.model || "—")}</span>
          </td>
          <td class="text-xs text-slate-400">${escapeHTML(l.prompt_id || "—")}</td>
          <td class="text-slate-200">${escapeHTML(l.message)}</td>
        </tr>
      `).join("");
    } else {
      tbody.innerHTML = list.map(l => `
        <tr>
          <td class="text-xs text-slate-400 whitespace-nowrap">${fmtDate(l.created_at)}</td>
          <td>${logLevelPill(l.level)}</td>
          <td class="text-slate-300">${escapeHTML(l.category)}${l.code ? ` <span class="text-slate-500">(${escapeHTML(l.code)})</span>` : ''}</td>
          <td class="text-slate-200">${escapeHTML(l.message)}</td>
        </tr>
      `).join("");
    }
  } catch (e) {
    toast(`Ошибка логов: ${e.message}`, "error");
  }
}
function logLevelPill(level) {
  const map = { info: "pill-blue", warning: "pill-amber", error: "pill-red", critical: "pill-red" };
  return `<span class="pill ${map[level] || "pill-gray"}">${escapeHTML(level)}</span>`;
}

/* ---------------------------- Settings view ---------------------------- */

async function renderSettings() {
  setHeader("Настройки", "Глобальные настройки инстанса, ключи и обслуживание");
  const readOnly = isReadOnlyRole();
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full space-y-6">

      <div class="card">
        <h3 class="font-semibold mb-1">Инстанс</h3>
        <p class="text-sm text-slate-400 mb-4">Глобальные переключатели бота. NULL в БД = используется значение из <code>.env</code> при старте.</p>
        <div id="instanceCard" class="text-sm text-slate-400">Загрузка…</div>
      </div>

      <div class="card">
        <h3 class="font-semibold mb-1">Ключ OpenRouter</h3>
        <p class="text-sm text-slate-400 mb-4">Шифруется Fernet-ключом из <code>OPENROUTER_KEY_ENCRYPTION_KEY</code>. Без этой переменной значение хранится в открытом виде с префиксом <code>p:</code>. ${readOnly ? "Роль read-only: изменение секрета недоступно." : ""}</p>
        <div id="orKeyCard" class="text-sm text-slate-400">Загрузка…</div>
      </div>

      <div class="card">
        <h3 class="font-semibold mb-1">Очистка диалогов</h3>
        <p class="text-sm text-slate-400 mb-4">Удаление выполняется батчами с проверкой ограничений. Системные таблицы (логи рассылок, MailingLog, аккаунты, прокси, классы клиентов) <b>не</b> удаляются — только переписки и client_interactions. ${readOnly ? "Роль read-only: cleanup недоступен." : ""}</p>
        <form id="cleanupForm" class="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
          <label class="block">
            <span class="text-slate-400 text-xs">Аккаунт ID (опц.)</span>
            <input name="account_id" type="number" min="1" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">Peer User ID (опц.)</span>
            <input name="peer_user_id" type="number" min="1" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">Старше N дней</span>
            <input name="older_than_days" type="number" min="1" max="3650" placeholder="например, 30" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">Классы клиентов (через запятую)</span>
            <input name="classes" type="text" placeholder="dead, bl, decline" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">Режим</span>
            <select name="mode" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
              <option value="archive" selected>archive — переносить в *_archive (восстановимо)</option>
              <option value="hard">hard — физически удалить</option>
            </select>
          </label>
          <label class="flex items-center gap-2 col-span-full text-slate-300">
            <input name="dry_run" type="checkbox" class="rounded border-ink-600 bg-ink-800" checked />
            Только посчитать (dry-run, без удаления)
          </label>
          <div class="col-span-full flex items-center gap-3">
            <button class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white" type="submit">Запустить</button>
            <a href="#/archive" class="text-sm text-slate-400 hover:text-slate-200">→ Архив для восстановления</a>
            <span id="cleanupResult" class="text-sm text-slate-400"></span>
          </div>
        </form>
      </div>

      <div class="card">
        <h3 class="font-semibold mb-1">Аккаунт</h3>
        <p class="text-sm text-slate-400 mb-2">Текущий пользователь: <span class="text-slate-200">${escapeHTML(state.user?.username || "—")}</span></p>
        <form id="myPassForm" class="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm mb-4">
          <label class="block">
            <span class="text-slate-400 text-xs">Текущий пароль</span>
            <input name="current_password" type="password" autocomplete="current-password"
                   class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">Новый пароль</span>
            <input name="new_password" type="password" autocomplete="new-password" minlength="6"
                   class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
          </label>
          <div class="flex items-end gap-3">
            <button type="submit" class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white">Сменить пароль</button>
            <span id="myPassMsg" class="text-xs text-slate-400"></span>
          </div>
        </form>

          <div class="border-t border-ink-700 pt-4">
          <h4 class="font-medium text-slate-200 mb-2">Операторы панели</h4>
            ${readOnly ? `<p class="text-sm text-slate-500 mb-3">Роль read-only: управление операторами недоступно.</p>` : ""}
          <form id="opCreateForm" class="grid grid-cols-1 md:grid-cols-4 gap-3 text-sm mb-4">
            <label class="block">
              <span class="text-slate-400 text-xs">Логин</span>
              <input name="username" minlength="3" required
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">Пароль</span>
              <input name="password" type="password" minlength="6" required
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">Роль</span>
              <select name="role" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
                <option value="tenant_viewer">tenant_viewer (read-only)</option>
                <option value="tenant_admin">tenant_admin (оператор)</option>
              </select>
            </label>
            <div class="flex items-end gap-3">
              <button type="submit" class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white">Создать</button>
              <span id="opCreateMsg" class="text-xs text-slate-400"></span>
            </div>
          </form>
          <div class="card p-0 overflow-hidden">
            <table class="cb-table">
              <thead><tr><th>ID</th><th>Username</th><th>Role</th><th>Tenant</th><th>Создан</th><th></th></tr></thead>
              <tbody id="opUsersBody"><tr><td colspan="6" class="text-center text-slate-500 py-6">Загрузка…</td></tr></tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  `;

  $("#cleanupForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (readOnly) {
      toast("Роль read-only: cleanup запрещен", "error");
      return;
    }
    const fd = new FormData(ev.currentTarget);
    const body = {};
    const acc = fd.get("account_id"); if (acc) body.account_id = Number(acc);
    const peer = fd.get("peer_user_id"); if (peer) body.peer_user_id = Number(peer);
    const days = fd.get("older_than_days"); if (days) body.older_than_days = Number(days);
    const cls = (fd.get("classes") || "").toString().trim();
    if (cls) body.classes = cls.split(",").map(s => s.trim()).filter(Boolean);
    body.dry_run = !!fd.get("dry_run");
    body.mode = (fd.get("mode") || "archive").toString();

    const out = $("#cleanupResult");
    out.textContent = "…";
    try {
      const r = await api("/business/cleanup/v2", { method: "POST", body });
      const arch = (r.messages_archived || r.interactions_archived)
        ? ` · в архив: ${r.messages_archived}/${r.interactions_archived}`
        : "";
      out.textContent = `${body.dry_run ? "[DRY] " : ""}[${r.mode}] диалогов: ${r.dialogs_deleted}, сообщений: ${r.messages_deleted}, событий: ${r.interactions_deleted}${arch}`;
      out.className = "text-sm " + (body.dry_run ? "text-amber-300" : "text-emerald-300");
      toast(body.dry_run ? "Подсчёт готов" : `Cleanup [${r.mode}] выполнен`, "success");
    } catch (e) {
      out.textContent = e.message;
      out.className = "text-sm text-rose-400";
    }
  });

  $("#myPassForm")?.addEventListener("submit", onChangeMyPassword);
  if (!readOnly) {
    $("#opCreateForm")?.addEventListener("submit", onCreateOperator);
    await loadOperators();
  } else {
    const opBody = $("#opUsersBody");
    if (opBody) {
      opBody.innerHTML = `<tr><td colspan="6" class="text-center text-slate-500 py-6">Недоступно для роли read-only.</td></tr>`;
    }
    $("#opCreateForm")?.querySelectorAll("input,select,button").forEach((el) => { el.disabled = true; });
    $("#cleanupForm")?.querySelectorAll("input,select,button").forEach((el) => { el.disabled = true; });
  }

  await loadInstanceSettings();
}

async function onChangeMyPassword(ev) {
  ev.preventDefault();
  const fd = new FormData(ev.currentTarget);
  const body = {
    current_password: (fd.get("current_password") || "").toString(),
    new_password: (fd.get("new_password") || "").toString(),
  };
  const out = $("#myPassMsg");
  out.textContent = "…";
  try {
    await api("/admin/me/password", { method: "POST", body });
    out.textContent = "ok";
    out.className = "text-xs text-emerald-300";
    toast("Пароль обновлён", "success");
    ev.currentTarget.reset();
  } catch (e) {
    out.textContent = e.message;
    out.className = "text-xs text-rose-400";
  }
}

async function onCreateOperator(ev) {
  ev.preventDefault();
  const fd = new FormData(ev.currentTarget);
  const body = {
    username: (fd.get("username") || "").toString().trim(),
    password: (fd.get("password") || "").toString(),
    role: (fd.get("role") || "tenant_viewer").toString(),
  };
  const out = $("#opCreateMsg");
  out.textContent = "…";
  try {
    const r = await api("/admin/users/create", { method: "POST", body });
    out.textContent = `ok (#${r.id})`;
    out.className = "text-xs text-emerald-300";
    toast(`Пользователь ${r.username} создан`, "success");
    ev.currentTarget.reset();
    await loadOperators();
  } catch (e) {
    out.textContent = e.message;
    out.className = "text-xs text-rose-400";
  }
}

async function loadOperators() {
  const tbody = $("#opUsersBody");
  if (!tbody) return;
  try {
    const users = await api("/admin/users");
    if (!users.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="text-center text-slate-500 py-6">Нет пользователей.</td></tr>`;
      return;
    }
    tbody.innerHTML = users.map(u => `
      <tr>
        <td class="text-slate-500">#${u.id}</td>
        <td class="text-slate-100">${escapeHTML(u.username)}</td>
        <td><span class="pill ${u.role === "super_admin" ? "pill-red" : u.role === "tenant_admin" ? "pill-amber" : "pill-gray"}">${escapeHTML(u.role)}</span></td>
        <td class="text-xs text-slate-400">${u.tenant_id}</td>
        <td class="text-xs text-slate-400">${fmtDate(u.created_at)}</td>
        <td class="text-right">
          <button data-op-reset="${u.id}" data-op-user="${escapeHTML(u.username)}"
            class="px-2 py-1 rounded bg-ink-700 hover:bg-ink-600 text-xs">Сброс пароля</button>
        </td>
      </tr>
    `).join("");
    tbody.querySelectorAll("button[data-op-reset]").forEach((b) => {
      b.addEventListener("click", async () => {
        const uid = Number(b.dataset.opReset);
        const uname = b.dataset.opUser || `#${uid}`;
        const pw = prompt(`Новый пароль для ${uname}:`);
        if (!pw) return;
        try {
          await api(`/admin/users/${uid}/password`, {
            method: "POST",
            body: { new_password: pw },
          });
          toast(`Пароль обновлён: ${uname}`, "success");
        } catch (e) {
          toast(`Ошибка: ${e.message}`, "error");
        }
      });
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="text-center text-rose-400 py-6">${escapeHTML(e.message)}</td></tr>`;
  }
}

async function loadInstanceSettings() {
  const inst = $("#instanceCard");
  const ork = $("#orKeyCard");
  if (!inst || !ork) return;
  try {
    const s = await api("/business/instance/settings");
    inst.innerHTML = `
      <form id="instanceForm" class="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
        <label class="block">
          <span class="text-slate-400 text-xs">Нейрочат включён глобально</span>
          <select name="neurochat_enabled" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
            <option value="">из .env (${s.neurochat_enabled_effective ? "включён" : "выключен"})</option>
            <option value="true" ${s.neurochat_enabled_db === true ? "selected" : ""}>Принудительно ВКЛ</option>
            <option value="false" ${s.neurochat_enabled_db === false ? "selected" : ""}>Принудительно ВЫКЛ</option>
          </select>
        </label>
        <label class="block">
          <span class="text-slate-400 text-xs">Базовый UTC-сдвиг для {date}/{time} (часы)</span>
          <input name="mailing_base_utc_offset" type="number" min="-12" max="14"
                 value="${s.mailing_base_utc_offset_db ?? ""}"
                 placeholder="из .env (${s.mailing_base_utc_offset_effective})"
                 class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
        </label>
        <div class="col-span-full flex items-center gap-3">
          <button class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white" type="submit">Сохранить</button>
          <button id="instanceResetBtn" type="button" class="px-3 py-2 rounded-lg bg-ink-700 hover:bg-ink-600 border border-ink-600 text-slate-200 text-sm">Сбросить оба к .env</button>
          <span id="instanceMsg" class="text-xs text-slate-400"></span>
        </div>
      </form>
    `;
    $("#instanceForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const fd = new FormData(ev.currentTarget);
      const body = {};
      const nc = fd.get("neurochat_enabled");
      if (nc === "true") body.neurochat_enabled = true;
      else if (nc === "false") body.neurochat_enabled = false;
      else body.reset_neurochat_enabled = true;
      const off = (fd.get("mailing_base_utc_offset") || "").toString().trim();
      if (off === "") body.reset_mailing_base_utc_offset = true;
      else body.mailing_base_utc_offset = Number(off);
      const out = $("#instanceMsg");
      out.textContent = "…";
      try {
        await api("/business/instance/settings", { method: "PATCH", body });
        toast("Инстанс обновлён", "success");
        await loadInstanceSettings();
      } catch (e) {
        out.textContent = e.message;
        out.className = "text-xs text-rose-400";
      }
    });
    $("#instanceResetBtn").addEventListener("click", async () => {
      try {
        await api("/business/instance/settings", {
          method: "PATCH",
          body: { reset_neurochat_enabled: true, reset_mailing_base_utc_offset: true },
        });
        toast("Сброшено к .env", "success");
        await loadInstanceSettings();
      } catch (e) {
        toast(e.message, "error");
      }
    });

    const encBadge = s.openrouter_key_encrypted
      ? `<span class="text-xs px-2 py-0.5 rounded bg-emerald-700/30 text-emerald-300 border border-emerald-700/40">шифр</span>`
      : (s.openrouter_key_set
        ? `<span class="text-xs px-2 py-0.5 rounded bg-amber-700/30 text-amber-300 border border-amber-700/40">plain</span>`
        : `<span class="text-xs px-2 py-0.5 rounded bg-slate-700/40 text-slate-400 border border-slate-600/40">не задан</span>`);
    ork.innerHTML = `
      <div class="flex items-center gap-3 mb-3">
        <span class="text-slate-200 font-mono">${escapeHTML(s.openrouter_key_masked || "—")}</span>
        ${encBadge}
        ${s.openrouter_key_set ? `<button id="orKeyDelete" class="ml-auto text-xs text-rose-400 hover:text-rose-300">Удалить</button>` : ""}
      </div>
      <form id="orKeyForm" class="flex items-center gap-2">
        <input name="key" type="password" placeholder="sk-or-v1-…"
               class="flex-1 bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
        <button type="submit" class="px-3 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white text-sm">Сохранить ключ</button>
      </form>
      <p class="text-xs text-slate-500 mt-2">Ключ применяется при следующем запросе нейрочата (без рестарта бота).</p>
    `;
    $("#orKeyForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const fd = new FormData(ev.currentTarget);
      const key = (fd.get("key") || "").toString().trim();
      if (!key) { toast("Пустой ключ", "warning"); return; }
      try {
        await api("/business/instance/openrouter-key", { method: "POST", body: { key } });
        toast("Ключ сохранён", "success");
        await loadInstanceSettings();
      } catch (e) { toast(e.message, "error"); }
    });
    const delBtn = $("#orKeyDelete");
    if (delBtn) {
      delBtn.addEventListener("click", async () => {
        if (!confirm("Удалить ключ OpenRouter? Нейрочат не сможет генерировать ответы.")) return;
        try {
          await api("/business/instance/openrouter-key", { method: "DELETE" });
          toast("Ключ удалён", "success");
          await loadInstanceSettings();
        } catch (e) { toast(e.message, "error"); }
      });
    }
  } catch (e) {
    inst.innerHTML = `<div class="text-rose-400">${escapeHTML(e.message)}</div>`;
    ork.innerHTML = "";
  }
}

/* ----------------------------- Groups view ----------------------------- */

async function renderGroups(groupIdStr) {
  setHeader("Группы аккаунтов", "Объединение userbot-аккаунтов в пулы для рассылок");
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full grid grid-cols-1 lg:grid-cols-3 gap-4">
      <div class="card lg:col-span-1">
        <div class="flex items-center justify-between mb-3">
          <h3 class="font-semibold">Группы</h3>
          <button id="grpRefresh" class="text-xs text-slate-400 hover:text-slate-200">⟳</button>
        </div>
        <form id="grpCreateForm" class="flex gap-2 mb-4">
          <input name="name" placeholder="Название (например, USA)"
                 class="flex-1 bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 text-sm" />
          <button type="submit" class="px-3 py-2 rounded-md bg-accent-600 hover:bg-accent-500 text-white text-sm">+ Создать</button>
        </form>
        <div id="grpList" class="space-y-1 text-sm">Загрузка…</div>
      </div>
      <div class="card lg:col-span-2">
        <div id="grpDetail" class="text-slate-500 text-sm">Выберите группу слева, чтобы изменить состав.</div>
      </div>
    </div>
  `;
  $("#grpRefresh").addEventListener("click", () => loadGroupsList(groupIdStr ? Number(groupIdStr) : null));
  $("#grpCreateForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.currentTarget);
    const name = (fd.get("name") || "").toString().trim();
    if (!name) return;
    try {
      const g = await api("/business/groups", { method: "POST", body: { name } });
      toast(`Группа «${g.name}» создана`, "success");
      ev.currentTarget.reset();
      window.location.hash = `#/groups/${g.id}`;
    } catch (e) { toast(e.message, "error"); }
  });
  await loadGroupsList(groupIdStr ? Number(groupIdStr) : null);
}

async function loadGroupsList(activeId = null) {
  const list = $("#grpList");
  if (!list) return;
  try {
    const groups = await api("/business/groups");
    if (!groups.length) {
      list.innerHTML = `<div class="text-slate-500 text-xs">Групп ещё нет.</div>`;
    } else {
      list.innerHTML = groups.map(g => `
        <a href="#/groups/${g.id}" data-gid="${g.id}"
           class="block px-3 py-2 rounded-md ${activeId === g.id ? 'bg-accent-600 text-white' : 'hover:bg-ink-700 text-slate-200'}">
          <div class="flex items-center justify-between">
            <span class="font-medium">${escapeHTML(g.name)}</span>
            <span class="text-xs ${activeId === g.id ? 'text-white/80' : 'text-slate-400'}">${g.accounts_count}</span>
          </div>
        </a>
      `).join("");
    }
    if (activeId) {
      await loadGroupDetail(activeId);
    } else {
      const det = $("#grpDetail");
      if (det) det.innerHTML = `<div class="text-slate-500 text-sm">Выберите группу слева, чтобы изменить состав.</div>`;
    }
  } catch (e) {
    list.innerHTML = `<div class="text-rose-400 text-sm">${escapeHTML(e.message)}</div>`;
  }
}

async function loadGroupDetail(groupId) {
  const det = $("#grpDetail");
  if (!det) return;
  det.innerHTML = `<div class="text-slate-500 text-sm">Загрузка…</div>`;
  try {
    const [groups, members, allAccounts] = await Promise.all([
      api("/business/groups"),
      api(`/business/groups/${groupId}/accounts`),
      api(`/business/accounts`),
    ]);
    const g = groups.find(x => x.id === groupId);
    if (!g) {
      det.innerHTML = `<div class="text-rose-400 text-sm">Группа не найдена.</div>`;
      return;
    }
    const memberIds = new Set(members.map(m => m.id));
    const checkboxes = (allAccounts || []).map(a => `
      <label class="flex items-center gap-2 px-2 py-1 rounded hover:bg-ink-700 text-sm">
        <input type="checkbox" data-aid="${a.id}" ${memberIds.has(a.id) ? "checked" : ""}
               class="rounded border-ink-600 bg-ink-800" />
        <span>${escapeHTML(a.list_label || a.username || a.phone || `#${a.id}`)}</span>
        <span class="text-xs text-slate-500">${escapeHTML(a.username || a.phone || '')}</span>
      </label>
    `).join("");
    det.innerHTML = `
      <div class="flex items-center justify-between mb-4">
        <div>
          <h3 class="font-semibold text-white">${escapeHTML(g.name)}</h3>
          <p class="text-xs text-slate-500">id=${g.id} · аккаунтов: ${g.accounts_count}</p>
        </div>
        <div class="flex gap-2">
          <button id="grpRenameBtn" class="px-3 py-1.5 rounded-md bg-ink-700 hover:bg-ink-600 text-sm">✎ Переименовать</button>
          <button id="grpDeleteBtn" class="px-3 py-1.5 rounded-md bg-rose-700 hover:bg-rose-600 text-sm text-white">🗑 Удалить</button>
        </div>
      </div>
      <div class="mb-3 flex items-center justify-between">
        <span class="text-sm text-slate-400">Аккаунты в группе:</span>
        <button id="grpSaveBtn" class="px-3 py-1.5 rounded-md bg-accent-600 hover:bg-accent-500 text-sm text-white">💾 Сохранить состав</button>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-1 max-h-[60vh] overflow-y-auto cb-scroll">${checkboxes}</div>
    `;
    $("#grpRenameBtn").addEventListener("click", async () => {
      const name = prompt("Новое имя группы:", g.name);
      if (!name) return;
      try {
        await api(`/business/groups/${groupId}`, { method: "PATCH", body: { name } });
        toast("Переименовано", "success");
        await loadGroupsList(groupId);
      } catch (e) { toast(e.message, "error"); }
    });
    $("#grpDeleteBtn").addEventListener("click", async () => {
      if (!confirm(`Удалить группу «${g.name}»? Аккаунты в ней останутся, но потеряют привязку.`)) return;
      try {
        await api(`/business/groups/${groupId}`, { method: "DELETE" });
        toast("Удалено", "success");
        window.location.hash = "#/groups";
      } catch (e) { toast(e.message, "error"); }
    });
    $("#grpSaveBtn").addEventListener("click", async () => {
      const ids = [];
      det.querySelectorAll('input[type="checkbox"][data-aid]').forEach(c => {
        if (c.checked) ids.push(Number(c.dataset.aid));
      });
      try {
        await api(`/business/groups/${groupId}/accounts`, {
          method: "PUT",
          body: { account_ids: ids },
        });
        toast("Состав группы обновлён", "success");
        await loadGroupsList(groupId);
      } catch (e) { toast(e.message, "error"); }
    });
  } catch (e) {
    det.innerHTML = `<div class="text-rose-400 text-sm">${escapeHTML(e.message)}</div>`;
  }
}

/* ----------------------------- Proxies view ---------------------------- */

async function renderProxies() {
  setHeader("Прокси", "SOCKS5/HTTP — пулы соединений для аккаунтов");
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full space-y-4">
      <div class="card">
        <div class="flex items-center justify-between mb-3">
          <h3 class="font-semibold">Список прокси</h3>
          <div class="flex gap-2">
            <button id="prxRefresh" class="px-3 py-1.5 rounded-md bg-ink-700 hover:bg-ink-600 text-sm">⟳</button>
            <button id="prxNewBtn" class="px-3 py-1.5 rounded-md bg-accent-600 hover:bg-accent-500 text-sm text-white">+ Добавить</button>
          </div>
        </div>
        <div id="prxTable" class="text-sm text-slate-400">Загрузка…</div>
      </div>
      <div id="prxFormCard" class="card hidden">
        <h3 class="font-semibold mb-3" id="prxFormTitle">Новый прокси</h3>
        <div id="prxForm"></div>
      </div>
    </div>
  `;
  $("#prxRefresh").addEventListener("click", () => loadProxiesTable());
  $("#prxNewBtn").addEventListener("click", () => openProxyForm(null));
  await loadProxiesTable();
}

async function loadProxiesTable() {
  const tbl = $("#prxTable");
  if (!tbl) return;
  try {
    const list = await api("/business/proxies");
    if (!list.length) {
      tbl.innerHTML = `<div class="text-slate-500 text-xs">Прокси пока нет. Добавьте через «+ Добавить».</div>`;
      return;
    }
    tbl.innerHTML = `
      <table class="cb-table">
        <thead>
          <tr>
            <th>ID</th><th>Имя</th><th>Тип</th><th>Хост:Порт</th>
            <th>Логин</th><th>Группа</th><th>Аккаунтов</th>
            <th>Статус</th><th>Проверка</th><th></th>
          </tr>
        </thead>
        <tbody>
          ${list.map(p => `
            <tr data-pid="${p.id}">
              <td class="text-slate-500">#${p.id}</td>
              <td class="font-medium text-slate-100">${escapeHTML(p.name)}</td>
              <td><span class="pill pill-gray">${p.proxy_type}</span></td>
              <td class="text-slate-300 font-mono text-xs">${escapeHTML(p.host)}:${p.port}</td>
              <td class="text-slate-400 text-xs">${escapeHTML(p.username || '—')}</td>
              <td class="text-slate-400">${escapeHTML(p.group_name || '—')}</td>
              <td class="text-right text-slate-300">${p.accounts_count}</td>
              <td>
                ${p.is_active
                  ? (p.is_working
                      ? `<span class="pill pill-green">ok</span>`
                      : `<span class="pill pill-red">fail</span>`)
                  : `<span class="pill pill-gray">off</span>`}
              </td>
              <td class="text-xs text-slate-500">${p.last_checked ? fmtRelative(p.last_checked) : '—'}</td>
              <td class="text-right whitespace-nowrap">
                <button data-act="test" class="px-2 py-1 rounded bg-ink-700 hover:bg-ink-600 text-xs mr-1">Тест</button>
                <button data-act="edit" class="px-2 py-1 rounded bg-ink-700 hover:bg-ink-600 text-xs mr-1">✎</button>
                <button data-act="delete" class="px-2 py-1 rounded bg-rose-700 hover:bg-rose-600 text-xs text-white">🗑</button>
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
    tbl.querySelectorAll("tr[data-pid]").forEach(tr => {
      const pid = Number(tr.dataset.pid);
      tr.querySelector('[data-act="test"]').addEventListener("click", async (ev) => {
        const btn = ev.currentTarget;
        btn.disabled = true; btn.textContent = "…";
        try {
          const r = await api(`/business/proxies/${pid}/test`, { method: "POST" });
          toast(`#${pid} ${r.ok ? '✓' : '✗'} ${r.elapsed_ms}ms — ${r.detail || ''}`, r.ok ? "success" : "warning");
          await loadProxiesTable();
        } catch (e) {
          toast(e.message, "error");
          btn.disabled = false; btn.textContent = "Тест";
        }
      });
      tr.querySelector('[data-act="edit"]').addEventListener("click", () => {
        openProxyForm(list.find(x => x.id === pid));
      });
      tr.querySelector('[data-act="delete"]').addEventListener("click", async () => {
        if (!confirm(`Удалить прокси #${pid}? У аккаунтов, использовавших его, prox_id обнулится.`)) return;
        try {
          await api(`/business/proxies/${pid}`, { method: "DELETE" });
          toast("Удалено", "success");
          await loadProxiesTable();
        } catch (e) { toast(e.message, "error"); }
      });
    });
  } catch (e) {
    tbl.innerHTML = `<div class="text-rose-400 text-sm">${escapeHTML(e.message)}</div>`;
  }
}

async function openProxyForm(existing) {
  const wrap = $("#prxFormCard");
  const form = $("#prxForm");
  $("#prxFormTitle").textContent = existing ? `Прокси #${existing.id}` : "Новый прокси";
  wrap.classList.remove("hidden");
  let groupOptions = `<option value="0">— без группы —</option>`;
  try {
    const groups = await api("/business/proxy-groups");
    groupOptions += (groups || []).map(g =>
      `<option value="${g.id}" ${existing && existing.group_id === g.id ? "selected" : ""}>${escapeHTML(g.name)} (${g.proxies_count})</option>`
    ).join("");
  } catch (_) {}
  form.innerHTML = `
    <form id="prxFormInner" class="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
      <label class="block">
        <span class="text-slate-400 text-xs">Имя</span>
        <input name="name" required value="${escapeHTML(existing?.name || "")}"
               class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
      </label>
      <label class="block">
        <span class="text-slate-400 text-xs">Хост</span>
        <input name="host" required value="${escapeHTML(existing?.host || "")}"
               class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
      </label>
      <label class="block">
        <span class="text-slate-400 text-xs">Порт</span>
        <input name="port" type="number" min="1" max="65535" required value="${existing?.port || 1080}"
               class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
      </label>
      <label class="block">
        <span class="text-slate-400 text-xs">Логин</span>
        <input name="username" value="${escapeHTML(existing?.username || "")}"
               class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
      </label>
      <label class="block">
        <span class="text-slate-400 text-xs">Пароль</span>
        <input name="password" placeholder="${existing ? "(оставить как есть)" : ""}"
               class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
      </label>
      <label class="block">
        <span class="text-slate-400 text-xs">Тип</span>
        <select name="proxy_type" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
          <option value="socks5" ${(!existing || existing.proxy_type === "socks5") ? "selected" : ""}>socks5</option>
          <option value="http" ${existing && existing.proxy_type === "http" ? "selected" : ""}>http</option>
        </select>
      </label>
      <label class="block">
        <span class="text-slate-400 text-xs">Группа</span>
        <select name="group_id" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">${groupOptions}</select>
      </label>
      <label class="flex items-center gap-2 mt-6 text-slate-300">
        <input name="is_active" type="checkbox" ${(!existing || existing.is_active) ? "checked" : ""}
               class="rounded border-ink-600 bg-ink-800" />
        Активен
      </label>
      <div class="md:col-span-3 flex items-center gap-3 mt-2">
        <button type="submit" class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white">${existing ? "Сохранить" : "Создать"}</button>
        <button type="button" id="prxFormCancel" class="px-3 py-2 rounded-lg bg-ink-700 hover:bg-ink-600 text-slate-200 text-sm">Отмена</button>
        <span id="prxFormMsg" class="text-xs text-slate-400"></span>
      </div>
    </form>
  `;
  $("#prxFormCancel").addEventListener("click", () => {
    wrap.classList.add("hidden");
    form.innerHTML = "";
  });
  $("#prxFormInner").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.currentTarget);
    const body = {
      name: (fd.get("name") || "").toString().trim(),
      host: (fd.get("host") || "").toString().trim(),
      port: Number(fd.get("port") || 0),
      username: (fd.get("username") || "").toString() || null,
      proxy_type: (fd.get("proxy_type") || "socks5").toString(),
      group_id: Number(fd.get("group_id") || 0),
      is_active: !!fd.get("is_active"),
    };
    const pwd = (fd.get("password") || "").toString();
    if (pwd) body.password = pwd;
    const out = $("#prxFormMsg");
    out.textContent = "…";
    try {
      if (existing) {
        await api(`/business/proxies/${existing.id}`, { method: "PATCH", body });
        toast("Сохранено", "success");
      } else {
        await api(`/business/proxies`, { method: "POST", body });
        toast("Создано", "success");
      }
      wrap.classList.add("hidden");
      form.innerHTML = "";
      await loadProxiesTable();
    } catch (e) {
      out.textContent = e.message;
      out.className = "text-xs text-rose-400";
    }
  });
}

/* --------------------------- Other helpers ----------------------------- */

function setHeader(title, sub = "") {
  $("#pageTitle").textContent = title;
  $("#pageSubtitle").textContent = sub;
}

function renderNotFound() {
  setHeader("Не найдено", "Раздел не существует");
  $("#pageRoot").innerHTML = `<div class="p-6 text-slate-400">Нет такого раздела.</div>`;
}

/* ---------------------------- Bootstrap -------------------------------- */

function bootstrap() {
  try {
    const splash = document.getElementById("bootSplash");
    if (splash) splash.style.display = "none";

    $("#loginForm").addEventListener("submit", loginFlow);
    $("#logoutBtn").addEventListener("click", () => handleLogout(false));
    $("#globalRefreshBtn").addEventListener("click", () => navigate(window.location.hash));

    if (state.token) enterApp();
    else handleLogout(true);
  } catch (err) {
    const box = document.getElementById("bootError");
    if (box) {
      box.style.display = "block";
      box.textContent = "Ошибка инициализации: " + (err && err.message ? err.message : String(err));
    }
    console.error("[bootstrap]", err);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", bootstrap);
} else {
  bootstrap();
}
