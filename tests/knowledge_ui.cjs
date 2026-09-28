const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "web-panel", "main.js"), "utf8");
const handlers = {};
const calls = [];
const classes = new Set(["hidden"]);
const field = value => ({ value, checked: false, focus() {} });
const form = {
  dataset: {}, elements: { title: field(""), content: field(""), keywords: field(""), enabled: field("",) },
  classList: { add: value => classes.add(value), remove: value => classes.delete(value) },
  addEventListener: (name, callback) => { handlers[`form:${name}`] = callback; },
  reset() { this.elements.title.value = ""; this.elements.content.value = ""; this.elements.keywords.value = ""; this.elements.enabled.checked = true; },
};
form.elements.enabled.checked = true;
const newButton = { addEventListener: (name, callback) => { handlers[`new:${name}`] = callback; } };
const cancelButton = { addEventListener: (name, callback) => { handlers[`cancel:${name}`] = callback; } };
const message = { textContent: "", className: "" };
const list = {
  innerHTML: "", textContent: "", buttons: {},
  querySelectorAll(selector) { return this.buttons[selector] || []; },
};
const context = vm.createContext({
  console, Date, URLSearchParams,
  localStorage: { getItem: () => null, setItem() {} },
  window: { location: { origin: "http://localhost:8081", hash: "" }, addEventListener() {} },
  document: { readyState: "loading", addEventListener() {}, querySelector() { return null; } },
  confirm: () => true,
  setTimeout, clearTimeout,
});
vm.runInContext(source, context);
context.$ = selector => ({
  "#mailKnowledgeList": list,
  "#mailKnowledgeForm": form,
  "#mailKnowledgeNew": newButton,
  "#mailKnowledgeCancel": cancelButton,
  "#mailKnowledgeMsg": message,
}[selector] || null);
context.toast = () => {};
context.fixture = [{ id: 5, title: "<Oferta>", content: "<script>unsafe()</script> справка", keywords: ["тариф", "цена"], enabled: true }];
context.api = async (url, options = {}) => {
  calls.push({ url, ...options });
  if (!options.method || options.method === "GET") return context.fixture;
  if (options.method === "POST") context.fixture = [...context.fixture, { id: 6, ...options.body }];
  if (options.method === "PATCH") context.fixture = context.fixture.map(item => item.id === 5 ? { ...item, ...options.body } : item);
  if (options.method === "DELETE") context.fixture = context.fixture.filter(item => item.id !== 5);
  return null;
};

function button(selector, dataset) {
  const value = { dataset, disabled: false, addEventListener(name, callback) { this.handler = callback; } };
  list.buttons[selector] = [value];
  return value;
}

(async () => {
  vm.runInContext('state.user = { role: "operator" }; mailingPromptUi.mailingId = 9;', context);
  await context.loadMailingKnowledge(9);
  assert.match(list.innerHTML, /&lt;Oferta&gt;/, "titles must be escaped");
  assert.match(list.innerHTML, /&lt;script&gt;unsafe\(\)&lt;\/script&gt;/, "content must be escaped");
  assert.match(list.innerHTML, /Ключевые слова: тариф, цена/);
  assert.match(source, /Семантического поиска нет/);
  assert.equal(calls[0].url, "/business/mailings/9/knowledge");

  handlers["new:click"]();
  form.elements.title.value = "Оплата";
  form.elements.content.value = "Ответ про оплату";
  form.elements.keywords.value = "оплата, тариф";
  await handlers["form:submit"]({ preventDefault() {} });
  const create = calls.find(call => call.method === "POST");
  assert.equal(create.url, "/business/mailings/9/knowledge");
  assert.deepEqual(JSON.parse(JSON.stringify(create.body)), { title: "Оплата", content: "Ответ про оплату", keywords: ["оплата", "тариф"], enabled: true });

  const toggle = button("[data-knowledge-toggle]", { knowledgeToggle: "5", enabled: "1" });
  list.buttons["[data-knowledge-edit]"] = [];
  list.buttons["[data-knowledge-delete]"] = [];
  context.renderMailingKnowledge(context.fixture);
  await toggle.handler({ currentTarget: toggle });
  assert(calls.some(call => call.url.endsWith("/knowledge/5") && call.method === "PATCH" && call.body.enabled === false));

  const remove = button("[data-knowledge-delete]", { knowledgeDelete: "5" });
  list.buttons["[data-knowledge-edit]"] = [];
  list.buttons["[data-knowledge-toggle]"] = [];
  context.renderMailingKnowledge(context.fixture);
  await remove.handler();
  assert(calls.some(call => call.url.endsWith("/knowledge/5") && call.method === "DELETE"));

  const edit = button("[data-knowledge-edit]", { knowledgeEdit: "6" });
  list.buttons["[data-knowledge-delete]"] = [];
  list.buttons["[data-knowledge-toggle]"] = [];
  context.renderMailingKnowledge(context.fixture);
  edit.handler();
  assert.equal(form.elements.title.value, "Оплата");

  handlers["new:click"]();
  form.elements.title.value = "Общая справка";
  form.elements.content.value = "Часы работы";
  form.elements.keywords.value = "";
  await handlers["form:submit"]({ preventDefault() {} });
  const general = calls.filter(call => call.method === "POST").at(-1);
  assert.deepEqual(JSON.parse(JSON.stringify(general.body.keywords)), []);

  vm.runInContext('state.user = { role: "tenant_viewer" };', context);
  context.renderMailingKnowledge(context.fixture);
  assert.doesNotMatch(list.innerHTML, /data-knowledge-(?:edit|toggle|delete)=/);
  console.log("mailing knowledge UI: ok");
})().catch(error => { console.error(error); process.exitCode = 1; });
