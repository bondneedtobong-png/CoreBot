const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const source = fs.readFileSync(path.join(__dirname, "..", "web-panel", "main.js"), "utf8");
assert.ok(source.includes('await api("/auth/stream-session", { method: "POST" })'));
assert.ok(source.includes('`${API}/business/stream`'));
assert.ok(source.includes('`${API}/business/parsing/stream`'));
assert.ok(!source.includes("stream?token="));
console.log("SSE uses an authenticated cookie without JWT in the URL");
