/* ================================================================
   CoreBot Control Panel вЂ” vanilla SPA
   - РҐРµС€-СЂРѕСѓС‚РёРЅРі (Dashboard / Accounts / Dialogs / Logs / Settings)
   - JWT РІ localStorage, РїРµСЂРµР·Р°РїСЂРѕСЃ С‚РѕРєРµРЅР° РЅРµ СЂРµР°Р»РёР·РѕРІР°РЅ (РїСЂРѕСЃС‚Р°СЏ login-С„РѕСЂРјР°)
   - SSE-РєР°РЅР°Р» РґР»СЏ live-СЃРѕРѕР±С‰РµРЅРёР№ РёР· corebot.db
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
    selectedGroupName: "Р’СЃРµ",
    groupAccountIds: null, // Set<int> | null
  },
  accounts: {
    q: "",
    sort: "id_desc",
  },
  listeners: {},
  parsing: { tab: "channels" },
};

function safeJSON(s) { try { return s ? JSON.parse(s) : null; } catch { return null; } }
function fmtDate(s) {
  if (!s) return "вЂ”";
  try {
    const d = (s instanceof Date) ? s : new Date(s);
    if (Number.isNaN(d.getTime())) return s;
    return d.toLocaleString("ru-RU", { hour12: false });
  } catch { return s; }
}
function fmtRelative(s) {
  if (!s) return "вЂ”";
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return s;
  const diff = Math.floor((Date.now() - d.getTime()) / 1000);
  if (diff < 60) return `${diff} СЃ РЅР°Р·Р°Рґ`;
  if (diff < 3600) return `${Math.floor(diff / 60)} РјРёРЅ РЅР°Р·Р°Рґ`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} С‡ РЅР°Р·Р°Рґ`;
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
      msg.textContent = r.status === 401 ? "РќРµРІРµСЂРЅС‹Р№ Р»РѕРіРёРЅ РёР»Рё РїР°СЂРѕР»СЊ" : `РћС€РёР±РєР° РІС…РѕРґР°: HTTP ${r.status}`;
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
    msg.textContent = "РћС€РёР±РєР° СЃРµС‚Рё";
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
  if (!silent) toast("РЎРµСЃСЃРёСЏ Р·Р°РІРµСЂС€РµРЅР°");
}

async function enterApp() {
  const app = $("#appShell");
  const lg = $("#loginScreen");
  // Р“Р°СЃРёРј СЌРєСЂР°РЅ Р»РѕРіРёРЅР° Рё РµРіРѕ inline style="display:flex" вЂ” РёРЅР°С‡Рµ РѕРЅ
  // РѕСЃС‚Р°С‘С‚СЃСЏ РІ РїРѕС‚РѕРєРµ Рё РІРёРґРµРЅ РїСЂРё РїСЂРѕРєСЂСѓС‚РєРµ РЅР°Рґ РїСЂРёР»РѕР¶РµРЅРёРµРј.
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
  $("#userBadge").textContent = (state.user?.username || "вЂ”") + roleSuffix;
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
    // Р•СЃР»Рё СЌС‚Рѕ assistant вЂ” РѕРЅ РјРѕРі СЃРѕРѕС‚РІРµС‚СЃС‚РІРѕРІР°С‚СЊ СЃС‚СЂРѕРєРµ РѕС‡РµСЂРµРґРё.
    // РџРµСЂРµСЂРёСЃРѕРІС‹РІР°РµРј РґРёР°Р»РѕРі С†РµР»РёРєРѕРј, С‡С‚РѕР±С‹ СѓР±СЂР°С‚СЊ placeholder РёР· outbound_queue.
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
    case "parsing":   return renderParsing(segments[1]);
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
  setHeader("РћС‡РµСЂРµРґСЊ", "Р СѓС‡РЅС‹Рµ РёСЃС…РѕРґСЏС‰РёРµ РёР· РІРµР±Р° (outbound_queue)");
  const readOnly = isReadOnlyRole();
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full space-y-4">
      <div class="card">
        <div class="flex items-center gap-3 text-sm flex-wrap">
          <label class="text-slate-400">РЎС‚Р°С‚СѓСЃ:</label>
          <select id="qStatus" class="bg-ink-800 border border-ink-600 rounded-md px-2 py-1 text-slate-100">
            <option value="">РІСЃРµ</option>
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
          <button id="qApply" class="px-3 py-1 rounded bg-accent-600 hover:bg-accent-500 text-white">РџСЂРёРјРµРЅРёС‚СЊ</button>
          <button id="qBulkRetry" class="px-3 py-1 rounded bg-emerald-700 hover:bg-emerald-600 text-white ${readOnly ? "opacity-50 cursor-not-allowed" : ""}" ${readOnly ? "disabled" : ""}>в†» Retry РІС‹Р±СЂР°РЅРЅС‹С…</button>
          <button id="qBulkCancel" class="px-3 py-1 rounded bg-rose-700 hover:bg-rose-600 text-white ${readOnly ? "opacity-50 cursor-not-allowed" : ""}" ${readOnly ? "disabled" : ""}>вњ• Cancel РІС‹Р±СЂР°РЅРЅС‹С…</button>
          <span id="qCount" class="text-xs text-slate-500 ml-auto"></span>
        </div>
      </div>
      <div class="card p-0 overflow-hidden">
        <table class="cb-table">
          <thead>
            <tr>
              <th><input id="qSelectAll" type="checkbox" class="rounded border-ink-600 bg-ink-800" ${readOnly ? "disabled" : ""} /></th>
              <th>ID</th><th>РђРєРєР°СѓРЅС‚</th><th>Peer</th><th>РўРµРєСЃС‚</th><th>РЎС‚Р°С‚СѓСЃ</th><th>Attempts</th><th>РћС€РёР±РєР°</th><th>Р’СЂРµРјСЏ</th><th></th>
            </tr>
          </thead>
          <tbody id="qBody"><tr><td colspan="10" class="text-center text-slate-500 py-8">Р—Р°РіСЂСѓР·РєР°вЂ¦</td></tr></tbody>
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
    $("#qCount").textContent = `РЅР°Р№РґРµРЅРѕ: ${list.length}`;
    if (!list.length) {
      tbody.innerHTML = `<tr><td colspan="10" class="text-center text-slate-500 py-8">РћС‡РµСЂРµРґСЊ РїСѓСЃС‚Р° РїРѕ С„РёР»СЊС‚СЂСѓ.</td></tr>`;
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
        <td class="max-w-[260px] truncate text-xs text-rose-300" title="${escapeHTML(q.error || "")}">${escapeHTML(q.error || "вЂ”")}</td>
        <td class="text-xs text-slate-400">${fmtDate(q.created_at)}</td>
        <td class="text-right whitespace-nowrap">
          <button data-q-act="retry" data-qid="${q.queue_id}" class="px-2 py-1 rounded bg-emerald-700 hover:bg-emerald-600 text-xs ${readOnly ? "opacity-50 cursor-not-allowed" : ""}" ${readOnly ? "disabled" : ""}>в†»</button>
          <button data-q-act="cancel" data-qid="${q.queue_id}" class="px-2 py-1 rounded bg-rose-700 hover:bg-rose-600 text-xs ml-1 ${readOnly ? "opacity-50 cursor-not-allowed" : ""}" ${readOnly ? "disabled" : ""}>вњ•</button>
        </td>
      </tr>
    `).join("");
    if (!readOnly) {
      tbody.querySelectorAll("button[data-q-act]").forEach((b) => {
        b.addEventListener("click", () => onQueueItemAction(Number(b.dataset.qid), b.dataset.qAct));
      });
    }
  } catch (e) {
    toast(`РћС‡РµСЂРµРґСЊ: ${e.message}`, "error");
  }
}

async function onQueueItemAction(queueId, action) {
  if (!queueId || !action) return;
  if (isReadOnlyRole()) {
    toast("Р РѕР»СЊ read-only: РґРµР№СЃС‚РІРёРµ Р·Р°РїСЂРµС‰РµРЅРѕ", "error");
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
    toast(`РћС‡РµСЂРµРґСЊ ${action}: ${e.message}`, "error");
  }
}

async function runQueueBulk(action) {
  if (isReadOnlyRole()) {
    toast("Р РѕР»СЊ read-only: РјР°СЃСЃРѕРІС‹Рµ РґРµР№СЃС‚РІРёСЏ Р·Р°РїСЂРµС‰РµРЅС‹", "error");
    return;
  }
  const ids = $$("#qBody input[type='checkbox'][data-qid]:checked")
    .map((c) => Number(c.dataset.qid))
    .filter((n) => Number.isFinite(n) && n > 0);
  if (!ids.length) {
    toast("РћС‚РјРµС‚СЊС‚Рµ СЌР»РµРјРµРЅС‚С‹ РѕС‡РµСЂРµРґРё", "info");
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

/* ------------------------------ Parsing (Telegram) ---------------------- */

function _parsingTabValid(t) {
  return t === "channels" || t === "groups" || t === "users" ? t : "channels";
}

async function downloadParsingExport(path) {
  const url = path.startsWith("http") ? path : API + path;
  const r = await fetch(url, {
    headers: { Authorization: `Bearer ${state.token}`, Accept: "text/plain,*/*" },
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

async function renderParsing(tabSeg) {
  try { state.parsing.stream?.close(); } catch {}
  state.parsing.stream = null;
  const tab = _parsingTabValid(tabSeg || state.parsing.tab);
  state.parsing.tab = tab;
  setHeader("РџР°СЂСЃРёРЅРі", "Р—Р°РґР°С‡Рё Telethon (parser-worker) вЂ” РєР°РЅР°Р»С‹, РіСЂСѓРїРїС‹, РїРѕР»СЊР·РѕРІР°С‚РµР»Рё");
  const readOnly = isReadOnlyRole();
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full space-y-4">
      <div class="flex flex-wrap gap-2 text-sm">
        <button data-ptab="channels" class="px-3 py-1.5 rounded-lg border ${tab === "channels" ? "bg-accent-600 border-accent-500 text-white" : "bg-ink-800 border-ink-600 text-slate-200"}">РљР°РЅР°Р»С‹</button>
        <button data-ptab="groups" class="px-3 py-1.5 rounded-lg border ${tab === "groups" ? "bg-accent-600 border-accent-500 text-white" : "bg-ink-800 border-ink-600 text-slate-200"}">Р“СЂСѓРїРїС‹</button>
        <button data-ptab="users" class="px-3 py-1.5 rounded-lg border ${tab === "users" ? "bg-accent-600 border-accent-500 text-white" : "bg-ink-800 border-ink-600 text-slate-200"}">РџРѕР»СЊР·РѕРІР°С‚РµР»Рё</button>
        <span class="text-xs text-slate-500 ml-auto self-center">РџР°СЂСЃРµСЂ Р·Р°РїСѓСЃРєР°РµС‚СЃСЏ РІРјРµСЃС‚Рµ СЃ РІРµР±-РїР°РЅРµР»СЊСЋ</span>
      </div>

      <div id="pFormCard" class="card space-y-3 text-sm"></div>

      <div class="card">
        <div class="flex items-center justify-between mb-2">
          <h3 class="font-semibold text-white">Р—Р°РґР°С‡Рё</h3>
          <span class="text-xs text-emerald-400">live</span>
        </div>
        <div class="overflow-x-auto">
          <table class="cb-table text-xs">
            <thead>
              <tr>
                <th>ID</th><th>РўРёРї</th><th>РЎС‚Р°С‚СѓСЃ</th><th>%</th><th>Р­С‚Р°Рї</th><th>РђРєРє</th><th>Р—Р°РїСЂРѕСЃ</th><th>Found</th><th>Filt</th><th>Err</th><th>РЎРѕР·РґР°РЅР°</th><th></th>
              </tr>
            </thead>
            <tbody id="pTaskBody"><tr><td colspan="12" class="text-center text-slate-500 py-6">Р—Р°РіСЂСѓР·РєР°вЂ¦</td></tr></tbody>
          </table>
        </div>
      </div>

      <div class="card" id="pDetailCard" style="display:none">
        <div class="flex flex-wrap items-center gap-2 mb-2">
          <h3 class="font-semibold text-white">Р—Р°РґР°С‡Р° #<span id="pDetailId">вЂ”</span></h3>
          <button id="pCancelTask" class="text-xs px-2 py-1 rounded bg-rose-800 border border-rose-600 ${readOnly ? "opacity-50 cursor-not-allowed" : ""}" ${readOnly ? "disabled" : ""}>РћС‚РјРµРЅРёС‚СЊ</button>
          <div class="ml-auto flex flex-wrap gap-2 text-xs">
            <button type="button" data-pex="channels" class="px-2 py-1 rounded bg-ink-700 border border-ink-600">export channels.txt</button>
            <button type="button" data-pex="groups" class="px-2 py-1 rounded bg-ink-700 border border-ink-600">export groups.txt</button>
            <button type="button" data-pex="users" class="px-2 py-1 rounded bg-ink-700 border border-ink-600">export users.txt</button>
          </div>
        </div>
        <pre id="pLogs" class="text-xs bg-ink-950 border border-ink-700 rounded p-3 max-h-64 overflow-y-auto text-slate-300 whitespace-pre-wrap"></pre>
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

  if (tab === "channels" || tab === "groups") {
    formCard.innerHTML = `
      <div class="font-medium text-slate-200">${tab === "channels" ? "РљР°РЅР°Р»С‹" : "Р“СЂСѓРїРїС‹"}: РЅРѕРІР°СЏ Р·Р°РґР°С‡Р°</div>
      <div class="grid md:grid-cols-2 gap-3 text-xs">
        <label class="block">РљР»СЋС‡РµРІС‹Рµ СЃР»РѕРІР° (РїРѕ РѕРґРЅРѕРјСѓ РІ СЃС‚СЂРѕРєРµ)
          <textarea id="pKeywords" rows="4" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100 font-mono" placeholder="crypto&#10;defi"></textarea>
        </label>
        <label class="block">РћРєРѕРЅС‡Р°РЅРёСЏ (РїРѕ РѕРґРЅРѕРјСѓ РІ СЃС‚СЂРѕРєРµ)
          <textarea id="pEndings" rows="4" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100 font-mono" placeholder="news&#10;chat&#10;channel"></textarea>
        </label>
      </div>
      <div class="grid md:grid-cols-3 gap-3 text-xs">
        <label class="block">Depth (1вЂ“3)
          <select id="pDepth" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100">
            <option value="1">1</option><option value="2">2</option><option value="3">3</option>
          </select>
        </label>
        <label class="block">Р РµР¶РёРј Р·Р°РґР°С‡Рё
          <select id="pMode" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100">
            <option value="max_coverage">max_coverage</option>
            <option value="active_only">active_only</option>
          </select>
        </label>
        <label class="block flex items-end gap-2">
          <input id="pExpandedSearch" type="checkbox" class="rounded border-ink-600 bg-ink-800" />
          <span>Р Р°СЃС€РёСЂРµРЅРЅС‹Р№ РїРѕРёСЃРє</span>
        </label>
      </div>
      <div class="grid md:grid-cols-3 gap-3 text-xs">
        <label class="inline-flex items-center gap-2"><input id="fActive7d" type="checkbox" class="rounded border-ink-600 bg-ink-800" /> РўРѕР»СЊРєРѕ Р°РєС‚РёРІРЅС‹Рµ (в‰Ґ1 РїРѕСЃС‚/7Рґ)</label>
        <label class="inline-flex items-center gap-2"><input id="fDiscussion" type="checkbox" class="rounded border-ink-600 bg-ink-800" /> РўРѕР»СЊРєРѕ РѕС‚РєСЂС‹С‚С‹Рµ РєРѕРјРјРµРЅС‚Р°СЂРёРё</label>
        <label class="inline-flex items-center gap-2"><input id="fPublic" type="checkbox" class="rounded border-ink-600 bg-ink-800" /> РўРѕР»СЊРєРѕ РѕС‚РєСЂС‹С‚С‹Рµ РєР°РЅР°Р»С‹</label>
        <label class="block">РџРѕРґРїРёСЃС‡РёРєРё min
          <input id="fSubsMin" type="number" min="0" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100" />
        </label>
        <label class="block">РџРѕРґРїРёСЃС‡РёРєРё max
          <input id="fSubsMax" type="number" min="0" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100" />
        </label>
        <label class="block">РЇР·С‹Рє
          <select id="fLang" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100">
            <option value="">Р»СЋР±Р°СЏ</option><option value="ru">ru</option><option value="en">en</option>
          </select>
        </label>
      </div>
      <div><span class="text-slate-400">РђРєРєР°СѓРЅС‚С‹:</span><div class="mt-1 flex flex-wrap">${accOpts || "<span class='text-slate-500'>РЅРµС‚ Р°РєРєР°СѓРЅС‚РѕРІ</span>"}</div></div>
      <button id="pSubmit" class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white text-sm ${readOnly ? "opacity-50 cursor-not-allowed" : ""}" ${readOnly ? "disabled" : ""}>Р—Р°РїСѓСЃС‚РёС‚СЊ</button>
    `;
    $("#pSubmit")?.addEventListener("click", async () => {
      if (readOnly) return;
      const ids = $$(".p-acc:checked").map((c) => Number(c.value)).filter((n) => n > 0);
      if (!ids.length) {
        toast("Р’С‹Р±РµСЂРёС‚Рµ С…РѕС‚СЏ Р±С‹ РѕРґРёРЅ Р°РєРєР°СѓРЅС‚", "error");
        return;
      }
      const keywords = ($("#pKeywords")?.value || "").split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
      if (!keywords.length) {
        toast("Р”РѕР±Р°РІСЊС‚Рµ С…РѕС‚СЏ Р±С‹ РѕРґРЅРѕ РєР»СЋС‡РµРІРѕРµ СЃР»РѕРІРѕ", "error");
        return;
      }
      const endings = ($("#pEndings")?.value || "").split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
      const depth = Number($("#pDepth")?.value || "1");
      const mode = ($("#pMode")?.value || "max_coverage").trim();
      const filters = {
        is_active_7d: $("#fActive7d")?.checked ? true : null,
        has_discussion: $("#fDiscussion")?.checked ? true : null,
        is_public: $("#fPublic")?.checked ? true : null,
        subscribers_min: ($("#fSubsMin")?.value || "").trim() ? Number($("#fSubsMin")?.value || 0) : null,
        subscribers_max: ($("#fSubsMax")?.value || "").trim() ? Number($("#fSubsMax")?.value || 0) : null,
        lang: ($("#fLang")?.value || "").trim() || null,
      };
      try {
        await api("/business/parsing/tasks", {
          method: "POST",
          body: {
            kind: tab,
            account_ids: ids,
            depth,
            mode,
            params: {
              keywords,
              endings,
              expanded_search: !!$("#pExpandedSearch")?.checked,
              filters,
            },
          },
        });
        toast("Р—Р°РґР°С‡Р° СЃРѕР·РґР°РЅР°", "success");
        await loadParsingTasks();
      } catch (e) {
        toast(e.message, "error");
      }
    });
  } else {
    formCard.innerHTML = `
      <div class="font-medium text-slate-200">РџРѕР»СЊР·РѕРІР°С‚РµР»Рё: РЅРѕРІР°СЏ Р·Р°РґР°С‡Р°</div>
      <label class="block text-xs">Р’РІРѕРґ РёСЃС‚РѕС‡РЅРёРєРѕРІ (@username РёР»Рё t.me СЃСЃС‹Р»РєРё), РїРѕ РѕРґРЅРѕР№ СЃС‚СЂРѕРєРµ
        <textarea id="pUserInputs" rows="5" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100 font-mono text-xs"
        placeholder="@group_one&#10;https://t.me/channel_one"></textarea>
      </label>
      <label class="block text-xs">Р—Р°РіСЂСѓР·РєР° .txt
        <input id="pUsersTxtFile" type="file" accept=".txt,text/plain" class="mt-1 block w-full text-slate-300 text-xs" />
      </label>
      <div class="grid md:grid-cols-2 gap-3 text-xs">
        <div class="space-y-2 border border-ink-700 rounded p-2">
          <div class="text-slate-300">РСЃС‚РѕС‡РЅРёРєРё (РіСЂСѓРїРїС‹)</div>
          <label class="inline-flex items-center gap-2"><input id="srcGroupMembers" type="checkbox" class="rounded border-ink-600 bg-ink-800" checked /> СѓС‡Р°СЃС‚РЅРёРєРё</label>
          <label class="inline-flex items-center gap-2"><input id="srcGroupActive" type="checkbox" class="rounded border-ink-600 bg-ink-800" checked /> Р°РєС‚РёРІРЅС‹Рµ (РїРёСЃР°Р»Рё)</label>
        </div>
        <div class="space-y-2 border border-ink-700 rounded p-2">
          <div class="text-slate-300">РСЃС‚РѕС‡РЅРёРєРё (РєР°РЅР°Р»С‹)</div>
          <label class="inline-flex items-center gap-2"><input id="srcChanCommenters" type="checkbox" class="rounded border-ink-600 bg-ink-800" checked /> РєРѕРјРјРµРЅС‚Р°С‚РѕСЂС‹</label>
          <label class="inline-flex items-center gap-2"><input id="srcChanActiveDiscussion" type="checkbox" class="rounded border-ink-600 bg-ink-800" checked /> Р°РєС‚РёРІРЅС‹Рµ (РµСЃР»Рё РµСЃС‚СЊ С‡Р°С‚)</label>
        </div>
      </div>
      <div class="grid md:grid-cols-3 gap-3 text-xs">
        <label class="block">Р РµР¶РёРј Р·Р°РґР°С‡Рё
          <select id="pUserMode" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100">
            <option value="max_coverage">max_coverage</option>
            <option value="active_only">active_only</option>
          </select>
        </label>
        <label class="inline-flex items-center gap-2"><input id="ufUsername" type="checkbox" class="rounded border-ink-600 bg-ink-800" /> С‚РѕР»СЊРєРѕ СЃ username</label>
        <label class="inline-flex items-center gap-2"><input id="ufAvatar" type="checkbox" class="rounded border-ink-600 bg-ink-800" /> С‚РѕР»СЊРєРѕ СЃ Р°РІР°С‚Р°СЂРѕРј</label>
        <label class="inline-flex items-center gap-2"><input id="ufRecentOnline" type="checkbox" class="rounded border-ink-600 bg-ink-800" /> С‚РѕР»СЊРєРѕ РЅРµРґР°РІРЅРѕ РѕРЅР»Р°Р№РЅ</label>
        <label class="inline-flex items-center gap-2"><input id="ufAntiBot" type="checkbox" class="rounded border-ink-600 bg-ink-800" checked /> Р°РЅС‚Рё-Р±РѕС‚ (СѓРґР°Р»С‘РЅРЅС‹Рµ/РїСѓСЃС‚С‹Рµ)</label>
        <label class="block">РЇР·С‹Рє
          <select id="ufLang" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded px-2 py-1 text-slate-100">
            <option value="">Р»СЋР±РѕР№</option><option value="ru">ru</option><option value="en">en</option>
          </select>
        </label>
      </div>
      <div><span class="text-slate-400">РђРєРєР°СѓРЅС‚С‹:</span><div class="mt-1 flex flex-wrap">${accOpts || "<span class='text-slate-500'>РЅРµС‚ Р°РєРєР°СѓРЅС‚РѕРІ</span>"}</div></div>
      <button id="pSubmitUsers" class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white text-sm ${readOnly ? "opacity-50 cursor-not-allowed" : ""}" ${readOnly ? "disabled" : ""}>Р—Р°РїСѓСЃС‚РёС‚СЊ</button>
    `;
    let txtUploaded = "";
    $("#pUsersTxtFile")?.addEventListener("change", async (ev) => {
      const f = ev?.target?.files?.[0];
      if (!f) return;
      try {
        txtUploaded = await f.text();
        toast(`Р—Р°РіСЂСѓР¶РµРЅ ${f.name}`, "success");
      } catch {
        toast("РќРµ СѓРґР°Р»РѕСЃСЊ РїСЂРѕС‡РёС‚Р°С‚СЊ С„Р°Р№Р»", "error");
      }
    });
    $("#pSubmitUsers")?.addEventListener("click", async () => {
      if (readOnly) return;
      const ids = $$(".p-acc:checked").map((c) => Number(c.value)).filter((n) => n > 0);
      if (!ids.length) {
        toast("Р’С‹Р±РµСЂРёС‚Рµ С…РѕС‚СЏ Р±С‹ РѕРґРёРЅ Р°РєРєР°СѓРЅС‚", "error");
        return;
      }
      const manualText = [($("#pUserInputs")?.value || "").trim(), txtUploaded.trim()].filter(Boolean).join("\n");
      if (!manualText) {
        toast("Р”РѕР±Р°РІСЊС‚Рµ СЃРїРёСЃРѕРє РёСЃС‚РѕС‡РЅРёРєРѕРІ РІСЂСѓС‡РЅСѓСЋ РёР»Рё С‡РµСЂРµР· txt", "error");
        return;
      }
      const mode = ($("#pUserMode")?.value || "max_coverage").trim();
      const userFilters = {
        require_username: !!$("#ufUsername")?.checked,
        require_avatar: !!$("#ufAvatar")?.checked,
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
      try {
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
            },
          },
        });
        toast("Р—Р°РґР°С‡Р° СЃРѕР·РґР°РЅР°", "success");
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
        tbody.innerHTML = `<tr><td colspan="12" class="text-center text-slate-500 py-6">РќРµС‚ Р·Р°РґР°С‡</td></tr>`;
        return;
      }
      const stageMap = { search: "РїРѕРёСЃРє", collect: "СЃР±РѕСЂ", filter: "С„РёР»СЊС‚СЂР°С†РёСЏ", done: "Р·Р°РІРµСЂС€РµРЅРѕ" };
      tbody.innerHTML = list.map((t) => `
        <tr class="cursor-pointer hover:bg-ink-800/80" data-pselect="${t.id}">
          <td class="text-slate-400">#${t.id}</td>
          <td>${escapeHTML(t.kind)}</td>
          <td>${escapeHTML(t.status)}</td>
          <td>${t.progress_percent ?? 0}</td>
          <td class="max-w-[140px] truncate" title="${escapeHTML(t.current_stage || "")}">${escapeHTML(stageMap[t.current_stage] || t.current_stage || "вЂ”")}</td>
          <td>${t.current_account_id ?? "вЂ”"}</td>
          <td class="max-w-[200px] truncate" title="${escapeHTML(t.current_query || "")}">${escapeHTML(t.current_query || "вЂ”")}</td>
          <td>${t.found_count ?? 0}</td>
          <td>${t.filtered_count ?? 0}</td>
          <td>${t.error_count ?? 0}</td>
          <td class="text-slate-400">${fmtDate(t.created_at)}</td>
          <td><button data-plogs="${t.id}" class="text-xs text-accent-400 hover:text-accent-300">Р»РѕРіРё</button></td>
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
      toast(`РџР°СЂСЃРёРЅРі: ${e.message}`, "error");
    }
  }

  async function loadParsingLogs(taskId) {
    if (!taskId) return;
    const card = $("#pDetailCard");
    const pre = $("#pLogs");
    $("#pDetailId").textContent = String(taskId);
    card.style.display = "block";
    pre.textContent = "Р—Р°РіСЂСѓР·РєР°вЂ¦";
    try {
      const logs = await api(`/business/parsing/tasks/${taskId}/logs?limit=300`);
      pre.textContent = (logs || []).map((l) =>
        `[${fmtDate(l.created_at)}] ${l.level} ${l.event}: ${l.message || ""}`
      ).join("\n");
    } catch (e) {
      pre.textContent = "РћС€РёР±РєР°: " + e.message;
    }
  }

  $("#pCancelTask")?.addEventListener("click", async () => {
    if (readOnly || !selectedTaskId) return;
    try {
      await api(`/business/parsing/tasks/${selectedTaskId}/cancel`, { method: "POST" });
      toast("РћС‚РјРµРЅР° Р·Р°РїСЂРѕС€РµРЅР°", "success");
      await loadParsingTasks();
    } catch (e) {
      toast(e.message, "error");
    }
  });
  $$("button[data-pex]").forEach((b) => {
    b.addEventListener("click", async () => {
      if (!selectedTaskId) {
        toast("Р’С‹Р±РµСЂРёС‚Рµ Р·Р°РґР°С‡Сѓ (СЃС‚СЂРѕРєР° С‚Р°Р±Р»РёС†С‹)", "info");
        return;
      }
      const kind = b.getAttribute("data-pex");
      const path = `/business/parsing/export/${kind}.txt?task_id=${selectedTaskId}`;
      try {
        await downloadParsingExport(path);
      } catch (e) {
        toast(e.message, "error");
      }
    });
  });

  await loadParsingTasks();

  // Realtime Р±РµР· РґС‘СЂРіР°РЅСЊСЏ UI: СЃРѕР±С‹С‚РёСЏ РїСЂРёС…РѕРґСЏС‚ РїРѕ SSE С‚РѕР»СЊРєРѕ РїСЂРё РёР·РјРµРЅРµРЅРёСЏС….
  try {
    const url = `${API}/business/parsing/stream?token=${encodeURIComponent(state.token)}`;
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
      // РћСЃС‚Р°РІР»СЏРµРј С‚РѕР»СЊРєРѕ SSE-РїСѓС‚СЊ, Р±РµР· РѕС‚РєР°С‚Р° РЅР° СЂСѓС‡РЅРѕР№ polling.
    };
  } catch {
    // SSE РЅРµРґРѕСЃС‚СѓРїРµРЅ вЂ” РїРѕРєР°Р¶РµРј РїСЂРµРґСѓРїСЂРµР¶РґРµРЅРёРµ.
    toast("Realtime stream parsing РЅРµРґРѕСЃС‚СѓРїРµРЅ", "error");
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
  setHeader("Р”Р°С€Р±РѕСЂРґ", "Р‘РёР·РЅРµСЃ-РјРµС‚СЂРёРєРё Р±РѕС‚Р°: СЂР°СЃСЃС‹Р»РєРё, РєР»РёРµРЅС‚С‹, РєР»Р°СЃСЃС‹, Р°РєС‚РёРІРЅРѕСЃС‚СЊ");
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full space-y-6">
      <!-- KPI СЂСЏРґ 1: РђРєРєР°СѓРЅС‚С‹ + РґРёР°Р»РѕРіРё -->
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
        ${kpi("РђРєРєР°СѓРЅС‚С‹", "kpiAccTotal", "вЂ”", "РІСЃРµРіРѕ")}
        ${kpi("AI Р°РєС‚РёРІРЅС‹", "kpiAccAI", "вЂ”", "auto-СЂРµР¶РёРј", "text-emerald-300")}
        ${kpi("MANUAL", "kpiAccManual", "вЂ”", "РѕРїРµСЂР°С‚РѕСЂ РѕС‚РІРµС‡Р°РµС‚", "text-amber-300")}
        ${kpi("РђРІС‚РѕСЂРёР·РѕРІР°РЅС‹", "kpiAccAuth", "вЂ”", "Telegram OK")}
      </div>
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
        ${kpi("Р”РёР°Р»РѕРіРё РІСЃРµРіРѕ", "kpiDialogs", "вЂ”", "СѓРЅРёРєР°Р»СЊРЅС‹С… РїР°СЂ (acc, peer)")}
        ${kpi("Р”РёР°Р»РѕРіРё 24С‡", "kpiDialogs24", "вЂ”", "Р°РєС‚РёРІРЅС‹С… Р·Р° СЃСѓС‚РєРё")}
        ${kpi("РљР»РёРµРЅС‚С‹", "kpiClients", "вЂ”", "Р·Р°РїРёСЃРµР№ РІ Р‘Р”")}
        ${kpi("Live-СЃС‚СЂРёРј", "dashLive", String(liveCounter), "СЃРѕР±С‹С‚РёР№ СЃ РІС…РѕРґР°")}
      </div>

      <!-- РЎРѕРѕР±С‰РµРЅРёСЏ -->
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
        ${kpi("Р’С…РѕРґСЏС‰РёРµ 24С‡", "kpiMsgIn", "вЂ”", "РѕС‚ РєР»РёРµРЅС‚РѕРІ", "text-sky-300")}
        ${kpi("РСЃС…РѕРґСЏС‰РёРµ AI 24С‡", "kpiMsgOut", "вЂ”", "РѕС‚РІРµС‚РёР» РЅРµР№СЂРѕС‡Р°С‚", "text-violet-300")}
        ${kpi("Р СѓС‡РЅС‹Рµ 24С‡", "kpiManualSent", "вЂ”", "РёР· РІРµР±-РїР°РЅРµР»Рё")}
        ${kpi("РћС‡РµСЂРµРґСЊ pending/failed", "kpiManualQueue", "вЂ”", "Р¶РґСѓС‚ РѕС‚РїСЂР°РІРєРё / РЅРµ РґРѕСЃС‚Р°РІР»РµРЅС‹", "text-amber-300")}
      </div>

      <!-- Р Р°СЃСЃС‹Р»РєРё KPI -->
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
        ${kpi("Р Р°СЃСЃС‹Р»РєРё RUNNING", "kpiMailRun", "вЂ”", "РёРґСѓС‚ СЃРµР№С‡Р°СЃ", "text-emerald-300")}
        ${kpi("Р Р°СЃСЃС‹Р»РєРё PAUSED", "kpiMailPause", "вЂ”", "Р¶РґСѓС‚ РїСЂРѕРґРѕР»Р¶РµРЅРёСЏ", "text-amber-300")}
        ${kpi("Р Р°СЃСЃС‹Р»РєРё 24С‡ Р·Р°РІРµСЂС€РµРЅРѕ", "kpiMailDone", "вЂ”", "")}
        ${kpi("РЎРѕРѕР±С‰. СЂР°СЃСЃС‹Р»РѕРє 24С‡", "kpiMailSent", "вЂ”", "СѓСЃРїРµС… / РѕС€РёР±РєРё СЃРј. РЅРёР¶Рµ")}
      </div>

      <!-- Р“СЂР°С„РёРєРё Рё Р°РєС‚РёРІРЅС‹Рµ СЂР°СЃСЃС‹Р»РєРё -->
      <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div class="card lg:col-span-2">
          <div class="flex items-center justify-between mb-3">
            <h3 class="font-semibold">РђРєС‚РёРІРЅРѕСЃС‚СЊ Р·Р° 24 С‡Р°СЃР°</h3>
            <div class="text-xs text-slate-400 flex items-center gap-3">
              <span class="flex items-center gap-1"><span class="ts-dot ts-in"></span> РІС…РѕРґСЏС‰РёРµ</span>
              <span class="flex items-center gap-1"><span class="ts-dot ts-out"></span> AI РѕС‚РІРµС‚</span>
              <span class="flex items-center gap-1"><span class="ts-dot ts-mn"></span> СЂСѓС‡РЅС‹Рµ</span>
            </div>
          </div>
          <div id="dashTimeseries" class="ts-chart">вЂ¦</div>
        </div>
        <div class="card">
          <h3 class="font-semibold mb-3">РђРєС‚РёРІРЅС‹Рµ СЂР°СЃСЃС‹Р»РєРё</h3>
          <div id="dashMailings" class="space-y-2 text-sm text-slate-400">вЂ¦</div>
        </div>
      </div>

      <!-- РљР»Р°СЃСЃС‹ РєР»РёРµРЅС‚РѕРІ Рё С‚РѕРї-Р°РєРєР°СѓРЅС‚С‹ -->
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div class="card">
          <h3 class="font-semibold mb-3">Р Р°СЃРїСЂРµРґРµР»РµРЅРёРµ РєР»Р°СЃСЃРѕРІ РєР»РёРµРЅС‚РѕРІ</h3>
          <div id="dashClasses" class="space-y-2 text-sm">вЂ¦</div>
        </div>
        <div class="card">
          <h3 class="font-semibold mb-3">РўРѕРї-Р°РєРєР°СѓРЅС‚С‹ РїРѕ Р°РєС‚РёРІРЅРѕСЃС‚Рё (24С‡)</h3>
          <table class="cb-table">
            <thead><tr><th>РђРєРєР°СѓРЅС‚</th><th>Р РµР¶РёРј</th><th class="text-right">In</th><th class="text-right">Out</th></tr></thead>
            <tbody id="dashTopAccounts"><tr><td colspan="4" class="text-slate-500 text-center py-4">вЂ¦</td></tr></tbody>
          </table>
        </div>
      </div>

      <!-- Recent live + РїРѕСЃР»РµРґРЅРёРµ СЃРѕРѕР±С‰РµРЅРёСЏ -->
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div class="card">
          <div class="flex items-center justify-between mb-3">
            <h3 class="font-semibold">Live-РїРѕС‚РѕРє СЃРѕРѕР±С‰РµРЅРёР№</h3>
            <span class="text-xs text-slate-400">РїРѕСЃР»Рµ РІС…РѕРґР° РІ РїР°РЅРµР»СЊ</span>
          </div>
          <div id="dashRecent" class="space-y-2 text-sm text-slate-400">РџРѕРґРѕР¶РґРёС‚Рµ СЃРѕР±С‹С‚РёР№вЂ¦</div>
        </div>
        <div class="card">
          <h3 class="font-semibold mb-3">РџРѕСЃР»РµРґРЅРёРµ СЃРѕРѕР±С‰РµРЅРёСЏ (Р‘Р”)</h3>
          <div id="dashRecentDb" class="space-y-2 text-sm text-slate-400">вЂ¦</div>
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
          <div class="text-[11px] text-slate-500">acc#${m.account_id} в†” ${m.peer_user_id} В· ${fmtRelative(m.created_at)}</div>
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
    $("#kpiMailSent").textContent = `${s.mailing_sent_24h} вњ“ / ${s.mailing_failed_24h} вњ•`;
  } catch (e) { toast(`summary: ${e.message}`, "error"); }
}

async function loadDashTimeseries() {
  const el = $("#dashTimeseries");
  if (!el) return;
  try {
    const points = await api("/business/dashboard/timeseries?hours=24");
    if (!points.length) { el.textContent = "РќРµС‚ РґР°РЅРЅС‹С… Р·Р° РїРµСЂРёРѕРґ."; return; }
    const max = points.reduce((m, p) =>
      Math.max(m, (p.messages_in || 0) + (p.messages_out || 0) + (p.manual_sent || 0)), 1);
    const cols = points.map(p => {
      const total = (p.messages_in || 0) + (p.messages_out || 0) + (p.manual_sent || 0);
      const h = Math.max(2, Math.round((total / max) * 100));
      const inH  = Math.round(((p.messages_in || 0) / Math.max(1, total)) * h);
      const outH = Math.round(((p.messages_out || 0) / Math.max(1, total)) * h);
      const mnH  = h - inH - outH;
      const tip = `${p.ts}\nв†ђ ${p.messages_in} В· в†’ ${p.messages_out} В· вњ‹ ${p.manual_sent}`;
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
  } catch (e) { el.textContent = `РћС€РёР±РєР°: ${e.message}`; }
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
            <span class="text-slate-500">РєР»РёРµРЅС‚РѕРІ: <b class="text-slate-200">${it.clients}</b> В· СЃРѕР±С‹С‚РёР№: ${it.events}</span>
          </div>
          <div class="cb-bar mt-1"><div class="cb-bar-fill cls-${escapeHTML(it.class_key)}" style="width:${w}%"></div></div>
        </div>
      `;
    }).join("") || `<div class="text-slate-500">РќРµС‚ РґР°РЅРЅС‹С… РїРѕ РєР»Р°СЃСЃР°Рј.</div>`;
  } catch (e) { el.innerHTML = `<div class="text-rose-400">${escapeHTML(e.message)}</div>`; }
}

async function loadDashTopAccounts() {
  const el = $("#dashTopAccounts");
  if (!el) return;
  try {
    const list = await api("/business/dashboard/top_accounts?hours=24&limit=10");
    if (!list.length) { el.innerHTML = `<tr><td colspan="4" class="text-slate-500 text-center py-3">РќРµС‚ Р°РєС‚РёРІРЅРѕСЃС‚Рё.</td></tr>`; return; }
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
    if (!list.length) { el.innerHTML = `<div class="text-slate-500">РќРµС‚ Р°РєС‚РёРІРЅС‹С… СЂР°СЃСЃС‹Р»РѕРє.</div>`; return; }
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
          <div class="text-[11px] text-slate-500 mt-1">РѕС‚РїСЂР°РІР»РµРЅРѕ ${m.sent}/${m.total} В· РѕС€РёР±РѕРє ${m.failed}</div>
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
    if (!list.length) { el.innerHTML = `<div class="text-slate-500">РЎРѕРѕР±С‰РµРЅРёР№ РЅРµС‚.</div>`; return; }
    el.innerHTML = list.map(m => `
      <div class="flex items-start gap-2">
        <span class="pill ${m.role === 'assistant' ? 'pill-blue' : 'pill-gray'}">${escapeHTML(m.role)}</span>
        <div class="min-w-0 flex-1">
          <div class="truncate text-slate-200">${escapeHTML(m.content || "")}</div>
          <div class="text-[11px] text-slate-500"><a href="#/dialogs/${m.account_id}/${m.peer_user_id}" class="hover:text-accent-500">${escapeHTML(m.account_title)} в†” ${escapeHTML(m.peer_title)}</a> В· ${fmtRelative(m.created_at)}</div>
        </div>
      </div>`).join("");
  } catch (e) { el.innerHTML = `<div class="text-rose-400">${escapeHTML(e.message)}</div>`; }
}

/* ---------------------------- Accounts view ---------------------------- */

async function renderAccounts(accountIdStr) {
  const accountId = accountIdStr ? Number(accountIdStr) : null;
  setHeader(
    "РђРєРєР°СѓРЅС‚С‹",
    accountId
      ? `Р РµРґР°РєС‚РѕСЂ #${accountId}`
      : "РЎРѕСЃС‚РѕСЏРЅРёРµ Р°РєРєР°СѓРЅС‚РѕРІ Рё СЂРµР¶РёРј Р°РІС‚РѕРѕС‚РІРµС‚Р° (AI / Manual)",
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
        <div class="mt-3 flex items-center gap-3">
          <button id="tdataUploadBtn" type="button" class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white">Загрузить ZIP</button>
          <span id="tdataMsg" class="text-xs text-slate-400"></span>
        </div>
      </div>
      <div class="card mb-4">
        <div class="flex items-center justify-between mb-3">
          <h3 class="font-semibold text-slate-100">РќРѕРІС‹Р№ Р°РєРєР°СѓРЅС‚ (Р±С‹СЃС‚СЂРѕРµ СЃРѕР·РґР°РЅРёРµ)</h3>
          <span class="text-xs text-slate-500">Tdata/.session РІСЃС‘ РµС‰С‘ РёРјРїРѕСЂС‚РёСЂСѓСЋС‚СЃСЏ РІ Telegram-Р±РѕС‚Рµ</span>
        </div>
        <form id="accountCreateForm" class="grid grid-cols-1 md:grid-cols-4 gap-3 text-sm">
          <label class="block">
            <span class="text-slate-400 text-xs">РўРµР»РµС„РѕРЅ *</span>
            <input name="phone" required placeholder="+79990001122"
                   class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">РќР°Р·РІР°РЅРёРµ РІ Р±РѕС‚Рµ (list_label)</span>
            <input name="list_label" placeholder="sales-01"
                   class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">Session name (РѕРїС†.)</span>
            <input name="session_name" placeholder="web_7999..."
                   class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 font-mono" />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">Username (РѕРїС†.)</span>
            <input name="username" placeholder="username"
                   class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">РРјСЏ</span>
            <input name="first_name"
                   class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">Р¤Р°РјРёР»РёСЏ</span>
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
            <span class="text-slate-400 text-xs">Р РµР¶РёРј</span>
            <select name="ai_mode" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
              <option value="MANUAL">MANUAL</option>
              <option value="AI_ACTIVE">AI_ACTIVE</option>
            </select>
          </label>
          <div class="md:col-span-4 flex items-center gap-3">
            <button type="submit" class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white">РЎРѕР·РґР°С‚СЊ Р°РєРєР°СѓРЅС‚</button>
            <span id="accCreateMsg" class="text-xs text-slate-400"></span>
          </div>
        </form>
      </div>
      <div class="card overflow-hidden p-0">
        <div class="px-4 py-3 border-b border-ink-700 flex items-center gap-3 text-sm flex-wrap">
          <input id="accSearch" placeholder="РџРѕРёСЃРє: list_label / @username / phone / #id"
                 value="${escapeHTML(state.accounts?.q || "")}"
                 class="px-3 py-1.5 rounded bg-ink-800 border border-ink-600 text-slate-100 w-80 max-w-full" />
          <select id="accSort" class="bg-ink-800 border border-ink-600 rounded px-2 py-1.5 text-slate-100">
            <option value="id_desc" ${(state.accounts?.sort || "id_desc") === "id_desc" ? "selected" : ""}>РЎРЅР°С‡Р°Р»Р° РЅРѕРІС‹Рµ (#id в†“)</option>
            <option value="id_asc" ${(state.accounts?.sort || "id_desc") === "id_asc" ? "selected" : ""}>РЎРЅР°С‡Р°Р»Р° СЃС‚Р°СЂС‹Рµ (#id в†‘)</option>
            <option value="dialogs_desc" ${(state.accounts?.sort || "id_desc") === "dialogs_desc" ? "selected" : ""}>РџРѕ РґРёР°Р»РѕРіР°Рј (Р±РѕР»СЊС€Рµ в†’ РјРµРЅСЊС€Рµ)</option>
            <option value="last_dialog_desc" ${(state.accounts?.sort || "id_desc") === "last_dialog_desc" ? "selected" : ""}>РљР°Рє РІ РјРµСЃСЃРµРЅРґР¶РµСЂРµ (РїРѕСЃР»РµРґРЅРёР№ РґРёР°Р»РѕРі)</option>
            <option value="label_asc" ${(state.accounts?.sort || "id_desc") === "label_asc" ? "selected" : ""}>РџРѕ РЅР°Р·РІР°РЅРёСЋ (Aв†’РЇ)</option>
          </select>
          <span id="accCount" class="text-slate-500 text-xs ml-auto"></span>
        </div>
        <table class="cb-table">
          <thead>
            <tr>
              <th>ID</th><th>РђРєРєР°СѓРЅС‚</th><th>РЎС‚Р°С‚СѓСЃ</th><th>Membership</th>
              <th>Р РµР¶РёРј AI</th><th class="text-right">Р”РёР°Р»РѕРіРё</th><th class="text-right">Р’ РѕС‡РµСЂРµРґРё</th>
              <th>РџРѕСЃР»РµРґРЅСЏСЏ Р°РєС‚РёРІРЅРѕСЃС‚СЊ</th><th></th>
            </tr>
          </thead>
          <tbody id="accountsBody">
            <tr><td colspan="9" class="text-center text-slate-500 py-8">Р—Р°РіСЂСѓР·РєР°вЂ¦</td></tr>
          </tbody>
        </table>
      </div>
      <div id="accountEditorWrap" class="${accountId ? '' : 'hidden'} mt-4 card">
        <div id="accountEditor">${accountId ? "Р—Р°РіСЂСѓР·РєР°вЂ¦" : ""}</div>
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
  out.textContent = "вЂ¦";
  try {
    const created = await api("/business/accounts", { method: "POST", body });
    toast(`РђРєРєР°СѓРЅС‚ #${created.id} СЃРѕР·РґР°РЅ`, "success");
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
    if (accCount) accCount.textContent = `РїРѕРєР°Р·Р°РЅРѕ: ${rows.length} / ${list.length}`;

    const tbody = $("#accountsBody");
    if (!tbody) return;
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="text-center text-slate-500 py-8">РќРµС‚ Р°РєРєР°СѓРЅС‚РѕРІ РІ Р‘Р”.</td></tr>`;
      return;
    }
    tbody.innerHTML = rows.map(a => {
      const title = escapeHTML(a.list_label || a.username || a.phone || `#${a.id}`);
      const fullName = [a.first_name, a.last_name].filter(Boolean).join(" ");
      const statusPill = accountStatusPill(a.status);
      const modePill = a.ai_mode === "MANUAL"
        ? `<span class="pill pill-amber">MANUAL</span>`
        : `<span class="pill pill-green">AI_ACTIVE</span>`;
      const toggleLabel = a.ai_mode === "MANUAL" ? "в†’ AI_ACTIVE" : "в†’ MANUAL";
      return `
        <tr data-account-id="${a.id}">
          <td class="text-slate-500">#${a.id}</td>
          <td>
            <div class="font-medium text-slate-100">${title}</div>
            <div class="text-xs text-slate-500">${escapeHTML(fullName) || "вЂ”"} В· ${escapeHTML(a.username || "")}${a.phone ? ' В· ' + escapeHTML(a.phone) : ''}</div>
          </td>
          <td>${statusPill}</td>
          <td><span class="pill pill-gray">${escapeHTML(a.membership || "вЂ”")}</span></td>
          <td>${modePill}</td>
          <td class="text-right text-slate-300">${a.dialogs_count}</td>
          <td class="text-right ${a.pending_outbound > 0 ? 'text-amber-400 font-medium' : 'text-slate-300'}">${a.pending_outbound}</td>
          <td class="text-slate-400 text-xs">${fmtRelative(a.last_activity)}</td>
          <td class="text-right whitespace-nowrap">
            <button data-act="goto-dialogs" class="px-2 py-1 rounded bg-ink-700 hover:bg-ink-600 text-xs mr-1">рџ’¬ Р”РёР°Р»РѕРіРё</button>
            <button data-act="edit" class="px-2 py-1 rounded bg-ink-700 hover:bg-ink-600 text-xs mr-1">вњЋ РР·РјРµРЅРёС‚СЊ</button>
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
        btn.disabled = true; btn.textContent = "вЂ¦";
        try {
          const acc = state.cache.accountById.get(accountId);
          const next = acc.ai_mode === "MANUAL" ? "AI_ACTIVE" : "MANUAL";
          await api(`/business/accounts/${accountId}/mode`, { method: "POST", body: { mode: next } });
          toast(`РђРєРєР°СѓРЅС‚ #${accountId} в†’ ${next}`, "success");
          await refreshAccountsTable();
        } catch (e) {
          toast(`РќРµ СѓРґР°Р»РѕСЃСЊ РїРµСЂРµРєР»СЋС‡РёС‚СЊ: ${e.message}`, "error");
          btn.disabled = false;
        }
      });
    });
  } catch (e) {
    toast(`РћС€РёР±РєР° Р·Р°РіСЂСѓР·РєРё Р°РєРєР°СѓРЅС‚РѕРІ: ${e.message}`, "error");
  }
}

function accountStatusPill(s) {
  if (!s) return `<span class="pill pill-gray">вЂ”</span>`;
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
  el.innerHTML = `<div class="text-slate-500 text-sm">Р—Р°РіСЂСѓР·РєР°вЂ¦</div>`;
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
    `).join("") || `<span class="text-slate-500 text-xs">Р“СЂСѓРїРї РµС‰С‘ РЅРµС‚ вЂ” СЃРѕР·РґР°Р№С‚Рµ РІ СЂР°Р·РґРµР»Рµ В«Р“СЂСѓРїРїС‹В».</span>`;
    const proxyOptions = [
      `<option value="0" ${!acc.proxy_id ? "selected" : ""}>вЂ” Р±РµР· РїСЂРѕРєСЃРё вЂ”</option>`,
      ...(proxies || []).map(p => `
        <option value="${p.id}" ${acc.proxy_id === p.id ? "selected" : ""}>
          ${escapeHTML(p.name)} (${escapeHTML(p.host)}:${p.port}) ${p.is_working ? "вњ“" : "вњ—"}
        </option>
      `),
    ].join("");
    el.innerHTML = `
      <div class="flex items-center justify-between mb-4 gap-3">
        <div>
          <h3 class="font-semibold text-white">Р РµРґР°РєС‚РёСЂРѕРІР°РЅРёРµ #${acc.id} вЂ” ${escapeHTML(acc.list_label || acc.username || acc.phone || "")}</h3>
          <p class="text-xs text-slate-500 mt-1">phone: ${escapeHTML(acc.phone || "вЂ”")} В· username: ${escapeHTML(acc.username || "вЂ”")} В· РѕС‚РїСЂР°РІР»РµРЅРѕ: ${acc.messages_sent} / today ${acc.messages_today}</p>
        </div>
        <div class="flex gap-2">
          <button id="accCloseBtn" class="px-3 py-2 rounded-md bg-ink-700 hover:bg-ink-600 text-sm">Р—Р°РєСЂС‹С‚СЊ</button>
          <button id="accDeleteBtn" class="px-3 py-2 rounded-md bg-rose-700 hover:bg-rose-600 text-white text-sm">рџ—‘ РЈРґР°Р»РёС‚СЊ</button>
        </div>
      </div>
      <form id="accountEditForm" class="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
        <label class="block">
          <span class="text-slate-400 text-xs">РџРѕРґРїРёСЃСЊ РІ СЃРїРёСЃРєРµ (list_label)</span>
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
          <span class="text-slate-400 text-xs">РРјСЏ (Telegram first_name)</span>
          <input name="first_name" value="${escapeHTML(acc.first_name || "")}"
                 class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
        </label>
        <label class="block">
          <span class="text-slate-400 text-xs">Р¤Р°РјРёР»РёСЏ (last_name)</span>
          <input name="last_name" value="${escapeHTML(acc.last_name || "")}"
                 class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
        </label>
        <label class="block">
          <span class="text-slate-400 text-xs">Bio</span>
          <textarea name="bio" rows="2" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">${escapeHTML(acc.bio || "")}</textarea>
        </label>
        <label class="block">
          <span class="text-slate-400 text-xs">РўРµРіРё (CSV, РЅР°РїСЂРёРјРµСЂ ,USA,Main,)</span>
          <input name="tags" value="${escapeHTML(acc.tags || "")}"
                 class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
        </label>
        <label class="block">
          <span class="text-slate-400 text-xs">Р”РЅРµРІРЅРѕР№ Р»РёРјРёС‚ СЃРѕРѕР±С‰РµРЅРёР№</span>
          <input name="daily_limit" type="number" min="0" max="10000" value="${acc.daily_limit}"
                 class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
        </label>
        <label class="block">
          <span class="text-slate-400 text-xs">РЎС‚Р°С‚СѓСЃ</span>
          <select name="status" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
            ${["active","inactive","banned","flood_wait","error","spam_blocked"].map(v => `<option value="${v}" ${acc.status === v ? "selected" : ""}>${v}</option>`).join("")}
          </select>
        </label>
        <label class="flex items-center gap-2 mt-6 text-slate-300">
          <input name="warmup_enabled" type="checkbox" ${acc.warmup_enabled ? "checked" : ""} class="rounded border-ink-600 bg-ink-800" />
          РџСЂРѕРіСЂРµРІ РІРєР»СЋС‡С‘РЅ
        </label>
        <label class="block">
          <span class="text-slate-400 text-xs">РџСЂРѕС„РёР»СЊ РїСЂРѕРіСЂРµРІР°</span>
          <input name="warmup_profile" value="${escapeHTML(acc.warmup_profile || "safe")}"
                 class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
        </label>
        <label class="block col-span-full">
          <span class="text-slate-400 text-xs">РџСЂРѕРєСЃРё</span>
          <select name="proxy_id" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
            ${proxyOptions}
          </select>
        </label>
        <div class="col-span-full">
          <span class="text-slate-400 text-xs">Р“СЂСѓРїРїС‹</span>
          <div class="mt-2 grid grid-cols-2 md:grid-cols-3 gap-2">${groupsCheckboxes}</div>
        </div>
        <div class="col-span-full flex items-center gap-3 mt-2">
          <button type="submit" class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white">РЎРѕС…СЂР°РЅРёС‚СЊ</button>
          <a href="#/dialogs/${acc.id}" class="text-sm text-slate-400 hover:text-slate-200">в†’ РћС‚РєСЂС‹С‚СЊ РґРёР°Р»РѕРіРё</a>
          <span id="accSaveMsg" class="text-xs text-slate-400"></span>
        </div>
      </form>
    `;

    $("#accCloseBtn").addEventListener("click", () => {
      window.location.hash = "#/accounts";
    });

    $("#accDeleteBtn").addEventListener("click", async () => {
      if (!confirm(`РЈРґР°Р»РёС‚СЊ Р°РєРєР°СѓРЅС‚ #${acc.id} РїРѕР»РЅРѕСЃС‚СЊСЋ? Р­С‚Рѕ СѓРґР°Р»РёС‚ РІСЃРµ РµРіРѕ РґРёР°Р»РѕРіРё, СЃРѕРѕР±С‰РµРЅРёСЏ Рё Р·Р°РїРёСЃРё.`)) return;
      try {
        await api(`/business/accounts/${acc.id}`, { method: "DELETE" });
        toast(`РђРєРєР°СѓРЅС‚ #${acc.id} СѓРґР°Р»С‘РЅ`, "success");
        window.location.hash = "#/accounts";
      } catch (e) {
        toast(`РћС€РёР±РєР° СѓРґР°Р»РµРЅРёСЏ: ${e.message}`, "error");
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
      out.textContent = "вЂ¦";
      try {
        await api(`/business/accounts/${acc.id}`, { method: "PATCH", body });
        toast("РЎРѕС…СЂР°РЅРµРЅРѕ", "success");
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
  setHeader("Р”РёР°Р»РѕРіРё", "Live-РїСЂРѕСЃРјРѕС‚СЂ РїРµСЂРµРїРёСЃРѕРє Рё СЂСѓС‡РЅС‹Рµ РѕС‚РІРµС‚С‹");
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
          <h3 class="font-medium text-slate-200 text-sm">Р”РёР°Р»РѕРіРё</h3>
          <button id="dlgAccountsRefresh" class="text-xs text-slate-400 hover:text-slate-200">вџі</button>
        </div>
        <div class="px-3 py-2 border-b border-ink-700 flex items-center gap-2">
          <button id="dlgTabAccounts" class="px-2 py-1 rounded text-xs ${state.dialogs.leftTab === 'accounts' ? 'bg-accent-600 text-white' : 'bg-ink-800 text-slate-300 hover:bg-ink-700'}">РђРєРєР°СѓРЅС‚С‹</button>
          <button id="dlgTabGroups" class="px-2 py-1 rounded text-xs ${state.dialogs.leftTab === 'groups' ? 'bg-accent-600 text-white' : 'bg-ink-800 text-slate-300 hover:bg-ink-700'}">Р“СЂСѓРїРїС‹</button>
        </div>
        <div class="px-3 py-2 border-b border-ink-700">
          <input
            id="dlgAccountsSearch"
            type="text"
            placeholder="РџРѕРёСЃРє РїРѕ РЅР°Р·РІР°РЅРёСЋ Р°РєРєР°СѓРЅС‚Р°вЂ¦"
            value="${escapeHTML(state.dialogs?.accountSearch || '')}"
            class="w-full bg-ink-800 border border-ink-700 rounded px-2 py-1.5 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:border-accent-500 ${state.dialogs.leftTab === 'accounts' ? '' : 'hidden'}"
          />
        </div>
        <div id="dlgAccountsList" class="flex-1 overflow-y-auto cb-scroll ${state.dialogs.leftTab === 'accounts' ? '' : 'hidden'}">
          <div class="p-4 text-slate-500 text-sm">Р—Р°РіСЂСѓР·РєР°вЂ¦</div>
        </div>
        <div id="dlgGroupsList" class="flex-1 overflow-y-auto cb-scroll ${state.dialogs.leftTab === 'groups' ? '' : 'hidden'}">
          <div class="p-4 text-slate-500 text-sm">Р—Р°РіСЂСѓР·РєР°вЂ¦</div>
        </div>
      </aside>
      <section class="col-span-${peerId ? '4' : '9'} min-w-0 min-h-0 border-r border-ink-700 flex flex-col overflow-hidden">
        <div class="px-4 py-3 border-b border-ink-700 flex items-center justify-between">
          <h3 id="dlgListTitle" class="font-medium text-slate-200 text-sm">${accountId ? `Р”РёР°Р»РѕРіРё Р°РєРєР°СѓРЅС‚Р° #${accountId}` : 'Р’С‹Р±РµСЂРёС‚Рµ Р°РєРєР°СѓРЅС‚'}</h3>
          <button id="dlgListRefresh" class="text-xs text-slate-400 hover:text-slate-200">вџі</button>
        </div>
        <div id="dlgList" class="flex-1 overflow-y-auto cb-scroll">
          ${accountId ? '<div class="p-4 text-slate-500 text-sm">Р—Р°РіСЂСѓР·РєР°вЂ¦</div>' : '<div class="p-4 text-slate-500 text-sm">РЎР»РµРІР° РІС‹Р±РµСЂРёС‚Рµ Р°РєРєР°СѓРЅС‚.</div>'}
        </div>
      </section>
      ${peerId ? `
        <section class="col-span-5 min-w-0 min-h-0 flex flex-col overflow-hidden">
          <div class="px-4 py-3 border-b border-ink-700 flex items-center justify-between gap-2">
            <div class="min-w-0">
              <h3 class="font-medium text-slate-200 text-sm truncate" id="dlgChatTitle">Р”РёР°Р»РѕРі СЃ ${peerId}</h3>
              <div class="text-xs text-slate-500" id="dlgChatSub">вЂ”</div>
            </div>
            <div class="flex items-center gap-2">
              <button id="dlgDeleteBtn" class="text-xs px-2 py-1 rounded bg-rose-900/40 hover:bg-rose-800 text-rose-200 border border-rose-700/50">рџ—‘ РЈРґР°Р»РёС‚СЊ</button>
            </div>
          </div>
          <div id="dlgMessages" class="flex-1 overflow-y-auto cb-scroll p-4 space-y-2 bg-ink-950/40">
            <div class="text-slate-500 text-sm text-center">Р—Р°РіСЂСѓР·РєР°вЂ¦</div>
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
    // РљСѓСЂСЃРѕСЂ РІ РєРѕРЅРµС†, С‡С‚РѕР±С‹ РїСЂРё РїРµСЂРµСЂРёСЃРѕРІРєРµ РЅРµ В«РїСЂС‹РіР°Р»В».
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

  // force=true: РїСЂРё Р·Р°С…РѕРґРµ РІ СЂР°Р·РґРµР» РІСЃРµРіРґР° С‚СЏРЅРµРј Р°РєС‚СѓР°Р»СЊРЅС‹Рµ last_dialog_at,
  // С‡С‚РѕР±С‹ РїРѕСЂСЏРґРѕРє В«РєР°Рє РІ РјРµСЃСЃРµРЅРґР¶РµСЂРµВ» РѕС‚СЂР°Р¶Р°Р» СЃРІРµР¶РёРµ СЃРѕРѕР±С‰РµРЅРёСЏ.
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
    // Р’СЃРµРіРґР° С‚СЏРЅРµРј СЃРІРµР¶РёР№ СЃРїРёСЃРѕРє, С‡С‚РѕР±С‹ last_dialog_at Р±С‹Р» Р°РєС‚СѓР°Р»СЊРЅС‹Рј
    // Рё РїРѕСЂСЏРґРѕРє В«РєР°Рє РІ РјРµСЃСЃРµРЅРґР¶РµСЂРµВ» РЅРµ РІСЂР°Р». РљСЌС€ РёСЃРїРѕР»СЊР·СѓРµРј С‚РѕР»СЊРєРѕ
    // РґР»СЏ СЃРёРЅС…СЂРѕРЅРЅС‹С… РїРµСЂРµСЂРёСЃРѕРІРѕРє РїСЂРё РЅР°Р±РѕСЂРµ С‚РµРєСЃС‚Р° РІ РїРѕРёСЃРєРµ.
    const list = (!opts.force && state.cache.accounts)
      ? state.cache.accounts
      : await api("/business/accounts");
    state.cache.accounts = list;
    state.cache.accountById = new Map(list.map(a => [a.id, a]));
    paintDialogsAccounts(list, activeAccountId);
  } catch (e) {
    el.innerHTML = `<div class="p-4 text-rose-400 text-sm">РћС€РёР±РєР°: ${escapeHTML(e.message)}</div>`;
  }
}

function paintDialogsAccounts(list, activeAccountId) {
  const el = $("#dlgAccountsList");
  if (!el) return;
  if (!list.length) {
    el.innerHTML = `<div class="p-4 text-slate-500 text-sm">РќРµС‚ Р°РєРєР°СѓРЅС‚РѕРІ.</div>`;
    return;
  }

  let scoped = list.slice();
  if (state.dialogs.selectedGroupId !== null && state.dialogs.groupAccountIds instanceof Set) {
    scoped = scoped.filter((a) => state.dialogs.groupAccountIds.has(Number(a.id)));
  }

  const q = (state.dialogs.accountSearch || "").trim().toLowerCase();
  const filtered = q
    ? scoped.filter(a => {
        // РџРѕРёСЃРє РїРѕ В«РёРјРµРЅРё Р°РєРєР°СѓРЅС‚Р° РІРЅСѓС‚СЂРё Р±РѕС‚Р°В» (list_label) вЂ” РїСЂРёРѕСЂРёС‚РµС‚,
        // РїР»СЋСЃ fallback РЅР° username/phone, С‡С‚РѕР±С‹ РїРѕР»СЊР·РѕРІР°С‚РµР»СЊ
        // РјРѕРі РЅР°Р№С‚Рё Р±РµР·С‹РјСЏРЅРЅС‹Рµ Р°РєРєР°СѓРЅС‚С‹.
        const hay = [a.list_label, a.username, a.phone, `#${a.id}`]
          .filter(Boolean).join(" ").toLowerCase();
        return hay.includes(q);
      })
    : scoped.slice();

  // РЎРѕСЂС‚РёСЂРѕРІРєР° В«РєР°Рє РІ TelegramВ»: СЃРЅР°С‡Р°Р»Р° СЃРІРµР¶РёРµ РґРёР°Р»РѕРіРё.
  // РђРєРєР°СѓРЅС‚С‹ Р±РµР· РґРёР°Р»РѕРіРѕРІ вЂ” РІ СЃР°РјРѕРј РЅРёР·Сѓ, СЃСЂРµРґРё РЅРёС… СЃС‚Р°Р±РёР»СЊРЅС‹Р№ РїРѕСЂСЏРґРѕРє РїРѕ id.
  filtered.sort((a, b) => {
    const ta = a.last_dialog_at ? Date.parse(a.last_dialog_at) : 0;
    const tb = b.last_dialog_at ? Date.parse(b.last_dialog_at) : 0;
    if (tb !== ta) return tb - ta;
    return (a.id || 0) - (b.id || 0);
  });

  if (!filtered.length) {
    el.innerHTML = `<div class="p-4 text-slate-500 text-sm">РќРёС‡РµРіРѕ РЅРµ РЅР°Р№РґРµРЅРѕ.</div>`;
    return;
  }

  el.innerHTML = filtered.map(a => {
    // РРјСЏ В«РєР°Рє РІ Р±РѕС‚РµВ»: list_label, fallback РЅР° username/phone/#id вЂ”
    // РёРјРµРЅРЅРѕ РїРѕ РЅРµРјСѓ РІРµРґС‘С‚СЃСЏ РїРѕРёСЃРє.
    const internalTitle = a.list_label || a.username || a.phone || `#${a.id}`;
    // РРјСЏ В«РєР°Рє РІ TelegramВ»: first_name + last_name, Р»РёР±Рѕ @username.
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
      ? `<span class="pill pill-amber">в†‘${a.pending_outbound}</span>` : '';
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
          <div class="text-[11px] text-slate-500 truncate">РґРёР°Р»РѕРіРѕРІ: ${a.dialogs_count}</div>
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
      { id: null, name: "Р’СЃРµ", accounts_count: state.cache.accounts?.length || 0 },
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
          state.dialogs.selectedGroupName = "Р’СЃРµ";
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
    el.innerHTML = `<div class="p-4 text-rose-400 text-sm">РћС€РёР±РєР°: ${escapeHTML(e.message)}</div>`;
  }
}

async function loadDialogsList(accountId, activePeerId) {
  const el = $("#dlgList");
  if (!el) return;
  try {
    const list = await api(`/business/accounts/${accountId}/dialogs?limit=200`);
    if (!list.length) {
      el.innerHTML = `<div class="p-4 text-slate-500 text-sm">РЈ СЌС‚РѕРіРѕ Р°РєРєР°СѓРЅС‚Р° РїРѕРєР° РЅРµС‚ РґРёР°Р»РѕРіРѕРІ РІ РЅРµР№СЂРѕС‡Р°С‚Рµ.</div>`;
      return;
    }
    el.innerHTML = list.map(d => {
      const title = d.client_username ? `@${d.client_username}` : `id ${d.peer_user_id}`;
      const isActive = d.peer_user_id === activePeerId;
      const lastWho = d.last_role === "assistant" ? "Р‘РѕС‚" : "РљР»РёРµРЅС‚";
      return `
        <a href="#/dialogs/${accountId}/${d.peer_user_id}" class="block px-4 py-3 border-b border-ink-700 ${isActive ? 'bg-ink-800' : 'hover:bg-ink-800/60'}">
          <div class="flex items-center justify-between gap-2">
            <div class="text-sm font-medium text-slate-100 truncate">${escapeHTML(title)}</div>
            <div class="text-[11px] text-slate-500 shrink-0">${fmtRelative(d.last_message_at)}</div>
          </div>
          <div class="text-xs text-slate-400 truncate mt-0.5"><span class="text-slate-500">${lastWho}:</span> ${escapeHTML(d.last_message || "")}</div>
          <div class="text-[11px] text-slate-500 mt-1">${d.messages_count} СЃРѕРѕР±С‰.</div>
        </a>
      `;
    }).join("");
  } catch (e) {
    el.innerHTML = `<div class="p-4 text-rose-400 text-sm">РћС€РёР±РєР°: ${escapeHTML(e.message)}</div>`;
  }
}

async function loadDialogMessages(accountId, peerId) {
  const el = $("#dlgMessages");
  if (!el) return;
  try {
    const list = await api(`/business/accounts/${accountId}/dialogs/${peerId}/messages?limit=200`);
    // Р’Р°Р¶РЅРѕ: СЃР±СЂР°СЃС‹РІР°РµРј РґРµРґСѓРї-РєСѓСЂСЃРѕСЂ РџР•Р Р•Р” РїРѕРІС‚РѕСЂРЅС‹Рј СЂРµРЅРґРµСЂРѕРј, РёРЅР°С‡Рµ
    // appendMessageToChat() РІС‹РєРёРЅРµС‚ РІСЃРµ В«СЃС‚Р°СЂС‹РµВ» СЃРѕРѕР±С‰РµРЅРёСЏ РєР°Рє СѓР¶Рµ РІРёРґРµРЅРЅС‹Рµ
    // Рё РІ Р»РµРЅС‚Рµ РѕСЃС‚Р°РЅСѓС‚СЃСЏ С‚РѕР»СЊРєРѕ РЅРѕРІС‹Рµ/queue. messageMaxId РѕР±РЅРѕРІР»СЏРµС‚СЃСЏ
    // РІРЅСѓС‚СЂРё appendMessageToChat (РѕРЅ СЃР°Рј Р±РµСЂС‘С‚ max).
    state.current.messageMaxId = 0;
    if (!list.length) {
      el.innerHTML = `<div class="text-slate-500 text-sm text-center">РЎРѕРѕР±С‰РµРЅРёР№ РїРѕРєР° РЅРµС‚.</div>`;
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
    case "pending":   return '<span class="qpill qpill-pend">РІ РѕС‡РµСЂРµРґРё</span>';
    case "sending":   return '<span class="qpill qpill-send">РѕС‚РїСЂР°РІР»СЏРµС‚СЃСЏ</span>';
    case "failed":    return '<span class="qpill qpill-fail">РЅРµ РґРѕСЃС‚Р°РІР»РµРЅРѕ</span>';
    case "cancelled": return '<span class="qpill qpill-cncl">РѕС‚РјРµРЅРµРЅРѕ</span>';
    case "sent":      return '<span class="qpill qpill-ok">РѕС‚РїСЂР°РІР»РµРЅРѕ</span>';
    default:          return `<span class="qpill">${escapeHTML(status || "?")}</span>`;
  }
}

function appendMessageToChat(msg) {
  const el = $("#dlgMessages");
  if (!el) return;
  // Р”СѓР±Р»Рё С‚РѕР»СЊРєРѕ РїРѕ СЂРµР°Р»СЊРЅС‹Рј neuro-СЃРѕРѕР±С‰РµРЅРёСЏРј (РїРѕР»РѕР¶РёС‚РµР»СЊРЅС‹Р№ id).
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
    if (msg.queue_attempts) metaExtra += ` В· РїРѕРїС‹С‚РєР° ${msg.queue_attempts}`;
    if (msg.queue_error) {
      metaExtra += ` В· <span class="text-rose-400" title="${escapeHTML(msg.queue_error)}">${escapeHTML(msg.queue_error.slice(0, 60))}</span>`;
    }
  }

  let actions = "";
  if (isQueue) {
    if (msg.queue_status === "failed" || msg.queue_status === "cancelled") {
      actions += `<button class="qbtn qbtn-retry" data-action="retry" data-qid="${msg.queue_id}">РџРѕРІС‚РѕСЂРёС‚СЊ</button>`;
    }
    if (msg.queue_status === "pending" || msg.queue_status === "failed") {
      actions += `<button class="qbtn qbtn-cancel" data-action="cancel" data-qid="${msg.queue_id}">РћС‚РјРµРЅРёС‚СЊ</button>`;
    }
  }

  wrap.innerHTML = `
    <div class="${bubbleCls}">${escapeHTML(msg.content || "")}</div>
    <div class="bubble-meta">${isAssistant ? "Р±РѕС‚" : "РєР»РёРµРЅС‚"} В· ${fmtDate(msg.created_at)}${metaExtra}</div>
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
      toast("РџРѕСЃС‚Р°РІР»РµРЅРѕ РЅР° РїРѕРІС‚РѕСЂРЅСѓСЋ РѕС‚РїСЂР°РІРєСѓ", "success", 1500);
    } else if (action === "cancel") {
      if (!confirm("РћС‚РјРµРЅРёС‚СЊ РѕС‚РїСЂР°РІРєСѓ СЌС‚РѕРіРѕ СЃРѕРѕР±С‰РµРЅРёСЏ?")) return;
      await api(`/business/queue/${queueId}/cancel`, { method: "POST", raw: true });
      toast("РћС‚РјРµРЅРµРЅРѕ", "success", 1500);
    }
    if (state.current.accountId && state.current.peerId) {
      loadDialogMessages(state.current.accountId, state.current.peerId);
    }
  } catch (e) {
    toast(`РћС€РёР±РєР°: ${e.message}`, "error");
  }
}

function renderComposer(accountId, peerId) {
  const el = $("#dlgComposer");
  if (!el) return;
  const acc = state.cache.accountById.get(accountId);
  const isManual = acc?.ai_mode === "MANUAL";
  const hint = isManual
    ? "РђРєРєР°СѓРЅС‚ РІ MANUAL вЂ” СЃРѕРѕР±С‰РµРЅРёСЏ РєР»РёРµРЅС‚Сѓ С€Р»С‘С‚Рµ С‚РѕР»СЊРєРѕ РІС‹."
    : "РђРєРєР°СѓРЅС‚ РІ AI_ACTIVE вЂ” СЂСѓС‡РЅРѕРµ СЃРѕРѕР±С‰РµРЅРёРµ РїРµСЂРµС…РІР°С‚РёС‚ РёРЅРёС†РёР°С‚РёРІСѓ, РР РїСЂРѕРґРѕР»Р¶РёС‚ РІРёРґРµС‚СЊ РµРіРѕ РІ РёСЃС‚РѕСЂРёРё.";
  el.innerHTML = `
    <form id="dlgSendForm" class="flex items-end gap-2">
      <textarea id="dlgInput" rows="2" maxlength="4000" placeholder="Р’РІРµРґРёС‚Рµ РѕС‚РІРµС‚ РѕС‚ РёРјРµРЅРё Р°РєРєР°СѓРЅС‚Р°вЂ¦"
        class="flex-1 resize-none px-3 py-2 rounded-lg bg-ink-800 border border-ink-600 text-sm text-slate-100 focus:border-accent-500 focus:outline-none focus:ring-1 focus:ring-accent-500"></textarea>
      <button class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white text-sm font-medium" type="submit">РћС‚РїСЂР°РІРёС‚СЊ</button>
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
      toast("РЎРѕРѕР±С‰РµРЅРёРµ РїРѕСЃС‚Р°РІР»РµРЅРѕ РІ РѕС‡РµСЂРµРґСЊ", "success", 1500);
      // РЎСЂР°Р·Сѓ РїРѕРґС‚СЏРіРёРІР°РµРј, С‡С‚РѕР±С‹ placeholder РїРѕСЏРІРёР»СЃСЏ РІ Р»РµРЅС‚Рµ.
      loadDialogMessages(accountId, peerId);
    } catch (e) {
      toast(`РќРµ СѓРґР°Р»РѕСЃСЊ РѕС‚РїСЂР°РІРёС‚СЊ: ${e.message}`, "error");
    }
  });
}

async function deleteCurrentDialog(accountId, peerId) {
  if (!confirm("РЈРґР°Р»РёС‚СЊ РґРёР°Р»РѕРі? Р’СЃРµ СЃРѕРѕР±С‰РµРЅРёСЏ Рё СЃРІСЏР·Р°РЅРЅС‹Рµ РёСЃС…РѕРґСЏС‰РёРµ Р±СѓРґСѓС‚ РІС‹С‡РёС‰РµРЅС‹ РёР· Р‘Р”.")) return;
  try {
    await api(`/business/accounts/${accountId}/dialogs/${peerId}`, { method: "DELETE", raw: true });
    toast("Р”РёР°Р»РѕРі СѓРґР°Р»С‘РЅ", "success");
    window.location.hash = `#/dialogs/${accountId}`;
  } catch (e) {
    toast(`РћС€РёР±РєР° СѓРґР°Р»РµРЅРёСЏ: ${e.message}`, "error");
  }
}

/* ----------------------------- Mailings view --------------------------- */

async function renderMailings(idStr) {
  const id = idStr ? Number(idStr) : null;
  setHeader("Р Р°СЃСЃС‹Р»РєРё", id ? `Р Р°СЃСЃС‹Р»РєР° #${id}` : "РЎРїРёСЃРѕРє Рё СѓРїСЂР°РІР»РµРЅРёРµ РєР°РјРїР°РЅРёСЏРјРё");
  const root = $("#pageRoot");
  if (!id) {
    root.innerHTML = `
      <div class="p-6 cb-scroll overflow-y-auto h-full space-y-4">
        <div class="card">
          <div class="flex items-center justify-between mb-3">
            <h3 class="font-semibold text-slate-100">РќРѕРІР°СЏ СЂР°СЃСЃС‹Р»РєР°</h3>
            <span class="text-xs text-slate-500">РЎРѕР·РґР°С‘С‚СЃСЏ РєР°Рє draft, РґР°Р»СЊС€Рµ вЂ” РЅР°СЃС‚СЂРѕР№РєР° РІ РєР°СЂС‚РѕС‡РєРµ</span>
          </div>
          <form id="mailCreateForm" class="grid grid-cols-1 md:grid-cols-4 gap-3 text-sm">
            <label class="block md:col-span-2">
              <span class="text-slate-400 text-xs">РќР°Р·РІР°РЅРёРµ *</span>
              <input name="name" required placeholder="РќРѕРІР°СЏ РєР°РјРїР°РЅРёСЏ"
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">РђСѓРґРёС‚РѕСЂРёСЏ</span>
              <select name="audience_mode" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
                <option value="classes">classes</option>
                <option value="test">test</option>
                <option value="all">all</option>
              </select>
            </label>
            <label class="flex items-center gap-2 mt-6 text-slate-300">
              <input name="neurochat_enabled" type="checkbox" class="rounded border-ink-600 bg-ink-800" />
              РќРµР№СЂРѕС‡Р°С‚ РІРєР»СЋС‡С‘РЅ
            </label>
            <label class="block md:col-span-4">
              <span class="text-slate-400 text-xs">РџРµСЂРІРѕРµ СЃРѕРѕР±С‰РµРЅРёРµ (РјРѕР¶РЅРѕ РїСѓСЃС‚РѕРµ)</span>
              <textarea name="message_text" rows="2"
                        class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100"></textarea>
            </label>
            <div class="md:col-span-4 flex items-center gap-3">
              <button type="submit" class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white">РЎРѕР·РґР°С‚СЊ СЂР°СЃСЃС‹Р»РєСѓ</button>
              <span id="mailCreateMsg" class="text-xs text-slate-400"></span>
            </div>
          </form>
        </div>
        <div class="flex items-center gap-3 text-sm">
          <label class="text-slate-400">РЎС‚Р°С‚СѓСЃ:</label>
          <select id="mailFilter" class="bg-ink-800 border border-ink-600 rounded-md px-2 py-1 text-slate-100">
            <option value="">РІСЃРµ</option>
            <option value="running">running</option>
            <option value="paused">paused</option>
            <option value="completed">completed</option>
            <option value="draft">draft</option>
            <option value="cancelled">cancelled</option>
            <option value="error">error</option>
          </select>
          <button id="mailRefresh" class="px-3 py-1 rounded bg-accent-600 hover:bg-accent-500 text-white text-sm">вџі РћР±РЅРѕРІРёС‚СЊ</button>
        </div>
        <div class="card p-0 overflow-hidden">
          <table class="cb-table">
            <thead><tr>
              <th>ID</th><th>РќР°Р·РІР°РЅРёРµ</th><th>РЎС‚Р°С‚СѓСЃ</th>
              <th class="text-right">РћС‚РїСЂР°РІР»РµРЅРѕ</th><th class="text-right">РћС€РёР±РѕРє</th>
              <th>РђСѓРґРёС‚РѕСЂРёСЏ</th><th>AI</th><th>РЎРѕР·РґР°РЅР°</th><th></th>
            </tr></thead>
            <tbody id="mailBody"><tr><td colspan="9" class="text-center text-slate-500 py-8">Р—Р°РіСЂСѓР·РєР°вЂ¦</td></tr></tbody>
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
  root.innerHTML = `<div id="mailDetail" class="p-6 cb-scroll overflow-y-auto h-full">Р—Р°РіСЂСѓР·РєР°вЂ¦</div>`;
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
  out.textContent = "вЂ¦";
  try {
    const created = await api("/business/mailings", { method: "POST", body });
    toast(`Р Р°СЃСЃС‹Р»РєР° #${created.id} СЃРѕР·РґР°РЅР°`, "success");
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
      tbody.innerHTML = `<tr><td colspan="9" class="text-center text-slate-500 py-8">РќРµС‚ СЂР°СЃСЃС‹Р»РѕРє.</td></tr>`;
      return;
    }
    tbody.innerHTML = list.map(m => `
      <tr>
        <td class="text-slate-500">#${m.id}</td>
        <td><a href="#/mailings/${m.id}" class="text-slate-100 hover:text-accent-500">${escapeHTML(m.name)}</a></td>
        <td>${mailingStatusPill(m.status)}</td>
        <td class="text-right text-slate-300">${m.sent}/${m.total || "вЂ”"}</td>
        <td class="text-right ${m.failed ? "text-rose-300" : "text-slate-400"}">${m.failed}</td>
        <td class="text-xs text-slate-400">${escapeHTML(m.audience_mode || "вЂ”")}</td>
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
  } catch (e) { toast(`Р Р°СЃСЃС‹Р»РєРё: ${e.message}`, "error"); }
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
    buttons.push(`<button class="px-2 py-1 rounded bg-amber-700 hover:bg-amber-600 text-xs" data-mail-act="pause" data-mid="${m.id}">вЏё РџР°СѓР·Р°</button>`);
    buttons.push(`<button class="px-2 py-1 rounded bg-rose-700 hover:bg-rose-600 text-xs ml-1" data-mail-act="stop" data-mid="${m.id}">вЏ№ РЎС‚РѕРї</button>`);
  } else if (m.status === "paused") {
    buttons.push(`<button class="px-2 py-1 rounded bg-emerald-700 hover:bg-emerald-600 text-xs" data-mail-act="start" data-mid="${m.id}">в–¶ Р—Р°РїСѓСЃРє</button>`);
    buttons.push(`<button class="px-2 py-1 rounded bg-rose-700 hover:bg-rose-600 text-xs ml-1" data-mail-act="stop" data-mid="${m.id}">вЏ№ РЎС‚РѕРї</button>`);
  } else {
    buttons.push(`<button class="px-2 py-1 rounded bg-emerald-700 hover:bg-emerald-600 text-xs" data-mail-act="start" data-mid="${m.id}">в–¶ Р—Р°РїСѓСЃРє</button>`);
  }
  return buttons.join("");
}

async function onMailingAction(mailingId, action) {
  if (!mailingId || !action) return;
  if (action === "stop" && !confirm(`РћСЃС‚Р°РЅРѕРІРёС‚СЊ СЂР°СЃСЃС‹Р»РєСѓ #${mailingId}?`)) return;
  try {
    const r = await api(`/business/mailings/${mailingId}/${action}`, { method: "POST" });
    if (r.status === "queued") {
      toast(`РљРѕРјР°РЅРґР° ${action} РїРѕСЃС‚Р°РІР»РµРЅР° РІ РѕС‡РµСЂРµРґСЊ`, "success", 1500);
    } else {
      toast(`${action}: ${r.detail || r.status}`, "info", 1500);
    }
    setTimeout(() => loadMailingsList(), 1500);
  } catch (e) { toast(`РћС€РёР±РєР° ${action}: ${e.message}`, "error"); }
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
      `<option value="0" ${!m.target_group_id ? "selected" : ""}>вЂ” РІСЃРµ РіСЂСѓРїРїС‹ вЂ”</option>`,
      ...(groups || []).map(g => `<option value="${g.id}" ${m.target_group_id === g.id ? "selected" : ""}>${escapeHTML(g.name)}</option>`),
    ].join("");
    root.innerHTML = `
      <div class="space-y-4 max-w-5xl">
        <div class="flex items-center gap-3">
          <a href="#/mailings" class="text-sm text-slate-400 hover:text-slate-200">в†ђ Рљ СЃРїРёСЃРєСѓ</a>
          ${mailingStatusPill(m.status)}
          <h2 class="text-lg text-slate-100 font-semibold">${escapeHTML(m.name)}</h2>
          <div class="ml-auto flex gap-1">${mailingActionButtons(m)}</div>
        </div>

        <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
          ${kpi("РћС‚РїСЂР°РІР»РµРЅРѕ", "mdSent", String(m.sent), `РёР· ${m.total || "вЂ”"}`)}
          ${kpi("РћС€РёР±РѕРє", "mdFail", String(m.failed), "", "text-rose-300")}
          ${kpi("РЎС‚Р°СЂС‚", "mdStart", fmtDate(m.started_at) || "вЂ”", "")}
          ${kpi("Р¤РёРЅРёС€", "mdEnd", fmtDate(m.completed_at) || "вЂ”", "")}
        </div>

        <!--
          Р’РђР–РќРћ: РЅР°СЃС‚СЂРѕР№РєРё СЂР°СЃСЃС‹Р»РєРё Рё РЅР°СЃС‚СЂРѕР№РєРё РЅРµР№СЂРѕС‡Р°С‚Р° СЂР°Р·РЅРµСЃРµРЅС‹
          РІ РґРІРµ РЅРµР·Р°РІРёСЃРёРјС‹Рµ С„РѕСЂРјС‹ СЃ СЃРѕР±СЃС‚РІРµРЅРЅС‹РјРё submit-РєРЅРѕРїРєР°РјРё,
          С‡С‚РѕР±С‹ РїРѕ РІРёР·СѓР°Р»Сѓ Рё UX СЃРѕРІРїР°РґР°С‚СЊ СЃ СЂР°Р·РґРµР»РµРЅРёРµРј В«СЂР°СЃСЃС‹Р»РєР° vs РЅРµР№СЂРѕС‡Р°С‚В».
          Р‘СЌРєРµРЅРґ РёСЃРїРѕР»СЊР·СѓРµС‚ РѕРґРёРЅ Рё С‚РѕС‚ Р¶Рµ PATCH /business/mailings/{id}
          Рё РїСЂРёРЅРёРјР°РµС‚ Р»СЋР±РѕР№ РїРѕРґРјРЅРѕР¶РµСЃС‚РІРµРЅРЅС‹Р№ РЅР°Р±РѕСЂ РїРѕР»РµР№.
        -->
        <div class="card">
          <div class="flex items-center justify-between mb-3">
            <h3 class="font-semibold">РќР°СЃС‚СЂРѕР№РєРё СЂР°СЃСЃС‹Р»РєРё</h3>
            ${editLocked
              ? `<span class="text-xs text-amber-400">RUNNING вЂ” РїРѕСЃС‚Р°РІСЊС‚Рµ РЅР° РїР°СѓР·Сѓ РґР»СЏ СЂРµРґР°РєС‚РёСЂРѕРІР°РЅРёСЏ</span>`
              : `<span class="text-xs text-slate-500">РџРµСЂРІРѕРµ СЃРѕРѕР±С‰РµРЅРёРµ, Р°СѓРґРёС‚РѕСЂРёСЏ, Р»РёРјРёС‚С‹ Рё Р·Р°РґРµСЂР¶РєРё</span>`}
          </div>
          <form id="mailEditForm" class="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm" ${editLocked ? "data-locked=1" : ""}>
            <label class="block md:col-span-3">
              <span class="text-slate-400 text-xs">РќР°Р·РІР°РЅРёРµ</span>
              <input name="name" value="${escapeHTML(m.name || "")}" ${editLocked ? "disabled" : ""}
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block md:col-span-3">
              <span class="text-slate-400 text-xs">РўРµРєСЃС‚ РѕСЃРЅРѕРІРЅРѕРіРѕ СЃРѕРѕР±С‰РµРЅРёСЏ</span>
              <textarea name="message_text" rows="4" ${editLocked ? "disabled" : ""}
                class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 font-mono text-xs">${escapeHTML(m.message_text || "")}</textarea>
            </label>
            <label class="block md:col-span-3">
              <span class="text-slate-400 text-xs">Р’Р°СЂРёР°РЅС‚С‹ СЃРѕРѕР±С‰РµРЅРёСЏ (РїРѕ РѕРґРЅРѕРјСѓ РІ СЃС‚СЂРѕРєРµ)</span>
              <textarea name="message_variants" rows="3" ${editLocked ? "disabled" : ""}
                class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 font-mono text-xs">${escapeHTML((m.message_variants || []).join("\n"))}</textarea>
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">РњРµР¶РґСѓ СЃРѕРѕР±С‰РµРЅРёСЏРјРё (СЃ)</span>
              <input name="delay_between_messages" type="number" step="0.1" min="0" max="600" value="${m.delay_between_messages}" ${editLocked ? "disabled" : ""}
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">РњРµР¶РґСѓ Р°РєРєР°СѓРЅС‚Р°РјРё (СЃ)</span>
              <input name="delay_between_accounts" type="number" step="0.1" min="0" max="600" value="${m.delay_between_accounts}" ${editLocked ? "disabled" : ""}
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">Р”РЅРµРІРЅРѕР№ Р»РёРјРёС‚</span>
              <input name="daily_limit" type="number" min="0" max="10000" value="${m.daily_limit}" ${editLocked ? "disabled" : ""}
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">Р’ РїР°РєРµС‚Рµ</span>
              <input name="messages_per_batch" type="number" min="0" max="10000" value="${m.messages_per_batch}" ${editLocked ? "disabled" : ""}
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">РњРµР¶РґСѓ РїР°РєРµС‚Р°РјРё (СЃ)</span>
              <input name="batch_delay" type="number" step="0.1" min="0" max="86400" value="${m.batch_delay}" ${editLocked ? "disabled" : ""}
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">Auto stop (С‡), 0 = РЅРµС‚</span>
              <input name="auto_stop_hours" type="number" step="0.5" min="0" max="720" value="${m.auto_stop_hours ?? 0}" ${editLocked ? "disabled" : ""}
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">Р¦РµР»РµРІР°СЏ РіСЂСѓРїРїР°</span>
              <select name="target_group_id" ${editLocked ? "disabled" : ""}
                class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">${groupOptions}</select>
            </label>
            <label class="block md:col-span-2">
              <span class="text-slate-400 text-xs">Community link (РґР»СЏ {link})</span>
              <input name="community_link" value="${escapeHTML(m.community_link || "")}" ${editLocked ? "disabled" : ""}
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">РђСѓРґРёС‚РѕСЂРёСЏ</span>
              <select name="audience_mode" ${editLocked ? "disabled" : ""}
                class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
                ${["classes","test","all"].map(v => `<option value="${v}" ${m.audience_mode === v ? "selected" : ""}>${v}</option>`).join("")}
              </select>
            </label>
            <div class="md:col-span-3 flex items-center gap-3">
              <button type="submit" ${editLocked ? "disabled" : ""}
                class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white disabled:opacity-50 disabled:cursor-not-allowed">РЎРѕС…СЂР°РЅРёС‚СЊ СЂР°СЃСЃС‹Р»РєСѓ</button>
              <span id="mailEditMsg" class="text-xs text-slate-400"></span>
            </div>
          </form>
        </div>

        <div class="card">
          <div class="flex items-center justify-between mb-3">
            <h3 class="font-semibold">РќР°СЃС‚СЂРѕР№РєРё РЅРµР№СЂРѕС‡Р°С‚Р°</h3>
            ${editLocked
              ? `<span class="text-xs text-amber-400">RUNNING вЂ” РїРѕСЃС‚Р°РІСЊС‚Рµ РЅР° РїР°СѓР·Сѓ РґР»СЏ СЂРµРґР°РєС‚РёСЂРѕРІР°РЅРёСЏ</span>`
              : `<span class="text-xs text-slate-500">РњРѕРґРµР»СЊ, sampling Рё system-РїСЂРѕРјРїС‚</span>`}
          </div>
          <form id="neuroEditForm" class="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm mb-5" ${editLocked ? "data-locked=1" : ""}>
            <label class="flex items-center gap-2 md:col-span-1 mt-6 text-slate-300">
              <input name="neurochat_enabled" type="checkbox" ${m.neurochat_enabled ? "checked" : ""} ${editLocked ? "disabled" : ""}
                     class="rounded border-ink-600 bg-ink-800" />
              РќРµР№СЂРѕС‡Р°С‚ РІРєР»СЋС‡С‘РЅ
            </label>
            <label class="block md:col-span-2">
              <span class="text-slate-400 text-xs">РњРѕРґРµР»СЊ OpenRouter (РЅР°РїСЂРёРјРµСЂ, openai/gpt-4o-mini)</span>
              <input name="neuro_model" value="${escapeHTML(m.neuro_model || "")}" ${editLocked ? "disabled" : ""}
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 font-mono" />
            </label>
            <label class="block md:col-span-3">
              <span class="text-slate-400 text-xs">Sampling overrides (JSON: temperature/top_p/max_tokens/вЂ¦)</span>
              <textarea name="neuro_sampling_json" rows="2" ${editLocked ? "disabled" : ""}
                class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 font-mono text-xs">${escapeHTML(m.neuro_sampling_json || "{}")}</textarea>
            </label>
            <div class="md:col-span-3 flex items-center gap-3">
              <button type="submit" ${editLocked ? "disabled" : ""}
                class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white disabled:opacity-50 disabled:cursor-not-allowed">РЎРѕС…СЂР°РЅРёС‚СЊ РЅРµР№СЂРѕС‡Р°С‚</button>
              <span id="neuroEditMsg" class="text-xs text-slate-400"></span>
            </div>
          </form>

          <div class="border-t border-ink-700 pt-4">
            <div class="flex items-center justify-between mb-2">
              <h4 class="font-medium text-slate-200">System-РїСЂРѕРјРїС‚</h4>
              <div id="mailPromptStatus" class="text-xs text-slate-500">вЂ¦</div>
            </div>
            <p class="text-xs text-slate-500 mb-3">РЎРѕС…СЂР°РЅСЏРµС‚СЃСЏ РІ <code>data/neuro/mailings/${id}/system.txt</code>. Р•СЃР»Рё С„Р°Р№Р»Р° РЅРµС‚ вЂ” РёСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ Р·РЅР°С‡РµРЅРёРµ РёР· <code>DEFAULT_NEURO_SYSTEM_PROMPT</code>. Р”РѕСЃС‚СѓРїРЅС‹Рµ РїР»РµР№СЃС…РѕР»РґРµСЂС‹ СЃРј. РІ Р±РѕС‚Рµ.</p>
            <textarea id="mailPromptText" rows="14"
              class="w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 font-mono text-xs">Р—Р°РіСЂСѓР·РєР°вЂ¦</textarea>
            <div class="flex items-center gap-3 mt-3">
              <button id="mailPromptSave" class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white text-sm">РЎРѕС…СЂР°РЅРёС‚СЊ РїСЂРѕРјРїС‚</button>
              <button id="mailPromptReset" class="px-3 py-2 rounded-lg bg-ink-700 hover:bg-ink-600 text-slate-200 text-sm">РЎР±СЂРѕСЃРёС‚СЊ Рє DEFAULT</button>
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
        out.textContent = "вЂ¦";
        try {
          await api(`/business/mailings/${id}`, { method: "PATCH", body });
          toast("Р Р°СЃСЃС‹Р»РєР° СЃРѕС…СЂР°РЅРµРЅР°", "success");
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
        out.textContent = "вЂ¦";
        try {
          await api(`/business/mailings/${id}`, { method: "PATCH", body });
          toast("РќРµР№СЂРѕС‡Р°С‚ СЃРѕС…СЂР°РЅС‘РЅ", "success");
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
    status.textContent = r.has_custom_file ? "С„Р°Р№Р» Р·Р°РґР°РЅ" : "РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ (.env)";
    status.className = `text-xs ${r.has_custom_file ? "text-emerald-300" : "text-slate-500"}`;
  } catch (e) {
    ta.value = "";
    status.textContent = `РѕС€РёР±РєР°: ${e.message}`;
    status.className = "text-xs text-rose-400";
  }
}

function bindMailingPromptHandlers(id) {
  $("#mailPromptSave").addEventListener("click", async () => {
    const ta = $("#mailPromptText");
    const out = $("#mailPromptMsg");
    if (!ta) return;
    out.textContent = "вЂ¦";
    try {
      await api(`/business/mailings/${id}/prompt`, {
        method: "PUT",
        body: { text: ta.value },
      });
      toast("РџСЂРѕРјРїС‚ СЃРѕС…СЂР°РЅС‘РЅ", "success");
      out.textContent = "ok";
      out.className = "text-xs text-emerald-300";
      await fetchMailingPromptText(id);
    } catch (e) {
      out.textContent = e.message;
      out.className = "text-xs text-rose-400";
    }
  });
  $("#mailPromptReset").addEventListener("click", async () => {
    if (!confirm("РЈРґР°Р»РёС‚СЊ С„Р°Р№Р» system.txt Рё РІРµСЂРЅСѓС‚СЊСЃСЏ Рє РїСЂРѕРјРїС‚Сѓ РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ?")) return;
    try {
      await api(`/business/mailings/${id}/prompt`, { method: "DELETE" });
      toast("РЎР±СЂРѕС€РµРЅРѕ", "success");
      await fetchMailingPromptText(id);
    } catch (e) { toast(e.message, "error"); }
  });
}

/* ----------------------------- Clients view ---------------------------- */

async function renderClients(idStr) {
  const id = idStr ? Number(idStr) : null;
  setHeader("РљР»РёРµРЅС‚С‹", id ? `РљР»РёРµРЅС‚ #${id}` : "Р‘Р°Р·Р° РєРѕРЅС‚Р°РєС‚РѕРІ Рё С„РёР»СЊС‚СЂ РїРѕ РєР»Р°СЃСЃР°Рј");
  const root = $("#pageRoot");
  if (!id) {
    root.innerHTML = `
      <div class="p-6 cb-scroll overflow-y-auto h-full space-y-4">
        <div class="flex items-center gap-3 text-sm flex-wrap">
          <input id="clQ" placeholder="РїРѕРёСЃРє РїРѕ @username" class="px-3 py-1.5 rounded bg-ink-800 border border-ink-600 text-slate-100 w-64" />
          <select id="clClass" class="bg-ink-800 border border-ink-600 rounded px-2 py-1.5 text-slate-100">
            <option value="">РІСЃРµ РєР»Р°СЃСЃС‹</option>
            <option value="accept">accept</option>
            <option value="alive">alive</option>
            <option value="pulse">pulse</option>
            <option value="dead">dead</option>
            <option value="bl">bl</option>
            <option value="decline">decline</option>
            <option value="stop">stop</option>
            <option value="send_link">send_link</option>
          </select>
          <button id="clApply" class="px-3 py-1.5 rounded bg-accent-600 hover:bg-accent-500 text-white">РџСЂРёРјРµРЅРёС‚СЊ</button>
          <span class="text-slate-500 text-xs ml-auto" id="clCount"></span>
        </div>
        <div class="card p-0 overflow-hidden">
          <table class="cb-table">
            <thead><tr>
              <th>ID</th><th>Username</th><th>TG ID</th><th>РЎС‚Р°С‚СѓСЃ</th>
              <th>РљР»Р°СЃСЃС‹</th><th>Р”РѕР±Р°РІР»РµРЅ</th><th>РљРѕРЅС‚Р°РєС‚</th><th></th>
            </tr></thead>
            <tbody id="clBody"><tr><td colspan="8" class="text-center text-slate-500 py-8">Р—Р°РіСЂСѓР·РєР°вЂ¦</td></tr></tbody>
          </table>
        </div>
      </div>
    `;
    $("#clApply").addEventListener("click", loadClientsList);
    $("#clQ").addEventListener("keydown", (e) => { if (e.key === "Enter") loadClientsList(); });
    await loadClientsList();
    return;
  }
  root.innerHTML = `<div id="clDetail" class="p-6 cb-scroll overflow-y-auto h-full">Р—Р°РіСЂСѓР·РєР°вЂ¦</div>`;
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
    $("#clCount").textContent = `РЅР°Р№РґРµРЅРѕ: ${list.length}`;
    if (!list.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="text-center text-slate-500 py-8">РќРµС‚ РєР»РёРµРЅС‚РѕРІ РїРѕРґ С„РёР»СЊС‚СЂ.</td></tr>`;
      return;
    }
    tbody.innerHTML = list.map(c => {
      const classes = c.classes.map(x => `<span class="pill cls-${escapeHTML(x.class_key)} pill-gray" title="${x.count}">${escapeHTML(x.class_key)}:${x.count}</span>`).join(" ") || `<span class="text-slate-500">вЂ”</span>`;
      return `
        <tr>
          <td class="text-slate-500">#${c.id}</td>
          <td><a href="#/clients/${c.id}" class="text-slate-100 hover:text-accent-500">@${escapeHTML(c.username)}</a></td>
          <td class="text-xs text-slate-400">${c.telegram_user_id ?? "вЂ”"}</td>
          <td>${escapeHTML(c.status)}</td>
          <td>${classes}</td>
          <td class="text-xs text-slate-400">${fmtDate(c.added_at)}</td>
          <td class="text-xs text-slate-400">${fmtRelative(c.last_contacted_at)}</td>
          <td class="text-right">
            <button data-cl-del="${c.id}" class="px-2 py-1 rounded bg-rose-900/40 hover:bg-rose-800 text-xs text-rose-200 border border-rose-700/50">рџ—‘</button>
          </td>
        </tr>`;
    }).join("");
    tbody.querySelectorAll("button[data-cl-del]").forEach(b => {
      b.addEventListener("click", () => deleteClient(Number(b.dataset.clDel)));
    });
  } catch (e) { toast(`РљР»РёРµРЅС‚С‹: ${e.message}`, "error"); }
}

async function deleteClient(id) {
  if (!confirm(`РЈРґР°Р»РёС‚СЊ РєР»РёРµРЅС‚Р° #${id}? РљР°СЃРєР°РґРЅРѕ СѓРґР°Р»РёС‚ РєР»Р°СЃСЃС‹/С‚РµРіРё/СЃРѕР±С‹С‚РёСЏ (NeuroChat вЂ” РѕС‚РґРµР»СЊРЅРѕ).`)) return;
  try {
    await api(`/business/clients/${id}`, { method: "DELETE", raw: true });
    toast(`РљР»РёРµРЅС‚ #${id} СѓРґР°Р»С‘РЅ`, "success");
    loadClientsList();
  } catch (e) { toast(`РћС€РёР±РєР°: ${e.message}`, "error"); }
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
          <a href="#/clients" class="text-sm text-slate-400 hover:text-slate-200">в†ђ Рљ СЃРїРёСЃРєСѓ</a>
          <h2 class="text-lg text-slate-100 font-semibold">@${escapeHTML(c.username)}</h2>
          <div class="text-xs text-slate-500">tg_id ${c.telegram_user_id ?? "вЂ”"} В· ${escapeHTML(c.status)}</div>
        </div>

        <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
          ${kpi("Р”РѕР±Р°РІР»РµРЅ", "cdAdd", fmtDate(c.added_at), "")}
          ${kpi("РџРѕСЃР»РµРґ. РєРѕРЅС‚Р°РєС‚", "cdLast", fmtDate(c.last_contacted_at) || "вЂ”", "")}
          ${kpi("РЎРѕР±С‹С‚РёР№", "cdInter", String(c.interactions_count), "client_interactions")}
          ${kpi("РљР»Р°СЃСЃРѕРІ", "cdCls", String(c.classes.length), "СЃС‡С‘С‚С‡РёРєРё")}
        </div>

        <div class="card">
          <h3 class="font-semibold mb-2">РљР»Р°СЃСЃС‹</h3>
          <div id="cdClassList" class="flex flex-wrap gap-2 mb-3">
            ${c.classes.map(x => `
              <span class="pill cls-${escapeHTML(x.class_key)} pill-gray flex items-center gap-1">
                ${escapeHTML(x.class_key)}:${x.count}
                <button data-cl-cdec="${escapeHTML(x.class_key)}" class="text-rose-300">в€’</button>
                <button data-cl-cinc="${escapeHTML(x.class_key)}" class="text-emerald-300">+</button>
              </span>`).join("") || `<span class="text-slate-500">вЂ”</span>`}
          </div>
          <form id="cdAddForm" class="flex items-center gap-2 text-sm">
            <input id="cdNewKey" placeholder="РЅРѕРІС‹Р№ РєР»Р°СЃСЃ" class="px-3 py-1.5 rounded bg-ink-800 border border-ink-600 text-slate-100" />
            <input id="cdNewVal" type="number" value="1" min="1" max="9999" class="px-3 py-1.5 rounded bg-ink-800 border border-ink-600 text-slate-100 w-24" />
            <button class="px-3 py-1.5 rounded bg-accent-600 hover:bg-accent-500 text-white">Р”РѕР±Р°РІРёС‚СЊ</button>
          </form>
        </div>

        ${c.tags?.length ? `
          <div class="card">
            <h3 class="font-semibold mb-2">РўРµРіРё</h3>
            <div class="flex flex-wrap gap-2">
              ${c.tags.map(t => `<span class="pill pill-gray">${escapeHTML(t)}</span>`).join("")}
            </div>
          </div>` : ""}

        <div class="card">
          <h3 class="font-semibold mb-2">РџРѕСЃР»РµРґРЅРёРµ РІР·Р°РёРјРѕРґРµР№СЃС‚РІРёСЏ (${interactions.length})</h3>
          ${interactions.length ? `
            <div class="space-y-2 text-sm">
              ${interactions.map(it => `
                <div class="flex items-start gap-2 border-b border-ink-700/60 pb-2">
                  <span class="pill ${it.direction === 'out' ? 'pill-blue' : it.direction === 'in' ? 'pill-gray' : 'pill-amber'}">${escapeHTML(it.direction)}</span>
                  <div class="min-w-0 flex-1">
                    <div class="text-slate-300 truncate">${escapeHTML(it.kind)}${it.body ? ': ' + escapeHTML(it.body.slice(0, 200)) : ''}</div>
                    <div class="text-[11px] text-slate-500">acc#${it.account_id ?? "вЂ”"} В· mailing#${it.mailing_id ?? "вЂ”"} В· ${fmtDate(it.created_at)}</div>
                  </div>
                </div>`).join("")}
            </div>` : `<div class="text-slate-500">РЎРѕР±С‹С‚РёР№ РЅРµС‚.</div>`}
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
  setHeader("РђСЂС…РёРІ", "Р’РѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёРµ РјСЏРіРєРѕ-СѓРґР°Р»С‘РЅРЅС‹С… РґРёР°Р»РѕРіРѕРІ");
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full space-y-4 max-w-4xl">
      <div class="card">
        <p class="text-sm text-slate-400 mb-3">РџСЂРё cleanup РІ СЂРµР¶РёРјРµ <b>archive</b> (РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ РІ РќР°СЃС‚СЂРѕР№РєР°С…) СЃРѕРѕР±С‰РµРЅРёСЏ Рё СЃРѕР±С‹С‚РёСЏ РїРµСЂРµРЅРѕСЃСЏС‚СЃСЏ РІ С‚Р°Р±Р»РёС†С‹ <code>*_archive</code>. Р—РґРµСЃСЊ РјРѕР¶РЅРѕ РІРѕСЃСЃС‚Р°РЅРѕРІРёС‚СЊ РїРµСЂРµРїРёСЃРєРё РїРѕР»РЅРѕСЃС‚СЊСЋ РёР»Рё РїРѕ С„РёР»СЊС‚СЂСѓ.</p>
        <div class="flex items-center gap-3 text-sm">
          <label class="text-slate-400">РђРєРєР°СѓРЅС‚ ID:</label>
          <input id="arAcc" type="number" min="1" class="bg-ink-800 border border-ink-600 rounded px-2 py-1.5 text-slate-100 w-32" />
          <button id="arRefresh" class="px-3 py-1.5 rounded bg-accent-600 hover:bg-accent-500 text-white">РџРѕРєР°Р·Р°С‚СЊ Р°СЂС…РёРІ</button>
        </div>
      </div>
      <div class="card p-0 overflow-hidden">
        <table class="cb-table">
          <thead><tr>
            <th>РђРєРєР°СѓРЅС‚</th><th>Peer</th><th class="text-right">РЎРѕРѕР±С‰РµРЅРёР№</th><th>РђСЂС…РёРІРёСЂРѕРІР°РЅ</th><th></th>
          </tr></thead>
          <tbody id="arBody"><tr><td colspan="5" class="text-center text-slate-500 py-8">Р’РІРµРґРёС‚Рµ С„РёР»СЊС‚СЂ Рё РЅР°Р¶РјРёС‚Рµ В«РџРѕРєР°Р·Р°С‚СЊ Р°СЂС…РёРІВ».</td></tr></tbody>
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
      tbody.innerHTML = `<tr><td colspan="5" class="text-center text-slate-500 py-8">РђСЂС…РёРІ РїСѓСЃС‚.</td></tr>`;
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
            data-restore-acc="${d.account_id}" data-restore-peer="${d.peer_user_id}">в†¶ Р’РѕСЃСЃС‚Р°РЅРѕРІРёС‚СЊ</button>
        </td>
      </tr>
    `).join("");
    tbody.querySelectorAll("button[data-restore-acc]").forEach(b => {
      b.addEventListener("click", () => restoreArchive(Number(b.dataset.restoreAcc), Number(b.dataset.restorePeer)));
    });
  } catch (e) { toast(`РђСЂС…РёРІ: ${e.message}`, "error"); }
}

async function restoreArchive(accountId, peerId) {
  if (!confirm(`Р’РѕСЃСЃС‚Р°РЅРѕРІРёС‚СЊ Р°СЂС…РёРІРЅС‹Р№ РґРёР°Р»РѕРі acc=${accountId} peer=${peerId}?`)) return;
  try {
    const r = await api(`/business/archive/restore`, {
      method: "POST",
      body: { account_id: accountId, peer_user_id: peerId },
    });
    toast(`Р’РѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРѕ: СЃРѕРѕР±С‰РµРЅРёР№ ${r.messages_restored}, СЃРѕР±С‹С‚РёР№ ${r.interactions_restored}`, "success");
    loadArchive();
  } catch (e) { toast(`РћС€РёР±РєР°: ${e.message}`, "error"); }
}

/* ------------------------------- Logs view ----------------------------- */

async function renderLogs() {
  setHeader("Р›РѕРіРё", "РЎРѕР±С‹С‚РёСЏ С‚РµР»РµРјРµС‚СЂРёРё (ingest РѕС‚ Р°РіРµРЅС‚РѕРІ Р±РѕС‚Р°)");
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full space-y-4">
      <div class="flex items-center gap-3 text-sm flex-wrap">
        <label class="text-slate-400">РљР°РЅР°Р»:</label>
        <select id="logsChannel" class="bg-ink-800 border border-ink-600 rounded-md px-2 py-1 text-slate-100">
          <option value="ingest">ingest (dashboard/logs)</option>
          <option value="openrouter">openrouter (file log)</option>
        </select>
        <label class="text-slate-400">РЈСЂРѕРІРµРЅСЊ:</label>
        <select id="logsLevel" class="bg-ink-800 border border-ink-600 rounded-md px-2 py-1 text-slate-100">
          <option value="">РІСЃРµ</option>
          <option value="info">info</option>
          <option value="warning">warning</option>
          <option value="error">error</option>
          <option value="critical">critical</option>
        </select>
        <label class="text-slate-400">Р›РёРјРёС‚:</label>
        <input id="logsLimit" type="number" value="100" min="1" max="500" class="w-20 bg-ink-800 border border-ink-600 rounded-md px-2 py-1 text-slate-100" />
        <input id="logsProvider" placeholder="provider (РЅР°РїСЂ. openai)" class="hidden w-44 bg-ink-800 border border-ink-600 rounded-md px-2 py-1 text-slate-100" />
        <input id="logsModel" placeholder="model contains" class="hidden w-56 bg-ink-800 border border-ink-600 rounded-md px-2 py-1 text-slate-100" />
        <input id="logsPromptId" placeholder="prompt_id" class="hidden w-36 bg-ink-800 border border-ink-600 rounded-md px-2 py-1 text-slate-100" />
        <input id="logsQ" placeholder="РїРѕРёСЃРє РІ СЃРѕРѕР±С‰РµРЅРёРё" class="hidden w-56 bg-ink-800 border border-ink-600 rounded-md px-2 py-1 text-slate-100" />
        <button id="logsApply" class="px-3 py-1 rounded bg-accent-600 hover:bg-accent-500 text-white">РџСЂРёРјРµРЅРёС‚СЊ</button>
      </div>
      <div class="card p-0 overflow-hidden">
        <table class="cb-table">
          <thead><tr id="logsHeadRow"><th>Р’СЂРµРјСЏ</th><th>РЈСЂРѕРІРµРЅСЊ</th><th>РљР°С‚РµРіРѕСЂРёСЏ</th><th>РЎРѕРѕР±С‰РµРЅРёРµ</th></tr></thead>
          <tbody id="logsBody"><tr><td colspan="4" class="text-center text-slate-500 py-8">Р—Р°РіСЂСѓР·РєР°вЂ¦</td></tr></tbody>
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
        ? "<th>Р’СЂРµРјСЏ</th><th>РЈСЂРѕРІРµРЅСЊ</th><th>Provider/Model</th><th>Prompt</th><th>РЎРѕРѕР±С‰РµРЅРёРµ</th>"
        : "<th>Р’СЂРµРјСЏ</th><th>РЈСЂРѕРІРµРЅСЊ</th><th>РљР°С‚РµРіРѕСЂРёСЏ</th><th>РЎРѕРѕР±С‰РµРЅРёРµ</th>";
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
      tbody.innerHTML = `<tr><td colspan="4" class="text-center text-slate-500 py-8">РќРµС‚ Р·Р°РїРёСЃРµР№.</td></tr>`;
      return;
    }
    if (channel === "openrouter") {
      tbody.innerHTML = list.map(l => `
        <tr>
          <td class="text-xs text-slate-400 whitespace-nowrap">${fmtDate(l.created_at)}</td>
          <td>${logLevelPill(l.level)}</td>
          <td class="text-slate-300">
            <span class="text-slate-400">${escapeHTML(l.provider || "вЂ”")}</span>
            <span class="text-slate-500">/</span>
            <span class="text-slate-200">${escapeHTML(l.model || "вЂ”")}</span>
          </td>
          <td class="text-xs text-slate-400">${escapeHTML(l.prompt_id || "вЂ”")}</td>
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
    toast(`РћС€РёР±РєР° Р»РѕРіРѕРІ: ${e.message}`, "error");
  }
}
function logLevelPill(level) {
  const map = { info: "pill-blue", warning: "pill-amber", error: "pill-red", critical: "pill-red" };
  return `<span class="pill ${map[level] || "pill-gray"}">${escapeHTML(level)}</span>`;
}

/* ---------------------------- Settings view ---------------------------- */

async function renderSettings() {
  setHeader("РќР°СЃС‚СЂРѕР№РєРё", "Р“Р»РѕР±Р°Р»СЊРЅС‹Рµ РЅР°СЃС‚СЂРѕР№РєРё РёРЅСЃС‚Р°РЅСЃР°, РєР»СЋС‡Рё Рё РѕР±СЃР»СѓР¶РёРІР°РЅРёРµ");
  const readOnly = isReadOnlyRole();
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full space-y-6">

      <div class="card">
        <h3 class="font-semibold mb-1">РРЅСЃС‚Р°РЅСЃ</h3>
        <p class="text-sm text-slate-400 mb-4">Р“Р»РѕР±Р°Р»СЊРЅС‹Рµ РїРµСЂРµРєР»СЋС‡Р°С‚РµР»Рё Р±РѕС‚Р°. NULL РІ Р‘Р” = РёСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ Р·РЅР°С‡РµРЅРёРµ РёР· <code>.env</code> РїСЂРё СЃС‚Р°СЂС‚Рµ.</p>
        <div id="instanceCard" class="text-sm text-slate-400">Р—Р°РіСЂСѓР·РєР°вЂ¦</div>
      </div>

      <div class="card">
        <h3 class="font-semibold mb-1">РљР»СЋС‡ OpenRouter</h3>
        <p class="text-sm text-slate-400 mb-4">РЁРёС„СЂСѓРµС‚СЃСЏ Fernet-РєР»СЋС‡РѕРј РёР· <code>OPENROUTER_KEY_ENCRYPTION_KEY</code>. Р‘РµР· СЌС‚РѕР№ РїРµСЂРµРјРµРЅРЅРѕР№ Р·РЅР°С‡РµРЅРёРµ С…СЂР°РЅРёС‚СЃСЏ РІ РѕС‚РєСЂС‹С‚РѕРј РІРёРґРµ СЃ РїСЂРµС„РёРєСЃРѕРј <code>p:</code>. ${readOnly ? "Р РѕР»СЊ read-only: РёР·РјРµРЅРµРЅРёРµ СЃРµРєСЂРµС‚Р° РЅРµРґРѕСЃС‚СѓРїРЅРѕ." : ""}</p>
        <div id="orKeyCard" class="text-sm text-slate-400">Р—Р°РіСЂСѓР·РєР°вЂ¦</div>
      </div>

      <div class="card">
        <h3 class="font-semibold mb-1">РћС‡РёСЃС‚РєР° РґРёР°Р»РѕРіРѕРІ</h3>
        <p class="text-sm text-slate-400 mb-4">РЈРґР°Р»РµРЅРёРµ РІС‹РїРѕР»РЅСЏРµС‚СЃСЏ Р±Р°С‚С‡Р°РјРё СЃ РїСЂРѕРІРµСЂРєРѕР№ РѕРіСЂР°РЅРёС‡РµРЅРёР№. РЎРёСЃС‚РµРјРЅС‹Рµ С‚Р°Р±Р»РёС†С‹ (Р»РѕРіРё СЂР°СЃСЃС‹Р»РѕРє, MailingLog, Р°РєРєР°СѓРЅС‚С‹, РїСЂРѕРєСЃРё, РєР»Р°СЃСЃС‹ РєР»РёРµРЅС‚РѕРІ) <b>РЅРµ</b> СѓРґР°Р»СЏСЋС‚СЃСЏ вЂ” С‚РѕР»СЊРєРѕ РїРµСЂРµРїРёСЃРєРё Рё client_interactions. ${readOnly ? "Р РѕР»СЊ read-only: cleanup РЅРµРґРѕСЃС‚СѓРїРµРЅ." : ""}</p>
        <form id="cleanupForm" class="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
          <label class="block">
            <span class="text-slate-400 text-xs">РђРєРєР°СѓРЅС‚ ID (РѕРїС†.)</span>
            <input name="account_id" type="number" min="1" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">Peer User ID (РѕРїС†.)</span>
            <input name="peer_user_id" type="number" min="1" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">РЎС‚Р°СЂС€Рµ N РґРЅРµР№</span>
            <input name="older_than_days" type="number" min="1" max="3650" placeholder="РЅР°РїСЂРёРјРµСЂ, 30" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">РљР»Р°СЃСЃС‹ РєР»РёРµРЅС‚РѕРІ (С‡РµСЂРµР· Р·Р°РїСЏС‚СѓСЋ)</span>
            <input name="classes" type="text" placeholder="dead, bl, decline" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">Р РµР¶РёРј</span>
            <select name="mode" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
              <option value="archive" selected>archive вЂ” РїРµСЂРµРЅРѕСЃРёС‚СЊ РІ *_archive (РІРѕСЃСЃС‚Р°РЅРѕРІРёРјРѕ)</option>
              <option value="hard">hard вЂ” С„РёР·РёС‡РµСЃРєРё СѓРґР°Р»РёС‚СЊ</option>
            </select>
          </label>
          <label class="flex items-center gap-2 col-span-full text-slate-300">
            <input name="dry_run" type="checkbox" class="rounded border-ink-600 bg-ink-800" checked />
            РўРѕР»СЊРєРѕ РїРѕСЃС‡РёС‚Р°С‚СЊ (dry-run, Р±РµР· СѓРґР°Р»РµРЅРёСЏ)
          </label>
          <div class="col-span-full flex items-center gap-3">
            <button class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white" type="submit">Р—Р°РїСѓСЃС‚РёС‚СЊ</button>
            <a href="#/archive" class="text-sm text-slate-400 hover:text-slate-200">в†’ РђСЂС…РёРІ РґР»СЏ РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёСЏ</a>
            <span id="cleanupResult" class="text-sm text-slate-400"></span>
          </div>
        </form>
      </div>

      <div class="card">
        <h3 class="font-semibold mb-1">РђРєРєР°СѓРЅС‚</h3>
        <p class="text-sm text-slate-400 mb-2">РўРµРєСѓС‰РёР№ РїРѕР»СЊР·РѕРІР°С‚РµР»СЊ: <span class="text-slate-200">${escapeHTML(state.user?.username || "вЂ”")}</span></p>
        <form id="myPassForm" class="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm mb-4">
          <label class="block">
            <span class="text-slate-400 text-xs">РўРµРєСѓС‰РёР№ РїР°СЂРѕР»СЊ</span>
            <input name="current_password" type="password" autocomplete="current-password"
                   class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
          </label>
          <label class="block">
            <span class="text-slate-400 text-xs">РќРѕРІС‹Р№ РїР°СЂРѕР»СЊ</span>
            <input name="new_password" type="password" autocomplete="new-password" minlength="6"
                   class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
          </label>
          <div class="flex items-end gap-3">
            <button type="submit" class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white">РЎРјРµРЅРёС‚СЊ РїР°СЂРѕР»СЊ</button>
            <span id="myPassMsg" class="text-xs text-slate-400"></span>
          </div>
        </form>

          <div class="border-t border-ink-700 pt-4">
          <h4 class="font-medium text-slate-200 mb-2">РћРїРµСЂР°С‚РѕСЂС‹ РїР°РЅРµР»Рё</h4>
            ${readOnly ? `<p class="text-sm text-slate-500 mb-3">Р РѕР»СЊ read-only: СѓРїСЂР°РІР»РµРЅРёРµ РѕРїРµСЂР°С‚РѕСЂР°РјРё РЅРµРґРѕСЃС‚СѓРїРЅРѕ.</p>` : ""}
          <form id="opCreateForm" class="grid grid-cols-1 md:grid-cols-4 gap-3 text-sm mb-4">
            <label class="block">
              <span class="text-slate-400 text-xs">Р›РѕРіРёРЅ</span>
              <input name="username" minlength="3" required
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">РџР°СЂРѕР»СЊ</span>
              <input name="password" type="password" minlength="6" required
                     class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
            </label>
            <label class="block">
              <span class="text-slate-400 text-xs">Р РѕР»СЊ</span>
              <select name="role" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
                <option value="tenant_viewer">tenant_viewer (read-only)</option>
                <option value="tenant_admin">tenant_admin (РѕРїРµСЂР°С‚РѕСЂ)</option>
              </select>
            </label>
            <div class="flex items-end gap-3">
              <button type="submit" class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white">РЎРѕР·РґР°С‚СЊ</button>
              <span id="opCreateMsg" class="text-xs text-slate-400"></span>
            </div>
          </form>
          <div class="card p-0 overflow-hidden">
            <table class="cb-table">
              <thead><tr><th>ID</th><th>Username</th><th>Role</th><th>Tenant</th><th>РЎРѕР·РґР°РЅ</th><th></th></tr></thead>
              <tbody id="opUsersBody"><tr><td colspan="6" class="text-center text-slate-500 py-6">Р—Р°РіСЂСѓР·РєР°вЂ¦</td></tr></tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  `;

  $("#cleanupForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (readOnly) {
      toast("Р РѕР»СЊ read-only: cleanup Р·Р°РїСЂРµС‰РµРЅ", "error");
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
    out.textContent = "вЂ¦";
    try {
      const r = await api("/business/cleanup/v2", { method: "POST", body });
      const arch = (r.messages_archived || r.interactions_archived)
        ? ` В· РІ Р°СЂС…РёРІ: ${r.messages_archived}/${r.interactions_archived}`
        : "";
      out.textContent = `${body.dry_run ? "[DRY] " : ""}[${r.mode}] РґРёР°Р»РѕРіРѕРІ: ${r.dialogs_deleted}, СЃРѕРѕР±С‰РµРЅРёР№: ${r.messages_deleted}, СЃРѕР±С‹С‚РёР№: ${r.interactions_deleted}${arch}`;
      out.className = "text-sm " + (body.dry_run ? "text-amber-300" : "text-emerald-300");
      toast(body.dry_run ? "РџРѕРґСЃС‡С‘С‚ РіРѕС‚РѕРІ" : `Cleanup [${r.mode}] РІС‹РїРѕР»РЅРµРЅ`, "success");
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
      opBody.innerHTML = `<tr><td colspan="6" class="text-center text-slate-500 py-6">РќРµРґРѕСЃС‚СѓРїРЅРѕ РґР»СЏ СЂРѕР»Рё read-only.</td></tr>`;
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
  out.textContent = "вЂ¦";
  try {
    await api("/admin/me/password", { method: "POST", body });
    out.textContent = "ok";
    out.className = "text-xs text-emerald-300";
    toast("РџР°СЂРѕР»СЊ РѕР±РЅРѕРІР»С‘РЅ", "success");
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
  out.textContent = "вЂ¦";
  try {
    const r = await api("/admin/users/create", { method: "POST", body });
    out.textContent = `ok (#${r.id})`;
    out.className = "text-xs text-emerald-300";
    toast(`РџРѕР»СЊР·РѕРІР°С‚РµР»СЊ ${r.username} СЃРѕР·РґР°РЅ`, "success");
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
      tbody.innerHTML = `<tr><td colspan="6" class="text-center text-slate-500 py-6">РќРµС‚ РїРѕР»СЊР·РѕРІР°С‚РµР»РµР№.</td></tr>`;
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
            class="px-2 py-1 rounded bg-ink-700 hover:bg-ink-600 text-xs">РЎР±СЂРѕСЃ РїР°СЂРѕР»СЏ</button>
        </td>
      </tr>
    `).join("");
    tbody.querySelectorAll("button[data-op-reset]").forEach((b) => {
      b.addEventListener("click", async () => {
        const uid = Number(b.dataset.opReset);
        const uname = b.dataset.opUser || `#${uid}`;
        const pw = prompt(`РќРѕРІС‹Р№ РїР°СЂРѕР»СЊ РґР»СЏ ${uname}:`);
        if (!pw) return;
        try {
          await api(`/admin/users/${uid}/password`, {
            method: "POST",
            body: { new_password: pw },
          });
          toast(`РџР°СЂРѕР»СЊ РѕР±РЅРѕРІР»С‘РЅ: ${uname}`, "success");
        } catch (e) {
          toast(`РћС€РёР±РєР°: ${e.message}`, "error");
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
          <span class="text-slate-400 text-xs">РќРµР№СЂРѕС‡Р°С‚ РІРєР»СЋС‡С‘РЅ РіР»РѕР±Р°Р»СЊРЅРѕ</span>
          <select name="neurochat_enabled" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
            <option value="">РёР· .env (${s.neurochat_enabled_effective ? "РІРєР»СЋС‡С‘РЅ" : "РІС‹РєР»СЋС‡РµРЅ"})</option>
            <option value="true" ${s.neurochat_enabled_db === true ? "selected" : ""}>РџСЂРёРЅСѓРґРёС‚РµР»СЊРЅРѕ Р’РљР›</option>
            <option value="false" ${s.neurochat_enabled_db === false ? "selected" : ""}>РџСЂРёРЅСѓРґРёС‚РµР»СЊРЅРѕ Р’Р«РљР›</option>
          </select>
        </label>
        <label class="block">
          <span class="text-slate-400 text-xs">Р‘Р°Р·РѕРІС‹Р№ UTC-СЃРґРІРёРі РґР»СЏ {date}/{time} (С‡Р°СЃС‹)</span>
          <input name="mailing_base_utc_offset" type="number" min="-12" max="14"
                 value="${s.mailing_base_utc_offset_db ?? ""}"
                 placeholder="РёР· .env (${s.mailing_base_utc_offset_effective})"
                 class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
        </label>
        <div class="col-span-full flex items-center gap-3">
          <button class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white" type="submit">РЎРѕС…СЂР°РЅРёС‚СЊ</button>
          <button id="instanceResetBtn" type="button" class="px-3 py-2 rounded-lg bg-ink-700 hover:bg-ink-600 border border-ink-600 text-slate-200 text-sm">РЎР±СЂРѕСЃРёС‚СЊ РѕР±Р° Рє .env</button>
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
      out.textContent = "вЂ¦";
      try {
        await api("/business/instance/settings", { method: "PATCH", body });
        toast("РРЅСЃС‚Р°РЅСЃ РѕР±РЅРѕРІР»С‘РЅ", "success");
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
        toast("РЎР±СЂРѕС€РµРЅРѕ Рє .env", "success");
        await loadInstanceSettings();
      } catch (e) {
        toast(e.message, "error");
      }
    });

    const encBadge = s.openrouter_key_encrypted
      ? `<span class="text-xs px-2 py-0.5 rounded bg-emerald-700/30 text-emerald-300 border border-emerald-700/40">С€РёС„СЂ</span>`
      : (s.openrouter_key_set
        ? `<span class="text-xs px-2 py-0.5 rounded bg-amber-700/30 text-amber-300 border border-amber-700/40">plain</span>`
        : `<span class="text-xs px-2 py-0.5 rounded bg-slate-700/40 text-slate-400 border border-slate-600/40">РЅРµ Р·Р°РґР°РЅ</span>`);
    ork.innerHTML = `
      <div class="flex items-center gap-3 mb-3">
        <span class="text-slate-200 font-mono">${escapeHTML(s.openrouter_key_masked || "вЂ”")}</span>
        ${encBadge}
        ${s.openrouter_key_set ? `<button id="orKeyDelete" class="ml-auto text-xs text-rose-400 hover:text-rose-300">РЈРґР°Р»РёС‚СЊ</button>` : ""}
      </div>
      <form id="orKeyForm" class="flex items-center gap-2">
        <input name="key" type="password" placeholder="sk-or-v1-вЂ¦"
               class="flex-1 bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
        <button type="submit" class="px-3 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white text-sm">РЎРѕС…СЂР°РЅРёС‚СЊ РєР»СЋС‡</button>
      </form>
      <p class="text-xs text-slate-500 mt-2">РљР»СЋС‡ РїСЂРёРјРµРЅСЏРµС‚СЃСЏ РїСЂРё СЃР»РµРґСѓСЋС‰РµРј Р·Р°РїСЂРѕСЃРµ РЅРµР№СЂРѕС‡Р°С‚Р° (Р±РµР· СЂРµСЃС‚Р°СЂС‚Р° Р±РѕС‚Р°).</p>
    `;
    $("#orKeyForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const fd = new FormData(ev.currentTarget);
      const key = (fd.get("key") || "").toString().trim();
      if (!key) { toast("РџСѓСЃС‚РѕР№ РєР»СЋС‡", "warning"); return; }
      try {
        await api("/business/instance/openrouter-key", { method: "POST", body: { key } });
        toast("РљР»СЋС‡ СЃРѕС…СЂР°РЅС‘РЅ", "success");
        await loadInstanceSettings();
      } catch (e) { toast(e.message, "error"); }
    });
    const delBtn = $("#orKeyDelete");
    if (delBtn) {
      delBtn.addEventListener("click", async () => {
        if (!confirm("РЈРґР°Р»РёС‚СЊ РєР»СЋС‡ OpenRouter? РќРµР№СЂРѕС‡Р°С‚ РЅРµ СЃРјРѕР¶РµС‚ РіРµРЅРµСЂРёСЂРѕРІР°С‚СЊ РѕС‚РІРµС‚С‹.")) return;
        try {
          await api("/business/instance/openrouter-key", { method: "DELETE" });
          toast("РљР»СЋС‡ СѓРґР°Р»С‘РЅ", "success");
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
  setHeader("Р“СЂСѓРїРїС‹ Р°РєРєР°СѓРЅС‚РѕРІ", "РћР±СЉРµРґРёРЅРµРЅРёРµ userbot-Р°РєРєР°СѓРЅС‚РѕРІ РІ РїСѓР»С‹ РґР»СЏ СЂР°СЃСЃС‹Р»РѕРє");
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full grid grid-cols-1 lg:grid-cols-3 gap-4">
      <div class="card lg:col-span-1">
        <div class="flex items-center justify-between mb-3">
          <h3 class="font-semibold">Р“СЂСѓРїРїС‹</h3>
          <button id="grpRefresh" class="text-xs text-slate-400 hover:text-slate-200">вџі</button>
        </div>
        <form id="grpCreateForm" class="flex gap-2 mb-4">
          <input name="name" placeholder="РќР°Р·РІР°РЅРёРµ (РЅР°РїСЂРёРјРµСЂ, USA)"
                 class="flex-1 bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100 text-sm" />
          <button type="submit" class="px-3 py-2 rounded-md bg-accent-600 hover:bg-accent-500 text-white text-sm">+ РЎРѕР·РґР°С‚СЊ</button>
        </form>
        <div id="grpList" class="space-y-1 text-sm">Р—Р°РіСЂСѓР·РєР°вЂ¦</div>
      </div>
      <div class="card lg:col-span-2">
        <div id="grpDetail" class="text-slate-500 text-sm">Р’С‹Р±РµСЂРёС‚Рµ РіСЂСѓРїРїСѓ СЃР»РµРІР°, С‡С‚РѕР±С‹ РёР·РјРµРЅРёС‚СЊ СЃРѕСЃС‚Р°РІ.</div>
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
      toast(`Р“СЂСѓРїРїР° В«${g.name}В» СЃРѕР·РґР°РЅР°`, "success");
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
      list.innerHTML = `<div class="text-slate-500 text-xs">Р“СЂСѓРїРї РµС‰С‘ РЅРµС‚.</div>`;
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
      if (det) det.innerHTML = `<div class="text-slate-500 text-sm">Р’С‹Р±РµСЂРёС‚Рµ РіСЂСѓРїРїСѓ СЃР»РµРІР°, С‡С‚РѕР±С‹ РёР·РјРµРЅРёС‚СЊ СЃРѕСЃС‚Р°РІ.</div>`;
    }
  } catch (e) {
    list.innerHTML = `<div class="text-rose-400 text-sm">${escapeHTML(e.message)}</div>`;
  }
}

async function loadGroupDetail(groupId) {
  const det = $("#grpDetail");
  if (!det) return;
  det.innerHTML = `<div class="text-slate-500 text-sm">Р—Р°РіСЂСѓР·РєР°вЂ¦</div>`;
  try {
    const [groups, members, allAccounts] = await Promise.all([
      api("/business/groups"),
      api(`/business/groups/${groupId}/accounts`),
      api(`/business/accounts`),
    ]);
    const g = groups.find(x => x.id === groupId);
    if (!g) {
      det.innerHTML = `<div class="text-rose-400 text-sm">Р“СЂСѓРїРїР° РЅРµ РЅР°Р№РґРµРЅР°.</div>`;
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
          <p class="text-xs text-slate-500">id=${g.id} В· Р°РєРєР°СѓРЅС‚РѕРІ: ${g.accounts_count}</p>
        </div>
        <div class="flex gap-2">
          <button id="grpRenameBtn" class="px-3 py-1.5 rounded-md bg-ink-700 hover:bg-ink-600 text-sm">вњЋ РџРµСЂРµРёРјРµРЅРѕРІР°С‚СЊ</button>
          <button id="grpDeleteBtn" class="px-3 py-1.5 rounded-md bg-rose-700 hover:bg-rose-600 text-sm text-white">рџ—‘ РЈРґР°Р»РёС‚СЊ</button>
        </div>
      </div>
      <div class="mb-3 flex items-center justify-between">
        <span class="text-sm text-slate-400">РђРєРєР°СѓРЅС‚С‹ РІ РіСЂСѓРїРїРµ:</span>
        <button id="grpSaveBtn" class="px-3 py-1.5 rounded-md bg-accent-600 hover:bg-accent-500 text-sm text-white">рџ’ѕ РЎРѕС…СЂР°РЅРёС‚СЊ СЃРѕСЃС‚Р°РІ</button>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-1 max-h-[60vh] overflow-y-auto cb-scroll">${checkboxes}</div>
    `;
    $("#grpRenameBtn").addEventListener("click", async () => {
      const name = prompt("РќРѕРІРѕРµ РёРјСЏ РіСЂСѓРїРїС‹:", g.name);
      if (!name) return;
      try {
        await api(`/business/groups/${groupId}`, { method: "PATCH", body: { name } });
        toast("РџРµСЂРµРёРјРµРЅРѕРІР°РЅРѕ", "success");
        await loadGroupsList(groupId);
      } catch (e) { toast(e.message, "error"); }
    });
    $("#grpDeleteBtn").addEventListener("click", async () => {
      if (!confirm(`РЈРґР°Р»РёС‚СЊ РіСЂСѓРїРїСѓ В«${g.name}В»? РђРєРєР°СѓРЅС‚С‹ РІ РЅРµР№ РѕСЃС‚Р°РЅСѓС‚СЃСЏ, РЅРѕ РїРѕС‚РµСЂСЏСЋС‚ РїСЂРёРІСЏР·РєСѓ.`)) return;
      try {
        await api(`/business/groups/${groupId}`, { method: "DELETE" });
        toast("РЈРґР°Р»РµРЅРѕ", "success");
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
        toast("РЎРѕСЃС‚Р°РІ РіСЂСѓРїРїС‹ РѕР±РЅРѕРІР»С‘РЅ", "success");
        await loadGroupsList(groupId);
      } catch (e) { toast(e.message, "error"); }
    });
  } catch (e) {
    det.innerHTML = `<div class="text-rose-400 text-sm">${escapeHTML(e.message)}</div>`;
  }
}

/* ----------------------------- Proxies view ---------------------------- */

async function renderProxies() {
  setHeader("РџСЂРѕРєСЃРё", "SOCKS5/HTTP вЂ” РїСѓР»С‹ СЃРѕРµРґРёРЅРµРЅРёР№ РґР»СЏ Р°РєРєР°СѓРЅС‚РѕРІ");
  const root = $("#pageRoot");
  root.innerHTML = `
    <div class="p-6 cb-scroll overflow-y-auto h-full space-y-4">
      <div class="card">
        <div class="flex items-center justify-between mb-3">
          <h3 class="font-semibold">РЎРїРёСЃРѕРє РїСЂРѕРєСЃРё</h3>
          <div class="flex gap-2">
            <button id="prxRefresh" class="px-3 py-1.5 rounded-md bg-ink-700 hover:bg-ink-600 text-sm">вџі</button>
            <button id="prxNewBtn" class="px-3 py-1.5 rounded-md bg-accent-600 hover:bg-accent-500 text-sm text-white">+ Р”РѕР±Р°РІРёС‚СЊ</button>
          </div>
        </div>
        <div id="prxTable" class="text-sm text-slate-400">Р—Р°РіСЂСѓР·РєР°вЂ¦</div>
      </div>
      <div id="prxFormCard" class="card hidden">
        <h3 class="font-semibold mb-3" id="prxFormTitle">РќРѕРІС‹Р№ РїСЂРѕРєСЃРё</h3>
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
      tbl.innerHTML = `<div class="text-slate-500 text-xs">РџСЂРѕРєСЃРё РїРѕРєР° РЅРµС‚. Р”РѕР±Р°РІСЊС‚Рµ С‡РµСЂРµР· В«+ Р”РѕР±Р°РІРёС‚СЊВ».</div>`;
      return;
    }
    tbl.innerHTML = `
      <table class="cb-table">
        <thead>
          <tr>
            <th>ID</th><th>РРјСЏ</th><th>РўРёРї</th><th>РҐРѕСЃС‚:РџРѕСЂС‚</th>
            <th>Р›РѕРіРёРЅ</th><th>Р“СЂСѓРїРїР°</th><th>РђРєРєР°СѓРЅС‚РѕРІ</th>
            <th>РЎС‚Р°С‚СѓСЃ</th><th>РџСЂРѕРІРµСЂРєР°</th><th></th>
          </tr>
        </thead>
        <tbody>
          ${list.map(p => `
            <tr data-pid="${p.id}">
              <td class="text-slate-500">#${p.id}</td>
              <td class="font-medium text-slate-100">${escapeHTML(p.name)}</td>
              <td><span class="pill pill-gray">${p.proxy_type}</span></td>
              <td class="text-slate-300 font-mono text-xs">${escapeHTML(p.host)}:${p.port}</td>
              <td class="text-slate-400 text-xs">${escapeHTML(p.username || 'вЂ”')}</td>
              <td class="text-slate-400">${escapeHTML(p.group_name || 'вЂ”')}</td>
              <td class="text-right text-slate-300">${p.accounts_count}</td>
              <td>
                ${p.is_active
                  ? (p.is_working
                      ? `<span class="pill pill-green">ok</span>`
                      : `<span class="pill pill-red">fail</span>`)
                  : `<span class="pill pill-gray">off</span>`}
              </td>
              <td class="text-xs text-slate-500">${p.last_checked ? fmtRelative(p.last_checked) : 'вЂ”'}</td>
              <td class="text-right whitespace-nowrap">
                <button data-act="test" class="px-2 py-1 rounded bg-ink-700 hover:bg-ink-600 text-xs mr-1">РўРµСЃС‚</button>
                <button data-act="edit" class="px-2 py-1 rounded bg-ink-700 hover:bg-ink-600 text-xs mr-1">вњЋ</button>
                <button data-act="delete" class="px-2 py-1 rounded bg-rose-700 hover:bg-rose-600 text-xs text-white">рџ—‘</button>
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
        btn.disabled = true; btn.textContent = "вЂ¦";
        try {
          const r = await api(`/business/proxies/${pid}/test`, { method: "POST" });
          toast(`#${pid} ${r.ok ? 'вњ“' : 'вњ—'} ${r.elapsed_ms}ms вЂ” ${r.detail || ''}`, r.ok ? "success" : "warning");
          await loadProxiesTable();
        } catch (e) {
          toast(e.message, "error");
          btn.disabled = false; btn.textContent = "РўРµСЃС‚";
        }
      });
      tr.querySelector('[data-act="edit"]').addEventListener("click", () => {
        openProxyForm(list.find(x => x.id === pid));
      });
      tr.querySelector('[data-act="delete"]').addEventListener("click", async () => {
        if (!confirm(`РЈРґР°Р»РёС‚СЊ РїСЂРѕРєСЃРё #${pid}? РЈ Р°РєРєР°СѓРЅС‚РѕРІ, РёСЃРїРѕР»СЊР·РѕРІР°РІС€РёС… РµРіРѕ, prox_id РѕР±РЅСѓР»РёС‚СЃСЏ.`)) return;
        try {
          await api(`/business/proxies/${pid}`, { method: "DELETE" });
          toast("РЈРґР°Р»РµРЅРѕ", "success");
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
  $("#prxFormTitle").textContent = existing ? `РџСЂРѕРєСЃРё #${existing.id}` : "РќРѕРІС‹Р№ РїСЂРѕРєСЃРё";
  wrap.classList.remove("hidden");
  let groupOptions = `<option value="0">вЂ” Р±РµР· РіСЂСѓРїРїС‹ вЂ”</option>`;
  try {
    const groups = await api("/business/proxy-groups");
    groupOptions += (groups || []).map(g =>
      `<option value="${g.id}" ${existing && existing.group_id === g.id ? "selected" : ""}>${escapeHTML(g.name)} (${g.proxies_count})</option>`
    ).join("");
  } catch (_) {}
  form.innerHTML = `
    <form id="prxFormInner" class="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
      <label class="block">
        <span class="text-slate-400 text-xs">РРјСЏ</span>
        <input name="name" required value="${escapeHTML(existing?.name || "")}"
               class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
      </label>
      <label class="block">
        <span class="text-slate-400 text-xs">РҐРѕСЃС‚</span>
        <input name="host" required value="${escapeHTML(existing?.host || "")}"
               class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
      </label>
      <label class="block">
        <span class="text-slate-400 text-xs">РџРѕСЂС‚</span>
        <input name="port" type="number" min="1" max="65535" required value="${existing?.port || 1080}"
               class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
      </label>
      <label class="block">
        <span class="text-slate-400 text-xs">Р›РѕРіРёРЅ</span>
        <input name="username" value="${escapeHTML(existing?.username || "")}"
               class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
      </label>
      <label class="block">
        <span class="text-slate-400 text-xs">РџР°СЂРѕР»СЊ</span>
        <input name="password" placeholder="${existing ? "(РѕСЃС‚Р°РІРёС‚СЊ РєР°Рє РµСЃС‚СЊ)" : ""}"
               class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100" />
      </label>
      <label class="block">
        <span class="text-slate-400 text-xs">РўРёРї</span>
        <select name="proxy_type" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">
          <option value="socks5" ${(!existing || existing.proxy_type === "socks5") ? "selected" : ""}>socks5</option>
          <option value="http" ${existing && existing.proxy_type === "http" ? "selected" : ""}>http</option>
        </select>
      </label>
      <label class="block">
        <span class="text-slate-400 text-xs">Р“СЂСѓРїРїР°</span>
        <select name="group_id" class="mt-1 w-full bg-ink-800 border border-ink-600 rounded-md px-3 py-2 text-slate-100">${groupOptions}</select>
      </label>
      <label class="flex items-center gap-2 mt-6 text-slate-300">
        <input name="is_active" type="checkbox" ${(!existing || existing.is_active) ? "checked" : ""}
               class="rounded border-ink-600 bg-ink-800" />
        РђРєС‚РёРІРµРЅ
      </label>
      <div class="md:col-span-3 flex items-center gap-3 mt-2">
        <button type="submit" class="px-4 py-2 rounded-lg bg-accent-600 hover:bg-accent-500 text-white">${existing ? "РЎРѕС…СЂР°РЅРёС‚СЊ" : "РЎРѕР·РґР°С‚СЊ"}</button>
        <button type="button" id="prxFormCancel" class="px-3 py-2 rounded-lg bg-ink-700 hover:bg-ink-600 text-slate-200 text-sm">РћС‚РјРµРЅР°</button>
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
    out.textContent = "вЂ¦";
    try {
      if (existing) {
        await api(`/business/proxies/${existing.id}`, { method: "PATCH", body });
        toast("РЎРѕС…СЂР°РЅРµРЅРѕ", "success");
      } else {
        await api(`/business/proxies`, { method: "POST", body });
        toast("РЎРѕР·РґР°РЅРѕ", "success");
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
  setHeader("РќРµ РЅР°Р№РґРµРЅРѕ", "Р Р°Р·РґРµР» РЅРµ СЃСѓС‰РµСЃС‚РІСѓРµС‚");
  $("#pageRoot").innerHTML = `<div class="p-6 text-slate-400">РќРµС‚ С‚Р°РєРѕРіРѕ СЂР°Р·РґРµР»Р°.</div>`;
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
  { label: "Импорт TData ZIP", section: "Действия", ico: "📦", action: () => { window.location.hash = "#/accounts"; setTimeout(() => document.getElementById("tdataDropZone")?.scrollIntoView({ behavior: "smooth", block: "center" }), 300); } },
  { label: "Новый аккаунт", section: "Действия", ico: "➕", action: () => { window.location.hash = "#/accounts"; setTimeout(() => document.querySelector("#accountCreateForm input[name=phone]")?.focus(), 300); } },
  { label: "Новая рассылка", section: "Действия", ico: "✉️", action: () => { window.location.hash = "#/mailings"; setTimeout(() => document.querySelector("#mailingCreateForm input[name=name]")?.focus(), 300); } },
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

/* ======================= TData ZIP Import ======================= */

async function uploadTdataZip(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".zip")) {
    toast("Нужен ZIP-архив с TData", "error");
    return;
  }
  const btn = $("#tdataUploadBtn");
  const msg = $("#tdataMsg");
  if (btn) { btn.disabled = true; btn.textContent = "Импорт…"; }
  if (msg) { msg.textContent = "Конвертация выполняется, это может занять время…"; msg.className = "text-xs text-slate-400"; }
  try {
    const fd = new FormData();
    fd.append("file", file);
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
    bindTdataZone();

    if (state.token) enterApp();
    else handleLogout(true);
  } catch (err) {
    const box = document.getElementById("bootError");
    if (box) {
      box.style.display = "block";
      box.textContent = "РћС€РёР±РєР° РёРЅРёС†РёР°Р»РёР·Р°С†РёРё: " + (err && err.message ? err.message : String(err));
    }
    console.error("[bootstrap]", err);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", bootstrap);
} else {
  bootstrap();
}
