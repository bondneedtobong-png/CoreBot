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
    state.user = { username };
    localStorage.setItem(TOKEN_KEY, state.token);
    localStorage.setItem(USER_KEY, JSON.stringify(state.user));
    enterApp();
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
  $("#appShell").classList.add("hidden");
  $("#loginScreen").classList.remove("hidden");
  $("#loginScreen").classList.add("flex");
  if (!silent) toast("Сессия завершена");
}

function enterApp() {
  $("#loginScreen").classList.add("hidden");
  $("#loginScreen").classList.remove("flex");
  $("#appShell").classList.remove("hidden");
  $("#userBadge").textContent = state.user?.username || "—";
  openSSE();
  navigate(window.location.hash || "#/dashboard");
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

  switch (route) {
    case "dashboard": return renderDashboard();
    case "accounts":  return renderAccounts();
    case "dialogs":   return renderDialogs(segments[1], segments[2]);
    case "logs":      return renderLogs();
    case "settings":  return renderSettings();
    default: return renderNotFound();
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
  setHeader("Дашборд", "Сводка по системе и активности");
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full space-y-6">
      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        ${statCard("Аккаунты активны", "dashAgents", "—")}
        ${statCard("События 24ч", "dashEvents", "—")}
        ${statCard("Ошибки 24ч", "dashErrors", "—", "text-rose-400")}
        ${statCard("Открытых алертов", "dashAlerts", "—", "text-amber-400")}
      </div>
      <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div class="card lg:col-span-2">
          <div class="flex items-center justify-between mb-3">
            <h3 class="font-semibold">Live-поток сообщений</h3>
            <span class="text-xs text-slate-400">принято с момента входа: <span id="dashLive" class="text-slate-200 font-medium">${liveCounter}</span></span>
          </div>
          <div id="dashRecent" class="space-y-2 text-sm text-slate-400">Подождите событий…</div>
        </div>
        <div class="card">
          <h3 class="font-semibold mb-3">Агенты</h3>
          <ul id="dashAgentsList" class="space-y-2 text-sm">—</ul>
        </div>
      </div>
    </div>
  `;

  try {
    const summary = await api("/dashboard/summary");
    $("#dashAgents").textContent = summary.active_agents;
    $("#dashEvents").textContent = summary.events_24h;
    $("#dashErrors").textContent = summary.errors_24h;
    $("#dashAlerts").textContent = summary.alerts_open;
  } catch (e) { toast(`Не удалось загрузить summary: ${e.message}`, "error"); }

  try {
    const agents = await api("/dashboard/agents");
    $("#dashAgentsList").innerHTML = (agents || []).map(a => `
      <li class="flex items-center justify-between text-slate-300">
        <span>${escapeHTML(a.name)}</span>
        <span class="${a.is_online ? "pill pill-green" : "pill pill-gray"}">${a.is_online ? "online" : "offline"}</span>
      </li>
    `).join("") || `<li class="text-slate-500">Нет агентов.</li>`;
  } catch (e) { /* skip */ }

  // Кэшируем последние 20 live-сообщений в правом блоке
  const recentEl = $("#dashRecent");
  const recent = [];
  state.listeners.dashboardLive = (msg) => {
    recent.unshift(msg);
    if (recent.length > 20) recent.length = 20;
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

function statCard(label, id, valueDefault = "—", colorClass = "text-white") {
  return `
    <div class="card">
      <div class="text-xs uppercase tracking-wide text-slate-400">${label}</div>
      <div id="${id}" class="text-3xl font-semibold mt-1 ${colorClass}">${valueDefault}</div>
    </div>
  `;
}

/* ---------------------------- Accounts view ---------------------------- */

async function renderAccounts() {
  setHeader("Аккаунты", "Состояние аккаунтов и режим автоответа (AI / Manual)");
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full">
      <div class="card overflow-hidden p-0">
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
    </div>
  `;
  await refreshAccountsTable();
}

async function refreshAccountsTable() {
  try {
    const list = await api("/business/accounts");
    state.cache.accounts = list;
    state.cache.accountById = new Map(list.map(a => [a.id, a]));

    const tbody = $("#accountsBody");
    if (!tbody) return;
    if (!list.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="text-center text-slate-500 py-8">Нет аккаунтов в БД.</td></tr>`;
      return;
    }
    tbody.innerHTML = list.map(a => {
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
    <div class="grid grid-cols-12 h-full">
      <aside class="col-span-3 min-w-0 border-r border-ink-700 flex flex-col">
        <div class="px-4 py-3 border-b border-ink-700 flex items-center justify-between">
          <h3 class="font-medium text-slate-200 text-sm">Аккаунты</h3>
          <button id="dlgAccountsRefresh" class="text-xs text-slate-400 hover:text-slate-200">⟳</button>
        </div>
        <div id="dlgAccountsList" class="flex-1 overflow-y-auto cb-scroll">
          <div class="p-4 text-slate-500 text-sm">Загрузка…</div>
        </div>
      </aside>
      <section class="col-span-${peerId ? '4' : '9'} min-w-0 border-r border-ink-700 flex flex-col">
        <div class="px-4 py-3 border-b border-ink-700 flex items-center justify-between">
          <h3 id="dlgListTitle" class="font-medium text-slate-200 text-sm">${accountId ? `Диалоги аккаунта #${accountId}` : 'Выберите аккаунт'}</h3>
          <button id="dlgListRefresh" class="text-xs text-slate-400 hover:text-slate-200">⟳</button>
        </div>
        <div id="dlgList" class="flex-1 overflow-y-auto cb-scroll">
          ${accountId ? '<div class="p-4 text-slate-500 text-sm">Загрузка…</div>' : '<div class="p-4 text-slate-500 text-sm">Слева выберите аккаунт.</div>'}
        </div>
      </section>
      ${peerId ? `
        <section class="col-span-5 min-w-0 flex flex-col">
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

  $("#dlgAccountsRefresh").addEventListener("click", () => loadDialogsAccounts(accountId, peerId));
  if ($("#dlgListRefresh") && accountId) {
    $("#dlgListRefresh").addEventListener("click", () => loadDialogsList(accountId, peerId));
  }
  if (peerId) {
    $("#dlgDeleteBtn").addEventListener("click", () => deleteCurrentDialog(accountId, peerId));
  }

  await loadDialogsAccounts(accountId, peerId);
  if (accountId) await loadDialogsList(accountId, peerId);
  if (accountId && peerId) await loadDialogMessages(accountId, peerId);
}

async function loadDialogsAccounts(activeAccountId, activePeerId) {
  const el = $("#dlgAccountsList");
  if (!el) return;
  try {
    const list = state.cache.accounts || await api("/business/accounts");
    state.cache.accounts = list;
    state.cache.accountById = new Map(list.map(a => [a.id, a]));
    if (!list.length) {
      el.innerHTML = `<div class="p-4 text-slate-500 text-sm">Нет аккаунтов.</div>`;
      return;
    }
    el.innerHTML = list.map(a => {
      const title = a.list_label || a.username || a.phone || `#${a.id}`;
      const isActive = a.id === activeAccountId;
      const modePill = a.ai_mode === "MANUAL"
        ? '<span class="pill pill-amber">M</span>'
        : '<span class="pill pill-green">AI</span>';
      const pendingBadge = a.pending_outbound > 0
        ? `<span class="pill pill-amber">↑${a.pending_outbound}</span>` : '';
      return `
        <a href="#/dialogs/${a.id}" class="block px-4 py-2 border-b border-ink-700 ${isActive ? 'bg-ink-800' : 'hover:bg-ink-800/60'}">
          <div class="flex items-center justify-between gap-2">
            <div class="text-sm text-slate-100 truncate">${escapeHTML(title)}</div>
            <div class="flex items-center gap-1 shrink-0">${modePill}${pendingBadge}</div>
          </div>
          <div class="text-[11px] text-slate-500 truncate">диалогов: ${a.dialogs_count}</div>
        </a>
      `;
    }).join("");
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
    if (!list.length) {
      el.innerHTML = `<div class="text-slate-500 text-sm text-center">Сообщений пока нет.</div>`;
    } else {
      el.innerHTML = "";
      list.forEach(appendMessageToChat);
    }
    // messageMaxId считаем только по реальным neuro-сообщениям (положительные id).
    const realIds = list.filter((m) => m.source !== "queue" && (m.id || 0) > 0).map((m) => m.id);
    state.current.messageMaxId = realIds.length ? Math.max(...realIds) : 0;
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

/* ------------------------------- Logs view ----------------------------- */

async function renderLogs() {
  setHeader("Логи", "События телеметрии (ingest от агентов бота)");
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full space-y-4">
      <div class="flex items-center gap-3 text-sm">
        <label class="text-slate-400">Уровень:</label>
        <select id="logsLevel" class="bg-ink-800 border border-ink-600 rounded-md px-2 py-1 text-slate-100">
          <option value="">все</option>
          <option value="info">info</option>
          <option value="warning">warning</option>
          <option value="error">error</option>
          <option value="critical">critical</option>
        </select>
        <label class="text-slate-400 ml-4">Лимит:</label>
        <input id="logsLimit" type="number" value="100" min="1" max="500" class="w-20 bg-ink-800 border border-ink-600 rounded-md px-2 py-1 text-slate-100" />
        <button id="logsApply" class="px-3 py-1 rounded bg-accent-600 hover:bg-accent-500 text-white">Применить</button>
      </div>
      <div class="card p-0 overflow-hidden">
        <table class="cb-table">
          <thead><tr><th>Время</th><th>Уровень</th><th>Категория</th><th>Сообщение</th></tr></thead>
          <tbody id="logsBody"><tr><td colspan="4" class="text-center text-slate-500 py-8">Загрузка…</td></tr></tbody>
        </table>
      </div>
    </div>
  `;
  $("#logsApply").addEventListener("click", () => loadLogs());
  await loadLogs();
}

async function loadLogs() {
  const level = $("#logsLevel").value;
  const limit = $("#logsLimit").value || 100;
  try {
    const qs = `limit=${encodeURIComponent(limit)}` + (level ? `&level=${encodeURIComponent(level)}` : "");
    const list = await api(`/dashboard/logs?${qs}`);
    const tbody = $("#logsBody");
    if (!list.length) {
      tbody.innerHTML = `<tr><td colspan="4" class="text-center text-slate-500 py-8">Нет записей.</td></tr>`;
      return;
    }
    tbody.innerHTML = list.map(l => `
      <tr>
        <td class="text-xs text-slate-400 whitespace-nowrap">${fmtDate(l.created_at)}</td>
        <td>${logLevelPill(l.level)}</td>
        <td class="text-slate-300">${escapeHTML(l.category)}${l.code ? ` <span class="text-slate-500">(${escapeHTML(l.code)})</span>` : ''}</td>
        <td class="text-slate-200">${escapeHTML(l.message)}</td>
      </tr>
    `).join("");
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
  setHeader("Настройки", "Очистка БД и обслуживание");
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full space-y-6">
      <div class="card">
        <h3 class="font-semibold mb-1">Очистка диалогов</h3>
        <p class="text-sm text-slate-400 mb-4">Удаление выполняется батчами с проверкой ограничений. Системные таблицы (логи рассылок, MailingLog, аккаунты, прокси, классы клиентов) <b>не</b> удаляются — только переписки и client_interactions.</p>
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
          <label class="flex items-center gap-2 col-span-full text-slate-300">
            <input name="dry_run" type="checkbox" class="rounded border-ink-600 bg-ink-800" checked />
            Только посчитать (dry-run, без удаления)
          </label>
          <div class="col-span-full flex items-center gap-3">
            <button class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white" type="submit">Запустить</button>
            <span id="cleanupResult" class="text-sm text-slate-400"></span>
          </div>
        </form>
      </div>

      <div class="card">
        <h3 class="font-semibold mb-1">Аккаунт</h3>
        <p class="text-sm text-slate-400 mb-2">Текущий пользователь: <span class="text-slate-200">${escapeHTML(state.user?.username || "—")}</span></p>
        <p class="text-sm text-slate-500">Смена пароля и приглашение оператора — TODO в следующей итерации (см. corebot v2.md, Этап 6).</p>
      </div>
    </div>
  `;

  $("#cleanupForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.currentTarget);
    const body = {};
    const acc = fd.get("account_id"); if (acc) body.account_id = Number(acc);
    const peer = fd.get("peer_user_id"); if (peer) body.peer_user_id = Number(peer);
    const days = fd.get("older_than_days"); if (days) body.older_than_days = Number(days);
    const cls = (fd.get("classes") || "").toString().trim();
    if (cls) body.classes = cls.split(",").map(s => s.trim()).filter(Boolean);
    body.dry_run = !!fd.get("dry_run");

    const out = $("#cleanupResult");
    out.textContent = "…";
    try {
      const r = await api("/business/cleanup", { method: "POST", body });
      out.textContent = `${body.dry_run ? "[DRY] " : ""}диалогов: ${r.dialogs_deleted}, сообщений: ${r.messages_deleted}, событий: ${r.interactions_deleted}`;
      out.className = "text-sm " + (body.dry_run ? "text-amber-300" : "text-emerald-300");
      toast(body.dry_run ? "Подсчёт готов" : "Очистка выполнена", "success");
    } catch (e) {
      out.textContent = e.message;
      out.className = "text-sm text-rose-400";
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
