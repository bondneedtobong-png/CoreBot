/* ================================================================
   CoreBot Control Panel — vanilla SPA
   - Хеш-роутинг (Dashboard / Accounts / Dialogs / Logs / Settings)
   - JWT в localStorage, перезапрос токена не реализован (простая login-форма)
   - SSE-канал для live-сообщений из corebot.db
   ================================================================ */

const API = window.location.origin.replace(/\/$/, "");
const TOKEN_KEY = "cb.access_token";
const USER_KEY = "cb.user";
const DIALOG_SOUND_KEY = "cb.dialog_sound";

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
    soundEnabled: localStorage.getItem(DIALOG_SOUND_KEY) === "1",
    accountSearch: "",
    peerSearch: "",
    listRequestNo: 0,
    listOffset: 0,
    listRows: [],
    listContext: "",
    waitingOnly: false,
    unreadOnly: false,
    leftTab: "accounts", // accounts | groups
    selectedGroupId: null, // null = all
    selectedGroupName: "Все",
    groupAccountIds: null, // Set<int> | null
  },
  accounts: {
    q: "",
    sort: "id_desc",
    selectedIds: new Set(),
  },
  listeners: {},
  parsing: { tab: "channels" },
};

let dialogAudioContext = null;
let dialogSoundAt = 0;
let dialogsLiveRefreshTimer = null;
let dialogsSearchTimer = null;
let streamSessionRefreshTimer = null;
const mailingPromptUi = { mailingId: null, versionId: null, savedText: "" };

function unlockDialogAudio() {
  if (!state.dialogs.soundEnabled) return;
  const AudioContextType = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextType) return;
  try {
    dialogAudioContext ||= new AudioContextType();
    if (dialogAudioContext.state === "suspended") dialogAudioContext.resume().catch(() => {});
  } catch (error) {
    console.warn("Dialog sound unavailable", error);
  }
}

function playDialogSound() {
  if (!state.dialogs.soundEnabled || state.route !== "dialogs") return;
  if (Date.now() - dialogSoundAt < 750) return;
  unlockDialogAudio();
  if (!dialogAudioContext) return;
  try {
    const oscillator = dialogAudioContext.createOscillator();
    const gain = dialogAudioContext.createGain();
    oscillator.type = "sine";
    oscillator.frequency.value = 660;
    gain.gain.setValueAtTime(0.0001, dialogAudioContext.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.06, dialogAudioContext.currentTime + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.0001, dialogAudioContext.currentTime + 0.2);
    oscillator.connect(gain);
    gain.connect(dialogAudioContext.destination);
    oscillator.start();
    oscillator.stop(dialogAudioContext.currentTime + 0.21);
    dialogSoundAt = Date.now();
  } catch (error) {
    console.warn("Dialog sound unavailable", error);
  }
}

function scheduleDialogsLiveRefresh(accountId) {
  if (state.route !== "dialogs" || (state.current.accountId !== null && state.current.accountId !== accountId)) return;
  clearTimeout(dialogsLiveRefreshTimer);
  clearTimeout(dialogsSearchTimer);
  dialogsLiveRefreshTimer = setTimeout(() => {
    if (state.route === "dialogs" && (state.current.accountId === null || state.current.accountId === accountId)) {
      loadDialogsList(state.current.accountId, state.current.peerId, { refresh: true });
    }
  }, 250);
}

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

/* Единая ошибка валидации под полем: текст + иконка (не только цвет).
   Возвращает true, если ошибка выставлена. */
function setFieldError(input, message) {
  if (!input) return false;
  const form = input.closest("form") || input.parentElement;
  let err = form ? form.querySelector(`[data-err-for="${input.name || input.id}"]`) : null;
  if (!err && form) {
    err = document.createElement("div");
    err.className = "field-error";
    err.setAttribute("data-err-for", input.name || input.id);
    input.insertAdjacentElement("afterend", err);
  }
  if (message) {
    input.setAttribute("aria-invalid", "true");
    if (err) err.textContent = message;
    return true;
  }
  input.removeAttribute("aria-invalid");
  if (err) err.remove();
  return false;
}

/* Подтверждение разрушительного действия: что + объект. */
function confirmDanger(message) {
  return confirm(message);
}

/* Блокируем только кнопку/строку, а не всю панель. */
function setBusy(el, busy, busyText = "…") {
  if (!el) return;
  if (busy) {
    if (el.dataset.origText === undefined) el.dataset.origText = el.textContent;
    el.disabled = true;
    el.setAttribute("data-loading", "1");
    el.textContent = busyText;
  } else {
    el.disabled = false;
    el.removeAttribute("data-loading");
    if (el.dataset.origText !== undefined) {
      el.textContent = el.dataset.origText;
      delete el.dataset.origText;
    }
  }
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
    // Панель — локальный админ-UI: никакого HTTP-кэша API. Иначе после
    // POST-мутаций (тест прокси и т.п.) GET-список может вернуться старым
    // и таблица покажет несвежие СТАТУС/ПРОВЕРКА.
    cache: "no-store",
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
    const error = new Error(detail);
    error.status = r.status;
    throw error;
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
  const oldToken = state.token;
  if (oldToken) {
    fetch(API + "/auth/stream-session", {
      method: "DELETE", headers: { Authorization: `Bearer ${oldToken}` },
    }).catch(() => {});
  }
  state.token = "";
  state.user = null;
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
  closeSSE();
  clearTimeout(streamSessionRefreshTimer);
  streamSessionRefreshTimer = null;
  clearTimeout(dialogsLiveRefreshTimer);
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
  await refreshStreamSession();
  if (!state.token) return;
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

async function refreshStreamSession() {
  try {
    await api("/auth/stream-session", { method: "POST" });
    if (!state.sse) openSSE();
    return true;
  } catch (error) {
    setSSE("offline");
    return false;
  } finally {
    clearTimeout(streamSessionRefreshTimer);
    if (state.token) streamSessionRefreshTimer = setTimeout(refreshStreamSession, 10 * 60 * 1000);
  }
}

function openSSE() {
  closeSSE();
  if (!state.token) return;
  try {
    const url = `${API}/business/stream`;
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
  if (msg.role === "user") {
    playDialogSound();
  }
  scheduleDialogsLiveRefresh(state.route === "dialogs" && state.current.accountId === null ? null : msg.account_id);
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
    const active = a.dataset.route === route && (route !== "engagement" || a.getAttribute("href") === `#/engagement/${segments[1] || "comment"}`);
    a.classList.toggle("active", active);
    if (active) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  // Drawer: на узком экране закрываем меню после перехода.
  document.body.classList.remove("nav-open");
  const navToggle = $("#navToggle");
  if (navToggle) navToggle.setAttribute("aria-expanded", "false");
  const pageRoot = $("#pageRoot");
  if (pageRoot) {
    pageRoot.classList.toggle("dialogs-no-scroll", route === "dialogs");
  }

  switch (route) {
    case "dashboard": return renderDashboard();
    case "accounts":  return renderAccounts(segments[1]);
    case "safety":    return renderAccountSafety();
    case "engagement": return renderEngagement(segments[1] || "comment");
    case "dialogs":   return renderDialogs(segments[1], segments[2]);
    case "queue":     return renderQueue();
    case "parsing":   return renderParsing(segments[1]);
    case "mailings":  return renderMailings(segments[1]);
    case "links":     return renderLinks();
    case "clients":   return renderClients(segments[1]);
    case "groups":    return renderGroups(segments[1]);
    case "proxies":   return renderProxies(segments[1]);
    case "tdata-check": return renderTdataCheck();
    case "archive":   return renderArchive();
    case "logs":      return renderLogs();
    case "settings":  return renderSettings();
    case "guide":     return renderGuide();
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
            <option value="uncertain">требует проверки</option>
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
              <th><input id="qSelectAll" type="checkbox" aria-label="Выбрать все строки" class="rounded border-ink-600 bg-ink-800" ${readOnly ? "disabled" : ""} /></th>
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
          <button data-q-act="retry" data-qid="${q.queue_id}" data-q-status="${escapeHTML(q.status)}" aria-label="Повторить отправку #${q.queue_id}" class="px-2 py-1 rounded bg-emerald-700 hover:bg-emerald-600 text-xs ${readOnly ? "opacity-50 cursor-not-allowed" : ""}" ${readOnly || !["failed", "cancelled", "uncertain"].includes(q.status) ? "disabled" : ""}>↻</button>
          <button data-q-act="cancel" data-qid="${q.queue_id}" aria-label="Отменить отправку #${q.queue_id}" class="px-2 py-1 rounded bg-rose-700 hover:bg-rose-600 text-xs ml-1 ${readOnly ? "opacity-50 cursor-not-allowed" : ""}" ${readOnly || !["pending", "failed"].includes(q.status) ? "disabled" : ""}>✕</button>
        </td>
      </tr>
    `).join("");
    if (!readOnly) {
      tbody.querySelectorAll("button[data-q-act]").forEach((b) => {
        b.addEventListener("click", () => onQueueItemAction(Number(b.dataset.qid), b.dataset.qAct, b.dataset.qStatus));
      });
    }
  } catch (e) {
    toast(`Очередь: ${e.message}`, "error");
  }
}

async function onQueueItemAction(queueId, action, queueStatus) {
  if (!queueId || !action) return;
  if (isReadOnlyRole()) {
    toast("Роль read-only: действие запрещено", "error");
    return;
  }
  if (action === "cancel" && !confirmDanger(`Отменить отправку #${queueId}? Сообщение не будет доставлено.`)) return;
  if (action === "retry" && queueStatus === "uncertain" && !confirmDanger(`Исход отправки #${queueId} неизвестен. Сначала проверьте диалог в Telegram: повтор может отправить сообщение второй раз. Повторить?`)) return;
  const rowBtns = $$(`#qBody button[data-qid="${queueId}"]`);
  rowBtns.forEach((b) => { b.disabled = true; });
  try {
    if (action === "retry") {
      await api(`/business/queue/${queueId}/retry`, { method: "POST" });
    } else if (action === "cancel") {
      await api(`/business/queue/${queueId}/cancel`, { method: "POST", raw: true });
    }
    await loadQueueList();
  } catch (e) {
    toast(`Очередь ${action}: ${e.message}`, "error");
    rowBtns.forEach((b) => { b.disabled = isReadOnlyRole(); });
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
  if (action === "cancel" && !confirmDanger(`Отменить ${ids.length} выбранных отправок? Они не будут доставлены.`)) return;
  if (action === "retry" && ids.some((id) => $(`#qBody button[data-q-act="retry"][data-qid="${id}"]`)?.dataset.qStatus === "uncertain") &&
      !confirmDanger("Среди выбранных отправок есть сообщения с неизвестным исходом. Проверьте диалоги в Telegram: повтор может создать дубли. Повторить выбранные?")) return;
  const bulkBtn = action === "retry" ? $("#qBulkRetry") : $("#qBulkCancel");
  setBusy(bulkBtn, true, "…");
  try {
    const r = await api("/business/queue/bulk", {
      method: "POST",
      body: { action, queue_ids: ids },
    });
    toast(`Bulk ${action}: updated=${r.updated}, skipped=${r.skipped}`, "success");
    await loadQueueList();
  } catch (e) {
    toast(`Bulk ${action}: ${e.message}`, "error");
  } finally {
    setBusy(bulkBtn, false);
  }
}

/* ------------------------------ Parsing (Telegram) ---------------------- */

function _parsingTabValid(t) {
  return ["channels", "groups", "users", "catalog"].includes(t) ? t : "channels";
}

async function downloadParsingExport(path) {
  const url = path.startsWith("http") ? path : API + path;
  const r = await fetch(url, {
    headers: { Authorization: `Bearer ${state.token}`, Accept: "*/*" },
    cache: "no-store",
  });
  if (r.status === 401) {
    handleLogout(true);
    throw new Error("Unauthorized");
  }
  if (!r.ok) {
    let d = `HTTP ${r.status}`;
    try {
      const j = await r.json();
      if (j.detail) d = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch { /* plain */ }
    throw new Error(d);
  }
  const blob = await r.blob();
  const a = document.createElement("a");
  const name = (path.split("?")[0].split("/").pop() || "export.txt");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

function parserSizeFilters(kind, minRaw, maxRaw) {
  const parse = (raw, label) => {
    const value = String(raw ?? "").trim();
    if (!value) return null;
    if (!/^\d+$/.test(value) || !Number.isSafeInteger(Number(value))) {
      throw new Error(`${label}: укажите целое неотрицательное число.`);
    }
    return Number(value);
  };
  const label = kind === "groups" ? "Участники" : "Подписчики";
  const min = parse(minRaw, `${label} min`);
  const max = parse(maxRaw, `${label} max`);
  if (min != null && max != null && min > max) {
    throw new Error(`${label}: минимум не может превышать максимум.`);
  }
  return kind === "groups"
    ? { members_min: min, members_max: max }
    : { subscribers_min: min, subscribers_max: max };
}

async function renderParsing(tabSeg) {
  try { state.parsing.stream?.close(); } catch {}
  state.parsing.stream = null;
  const tab = _parsingTabValid(tabSeg || state.parsing.tab);
  state.parsing.tab = tab;
  setHeader("Парсинг", "Задачи Telethon (parser-worker) — каналы, группы, пользователи");
  const readOnly = isReadOnlyRole();
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full space-y-4">
      <div class="flex flex-wrap gap-2 text-sm">
        <button data-ptab="channels" class="px-3 py-1.5 rounded-lg border ${tab === "channels" ? "bg-accent-600 border-accent-500 text-white" : "bg-ink-800 border-ink-600 text-slate-200"}">Каналы</button>
        <button data-ptab="groups" class="px-3 py-1.5 rounded-lg border ${tab === "groups" ? "bg-accent-600 border-accent-500 text-white" : "bg-ink-800 border-ink-600 text-slate-200"}">Группы</button>
        <button data-ptab="users" class="px-3 py-1.5 rounded-lg border ${tab === "users" ? "bg-accent-600 border-accent-500 text-white" : "bg-ink-800 border-ink-600 text-slate-200"}">Пользователи</button>
        <button data-ptab="catalog" class="px-3 py-1.5 rounded-lg border ${tab === "catalog" ? "bg-accent-600 border-accent-500 text-white" : "bg-ink-800 border-ink-600 text-slate-200"}">Своя база площадок</button>
        <span class="text-xs text-slate-500 ml-auto self-center">Парсер запускается вместе с веб-панелью</span>
      </div>

      <div id="pFormCard" class="card space-y-3 text-sm"></div>

      <div class="card">
        <div class="flex items-center justify-between mb-2">
          <h3 class="font-semibold text-white">Задачи</h3>
          <span class="text-xs text-emerald-400">live</span>
        </div>
        <div class="overflow-x-auto">
          <table class="cb-table text-xs">
            <thead>
              <tr>
                <th>ID</th><th>Тип</th><th>Статус</th><th>%</th><th>Этап</th><th>Акк</th><th>Запрос</th><th>Found</th><th>Filt</th><th>Err</th><th>Создана</th><th></th>
              </tr>
            </thead>
            <tbody id="pTaskBody"><tr><td colspan="12" class="text-center text-slate-500 py-6">Загрузка…</td></tr></tbody>
          </table>
        </div>
      </div>

      <div class="card" id="pDetailCard" style="display:none">
        <div class="flex flex-wrap items-center gap-2 mb-2">
          <h3 class="font-semibold text-white">Задача #<span id="pDetailId">—</span></h3>
          <button id="pCancelTask" class="text-xs px-2 py-1 rounded bg-rose-800 border border-rose-600 ${readOnly ? "opacity-50 cursor-not-allowed" : ""}" ${readOnly ? "disabled" : ""}>Отменить</button>
          <div class="ml-auto flex flex-wrap gap-2 text-xs">
            <button type="button" data-pex="channels" class="px-2 py-1 rounded bg-ink-700 border border-ink-600">export channels.txt</button>
            <button type="button" data-pex="channels.csv" class="px-2 py-1 rounded bg-ink-700 border border-ink-600">channels.csv</button>
            <button type="button" data-pex="channels.xlsx" class="px-2 py-1 rounded bg-ink-700 border border-ink-600">channels.xlsx</button>
            <button type="button" data-pex="groups" class="px-2 py-1 rounded bg-ink-700 border border-ink-600">export groups.txt</button>
            <button type="button" data-pex="groups.csv" class="px-2 py-1 rounded bg-ink-700 border border-ink-600">groups.csv</button>
            <button type="button" data-pex="groups.xlsx" class="px-2 py-1 rounded bg-ink-700 border border-ink-600">groups.xlsx</button>
            <button type="button" data-pex="users" class="px-2 py-1 rounded bg-ink-700 border border-ink-600">export users.txt</button>
            <button type="button" data-pex="users.csv" class="px-2 py-1 rounded bg-ink-700 border border-ink-600">users.csv с источниками</button>
            <button type="button" data-pex="users.xlsx" class="px-2 py-1 rounded bg-ink-700 border border-ink-600">users.xlsx с источниками</button>
          </div>
        </div>
        <pre id="pLogs" class="text-xs bg-ink-950 border border-ink-700 rounded p-3 max-h-64 overflow-y-auto text-slate-300 whitespace-pre-wrap"></pre>
        <div id="pFilterReasons" class="mt-2 text-xs text-slate-400"></div>
        <div id="pUserEvidence" class="mt-3 text-xs text-slate-300"></div>
        <div id="pSourceEvidence" class="mt-2 text-xs text-slate-400"></div>
      </div>
    </div>
  `;

  $$("button[data-ptab]").forEach((b) => {
    b.addEventListener("click", () => {
      const t = b.getAttribute("data-ptab");
      navigate(`#/parsing/${t}`);
    });
  });

  const formCard = $("#pFormCard");
  const accList = await api("/business/accounts").catch(() => []);
  const accOpts = (accList || []).map((a) => {
    const cap = escapeHTML(a.list_label || a.username || a.phone || `#${a.id}`);
    return `<label class="flex items-center gap-2 mr-3 mb-1"><input type="checkbox" class="p-acc rounded border-ink-600 bg-ink-800" value="${a.id}" /> <span>${cap} <span class="text-slate-500">#${a.id}</span></span></label>`;
  }).join("");

  if (tab === "catalog") {
    formCard.innerHTML = `
      <div class="font-medium text-slate-200">Поиск по собранной базе — без подключения аккаунта</div>
      <p class="text-xs text-slate-500">Здесь только площадки, которые уже сохранил парсер CoreBot. Источник и дата последней проверки видны в результате.</p>
      <div class="grid md:grid-cols-3 gap-2">
        <input id="catalogQuery" maxlength="100" placeholder="Название или @username" class="bg-ink-800 border border-ink-600 rounded px-2 py-1.5 text-slate-100" />
        <select id="catalogKind" class="bg-ink-800 border border-ink-600 rounded px-2 py-1.5 text-slate-100"><option value="all">Каналы и группы</option><option value="channels">Каналы</option><option value="groups">Группы</option></select>
        <input id="catalogLang" maxlength="16" placeholder="Язык, например ru" class="bg-ink-800 border border-ink-600 rounded px-2 py-1.5 text-slate-100" />
        <input id="catalogMin" type="number" min="0" placeholder="Участников от" class="bg-ink-800 border border-ink-600 rounded px-2 py-1.5 text-slate-100" />
        <input id="catalogMax" type="number" min="0" placeholder="Участников до" class="bg-ink-800 border border-ink-600 rounded px-2 py-1.5 text-slate-100" />
        <div class="flex gap-3 items-center"><label><input id="catalogActive" type="checkbox" /> Активны за 7 дней</label><label>Комментарии каналов <select id="catalogDiscussion" class="bg-ink-800 border border-ink-600 rounded px-2 py-1"><option value="">любые</option><option value="true">есть</option><option value="false">нет</option></select></label></div>
      </div>
      <button id="catalogSearch" class="px-4 py-2 rounded-lg bg-accent-600 text-white">Найти</button>
      <div class="border-t border-ink-700 pt-3 mt-2 flex flex-wrap gap-2 items-center">
        <select id="catalogFolder" class="bg-ink-800 border border-ink-600 rounded px-2 py-1.5 text-slate-100"><option value="">Выберите папку</option></select>
        <button id="catalogOpenFolder" class="px-3 py-1.5 rounded bg-ink-700 text-slate-200">Открыть папку</button>
        <button id="catalogExportFolder" class="px-3 py-1.5 rounded bg-ink-700 text-slate-200">CSV для Excel</button>
        ${readOnly ? "" : `<input id="catalogNewFolder" maxlength="120" placeholder="Новая папка" class="bg-ink-800 border border-ink-600 rounded px-2 py-1.5 text-slate-100" />
        <button id="catalogCreateFolder" class="px-3 py-1.5 rounded bg-ink-700 text-slate-200">Создать</button>
        <button id="catalogDeleteFolder" class="px-3 py-1.5 rounded bg-rose-900 text-rose-100">Удалить папку</button>`}
      </div>
      <div id="catalogFolderItems" class="text-xs text-slate-300"></div>
      <div id="catalogResults" class="text-xs text-slate-300">Загрузка…</div>
      <div id="catalogSimilar" class="text-xs text-slate-300"></div>`;
    const loadFolders = async () => {
      const selected = $("#catalogFolder").value;
      const folders = await api("/business/parsing/catalog/folders");
      $("#catalogFolder").innerHTML = `<option value="">Выберите папку</option>` +
        folders.map(folder => `<option value="${Number(folder.id)}">${escapeHTML(folder.name)} (${Number(folder.entry_count)})</option>`).join("");
      if (selected) $("#catalogFolder").value = selected;
    };
    const openFolder = async () => {
      const id = Number($("#catalogFolder").value);
      const box = $("#catalogFolderItems");
      if (!id) { box.textContent = "Выберите папку"; return; }
      box.textContent = "Загрузка папки…";
      try {
        const folder = await api(`/business/parsing/catalog/folders/${id}`);
        box.innerHTML = `<p class="mt-3 mb-2">${escapeHTML(folder.name)} · ${folder.items.length} площадок</p>` +
          (folder.items.length ? folder.items.map(item => `<div class="py-1 border-b border-ink-700">${escapeHTML(item.title || item.username || String(item.telegram_id))} · ${escapeHTML(item.kind)} · ${item.available ? "в базе" : "источник удалён"} ${readOnly ? "" : `<button class="catalog-folder-remove text-rose-300 ml-2" data-kind="${escapeHTML(item.kind)}" data-id="${Number(item.telegram_id)}">Убрать</button>`}</div>`).join("") : "Папка пуста");
        box.querySelectorAll(".catalog-folder-remove").forEach(button => button.addEventListener("click", async () => {
          try {
            await api(`/business/parsing/catalog/folders/${id}/entries/${button.dataset.kind}/${button.dataset.id}`, { method: "DELETE" });
            await loadFolders();
            await openFolder();
          } catch (error) { toast(error.message || "Не удалось убрать площадку", "error"); }
        }));
      } catch (error) { box.textContent = error.message || "Папка не загрузилась"; }
    };
    $("#catalogOpenFolder").addEventListener("click", openFolder);
    $("#catalogExportFolder").addEventListener("click", async () => {
      const id = Number($("#catalogFolder").value);
      if (!id) { toast("Сначала выберите папку", "error"); return; }
      try { await downloadParsingExport(`/business/parsing/catalog/folders/${id}/export.csv`); }
      catch (error) { toast(error.message || "Не удалось выгрузить папку", "error"); }
    });
    if (!readOnly) {
      $("#catalogCreateFolder").addEventListener("click", async () => {
        const name = $("#catalogNewFolder").value.trim();
        if (!name) return;
        try {
          const created = await api("/business/parsing/catalog/folders", { method: "POST", body: { name } });
          await loadFolders();
          $("#catalogFolder").value = String(created.id);
          $("#catalogNewFolder").value = "";
          await openFolder();
        } catch (error) { toast(error.message || "Не удалось создать папку", "error"); }
      });
      $("#catalogDeleteFolder").addEventListener("click", async () => {
        const id = Number($("#catalogFolder").value);
        if (!id || !confirm("Удалить выбранную папку и её список площадок?")) return;
        try {
          await api(`/business/parsing/catalog/folders/${id}`, { method: "DELETE" });
          await loadFolders();
          $("#catalogFolderItems").textContent = "Папка удалена";
        } catch (error) { toast(error.message || "Не удалось удалить папку", "error"); }
      });
    }
    const loadCatalog = async (offset = 0) => {
      const pageOffset = Math.max(0, Number(offset) || 0);
      const qs = new URLSearchParams({ kind: $("#catalogKind").value, limit: "100", offset: String(pageOffset) });
      const term = $("#catalogQuery").value.trim();
      if (term) qs.set("q", term);
      const lang = $("#catalogLang").value.trim();
      if (lang) qs.set("lang", lang);
      if ($("#catalogMin").value) qs.set("min_count", $("#catalogMin").value);
      if ($("#catalogMax").value) qs.set("max_count", $("#catalogMax").value);
      if ($("#catalogActive").checked) qs.set("active_7d", "true");
      if ($("#catalogDiscussion").value) qs.set("has_discussion", $("#catalogDiscussion").value);
      const out = $("#catalogResults");
      out.textContent = "Поиск…";
      $("#catalogSimilar").textContent = "";
      try {
        const result = await api(`/business/parsing/catalog/search?${qs}`);
        const total = Math.max(0, Number(result.total) || 0);
        const pageEnd = pageOffset + result.items.length;
        out.innerHTML = `<p class="mb-2">Найдено: ${total}. Показаны ${result.items.length ? pageOffset + 1 : 0}–${pageEnd}.</p>
          ${result.items.length ? `<div class="overflow-x-auto"><table class="cb-table text-xs"><thead><tr><th>Тип</th><th>Площадка</th><th>Аудитория</th><th>Активность</th><th>Источник</th><th>Проверено</th><th>Похожие</th><th>Папка</th></tr></thead><tbody>
          ${result.items.map(item => {
            const validUsername = /^[A-Za-z0-9_]{5,32}$/.test(item.username || "");
            const title = escapeHTML(item.title || item.username || String(item.telegram_id));
            const place = validUsername ? `<a href="https://t.me/${encodeURIComponent(item.username)}" target="_blank" rel="noopener noreferrer" class="text-accent-400">${title}</a>` : title;
            return `<tr><td>${escapeHTML(item.kind)}</td><td>${place}<br><small>${escapeHTML(item.username ? `@${item.username}` : String(item.telegram_id))}</small></td><td>${item.audience_count ?? "—"}</td><td>${item.active_7d ? "7д" : "—"}${item.has_discussion ? " · комментарии" : ""}</td><td>${item.source_task_id ? `задача #${item.source_task_id}` : "импорт/неизвестно"}</td><td>${escapeHTML(fmtDate(item.last_seen))}</td><td><button class="catalog-similar-btn text-accent-400" data-kind="${escapeHTML(item.kind)}" data-id="${Number(item.telegram_id)}">Найти</button></td><td>${readOnly ? "—" : `<button class="catalog-folder-add text-accent-400" data-kind="${escapeHTML(item.kind)}" data-id="${Number(item.telegram_id)}">Добавить</button>`}</td></tr>`;
          }).join("")}</tbody></table></div>` : `<p>В собственной базе совпадений нет.</p>`}
          <div class="flex gap-2 items-center mt-3">
            <button id="catalogPrev" class="px-3 py-1 rounded bg-ink-700 text-slate-200" ${pageOffset === 0 ? "disabled" : ""}>◀ Назад</button>
            <button id="catalogNext" class="px-3 py-1 rounded bg-ink-700 text-slate-200" ${pageEnd >= total || pageOffset >= 10000 ? "disabled" : ""}>Далее ▶</button>
            ${pageOffset >= 10000 && pageEnd < total ? "<span>Достигнут предел просмотра 10 100 записей; уточните фильтры.</span>" : ""}
          </div>`;
        out.querySelector("#catalogPrev")?.addEventListener("click", () => loadCatalog(pageOffset - 100));
        out.querySelector("#catalogNext")?.addEventListener("click", () => loadCatalog(pageOffset + 100));
        out.querySelectorAll(".catalog-folder-add").forEach(button => button.addEventListener("click", async () => {
          const folderId = Number($("#catalogFolder").value);
          if (!folderId) { toast("Сначала выберите папку", "error"); return; }
          try {
            await api(`/business/parsing/catalog/folders/${folderId}/entries`, { method: "POST", body: { kind: button.dataset.kind, telegram_id: Number(button.dataset.id) } });
            await loadFolders();
            await openFolder();
          } catch (error) { toast(error.message || "Не удалось добавить площадку", "error"); }
        }));
        out.querySelectorAll(".catalog-similar-btn").forEach(button => button.addEventListener("click", async () => {
          const box = $("#catalogSimilar");
          box.textContent = "Поиск похожих в сохранённой базе…";
          try {
            const qs = new URLSearchParams({ kind: button.dataset.kind, telegram_id: button.dataset.id });
            const related = await api(`/business/parsing/catalog/similar?${qs}`);
            box.innerHTML = `<p class="mt-3 mb-2">Похожие по словам названия (локальная база): ${related.items.length}</p>` +
              (related.items.length ? related.items.map(item => {
                const name = escapeHTML(item.title || item.username || String(item.telegram_id));
                const valid = /^[A-Za-z0-9_]{5,32}$/.test(item.username || "");
                const place = valid ? `<a href="https://t.me/${encodeURIComponent(item.username)}" target="_blank" rel="noopener noreferrer" class="text-accent-400">${name}</a>` : name;
                return `<div class="py-1 border-b border-ink-700">${place} · ${Number(item.score)}% · общие слова: ${escapeHTML(item.shared_title_words.join(", "))}</div>`;
              }).join("") : "<p>Совпадений по словам названия нет.</p>");
          } catch (error) { box.textContent = error.message || "Ошибка поиска похожих"; }
        }));
      } catch (error) { out.textContent = error.message || "Ошибка поиска"; }
    };
    $("#catalogSearch").addEventListener("click", () => loadCatalog(0));
    $("#catalogQuery").addEventListener("keydown", event => { if (event.key === "Enter") loadCatalog(0); });
    try { await loadFolders(); } catch (error) { $("#catalogFolderItems").textContent = error.message || "Папки не загрузились"; }
    await loadCatalog();
  } else if (tab === "channels" || tab === "groups") {
    formCard.innerHTML = `
      <div class="font-medium text-slate-200">${tab === "channels" ? "Каналы" : "Группы"}: новая задача</div>
      <div class="grid md:grid-cols-2 gap-3 text-xs">
        <label class="block">Ключевые слова (по одному в строке)
          <textarea id="pKeywords" rows="4" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100 font-mono" placeholder="crypto&#10;defi"></textarea>
        </label>
        <label class="block">Окончания (по одному в строке)
          <textarea id="pEndings" rows="4" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100 font-mono" placeholder="news&#10;chat&#10;channel"></textarea>
        </label>
      </div>
      <label class="block text-xs">Ручные источники (@username, публичная ссылка или числовой ID), по одному в строке
        <textarea id="pManualSources" rows="3" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100 font-mono" placeholder="@my_channel&#10;https://t.me/my_group"></textarea>
      </label>
      <div class="grid md:grid-cols-3 gap-3 text-xs">
        <label class="block">Depth (1–3)
          <select id="pDepth" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100">
            <option value="1">1</option><option value="2">2</option><option value="3">3</option>
          </select>
        </label>
        <label class="block">Режим задачи
          <select id="pMode" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100">
            <option value="max_coverage">max_coverage</option>
            <option value="active_only">active_only</option>
          </select>
        </label>
        <label class="block flex items-end gap-2">
          <input id="pExpandedSearch" type="checkbox" class="rounded border-ink-600 bg-ink-800" />
          <span>Расширенный поиск</span>
        </label>
      </div>
      <div class="grid md:grid-cols-3 gap-3 text-xs">
        <label class="inline-flex items-center gap-2"><input id="fActive7d" type="checkbox" class="rounded border-ink-600 bg-ink-800" /> Только активные (≥1 пост/7д)</label>
        <label class="inline-flex items-center gap-2"><input id="fDiscussion" type="checkbox" class="rounded border-ink-600 bg-ink-800" /> Только открытые комментарии</label>
        <label class="inline-flex items-center gap-2"><input id="fPublic" type="checkbox" class="rounded border-ink-600 bg-ink-800" /> Только открытые каналы</label>
        <label class="block">${tab === "groups" ? "Участники" : "Подписчики"} min
          <input id="fSubsMin" type="number" min="0" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100" />
        </label>
        <label class="block">${tab === "groups" ? "Участники" : "Подписчики"} max
          <input id="fSubsMax" type="number" min="0" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100" />
        </label>
        <label class="block">Язык
          <select id="fLang" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100">
            <option value="">любая</option><option value="ru">ru</option><option value="en">en</option>
          </select>
        </label>
      </div>
      <div><span class="text-slate-400">Аккаунты:</span><div class="mt-1 flex flex-wrap">${accOpts || "<span class='text-slate-500'>нет аккаунтов</span>"}</div></div>
      <div class="flex items-center gap-3 text-xs"><button type="button" id="pSearchPreview" class="px-3 py-1.5 rounded border border-ink-600 hover:bg-ink-700 text-slate-200">Проверить запросы</button><span id="pSearchPreviewCount" class="text-slate-400" aria-live="polite"></span></div>
      <pre id="pSearchPreviewDetails" class="text-xs text-slate-400 whitespace-pre-wrap max-h-48 overflow-y-auto" aria-live="polite"></pre>
      <button id="pSubmit" class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white text-sm ${readOnly ? "opacity-50 cursor-not-allowed" : ""}" ${readOnly ? "disabled" : ""}>Запустить</button>
    `;
    function searchParams() {
      return {
        keywords: ($("#pKeywords")?.value || "").split(/\r?\n/).map((x) => x.trim()).filter(Boolean),
        endings: ($("#pEndings")?.value || "").split(/\r?\n/).map((x) => x.trim()).filter(Boolean),
        manual_usernames_text: ($("#pManualSources")?.value || "").trim(),
        expanded_search: !!$("#pExpandedSearch")?.checked,
        filters: {
          is_active_7d: $("#fActive7d")?.checked ? true : null,
          has_discussion: $("#fDiscussion")?.checked ? true : null,
          is_public: $("#fPublic")?.checked ? true : null,
          ...parserSizeFilters(tab, $("#fSubsMin")?.value, $("#fSubsMax")?.value),
          lang: ($("#fLang")?.value || "").trim() || null,
        },
      };
    }
    async function previewSearchQueries() {
      const preview = await api("/business/parsing/search-preview", {
        method: "POST", body: { kind: tab, params: searchParams() },
      });
      $("#pSearchPreviewCount").textContent = `${preview.count} запросов, ${preview.error_count} ошибок`;
      const issues = (preview.errors || []).slice(0, 20).map((item) =>
        `${item.field || "ввод"}: ${item.reason || "ошибка"}`);
      const queries = (preview.queries || []).slice(0, 20);
      $("#pSearchPreviewDetails").textContent = [
        ...issues, ...(issues.length ? [""] : []), ...queries,
        ...(preview.count > queries.length ? [`…ещё ${preview.count - queries.length}`] : []),
      ].join("\n");
      return preview;
    }
    $("#pSearchPreview")?.addEventListener("click", async () => {
      try { await previewSearchQueries(); } catch (e) { toast(e.message, "error"); }
    });
    $("#pSubmit")?.addEventListener("click", async () => {
      if (readOnly) return;
      const ids = $$(".p-acc:checked").map((c) => Number(c.value)).filter((n) => n > 0);
      if (!ids.length) {
        toast("Выберите хотя бы один аккаунт", "error");
        return;
      }
      const depth = Number($("#pDepth")?.value || "1");
      const mode = ($("#pMode")?.value || "max_coverage").trim();
      try {
        const preview = await previewSearchQueries();
        if (preview.error_count || !preview.count) {
          toast("Исправьте запросы перед запуском", "error");
          return;
        }
        await api("/business/parsing/tasks", {
          method: "POST",
          body: {
            kind: tab,
            account_ids: ids,
            depth,
            mode,
            params: searchParams(),
          },
        });
        toast("Задача создана", "success");
        await loadParsingTasks();
      } catch (e) {
        toast(e.message, "error");
      }
    });
  } else {
    formCard.innerHTML = `
      <div class="font-medium text-slate-200">Пользователи: новая задача</div>
      <label class="block text-xs">Ввод источников (@username или t.me ссылки), по одной строке
        <textarea id="pUserInputs" rows="5" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100 font-mono text-xs"
        placeholder="@group_one&#10;https://t.me/channel_one"></textarea>
      </label>
      <label class="block text-xs">Загрузка .txt
        <input id="pUsersTxtFile" type="file" accept=".txt,text/plain" class="mt-1 block w-full text-slate-300 text-xs" />
      </label>
      <div class="flex flex-wrap items-center gap-3 text-xs">
        <button id="pPreviewUsers" type="button" class="px-3 py-1.5 rounded border border-ink-600 hover:bg-ink-700 text-slate-200">Проверить источники</button>
        <span id="pUserSourcePreview" class="text-slate-400" aria-live="polite"></span>
      </div>
      <div id="pUserSourceErrors" class="text-xs text-red-400 whitespace-pre-wrap" aria-live="polite"></div>
      <div class="grid md:grid-cols-2 gap-3 text-xs">
        <div class="space-y-2 border border-ink-700 rounded p-2">
          <div class="text-slate-300">Источники (группы)</div>
          <label class="inline-flex items-center gap-2"><input id="srcGroupMembers" type="checkbox" class="rounded border-ink-600 bg-ink-800" checked /> участники</label>
          <label class="inline-flex items-center gap-2"><input id="srcGroupActive" type="checkbox" class="rounded border-ink-600 bg-ink-800" checked /> активные (писали)</label>
        </div>
        <div class="space-y-2 border border-ink-700 rounded p-2">
          <div class="text-slate-300">Источники (каналы)</div>
          <label class="inline-flex items-center gap-2"><input id="srcChanCommenters" type="checkbox" class="rounded border-ink-600 bg-ink-800" checked /> комментаторы</label>
          <label class="inline-flex items-center gap-2"><input id="srcChanActiveDiscussion" type="checkbox" class="rounded border-ink-600 bg-ink-800" checked /> активные (если есть чат)</label>
        </div>
      </div>
      <div class="grid md:grid-cols-3 gap-3 text-xs">
        <label class="block">Режим задачи
          <select id="pUserMode" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100">
            <option value="max_coverage">max_coverage</option>
            <option value="active_only">active_only</option>
          </select>
        </label>
        <label class="inline-flex items-center gap-2"><input id="ufUsername" type="checkbox" class="rounded border-ink-600 bg-ink-800" /> только с username</label>
        <label class="inline-flex items-center gap-2"><input id="ufAvatar" type="checkbox" class="rounded border-ink-600 bg-ink-800" /> только с аватаром</label>
        <label class="inline-flex items-center gap-2"><input id="ufPremium" type="checkbox" class="rounded border-ink-600 bg-ink-800" /> только Telegram Premium</label>
        <label class="inline-flex items-center gap-2"><input id="ufScamFake" type="checkbox" class="rounded border-ink-600 bg-ink-800" /> исключать scam/fake</label>
        <label class="inline-flex items-center gap-2"><input id="ufRecentOnline" type="checkbox" class="rounded border-ink-600 bg-ink-800" /> только недавно онлайн</label>
        <label class="inline-flex items-center gap-2"><input id="ufAntiBot" type="checkbox" class="rounded border-ink-600 bg-ink-800" checked /> анти-бот (удалённые/пустые)</label>
        <label class="block">Язык
          <select id="ufLang" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100">
            <option value="">любой</option><option value="ru">ru</option><option value="en">en</option>
          </select>
        </label>
      </div>
      <div class="border border-ink-700 rounded p-3 space-y-2 text-xs">
        <div class="text-slate-300">Фильтр сообщений источника</div>
        <p class="text-slate-500">Применяется к авторам видимых сообщений групп и комментариев каналов. Участники из списка группы без сообщений не проходят через этот фильтр.</p>
        <label class="block">Слова или фразы, по одному в строке (достаточно одного совпадения)
          <textarea id="ufMessageKeywords" rows="3" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100" placeholder="интересующая тема"></textarea>
        </label>
        <div class="grid md:grid-cols-2 gap-3">
          <label class="block">Сообщения с
            <input id="ufMessageFrom" type="datetime-local" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100" />
          </label>
          <label class="block">Сообщения по
            <input id="ufMessageTo" type="datetime-local" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100" />
          </label>
        </div>
      </div>
      <div><span class="text-slate-400">Аккаунты:</span><div class="mt-1 flex flex-wrap">${accOpts || "<span class='text-slate-500'>нет аккаунтов</span>"}</div></div>
      <button id="pSubmitUsers" class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white text-sm ${readOnly ? "opacity-50 cursor-not-allowed" : ""}" ${readOnly ? "disabled" : ""}>Запустить</button>
    `;
    let txtUploaded = "";
    const sourceText = () => [($("#pUserInputs")?.value || "").trim(), txtUploaded.trim()].filter(Boolean).join("\n");
    async function previewUserSources() {
      const result = await api("/business/parsing/sources-preview", {
        method: "POST", body: { text: sourceText() },
      });
      const label = $("#pUserSourcePreview");
      label.textContent = `${result.count} источников, ${result.duplicates} дублей, ${result.error_count} ошибок`;
      label.className = result.error_count || !result.count ? "text-xs text-red-400" : "text-xs text-emerald-400";
      const reasons = {
        too_many_lines: "слишком много строк (максимум 2000)",
        too_long: "строка слишком длинная",
        unsupported_link: "ссылка на приглашение или сообщение не поддерживается",
        invalid_source: "ожидается @username, публичная ссылка или числовой ID",
      };
      const errors = (result.errors || []).slice(0, 20).map((entry) =>
        `${entry.line ? `Строка ${entry.line}` : "Список"}: ${reasons[entry.reason] || entry.reason}`);
      if (result.error_count > errors.length) errors.push(`И ещё ${result.error_count - errors.length} ошибок`);
      $("#pUserSourceErrors").textContent = errors.join("\n");
      return result;
    }
    $("#pPreviewUsers")?.addEventListener("click", async () => {
      try { await previewUserSources(); } catch (e) { toast(e.message, "error"); }
    });
    $("#pUsersTxtFile")?.addEventListener("change", async (ev) => {
      const f = ev?.target?.files?.[0];
      if (!f) return;
      try {
        txtUploaded = await f.text();
        toast(`Загружен ${f.name}`, "success");
      } catch {
        toast("Не удалось прочитать файл", "error");
      }
    });
    $("#pSubmitUsers")?.addEventListener("click", async () => {
      if (readOnly) return;
      const ids = $$(".p-acc:checked").map((c) => Number(c.value)).filter((n) => n > 0);
      if (!ids.length) {
        toast("Выберите хотя бы один аккаунт", "error");
        return;
      }
      const manualText = sourceText();
      if (!manualText) {
        toast("Добавьте список источников вручную или через txt", "error");
        return;
      }
      const mode = ($("#pUserMode")?.value || "max_coverage").trim();
      const userFilters = {
        require_username: !!$("#ufUsername")?.checked,
        require_avatar: !!$("#ufAvatar")?.checked,
        require_premium: !!$("#ufPremium")?.checked,
        exclude_scam_fake: !!$("#ufScamFake")?.checked,
        recent_online_7d: !!$("#ufRecentOnline")?.checked,
        anti_bot: !!$("#ufAntiBot")?.checked,
        exclude_deleted: !!$("#ufAntiBot")?.checked,
        lang: ($("#ufLang")?.value || "").trim() || null,
      };
      const sourceOptions = {
        group_members: !!$("#srcGroupMembers")?.checked,
        group_active: !!$("#srcGroupActive")?.checked,
        channel_commenters: !!$("#srcChanCommenters")?.checked,
        channel_active_if_discussion: !!$("#srcChanActiveDiscussion")?.checked,
      };
      const messageDate = (selector) => {
        const value = $(selector)?.value;
        return value ? new Date(value).toISOString() : null;
      };
      const messageFilters = {
        keywords: ($("#ufMessageKeywords")?.value || "").split(/\r?\n/).map((x) => x.trim()).filter(Boolean),
        from_at: messageDate("#ufMessageFrom"),
        to_at: messageDate("#ufMessageTo"),
      };
      try {
        const preview = await previewUserSources();
        if (preview.error_count || !preview.count) {
          toast("Исправьте список источников перед запуском", "error");
          return;
        }
        await api("/business/parsing/tasks", {
          method: "POST",
          body: {
            kind: "users",
            account_ids: ids,
            depth: 1,
            mode,
            params: {
              user_inputs_text: manualText,
              source_options: sourceOptions,
              filters: userFilters,
              message_filters: messageFilters,
            },
          },
        });
        toast("Задача создана", "success");
        await loadParsingTasks();
      } catch (e) {
        toast(e.message, "error");
      }
    });
  }

  let selectedTaskId = null;

  async function loadParsingTasks() {
    try {
      const list = await api("/business/parsing/tasks?limit=100");
      const tbody = $("#pTaskBody");
      if (!list.length) {
        tbody.innerHTML = `<tr><td colspan="12" class="text-center text-slate-500 py-6">Нет задач</td></tr>`;
        return;
      }
      const stageMap = { search: "поиск", collect: "сбор", filter: "фильтрация", done: "завершено" };
      tbody.innerHTML = list.map((t) => `
        <tr class="cursor-pointer hover:bg-ink-800/80" data-pselect="${t.id}">
          <td class="text-slate-400">#${t.id}</td>
          <td>${escapeHTML(t.kind)}</td>
          <td>${escapeHTML(t.status)}</td>
          <td>${t.progress_percent ?? 0}</td>
          <td class="max-w-[140px] truncate" title="${escapeHTML(t.current_stage || "")}">${escapeHTML(stageMap[t.current_stage] || t.current_stage || "—")}</td>
          <td>${t.current_account_id ?? "—"}</td>
          <td class="max-w-[200px] truncate" title="${escapeHTML(t.current_query || "")}">${escapeHTML(t.current_query || "—")}</td>
          <td>${t.found_count ?? 0}</td>
          <td>${t.filtered_count ?? 0}</td>
          <td>${t.error_count ?? 0}</td>
          <td class="text-slate-400">${fmtDate(t.created_at)}</td>
          <td><button data-plogs="${t.id}" class="text-xs text-accent-400 hover:text-accent-300">логи</button></td>
        </tr>
      `).join("");
      tbody.querySelectorAll("tr[data-pselect]").forEach((row) => {
        row.addEventListener("click", (ev) => {
          if (ev.target.closest("button[data-plogs]")) return;
          selectedTaskId = Number(row.dataset.pselect);
          loadParsingLogs(selectedTaskId);
        });
      });
      tbody.querySelectorAll("button[data-plogs]").forEach((b) => {
        b.addEventListener("click", (ev) => {
          ev.stopPropagation();
          selectedTaskId = Number(b.dataset.plogs);
          loadParsingLogs(selectedTaskId);
        });
      });
    } catch (e) {
      toast(`Парсинг: ${e.message}`, "error");
    }
  }

  async function loadParsingLogs(taskId) {
    if (!taskId) return;
    const card = $("#pDetailCard");
    const pre = $("#pLogs");
    $("#pDetailId").textContent = String(taskId);
    card.style.display = "block";
    pre.textContent = "Загрузка…";
    try {
      const logs = await api(`/business/parsing/tasks/${taskId}/logs?limit=300`);
      pre.textContent = (logs || []).map((l) =>
        `[${fmtDate(l.created_at)}] ${l.level} ${l.event}: ${l.message || ""}`
      ).join("\n");
    } catch (e) {
      pre.textContent = "Ошибка: " + e.message;
    }
    try {
      const reasons = await api(`/business/parsing/tasks/${taskId}/filter-reasons`);
      $("#pFilterReasons").textContent = reasons.length
        ? "Исключены фильтрами: " + reasons.map(item => `${item.reason} — ${item.count}`).join(" · ")
        : "";
    } catch (e) { $("#pFilterReasons").textContent = "Причины исключения: " + e.message; }
    const usersBox = $("#pUserEvidence");
    const sourceBox = $("#pSourceEvidence");
    sourceBox.textContent = "";
    try {
      const users = await api(`/business/parsing/results/users?task_id=${taskId}&limit=20`);
      usersBox.innerHTML = users.length
        ? `<div class="mb-1">Найденные пользователи (первые 20). Результат парсинга не даёт права на ЛС.</div>` + users.map(user =>
            `<button class="block py-1 text-left text-accent-300 hover:text-accent-200" data-user-source="${Number(user.id)}">${escapeHTML(user.username ? `@${user.username}` : user.display_name || String(user.telegram_id))} · ID ${Number(user.telegram_id)}</button>`
          ).join("")
        : "";
      usersBox.querySelectorAll("button[data-user-source]").forEach(button => button.addEventListener("click", async () => {
        const userId = Number(button.dataset.userSource);
        try {
          const sources = await api(`/business/parsing/results/users/${userId}/sources?task_id=${taskId}`);
          sourceBox.innerHTML = sources.length ? sources.map(source =>
            `<div class="py-1 border-b border-ink-700">${escapeHTML(source.source_entity_kind)} ${Number(source.source_entity_id)} · ${escapeHTML(source.source_kind)}${source.post_id ? ` · пост ${Number(source.post_id)}` : ""}${source.message_id ? ` · сообщение ${Number(source.message_id)}` : ""} · событие ${fmtDate(source.message_at) || "—"} · обнаружено ${fmtDate(source.observed_at) || "—"}</div>`
          ).join("") : "Источник не найден";
        } catch (error) { sourceBox.textContent = error.message || "Ошибка источника"; }
      }));
    } catch (e) {
      usersBox.textContent = "Результаты пользователей: " + e.message;
    }
  }

  $("#pCancelTask")?.addEventListener("click", async () => {
    if (readOnly || !selectedTaskId) return;
    try {
      await api(`/business/parsing/tasks/${selectedTaskId}/cancel`, { method: "POST" });
      toast("Отмена запрошена", "success");
      await loadParsingTasks();
    } catch (e) {
      toast(e.message, "error");
    }
  });
  $$("button[data-pex]").forEach((b) => {
    b.addEventListener("click", async () => {
      if (!selectedTaskId) {
        toast("Выберите задачу (строка таблицы)", "info");
        return;
      }
      const exportType = b.getAttribute("data-pex");
      const filename = exportType.includes(".") ? exportType : `${exportType}.txt`;
      const path = `/business/parsing/export/${filename}?task_id=${selectedTaskId}`;
      try {
        await downloadParsingExport(path);
      } catch (e) {
        toast(e.message, "error");
      }
    });
  });

  await loadParsingTasks();

  // Realtime без дёрганья UI: события приходят по SSE только при изменениях.
  try {
    const url = `${API}/business/parsing/stream`;
    const es = new EventSource(url);
    state.parsing.stream = es;

    es.addEventListener("tasks_changed", async () => {
      if (state.route !== "parsing") return;
      await loadParsingTasks();
    });
    es.addEventListener("log", async (ev) => {
      if (state.route !== "parsing") return;
      let x = null;
      try { x = JSON.parse(ev.data || "{}"); } catch { x = null; }
      if (!x || !selectedTaskId) return;
      if (Number(x.task_id) !== Number(selectedTaskId)) return;
      const pre = $("#pLogs");
      if (!pre) return;
      const line = `[${fmtDate(x.created_at)}] ${x.level} ${x.event}: ${x.message || ""}`;
      const nearBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
      pre.textContent = (pre.textContent ? `${pre.textContent}\n` : "") + line;
      if (nearBottom) pre.scrollTop = pre.scrollHeight;
    });
    es.onerror = () => {
      // Оставляем только SSE-путь, без отката на ручной polling.
    };
  } catch {
    // SSE недоступен — покажем предупреждение.
    toast("Realtime stream parsing недоступен", "error");
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

      <!-- Recent live + переходы по ссылкам -->
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div class="card">
          <div class="flex items-center justify-between mb-3">
            <h3 class="font-semibold">Live-поток сообщений</h3>
            <span class="text-xs text-slate-400">после входа в панель<span id="dashUpdated"></span></span>
          </div>
          <div id="dashRecent" class="space-y-2 text-sm text-slate-400">Подождите событий…</div>
        </div>
        <div class="card">
          <div class="flex items-center justify-between mb-3">
            <h3 class="font-semibold">🔗 Переходы по ссылкам</h3>
            <a href="#/links" class="text-xs text-accent-500 hover:text-accent-400">все ссылки →</a>
          </div>
          <div class="flex gap-6 mb-3">
            <div><div id="dashClicksTotal" class="text-2xl font-semibold text-white">…</div>
              <div class="text-[11px] text-slate-500">всего переходов</div></div>
            <div><div id="dashClicks24" class="text-2xl font-semibold text-emerald-300">…</div>
              <div class="text-[11px] text-slate-500">за 24 часа</div></div>
          </div>
          <div id="dashLinks" class="space-y-2 text-sm text-slate-400">…</div>
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
    loadDashLinks(),
  ]);
  const upd = $("#dashUpdated");
  if (upd) upd.textContent = ` · обновлено ${new Date().toLocaleTimeString("ru-RU", { hour12: false })}`;

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
          <div class="text-[11px] text-slate-500">acc#${m.account_id} ↔ ${m.peer_user_id} В· ${fmtRelative(m.created_at)}</div>
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
      const tip = `${p.ts}\n← ${p.messages_in} В· → ${p.messages_out} В· ✋ ${p.manual_sent}`;
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
            <span class="text-slate-500">клиентов: <b class="text-slate-200">${it.clients}</b> В· событий: ${it.events}</span>
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
          <div class="text-[11px] text-slate-500 mt-1">отправлено ${m.sent}/${m.total} В· ошибок ${m.failed}</div>
          <div class="cb-bar mt-1"><div class="cb-bar-fill" style="width:${pct}%"></div></div>
        </a>`;
    }).join("");
  } catch (e) { el.innerHTML = `<div class="text-rose-400">${escapeHTML(e.message)}</div>`; }
}

/* Виджет переходов: топ-5 ссылок по кликам за 24ч. */
async function loadDashLinks() {
  const box = $("#dashLinks");
  if (!box) return;
  try {
    const list = await api("/business/links");
    const total = list.reduce((m, l) => m + (l.clicks_total || 0), 0);
    const day = list.reduce((m, l) => m + (l.clicks_24h || 0), 0);
    $("#dashClicksTotal").textContent = total;
    $("#dashClicks24").textContent = day;
    if (!list.length) {
      box.innerHTML = `<div class="text-slate-500">Ссылок пока нет — создайте в разделе «Ссылки».</div>`;
      return;
    }
    const top = [...list].sort((a, b) => (b.clicks_24h || 0) - (a.clicks_24h || 0)).slice(0, 5);
    const max = Math.max(1, ...top.map(l => l.clicks_24h || 0));
    box.innerHTML = top.map(l => `
      <div>
        <div class="flex items-center justify-between text-xs gap-2">
          <span class="text-slate-200 truncate" title="${escapeHTML(l.target_url)}">/${escapeHTML(l.code)} · ${escapeHTML(l.name)}</span>
          <span class="text-slate-500 whitespace-nowrap"><b class="text-emerald-300">${l.clicks_24h || 0}</b> / ${l.clicks_total || 0}</span>
        </div>
        <div class="cb-bar mt-1"><div class="cb-bar-fill" style="width:${Math.round(((l.clicks_24h || 0) / max) * 100)}%"></div></div>
      </div>
    `).join("");
  } catch (e) {
    box.innerHTML = `<div class="text-rose-400">${escapeHTML(e.message)}</div>`;
  }
}

/* ---------------------------- Accounts view ---------------------------- */

/* Трекинг-ссылки: создание /r/{code}, счётчики переходов. */
async function renderLinks() {
  setHeader("Ссылки", "Трекинг переходов по рекламе: короткая ссылка → ваш канал");
  const readOnly = isReadOnlyRole();
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full space-y-4">
      <div class="card">
        <p class="text-xs text-slate-500">Как это работает: создайте ссылку на канал/пост → вставьте короткую
        <code>/r/…</code> в рекламу (или в <b>community_link</b> рассылки — тогда <code>{link}</code> станет трекаемой).
        Каждый переход считается в дашборде. Важно: снаружи ссылка открывается, только если Control Plane
        доступен из интернета (домен/VPS); на локальном <code>127.0.0.1</code> — лишь для проверки.</p>
      </div>
      ${readOnly ? "" : `
      <div class="card">
        <h3 class="font-semibold mb-3">Новая ссылка</h3>
        <form id="linkForm" class="grid grid-cols-1 md:grid-cols-4 gap-3 text-sm">
          <label class="block">
            <span class="text-slate-400 text-xs">Название</span>
            <input name="name" placeholder="например, Канал — посев 23.09"
                   class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
          </label>
          <label class="block md:col-span-2">
            <span class="text-slate-400 text-xs">Куда ведёт (https://…)</span>
            <input name="target_url" placeholder="https://t.me/+abcdef"
                   class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 font-mono" />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">Рассылка (необязательно)</span>
            <select name="mailing_id" id="linkMailing"
                    class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
              <option value="0">— без привязки —</option>
            </select>
          </label>
          <div class="md:col-span-4 flex items-center gap-3">
            <button type="submit" class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white">Создать ссылку</button>
            <span id="linkFormMsg" class="text-xs text-slate-400"></span>
          </div>
        </form>
      </div>`}
      <div class="card">
        <div class="flex items-center justify-between mb-3 flex-wrap gap-2">
          <h3 class="font-semibold">Ссылки</h3>
          <button id="linkRefresh" class="px-3 py-1.5 rounded-md bg-ink-700 hover:bg-ink-600 text-sm">⟳</button>
        </div>
        <div id="linkTable" class="text-sm text-slate-400">Загрузка…</div>
      </div>
    </div>
  `;
  try {
    const ms = await api("/business/mailings?limit=500").catch(() => []);
    const sel = $("#linkMailing");
    if (sel && ms?.length) {
      sel.innerHTML = `<option value="0">— без привязки —</option>` + ms.map(m =>
        `<option value="${m.id}">${escapeHTML(m.name)} (#${m.id})</option>`).join("");
    }
  } catch {}
  $("#linkRefresh").addEventListener("click", loadLinksTable);
  $("#linkForm")?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.currentTarget);
    const out = $("#linkFormMsg");
    const name = (fd.get("name") || "").toString().trim();
    const target_url = (fd.get("target_url") || "").toString().trim();
    const mailing_id = Number(fd.get("mailing_id") || 0);
    if (!name || !target_url) {
      out.textContent = "Заполните название и ссылку.";
      out.className = "text-xs text-rose-400";
      return;
    }
    out.textContent = "…";
    try {
      const r = await api("/business/links", {
        method: "POST",
        body: { name, target_url, mailing_id: mailing_id > 0 ? mailing_id : null },
      });
      out.textContent = `ok: ${location.origin}/r/${r.code}`;
      out.className = "text-xs text-emerald-300";
      toast("Ссылка создана", "success");
      ev.currentTarget.reset();
      await loadLinksTable();
    } catch (e) {
      out.textContent = e.message;
      out.className = "text-xs text-rose-400";
    }
  });
  await loadLinksTable();
}

async function loadLinksTable() {
  const tbl = $("#linkTable");
  if (!tbl) return;
  const readOnly = isReadOnlyRole();
  try {
    const list = await api("/business/links");
    if (!list.length) {
      tbl.innerHTML = `<div class="text-slate-500 text-xs">Ссылок пока нет.</div>`;
      return;
    }
    tbl.innerHTML = `
      <table class="cb-table">
        <thead><tr>
          <th>Короткая ссылка</th><th>Название</th><th>Куда ведёт</th>
          <th class="text-right">Всего</th><th class="text-right">24ч</th><th>Создана</th><th></th>
        </tr></thead>
        <tbody>
          ${list.map(l => {
            const short = `${location.origin}/r/${l.code}`;
            return `<tr>
              <td class="font-mono text-xs whitespace-nowrap">
                <span class="text-accent-500">/r/${escapeHTML(l.code)}</span>
                <button data-act="copy" data-url="${escapeHTML(short)}" aria-label="Скопировать ссылку"
                        class="ml-1 px-1.5 py-0.5 rounded bg-ink-700 hover:bg-ink-600 text-xs">⧉</button>
              </td>
              <td class="text-slate-100">${escapeHTML(l.name)}</td>
              <td class="text-xs text-slate-400 max-w-[280px] truncate" title="${escapeHTML(l.target_url)}">${escapeHTML(l.target_url)}</td>
              <td class="text-right text-slate-200"><b>${l.clicks_total || 0}</b></td>
              <td class="text-right text-emerald-300"><b>${l.clicks_24h || 0}</b></td>
              <td class="text-xs text-slate-500">${fmtRelative(l.created_at)}</td>
              <td class="text-right whitespace-nowrap">
                ${readOnly ? "" : `<button data-act="del" data-id="${l.id}" aria-label="Удалить ссылку"
                    class="px-2 py-1 rounded bg-rose-700 hover:bg-rose-600 text-xs text-white">🗑</button>`}
              </td>
            </tr>`;
          }).join("")}
        </tbody>
      </table>`;
    tbl.querySelectorAll('[data-act="copy"]').forEach(b => {
      b.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(b.dataset.url);
          toast("Ссылка скопирована", "success");
        } catch { toast(b.dataset.url, "info"); }
      });
    });
    tbl.querySelectorAll('[data-act="del"]').forEach(b => {
      b.addEventListener("click", async () => {
        if (!confirmDanger(`Удалить ссылку #${b.dataset.id} вместе со статистикой?`)) return;
        try {
          await api(`/business/links/${b.dataset.id}`, { method: "DELETE" });
          toast("Удалено", "success");
          await loadLinksTable();
        } catch (e) { toast(e.message, "error"); }
      });
    });
  } catch (e) {
    tbl.innerHTML = `<div class="text-rose-400 text-sm">${escapeHTML(e.message)}</div>`;
  }
}

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
      <div class="card mb-4" id="tdataImportCard">
        <div class="flex items-center justify-between mb-3">
          <h3 class="font-semibold text-slate-100">Импорт TData (ZIP)</h3>
          <span class="text-xs text-slate-500">Массовая загрузка аккаунтов из архива</span>
        </div>
        <div id="tdataDropZone" tabindex="0" role="button" aria-label="Загрузить TData ZIP">
          <div class="tdata-ico" aria-hidden="true">📦</div>
          <div class="text-slate-200 text-sm font-medium mb-1">Перетащите ZIP или нажмите для выбора</div>
          <div class="text-slate-500 text-xs">Архив с одной или несколькими папками tdata</div>
          <input id="tdataFileInput" type="file" accept=".zip" class="hidden" />
        </div>
        <label class="block mt-3 text-sm text-slate-300" for="tdataProxyGroup">Пул SOCKS5 для новых аккаунтов *</label>
        <select id="tdataProxyGroup" required class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
          <option value="">Загрузка пулов…</option>
        </select>
        <p class="mt-1 text-xs text-slate-500">Выберите регион до импорта. Каждая сессия подключается через прокси этого пула.</p>
        <div class="mt-3 flex items-center gap-3">
          <button id="tdataUploadBtn" type="button" class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white">Загрузить ZIP</button>
          <span id="tdataMsg" class="text-xs text-slate-400"></span>
        </div>
      </div>
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
      ${isReadOnlyRole() ? "" : `<form id="accountBulkMetadataForm" class="card mb-4 text-sm">
        <h3 class="font-semibold text-slate-100 mb-2">Теги и группы для выбранных аккаунтов</h3>
        <div class="flex flex-wrap items-end gap-3">
          <label class="block">Действие <select name="action" class="block mt-1 bg-ink-800 border border-ink-600 rounded px-2 py-1.5"><option value="add">Добавить</option><option value="remove">Удалить</option></select></label>
          <label class="block flex-1 min-w-48">Теги через запятую <input name="tags" class="block mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1.5" placeholder="USA, Main" /></label>
          <label class="block">Группа <select name="group_id" id="accBulkGroup" class="block mt-1 bg-ink-800 border border-ink-600 rounded px-2 py-1.5"><option value="0">— без группы —</option></select></label>
          <button type="submit" class="px-3 py-1.5 rounded bg-accent-600 hover:bg-accent-500 text-white">Применить</button>
        </div>
        <p class="text-xs text-slate-500 mt-2"><span id="accBulkSelection">Выбрано: 0</span> · Меняются только локальные теги и группы, без действий в Telegram.</p>
      </form>`}
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
              <th><input id="accSelectAll" type="checkbox" aria-label="Выбрать показанные аккаунты" ${isReadOnlyRole() ? "disabled" : ""} /></th><th>ID</th><th>Аккаунт</th><th>Статус</th><th>Membership</th>
              <th>Режим AI</th><th class="text-right">Диалоги</th><th class="text-right">В очереди</th>
              <th>Последняя активность</th><th></th>
            </tr>
          </thead>
          <tbody id="accountsBody">
            <tr><td colspan="10" class="text-center text-slate-500 py-8">Загрузка…</td></tr>
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
  $("#accSelectAll")?.addEventListener("change", (ev) => {
    $$(".acc-select").forEach((box) => {
      box.checked = ev.currentTarget.checked;
      const id = Number(box.value);
      if (box.checked) state.accounts.selectedIds.add(id);
      else state.accounts.selectedIds.delete(id);
    });
    updateAccountBulkSelection();
  });
  $("#accountBulkMetadataForm")?.addEventListener("submit", onBulkAccountMetadataSubmit);
  await refreshAccountsTable();
  await loadAccountBulkGroups();
  await loadTdataProxyGroups();
  if (accountId) await loadAccountEditor(accountId);
}

function updateAccountBulkSelection() {
  const label = $("#accBulkSelection");
  if (label) label.textContent = `Выбрано: ${state.accounts.selectedIds.size}`;
  const boxes = $$(".acc-select");
  const all = $("#accSelectAll");
  if (all) {
    all.checked = boxes.length > 0 && boxes.every((box) => box.checked);
    all.indeterminate = boxes.some((box) => box.checked) && !all.checked;
  }
}

async function loadAccountBulkGroups() {
  const select = $("#accBulkGroup");
  if (!select) return;
  try {
    const groups = await api("/business/groups");
    select.innerHTML = '<option value="0">— без группы —</option>' + groups.map((group) =>
      `<option value="${Number(group.id)}">${escapeHTML(group.name)}</option>`).join("");
  } catch (error) {
    toast(`Не удалось загрузить группы: ${error.message}`, "error");
  }
}

async function onBulkAccountMetadataSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const ids = [...state.accounts.selectedIds].sort((a, b) => a - b);
  const tags = (data.get("tags") || "").toString().split(",").map((tag) => tag.trim()).filter(Boolean);
  const groupId = Number(data.get("group_id") || 0);
  if (!ids.length || (!tags.length && groupId <= 0)) {
    toast("Выберите аккаунты и хотя бы один тег или группу", "error");
    return;
  }
  const button = form.querySelector('button[type="submit"]');
  setBusy(button, true, "Сохраняем…");
  try {
    const result = await api("/business/accounts/bulk-metadata", {
      method: "POST", body: { account_ids: ids, action: data.get("action"), tags,
        group_ids: groupId > 0 ? [groupId] : [] },
    });
    toast(`Обновлено аккаунтов: ${result.accounts_changed}`, "success");
    await refreshAccountsTable();
    if (state.current.accountId) await loadAccountEditor(state.current.accountId);
  } catch (error) {
    toast(error.message || "Не удалось изменить аккаунты", "error");
  } finally {
    setBusy(button, false);
  }
}

async function onCreateAccountSubmit(ev) {
  ev.preventDefault();
  const form = ev.currentTarget;
  const phoneInput = form.querySelector('input[name="phone"]');
  const fd = new FormData(form);
  const phone = (fd.get("phone") || "").toString().trim();
  setFieldError(phoneInput, "");
  if (!phone) {
    setFieldError(phoneInput, "Укажите телефон аккаунта.");
    phoneInput?.focus();
    return;
  }
  if (!/^\+?[0-9()\-\s]{6,20}$/.test(phone)) {
    setFieldError(phoneInput, "Похоже на опечатку: нужен номер вида +79990001122.");
    phoneInput?.focus();
    return;
  }
  const body = {
    phone,
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
    state.accounts.selectedIds = new Set([...state.accounts.selectedIds].filter((id) => state.cache.accountById.has(id)));
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
      tbody.innerHTML = `<tr><td colspan="10" class="text-center text-slate-500 py-8">Нет аккаунтов в БД.</td></tr>`;
      updateAccountBulkSelection();
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
          <td><input class="acc-select" type="checkbox" value="${a.id}" aria-label="Выбрать аккаунт #${a.id}" ${state.accounts.selectedIds.has(a.id) ? "checked" : ""} ${isReadOnlyRole() ? "disabled" : ""} /></td>
          <td class="text-slate-500">#${a.id}</td>
          <td>
            <div class="font-medium text-slate-100">${title}</div>
            <div class="text-xs text-slate-500">${escapeHTML(fullName) || "—"} В· ${escapeHTML(a.username || "")}${a.phone ? ' В· ' + escapeHTML(a.phone) : ''}</div>
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
      tr.querySelector(".acc-select")?.addEventListener("change", (event) => {
        if (event.currentTarget.checked) state.accounts.selectedIds.add(accountId);
        else state.accounts.selectedIds.delete(accountId);
        updateAccountBulkSelection();
      });
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
    updateAccountBulkSelection();
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
          <p class="text-xs text-slate-500 mt-1">phone: ${escapeHTML(acc.phone || "—")} В· username: ${escapeHTML(acc.username || "—")} В· отправлено: ${acc.messages_sent} / today ${acc.messages_today}</p>
          <p class="text-xs text-slate-500 mt-1">Источник записи: ${escapeHTML(({ manual_web: "создан вручную в панели", tdata_web: "TData через панель", tdata_bot: "TData через бота", tdata_bot_v2: "TData через бота" })[acc.import_source] || "неизвестен (старая запись)")} В· создан: ${escapeHTML(fmtDate(acc.imported_at || acc.created_at))}</p>
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
        <div class="col-span-full border border-ink-700 rounded p-3 text-xs">
          <div class="flex items-center justify-between gap-3"><strong class="text-slate-200">Предпросмотр прогрева</strong><button id="accWarmupPreviewRefresh" type="button" class="text-accent-300 hover:text-accent-200">Обновить</button></div>
          <div id="accWarmupPreview" class="mt-2 text-slate-400" aria-live="polite">Проверяем сохранённые настройки…</div>
        </div>
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
    $("#accWarmupPreviewRefresh")?.addEventListener("click", () => loadAccountWarmupPreview(acc.id));
    await loadAccountWarmupPreview(acc.id);

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

async function loadAccountWarmupPreview(accountId) {
  const el = $("#accWarmupPreview");
  if (!el) return;
  el.textContent = "Проверяем сохранённые настройки…";
  try {
    const preview = await api(`/business/warmup/preview?account_id=${accountId}`);
    const profile = preview.profile || {};
    const budget = preview.warmup_daily || {};
    const outbound = preview.outbound_daily || {};
    const actions = (preview.actions || []).map((item) => `<li>${escapeHTML(item.name)}: ${item.available_now ? "доступно" : escapeHTML((item.unavailable_reasons || []).join(", ") || "недоступно")}${item.executed_as !== item.name ? ` · выполняется как ${escapeHTML(item.executed_as)}` : ""}</li>`).join("");
    el.innerHTML = `<p>${preview.ready_now ? "Готов к следующему действию" : "Сейчас не готов"} · профиль: ${escapeHTML(profile.effective_name || "настройки по умолчанию")} (${escapeHTML(profile.source || "—")}) · целей: ${Number(profile.target_count || 0)}</p>
      <p class="mt-1">Бюджет прогрева: ${Number(budget.used || 0)} / ${Number(budget.limit || 0)} · остаток ${Number(budget.remaining || 0)}. Лимит отправки: ${Number(outbound.used || 0)} / ${Number(outbound.limit || 0)}.</p>
      <p class="mt-1">Следующая возможная проверка: ${escapeHTML(fmtDate(preview.next_eligible_at_utc))}${preview.blocking_reasons?.length ? ` · причины: ${escapeHTML(preview.blocking_reasons.join(", "))}` : ""}</p>
      <ul class="mt-2 space-y-1">${actions}</ul>
      <p class="mt-2 text-slate-500">Показаны сохранённые настройки и локальные данные. После изменения полей нажмите «Сохранить».</p>`;
  } catch (error) {
    el.textContent = `Не удалось построить предпросмотр: ${error.message || "ошибка сети"}`;
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
          <button id="dlgAccountsRefresh" aria-label="Обновить список аккаунтов" class="text-xs text-slate-400 hover:text-slate-200">⟳</button>
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
        <div class="px-4 py-3 border-b border-ink-700 flex flex-col gap-2">
          <h3 id="dlgListTitle" class="font-medium text-slate-200 text-sm">${accountId ? `Диалоги аккаунта #${accountId}` : 'Все диалоги'}</h3>
          <div class="flex items-center flex-wrap gap-2">
            <button id="dlgSoundToggle" aria-label="Звук новых сообщений" aria-pressed="${state.dialogs.soundEnabled}" class="text-xs px-2 py-1 rounded border ${state.dialogs.soundEnabled ? 'border-accent-500 text-accent-300' : 'border-ink-600 text-slate-400'}">${state.dialogs.soundEnabled ? '🔔 Звук включён' : '🔕 Звук выключен'}</button>
            <button id="dlgWaitingToggle" aria-pressed="${state.dialogs.waitingOnly}" class="text-xs px-2 py-1 rounded border ${state.dialogs.waitingOnly ? 'border-amber-500 text-amber-300' : 'border-ink-600 text-slate-400'}">Ждут ответа</button>
            <button id="dlgUnreadToggle" aria-pressed="${state.dialogs.unreadOnly}" class="text-xs px-2 py-1 rounded border ${state.dialogs.unreadOnly ? 'border-accent-500 text-accent-300' : 'border-ink-600 text-slate-400'}">Непрочитанные</button>
            <button id="dlgListRefresh" aria-label="Обновить список диалогов" class="text-xs text-slate-400 hover:text-slate-200">⟳</button>
          </div>
        </div>
        <div class="px-4 py-2 border-b border-ink-700">
          <input id="dlgPeerSearch" type="search" aria-label="Поиск диалогов" placeholder="ID, @username или последняя реплика…" value="${escapeHTML(state.dialogs.peerSearch)}" class="w-full bg-ink-800 border border-ink-700 rounded px-2 py-1.5 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:border-accent-500" />
        </div>
        <div id="dlgList" class="flex-1 overflow-y-auto cb-scroll">
          <div class="p-4 text-slate-500 text-sm">Загрузка…</div>
        </div>
      </section>
      ${peerId ? `
        <section class="col-span-5 min-w-0 min-h-0 flex flex-col overflow-hidden">
          <div class="px-4 py-3 border-b border-ink-700 flex items-center justify-between gap-2">
            <div class="min-w-0">
              <h3 class="font-medium text-slate-200 text-sm truncate" id="dlgChatTitle">Диалог с ${peerId}</h3>
              <div class="text-xs text-slate-500" id="dlgChatSub">—</div>
              ${isReadOnlyRole() ? '' : '<div class="text-[11px] text-slate-500" id="dlgExportHistory"></div>'}
            </div>
            <div class="flex items-center gap-2">
              ${isReadOnlyRole() ? '' : '<button id="dlgMarkRead" class="text-xs px-2 py-1 rounded border border-ink-600 text-slate-300 hover:bg-ink-700">Прочитано</button>'}
              ${isReadOnlyRole() ? '' : '<button id="dlgExportCsv" class="text-xs px-2 py-1 rounded border border-ink-600 text-slate-300 hover:bg-ink-700">CSV</button><button id="dlgExportTxt" class="text-xs px-2 py-1 rounded border border-ink-600 text-slate-300 hover:bg-ink-700">TXT</button>'}
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
  if ($("#dlgListRefresh")) {
    $("#dlgListRefresh").addEventListener("click", () => loadDialogsList(accountId, peerId));
  }
  $("#dlgList")?.addEventListener("click", (event) => {
    if (event.target.closest("#dlgLoadMore")) loadDialogsList(accountId, peerId, { append: true });
  });
  $("#dlgPeerSearch")?.addEventListener("input", (event) => {
    state.dialogs.peerSearch = event.target.value || "";
    clearTimeout(dialogsSearchTimer);
    dialogsSearchTimer = setTimeout(() => loadDialogsList(accountId, peerId), 250);
  });
  $("#dlgWaitingToggle")?.addEventListener("click", () => {
    state.dialogs.waitingOnly = !state.dialogs.waitingOnly;
    navigate(`#/dialogs/${accountId || ""}`);
  });
  $("#dlgSoundToggle")?.addEventListener("click", () => {
    state.dialogs.soundEnabled = !state.dialogs.soundEnabled;
    localStorage.setItem(DIALOG_SOUND_KEY, state.dialogs.soundEnabled ? "1" : "0");
    const button = $("#dlgSoundToggle");
    button.setAttribute("aria-pressed", String(state.dialogs.soundEnabled));
    button.className = `text-xs px-2 py-1 rounded border ${state.dialogs.soundEnabled ? 'border-accent-500 text-accent-300' : 'border-ink-600 text-slate-400'}`;
    button.textContent = state.dialogs.soundEnabled ? '🔔 Звук включён' : '🔕 Звук выключен';
    if (state.dialogs.soundEnabled) playDialogSound();
  });
  $("#dlgUnreadToggle")?.addEventListener("click", () => {
    state.dialogs.unreadOnly = !state.dialogs.unreadOnly;
    navigate(`#/dialogs/${accountId || ""}`);
  });
  $("#dlgMarkRead")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    setBusy(button, true, "Сохраняем…");
    try {
      await api(`/business/accounts/${accountId}/dialogs/${peerId}/read`, { method: "POST" });
      await loadDialogsList(accountId, peerId);
      toast("Диалог отмечен прочитанным", "success");
    } catch (error) {
      toast(error.message || "Не удалось отметить диалог", "error");
    } finally {
      setBusy(button, false);
    }
  });
  for (const format of ["csv", "txt"]) {
    $(format === "csv" ? "#dlgExportCsv" : "#dlgExportTxt")?.addEventListener("click", (event) =>
      downloadDialogExport(accountId, peerId, format, event.currentTarget));
  }
  if (peerId) {
    $("#dlgDeleteBtn").addEventListener("click", () => deleteCurrentDialog(accountId, peerId));
  }

  // force=true: при заходе в раздел всегда тянем актуальные last_dialog_at,
  // чтобы порядок «как в мессенджере» отражал свежие сообщения.
  await loadDialogsAccounts(accountId, peerId, { force: true });
  await loadDialogsGroups();
  await loadDialogsList(accountId, peerId);
  if (accountId && peerId) await loadDialogMessages(accountId, peerId);
  if (accountId && peerId && !isReadOnlyRole()) await loadDialogExportHistory(accountId, peerId);
}

async function loadDialogExportHistory(accountId, peerId) {
  const label = $("#dlgExportHistory");
  if (!label) return;
  try {
    const rows = await api(`/business/accounts/${accountId}/dialogs/${peerId}/exports?limit=20`);
    label.textContent = rows.length
      ? `Последняя выгрузка: ${fmtDate(rows[0].created_at)} · ${rows[0].message_count} сообщ. · ${rows[0].format.toUpperCase()}`
      : "Выгрузок ещё не было";
  } catch (error) {
    label.textContent = `История выгрузок: ${error.message}`;
  }
}

async function downloadDialogExport(accountId, peerId, format, button) {
  setBusy(button, true, "…");
  try {
    const response = await fetch(`${API}/business/accounts/${accountId}/dialogs/${peerId}/export?format=${format}`, {
      headers: { Authorization: `Bearer ${state.token}` }, cache: "no-store",
    });
    if (response.status === 401) {
      handleLogout(true);
      throw new Error("Требуется повторный вход");
    }
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const data = await response.json();
        if (data?.detail) detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
      } catch {}
      throw new Error(detail);
    }
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = `account-${accountId}-dialog-${peerId}.${format}`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    toast("Переписка выгружена; запись добавлена в журнал", "success");
    await loadDialogExportHistory(accountId, peerId);
  } catch (error) {
    toast(error.message || "Не удалось выгрузить диалог", "error");
  } finally {
    setBusy(button, false);
  }
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
      tgLabel = `${tgFull} В· ${tgHandle}`;
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
            ${tgLabel ? `<span class="text-[11px] text-slate-400 ml-1">В· ${escapeHTML(tgLabel)}</span>` : ''}
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

async function loadDialogsList(accountId, activePeerId, options = {}) {
  const el = $("#dlgList");
  if (!el) return;
  const requestNo = ++state.dialogs.listRequestNo;
  const globalMode = !accountId;
  const context = JSON.stringify([accountId, state.dialogs.peerSearch, state.dialogs.waitingOnly, state.dialogs.unreadOnly]);
  const sameContext = state.dialogs.listContext === context;
  const append = globalMode && options.append === true && sameContext;
  const refresh = globalMode && options.refresh === true && sameContext && state.dialogs.listRows.length > 0;
  const loadedCount = state.dialogs.listRows.length;
  if (!append && !refresh) {
    state.dialogs.listContext = context;
    state.dialogs.listOffset = 0;
    state.dialogs.listRows = [];
  }
  try {
    const params = new URLSearchParams({
      limit: globalMode ? "50" : "200", waiting_only: String(state.dialogs.waitingOnly),
      unread_only: String(state.dialogs.unreadOnly), q: state.dialogs.peerSearch,
    });
    if (globalMode) params.set("offset", String(append ? state.dialogs.listOffset : 0));
    const endpoint = accountId
      ? `/business/accounts/${accountId}/dialogs?${params.toString()}`
      : `/business/dialogs?${params.toString()}`;
    let list;
    let moreAvailable;
    if (refresh) {
      const pageCount = Math.ceil(loadedCount / 50);
      const pages = await Promise.all(Array.from({ length: pageCount }, async (_, index) => {
        const pageParams = new URLSearchParams(params);
        pageParams.set("offset", String(index * 50));
        return api(`/business/dialogs?${pageParams.toString()}`);
      }));
      list = pages.flat();
      moreAvailable = pages.length > 0 && pages[pages.length - 1].length >= 50;
    } else {
      list = await api(endpoint);
      moreAvailable = list.length >= 50;
    }
    if (requestNo !== state.dialogs.listRequestNo || state.route !== "dialogs" ||
        state.current.accountId !== accountId || context !== JSON.stringify([accountId, state.dialogs.peerSearch, state.dialogs.waitingOnly, state.dialogs.unreadOnly])) return;
    const pageLength = list.length;
    if (globalMode) {
      const rows = append ? state.dialogs.listRows.concat(list) : list;
      const seen = new Set();
      state.dialogs.listRows = rows.filter((row) => {
        const key = String(row.account_id) + ":" + String(row.peer_user_id);
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
      state.dialogs.listOffset = append ? state.dialogs.listOffset + pageLength : pageLength;
      list = state.dialogs.listRows;
    }
    if (!list.length) {
      el.innerHTML = `<div class="p-4 text-slate-500 text-sm">${state.dialogs.peerSearch.trim() ? 'По запросу диалоги не найдены.' : state.dialogs.unreadOnly ? 'Нет непрочитанных диалогов.' : state.dialogs.waitingOnly ? 'Нет диалогов, ожидающих ответа.' : accountId ? 'У этого аккаунта пока нет диалогов в нейрочате.' : 'Диалогов пока нет.'}</div>`;
      return;
    }
    const rowsHTML = list.map(d => {
      const title = d.client_username ? `@${d.client_username}` : `id ${d.peer_user_id}`;
      const rowAccountId = Number(d.account_id ?? accountId);
      const account = (state.cache.accounts || []).find((item) => Number(item.id) === rowAccountId);
      const accountTitle = account?.list_label || account?.username || account?.phone || `#${rowAccountId}`;
      const isActive = d.peer_user_id === activePeerId;
      const lastWho = d.last_role === "assistant" ? "Бот" : "Клиент";
      return `
        <a href="#/dialogs/${rowAccountId}/${d.peer_user_id}" class="block px-4 py-3 border-b border-ink-700 ${isActive ? 'bg-ink-800' : 'hover:bg-ink-800/60'}">
          <div class="flex items-center justify-between gap-2">
            <div class="min-w-0 text-sm font-medium text-slate-100 truncate">${escapeHTML(title)}</div>
            <div class="flex items-center gap-2 shrink-0">${d.unread_count ? `<span class="pill pill-amber" title="Непрочитанных входящих">${Number(d.unread_count)}</span>` : ''}${d.waiting_for_reply ? '<span class="text-[11px] text-amber-300">ждёт ответа</span>' : ''}<span class="text-[11px] text-slate-500">${fmtRelative(d.last_message_at)}</span></div>
          </div>
          ${accountId ? '' : `<div class="text-[11px] text-slate-500 truncate mt-0.5">Аккаунт ${escapeHTML(accountTitle)}</div>`}
          <div class="text-xs text-slate-400 truncate mt-0.5"><span class="text-slate-500">${lastWho}:</span> ${escapeHTML(d.last_message || "")}</div>
          <div class="text-[11px] text-slate-500 mt-1">${d.messages_count} сообщ.</div>
        </a>
      `;
    }).join("");
    const hasMore = globalMode && moreAvailable && state.dialogs.listOffset < 10000;
    el.innerHTML = rowsHTML + (hasMore ? '<button id="dlgLoadMore" class="w-full px-4 py-3 text-sm text-accent-300 hover:bg-ink-800">Показать ещё</button>' : "");
  } catch (e) {
    if (requestNo !== state.dialogs.listRequestNo) return;
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
    case "uncertain": return '<span class="qpill qpill-fail">исход неизвестен · проверьте диалог</span>';
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
    metaExtra = ` В· ${_queueStatusBadge(msg.queue_status)}`;
    if (msg.queue_attempts) metaExtra += ` В· попытка ${msg.queue_attempts}`;
    if (msg.queue_error) {
      metaExtra += ` В· <span class="text-rose-400" title="${escapeHTML(msg.queue_error)}">${escapeHTML(msg.queue_error.slice(0, 60))}</span>`;
    }
  }

  let actions = "";
  if (isQueue) {
    if (msg.queue_status === "failed" || msg.queue_status === "cancelled" || msg.queue_status === "uncertain") {
      actions += `<button class="qbtn qbtn-retry" data-action="retry" data-qid="${msg.queue_id}" data-q-status="${escapeHTML(msg.queue_status)}">Повторить</button>`;
    }
    if (msg.queue_status === "pending" || msg.queue_status === "failed") {
      actions += `<button class="qbtn qbtn-cancel" data-action="cancel" data-qid="${msg.queue_id}">Отменить</button>`;
    }
  }

  wrap.innerHTML = `
    <div class="${bubbleCls}">${escapeHTML(msg.content || "")}</div>
    <div class="bubble-meta">${isAssistant ? "бот" : "клиент"} В· ${fmtDate(msg.created_at)}${metaExtra}</div>
    ${actions ? `<div class="bubble-actions">${actions}</div>` : ""}
  `;

  if (actions) {
    wrap.querySelectorAll("button[data-action]").forEach((b) => {
      b.addEventListener("click", () => onQueueAction(b.dataset.action, parseInt(b.dataset.qid, 10), b.dataset.qStatus));
    });
  }

  el.appendChild(wrap);
  el.scrollTop = el.scrollHeight;
}

async function onQueueAction(action, queueId, queueStatus) {
  if (!queueId) return;
  try {
    if (action === "retry") {
      if (queueStatus === "uncertain" && !confirmDanger("Исход отправки неизвестен. Проверьте этот диалог в Telegram: повтор может отправить сообщение второй раз. Повторить?")) return;
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
  if (m.queued_start) {
    buttons.push(`<button class="px-2 py-1 rounded bg-rose-700 hover:bg-rose-600 text-xs" data-mail-act="cancel-start" data-mid="${m.id}">✕ Отменить запуск</button>`);
  } else if (m.status === "running") {
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
  if (action === "cancel-start" && !confirm(`Отменить ожидающий запуск рассылки #${mailingId}?`)) return;
  try {
    let body;
    if (action === "start") {
      const preview = await api(`/business/mailings/${mailingId}/audience-preview`);
      if (!preview.eligible_count) throw new Error("Нет получателей с подтверждённым правом на контакт. Проверьте базу клиентов и список тестовых адресатов.");
      const sample = preview.sample.map(item => item.username ? `@${item.username}` : `#${item.client_id}`).join(", ");
      const scheduleInput = $("#mailStartAt");
      const localTime = scheduleInput?.value || "";
      const planned = localTime ? new Date(localTime) : null;
      if (planned && (Number.isNaN(planned.getTime()) || planned <= new Date())) throw new Error("Время запуска должно быть в будущем");
      if (planned) body = { scheduled_at: planned.toISOString() };
      const when = planned ? `Запланировать на ${planned.toLocaleString("ru-RU")}` : "Запустить сейчас";
      if (!confirm(`${when} рассылку #${mailingId}? Получателей: ${preview.eligible_count}. Пример: ${sample}`)) return;
    }
    const r = await api(`/business/mailings/${mailingId}/${action}`, { method: "POST", ...(body ? { body } : {}) });
    if (r.status === "queued") {
      toast(`Команда ${action} поставлена в очередь`, "success", 1500);
    } else {
      toast(`${action}: ${r.detail || r.status}`, "info", 1500);
    }
    setTimeout(() => {
      if (window.location.hash === `#/mailings/${mailingId}`) loadMailingDetail(mailingId);
      else loadMailingsList();
    }, 500);
  } catch (e) { toast(`Ошибка ${action}: ${e.message}`, "error"); }
}

function minuteToClock(value) {
  if (value == null || value === "") return "";
  const minute = Number(value);
  if (!Number.isInteger(minute) || minute < 0 || minute > 1439) return "";
  return `${String(Math.floor(minute / 60)).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`;
}

function renderNeuroHoursSettings(mailing, editLocked = false) {
  const start = minuteToClock(mailing.neuro_active_start_minute);
  const end = minuteToClock(mailing.neuro_active_end_minute);
  const zone = mailing.neuro_timezone || "UTC";
  const allDay = mailing.neuro_active_start_minute == null && mailing.neuro_active_end_minute == null;
  const summary = allDay ? "Круглосуточно" : `${start || "—"}–${end || "—"}`;
  if (isReadOnlyRole()) {
    return `<div class="border-t border-ink-700 pt-4"><h4 class="font-medium text-slate-200 mb-1">Часы активности AI-ответов</h4><p class="text-xs text-slate-400">${escapeHTML(summary)} · ${escapeHTML(zone)}</p><p class="text-xs text-slate-500 mt-1">Только просмотр</p></div>`;
  }
  const disabled = editLocked ? "disabled" : "";
  return `<form id="neuroHoursForm" class="border-t border-ink-700 pt-4 space-y-3" novalidate>
    <div><h4 class="font-medium text-slate-200 mb-1">Часы активности AI-ответов</h4><p id="neuroHoursSummary" class="text-xs text-slate-400">Текущее окно: ${escapeHTML(summary)} · ${escapeHTML(zone)}</p></div>
    <p class="text-xs text-slate-500">Оставьте начало и конец пустыми для работы круглосуточно. Время задаётся в выбранном часовом поясе; одинаковые значения недопустимы.</p>
    <div class="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
      <label class="block"><span class="text-slate-400 text-xs">Начало (HH:MM)</span><input name="neuro_active_start" type="time" value="${start}" ${disabled} class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" /></label>
      <label class="block"><span class="text-slate-400 text-xs">Конец (HH:MM)</span><input name="neuro_active_end" type="time" value="${end}" ${disabled} class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" /></label>
      <label class="block"><span class="text-slate-400 text-xs">Часовой пояс (IANA)</span><input name="neuro_timezone" type="text" value="${escapeHTML(zone)}" placeholder="Europe/Samara" ${disabled} class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 font-mono" /></label>
    </div>
    <div class="flex items-center gap-3"><button type="submit" ${disabled} class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white disabled:opacity-50 disabled:cursor-not-allowed">Сохранить часы</button><span id="neuroHoursMsg" class="text-xs text-slate-400" aria-live="polite"></span></div>
  </form>`;
}

function bindNeuroHoursForm(id) {
  const form = $("#neuroHoursForm");
  if (!form) return;
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const fd = new FormData(ev.currentTarget);
    const startText = (fd.get("neuro_active_start") || "").toString().trim();
    const endText = (fd.get("neuro_active_end") || "").toString().trim();
    const timezone = (fd.get("neuro_timezone") || "").toString().trim();
    const out = $("#neuroHoursMsg");
    const fail = message => { out.textContent = message; out.className = "text-xs text-rose-400"; };
    if (!!startText !== !!endText) return fail("Укажите оба времени или оставьте оба пустыми для режима 24 часа.");
    if (!timezone || timezone.length > 100) return fail("Укажите часовой пояс IANA.");
    const toMinutes = value => {
      if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(value)) return null;
      const [hours, minutes] = value.split(":").map(Number);
      return hours * 60 + minutes;
    };
    const start = startText ? toMinutes(startText) : null;
    const end = endText ? toMinutes(endText) : null;
    if (startText && (start == null || end == null)) return fail("Введите время в формате HH:MM в диапазоне 00:00–23:59.");
    if (start != null && end != null && start === end) return fail("Начало и конец окна должны различаться.");
    const body = { neuro_active_start_minute: start, neuro_active_end_minute: end, neuro_timezone: timezone };
    out.textContent = "…";
    try {
      await api(`/business/mailings/${id}`, { method: "PATCH", body });
      toast("Часы активности AI-ответов сохранены", "success");
      out.textContent = "Сохранено";
      out.className = "text-xs text-emerald-300";
      await loadMailingDetail(id);
    } catch (error) {
      fail(error.message);
    }
  });
}

function renderNeuroDailyReplyLimit(mailing, editLocked = false) {
  const value = Number.isInteger(mailing.neuro_daily_reply_limit) ? mailing.neuro_daily_reply_limit : 0;
  if (isReadOnlyRole()) {
    return `<div class="border-t border-ink-700 pt-4"><h4 class="font-medium text-slate-200 mb-1">Дневной лимит AI-ответов</h4><p class="text-xs text-slate-400">${value === 0 ? "Без лимита" : `${value} успешных AI-ответов в день`}</p><p class="text-xs text-slate-500 mt-1">Локальный день зоны ${escapeHTML(mailing.neuro_timezone || "UTC")}. Это лимит AI-ответов, не лимит Telegram.</p><p class="text-xs text-slate-500 mt-1">Только просмотр</p></div>`;
  }
  const disabled = editLocked ? "disabled" : "";
  return `<form id="neuroDailyReplyLimitForm" class="border-t border-ink-700 pt-4 space-y-3" novalidate>
    <div><h4 class="font-medium text-slate-200 mb-1">Дневной лимит AI-ответов</h4><p class="text-xs text-slate-500">Максимум успешных AI-ответов на эту рассылку за локальный день её часового пояса (${escapeHTML(mailing.neuro_timezone || "UTC")}). Это не лимит Telegram. 0 = без лимита.</p></div>
    <div class="flex flex-wrap items-end gap-3"><label class="block"><span class="text-slate-400 text-xs">Успешных AI-ответов в день</span><input name="neuro_daily_reply_limit" type="number" min="0" max="10000" step="1" value="${value}" ${disabled} class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" /></label><button type="submit" ${disabled} class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white disabled:opacity-50 disabled:cursor-not-allowed">Сохранить лимит</button><span id="neuroDailyReplyLimitMsg" class="text-xs text-slate-400" aria-live="polite"></span></div>
  </form>`;
}

function bindNeuroDailyReplyLimitForm(id) {
  const form = $("#neuroDailyReplyLimitForm");
  if (!form) return;
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const raw = (new FormData(ev.currentTarget).get("neuro_daily_reply_limit") || "").toString().trim();
    const value = Number(raw);
    const out = $("#neuroDailyReplyLimitMsg");
    const fail = message => { out.textContent = message; out.className = "text-xs text-rose-400"; };
    if (!/^(0|[1-9]\d*)$/.test(raw) || !Number.isInteger(value) || value < 0 || value > 10000) {
      return fail("Введите целое число от 0 до 10000.");
    }
    out.textContent = "…";
    try {
      await api(`/business/mailings/${id}`, { method: "PATCH", body: { neuro_daily_reply_limit: value } });
      toast("Дневной лимит AI-ответов сохранён", "success");
      out.textContent = "Сохранено";
      out.className = "text-xs text-emerald-300";
      await loadMailingDetail(id);
    } catch (error) {
      fail(error.message);
    }
  });
}

async function loadMailingDetail(id) {
  const root = $("#mailDetail");
  try {
    const [m, groups] = await Promise.all([
      api(`/business/mailings/${id}`),
      api(`/business/groups`).catch(() => []),
    ]);
    const editLocked = m.status === "running" || m.queued_start;
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
        ${!m.queued_start && m.status !== "running" ? `<label class="block text-xs text-slate-400">Запланировать запуск (местное время)
          <input id="mailStartAt" type="datetime-local" class="mt-1 block rounded bg-ink-800 border border-ink-600 px-2 py-1 text-slate-100" />
        </label>` : ""}

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
              <span class="text-slate-400 text-xs">Community link (для {link}) — вставьте сюда /r/… из раздела «Ссылки», и переходы посчитаются</span>
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
            <label class="flex items-center gap-2 mt-6 text-slate-300">
              <input name="use_typing" type="checkbox" ${m.use_typing ? "checked" : ""} ${editLocked ? "disabled" : ""}
                     class="rounded border-ink-600 bg-ink-800" />
              Имитация «печатает…» (пауза 5–10с)
            </label>
            <label class="flex items-center gap-2 mt-6 text-slate-300">
              <input name="smart_delay" type="checkbox" ${m.smart_delay ? "checked" : ""} ${editLocked ? "disabled" : ""}
                     class="rounded border-ink-600 bg-ink-800" />
              Умная задержка (±30% джиттер)
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">Порядок вариантов</span>
              <select name="variant_mode" ${editLocked ? "disabled" : ""}
                class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
                ${["random","sequential"].map(v => `<option value="${v}" ${(m.variant_mode || "random") === v ? "selected" : ""}>${v}</option>`).join("")}
              </select>
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">Лимит успешных за запуск (0 = нет)</span>
              <input name="max_recipients" type="number" min="0" max="1000000" value="${m.max_recipients ?? 0}" ${editLocked ? "disabled" : ""}
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">Кулдаун аккаунта после пакета (ч)</span>
              <input name="mailing_cooldown_hours" type="number" step="0.1" min="0" max="168" value="${m.mailing_cooldown_hours ?? 12}" ${editLocked ? "disabled" : ""}
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">Статус клиентов (фильтр)</span>
              <select name="audience_client_status" ${editLocked ? "disabled" : ""}
                class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
                ${["new","open"].map(v => `<option value="${v}" ${(m.audience_client_status || "new") === v ? "selected" : ""}>${v}</option>`).join("")}
              </select>
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">Только классы (через запятую)</span>
              <input name="audience_include_classes" value="${escapeHTML((m.audience_include_classes || []).join(", "))}" ${editLocked ? "disabled" : ""}
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 font-mono" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">Исключить классы (через запятую)</span>
              <input name="audience_exclude_classes" value="${escapeHTML((m.audience_exclude_classes || []).join(", "))}" ${editLocked ? "disabled" : ""}
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 font-mono" />
            </label>
            <div class="md:col-span-3 flex items-center gap-3">
              <button type="submit" ${editLocked ? "disabled" : ""}
                class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white disabled:opacity-50 disabled:cursor-not-allowed">Сохранить рассылку</button>
              <span id="mailEditMsg" class="text-xs text-slate-400"></span>
            </div>
          </form>
        </div>

        <div class="card">
          <div class="flex items-center justify-between mb-1 flex-wrap gap-2">
            <h3 class="font-semibold">Аудитория перед запуском</h3>
            <span id="mailAudienceCount" class="text-xs text-slate-500">Загрузка…</span>
          </div>
          <p class="text-xs text-slate-500 mb-2">Для обычной рассылки нужны подтверждённое право на контакт и отсутствие отказа. Импортированный username сам по себе не даёт разрешения.</p>
          <div id="mailAudienceSample" class="text-xs text-slate-300 mb-4"></div>
          <div class="flex items-center justify-between mb-1 flex-wrap gap-2">
            <h3 class="font-semibold">Тестовые получатели</h3>
            <span id="mailTestCount" class="text-xs text-slate-500">…</span>
          </div>
          <p class="text-xs text-slate-500 mb-3">Режим <b>test</b>: каждый подключённый аккаунт пишет каждому адресату из списка. Добавляйте свои тестовые аккаунты и подтверждайте право на контакт в карточке клиента.</p>
          <textarea id="mailTestUsers" rows="3" ${editLocked ? "disabled" : ""}
            placeholder="@username1&#10;username2"
            class="w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 font-mono text-xs"></textarea>
          <div class="flex items-center gap-3 mt-2">
            <button id="mailTestSave" ${editLocked ? "disabled" : ""}
              class="px-4 py-2 rounded-lg bg-ink-700 hover:bg-ink-600 text-slate-200 text-sm disabled:opacity-50">Сохранить тестовый список</button>
            <span id="mailTestMsg" class="text-xs text-slate-400"></span>
          </div>
          <div class="border-t border-ink-700 pt-4 mt-4">
            <h4 class="font-medium text-slate-200 mb-2">История запусков</h4>
            <p class="text-xs text-slate-500 mb-2">Состав адресатов фиксируется при старте. Отказ от контакта проверяется и после старта.</p>
            <div id="mailRuns" class="text-xs text-slate-300">Загрузка…</div>
          </div>
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

          ${renderNeuroHoursSettings(m, editLocked)}
          ${renderNeuroDailyReplyLimit(m, editLocked)}

          <div class="border-t border-ink-700 pt-4">
            <div class="flex items-center justify-between mb-2">
              <h4 class="font-medium text-slate-200">System-промпт</h4>
              <div id="mailPromptStatus" class="text-xs text-slate-500">…</div>
            </div>
            <p class="text-xs text-slate-500 mb-3">Активный промпт и его версии сохраняются для этой рассылки. После сброса используется <code>DEFAULT_NEURO_SYSTEM_PROMPT</code>. Доступные плейсхолдеры см. в боте.</p>
            <textarea id="mailPromptText" rows="14"
              ${isReadOnlyRole() ? "readonly" : ""}
              class="w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 font-mono text-xs">Загрузка…</textarea>
            <div class="flex items-center gap-3 mt-3">
              <button id="mailPromptSave" ${isReadOnlyRole() ? "disabled" : ""} class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white text-sm disabled:opacity-50">Сохранить промпт</button>
              <button id="mailPromptReset" ${isReadOnlyRole() ? "disabled" : ""} class="px-3 py-2 rounded-lg bg-ink-700 hover:bg-ink-600 text-slate-200 text-sm disabled:opacity-50">Сбросить к DEFAULT</button>
              <span id="mailPromptMsg" class="text-xs text-slate-400"></span>
            </div>
            <div class="border-t border-ink-700 mt-4 pt-4">
              <div class="flex items-center justify-between gap-2 flex-wrap mb-1">
                <h4 class="font-medium text-slate-200">База знаний</h4>
                ${isReadOnlyRole() ? '<span class="text-xs text-slate-500">Только просмотр</span>' : '<button id="mailKnowledgeNew" type="button" class="px-2 py-1 rounded bg-ink-700 hover:bg-ink-600 text-xs">Добавить фрагмент</button>'}
              </div>
              <p class="text-xs text-slate-500 mb-3">Справочные фрагменты подбираются по совпадению ключевых слов. Без ключевых слов фрагмент используется как общая справка. Семантического поиска нет.</p>
              <div id="mailKnowledgeList" class="space-y-2 text-sm">Загрузка…</div>
              ${isReadOnlyRole() ? '' : `<form id="mailKnowledgeForm" class="hidden mt-3 border border-ink-700 rounded-lg p-3 space-y-2" novalidate>
                <input name="title" maxlength="120" required placeholder="Название (до 120 символов)" class="w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
                <textarea name="content" maxlength="3000" required rows="4" placeholder="Справочный текст (до 3000 символов)" class="w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100"></textarea>
                <input name="keywords" maxlength="659" placeholder="Ключевые слова через запятую (до 10; пусто — общая справка)" class="w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
                <label class="flex items-center gap-2 text-xs text-slate-300"><input name="enabled" type="checkbox" checked class="rounded border-ink-600 bg-ink-800" /> Фрагмент включён</label>
                <div class="flex items-center gap-2"><button type="submit" class="px-3 py-1.5 rounded bg-accent-600 text-white text-xs">Сохранить</button><button id="mailKnowledgeCancel" type="button" class="px-3 py-1.5 rounded bg-ink-700 text-slate-200 text-xs">Отмена</button><span id="mailKnowledgeMsg" class="text-xs text-slate-400" aria-live="polite"></span></div>
              </form>`}
            </div>
            <details class="border-t border-ink-700 mt-4 pt-4 text-sm">
              <summary class="cursor-pointer text-slate-200">История версий промпта</summary>
              <div id="mailPromptVersions" class="mt-3 space-y-2 text-xs text-slate-400">Загрузка…</div>
              <pre id="mailPromptVersionPreview" class="hidden mt-3 p-3 bg-ink-800 rounded text-xs whitespace-pre-wrap break-words max-h-72 overflow-y-auto"></pre>
              ${isReadOnlyRole() ? '' : '<button id="mailPromptUseVersion" type="button" class="hidden mt-2 px-2 py-1 rounded bg-ink-700 text-xs text-slate-200">Подставить в поле для сравнения</button>'}
            </details>
            <div class="border-t border-ink-700 mt-4 pt-4 text-sm">
              <h5 class="font-medium text-slate-200 mb-1">Сравнить ответ до сохранения</h5>
              <p class="text-xs text-slate-500 mb-2">Каждая строка — следующий ход синтетического диалога (до 3). Сравнение обращается к выбранному провайдеру до 6 раз; в Telegram сообщения не отправляются.</p>
              <textarea id="mailPromptSample" rows="3" maxlength="3002" placeholder="Здравствуйте!\nРасскажите подробнее.\nСколько это стоит?" class="w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100"></textarea>
              <div class="flex flex-wrap items-end gap-3 mt-2">
                <label class="text-xs text-slate-400">Контекст аккаунта (ID, необязательно)<input id="mailPromptAccountId" type="number" min="1" class="block mt-1 w-36 bg-ink-800 border border-ink-600 rounded px-2 py-1.5 text-slate-100" /></label>
                <button id="mailPromptCompare" ${isReadOnlyRole() ? "disabled" : ""} class="px-3 py-2 rounded-lg bg-ink-700 hover:bg-ink-600 text-slate-200 disabled:opacity-50">Сравнить</button>
                <span id="mailPromptCompareStatus" class="text-xs text-slate-400" aria-live="polite"></span>
              </div>
              <div class="grid md:grid-cols-2 gap-3 mt-3"><div><div class="text-xs text-slate-400 mb-1">Сохранённый промпт</div><pre id="mailPromptSavedReply" class="text-xs whitespace-pre-wrap break-words bg-ink-800 rounded p-3 min-h-16">—</pre></div><div><div class="text-xs text-slate-400 mb-1">Текст в поле сейчас</div><pre id="mailPromptCandidateReply" class="text-xs whitespace-pre-wrap break-words bg-ink-800 rounded p-3 min-h-16">—</pre></div></div>
            </div>
          </div>
        </div>
      </div>
    `;
    bindNeuroHoursForm(id);
    bindNeuroDailyReplyLimitForm(id);
    root.querySelectorAll("button[data-mail-act]").forEach(b => {
      b.addEventListener("click", () => onMailingAction(Number(b.dataset.mid), b.dataset.mailAct));
    });
    try {
      const preview = await api(`/business/mailings/${id}/audience-preview`);
      $("#mailAudienceCount").textContent = `доступно: ${preview.eligible_count}`;
      $("#mailAudienceSample").textContent = preview.sample.map(item => item.username ? `@${item.username}` : `#${item.client_id}`).join(", ") || "Подходящих адресатов нет";
    } catch (error) {
      $("#mailAudienceCount").textContent = error.message || "Ошибка проверки";
    }
    try {
      const runs = await api(`/business/mailings/${id}/runs`);
      $("#mailRuns").innerHTML = runs.length
        ? runs.map(run => `<div class="py-1 border-b border-ink-700">#${Number(run.run_id)} · ${escapeHTML(run.status)}${run.scheduled_at ? ` · запуск ${fmtDate(run.scheduled_at)}` : ""} · ${Number(run.audience_count)} адресатов · отправлено ${Number(run.messages_sent)} / ошибок ${Number(run.messages_failed)} · ${fmtDate(run.created_at)} · конфигурация ${escapeHTML(run.config_sha256.slice(0, 12))}</div>`).join("")
        : "Запусков ещё нет";
    } catch (error) {
      $("#mailRuns").textContent = error.message || "История не загрузилась";
    }
    if (!editLocked) {
      const loadTestRecipients = async () => {
        try {
          const tr = await api(`/business/mailings/${id}/test-recipients`);
          $("#mailTestUsers").value = (tr.usernames || []).map(u => "@" + u).join("\n");
          $("#mailTestCount").textContent = `в списке: ${tr.unique ?? (tr.usernames || []).length}`;
        } catch (e) {
          $("#mailTestCount").textContent = "не загрузилось";
        }
      };
      loadTestRecipients();
      $("#mailTestSave").addEventListener("click", async () => {
        const out = $("#mailTestMsg");
        const usernames = $("#mailTestUsers").value.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
        if (!usernames.length) {
          out.textContent = "Список пуст — нечего сохранять.";
          out.className = "text-xs text-amber-300";
          return;
        }
        out.textContent = "…";
        try {
          const r = await api(`/business/mailings/${id}/test-recipients`, { method: "PUT", body: { usernames } });
          out.textContent = `ok: уникальных ${r.unique}, дубликатов ${r.duplicates}, новых клиентов ${r.created_clients}`;
          out.className = "text-xs text-emerald-300";
          toast("Тестовые получатели сохранены", "success");
          loadTestRecipients();
        } catch (e) {
          out.textContent = e.message;
          out.className = "text-xs text-rose-400";
        }
      });
      $("#mailEditForm").addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const fd = new FormData(ev.currentTarget);
        const variantsRaw = (fd.get("message_variants") || "").toString();
        const variants = variantsRaw.split("\n").map(s => s.trim()).filter(Boolean);
        const auto = Number(fd.get("auto_stop_hours") || 0);
        const cap = Number(fd.get("max_recipients") || 0);
        const splitClasses = (v) => (v || "").toString().split(/[,;\s]+/).map(s => s.trim()).filter(Boolean);
        const body = {
          name: (fd.get("name") || "").toString(),
          message_text: (fd.get("message_text") || "").toString(),
          message_variants: variants,
          variant_mode: (fd.get("variant_mode") || "random").toString(),
          use_typing: !!fd.get("use_typing"),
          smart_delay: !!fd.get("smart_delay"),
          delay_between_messages: Number(fd.get("delay_between_messages") || 0),
          delay_between_accounts: Number(fd.get("delay_between_accounts") || 0),
          daily_limit: Number(fd.get("daily_limit") || 0),
          messages_per_batch: Number(fd.get("messages_per_batch") || 0),
          batch_delay: Number(fd.get("batch_delay") || 0),
          max_recipients: cap > 0 ? cap : 0,
          mailing_cooldown_hours: Number(fd.get("mailing_cooldown_hours") || 0),
          auto_stop_hours: auto > 0 ? auto : 0,
          target_group_id: Number(fd.get("target_group_id") || 0),
          community_link: (fd.get("community_link") || "").toString(),
          audience_mode: (fd.get("audience_mode") || "classes").toString(),
          audience_client_status: (fd.get("audience_client_status") || "new").toString(),
          audience_include_classes: splitClasses(fd.get("audience_include_classes")),
          audience_exclude_classes: splitClasses(fd.get("audience_exclude_classes")),
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
    mailingPromptUi.mailingId = id;
    mailingPromptUi.versionId = null;
    mailingPromptUi.savedText = "";
    await fetchMailingPromptText(id);
    await loadMailingKnowledge(id);
    await loadMailingPromptVersions(id);
    bindMailingPromptHandlers(id);
  } catch (e) { root.innerHTML = `<div class="text-rose-400">${escapeHTML(e.message)}</div>`; }
}

async function fetchMailingPromptText(id) {
  const ta = $("#mailPromptText");
  const status = $("#mailPromptStatus");
  if (!ta) return;
  try {
    const r = await api(`/business/mailings/${id}/prompt`);
    if (mailingPromptUi.mailingId !== id) return;
    mailingPromptUi.versionId = r.version_id ?? null;
    ta.value = r.text || "";
    mailingPromptUi.savedText = ta.value;
    status.textContent = r.has_custom_file ? `версия #${r.version_id}` : "по умолчанию";
    status.className = `text-xs ${r.has_custom_file ? "text-emerald-300" : "text-slate-500"}`;
  } catch (e) {
    ta.value = "";
    status.textContent = `ошибка: ${e.message}`;
    status.className = "text-xs text-rose-400";
  }
}

let mailingKnowledgeEditId = null;

function renderMailingKnowledge(entries, readOnly = isReadOnlyRole()) {
  const root = $("#mailKnowledgeList");
  if (!root) return;
  const rows = Array.isArray(entries) ? entries : [];
  if (!rows.length) {
    root.innerHTML = '<div class="text-xs text-slate-500">Фрагментов пока нет.</div>';
    return;
  }
  root.innerHTML = rows.map((entry) => {
    const id = Number(entry.id);
    const keywords = Array.isArray(entry.keywords) ? entry.keywords : [];
    const content = String(entry.content || "");
    const summary = content.length > 260 ? `${content.slice(0, 260)}…` : content;
    return `<article class="border border-ink-700 rounded-lg p-3" data-knowledge-entry="${id}">
      <div class="flex items-start justify-between gap-3 flex-wrap"><div class="min-w-0 flex-1">
        <div class="font-medium text-slate-200 break-words">${escapeHTML(entry.title || "Без названия")}</div>
        <div class="text-xs text-slate-400 mt-1">Ключевые слова: ${keywords.map(escapeHTML).join(", ") || "—"} · <span class="${entry.enabled ? "text-emerald-300" : "text-slate-500"}">${entry.enabled ? "включён" : "выключен"}</span></div>
        <div class="text-xs text-slate-300 mt-2 whitespace-pre-wrap break-words">${escapeHTML(summary)}</div>
      </div>
      ${readOnly ? "" : `<div class="flex gap-1 shrink-0"><button type="button" data-knowledge-edit="${id}" class="px-2 py-1 rounded bg-ink-700 text-xs">Изменить</button><button type="button" data-knowledge-toggle="${id}" data-enabled="${entry.enabled ? "1" : "0"}" class="px-2 py-1 rounded bg-ink-700 text-xs">${entry.enabled ? "Выключить" : "Включить"}</button><button type="button" data-knowledge-delete="${id}" class="px-2 py-1 rounded bg-rose-900/60 text-xs">Удалить</button></div>`}
    </article>`;
  }).join("");
  if (readOnly) return;
  root.querySelectorAll("[data-knowledge-edit]").forEach(button => button.addEventListener("click", () => {
    const entry = rows.find(item => Number(item.id) === Number(button.dataset.knowledgeEdit));
    if (!entry) return;
    mailingKnowledgeEditId = Number(entry.id);
    const form = $("#mailKnowledgeForm");
    if (!form) return;
    form.elements.title.value = entry.title || "";
    form.elements.content.value = entry.content || "";
    form.elements.keywords.value = (Array.isArray(entry.keywords) ? entry.keywords : []).join(", ");
    form.elements.enabled.checked = !!entry.enabled;
    form.classList.remove("hidden");
    $("#mailKnowledgeMsg").textContent = "Редактирование фрагмента";
    form.scrollIntoView?.({ block: "nearest" });
  }));
  root.querySelectorAll("[data-knowledge-toggle]").forEach(button => button.addEventListener("click", async () => {
    const entryId = Number(button.dataset.knowledgeToggle);
    button.disabled = true;
    try {
      await api(`/business/mailings/${mailingPromptUi.mailingId}/knowledge/${entryId}`, { method: "PATCH", body: { enabled: button.dataset.enabled !== "1" } });
      await loadMailingKnowledge(mailingPromptUi.mailingId);
    } catch (error) { toast(error.message || "Не удалось изменить фрагмент", "error"); button.disabled = false; }
  }));
  root.querySelectorAll("[data-knowledge-delete]").forEach(button => button.addEventListener("click", async () => {
    const entryId = Number(button.dataset.knowledgeDelete);
    const title = rows.find(item => Number(item.id) === entryId)?.title || `#${entryId}`;
    if (!confirmDanger(`Удалить фрагмент базы знаний «${title}»?`)) return;
    button.disabled = true;
    try {
      await api(`/business/mailings/${mailingPromptUi.mailingId}/knowledge/${entryId}`, { method: "DELETE", raw: true });
      await loadMailingKnowledge(mailingPromptUi.mailingId);
      toast("Фрагмент удалён", "success");
    } catch (error) { toast(error.message || "Не удалось удалить фрагмент", "error"); button.disabled = false; }
  }));
}

function bindMailingKnowledgeHandlers(id) {
  const form = $("#mailKnowledgeForm");
  if (!form || form.dataset.bound === "1") return;
  form.dataset.bound = "1";
  const newButton = $("#mailKnowledgeNew");
  const resetForm = () => {
    mailingKnowledgeEditId = null;
    form?.reset();
    if (form) form.classList.add("hidden");
    const message = $("#mailKnowledgeMsg");
    if (message) message.textContent = "";
  };
  newButton?.addEventListener("click", () => {
    resetForm();
    form?.classList.remove("hidden");
    form?.scrollIntoView?.({ block: "nearest" });
    form?.elements.title.focus();
  });
  $("#mailKnowledgeCancel")?.addEventListener("click", resetForm);
  form?.addEventListener("submit", async event => {
    event.preventDefault();
    if (isReadOnlyRole()) return;
    const title = form.elements.title.value.trim();
    const content = form.elements.content.value.trim();
    const keywords = form.elements.keywords.value.split(/[\n,;]+/).map(value => value.trim()).filter(Boolean);
    const output = $("#mailKnowledgeMsg");
    const fail = message => { output.textContent = message; output.className = "text-xs text-rose-400"; };
    if (!title || title.length > 120) return fail("Название обязательно (до 120 символов).");
    if (!content || content.length > 3000) return fail("Текст обязателен (до 3000 символов).");
    if (keywords.length > 10 || keywords.some(word => word.length > 64)) return fail("Допускается до 10 ключевых слов длиной до 64 символов каждое.");
    const body = { title, content, keywords, enabled: !!form.elements.enabled.checked };
    output.textContent = "Сохраняем…";
    try {
      if (mailingKnowledgeEditId) {
        await api(`/business/mailings/${id}/knowledge/${mailingKnowledgeEditId}`, { method: "PATCH", body });
      } else {
        await api(`/business/mailings/${id}/knowledge`, { method: "POST", body });
      }
      resetForm();
      await loadMailingKnowledge(id);
      toast("Фрагмент базы знаний сохранён", "success");
    } catch (error) {
      output.textContent = error.message || "Не удалось сохранить фрагмент";
      output.className = "text-xs text-rose-400";
    }
  });
}

async function loadMailingKnowledge(id) {
  const root = $("#mailKnowledgeList");
  if (!root) return;
  try {
    const entries = await api(`/business/mailings/${id}/knowledge`);
    if (mailingPromptUi.mailingId !== id) return;
    renderMailingKnowledge(entries);
    bindMailingKnowledgeHandlers(id);
  } catch (error) {
    root.textContent = `База знаний недоступна: ${error.message || "ошибка"}`;
  }
}

async function loadMailingPromptVersions(id) {
  const root = $("#mailPromptVersions");
  const preview = $("#mailPromptVersionPreview");
  const useButton = $("#mailPromptUseVersion");
  if (!root) return;
  if (preview) {
    preview.textContent = "";
    preview.classList.add("hidden");
  }
  if (useButton) useButton.classList.add("hidden");
  try {
    const response = await api(`/business/mailings/${id}/prompt/versions`);
    if (mailingPromptUi.mailingId !== id) return;
    const versions = response.versions || [];
    root.innerHTML = versions.length ? versions.map((v) => `
      <div class="border border-ink-700 rounded p-2 flex flex-wrap items-center gap-2">
        <span class="text-slate-200">#${Number(v.id)} · ${escapeHTML(v.action)}</span>
        <span>${escapeHTML(fmtDate(v.created_at))} · ${escapeHTML(v.actor || "—")}</span>
        ${v.is_active ? '<span class="text-emerald-300">активна</span>' : ''}
        <button type="button" data-prompt-preview="${Number(v.id)}" class="px-2 py-1 rounded bg-ink-700 text-slate-200">Просмотр</button>
        ${v.is_active || isReadOnlyRole() ? '' : `<button type="button" data-prompt-restore="${Number(v.id)}" class="px-2 py-1 rounded bg-ink-700 text-slate-200">Восстановить</button>`}
      </div>`).join("") : '<div class="text-slate-500">Версий пока нет.</div>';
    root.querySelectorAll("[data-prompt-preview]").forEach((button) => button.addEventListener("click", async () => {
      setBusy(button, true, "Загрузка…");
      try {
        const version = await api(`/business/mailings/${id}/prompt/versions/${button.dataset.promptPreview}`);
        if (mailingPromptUi.mailingId !== id || !preview) return;
        preview.textContent = version.is_default ? "Промпт по умолчанию" : version.text;
        preview.classList.remove("hidden");
        if (useButton) {
          useButton.classList.remove("hidden");
          useButton.onclick = () => {
            const textarea = $("#mailPromptText");
            if (!textarea) return;
            if (textarea.value !== mailingPromptUi.savedText &&
                !confirm("Заменить несохранённый текст в поле выбранной версией?")) return;
            textarea.value = version.text;
            toast("Версия подставлена в поле. Можно сравнить её ответ до сохранения.", "info");
          };
        }
      } catch (error) { toast(error.message, "error"); }
      finally { setBusy(button, false); }
    }));
    root.querySelectorAll("[data-prompt-restore]").forEach((button) => button.addEventListener("click", async () => {
      const versionId = Number(button.dataset.promptRestore);
      const hasDraft = $("#mailPromptText")?.value !== mailingPromptUi.savedText;
      if (!confirm(`Восстановить промпт из версии #${versionId}? Сохранённый текст останется в истории.${hasDraft ? " Несохранённый текст в поле будет потерян." : ""}`)) return;
      setBusy(button, true, "Восстанавливаем…");
      try {
        await api(`/business/mailings/${id}/prompt/versions/${versionId}/restore`, {
          method: "POST", body: { expected_version_id: mailingPromptUi.versionId },
        });
        await fetchMailingPromptText(id);
        await loadMailingPromptVersions(id);
        toast("Версия восстановлена", "success");
      } catch (error) {
        toast(error.status === 409 ? "Промпт изменился в другом окне. Сохраните свой черновик перед обновлением страницы." : error.message, "error");
        if (error.status === 409) await loadMailingPromptVersions(id);
      } finally { setBusy(button, false); }
    }));
  } catch (error) {
    root.textContent = `История недоступна: ${error.message}`;
  }
}

function bindMailingPromptHandlers(id) {
  $("#mailPromptCompare")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const samples = ($("#mailPromptSample")?.value || "").split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    const candidate = ($("#mailPromptText")?.value || "").trim();
    const rawAccountId = ($("#mailPromptAccountId")?.value || "").trim();
    const status = $("#mailPromptCompareStatus");
    if (!samples.length || samples.length > 3 || samples.some((sample) => sample.length < 3 || sample.length > 1000) || !candidate) {
      status.textContent = "Введите 1–3 сообщения по 3–1000 символов и текст промпта";
      return;
    }
    setBusy(button, true, "Сравниваем…");
    status.textContent = "Запрашиваем ответы модели…";
    try {
      const result = await api(`/business/mailings/${id}/prompt/compare`, {
        method: "POST", body: { sample_message: samples[0], sample_messages: samples, candidate_text: candidate,
          account_id: rawAccountId ? Number(rawAccountId) : null },
      });
      const turns = Array.isArray(result.turns) ? result.turns : [];
      const showTurns = (key, fallback) => turns.length
        ? turns.map((turn, index) => `Ход ${index + 1} · ${turn.user_message}\n${turn[key] || "—"}`).join("\n\n")
        : (fallback || "—");
      $("#mailPromptSavedReply").textContent = showTurns("saved_reply", result.saved_reply);
      $("#mailPromptCandidateReply").textContent = showTurns("candidate_reply", result.candidate_reply);
      const requests = samples.length * (result.same_prompt ? 1 : 2);
      status.textContent = `Модель: ${result.model}. Ходов: ${samples.length}; запросов: ${requests}.`;
    } catch (error) {
      status.textContent = `Сравнение не выполнено: ${error.message || "ошибка"}`;
    } finally {
      setBusy(button, false);
    }
  });
  $("#mailPromptSave").addEventListener("click", async () => {
    const ta = $("#mailPromptText");
    const out = $("#mailPromptMsg");
    if (!ta) return;
    out.textContent = "…";
    try {
      await api(`/business/mailings/${id}/prompt`, {
        method: "PUT",
        body: { text: ta.value, expected_version_id: mailingPromptUi.versionId },
      });
      toast("Промпт сохранён", "success");
      out.textContent = "ok";
      out.className = "text-xs text-emerald-300";
      await fetchMailingPromptText(id);
      await loadMailingPromptVersions(id);
    } catch (e) {
      out.textContent = e.status === 409 ? "Промпт изменился в другом окне. Скопируйте свой текст и обновите страницу." : e.message;
      out.className = "text-xs text-rose-400";
      if (e.status === 409) await loadMailingPromptVersions(id);
    }
  });
  $("#mailPromptReset").addEventListener("click", async () => {
    const hasDraft = $("#mailPromptText")?.value !== mailingPromptUi.savedText;
    if (!confirm(`Сбросить активный промпт к значению по умолчанию? Сохранённый текст останется в истории.${hasDraft ? " Несохранённый текст в поле будет потерян." : ""}`)) return;
    try {
      await api(`/business/mailings/${id}/prompt`, {
        method: "DELETE", body: { expected_version_id: mailingPromptUi.versionId },
      });
      toast("Сброшено", "success");
      await fetchMailingPromptText(id);
      await loadMailingPromptVersions(id);
    } catch (e) {
      toast(e.status === 409 ? "Промпт изменился в другом окне. Обновите страницу перед сбросом." : e.message, "error");
      if (e.status === 409) await loadMailingPromptVersions(id);
    }
  });
}

/* ----------------------------- Clients view ---------------------------- */

async function renderClients(idStr) {
  const id = idStr ? Number(idStr) : null;
  setHeader("Клиенты", id ? `Клиент #${id}` : "База контактов и фильтр по классам");
  const root = $("#pageRoot");
  if (!id) {
    const cliReadOnly = isReadOnlyRole();
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
        ${cliReadOnly ? "" : `<div id="clBulkPermission" class="card flex flex-wrap gap-2 items-center text-sm">
          <span class="text-slate-300">Отмеченные контакты:</span>
          <input id="clBulkSource" maxlength="255" placeholder="Источник права или отказа" class="flex-1 min-w-64 px-3 py-1.5 rounded bg-ink-800 border border-ink-600 text-slate-100" />
          <button data-cl-bulk-permission="opt_in" class="px-3 py-1.5 rounded bg-emerald-800">Подтвердить право</button>
          <button data-cl-bulk-permission="opt_out" class="px-3 py-1.5 rounded bg-rose-900">Зафиксировать отказ</button>
        </div>`}
        <div class="card">
          <div class="flex items-center justify-between mb-1 flex-wrap gap-2">
            <h3 class="font-semibold">Загрузка клиентов из файла</h3>
            <span class="text-xs text-slate-500">один запрос на весь список</span>
          </div>
          ${cliReadOnly
            ? `<p class="text-xs text-amber-300">ⓘ Импорт недоступен для роли read-only.</p>`
            : `<p class="text-xs text-slate-500 mb-2">По одному @username в строке, с @ или без (как txt в боте). Дубликаты пропускаются, мусорные строки — в отчёте.</p>
          <div class="flex items-end gap-2 flex-wrap text-sm">
            <label class="block">
              <span class="text-slate-400 text-xs">Файл .txt</span>
              <input id="clImportFile" type="file" accept=".txt,text/plain"
                     class="mt-1 block bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 text-xs" />
            </label>
            <button id="clImportBtn" class="px-3 py-2 rounded-lg bg-ink-700 hover:bg-ink-600 text-slate-200 text-sm">Импортировать</button>
            <span id="clImportMsg" class="text-xs text-slate-400"></span>
          </div>
          <label class="block text-sm mt-2">
            <span class="text-slate-400 text-xs">Или вставьте строки вручную</span>
            <textarea id="clImportText" rows="3" placeholder="@username1&#10;username2"
                      class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 font-mono text-xs"></textarea>
          </label>`}
        </div>
        <div class="card p-0 overflow-hidden">
          <table class="cb-table">
            <thead><tr>
              <th><input id="clSelectAll" type="checkbox" aria-label="Выбрать видимые контакты" ${cliReadOnly ? "disabled" : ""} /></th><th>ID</th><th>Username</th><th>TG ID</th><th>Статус</th><th>Право на контакт</th>
              <th>Классы</th><th>Добавлен</th><th>Контакт</th><th></th>
            </tr></thead>
            <tbody id="clBody"><tr><td colspan="10" class="text-center text-slate-500 py-8">Загрузка…</td></tr></tbody>
          </table>
        </div>
      </div>
    `;
    $("#clApply").addEventListener("click", loadClientsList);
    $("#clSelectAll").addEventListener("change", event => {
      $$("#clBody input[data-cl-select]").forEach(box => { box.checked = event.target.checked; });
    });
    $("#clBulkPermission")?.addEventListener("click", async event => {
      const button = event.target.closest("button[data-cl-bulk-permission]");
      if (!button) return;
      const ids = $$("#clBody input[data-cl-select]:checked").map(box => Number(box.value));
      const source = $("#clBulkSource").value.trim();
      if (!ids.length) { toast("Отметьте контакты в таблице", "error"); return; }
      if (source.length < 5) { toast("Укажите проверяемый источник права или отказа", "error"); return; }
      const permission = button.dataset.clBulkPermission;
      if (permission === "opt_in" && !confirm(`Подтвердить право на контакт для ${ids.length} выбранных клиентов?`)) return;
      try {
        await api("/business/clients/contact-permissions/bulk", { method: "POST", body: {
          client_ids: ids, state: permission, source,
        } });
        toast(`Обновлено контактов: ${ids.length}`, "success");
        await loadClientsList();
      } catch (error) { toast(error.message || "Не удалось обновить", "error"); }
    });
    $("#clQ").addEventListener("keydown", (e) => { if (e.key === "Enter") loadClientsList(); });
    $("#clImportFile")?.addEventListener("change", (ev) => {
      const f = ev.currentTarget.files?.[0];
      if (!f) return;
      if (f.size > 15 * 1024 * 1024) {
        toast("Файл слишком большой (макс. 15 МБ)", "error");
        ev.currentTarget.value = "";
        return;
      }
      const rd = new FileReader();
      rd.onload = () => { $("#clImportText").value = String(rd.result || ""); };
      rd.onerror = () => toast("Не удалось прочитать файл", "error");
      rd.readAsText(f);
    });
    $("#clImportBtn")?.addEventListener("click", async () => {
      const out = $("#clImportMsg");
      const btn = $("#clImportBtn");
      const usernames = ($("#clImportText")?.value || "").split(/\r?\n/).map(s => s.trim()).filter(Boolean);
      if (!usernames.length) {
        out.textContent = "Выберите .txt файл или вставьте хотя бы одну строку.";
        out.className = "text-xs text-rose-400";
        return;
      }
      setBusy(btn, true, "Импорт…");
      try {
        const r = await api("/business/clients/import", { method: "POST", body: { usernames } });
        const summary = `Добавлено ${r.added}, дубликатов ${r.skipped_duplicates}, мусорных ${r.invalid}`;
        out.textContent = summary;
        out.className = "text-xs " + (r.added ? "text-emerald-300" : "text-amber-300");
        toast(summary, r.added ? "success" : "info");
        $("#clImportText").value = "";
        const fi = $("#clImportFile");
        if (fi) fi.value = "";
        await loadClientsList();
      } catch (e) {
        out.textContent = e.message;
        out.className = "text-xs text-rose-400";
      } finally {
        setBusy(btn, false);
      }
    });
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
      tbody.innerHTML = `<tr><td colspan="10" class="text-center text-slate-500 py-8">Нет клиентов под фильтр.</td></tr>`;
      return;
    }
    tbody.innerHTML = list.map(c => {
      const classes = c.classes.map(x => `<span class="pill cls-${escapeHTML(x.class_key)} pill-gray" title="${x.count}">${escapeHTML(x.class_key)}:${x.count}</span>`).join(" ") || `<span class="text-slate-500">—</span>`;
      return `
        <tr>
          <td><input type="checkbox" data-cl-select value="${c.id}" aria-label="Выбрать клиента #${c.id}" ${isReadOnlyRole() ? "disabled" : ""} /></td>
          <td class="text-slate-500">#${c.id}</td>
          <td><a href="#/clients/${c.id}" class="text-slate-100 hover:text-accent-500">@${escapeHTML(c.username)}</a></td>
          <td class="text-xs text-slate-400">${c.telegram_user_id ?? "—"}</td>
          <td>${escapeHTML(c.status)}</td>
          <td class="text-xs">${escapeHTML(c.contact_permission || "unverified")}</td>
          <td>${classes}</td>
          <td class="text-xs text-slate-400">${fmtDate(c.added_at)}</td>
          <td class="text-xs text-slate-400">${fmtRelative(c.last_contacted_at)}</td>
          <td class="text-right">
            <button data-cl-del="${c.id}" aria-label="Удалить клиента #${c.id}" class="px-2 py-1 rounded bg-rose-900/40 hover:bg-rose-800 text-xs text-rose-200 border border-rose-700/50">🗑</button>
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
          <div class="text-xs text-slate-500">tg_id ${c.telegram_user_id ?? "—"} В· ${escapeHTML(c.status)}</div>
        </div>

        <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
          ${kpi("Добавлен", "cdAdd", fmtDate(c.added_at), "")}
          ${kpi("Послед. контакт", "cdLast", fmtDate(c.last_contacted_at) || "—", "")}
          ${kpi("Событий", "cdInter", String(c.interactions_count), "client_interactions")}
          ${kpi("Классов", "cdCls", String(c.classes.length), "счётчики")}
        </div>

        <div class="card">
          <h3 class="font-semibold mb-2">Право на контакт</h3>
          <p class="text-sm text-slate-300 mb-2">${escapeHTML(c.contact_permission || "unverified")}${c.contact_permission_source ? ` · ${escapeHTML(c.contact_permission_source)}` : ""}</p>
          <p class="text-xs text-slate-500 mb-2">Публичный профиль или импорт в CRM не дают согласия на рассылку. Укажите основание из собственного источника или зафиксируйте отказ получателя.</p>
          ${isReadOnlyRole() ? "" : `<div class="flex flex-wrap gap-2 items-center">
            <input id="contactPermissionSource" maxlength="255" placeholder="Источник: форма регистрации, запрос клиента…" class="flex-1 min-w-64 px-3 py-1.5 rounded bg-ink-800 border border-ink-600 text-slate-100 text-sm" />
            <button data-contact-permission="opt_in" class="px-3 py-1.5 rounded bg-emerald-800 text-sm">Подтвердить</button>
            <button data-contact-permission="opt_out" class="px-3 py-1.5 rounded bg-rose-900 text-sm">Отказ</button>
          </div>`}
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
                    <div class="text-[11px] text-slate-500">acc#${it.account_id ?? "—"} В· mailing#${it.mailing_id ?? "—"} В· ${fmtDate(it.created_at)}</div>
                  </div>
                </div>`).join("")}
            </div>` : `<div class="text-slate-500">Событий нет.</div>`}
        </div>
      </div>
    `;

    root.querySelectorAll("button[data-contact-permission]").forEach(button => {
      button.addEventListener("click", async () => {
        const source = $("#contactPermissionSource")?.value.trim() || "";
        if (source.length < 5) { toast("Укажите проверяемый источник права или отказа", "error"); return; }
        const permission = button.dataset.contactPermission;
        if (permission === "opt_in" && !confirm(`Подтвердить право написать клиенту #${id} по указанному источнику?`)) return;
        try {
          await api("/business/clients/contact-permissions/bulk", { method: "POST", body: {
            client_ids: [id], state: permission, source,
          } });
          toast("Право на контакт обновлено", "success");
          await loadClientDetail(id);
        } catch (error) { toast(error.message || "Не удалось сохранить", "error"); }
      });
    });

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
        <label class="inline-flex items-center gap-2 text-slate-400">
          <input id="logsSearch" placeholder="фильтр по строкам" class="w-48 bg-ink-800 border border-ink-600 rounded-md px-2 py-1 text-slate-100" />
        </label>
        <label class="inline-flex items-center gap-2 text-slate-400">
          <input id="logsWrap" type="checkbox" checked class="rounded border-ink-600 bg-ink-800" /> перенос строк
        </label>
      </div>
      <div class="card p-0 overflow-hidden">
        <div class="table-scroll">
        <table class="cb-table logs-mono" id="logsTable">
          <thead><tr id="logsHeadRow"><th>Время</th><th>Уровень</th><th>Категория</th><th>Сообщение</th></tr></thead>
          <tbody id="logsBody"><tr><td colspan="4" class="text-center text-slate-500 py-8">Загрузка…</td></tr></tbody>
        </table>
        </div>
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
  $("#logsLevel").addEventListener("change", () => loadLogs());
  $("#logsSearch")?.addEventListener("input", () => paintLogsRows());
  $("#logsWrap")?.addEventListener("change", (ev) => {
    $("#logsTable")?.classList.toggle("logs-nowrap", !ev.currentTarget.checked);
  });
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
    state.logsCache = { channel, list };
    paintLogsRows();
  } catch (e) {
    toast(`Ошибка логов: ${e.message}`, "error");
  }
}

/* Клиентский фильтр по строкам (для ingest-канала серверного q нет) +
   динамический colspan пустого состояния под число колонок. */
function paintLogsRows() {
  const cache = state.logsCache || {};
  if (!cache.list) return; // loadLogs ещё не положил данные — не затираем «Загрузка…»
  const channel = cache.channel || "ingest";
  const list = cache.list || [];
  const cols = channel === "openrouter" ? 5 : 4;  const tbody = $("#logsBody");
  if (!tbody) return;
  const q = (($("#logsSearch")?.value || "").trim().toLowerCase());
  const rows = q
    ? list.filter(l => [l.message, l.category, l.code, l.level, l.provider, l.model, l.prompt_id]
        .filter(Boolean).join(" ").toLowerCase().includes(q))
    : list;
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="${cols}" class="text-center text-slate-500 py-8">${list.length ? "Нет записей под фильтр." : "Нет записей."}</td></tr>`;
    return;
  }
  if (channel === "openrouter") {
    tbody.innerHTML = rows.map(l => `
      <tr>
        <td class="text-xs text-slate-400 whitespace-nowrap">${fmtDate(l.created_at)}</td>
        <td>${logLevelPill(l.level)}</td>
        <td class="text-slate-300">
          <span class="text-slate-400">${escapeHTML(l.provider || "—")}</span>
          <span class="text-slate-500">/</span>
          <span class="text-slate-200">${escapeHTML(l.model || "—")}</span>
        </td>
        <td class="text-xs text-slate-400">${escapeHTML(l.prompt_id || "—")}</td>
        <td class="text-slate-200 msg-cell">${escapeHTML(l.message)}</td>
      </tr>
    `).join("");
  } else {
    tbody.innerHTML = rows.map(l => `
      <tr>
        <td class="text-xs text-slate-400 whitespace-nowrap">${fmtDate(l.created_at)}</td>
        <td>${logLevelPill(l.level)}</td>
        <td class="text-slate-300">${escapeHTML(l.category)}${l.code ? ` <span class="text-slate-500">(${escapeHTML(l.code)})</span>` : ''}</td>
        <td class="text-slate-200 msg-cell">${escapeHTML(l.message)}</td>
      </tr>
    `).join("");
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
    if (body.mode === "hard" && !body.dry_run) {
      const scope = [
        acc ? `аккаунт #${acc}` : null,
        peer ? `peer ${peer}` : null,
        days ? `старше ${days} дн.` : null,
        cls ? `классы: ${cls}` : null,
      ].filter(Boolean).join(", ") || "вся БД переписок";
      if (!confirmDanger(`ФИЗИЧЕСКИ удалить переписки (${scope})? Восстановить будет нельзя (не archive).`)) return;
    }

    const out = $("#cleanupResult");
    out.textContent = "…";
    try {
      const r = await api("/business/cleanup/v2", { method: "POST", body });
      const arch = (r.messages_archived || r.interactions_archived)
        ? ` В· в архив: ${r.messages_archived}/${r.interactions_archived}`
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
          <button id="grpRefresh" aria-label="Обновить список групп" class="text-xs text-slate-400 hover:text-slate-200">⟳</button>
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
          <p class="text-xs text-slate-500">id=${g.id} В· аккаунтов: ${g.accounts_count}</p>
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
  const readOnly = isReadOnlyRole();
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full space-y-4">
      <div class="card">
        <div class="flex items-center justify-between mb-1 flex-wrap gap-2">
          <h3 class="font-semibold">Пулы (по регионам)</h3>
          <span class="text-xs text-slate-500">клик по пулу — фильтр таблицы</span>
        </div>
        <p class="text-xs text-slate-500 mb-3">Пулы <b>TDATA_CHECK</b> используются только проверкой TData и никогда — runtime-аккаунтами. Свободно — прокси без привязанных аккаунтов, занято — с аккаунтами.</p>
        <div id="prxGroups" class="grid gap-2 text-sm mb-3" style="grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));">Загрузка…</div>
        ${readOnly
          ? `<p class="text-xs text-amber-300">ⓘ Создание групп недоступно для роли read-only.</p>`
          : `<form id="prxGroupForm" class="flex items-end gap-2 text-sm flex-wrap">
              <label class="block">
                <span class="text-slate-400 text-xs">Новая группа</span>
                <input name="name" placeholder="например, EU-CHECK" class="mt-1 bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
              </label>
              <label class="block">
                <span class="text-slate-400 text-xs">Назначение</span>
                <select name="purpose" class="mt-1 bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
                  <option value="ACCOUNT_RUNTIME">ACCOUNT_RUNTIME</option>
                  <option value="TDATA_CHECK">TDATA_CHECK</option>
                </select>
              </label>
              <button type="submit" class="btn btn-secondary">+ Создать пул</button>
              <span id="prxGroupMsg" class="text-xs text-slate-400"></span>
            </form>`}
      </div>
      <div class="card">
        <div class="flex items-center justify-between mb-3 flex-wrap gap-2">
          <h3 class="font-semibold">Список прокси</h3>
          <div class="flex gap-2 items-center flex-wrap">
            <select id="prxGroupFilter" aria-label="Фильтр по группе" class="bg-ink-800 border border-ink-600 rounded-md px-2 py-1.5 text-slate-100 text-sm">
              <option value="">все группы</option>
            </select>
            <button id="prxRefresh" aria-label="Обновить список прокси" class="px-3 py-1.5 rounded-md bg-ink-700 hover:bg-ink-600 text-sm">⟳</button>
            <button id="prxCheckAll" class="px-3 py-1.5 rounded-md bg-ink-700 hover:bg-ink-600 text-sm" ${readOnly ? "disabled title='Недоступно для роли read-only'" : ""}>Проверить все</button>
            <button id="prxNewBtn" class="px-3 py-1.5 rounded-md bg-accent-600 hover:bg-accent-500 text-sm text-white">+ Добавить</button>
          </div>
        </div>
        <div id="prxTable" class="text-sm text-slate-400">Загрузка…</div>
      </div>
      <div id="prxFormCard" class="card hidden">
        <h3 class="font-semibold mb-3" id="prxFormTitle">Новый прокси</h3>
        <div id="prxForm"></div>
      </div>
      <div class="card">
        <div class="flex items-center justify-between mb-1 flex-wrap gap-2">
          <h3 class="font-semibold">Загрузка прокси из файла</h3>
          <span class="text-xs text-slate-500">один запрос на весь список — быстро даже для 1000+ строк</span>
        </div>
        <p class="text-xs text-slate-500 mb-3">Форматы строк: <code>host:port@user:pass</code>, <code>user:pass@host:port</code>, <code>host:port:user:pass</code>, <code>host:port</code>; префикс <code>http://</code> — для HTTP. Дубликаты (host/port/логин/пароль) пропускаются, ошибочные строки — в отчёте.</p>
        ${readOnly
          ? `<p class="text-xs text-amber-300">ⓘ Импорт недоступен для роли read-only.</p>`
          : `<div class="flex items-end gap-2 flex-wrap text-sm mb-2">
              <label class="block">
                <span class="text-slate-400 text-xs">Файл .txt (лист на ~1000 прокси)</span>
                <input id="prxImportFile" type="file" accept=".txt,text/plain"
                       class="mt-1 block bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 text-xs" />
              </label>
              <label class="block">
                <span class="text-slate-400 text-xs">Пул (новый или существующий)</span>
                <input id="prxImportGroup" placeholder="например, КЕНИЯ-989"
                       class="mt-1 bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
              </label>
              <label class="block">
                <span class="text-slate-400 text-xs">Назначение (для нового пула)</span>
                <select id="prxImportPurpose" class="mt-1 bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
                  <option value="ACCOUNT_RUNTIME">ACCOUNT_RUNTIME</option>
                  <option value="TDATA_CHECK">TDATA_CHECK</option>
                </select>
              </label>
            </div>
            <label class="block text-sm mb-2">
              <span class="text-slate-400 text-xs">Или вставьте строки вручную (по одной в строке)</span>
              <textarea id="prxImportText" rows="4" placeholder="10.0.0.1:1080@user:pw&#10;http://10.0.0.2:8080:user:pw"
                        class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 font-mono text-xs"></textarea>
            </label>
            <div class="flex items-center gap-3 flex-wrap">
              <button id="prxImportBtn" class="btn btn-secondary">Импортировать</button>
              <span id="prxImportMsg" class="text-xs text-slate-400"></span>
            </div>`}
      </div>
    </div>
  `;
  $("#prxRefresh").addEventListener("click", () => loadProxiesTable());
  $("#prxNewBtn").addEventListener("click", () => openProxyForm(null));
  $("#prxGroupFilter")?.addEventListener("change", (ev) => {
    state.proxies = state.proxies || {};
    state.proxies.group = ev.currentTarget.value || "";
    paintProxiesTable();
    paintPoolCards();
  });
  $("#prxGroupForm")?.addEventListener("submit", onCreateProxyGroup);
  $("#prxCheckAll")?.addEventListener("click", checkAllProxies);
  $("#prxImportBtn")?.addEventListener("click", importProxiesBulk);
  $("#prxImportFile")?.addEventListener("change", (ev) => {
    // Файл подставляем в textarea — импорт один (bulk-эндпоинт), лимит 15 МБ как в боте.
    const f = ev.currentTarget.files?.[0];
    if (!f) return;
    if (f.size > 15 * 1024 * 1024) {
      toast("Файл слишком большой (макс. 15 МБ)", "error");
      ev.currentTarget.value = "";
      return;
    }
    const rd = new FileReader();
    rd.onload = () => { $("#prxImportText").value = String(rd.result || ""); };
    rd.onerror = () => toast("Не удалось прочитать файл", "error");
    rd.readAsText(f);
  });
  await loadProxyGroups();
  await loadProxiesTable();
}

async function loadProxyGroups() {
  const box = $("#prxGroups");
  if (!box) return;
  try {
    const groups = await api("/business/proxy-groups");
    state.proxies = state.proxies || {};
    state.proxies.groups = groups || [];
    if (!groups?.length) {
      box.innerHTML = `<span class="text-slate-500 text-xs">Групп пока нет.</span>`;
      return;
    }
    const sel = $("#prxGroupFilter");
    if (sel) {
      const cur = state.proxies.group || "";
      sel.innerHTML = `<option value="">все группы</option><option value="none">без пула</option>` + groups.map(g =>
        `<option value="${g.id}" ${String(g.id) === String(cur) ? "selected" : ""}>${escapeHTML(g.name)}</option>`
      ).join("");
      if (cur === "none") sel.value = "none";
    }
    paintPoolCards();
  } catch (e) {
    box.innerHTML = `<span class="text-rose-400 text-xs">${escapeHTML(e.message)}</span>`;
  }
}

/* Карточки пулов: всего / свободно / занято / ok / fail. Клик — фильтр таблицы. */
function paintPoolCards() {
  const box = $("#prxGroups");
  if (!box) return;
  const groups = (state.proxies && state.proxies.groups) || [];
  const all = (state.proxies && state.proxies.list) || [];
  const cur = (state.proxies && state.proxies.group) || "";
  const card = (gid, name, poolPurpose, members) => {
    const total = members.length;
    const busy = members.filter(p => (p.accounts_count || 0) > 0).length;
    const free = total - busy;
    const ok = members.filter(p => p.is_active && p.is_working).length;
    const fail = members.filter(p => p.is_active && !p.is_working).length;
    const purpose = (poolPurpose || "").toUpperCase();
    const active = String(gid ?? "") === String(cur);
    return `<button data-gid="${gid ?? ""}" title="Показать только этот пул"
              class="text-left rounded-lg border px-3 py-2 ${active ? "border-accent-500 bg-ink-700" : "border-ink-600 bg-ink-800 hover:border-slate-500"}">
      <div class="flex items-center justify-between gap-2">
        <span class="font-semibold text-slate-100 truncate">${escapeHTML(name)}</span>
        <span class="pill ${purpose === "TDATA_CHECK" ? "pill-blue" : "pill-gray"}">${escapeHTML(purpose || "—")}</span>
      </div>
      <div class="mt-1 text-xs text-slate-400">всего <b class="text-slate-200">${total}</b> · свободно <b class="text-emerald-300">${free}</b> · занято <b class="text-amber-300">${busy}</b></div>
      <div class="mt-1 flex gap-1">
        <span class="pill pill-green">ok ${ok}</span>
        ${fail ? `<span class="pill pill-red">fail ${fail}</span>` : ""}
      </div>
    </button>`;
  };
  const ungrouped = all.filter(p => !p.group_id);
  box.innerHTML =
    groups.map(g => card(g.id, g.name, g.purpose,
      all.filter(p => String(p.group_id) === String(g.id)))).join("") +
    (ungrouped.length ? card("none", "Без пула", "", ungrouped) : "");
  box.querySelectorAll("[data-gid]").forEach(btn => {
    btn.addEventListener("click", () => {
      state.proxies.group = btn.dataset.gid || "";
      const sel = $("#prxGroupFilter");
      if (sel) sel.value = state.proxies.group;
      paintProxiesTable();
      paintPoolCards();
    });
  });
}

async function onCreateProxyGroup(ev) {
  ev.preventDefault();
  const form = ev.currentTarget;
  const nameInput = form.querySelector('input[name="name"]');
  const fd = new FormData(form);
  const out = $("#prxGroupMsg");
  setFieldError(nameInput, "");
  const name = (fd.get("name") || "").toString().trim();
  if (!name) {
    setFieldError(nameInput, "Укажите название пула.");
    return;
  }
  out.textContent = "…";
  try {
    const g = await api("/business/proxy-groups", {
      method: "POST",
      body: { name, purpose: (fd.get("purpose") || "ACCOUNT_RUNTIME").toString() },
    });
    toast(`Пул «${g.name}» создан`, "success");
    out.textContent = `ok (#${g.id})`;
    out.className = "text-xs text-emerald-300";
    form.reset();
    await loadProxyGroups();
    await loadProxiesTable();
  } catch (e) {
    out.textContent = e.message;
    out.className = "text-xs text-rose-400";
  }
}

/* Bulk-импорт одним запросом: сервер парсит, дедупит и раскладывает по пулу. */
async function importProxiesBulk() {
  const ta = $("#prxImportText");
  const out = $("#prxImportMsg");
  const btn = $("#prxImportBtn");
  const groupInput = $("#prxImportGroup");
  const lines = (ta?.value || "").split(/\r?\n/).map(s => s.trim()).filter(Boolean);
  const group_name = (groupInput?.value || "").trim();
  if (!lines.length) {
    out.textContent = "Выберите .txt файл или вставьте хотя бы одну строку.";
    out.className = "text-xs text-rose-400";
    return;
  }
  if (!group_name) {
    out.textContent = "Укажите пул (новый создастся, в существующий добавится).";
    out.className = "text-xs text-rose-400";
    groupInput?.focus();
    return;
  }
  setBusy(btn, true, "Импорт…");
  out.textContent = `Отправка ${lines.length} строк…`;
  out.className = "text-xs text-slate-400";
  try {
    const r = await api("/business/proxies/import", {
      method: "POST",
      body: {
        group_name,
        purpose: ($("#prxImportPurpose")?.value || "ACCOUNT_RUNTIME"),
        lines,
      },
    });
    const summary = `Пул «${r.group_name}»: добавлено ${r.added}, дубликатов ${r.skipped_duplicates}, ошибочных ${r.bad}`;
    out.textContent = r.bad_samples?.length ? `${summary}. Примеры: ${r.bad_samples.join(" · ")}` : summary;
    out.className = "text-xs " + (r.added ? "text-emerald-300" : "text-amber-300");
    toast(summary, r.added ? "success" : "info");
    if (ta) ta.value = "";
    const fi = $("#prxImportFile");
    if (fi) fi.value = "";
    await loadProxyGroups();
    await loadProxiesTable();
  } catch (e) {
    out.textContent = e.message;
    out.className = "text-xs text-rose-400";
  } finally {
    setBusy(btn, false);
  }
}

async function checkAllProxies() {
  const list = (state.proxies && state.proxies.list) || [];
  if (!list.length) {
    toast("Проверять нечего: список пуст", "info");
    return;
  }
  const btn = $("#prxCheckAll");
  setBusy(btn, true, "…");
  let ok = 0, fail = 0;
  for (let i = 0; i < list.length; i++) {
    btn.textContent = `${i + 1}/${list.length}…`;
    try {
      const r = await api(`/business/proxies/${list[i].id}/test`, { method: "POST" });
      if (r.ok) ok++; else fail++;
    } catch {
      fail++;
    }
  }
  setBusy(btn, false);
  btn.textContent = "Проверить все";
  toast(`Проверка всех: ok=${ok} fail=${fail}`, fail ? "info" : "success");
  await loadProxiesTable();
}

async function loadProxiesTable() {
  const tbl = $("#prxTable");
  if (!tbl) return;
  try {
    const list = await api("/business/proxies");
    state.proxies = state.proxies || {};
    state.proxies.list = list || [];
    paintProxiesTable();
    paintPoolCards();
  } catch (e) {
    tbl.innerHTML = `<div class="text-rose-400 text-sm">${escapeHTML(e.message)}</div>`;
  }
}

/* Клиентский фильтр по группе (серверного нет — честно фильтруем локально). */
function paintProxiesTable() {
  const tbl = $("#prxTable");
  if (!tbl) return;
  const all = (state.proxies && state.proxies.list) || [];
  const gf = (state.proxies && state.proxies.group) || "";
  const list = !gf ? all
    : gf === "none" ? all.filter(p => !p.group_id)
    : all.filter(p => String(p.group_id) === String(gf));
  if (!all.length) {
    tbl.innerHTML = `<div class="text-slate-500 text-xs">Прокси пока нет. Добавьте через «+ Добавить» или импортом ниже.</div>`;
    return;
  }
  if (!list.length) {
    tbl.innerHTML = `<div class="text-slate-500 text-xs">В этой группе прокси нет.</div>`;
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
                <button data-act="test" aria-label="Проверить прокси #${p.id}" class="px-2 py-1 rounded bg-ink-700 hover:bg-ink-600 text-xs mr-1">Тест</button>
                <button data-act="edit" aria-label="Изменить прокси #${p.id}" class="px-2 py-1 rounded bg-ink-700 hover:bg-ink-600 text-xs mr-1">✎</button>
                <button data-act="delete" aria-label="Удалить прокси #${p.id}" class="px-2 py-1 rounded bg-rose-700 hover:bg-rose-600 text-xs text-white">🗑</button>
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
          // Мгновенно отражаем результат в строке, не дожидаясь refetch:
          // сервер и так сохраняет last_checked/is_working, а так колонка
          // ПРОВЕРКА обновится даже если GET где-то закэширован.
          const item = ((state.proxies && state.proxies.list) || []).find(x => x.id === pid);
          if (item) {
            item.is_working = !!r.ok;
            item.last_checked = new Date().toISOString();
            paintProxiesTable();
          }
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
        if (!confirmDanger(`Удалить прокси #${pid}? У аккаунтов, использовавших его, prox_id обнулится.`)) return;
        try {
          await api(`/business/proxies/${pid}`, { method: "DELETE" });
          toast("Удалено", "success");
          await loadProxiesTable();
        } catch (e) { toast(e.message, "error"); }
      });
    });
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
        <select name="proxy_type" required class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
          ${existing && existing.proxy_type !== "socks5" ? `<option value="" selected>Устаревший ${escapeHTML(existing.proxy_type)} — выберите SOCKS5</option>` : ""}
          <option value="socks5" ${(!existing || existing.proxy_type === "socks5") ? "selected" : ""}>socks5</option>
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
    const form = ev.currentTarget;
    const hostInput = form.querySelector('input[name="host"]');
    const portInput = form.querySelector('input[name="port"]');
    const fd = new FormData(form);
    setFieldError(hostInput, "");
    setFieldError(portInput, "");
    const host = (fd.get("host") || "").toString().trim();
    const port = Number(fd.get("port") || 0);
    let bad = false;
    if (fd.get("proxy_type") !== "socks5") {
      toast("Поддерживается только SOCKS5. Проверьте тип прокси перед сохранением.", "error");
      bad = true;
    }
    if (!host) {
      setFieldError(hostInput, "Укажите хост прокси.");
      bad = true;
    }
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      setFieldError(portInput, "Порт — число от 1 до 65535.");
      bad = true;
    }
    if (bad) return;
    const body = {
      name: (fd.get("name") || "").toString().trim(),
      host,
      port,
      username: (fd.get("username") || "").toString() || null,
      proxy_type: fd.get("proxy_type").toString(),
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

/* ------------------------------- Guide --------------------------------- */

function renderGuide() {
  setHeader("Гайд", "Как пользоваться CoreBot: от первого запуска до рассылки");
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full space-y-4 max-w-5xl">
      <div class="card">
        <h3 class="font-semibold mb-2">0. Что это за сервис (30 секунд)</h3>
        <p class="text-sm text-slate-400">CoreBot — один инстанс на одном VPS: <b class="text-slate-200">Telegram-аккаунты → прокси → рассылки → диалоги → нейрочат</b>. Веб-панель ничего не шлёт в Telegram напрямую: ручные ответы идут через <span class="font-mono">outbound_queue</span>, запуск/пауза/стоп рассылки — через <span class="font-mono">bot_commands</span>. Бот-процесс разбирает очереди и пишет всё в <span class="font-mono">corebot.db</span>, панель читает ту же базу и показывает live через SSE.</p>
        <div class="grid grid-cols-2 lg:grid-cols-4 gap-3 mt-3">
          ${kpi("Шаг 1", "gd1", "Прокси", "пулы ACCOUNT_RUNTIME")}
          ${kpi("Шаг 2", "gd2", "Аккаунты", "TData → session")}
          ${kpi("Шаг 3", "gd3", "Рассылка", "черновик → старт")}
          ${kpi("Шаг 4", "gd4", "Диалоги", "ручное + AI")}
        </div>
      </div>
      <div class="card">
        <h3 class="font-semibold mb-2">1. Первый запуск: за 5 минут</h3>
        <ol class="text-sm text-slate-400 space-y-1 list-decimal ml-5">
          <li>Откройте <a href="#/proxies" class="text-accent-400">Прокси</a>: создайте группу (ACCOUNT_RUNTIME) и импортируйте строки <span class="font-mono">host:port@user:pass</span> или <span class="font-mono">user:pass@host:port</span>. Проверьте кнопкой «Тест» — строка сразу подсвечивает результат.</li>
          <li>Откройте <a href="#/tdata-check" class="text-accent-400">TData-проверка</a>: залейте ZIP, выберите check-пул (TDATA_CHECK). Статус <b>годен</b> = можно импортировать.</li>
          <li>Откройте <a href="#/accounts" class="text-accent-400">Аккаунты → Импорт TData</a>: залейте тот же ZIP. Привяжите прокси к каждому аккаунту (без прокси подключение блокируется защитой от бана).</li>
          <li>Откройте <a href="#/mailings" class="text-accent-400">Рассылки</a>: создайте черновик, заполните текст + варианты, выберите группу аккаунтов, нажмите Старт. Статус смотрите в <a href="#/dashboard" class="text-accent-400">Дашборде</a>.</li>
          <li>Ответы клиентов появятся в <a href="#/dialogs" class="text-accent-400">Диалогах</a> (live-точка сверху = SSE online). Ручной ответ — через композер, он уйдёт той же Telethon-сессией.</li>
        </ol>
      </div>
      <div class="card">
        <h3 class="font-semibold mb-2">2. Разделы панели — что где</h3>
        <div class="table-scroll"><table class="cb-table text-xs">
          <thead><tr><th>Раздел</th><th>Для чего</th><th>Частая ошибка</th></tr></thead>
          <tbody>
            <tr><td>📊 Дашборд</td><td>KPI, активность, рассылки за 24ч</td><td class="text-slate-400">«0 везде» = бот-процесс не пишет heartbeat &gt;2 мин</td></tr>
            <tr><td>👤 Аккаунты</td><td>AI_ACTIVE/MANUAL, импорт, 2FA, фото</td><td class="text-slate-400">MANUAL режет нейрочат, но входящие всё равно видны</td></tr>
            <tr><td>🛡 TData-проверка</td><td>Проверка ZIP без создания аккаунта</td><td class="text-slate-400">Нужен пул TDATA_CHECK, runtime-пул отклонит API</td></tr>
            <tr><td>📁 Группы</td><td>Пулы аккаунтов для рассылок</td><td class="text-slate-400">Пустая группа = «нет доступных аккаунтов», пауза 60с</td></tr>
            <tr><td>🧑‍🤝‍🧑 Клиенты</td><td>База контактов, классы (accept/stop/bl/pulse)</td><td class="text-slate-400">Дубли username/tg-id режутся уникальными индексами</td></tr>
            <tr><td>💬 Диалоги</td><td>Live-переписки, ручные ответы</td><td class="text-slate-400">offline сверху = SSE оборван, обновите страницу</td></tr>
            <tr><td>📨 Очередь</td><td>Ручные исходящие: pending → sending → sent/uncertain</td><td class="text-slate-400">При uncertain проверьте диалог: повтор может создать дубль</td></tr>
            <tr><td>📣 Рассылки</td><td>Настройка отдельно от запуска (start/pause/stop)</td><td class="text-slate-400">«scheduled» при занятом пуле теперь честно отклоняется</td></tr>
            <tr><td>🔗 Ссылки</td><td>Трекинг переходов /r/код → 307</td><td class="text-slate-400">Снаружи кликабельно, только если CP доступен из интернета</td></tr>
            <tr><td>🔎 Парсинг</td><td>Задачи Telethon: каналы/группы/пользователи</td><td class="text-slate-400">Только один loop: PARSER_EMBEDDED=1 xor parser_worker</td></tr>
            <tr><td>🌐 Прокси</td><td>SOCKS5/HTTP пулы, TDATA_CHECK отдельно</td><td class="text-slate-400">Мёртвый прокси = аккаунт пропускается при connect_all</td></tr>
            <tr><td>🗃 Архив</td><td>Восстановление мягко-удалённых диалогов</td><td class="text-slate-400">hard-удаление без архива не восстановить</td></tr>
            <tr><td>⚙️ Настройки</td><td>Инстанс, OpenRouter-ключ, очистка, операторы</td><td class="text-slate-400">viewer — read-only, пишет только operator+</td></tr>
          </tbody>
        </table></div>
      </div>
      <div class="card">
        <h3 class="font-semibold mb-2">3. Нейрочат: как не сжечь бюджет</h3>
        <ul class="text-sm text-slate-400 space-y-1 list-disc ml-5">
          <li>Глобальный выключатель + выключатель рассылки + режим аккаунта: <span class="font-mono">AI_ACTIVE</span> отвечает, <span class="font-mono">MANUAL</span> — только оператор.</li>
          <li>Входящий текст длиннее 6000 обрезается для LLM (в историю пишется как есть, в промпт идёт срез).</li>
          <li>Метки LLM: [SEND_LINK] только по просьбе ссылки, [STOP]/[ACCEPT]/[DECLINE]/[HATER] — классы клиента.</li>
          <li>Нет ключа OpenRouter = deny <span class="font-mono">missing_openrouter_key</span>, смотрите Логи.</li>
        </ul>
      </div>
      <div class="card">
        <h3 class="font-semibold mb-2">4. Если что-то пошло не так</h3>
        <ul class="text-sm text-slate-400 space-y-1 list-disc ml-5">
          <li><b class="text-slate-200">Рассылка стоит:</b> Дашборд → heartbeat бота, затем Логи → FloodWait/PEER_FLOOD. Увеличьте задержки, смените аккаунт.</li>
          <li><b class="text-slate-200">Аккаунт не подключается:</b> Аккаунты → прокси рабочий? сессия авторизована? SpamBot-статус?</li>
          <li><b class="text-slate-200">Диалоги пустые, SSE offline:</b> токен протух (перелогиньтесь), CP недоступен, или БД &gt;2ГБ/диск &gt;80%.</li>
          <li><b class="text-slate-200">Боюсь нажать не то:</b> viewer ничего не сломает (read-only), опасные кнопки спрашивают подтверждение, архив лечит hard-ошибки очистки.</li>
        </ul>
      </div>
      <div class="card">
        <h3 class="font-semibold mb-2">5. Безопасность за 3 правила</h3>
        <p class="text-sm text-slate-400">Панель только на <span class="font-mono">127.0.0.1:8081</span> (не открывать наружу). Пароли — только через JSON-body, никогда в URL. JWT живёт в памяти вкладки: не делитесь URL с <span class="font-mono">?token=</span> (это костыль для EventSource), выходите кнопкой «Выйти» на чужом ПК. Навигация: <span class="font-mono">Ctrl+K</span> — палитра команд.</p>
      </div>
    </div>
  `;
}

/* --------------------------- Other helpers ----------------------------- */

function setHeader(title, sub = "") {
  $("#pageTitle").textContent = title;
  $("#pageSubtitle").textContent = sub;
}

async function requestEngagementPreview(accountId, messageLink, onProgress = () => {}) {
  const queued = await api("/business/engagement/previews", {
    method: "POST", body: { account_id: accountId, message_link: messageLink },
  });
  if (!Number.isInteger(Number(queued.id)) || Number(queued.id) <= 0) {
    throw new Error("Не удалось поставить проверку сообщения в очередь");
  }
  for (let attempt = 0; attempt < 20; attempt++) {
    const preview = await api(`/business/engagement/previews/${queued.id}`);
    if (preview.status === "done") return preview;
    if (preview.status === "failed") {
      throw new Error(`Проверка сообщения не прошла: ${preview.error_code || "нет доступа"}`);
    }
    onProgress(preview.status);
    if (attempt < 19) await new Promise(resolve => setTimeout(resolve, 1500));
  }
  throw new Error("Проверка ещё выполняется. Попробуйте позже.");
}

async function discoverManagedMessages(accountId, groupRef, onProgress = () => {}) {
  const queued = await api("/business/engagement/discoveries", {
    method: "POST", body: { account_id: accountId, group_ref: groupRef },
  });
  if (!Number.isInteger(Number(queued.id)) || Number(queued.id) <= 0) {
    throw new Error("Не удалось поставить чтение группы в очередь");
  }
  for (let attempt = 0; attempt < 20; attempt++) {
    const result = await api(`/business/engagement/discoveries/${queued.id}`);
    if (result.status === "done") return result;
    if (result.status === "failed") {
      throw new Error(`Не удалось прочитать группу: ${result.error_code || "нет доступа"}`);
    }
    onProgress(result.status);
    if (attempt < 19) await new Promise(resolve => setTimeout(resolve, 1500));
  }
  throw new Error("Проверка группы ещё выполняется. Попробуйте позже.");
}

async function generateVerifiedEngagementDraft(mode, previewId, instruction) {
  return api("/business/engagement/drafts", {
    method: "POST", body: { mode, preview_id: previewId, instruction },
  });
}

async function renderEngagement(mode) {
  mode = mode === "chat" ? "chat" : "comment";
  const title = mode === "comment" ? "Нейрокомментинг" : "Нейрошиллинг";
  setHeader(title, "Контекст → черновик → проверка → отправка");
  const root = $("#pageRoot");
  const readonly = isReadOnlyRole();
  root.innerHTML = `
    <div class="v2-engagement cb-scroll">
      <div class="v2-module-heading">
        <p class="v2-eyebrow">COMMUNITY / ${mode === "comment" ? "COMMENTS" : "CHAT"}</p>
        <h2>${title}</h2>
        <p>${mode === "comment" ? "Составляйте комментарии к публикациям в своих обсуждениях." : "Готовьте уместные ответы о своём продукте в своих групповых чатах."} ИИ создаёт черновик; отправка начинается только после вашей проверки.</p>
        <div class="v2-module-tabs"><a href="#/engagement/comment" class="${mode === "comment" ? "active" : ""}">Комментарии</a><a href="#/engagement/chat" class="${mode === "chat" ? "active" : ""}">Групповые ответы</a></div>
      </div>
      <section class="v2-module-list">
        <div class="v2-section-heading"><h3>Найти сообщение в своей группе</h3><span>Только чтение, до 10 текстовых сообщений</span></div>
        <div class="v2-form-content">
          <label>Ссылка на группу или @username<input id="engagementGroupRef" type="text" maxlength="180" placeholder="https://t.me/c/1234567890 или @my_group" /></label>
          <button id="engagementDiscover" class="v2-button" type="button" ${readonly ? "disabled" : ""}>Показать сообщения ↗</button>
          <p id="engagementDiscoverError" class="v2-form-error" role="alert"></p>
          <div id="engagementCandidates" aria-live="polite"></div>
        </div>
      </section>
      <div class="v2-engagement-grid">
        <form id="engagementForm" class="v2-module-form">
          <div class="v2-section-heading"><h3>Новый черновик</h3><span>01 / Контекст сообщения</span></div>
          <div class="v2-form-content">
            <label>Аккаунт<select id="engagementAccount" required><option value="">Загрузка…</option></select></label>
            <label>Ссылка на сообщение в группе<input id="engagementLink" type="url" required maxlength="180" placeholder="https://t.me/c/1234567890/42" /></label>
            <p class="v2-field-note">Для комментария укажите ссылку на сообщение в привязанной группе обсуждения. Ссылка на пост канала здесь не подходит.</p>
            <div id="engagementVerifiedSource" class="v2-field-note" aria-live="polite">Сначала проверьте сообщение. Бот прочитает исходный текст и права выбранного аккаунта; этот шаг ничего не отправляет в чат.</div>
            <p class="v2-field-note">После просмотра контекста он и ваше уточнение будут переданы выбранному ИИ-провайдеру для генерации черновика.</p>
            <label>Уточнение для ИИ <small>необязательно</small><textarea id="engagementInstruction" maxlength="1000" rows="3" placeholder="Например: ответить на вопрос о доставке"></textarea></label>
            <button id="engagementGenerate" class="v2-button" type="submit" ${readonly ? "disabled" : ""}>Проверить сообщение ↗</button>
            <p id="engagementFormError" class="v2-form-error" role="alert"></p>
          </div>
        </form>
        <section class="v2-module-list">
          <div class="v2-section-heading"><h3>Черновики и отправки</h3><button id="engagementReload" class="v2-text-button">Обновить</button></div>
          <div id="engagementDrafts"><p class="v2-empty">Загружаем…</p></div>
        </section>
      </div>
    </div>`;
  $("#engagementReload").addEventListener("click", () => loadEngagementDrafts(mode));
  let verifiedPreview = null;
  const resetPreview = () => {
    verifiedPreview = null;
    $("#engagementVerifiedSource").textContent = "Сначала проверьте сообщение. Бот прочитает исходный текст и права выбранного аккаунта; этот шаг ничего не отправляет в чат.";
    $("#engagementGenerate").textContent = "Проверить сообщение ↗";
  };
  $("#engagementAccount").addEventListener("change", resetPreview);
  $("#engagementLink").addEventListener("input", resetPreview);
  $("#engagementDiscover").addEventListener("click", async () => {
    const button = $("#engagementDiscover");
    const error = $("#engagementDiscoverError");
    const candidates = $("#engagementCandidates");
    error.textContent = "";
    candidates.innerHTML = "";
    const accountId = Number($("#engagementAccount").value);
    const groupRef = $("#engagementGroupRef").value.trim();
    if (!accountId || !groupRef) {
      error.textContent = "Выберите аккаунт и укажите свою группу.";
      return;
    }
    setBusy(button, true, "Читаем…");
    try {
      const result = await discoverManagedMessages(accountId, groupRef, (status) => {
        candidates.textContent = status === "processing" ? "Читаем сообщения группы…" : "Ожидаем проверку прав…";
      });
      const messages = Array.isArray(result.messages) ? result.messages.slice(0, 10) : [];
      candidates.innerHTML = messages.length
        ? messages.map(m => `<article class="v2-draft-card"><p class="v2-draft-source">${escapeHTML(m.source_text || "")}</p><button type="button" data-candidate-link="${escapeHTML(m.message_link || "")}">Выбрать сообщение ↗</button></article>`).join("")
        : `<p class="v2-empty">Подходящих новых текстовых сообщений не найдено.</p>`;
    } catch (e) {
      error.textContent = e.message || "Не удалось прочитать группу";
      candidates.innerHTML = "";
    } finally {
      setBusy(button, false);
    }
  });
  $("#engagementCandidates").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-candidate-link]");
    if (!button) return;
    $("#engagementLink").value = button.dataset.candidateLink;
    resetPreview();
    $("#engagementLink").focus();
  });
  $("#engagementForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("#engagementGenerate");
    const error = $("#engagementFormError");
    error.textContent = "";
    const accountId = Number($("#engagementAccount").value);
    const messageLink = $("#engagementLink").value.trim();
    setBusy(button, true, verifiedPreview ? "Генерируем…" : "Проверяем…");
    try {
      if (!verifiedPreview) {
        const preview = await requestEngagementPreview(accountId, messageLink, (status) => {
          $("#engagementVerifiedSource").textContent = status === "processing"
            ? "Проверяем права и исходное сообщение…" : "Проверка ожидает запуска…";
        });
        verifiedPreview = { id: Number(preview.id), accountId, messageLink };
        $("#engagementVerifiedSource").innerHTML =
          `<strong>Проверено: ${escapeHTML(preview.managed_title || "управляемый чат")}</strong><br>` +
          `Исходный текст: ${escapeHTML(preview.source_text || "")}`;
        toast("Сообщение и права проверены. Просмотрите контекст и создайте черновик.", "success");
      } else {
        if (verifiedPreview.accountId !== accountId || verifiedPreview.messageLink !== messageLink) {
          resetPreview();
          throw new Error("Ссылка или аккаунт изменились. Проверьте сообщение снова.");
        }
        await generateVerifiedEngagementDraft(
          mode, verifiedPreview.id, $("#engagementInstruction").value.trim(),
        );
        resetPreview();
        toast("Черновик создан. Проверьте текст перед отправкой.", "success");
        await loadEngagementDrafts(mode);
      }
    } catch (e) {
      error.textContent = e.message || "Не удалось создать черновик";
    } finally {
      setBusy(button, false);
      button.textContent = verifiedPreview ? "Создать черновик ↗" : "Проверить сообщение ↗";
    }
  });
  $("#engagementDrafts").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-engagement-action]");
    if (!button) return;
    const id = Number(button.dataset.draftId);
    const card = button.closest("article");
    const body = card?.querySelector("textarea[data-draft-body]")?.value.trim() || "";
    const action = button.dataset.engagementAction;
    if (action === "approve" && !confirm("Отправить проверенный ответ в указанное обсуждение Telegram?")) return;
    if (body.length < 2 || body.length > 2000) { toast("Текст должен быть от 2 до 2000 символов", "error"); return; }
    setBusy(button, true, "Сохраняем…");
    try {
      await api(`/business/engagement/drafts/${id}`, { method: "PATCH", body: { draft_text: body } });
      if (action === "approve") await api(`/business/engagement/drafts/${id}/approve`, { method: "POST" });
      toast(action === "approve" ? "Ответ поставлен в очередь" : "Черновик сохранён", "success");
      await loadEngagementDrafts(mode);
    } catch (e) {
      toast(e.message || "Не удалось сохранить черновик", "error");
      setBusy(button, false);
    }
  });
  try {
    const accounts = await api("/business/accounts");
    const active = accounts.filter(a => a.status === "active");
    $("#engagementAccount").innerHTML = `<option value="">Выберите аккаунт</option>${active.map(a => `<option value="${a.id}">${escapeHTML(a.list_label || a.username || `Аккаунт #${a.id}`)}</option>`).join("")}`;
    if (!active.length) $("#engagementFormError").textContent = "Нет активного аккаунта. Добавьте и подключите аккаунт в разделе «Аккаунты».";
  } catch (e) {
    $("#engagementFormError").textContent = e.message || "Не удалось загрузить аккаунты";
  }
  await loadEngagementDrafts(mode);
}

async function loadEngagementDrafts(mode) {
  const root = $("#engagementDrafts");
  try {
    const drafts = (await api("/business/engagement/drafts?limit=100")).filter(d => d.mode === mode);
    if (!drafts.length) {
      root.innerHTML = `<p class="v2-empty">Пока нет черновиков. Добавьте контекст слева и создайте первый ответ.</p>`;
      return;
    }
    const labels = { draft: "Черновик", queued: "В очереди", sending: "Отправляется", sent: "Отправлено", failed: "Ошибка", uncertain: "Нужна проверка" };
    root.innerHTML = drafts.map(d => `<article class="v2-draft-card">
      <div class="v2-draft-meta"><span>#${d.id} · ${escapeHTML(fmtDate(d.created_at))}</span><strong class="v2-draft-status ${escapeHTML(d.status)}">${labels[d.status] || escapeHTML(d.status)}</strong></div>
      <a href="${escapeHTML(d.message_link)}" target="_blank" rel="noopener noreferrer">${escapeHTML(d.message_link)}</a>
      <p class="v2-draft-source">${escapeHTML(d.source_text)}</p>
      <label>Текст ответа<textarea data-draft-body rows="4" maxlength="2000" ${d.status === "draft" && !isReadOnlyRole() ? "" : "readonly"}>${escapeHTML(d.draft_text)}</textarea></label>
      ${d.error_code ? `<small class="v2-draft-error">Причина: ${escapeHTML(d.error_code.replaceAll("_", " "))}</small>` : ""}
      ${d.status === "draft" && !isReadOnlyRole() ? `<div class="v2-draft-actions"><button data-engagement-action="save" data-draft-id="${d.id}">Сохранить</button><button data-engagement-action="approve" data-draft-id="${d.id}">Одобрить и отправить ↗</button></div>` : ""}
    </article>`).join("");
  } catch (e) {
    root.innerHTML = `<p class="v2-empty">Не удалось загрузить черновики: ${escapeHTML(e.message || "ошибка сети")}</p>`;
  }
}

async function renderAccountSafety() {
  setHeader("Состояние аккаунтов", "Ограничения, остановки и ручная проверка");
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="v2-safety cb-scroll">
      <div class="v2-safety-hero">
        <div>
          <p class="v2-eyebrow">CONTROL / ACCOUNT HEALTH</p>
          <h2>Все аккаунты.<br><em>Один контур контроля.</em></h2>
          <p>Если Telegram ограничил отправку или оператор поставил паузу, новые сообщения не уйдут ни из одной кампании. Причина остановки сохраняется после перезапуска.</p>
        </div>
        <button id="safetyRefresh" class="v2-button">Обновить состояние ↗</button>
      </div>
      <div id="safetyStats" class="v2-safety-stats" aria-live="polite"></div>
      <div class="v2-safety-section">
        <div class="v2-section-heading"><h3>Аккаунты</h3><span>Ручное возобновление доступно администратору после проверки ограничения</span></div>
        <div id="safetyRows" class="v2-safety-rows" aria-live="polite"><p class="v2-empty">Загружаем состояние…</p></div>
      </div>
      <div id="safetyEvents" class="v2-safety-events hidden"></div>
      <div class="v2-safety-section">
        <div class="v2-section-heading"><h3>Отчёт прогрева</h3><label class="text-xs">Период <select id="warmupReportDays" class="bg-ink-800 border border-ink-600 rounded px-2 py-1"><option value="7">7 дней</option><option value="30">30 дней</option></select></label></div>
        <div id="warmupReport" class="text-sm text-slate-300" aria-live="polite">Загружаем отчёт…</div>
      </div>
      <p class="v2-safety-note">Индикаторы показывают состояние интеграции и явные ответы Telegram. Они не прогнозируют блокировки аккаунтов.</p>
    </div>`;
  $("#safetyRefresh").addEventListener("click", loadAccountSafety);
  $("#warmupReportDays")?.addEventListener("change", loadWarmupReport);
  $("#safetyRows").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-safety-action]");
    if (!button) return;
    const id = Number(button.dataset.accountId);
    const action = button.dataset.safetyAction;
    if (!Number.isInteger(id) || id <= 0) return;
    if (action === "events") {
      await showAccountSafetyEvents(id, button.dataset.accountTitle || `#${id}`);
      return;
    }
    if (action === "health") {
      await showAccountObservedHealth(id, button.dataset.accountTitle || `#${id}`);
      return;
    }
    if (action === "ai") {
      await showAccountSafetyAssessment(id, button.dataset.accountTitle || `#${id}`);
      return;
    }
    if (action === "resume" && !confirm("Запустить проверку авторизации через назначенный SOCKS5-прокси и затем возобновить аккаунт?")) return;
    setBusy(button, true, "Сохраняем…");
    try {
      const result = await api(`/business/account-safety/${id}/${action}`, { method: "POST" });
      if (action === "resume") {
        toast("Проверка сессии и прокси поставлена в очередь", "info");
        for (let attempt = 0; attempt < 20; attempt++) {
          await new Promise(resolve => setTimeout(resolve, 1500));
          const command = await api(`/business/account-safety/resume-commands/${result.command_id}`);
          if (command.status === "done") {
            toast("Авторизация и прокси проверены, аккаунт возобновлён", "success");
            break;
          }
          if (command.status === "failed") {
            toast(`Возобновление не прошло: ${command.error || "требуется проверка"}`, "error", 6000);
            break;
          }
          if (attempt === 19) toast("Проверка продолжается; обновите состояние позже", "info", 5000);
        }
      } else {
        toast("Аккаунт остановлен", "success");
      }
      await loadAccountSafety();
    } catch (error) {
      toast(error.message || "Не удалось изменить состояние", "error");
      setBusy(button, false);
    }
  });
  await loadAccountSafety();
  await loadWarmupReport();
}

async function loadWarmupReport() {
  const target = $("#warmupReport");
  if (!target) return;
  const days = Number($("#warmupReportDays")?.value || 7);
  try {
    const report = await api(`/business/warmup/report?days=${days}`);
    const totals = report.totals || {};
    const count = (value) => Number(value || 0);
    const accountRows = (report.accounts || []).map((account) => `<tr>
      <td>${escapeHTML(account.title || `#${account.account_id}`)}</td>
      <td>${escapeHTML(account.effective_profile_name || "—")}</td>
      <td>${count(account.totals?.ok)}</td><td>${count(account.totals?.skip)}</td><td>${count(account.totals?.fail)}</td>
      <td>${escapeHTML(account.pause_reason || "—")}</td></tr>`).join("");
    const reasons = Object.entries(report.skip_fail_reasons || {})
      .sort((a, b) => count(b[1].total) - count(a[1].total))
      .slice(0, 12)
      .map(([reason, values]) => `${escapeHTML(reason.replaceAll("_", " "))}: ${count(values.total)}`)
      .join(" · ");
    target.innerHTML = `<p class="mb-3">Действий по журналу: <strong>${count(totals.total)}</strong> · выполнено ${count(totals.ok)} · пропущено ${count(totals.skip)} · ошибок ${count(totals.fail)}.</p>
      <div class="overflow-x-auto"><table class="cb-table text-xs"><thead><tr><th>Аккаунт</th><th>Профиль</th><th>Выполнено</th><th>Пропуск</th><th>Ошибка</th><th>Пауза</th></tr></thead><tbody>${accountRows || '<tr><td colspan="6">Нет аккаунтов</td></tr>'}</tbody></table></div>
      <p class="text-xs text-slate-400 mt-3">Причины пропуска и ошибок: ${reasons || "нет"}</p>
      <p class="text-xs text-slate-500 mt-1">Отчёт строится по сохранённым действиям за последние ${days} дней; ранние остановки без записи в журнал не входят.</p>`;
  } catch (error) {
    target.textContent = `Не удалось загрузить отчёт: ${error.message || "ошибка сети"}`;
  }
}

async function loadAccountSafety() {
  try {
    const rows = await api("/business/account-safety");
    const review = rows.filter(row => ["review_required", "cooling_down", "needs_reauth", "verifying"].includes(row.state)).length;
    const ready = rows.filter(row => row.state === "ready").length;
    const unavailable = rows.length - ready - review;
    $("#safetyStats").innerHTML = `
      <div><strong>${rows.length}</strong><span>Всего аккаунтов</span></div>
      <div><strong>${ready}</strong><span>Готовы к отправке</span></div>
      <div class="${review ? "attention" : ""}"><strong>${review}</strong><span>Требуют проверки</span></div>
      <div><strong>${unavailable}</strong><span>Недоступны</span></div>`;
    if (!rows.length) {
      $("#safetyRows").innerHTML = `<p class="v2-empty">Аккаунтов пока нет. <a href="#/accounts">Добавьте первый аккаунт</a>, чтобы видеть его состояние здесь.</p>`;
      return;
    }
    const admin = ["super_admin", "tenant_admin"].includes(state.user?.role || "");
    const writable = !isReadOnlyRole();
    $("#safetyRows").innerHTML = rows.map(row => {
      const label = row.state === "ready" ? "Готов" : row.state === "cooling_down" ? "Ожидание" : row.state === "needs_reauth" ? "Авторизация" : row.state === "verifying" ? "Проверяем" : row.state === "review_required" ? "Проверка" : "Недоступен";
      const reason = row.reason_code ? row.reason_code.replaceAll("_", " ") : (row.state === "unavailable" ? row.account_status : "Нет ограничений");
      const cooldownDone = !row.resume_at || new Date(row.resume_at).getTime() <= Date.now();
      const action = row.state === "ready"
        ? (writable ? `<button class="v2-text-button" data-safety-action="pause" data-account-id="${row.account_id}">Остановить</button>` : "")
        : (["review_required", "cooling_down"].includes(row.state) && cooldownDone && admin ? `<button class="v2-text-button" data-safety-action="resume" data-account-id="${row.account_id}">Возобновить</button>` : "");
      return `<article class="v2-safety-row">
        <div class="v2-account-monogram" aria-hidden="true">${escapeHTML((row.title || "A").slice(0, 1).toUpperCase())}</div>
        <div class="v2-account-title"><strong>${escapeHTML(row.title)}</strong><small>Аккаунт #${row.account_id} · ${escapeHTML(row.account_status)}</small></div>
        <div class="v2-safety-status ${escapeHTML(row.state)}"><span class="v2-status-dot"></span>${label}</div>
        <div class="v2-safety-reason"><small>Причина</small><span>${escapeHTML(reason)}</span>${row.resume_at ? `<small>До ${escapeHTML(fmtDate(row.resume_at))}</small>` : ""}</div>
        <div class="v2-safety-budget"><small>Попытки сегодня</small><span>${row.attempts_today} / ${row.daily_limit}</span></div>
        <div class="v2-safety-actions">${action}<button class="v2-text-button muted" data-safety-action="health" data-account-id="${row.account_id}" data-account-title="${escapeHTML(row.title)}">Здоровье</button>${writable ? `<button class="v2-text-button muted" data-safety-action="ai" data-account-id="${row.account_id}" data-account-title="${escapeHTML(row.title)}">ИИ-разбор</button>` : ""}<button class="v2-text-button muted" data-safety-action="events" data-account-id="${row.account_id}" data-account-title="${escapeHTML(row.title)}">Журнал</button></div>
      </article>`;
    }).join("");
  } catch (error) {
    $("#safetyRows").innerHTML = `<p class="v2-empty">Не удалось загрузить состояние: ${escapeHTML(error.message || "ошибка сети")}</p>`;
  }
}

async function showAccountSafetyEvents(accountId, title) {
  const panel = $("#safetyEvents");
  panel.classList.remove("hidden");
  panel.innerHTML = `<div class="v2-section-heading"><h3>Журнал · ${escapeHTML(title)}</h3></div><p class="v2-empty">Загружаем события…</p>`;
  try {
    const events = await api(`/business/account-safety/${accountId}/events`);
    panel.innerHTML = `<div class="v2-section-heading"><h3>Журнал · ${escapeHTML(title)}</h3><button class="v2-text-button" id="safetyEventsClose">Закрыть</button></div>
      ${events.length ? `<ol class="v2-event-list">${events.map(item => `<li><time>${escapeHTML(fmtDate(item.created_at))}</time><strong>${item.event_type === "chat_paused" ? "Пауза чата" : item.event_type === "paused" ? "Пауза" : item.event_type === "resume_requested" ? "Проверка запрошена" : item.event_type === "verification_failed" ? "Проверка не прошла" : "Возобновление"}</strong><span>${escapeHTML(item.reason_code.replaceAll("_", " "))}${item.peer_ref ? ` · ${escapeHTML(item.peer_ref)}` : ""}</span><small>${escapeHTML(item.source)}${item.resume_at ? ` · до ${escapeHTML(fmtDate(item.resume_at))}` : ""}</small></li>`).join("")}</ol>` : `<p class="v2-empty">Событий пока нет.</p>`}`;
    $("#safetyEventsClose").addEventListener("click", () => panel.classList.add("hidden"));
  } catch (error) {
    panel.innerHTML = `<p class="v2-empty">Не удалось загрузить журнал: ${escapeHTML(error.message || "ошибка сети")}</p>`;
  }
}

async function showAccountSafetyAssessment(accountId, title) {
  const panel = $("#safetyEvents");
  panel.classList.remove("hidden");
  panel.innerHTML = `<div class="v2-section-heading"><h3>ИИ-разбор · ${escapeHTML(title)}</h3></div><p class="v2-empty">Анализируем обезличенные сигналы аккаунта…</p>`;
  try {
    const result = await api(`/business/account-safety/${accountId}/ai-assessment`, { method: "POST" });
    panel.innerHTML = `<div class="v2-section-heading"><h3>ИИ-разбор · ${escapeHTML(title)}</h3><button class="v2-text-button" id="safetyAssessmentClose">Закрыть</button></div>
      <p class="mb-3 whitespace-pre-wrap">${escapeHTML(result.assessment || "Анализ недоступен.")}</p>
      <p class="text-xs text-slate-500">${escapeHTML(result.disclaimer || "Вывод ИИ основан только на наблюдаемых сигналах и не меняет ограничения аккаунта.")}</p>`;
    $("#safetyAssessmentClose").addEventListener("click", () => panel.classList.add("hidden"));
  } catch (error) {
    panel.innerHTML = `<div class="v2-section-heading"><h3>ИИ-разбор · ${escapeHTML(title)}</h3><button class="v2-text-button" id="safetyAssessmentClose">Закрыть</button></div><p class="v2-form-error">${escapeHTML(error.message || "Не удалось выполнить анализ")}</p>`;
    $("#safetyAssessmentClose").addEventListener("click", () => panel.classList.add("hidden"));
  }
}

async function showAccountObservedHealth(accountId, title) {
  const panel = $("#safetyEvents");
  panel.classList.remove("hidden");
  panel.innerHTML = `<div class="v2-section-heading"><h3>Здоровье · ${escapeHTML(title)}</h3></div><p class="v2-empty">Считаем наблюдаемые показатели…</p>`;
  try {
    const health = await api(`/business/account-safety/${accountId}/health`);
    const checks = await api(`/business/account-safety/${accountId}/health-checks?limit=10`);
    const reasons = Object.entries(health.score_deductions || {})
      .map(([reason, weight]) => `<li>${escapeHTML(reason.replaceAll("_", " "))}: −${Number(weight)}</li>`).join("");
    const windowRow = (label, value) => `<tr><td>${label}</td><td>${Number(value.mailing_sent)}</td><td>${Number(value.mailing_failed)}</td><td>${Object.entries(value.safety_events_by_reason || {}).map(([reason, count]) => `${escapeHTML(reason)}: ${Number(count)}`).join(", ") || "—"}</td></tr>`;
    const checkRows = checks.map((item) => `<tr><td>${escapeHTML(fmtDate(item.requested_at))}</td><td>${escapeHTML(item.status)}</td><td>${escapeHTML(item.proxy_state)}</td><td>${escapeHTML(item.auth_state)}</td><td>${escapeHTML((item.reason_code || "—").replaceAll("_", " "))}</td></tr>`).join("");
    panel.innerHTML = `<div class="v2-section-heading"><h3>Здоровье · ${escapeHTML(title)}</h3><button class="v2-text-button" id="safetyHealthClose">Закрыть</button></div>
      <p class="mb-2">Операционная готовность: <strong>${Number(health.readiness_score_1_10)}/10</strong>. Это локальная оценка текущего состояния, не прогноз блокировки.</p>
      <p class="mb-2">Прокси: ${health.proxy_assigned ? "назначен" : "не назначен"} · Сессия: ${escapeHTML(health.account_status)} · Защита: ${escapeHTML(health.safety_state)}</p>
      ${reasons ? `<ul class="mb-3">${reasons}</ul>` : `<p class="mb-3">Штрафов по текущим сигналам нет.</p>`}
      <div class="overflow-x-auto"><table class="cb-table text-xs"><thead><tr><th>Период</th><th>Успешно</th><th>Ошибки</th><th>Сигналы защиты</th></tr></thead><tbody>${windowRow("7 дней", health.windows["7d"])}${windowRow("30 дней", health.windows["30d"])}</tbody></table></div>
      <p class="text-xs text-slate-500 mt-2">Эта оценка построена по локальным данным. Последняя проверка спамблока: ${escapeHTML(fmtDate(health.spam_check_at))}.</p>
      <div class="flex items-center gap-3 mt-4 mb-2"><h4 class="font-medium">История проверки прокси и авторизации</h4>${isReadOnlyRole() ? "" : '<button id="safetyLiveHealthCheck" class="v2-text-button">Проверить сейчас</button>'}</div>
      <div class="overflow-x-auto"><table class="cb-table text-xs"><thead><tr><th>Запрошена</th><th>Состояние</th><th>SOCKS5</th><th>Авторизация</th><th>Причина</th></tr></thead><tbody>${checkRows || '<tr><td colspan="5">Проверок ещё нет</td></tr>'}</tbody></table></div>
      <p class="text-xs text-slate-500 mt-2">Ожидающая проверка не подтверждает доступность аккаунта; действие выполняет бот через назначенный прокси.</p>`;
    $("#safetyHealthClose").addEventListener("click", () => panel.classList.add("hidden"));
    $("#safetyLiveHealthCheck")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      setBusy(button, true, "Проверяем…");
      try {
        const requested = await api(`/business/account-safety/${accountId}/health-checks`, { method: "POST" });
        for (let attempt = 0; attempt < 20; attempt++) {
          await new Promise((resolve) => setTimeout(resolve, 1500));
          const history = await api(`/business/account-safety/${accountId}/health-checks?limit=10`);
          const current = history.find((item) => item.id === requested.id);
          if (current && ["completed", "failed", "interrupted"].includes(current.status)) {
            toast(current.proxy_state === "ok" && current.auth_state === "ok"
              ? "Прокси и авторизация подтверждены" : `Проверка: ${current.reason_code || current.status}`,
            current.proxy_state === "ok" && current.auth_state === "ok" ? "success" : "info");
            break;
          }
          if (attempt === 19) toast("Проверка продолжается; результат появится в истории", "info");
        }
        await showAccountObservedHealth(accountId, title);
      } catch (error) {
        toast(error.message || "Не удалось выполнить проверку", "error");
        setBusy(button, false);
      }
    });
  } catch (error) {
    panel.innerHTML = `<p class="v2-empty">Не удалось загрузить показатели: ${escapeHTML(error.message || "ошибка сети")}</p>`;
  }
}

function renderNotFound() {
  setHeader("Не найдено", "Раздел не существует");
  $("#pageRoot").innerHTML = `<div class="p-6 text-slate-400">Нет такого раздела.</div>`;
}

/* ---------------------------- Bootstrap -------------------------------- */


/* ======================= Command Palette (Ctrl+K) ======================= */

const CP_COMMANDS = [
  { label: "Дашборд", section: "Навигация", ico: "📊", action: () => { window.location.hash = "#/dashboard"; } },
  { label: "Аккаунты", section: "Навигация", ico: "👤", action: () => { window.location.hash = "#/accounts"; } },
  { label: "Диалоги", section: "Навигация", ico: "💬", action: () => { window.location.hash = "#/dialogs"; } },
  { label: "Очередь", section: "Навигация", ico: "📨", action: () => { window.location.hash = "#/queue"; } },
  { label: "Парсинг", section: "Навигация", ico: "🔎", action: () => { window.location.hash = "#/parsing/channels"; } },
  { label: "Рассылки", section: "Навигация", ico: "📢", action: () => { window.location.hash = "#/mailings"; } },
  { label: "Клиенты", section: "Навигация", ico: "🧑‍🤝‍🧑", action: () => { window.location.hash = "#/clients"; } },
  { label: "Группы", section: "Навигация", ico: "📁", action: () => { window.location.hash = "#/groups"; } },
  { label: "Прокси", section: "Навигация", ico: "🌐", action: () => { window.location.hash = "#/proxies"; } },
  { label: "Архив", section: "Навигация", ico: "🗃", action: () => { window.location.hash = "#/archive"; } },
  { label: "Логи", section: "Навигация", ico: "📄", action: () => { window.location.hash = "#/logs"; } },
  { label: "Настройки", section: "Навигация", ico: "⚙️", action: () => { window.location.hash = "#/settings"; } },
  { label: "Гайд", section: "Навигация", ico: "📖", action: () => { window.location.hash = "#/guide"; } },
  { label: "Импорт TData ZIP", section: "Действия", ico: "📦", action: () => { window.location.hash = "#/accounts"; setTimeout(() => document.getElementById("tdataDropZone")?.scrollIntoView({ behavior: "smooth", block: "center" }), 300); } },
  { label: "Новый аккаунт", section: "Действия", ico: "➕", action: () => { window.location.hash = "#/accounts"; setTimeout(() => document.querySelector("#accountCreateForm input[name=phone]")?.focus(), 300); } },
  { label: "Новая рассылка", section: "Действия", ico: "✉️", action: () => { window.location.hash = "#/mailings"; setTimeout(() => document.querySelector("#mailCreateForm input[name=name]")?.focus(), 300); } },
  { label: "TData-проверка", section: "Действия", ico: "🛡", action: () => { window.location.hash = "#/tdata-check"; } },
  { label: "Очередь", section: "Навигация", ico: "📨", action: () => { window.location.hash = "#/queue"; } },
  { label: "Обновить данные", section: "Действия", ico: "⟳", action: () => navigate(window.location.hash) },
];

const cpState = { open: false, selected: 0, filtered: [] };

function cpOpen() {
  if (!state.token) return;
  cpState.open = true;
  cpState.selected = 0;
  $("#cpBackdrop").classList.add("open");
  $("#cpInput").value = "";
  cpRender("");
  $("#cpInput").focus();
}

function cpClose() {
  cpState.open = false;
  $("#cpBackdrop").classList.remove("open");
  $("#cpInput").blur();
}

function cpToggle() { cpState.open ? cpClose() : cpOpen(); }

function cpScore(cmd, q) {
  if (!q) return 1;
  const label = cmd.label.toLowerCase();
  const section = cmd.section.toLowerCase();
  if (label.includes(q)) return 100 - label.indexOf(q);
  if (section.includes(q)) return 50;
  return 0;
}

function cpRender(query) {
  const q = (query || "").trim().toLowerCase();
  cpState.filtered = CP_COMMANDS
    .map(cmd => ({ cmd, score: cpScore(cmd, q) }))
    .filter(x => x.score > 0)
    .sort((a, b) => b.score - a.score)
    .map(x => x.cmd)
    .slice(0, 50);

  if (cpState.selected >= cpState.filtered.length) cpState.selected = 0;

  const box = $("#cpResults");
  if (!cpState.filtered.length) {
    box.innerHTML = '<div class="cp-empty">Ничего не найдено</div>';
    return;
  }
  box.innerHTML = cpState.filtered.map((cmd, i) => `
    <div class="cp-item${i === cpState.selected ? " active" : ""}" data-idx="${i}" role="option" aria-selected="${i === cpState.selected}">
      <span class="cp-ico">${cmd.ico}</span>
      <span class="cp-label">${escapeHTML(cmd.label)}</span>
      <span class="cp-section">${escapeHTML(cmd.section)}</span>
    </div>
  `).join("");

  $$("#cpResults .cp-item").forEach(el => {
    el.addEventListener("click", () => cpExecute(Number(el.dataset.idx)));
    el.addEventListener("mousemove", () => { cpState.selected = Number(el.dataset.idx); cpHighlight(); });
  });
}

function cpHighlight() {
  $$("#cpResults .cp-item").forEach(el => {
    const active = Number(el.dataset.idx) === cpState.selected;
    el.classList.toggle("active", active);
    el.setAttribute("aria-selected", String(active));
  });
}

function cpMove(delta) {
  if (!cpState.filtered.length) return;
  cpState.selected = (cpState.selected + delta + cpState.filtered.length) % cpState.filtered.length;
  cpHighlight();
  $$("#cpResults .cp-item")[cpState.selected]?.scrollIntoView({ block: "nearest" });
}

function cpExecute(idx) {
  const cmd = cpState.filtered[idx];
  if (!cmd) return;
  cpClose();
  try { cmd.action(); } catch (e) { toast("Команда недоступна: " + e.message, "error"); }
}

function cpHandleKeys(e) {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    cpToggle();
    return;
  }
  if (!cpState.open) return;
  switch (e.key) {
    case "Escape": e.preventDefault(); cpClose(); break;
    case "ArrowDown": e.preventDefault(); cpMove(1); break;
    case "ArrowUp": e.preventDefault(); cpMove(-1); break;
    case "Enter": e.preventDefault(); cpExecute(cpState.selected); break;
  }
}

document.addEventListener("keydown", cpHandleKeys);
$("#cpBackdrop")?.addEventListener("click", (e) => { if (e.target.id === "cpBackdrop") cpClose(); });
$("#cpInput")?.addEventListener("input", (e) => { cpState.selected = 0; cpRender(e.currentTarget.value); });

/* ======================= TData precheck (задача 12) =======================
   Отдельный экран проверки TData-архива БЕЗ создания Account.
   Контракт: POST /business/tdata/check (multipart: file + group_id) +
   GET /business/tdata/check/{run_id} (polling). Импорт (создание
   аккаунтов) живёт отдельно — в разделе Аккаунты (tdataDropZone). */

const TDATA_STATUS_RU = {
  ok: "годен",
  archive_invalid: "битый архив",
  structure_invalid: "неверная структура",
  conversion_failed: "не сконвертировался",
  proxy_required: "нужен прокси",
  proxy_failed: "прокси не отвечает",
  unauthorized: "не авторизован",
  session_revoked: "сессия отозвана",
  account_deactivated: "аккаунт деактивирован",
  flood_wait: "флуд-ожидание",
  spam_restriction: "спам-ограничение",
  unknown: "неизвестно",
};

function tdataStatusPill(status) {
  const s = status || "unknown";
  const cls = s === "ok" ? "pill-green"
    : ["unauthorized", "session_revoked", "account_deactivated"].includes(s) ? "pill-red"
    : ["proxy_required", "proxy_failed", "conversion_failed", "archive_invalid", "structure_invalid"].includes(s) ? "pill-amber"
    : s === "flood_wait" || s === "spam_restriction" ? "pill-blue"
    : "pill-gray";
  return `<span class="pill ${cls}">${escapeHTML(TDATA_STATUS_RU[s] || s)}</span>`;
}

async function renderTdataCheck() {
  setHeader("TData-проверка", "Проверка архива БЕЗ создания аккаунта — только пул TDATA_CHECK");
  const readOnly = isReadOnlyRole();
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full space-y-4 max-w-5xl">
      <div class="card">
        <p class="text-sm text-slate-400 mb-1">
          Этот экран <b class="text-slate-200">не создаёт аккаунты</b> — он только проверяет,
          какие TData-папки в ZIP живые, через изолированный proxy-пул.
          Массовое создание аккаунтов — в разделе
          <a href="#/accounts" class="text-accent-400 hover:text-accent-300">Аккаунты → Импорт TData</a>.
        </p>
      </div>
      <div class="card">
        <h3 class="font-semibold mb-3">Новая проверка</h3>
        <form id="checkForm" class="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
          <label class="block">
            <span class="text-slate-400 text-xs">TData ZIP-архив *</span>
            <input id="checkFile" name="file" type="file" accept=".zip,application/zip"
                   class="mt-1 block w-full text-slate-300 text-xs" ${readOnly ? "disabled" : ""} />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">Check-пул прокси (purpose=TDATA_CHECK) *</span>
            <select id="checkGroup" name="group_id"
                    class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" ${readOnly ? "disabled" : ""}>
              <option value="">Загрузка пулов…</option>
            </select>
          </label>
          <div class="md:col-span-2 flex items-center gap-3 flex-wrap">
            <button id="checkSubmit" type="submit" class="btn btn-primary" ${readOnly ? "disabled title='Недоступно для роли read-only'" : ""}>Проверить архив</button>
            ${readOnly ? `<span class="text-xs text-amber-300">ⓘ Недоступно для роли read-only: проверка выполняет внешние подключения.</span>` : ""}
            <span id="checkMsg" class="text-xs text-slate-400"></span>
          </div>
        </form>
      </div>
      <div class="card">
        <h3 class="font-semibold mb-1">Предварительная проверка .session</h3>
        <p class="text-xs text-slate-400 mb-3">Проверяет готовый файл .session через тот же пул TDATA_CHECK. Эта операция не импортирует файл и не создаёт Account. Максимальный размер — 16 МиБ.</p>
        <form id="sessionCheckForm" class="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
          <label class="block">
            <span class="text-slate-400 text-xs">Файл Telegram .session *</span>
            <input id="sessionCheckFile" name="file" type="file" accept=".session" class="mt-1 block w-full text-slate-300 text-xs" ${readOnly ? "disabled" : ""} />
          </label>
          <div class="md:col-span-2 flex items-center gap-3 flex-wrap">
            <button id="sessionCheckSubmit" type="submit" class="btn btn-primary" ${readOnly ? "disabled title='Недоступно для роли read-only'" : ""}>Проверить .session</button>
            <span id="sessionCheckMsg" class="text-xs text-slate-400"></span>
          </div>
        </form>
      </div>
      <div id="checkResult" class="space-y-4">
        <div class="card text-sm text-slate-500">Проверка ещё не запускалась (idle). Выберите ZIP и check-пул.</div>
      </div>
      ${readOnly ? "" : `<section class="card" aria-label="Последние проверки">
        <div class="flex items-center gap-3 mb-3"><h3 class="font-semibold">Последние проверки</h3><button id="checkHistoryRefresh" class="btn btn-ghost ml-auto">Обновить</button></div>
        <div id="checkHistory" class="space-y-2 text-sm"><div class="text-slate-500">Загрузка истории…</div></div>
      </section>`}
    </div>
  `;
  await loadCheckGroups();
  $("#checkForm")?.addEventListener("submit", onTdataCheckSubmit);
  $("#sessionCheckForm")?.addEventListener("submit", onTdataSessionCheckSubmit);
  $("#checkHistoryRefresh")?.addEventListener("click", loadTdataCheckHistory);
  $("#checkHistory")?.addEventListener("click", (ev) => {
    const button = ev.target.closest("[data-history-run]");
    if (button) openTdataCheckHistoryRun(button.dataset.historyRun, button);
  });
  if (!readOnly) await loadTdataCheckHistory();
}

function tdataHistoryMarkup(runs) {
  const rows = (Array.isArray(runs) ? runs : []).slice(0, 20);
  if (!rows.length) return `<div class="text-slate-500">Проверок пока нет.</div>`;
  return rows.map(run => `<div class="flex flex-wrap items-center gap-2 border-b border-ink-700 py-2">
    <span class="font-mono text-xs text-slate-400">${escapeHTML(String(run.run_id || "—").slice(0, 12))}</span>
    <span>${escapeHTML(run.created_at || "—")}</span>${tdataStatusPill(run.status)}
    <span class="text-slate-400">Всего ${escapeHTML(String(run.total ?? 0))}, годных ${escapeHTML(String(run.ok_count ?? 0))}, ошибок ${escapeHTML(String(run.failed_count ?? 0))}</span>
    <span class="text-slate-500">${escapeHTML(run.requested_by || "")}</span>
    <button class="btn btn-ghost ml-auto" data-history-run="${escapeHTML(String(run.run_id || ""))}">Открыть</button>
  </div>`).join("");
}

async function loadTdataCheckHistory() {
  const box = $("#checkHistory");
  if (!box) return;
  box.innerHTML = `<div class="text-slate-500">Загрузка истории…</div>`;
  try {
    const data = await api("/business/tdata/check/history?limit=20");
    box.innerHTML = tdataHistoryMarkup(data?.runs);
  } catch (e) {
    box.innerHTML = `<div class="text-rose-300">Не удалось загрузить историю: ${escapeHTML(e.message)}</div>`;
  }
}

async function openTdataCheckHistoryRun(runId, button) {
  if (!runId) return;
  setBusy(button, true, "Загрузка…");
  try {
    const data = await api(`/business/tdata/check/${encodeURIComponent(runId)}`);
    paintCheckRun($("#checkResult"), data);
  } catch (e) {
    toast(`Не удалось открыть проверку: ${e.message}`, "error");
  } finally {
    setBusy(button, false);
  }
}

async function loadCheckGroups() {
  const sel = $("#checkGroup");
  if (!sel) return;
  try {
    const groups = await api("/business/proxy-groups");
    if (!groups?.length) {
      sel.innerHTML = `<option value="">Нет proxy-групп</option>`;
      setFieldError(sel, "Создайте группу с purpose=TDATA_CHECK в разделе Прокси.");
      return;
    }
    sel.innerHTML = `<option value="">— выберите пул —</option>` + groups.map(g => {
      const purpose = (g.purpose || "ACCOUNT_RUNTIME").toUpperCase();
      const mark = purpose === "TDATA_CHECK" ? "✓" : "✗";
      return `<option value="${g.id}" data-purpose="${escapeHTML(purpose)}">${escapeHTML(g.name)} — ${escapeHTML(purpose)} ${mark}</option>`;
    }).join("");
    const firstCheck = groups.find(g => (g.purpose || "").toUpperCase() === "TDATA_CHECK");
    if (firstCheck) sel.value = String(firstCheck.id);
    if (!firstCheck) setFieldError(sel, "Среди групп нет пула TDATA_CHECK — проверка будет отклонена API.");
  } catch (e) {
    sel.innerHTML = `<option value="">Ошибка загрузки</option>`;
    setFieldError(sel, `Не удалось загрузить пулы: ${e.message}`);
  }
}

async function onTdataCheckSubmit(ev) {
  ev.preventDefault();
  if (isReadOnlyRole()) {
    toast("Роль read-only: проверка запрещена", "error");
    return;
  }
  const fileInput = $("#checkFile");
  const groupSel = $("#checkGroup");
  const btn = $("#checkSubmit");
  const msg = $("#checkMsg");
  const box = $("#checkResult");
  setFieldError(fileInput, "");
  setFieldError(groupSel, "");
  const file = fileInput?.files?.[0];
  if (!file) {
    setFieldError(fileInput, "Выберите ZIP-архив с TData.");
    return;
  }
  if (!/\.zip$/i.test(file.name)) {
    setFieldError(fileInput, "Нужен именно .zip архив.");
    return;
  }
  const opt = groupSel?.selectedOptions?.[0];
  const gid = Number(groupSel?.value || 0);
  if (!gid) {
    setFieldError(groupSel, "Выберите check-пул.");
    return;
  }
  if ((opt?.dataset?.purpose || "").toUpperCase() !== "TDATA_CHECK") {
    setFieldError(groupSel, "Для проверки нужен пул с purpose=TDATA_CHECK (runtime-пул запрещён API).");
    return;
  }
  setBusy(btn, true, "Проверяется…");
  msg.textContent = "Загрузка и проверка выполняются, это может занять время…";
  msg.className = "text-xs text-slate-400";
  box.innerHTML = `
    <div class="card text-sm">
      <div class="flex items-center gap-3 mb-2">
        <span class="text-slate-200">Проверка выполняется…</span>
        <span class="text-xs text-slate-500">POST /business/tdata/check (sync, bounded)</span>
      </div>
      <div class="check-progress"><div style="width:45%"></div></div>
    </div>`;
  try {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("group_id", String(gid));
    const res = await fetch(API + "/business/tdata/check", {
      method: "POST",
      headers: { Authorization: `Bearer ${state.token}` },
      body: fd,
    });
    if (res.status === 401 || res.status === 403) {
      paintCheckForbidden(box, res.status);
      msg.textContent = `Доступ запрещён (HTTP ${res.status}).`;
      msg.className = "text-xs text-rose-400";
      return;
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    paintCheckRun(box, data);
    loadTdataCheckHistory();
    msg.textContent = `Готово: ok=${data.ok_count} failed=${data.failed_count} (run ${data.run_id}).`;
    msg.className = "text-xs text-emerald-300";
  } catch (e) {
    box.innerHTML = `<div class="card text-sm"><div class="text-rose-300 font-medium mb-1">ⓘ Ошибка проверки (server error)</div><div class="text-slate-400">${escapeHTML(e.message)}</div></div>`;
    msg.textContent = e.message;
    msg.className = "text-xs text-rose-400";
  } finally {
    setBusy(btn, false);
  }
}

async function onTdataSessionCheckSubmit(ev) {
  ev.preventDefault();
  if (isReadOnlyRole()) {
    toast("Роль read-only: проверка запрещена", "error");
    return;
  }
  const fileInput = $("#sessionCheckFile");
  const groupSel = $("#checkGroup");
  const btn = $("#sessionCheckSubmit");
  const msg = $("#sessionCheckMsg");
  const box = $("#checkResult");
  setFieldError(fileInput, "");
  setFieldError(groupSel, "");
  const file = fileInput?.files?.[0];
  if (!file) {
    setFieldError(fileInput, "Выберите .session файл.");
    return;
  }
  if (!/\.session$/i.test(file.name)) {
    setFieldError(fileInput, "Нужен именно файл с расширением .session.");
    return;
  }
  if (file.size > 16 * 1024 * 1024) {
    setFieldError(fileInput, "Размер .session файла не должен превышать 16 МиБ.");
    return;
  }
  const opt = groupSel?.selectedOptions?.[0];
  const gid = Number(groupSel?.value || 0);
  if (!gid) {
    setFieldError(groupSel, "Выберите check-пул.");
    return;
  }
  if ((opt?.dataset?.purpose || "").toUpperCase() !== "TDATA_CHECK") {
    setFieldError(groupSel, "Для проверки нужен пул с purpose=TDATA_CHECK (runtime-пул запрещён API).");
    return;
  }
  setBusy(btn, true, "Проверяется…");
  msg.textContent = "Загрузка и проверка выполняются…";
  msg.className = "text-xs text-slate-400";
  box.innerHTML = `<div class="card text-sm"><span class="text-slate-200">Проверка .session выполняется…</span><div class="check-progress mt-2"><div style="width:45%"></div></div></div>`;
  try {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("group_id", String(gid));
    const res = await fetch(API + "/business/tdata/check-session", {
      method: "POST",
      headers: { Authorization: `Bearer ${state.token}` },
      body: fd,
    });
    if (res.status === 401 || res.status === 403) {
      paintCheckForbidden(box, res.status);
      msg.textContent = `Доступ запрещён (HTTP ${res.status}).`;
      msg.className = "text-xs text-rose-400";
      return;
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    paintCheckRun(box, data);
    await loadTdataCheckHistory();
    msg.textContent = `Готово: ok=${data.ok_count} failed=${data.failed_count} (run ${data.run_id}).`;
    msg.className = "text-xs text-emerald-300";
  } catch (e) {
    const message = String(e?.message || "Ошибка проверки");
    box.innerHTML = `<div class="card text-sm"><div class="text-rose-300 font-medium mb-1">ⓘ Ошибка проверки (server error)</div><div class="text-slate-400">${escapeHTML(message)}</div></div>`;
    msg.textContent = message;
    msg.className = "text-xs text-rose-400";
  } finally {
    setBusy(btn, false);
  }
}

function paintCheckForbidden(box, status) {
  box.innerHTML = `
    <div class="card text-sm">
      <div class="text-amber-300 font-medium mb-1">ⓘ Forbidden (HTTP ${status})</div>
      <div class="text-slate-400">Недостаточно прав для запуска проверки. Нужна роль operator (write), read-only недостаточно.</div>
    </div>`;
}

async function refreshCheckRun(runId, btn) {
  const box = $("#checkResult");
  if (!box || !runId) return;
  setBusy(btn, true, "…");
  try {
    const data = await api(`/business/tdata/check/${encodeURIComponent(runId)}`);
    paintCheckRun(box, data);
    toast(`Run ${runId}: ok=${data.ok_count} failed=${data.failed_count}`, "info");
  } catch (e) {
    toast(`Обновление run: ${e.message}`, "error");
  } finally {
    setBusy(btn, false);
  }
}

function paintCheckRun(box, data) {
  const items = data.items || [];
  const hasRunError = ["failed", "interrupted"].includes(String(data.status || "").toLowerCase()) || Boolean(data.error_code);
  const isEmpty = !hasRunError && (data.total || 0) === 0 && !items.length;
  const isPartial = (data.ok_count || 0) > 0 && (data.failed_count || 0) > 0;
  const verdict = hasRunError
    ? `<span class="pill pill-red">${String(data.status || "").toLowerCase() === "interrupted" ? "interrupted" : "error"}</span>`
    : isEmpty
    ? `<span class="pill pill-gray">empty — tdata не найдены</span>`
    : isPartial
      ? `<span class="pill pill-amber">partial — часть не прошла</span>`
      : (data.failed_count || 0) > 0
        ? `<span class="pill pill-red">failed</span>`
        : `<span class="pill pill-green">success</span>`;
  box.innerHTML = `
    <div class="card">
      <div class="flex items-center gap-3 flex-wrap mb-3">
        <h3 class="font-semibold">Результат проверки</h3>
        ${verdict}
        ${data.truncated ? `<span class="pill pill-amber">truncated — показан лимит</span>` : ""}
        <button id="checkRefresh" class="btn btn-ghost ml-auto" data-run="${escapeHTML(data.run_id || "")}">⟳ Обновить (GET run)</button>
      </div>
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-3">
        ${kpi("Всего папок", "ckTotal", String(data.total ?? 0), `run ${escapeHTML((data.run_id || "").slice(0, 8))}…`)}
        ${kpi("Годных", "ckOk", String(data.ok_count ?? 0), "", "text-emerald-300")}
        ${kpi("Не прошло", "ckFail", String(data.failed_count ?? 0), "", "text-rose-300")}
        ${kpi("Пул", "ckPool", `#${data.check_group_id ?? "—"}`, escapeHTML(data.requested_by ? `запустил ${data.requested_by}` : ""))}
      </div>
      ${data.error_code ? `<div class="text-sm text-amber-300 mb-3">ⓘ ${escapeHTML(data.error_code)}${data.error_detail ? ` — ${escapeHTML(data.error_detail)}` : ""}</div>` : ""}
      ${isEmpty
        ? `<div class="text-sm text-slate-500">В архиве нет пригодных TData-папок. Проверьте структуру ZIP (папка tdata: settings + key_datas + sessions).</div>`
        : `<div class="table-scroll"><table class="cb-table text-xs">
            <thead><tr><th>Папка</th><th>Статус</th><th>Профиль</th><th>Прокси</th><th>Ошибка</th></tr></thead>
            <tbody>
              ${items.map(it => `
                <tr>
                  <td class="font-mono max-w-[220px] truncate" title="${escapeHTML(it.relpath || it.item_id || "")}">${escapeHTML(it.relpath || it.item_id || "—")}</td>
                  <td>${tdataStatusPill(it.status)}</td>
                  <td>${it.phone ? escapeHTML(it.phone) : "—"}${it.username ? ` @${escapeHTML(it.username)}` : ""}${it.retry_after ? ` <span class="text-slate-500">(retry ${it.retry_after}с)</span>` : ""}</td>
                  <td class="text-slate-400">${it.proxy_label ? escapeHTML(it.proxy_label) : it.proxy_id ? `#${it.proxy_id}` : "—"}</td>
                  <td class="text-rose-300 max-w-[260px] truncate" title="${escapeHTML([it.error_code, it.error_detail].filter(Boolean).join(" — ") || "")}">${escapeHTML(it.error_code || "")}${it.error_detail ? ` <span class="text-slate-500">${escapeHTML(it.error_detail.slice(0, 80))}</span>` : ""}</td>
                </tr>`).join("")}
            </tbody>
          </table></div>`}
    </div>`;
  $("#checkRefresh")?.addEventListener("click", (ev) => {
    refreshCheckRun(ev.currentTarget.dataset.run, ev.currentTarget);
  });
}

/* ======================= TData ZIP Import ======================= */

async function loadTdataProxyGroups() {
  const sel = $("#tdataProxyGroup");
  if (!sel) return;
  try {
    const groups = await api("/business/proxy-groups");
    const runtime = (groups || []).filter(g =>
      (g.purpose || "ACCOUNT_RUNTIME").toUpperCase() === "ACCOUNT_RUNTIME" && g.proxies_count > 0
    );
    sel.innerHTML = `<option value="">— выберите региональный пул —</option>` + runtime.map(g =>
      `<option value="${g.id}">${escapeHTML(g.name)} (${g.proxies_count} прокси)</option>`
    ).join("");
    if (!runtime.length) $("#tdataMsg").textContent = "Сначала создайте пул ACCOUNT_RUNTIME с SOCKS5 прокси в разделе Прокси.";
  } catch (e) {
    sel.innerHTML = `<option value="">Не удалось загрузить пулы</option>`;
    $("#tdataMsg").textContent = e.message;
  }
}

async function uploadTdataZip(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".zip")) {
    toast("Нужен ZIP-архив с TData", "error");
    return;
  }
  const groupId = Number($("#tdataProxyGroup")?.value || 0);
  if (!Number.isSafeInteger(groupId) || groupId <= 0) {
    toast("Выберите пул SOCKS5 для новых аккаунтов", "error");
    return;
  }
  const btn = $("#tdataUploadBtn");
  const msg = $("#tdataMsg");
  if (btn) { btn.disabled = true; btn.textContent = "Импорт…"; }
  if (msg) { msg.textContent = "Конвертация выполняется, это может занять время…"; msg.className = "text-xs text-slate-400"; }
  try {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("group_id", String(groupId));
    const token = state.token;
    const res = await fetch("/business/tdata/import", {
      method: "POST",
      headers: token ? { Authorization: "Bearer " + token } : {},
      body: fd,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Ошибка импорта");
    const line = `Готово: ${data.converted}/${data.total} сконвертировано` + (data.failed ? `, ошибок: ${data.failed}` : "");
    if (msg) { msg.textContent = line; msg.className = "text-xs " + (data.failed ? "text-amber-300" : "text-emerald-300"); }
    toast(line, data.failed ? "warn" : "success", 5000);
    await refreshAccountsTable();
  } catch (e) {
    if (msg) { msg.textContent = e.message; msg.className = "text-xs text-rose-400"; }
    toast("Импорт TData: " + e.message, "error", 5000);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Загрузить ZIP"; }
    const inp = $("#tdataFileInput");
    if (inp) inp.value = "";
  }
}

function bindTdataZone() {
  const zone = $("#tdataDropZone");
  const input = $("#tdataFileInput");
  const btn = $("#tdataUploadBtn");
  if (!zone || !input || !btn) return;

  zone.addEventListener("click", () => input.click());
  ["dragenter", "dragover"].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.remove("dragover"); }));
  zone.addEventListener("drop", e => { const f = e.dataTransfer?.files?.[0]; if (f) uploadTdataZip(f); });
  input.addEventListener("change", () => { const f = input.files?.[0]; if (f) uploadTdataZip(f); });
  btn.addEventListener("click", () => input.click());
}

function bootstrap() {
  try {
    const splash = document.getElementById("bootSplash");
    if (splash) splash.style.display = "none";

    $("#loginForm").addEventListener("submit", loginFlow);
    $("#logoutBtn").addEventListener("click", () => handleLogout(false));
    $("#globalRefreshBtn").addEventListener("click", () => navigate(window.location.hash));
    $("#navToggle")?.addEventListener("click", () => {
      const open = document.body.classList.toggle("nav-open");
      $("#navToggle").setAttribute("aria-expanded", String(open));
    });
    $("#navBackdrop")?.addEventListener("click", () => {
      document.body.classList.remove("nav-open");
      $("#navToggle")?.setAttribute("aria-expanded", "false");
    });
    document.addEventListener("keydown", (e) => {
      unlockDialogAudio();
      if (e.key === "Escape") {
        document.body.classList.remove("nav-open");
        $("#navToggle")?.setAttribute("aria-expanded", "false");
      }
    });
    document.addEventListener("pointerdown", unlockDialogAudio);
    bindTdataZone();

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
