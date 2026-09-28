const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "..", "web-panel", "main.js"), "utf8");
const start = source.indexOf("function tdataHistoryMarkup(runs) {");
const end = source.indexOf("\nasync function loadTdataCheckHistory()", start);
assert.ok(start >= 0 && end > start, "history renderer should exist");
const rendererSource = source.slice(start, end);
const context = {
  escapeHTML: value => String(value).replace(/[&<>\"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]),
  tdataStatusPill: status => `<pill>${String(status)}</pill>`,
};
vm.createContext(context);
vm.runInContext(`${rendererSource}; this.render = tdataHistoryMarkup;`, context);

const rows = Array.from({ length: 25 }, (_, i) => ({
  run_id: i === 0 ? '" onclick="alert(1)' : `run-${i}`,
  created_at: "<script>bad</script>", status: "ok", total: i, ok_count: i,
  failed_count: 0, requested_by: "<img src=x>",
}));
const html = context.render(rows);
assert.equal((html.match(/data-history-run=/g) || []).length, 20, "render at most 20 runs");
assert.ok(html.includes("&lt;script&gt;bad&lt;/script&gt;"), "escape timestamp");
assert.ok(html.includes("&lt;img src=x&gt;"), "escape requester");
assert.ok(!html.includes('<script>'), "never emit raw HTML from API fields");
assert.ok(html.includes("&quot; onclick=&quot;alert(1)"), "escape run id in action attribute");
assert.match(context.render([]), /Проверок пока нет/);
console.log("TData history UI renderer checks passed");
