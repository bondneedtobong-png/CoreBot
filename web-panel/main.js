const API = window.location.origin;
let token = "";

async function login() {
  const username = document.getElementById("username").value.trim();
  const password = document.getElementById("password").value;
  const r = await fetch(`${API}/auth/login`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({username, password}),
  });
  if (!r.ok) {
    document.getElementById("loginMsg").textContent = "Login failed";
    return;
  }
  const data = await r.json();
  token = data.access_token;
  document.getElementById("loginBox").classList.add("hidden");
  document.getElementById("dashboardBox").classList.remove("hidden");
  document.getElementById("agentsBox").classList.remove("hidden");
  document.getElementById("logsBox").classList.remove("hidden");
  document.getElementById("alertsBox").classList.remove("hidden");
  await refresh();
}

async function authGet(path) {
  const r = await fetch(`${API}${path}`, {
    headers: {Authorization: `Bearer ${token}`},
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

async function refresh() {
  const summary = await authGet("/dashboard/summary");
  document.getElementById("summary").textContent =
    `Active agents: ${summary.active_agents} | Events 24h: ${summary.events_24h} | Errors 24h: ${summary.errors_24h} | Open alerts: ${summary.alerts_open}`;

  const agents = await authGet("/dashboard/agents");
  document.getElementById("agents").innerHTML = agents
    .map(a => `<li>${a.name} | online=${a.is_online} | last_seen=${a.last_seen_at || "-"}</li>`)
    .join("");

  const logs = await authGet("/dashboard/logs?limit=20");
  document.getElementById("logs").innerHTML = logs
    .map(l => `<li>[${l.level}] ${l.category}: ${l.message}</li>`)
    .join("");

  const alerts = await authGet("/dashboard/alerts?limit=20");
  document.getElementById("alerts").innerHTML = alerts
    .map(a => `<li>[${a.severity}] ${a.title} (x${a.count})</li>`)
    .join("");
}

document.getElementById("loginBtn").addEventListener("click", login);
document.getElementById("refreshBtn").addEventListener("click", refresh);
