const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "web-panel", "main.js"), "utf8");
const start = source.indexOf("    const loadCatalog = async (offset = 0) => {");
const end = source.indexOf('    $("#catalogSearch").addEventListener', start);
assert.ok(start >= 0 && end > start, "catalog pagination loader exists");
const loader = source.slice(start, end);

const calls = [];
const listeners = {};
const out = {
  textContent: "", innerHTML: "",
  querySelectorAll: () => [],
  querySelector: selector => ({
    addEventListener: (_event, callback) => { listeners[selector] = callback; },
  }),
};
const nodes = {
  "#catalogKind": { value: "channels" },
  "#catalogQuery": { value: "Новости" },
  "#catalogLang": { value: "ru" },
  "#catalogMin": { value: "" },
  "#catalogMax": { value: "" },
  "#catalogActive": { checked: false },
  "#catalogDiscussion": { value: "false" },
  "#catalogResults": out,
  "#catalogSimilar": { textContent: "" },
};
const context = {
  $: selector => nodes[selector],
  URLSearchParams,
  readOnly: true,
  escapeHTML: value => String(value).replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]),
  fmtDate: () => "today",
  api: async url => {
    calls.push(url);
    const params = new URL(url, "https://panel.test").searchParams;
    const offset = Number(params.get("offset"));
    const count = offset === 0 ? 100 : 1;
    return {
      total: 101,
      items: Array.from({ length: count }, (_, index) => ({
        kind: "channel", telegram_id: offset + index + 1,
        username: `news_${offset + index + 1}`, title: "Новости",
        audience_count: 100, active_7d: false, has_discussion: false,
      })),
    };
  },
  toast: () => assert.fail("unexpected toast"),
  loadFolders: async () => {},
  openFolder: async () => {},
};
vm.createContext(context);
vm.runInContext(`${loader}\nthis.loadCatalog = loadCatalog;`, context);

(async () => {
  await context.loadCatalog();
  let params = new URL(calls[0], "https://panel.test").searchParams;
  assert.equal(params.get("has_discussion"), "false");
  assert.equal(params.get("offset"), "0");
  assert.match(out.innerHTML, /Показаны 1–100/);
  assert.ok(listeners["#catalogNext"], "next page is clickable");

  await listeners["#catalogNext"]();
  params = new URL(calls[1], "https://panel.test").searchParams;
  assert.equal(params.get("offset"), "100");
  assert.equal(params.get("has_discussion"), "false", "filters persist across pages");
  assert.match(out.innerHTML, /Показаны 101–101/);

  await listeners["#catalogPrev"]();
  params = new URL(calls[2], "https://panel.test").searchParams;
  assert.equal(params.get("offset"), "0");
  console.log("Catalog search pagination UI checks passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
