// /workflow 页面 JS 的无浏览器冒烟测试 (tests/test_workflow_page.py 调用; 也可以手动跑在真实导出数据上)。
//
//   node workflow_smoke.js <workflow.html> <data.json>
//
// data.json: {list: [...任务列表行], tasks: [{summary, tree}], calls: {callId: 明细}, texts: {nodeId: 全文},
//             scripts: {runId: 脚本接口返回}, stats: 统计接口返回, drills: {"ref|flag": 明细接口返回}}
// 桩掉 DOM 与 fetch, 对每个任务跑: 渲染 / 三种展开模式 / 系统事件开关 / 每类节点的明细抽屉 / 关键时刻与异常跳转 /
// 流程条跳转与展开 / 数据依赖跳转。S3 统计子页: 各种排序 / 维度 / 指标; 每个可点的数字 -> 追溯抽屉
// (计数类数字必须 == 明细条数) -> 点一条跳回回放 (必须打开那个任务并定位到那一步)。
// 环境变量 SMOKE_STATS_ONLY=1: 跳过逐任务回放, 只测统计子页 (真实数据导出很大时用)。异步加载 (明细 / 全文 / 脚本) 里被 try/catch 吞掉的错误也会被找出来:
// 抽屉渲染完后扫一遍 DOM 里页面自己写的「请求失败：」, 再加上未处理的 Promise 拒绝。输出一行 JSON 汇总; 有错误时退出码 1。
"use strict";
const fs = require("fs"), vm = require("vm");

const [htmlPath, dataPath] = process.argv.slice(2);
const html = fs.readFileSync(htmlPath, "utf8");
const js = (html.match(/<script>([\s\S]*)<\/script>/) || [])[1];
const DATA = JSON.parse(fs.readFileSync(dataPath, "utf8"));

const els = {};
function el(id) {
  if (!els[id]) els[id] = { id, innerHTML: "", textContent: "", value: "", scrollTop: 0, offsetTop: 0, dataset: {}, style: {},
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
    else if (url.startsWith("/api/workflow/task")) body = (DATA.tasks || []).find(t => t.summary && t.summary.id === q.get("id")) || current || { error: "none" };
    else if (url.startsWith("/api/workflow/stats")) body = DATA.stats || { error: "no stats" };
    else if (url.startsWith("/api/workflow/drill")) body = (DATA.drills || {})[q.get("ref") + "|" + (q.get("flag") || "")] || { error: "没有导出这个明细" };
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
const seen = { stage_strip: false, band: false, dep_badge: false, script_view: false, glossary: false, full_text: false,
               stats: false, dots: false, mcp_errors: false, drill_jump: false, changes: false, chg_jump: false };
let statDrills = 0, statJumps = 0;
function run(label, fn) {
  try { fn(); } catch (e) {
    const at = (e && e.stack ? e.stack.split("\n").filter(l => /\bat /.test(l))[0] : "") || "";
    errors.push(`${label}: ${(e && e.message) || e} @ ${at.trim()}`);
  }
}
// 页面只在异常时写「请求失败：」(fetch 抛错, 或 try 块里的 JS 错误被 catch 住)。工具输出里本来就可能有 TypeError 字样,
// 所以不拿错误名去扫 DOM; 同步异常由 run() 抓, 异步里漏掉的由 unhandledRejection 抓。
const BAD = /class="muted">请求失败：/;        // 只认页面自己写的那段标记 (工具输出里本来就可能出现这四个字)
process.on("unhandledRejection", e => errors.push("unhandledRejection: " + (e && e.stack ? String(e.stack).split(/\r?\n/)[0] : e)));
function scanDom(label) {
  for (const [id, e] of Object.entries(els)) {
    const h = String(e.innerHTML || "") + String(e.textContent || "");
    const m = h.match(BAD);
    if (m) errors.push(`${label}: #${id} 里出现「${m[0]}」 ${h.slice(Math.max(0, m.index - 40), m.index + 90)}`);
  }
}
const tick = () => new Promise(r => setImmediate(r));

const attrs = (html, rx) => [...String(html).matchAll(rx)].map(m => m.slice(1));
async function jumpCheck(label, task, node, kind) {
  if (!(DATA.tasks || []).some(t => t.summary && t.summary.id === task)) return;   // 这个任务没导出 (脚手架只带了几个)
  ctx.__G = [task, node, kind];
  run("goTo " + label, () => vm.runInContext("goTo(__G[0], __G[1], __G[2])", ctx));
  for (let i = 0; i < 4; i++) await tick();
  const st = vm.runInContext("({view: VIEW, sel: SEL, cur: CUR && CUR.summary.id, drawer: DRAWER, found: !!(CUR && findNode(CUR.tree, __G[1]))})", ctx);
  if (st.view !== "replay" || st.sel !== task || st.cur !== task) errors.push(`goTo ${label}: 没有切到回放里的那个任务 ${JSON.stringify(st)}`);
  else if (kind !== "task" && (!st.found || st.drawer !== node)) errors.push(`goTo ${label}: 回放里没定位到 ${node} ${JSON.stringify(st)}`);
  else { statJumps++; seen.drill_jump = true; }
  run("back to stats", () => vm.runInContext("switchView('stats')", ctx));
  await tick();
}
async function statsSmoke() {
  run("stats view", () => vm.runInContext("switchView('stats')", ctx));
  for (let i = 0; i < 3; i++) await tick();
  const html0 = String(el("stats").innerHTML);
  seen.stats = html0.includes("工具排行") && html0.includes("跨任务对照");
  seen.mcp_errors = html0.includes('class="e" data-go-task=');
  for (const [tab, keys] of [["tools", ["name", "calls", "tasks", "time", "p50", "fail", "result_est"]], ["skills", ["name", "tasks", "calls", "tokens", "time", "fail"]]])
    for (const k of keys) run(`sort ${tab} ${k}`, () => vm.runInContext(`ST_SORT[${JSON.stringify(tab)}] = ${JSON.stringify(k)}; renderStats()`, ctx));
  run("more", () => vm.runInContext("ST_MORE.tools = ST_MORE.skills = ST_MORE.cmp = true; renderStats()", ctx));
  const spans = new Map(), gos = new Map();
  const collect = () => {
    const h = String(el("stats").innerHTML);
    for (const [ref, flag, sort, title, n] of attrs(h, /<span class="num[^"]*" data-drill="([^"]*)" data-flag="([^"]*)" data-sort="([^"]*)" data-title="([^"]*)"(?: data-n="(\d+)")?/g))
      spans.set(ref + "|" + flag + "|" + sort, {ref, flag, sort, title, n});
    for (const [t, n, k] of attrs(h, /data-go-task="([^"]*)" data-go-node="([^"]*)" data-go-kind="([^"]*)"/g)) gos.set(t + "|" + n, [t, n, k]);
    if (h.includes('class="pt')) seen.dots = true;
  };
  collect();
  for (const dim of ["skill", "phase", "role", "tool"]) for (const m of ["dur", "tokens", "calls"]) {
    run(`cmp ${dim} ${m}`, () => vm.runInContext(`CMP_DIM='${dim}'; CMP_METRIC='${m}'; renderStats()`, ctx));
    collect();
  }
  scanDom("stats");
  for (const sp of spans.values()) {
    ctx.__S = sp;
    run("drill " + sp.title, () => vm.runInContext("openDrill(__S.ref, __S.flag, __S.sort, __S.title)", ctx));
    for (let i = 0; i < 3; i++) await tick();
    const tot = vm.runInContext("DRILL && DRILL.total", ctx);
    const body = String(el("dbody").innerHTML);
    const shown = (body.match(/class="dit"/g) || []).length;
    if (tot == null) { errors.push(`drill ${sp.title}: 没拿到明细 (${body.slice(0, 80)})`); continue; }
    if (sp.n != null && tot !== +sp.n) errors.push(`drill ${sp.title}: 数字 ${sp.n} ≠ 明细 ${tot} 条`);
    if (shown !== Math.min(tot, 100)) errors.push(`drill ${sp.title}: 列出 ${shown} 条, 应为 ${Math.min(tot, 100)}`);
    statDrills++;
    scanDom("drill " + sp.title);
    const first = attrs(body, /data-go-task="([^"]*)" data-go-node="([^"]*)" data-go-kind="([^"]*)"/g)[0];
    if (first) await jumpCheck("from drill " + sp.title, ...first);
  }
  for (const [t, n, k] of [...gos.values()].slice(0, 30)) await jumpCheck("from stats", t, n, k);
}

(async () => {
  await tick();
  run("renderTasks", () => vm.runInContext("renderTasks()", ctx));
  for (const t of process.env.SMOKE_STATS_ONLY ? [] : (DATA.tasks || [])) {   // 真实数据只测统计时跳过逐任务回放
    const name = String((t.summary && t.summary.prompt) || "").slice(0, 20);
    current = t;
    ctx.__T = t;
    run("render " + name, () => vm.runInContext("CUR = __T; SEL = __T.summary.id; MODE='default'; EXP={}; FORCE=new Set(); STAGE_ALL=false; render()", ctx));
    const replay = String(el("replay").innerHTML);
    if (replay.includes("流程（推断")) seen.stage_strip = true;
    if (replay.includes('class="r band"')) seen.band = true;
    if (replay.includes('class="fl dep"')) seen.dep_badge = true;
    if (replay.includes('class="chg"')) {                       // S4 改动块: 展开 / 时间线 / 点一条跳到那一步
      seen.changes = true;
      run("changes " + name, () => vm.runInContext(
        "(() => { CHG_ALL = true; CHG_TL = true; render(); CHG_TL = false; render();"
        + " const f = ((CUR.summary.changes || {}).files || [])[0]; if (f) { CHG_OPEN = new Set([f.path]); render(); } })()", ctx));
      const ids = attrs(String(el("replay").innerHTML), /data-chg="([^"]*)"/g).map(x => x[0]);
      if (ids.length) {
        ctx.__C = ids[0];
        run("chg jump " + name, () => vm.runInContext("reveal(__C); openDrawer(__C)", ctx));
        if (vm.runInContext("DRAWER", ctx) === ids[0]) seen.chg_jump = true;
        else errors.push(`改动块跳转没打开明细: ${ids[0]}`);
      }
      run("changes reset " + name, () => vm.runInContext("CHG_ALL = false; CHG_OPEN = new Set(); render()", ctx));
    }
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
  if (DATA.stats) await statsSmoke();
  const out = { tasks: (DATA.tasks || []).length, drawers, errors, seen, stat_drills: statDrills, stat_jumps: statJumps };
  console.log(JSON.stringify(out));
  process.exit(errors.length ? 1 : 0);
})();
