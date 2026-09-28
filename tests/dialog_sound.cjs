const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "web-panel", "main.js"), "utf8");
const values = new Map();
let tones = 0;
let refreshed = 0;
let timer;
const context = vm.createContext({
  console,
  Date,
  localStorage: {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  },
  window: {
    location: { origin: "http://localhost:8081" },
    addEventListener() {},
    AudioContext: class {
      state = "running";
      currentTime = 0;
      destination = {};
      createOscillator() {
        return {
          type: "",
          frequency: { value: 0 },
          connect() {},
          start() { tones += 1; },
          stop() {},
        };
      }
      createGain() {
        return {
          gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} },
          connect() {},
        };
      }
    },
  },
  document: {
    readyState: "loading",
    addEventListener() {},
    querySelector() { return null; },
  },
  setTimeout: callback => { timer = callback; return 1; },
  clearTimeout: () => { timer = null; },
});

vm.runInContext(source, context);
context.refresh = () => { refreshed += 1; };
vm.runInContext(`
  state.route = "dialogs";
  state.current.accountId = 5;
  state.current.peerId = null;
  state.dialogs.soundEnabled = true;
  loadDialogsList = refresh;
`, context);

vm.runInContext('onLiveMessage({ role: "user", account_id: 5, peer_user_id: 9 })', context);
assert.equal(tones, 1, "incoming event should sound");
assert.equal(typeof timer, "function", "selected account should refresh");
timer();
timer = null;
assert.equal(refreshed, 1);

vm.runInContext('onLiveMessage({ role: "assistant", account_id: 5, peer_user_id: 9 })', context);
assert.equal(tones, 1, "outgoing event must remain silent");
timer();
timer = null;
assert.equal(refreshed, 2);

vm.runInContext('onLiveMessage({ role: "user", account_id: 6, peer_user_id: 9 })', context);
assert.equal(timer, null, "other account must not refresh selected list");
assert.equal(tones, 1, "bursts should be throttled");

vm.runInContext('state.dialogs.soundEnabled = false; onLiveMessage({ role: "user", account_id: 5, peer_user_id: 9 })', context);
assert.equal(tones, 1, "disabled sound must remain silent");
console.log("dialog sound: ok");
