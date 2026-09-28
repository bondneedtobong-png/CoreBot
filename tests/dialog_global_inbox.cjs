const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "web-panel", "main.js"), "utf8");
const listElement = { innerHTML: "" };
let requestedUrl = "";
let refreshes = 0;
let timer;
const context = vm.createContext({
  console,
  Date,
  URLSearchParams,
  localStorage: { getItem: () => null, setItem() {} },
  window: { location: { origin: "http://localhost:8081" }, addEventListener() {} },
  document: { readyState: "loading", addEventListener() {}, querySelector() { return null; } },
  $: selector => selector === "#dlgList" ? listElement : null,
  setTimeout: callback => { timer = callback; return 1; },
  clearTimeout: () => { timer = null; },
});

vm.runInContext(source, context);
context.listElement = listElement;
context.fixture = [{
  account_id: 42,
  peer_user_id: 77,
  client_username: "<client>",
  last_message: "<script>alert(1)</script>",
  last_role: "user",
  last_message_at: null,
  messages_count: 3,
  unread_count: 1,
  waiting_for_reply: true,
}];
vm.runInContext('$ = selector => selector === "#dlgList" ? globalThis.listElement : null; api = async url => { globalThis.requestedUrl = url; return globalThis.fixture; }', context);
context.refresh = () => { refreshes += 1; };
vm.runInContext(`
  state.route = "dialogs";
  state.current.accountId = null;
  state.current.peerId = null;
  state.dialogs.peerSearch = "<query>";
  state.dialogs.waitingOnly = true;
  state.dialogs.unreadOnly = true;
  state.cache.accounts = [{ id: 42, list_label: "<Inbox>" }];
`, context);

(async () => {
  await vm.runInContext("loadDialogsList(null, null)", context);
  requestedUrl = context.requestedUrl;
  assert.match(requestedUrl, /^\/business\/dialogs\?/);
  const params = new URLSearchParams(requestedUrl.split("?")[1]);
  assert.equal(params.get("limit"), "50");
  assert.equal(params.get("offset"), "0");
  assert.equal(params.get("q"), "<query>");
  assert.equal(params.get("waiting_only"), "true");
  assert.equal(params.get("unread_only"), "true");
  assert.match(listElement.innerHTML, /#\/dialogs\/42\/77/);
  assert.match(listElement.innerHTML, /&lt;Inbox&gt;/);
  assert.match(listElement.innerHTML, /&lt;client&gt;/);
  assert.match(listElement.innerHTML, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);

  context.fixture = Array.from({ length: 50 }, () => ({ ...context.fixture[0] }));
  await vm.runInContext("loadDialogsList(null, null)", context);
  assert.match(listElement.innerHTML, /id="dlgLoadMore"/);
  await vm.runInContext("loadDialogsList(null, null, { append: true })", context);
  requestedUrl = context.requestedUrl;
  assert.equal(new URLSearchParams(requestedUrl.split("?")[1]).get("offset"), "50");
  assert.equal((listElement.innerHTML.match(/href="#\/dialogs\/42\/77"/g) || []).length, 1, "paged rows should be deduplicated");

  vm.runInContext("loadDialogsList = refresh", context);
  vm.runInContext('onLiveMessage({ role: "user", account_id: 42, peer_user_id: 77 })', context);
  assert.equal(typeof timer, "function", "global inbox should refresh on any account message");
  timer();
  timer = null;
  assert.equal(refreshes, 1);
  console.log("global dialog inbox: ok");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
