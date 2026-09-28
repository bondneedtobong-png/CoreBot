const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "web-panel", "main.js"), "utf8");
const context = vm.createContext({
  console,
  localStorage: { getItem: () => null, setItem() {} },
  window: { location: { origin: "http://localhost:8081", hash: "" }, addEventListener() {} },
  document: { readyState: "loading", addEventListener() {}, querySelector() { return null; } },
});
vm.runInContext(source, context);

const clean = value => JSON.parse(JSON.stringify(value));
assert.deepEqual(clean(context.parserSizeFilters("groups", "100", "5000")), { members_min: 100, members_max: 5000 });
assert.deepEqual(clean(context.parserSizeFilters("channels", "0", "")), { subscribers_min: 0, subscribers_max: null });
assert.deepEqual(clean(context.parserSizeFilters("groups", "", "")), { members_min: null, members_max: null });
assert.throws(() => context.parserSizeFilters("groups", "10", "9"), /минимум/);
assert.throws(() => context.parserSizeFilters("groups", "1.5", ""), /целое/);
assert.throws(() => context.parserSizeFilters("groups", "-1", ""), /целое/);
assert.match(source, /tab === "groups" \? "Участники" : "Подписчики"/);
assert.match(source, /\.\.\.parserSizeFilters\(tab,/);
console.log("parser group size UI: ok");
