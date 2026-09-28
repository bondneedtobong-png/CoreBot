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
context.$ = selector => selector === "#neuroHoursForm" ? form : selector === "#neuroHoursMsg" ? output : null;
context.toast = () => {};
context.loadMailingDetail = async () => {};
context.api = async (url, options) => { calls.push({ url, ...options }); return {}; };

const submit = values => handlers.submit({ preventDefault() {}, currentTarget: { values } });

(async () => {
  vm.runInContext('state.user = { role: "operator" };', context);
  const current = { neuro_active_start_minute: 9 * 60 + 30, neuro_active_end_minute: 18 * 60 + 45, neuro_timezone: "Europe/Samara" };
  const markup = context.renderNeuroHoursSettings(current);
  assert.match(markup, /09:30/);
  assert.match(markup, /18:45/);
  assert.match(markup, /Europe\/Samara/);
  const allDayMarkup = context.renderNeuroHoursSettings({ neuro_timezone: "UTC" });
  assert.match(allDayMarkup, /Круглосуточно/);
  assert.match(allDayMarkup, /name="neuro_active_start" type="time" value=""/);
  assert.match(allDayMarkup, /name="neuro_active_end" type="time" value=""/);
  context.bindNeuroHoursForm(9);

  await submit({ neuro_active_start: "09:30", neuro_active_end: "18:45", neuro_timezone: "Europe/Samara" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/business/mailings/9");
  assert.equal(calls[0].method, "PATCH");
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)), {
    neuro_active_start_minute: 570, neuro_active_end_minute: 1125, neuro_timezone: "Europe/Samara",
  });

  await submit({ neuro_active_start: "09:30", neuro_active_end: "", neuro_timezone: "UTC" });
  assert.match(output.textContent, /оба времени/);
  await submit({ neuro_active_start: "25:00", neuro_active_end: "26:00", neuro_timezone: "UTC" });
  assert.match(output.textContent, /диапазоне/);
  await submit({ neuro_active_start: "09:30", neuro_active_end: "09:30", neuro_timezone: "UTC" });
  assert.match(output.textContent, /различаться/);
  await submit({ neuro_active_start: "", neuro_active_end: "", neuro_timezone: "" });
  assert.match(output.textContent, /часовой пояс/);
  assert.equal(calls.length, 1, "invalid values must not be sent");

  await submit({ neuro_active_start: "", neuro_active_end: "", neuro_timezone: "UTC" });
  assert.deepEqual(JSON.parse(JSON.stringify(calls[1].body)), {
    neuro_active_start_minute: null, neuro_active_end_minute: null, neuro_timezone: "UTC",
  });

  vm.runInContext('state.user = { role: "tenant_viewer" };', context);
  const viewerMarkup = context.renderNeuroHoursSettings(current);
  assert.match(viewerMarkup, /Только просмотр/);
  assert.doesNotMatch(viewerMarkup, /<form|name="neuro_timezone"/);
  console.log("neuro hours UI: ok");
})().catch(error => { console.error(error); process.exitCode = 1; });
