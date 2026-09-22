// /workflow 页面 JS 的无浏览器冒烟测试 (tests/test_workflow_page.py 调用; 也可以手动跑在真实导出数据上)。
//
//   node workflow_smoke.js <workflow.html> <data.json>
//
// data.json: {list: [...任务列表行], tasks: [{summary, tree}], calls: {callId: 明细}, texts: {nodeId: 全文},
//             scripts: {runId: 脚本接口返回}}
// 桩掉 DOM 与 fetch, 对每个任务跑: 渲染 / 三种展开模式 / 系统事件开关 / 每类节点的明细抽屉 / 关键时刻与异常跳转 /
// 流程条跳转与展开 / 数据依赖跳转。异步加载 (明细 / 全文 / 脚本) 里被 try/catch 吞掉的错误也会被找出来:
// 抽屉渲染完后扫一遍 DOM 里页面自己写的「请求失败：」, 再加上未处理的 Promise 拒绝。输出一行 JSON 汇总; 有错误时退出码 1。
"use strict";
const fs = require("fs"), vm = require("vm");

const [htmlPath, dataPath] = process.argv.slice(2);
const html = fs.readFileSync(htmlPath, "utf8");
const js = (html.match(/<script>([\s\S]*)<\/script>/) || [])[1];
const DATA = JSON.parse(fs.readFileSync(dataPath, "utf8"));

const els = {};
function el(id) {
  if (!els[id]) els[id] = { id, innerHTML: "", textContent: "", value: "", scrollTop: 0, offsetTop: 0, dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {}, scrollIntoView() {}, remove() {}, prepend() {} };
  return els[id];
}
let current = null;
const ctx = {
  console, setTimeout: () => 0, clearTimeout: () => {}, URLSearchParams, Date, Math, JSON, Object, Array, String,
  Number, Set, Map, Infinity, isNaN, encodeURIComponent, decodeURIComponent, Promise, RegExp, Error,
  location: { search: "" }, history: { replaceState() {} }, navigator: {}, window: { getSelection: () => null },
  document: {
    querySelector: s => el(s.replace(/^#/, "")),
    querySelectorAll: () => [],
    getElementById: id => el(id),
    createElement: () => el("tmp-" + Math.random()),
    addEventListener: () => {},
  },
  fetch: async (url) => {
    const q = new URLSearchParams(url.split("?")[1] || "");
    let body;
    if (url.startsWith("/api/workflow/tasks")) body = { tasks: DATA.list || [] };
    else if (url.startsWith("/api/workflow/task")) body = current || { error: "none" };
    else if (url.startsWith("/api/workflow/call")) body = (DATA.calls || {})[q.get("call")] || { error: "找不到这个调用" };
    else if (url.startsWith("/api/workflow/text")) body = (DATA.texts || {})[q.get("node")] || { error: "找不到这段文字" };
    else if (url.startsWith("/api/workflow/script")) body = (DATA.scripts || {})[q.get("run")] || { error: "找不到这个 workflow 的脚本" };
    else body = { error: "unknown " + url };
    return { json: async () => JSON.parse(JSON.stringify(body)) };
  },
};
vm.createContext(ctx);
vm.runInContext(js, ctx);

const errors = [];
let drawers = 0;
const seen = { stage_strip: false, band: false, dep_badge: false, script_view: false, glossary: false, full_text: false };
function run(label, fn) {
  try { fn(); } catch (e) { errors.push(`${label}: ${e && e.stack ? e.stack.split("\n").slice(0, 2).join(" | ") : e}`); }
}
// 页面只在异常时写「请求失败：」(fetch 抛错, 或 try 块里的 JS 错误被 catch 住)。工具输出里本来就可能有 TypeError 字样,
// 所以不拿错误名去扫 DOM; 同步异常由 run() 抓, 异步里漏掉的由 unhandledRejection 抓。
const BAD = /请求失败：/;
process.on("unhandledRejection", e => errors.push("unhandledRejection: " + (e && e.stack ? String(e.stack).split(/\r?\n/)[0] : e)));
function scanDom(label) {
  for (const [id, e] of Object.entries(els)) {
    const h = String(e.innerHTML || "") + String(e.textContent || "");
    const m = h.match(BAD);
    if (m) errors.push(`${label}: #${id} 里出现「${m[0]}」`);
  }
}
const tick = () => new Promise(r => setImmediate(r));

(async () => {
  await tick();
  run("renderTasks", () => vm.runInContext("renderTasks()", ctx));
  for (const t of DATA.tasks || []) {
    const name = String((t.summary && t.summary.prompt) || "").slice(0, 20);
    current = t;
    ctx.__T = t;
    run("render " + name, () => vm.runInContext("CUR = __T; SEL = __T.summary.id; MODE='default'; EXP={}; FORCE=new Set(); STAGE_ALL=false; render()", ctx));
    const replay = String(el("replay").innerHTML);
    if (replay.includes("流程（推断")) seen.stage_strip = true;
    if (replay.includes('class="r band"')) seen.band = true;
    if (replay.includes('class="fl dep"')) seen.dep_badge = true;
    for (const mode of ["all", "none", "default"]) run(`mode ${mode} ${name}`, () => vm.runInContext(`MODE='${mode}'; EXP={}; rerenderGrid()`, ctx));
    run("events " + name, () => vm.runInContext("SHOW_EVENTS=true; rerenderGrid(); SHOW_EVENTS=false", ctx));
    run("stage all " + name, () => vm.runInContext("STAGE_ALL=true; render(); STAGE_ALL=false; render()", ctx));
    const byKind = {};
    (function walk(n) { (byKind[n.kind] = byKind[n.kind] || []).push(n.id); (n.children || []).forEach(walk); })(t.tree);
    for (const [k, ids] of Object.entries(byKind)) {
      for (const id of ids.slice(0, 3)) {
        ctx.__ID = id;
        run(`drawer ${k} ${name}`, () => vm.runInContext("openDrawer(__ID)", ctx));
        drawers++;
        await tick(); await tick();
        const body = String(el("dbody").innerHTML) + String((els["script-wrap"] || {}).innerHTML || "")
                   + String((els["dtxt-wrap"] || {}).innerHTML || "");
        if (body.includes('id="sl-')) seen.script_view = true;
        if (body.includes("这是什么")) seen.glossary = true;
        if (k === "say" && (els["dtxt-wrap"] || {}).innerHTML && !String(els["dtxt-wrap"].innerHTML).includes("读取全文中")) seen.full_text = true;
        scanDom(`drawer ${k} ${name}`);
      }
    }
    for (const m of (t.summary.moments || []).slice(0, 4)) { ctx.__ID = m.ref; run("reveal " + m.kind, () => vm.runInContext("reveal(__ID)", ctx)); }
    for (const st of (t.summary.stages || []).slice(0, 4)) { ctx.__ID = st.first; run("stage jump", () => vm.runInContext("reveal(__ID)", ctx)); }
    for (const f of ["fail", "bg-fail", "retry", "loop", "slow", "huge"]) {
      ctx.__F = f;
      run("jump " + f, () => vm.runInContext("(() => { const n = firstWithFlag(CUR.tree, __F); if (n) { reveal(n.id); openDrawer(n.id); } })()", ctx));
    }
    await tick();
    scanDom("after " + name);
  }
  const out = { tasks: (DATA.tasks || []).length, drawers, errors, seen };
  console.log(JSON.stringify(out));
  process.exit(errors.length ? 1 : 0);
})();
