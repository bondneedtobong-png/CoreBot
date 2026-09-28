const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "web-panel", "main.js"), "utf8");
const start = source.indexOf("async function onTdataSessionCheckSubmit(ev) {");
const end = source.indexOf("\nfunction paintCheckForbidden", start);
assert.ok(start >= 0 && end > start, "session precheck handler should exist");
const handlerSource = source.slice(start, end);
const paintStart = source.indexOf("function paintCheckRun(box, data) {");
const paintEnd = source.indexOf("\n/* ======================= TData ZIP Import", paintStart);
assert.ok(paintStart >= 0 && paintEnd > paintStart, "check result painter should exist");
const paintSource = source.slice(paintStart, paintEnd);

function harness({ purpose = "TDATA_CHECK", file = { name: "probe.session", size: 32 }, response = { status: 200, ok: true, json: async () => ({ run_id: "r1", total: 1, ok_count: 1, failed_count: 0, items: [{ error_detail: "<img src=x>" }] }) } } = {}) {
  const nodes = {
    "#sessionCheckFile": { files: file ? [file] : [], error: "" },
    "#checkGroup": { value: "7", selectedOptions: [{ dataset: { purpose } }], error: "" },
    "#sessionCheckSubmit": {}, "#sessionCheckMsg": { textContent: "", className: "" },
    "#checkResult": { innerHTML: "" },
  };
  const calls = [];
  const context = {
    API: "https://panel.test", state: { token: "test-token" },
    $: selector => nodes[selector],
    isReadOnlyRole: () => false,
    toast: () => assert.fail("unexpected toast"),
    setFieldError: (node, value) => { if (node) node.error = value; },
    setBusy: () => {},
    escapeHTML: value => String(value).replace(/[&<>\"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]),
    paintCheckRun: null,
    kpi: (...args) => `<kpi>${args.map(String).join(" ")}</kpi>`,
    tdataStatusPill: status => `<pill>${String(status)}</pill>`,
    paintCheckForbidden: () => {},
    loadTdataCheckHistory: async () => { calls.push({ history: true }); },
    FormData: class { constructor() { this.fields = []; } append(...args) { this.fields.push(args); } },
    fetch: async (...args) => { calls.push(args); return response; },
    console,
  };
  vm.createContext(context);
  vm.runInContext(`${paintSource}; this.paint = paintCheckRun; ${handlerSource}; this.submit = onTdataSessionCheckSubmit;`, context);
  context.paintCheckRun = context.paint;
  return { context, nodes, calls };
}

(async () => {
  const badPool = harness({ purpose: "ACCOUNT_RUNTIME" });
  await badPool.context.submit({ preventDefault() {} });
  assert.equal(badPool.calls.length, 0, "wrong-purpose pool must not send a request");
  assert.match(badPool.nodes["#checkGroup"].error, /TDATA_CHECK/);

  const oversize = harness({ file: { name: "large.session", size: 16 * 1024 * 1024 + 1 } });
  await oversize.context.submit({ preventDefault() {} });
  assert.equal(oversize.calls.length, 0, "oversize session must not send a request");
  assert.match(oversize.nodes["#sessionCheckFile"].error, /16 МиБ/);

  const good = harness();
  await good.context.submit({ preventDefault() {} });
  const request = good.calls.find(call => Array.isArray(call));
  assert.equal(request[0], "https://panel.test/business/tdata/check-session");
  assert.equal(request[1].method, "POST");
  assert.equal(request[1].headers.Authorization, "Bearer test-token");
  assert.equal(request[1].body.fields[0][0], "file");
  assert.equal(request[1].body.fields[1][0], "group_id");
  assert.ok(!request[0].includes("import"), "precheck must not call import endpoint");
  assert.ok(good.nodes["#checkResult"].innerHTML.includes("&lt;img src=x&gt;"), "API result fields are escaped by the shared renderer");
  assert.ok(good.calls.some(call => call.history), "history refreshes after success");

  const unsafe = harness({ response: { status: 500, ok: false, json: async () => ({ detail: "<script>alert(1)</script>" }) } });
  await unsafe.context.submit({ preventDefault() {} });
  assert.ok(unsafe.nodes["#checkResult"].innerHTML.includes("&lt;script&gt;"), "server error text is escaped");
  assert.ok(!unsafe.nodes["#checkResult"].innerHTML.includes("<script>"));

  const interrupted = harness({ response: { status: 200, ok: true, json: async () => ({ run_id: "broken", status: "interrupted", total: 0, ok_count: 0, failed_count: 0, error_code: "<stop>" }) } });
  await interrupted.context.submit({ preventDefault() {} });
  assert.match(interrupted.nodes["#checkResult"].innerHTML, /pill-red/);
  assert.doesNotMatch(interrupted.nodes["#checkResult"].innerHTML, /empty — tdata не найдены/);
  assert.ok(interrupted.nodes["#checkResult"].innerHTML.includes("&lt;stop&gt;"), "error code is escaped");
  console.log("Session precheck UI checks passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
