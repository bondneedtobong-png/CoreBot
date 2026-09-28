const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "web-panel", "main.js"), "utf8");
const handlers = {};
const calls = [];
const output = { textContent: "", className: "" };
const form = { addEventListener(name, callback) { handlers[name] = callback; } };
const context = vm.createContext({
  console, Date, URLSearchParams,
  localStorage: { getItem: () => null, setItem() {} },
  window: { location: { origin: "http://localhost:8081", hash: "" }, addEventListener() {} },
  document: { readyState: "loading", addEventListener() {}, querySelector() { return null; } },
  FormData: class { constructor(eventForm) { this.values = eventForm.values; } get(key) { return this.values[key]; } },
});
vm.runInContext(source, context);
context.$ = selector => selector === "#neuroDailyReplyLimitForm" ? form : selector === "#neuroDailyReplyLimitMsg" ? output : null;
context.toast = () => {};
context.loadMailingDetail = async () => {};
context.api = async (url, options) => { calls.push({ url, ...options }); return {}; };

const submit = values => handlers.submit({ preventDefault() {}, currentTarget: { values } });

(async () => {
  vm.runInContext('state.user = { role: "operator" };', context);
  // This object represents the mailing detail returned by GET /business/mailings/{id}.
  const fromDetail = { neuro_daily_reply_limit: 37, neuro_timezone: "Europe/Samara" };
  const markup = context.renderNeuroDailyReplyLimit(fromDetail);
  assert.match(markup, /value="37"/);
  assert.match(markup, /Europe\/Samara/);
  assert.match(markup, /локальный день/);
  assert.match(markup, /не лимит Telegram/);
  assert.match(context.renderNeuroDailyReplyLimit({ neuro_daily_reply_limit: 0 }), /value="0"/);

  context.bindNeuroDailyReplyLimitForm(9);
  await submit({ neuro_daily_reply_limit: "0" });
  await submit({ neuro_daily_reply_limit: "125" });
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url, "/business/mailings/9");
  assert.equal(calls[0].method, "PATCH");
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)), { neuro_daily_reply_limit: 0 });
  assert.deepEqual(JSON.parse(JSON.stringify(calls[1].body)), { neuro_daily_reply_limit: 125 });

  for (const invalid of ["", " ", "-1", "10001", "1.5", "2.0", "abc"]) {
    await submit({ neuro_daily_reply_limit: invalid });
    assert.match(output.textContent, /целое число от 0 до 10000/);
  }
  assert.equal(calls.length, 2, "invalid values must not be sent");

  vm.runInContext('state.user = { role: "tenant_viewer" };', context);
  const viewerMarkup = context.renderNeuroDailyReplyLimit(fromDetail);
  assert.match(viewerMarkup, /37 успешных AI-ответов/);
  assert.match(viewerMarkup, /Только просмотр/);
  assert.doesNotMatch(viewerMarkup, /<form|<button|name="neuro_daily_reply_limit"/);
  console.log("neuro reply quota UI: ok");
})().catch(error => { console.error(error); process.exitCode = 1; });
