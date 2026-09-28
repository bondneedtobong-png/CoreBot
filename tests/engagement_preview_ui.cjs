const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "web-panel", "main.js"), "utf8");
const start = source.indexOf("async function requestEngagementPreview(");
const end = source.indexOf("\nasync function renderEngagement(", start);
assert.ok(start >= 0 && end > start, "verified engagement helpers exist");
const flow = source.slice(start, end);

async function scenario(responses) {
  const calls = [];
  const context = {
    api: async (url, options) => {
      calls.push({ url, options });
      const response = responses.shift();
      if (response instanceof Error) throw response;
      return response;
    },
    setTimeout: resolve => resolve(),
  };
  vm.createContext(context);
  vm.runInContext(`${flow}\nthis.preview = requestEngagementPreview; this.discover = discoverManagedMessages; this.draft = generateVerifiedEngagementDraft;`, context);
  return { context, calls };
}

(async () => {
  const ok = await scenario([
    { id: 12, status: "pending" },
    { id: 12, status: "processing" },
    { id: 12, status: "done", source_text: "Actual Telegram text", managed_title: "Own group" },
    { id: 55, status: "draft" },
  ]);
  const checked = await ok.context.preview(7, "https://t.me/own_group/42");
  assert.equal(checked.source_text, "Actual Telegram text");
  await ok.context.draft("comment", checked.id, "Answer the question");
  assert.deepEqual(JSON.parse(JSON.stringify(ok.calls[0].options.body)), {
    account_id: 7, message_link: "https://t.me/own_group/42",
  });
  assert.equal(ok.calls[1].url, "/business/engagement/previews/12");
  assert.equal(ok.calls[2].url, "/business/engagement/previews/12");
  assert.deepEqual(JSON.parse(JSON.stringify(ok.calls[3].options.body)), {
    mode: "comment", preview_id: 12, instruction: "Answer the question",
  });
  assert.ok(!("source_text" in ok.calls[3].options.body));
  assert.ok(!("managed_target" in ok.calls[3].options.body));

  const rejected = await scenario([
    { id: 13, status: "pending" }, { id: 13, status: "failed", error_code: "admin_required" },
  ]);
  await assert.rejects(rejected.context.preview(7, "https://t.me/foreign/42"), /admin_required/);
  assert.equal(rejected.calls.length, 2, "unmanaged target never reaches draft creation");
  const discovered = await scenario([
    { id: 27, status: "pending" },
    { id: 27, status: "done", messages: [
      { message_link: "https://t.me/c/123/42", source_text: "Question", message_id: 42 },
    ] },
  ]);
  const candidates = await discovered.context.discover(7, "https://t.me/c/123");
  assert.equal(candidates.messages[0].source_text, "Question");
  assert.deepEqual(JSON.parse(JSON.stringify(discovered.calls[0].options.body)), {
    account_id: 7, group_ref: "https://t.me/c/123",
  });
  assert.equal(discovered.calls[1].url, "/business/engagement/discoveries/27");
  assert.ok(!source.includes('id="engagementSource"'), "manual source text is absent from form");
  assert.ok(!source.includes('id="engagementManaged"'), "self-attested management checkbox is absent");
  console.log("Engagement verified-preview UI checks passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
