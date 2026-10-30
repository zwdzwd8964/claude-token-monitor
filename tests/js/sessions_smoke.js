// /sessions 页面 JS 的无浏览器冒烟测试 (tests/test_s1_attribution.py 调用)。CONSOLES_PLAN S1: 控制台的会话按运行折叠。
//
//   node sessions_smoke.js <sessions.html> <data.json>      data.json = 一份 /api/sessions 的返回 (+ __run, __kids, __loose)
//
// 桩掉 DOM 与 fetch, 跑: 首屏 (运行折叠、默认收起、交互会话不进折叠) / 点开 / 文字筛选后「n/总数」/ 从通知点进来自动展开。
// 输出一行 JSON 汇总; 有错误时退出码 1。
"use strict";
const fs = require("fs"), vm = require("vm");

const [htmlPath, dataPath] = process.argv.slice(2);
const html = fs.readFileSync(htmlPath, "utf8");
const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const js = scripts[scripts.length - 1];                     // 最后一段是页面本身 (前面的是导航铃铛)
const DATA = JSON.parse(fs.readFileSync(dataPath, "utf8"));
for (const s of DATA.sessions || []) if (s.session_id === DATA.__kids[0]) s.title = DATA.__kidsFilter;   // 只让一个子会话匹配文字筛选
// 这次运行自己那一段 HTML (从它的 data-run 到下一个运行那一行之前)
const runBlock = (h, run) => { const i = h.indexOf(`data-run="${run}"`), j = h.indexOf('class="row runrow', i + 1); return i < 0 ? "" : h.slice(i, j < 0 ? h.length : j); };

const els = {};
function mk(id) {
  return { id, innerHTML: "", textContent: "", value: "", checked: false, hidden: false, dataset: {}, style: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {}, setAttribute() {}, getAttribute() { return null; }, querySelector() { return null; },
    querySelectorAll() { return []; }, scrollIntoView() {} };
}
function el(id) { if (!els[id]) els[id] = mk(id); return els[id]; }
const store = {};
let hash = "";
const ctx = {
  console, setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 1, clearInterval: () => {},
  Date, Math, JSON, Object, Array, String, Number, Set, Map, Infinity, NaN, isNaN, isFinite, parseFloat, parseInt,
  encodeURIComponent, decodeURIComponent, Promise, RegExp, Error,
  location: { get hash() { return hash; } },
  localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
  window: { addEventListener() {} },
  document: {
    querySelector: s => el(s.replace(/^#/, "")),
    querySelectorAll: () => [],
    // 行在不在一个收起的运行里: 看页面自己的 OPEN (真浏览器里就是 .kids[hidden] 的祖先)
    getElementById: id => {
      const sid = id.replace(/^s-/, "");
      const row = (DATA.sessions || []).find(s => s.session_id === sid);
      if (!row || !String(el("app").innerHTML).includes(`id="${id}"`)) return null;
      const key = row.console ? row.console.code + ":" + row.console.run : null;
      return Object.assign(mk(id), {
        closest: sel => (sel === ".kids[hidden]" && key && !vm.runInContext("OPEN", ctx).has(key))
          ? { parentElement: { dataset: { run: key } } } : null,
      });
    },
  },
  fetch: async url => {
    const body = url.startsWith("/api/sessions") ? DATA : (url.startsWith("/api/events") ? { events: [], last_seq: 0 } : {});
    return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)) };
  },
};
vm.createContext(ctx);
const errors = [], seen = {};
const R = code => vm.runInContext(code, ctx);
const tick = () => new Promise(r => setImmediate(r));

(async () => {
  try { vm.runInContext(js, ctx); } catch (e) { errors.push("页面脚本: " + e.message); }
  for (let i = 0; i < 6; i++) await tick();
  const run = DATA.__run, kids = DATA.__kids, loose = DATA.__loose;
  let h = String(el("app").innerHTML);
  if ((h.match(/class="row runrow/g) || []).length < 1) errors.push("首屏: 没有画出控制台运行那一行");
  if (!h.includes(`data-run="${run}"`)) errors.push("首屏: 运行那一行的 key 不对");
  if (!/<div class="kids" hidden>/.test(runBlock(h, run))) errors.push("首屏: 运行默认应该收起 (kids hidden)");
  for (const k of kids) if (!runBlock(h, run).includes(`id="s-${k}"`)) errors.push("首屏: 子会话 " + k + " 没有折叠在运行下面");
  if (!h.includes(`id="s-${loose}"`)) errors.push("首屏: 交互会话不见了");
  if (!h.includes(`${kids.length} 个会话`)) errors.push("首屏: 运行那一行没写会话数");
  else seen.folded = true;

  R(`toggleRun(${JSON.stringify(run)})`);
  h = String(el("app").innerHTML);
  if (/<div class="kids" hidden>/.test(runBlock(h, run))) errors.push("点开: 还是收起的");
  else seen.toggle_open = true;
  if (!String(store.mc_sess_runs_open || "").includes(run)) errors.push("点开: 展开状态没记到本机");
  R(`toggleRun(${JSON.stringify(run)})`);                    // 收回去

  // 文字筛选只留下一个子会话: 运行那一行写「1/总数」, 钱和角色仍是整次运行的
  el("filter").value = DATA.__kidsFilter;
  R("render()");
  h = String(el("app").innerHTML);
  if (!h.includes(`1/${kids.length} 个会话`)) errors.push("筛选: 运行那一行应写「1/" + kids.length + "」");
  else seen.filtered_count = true;
  el("filter").value = "";
  R("render()");

  // 从「等你」通知点进来 (#s-<子会话>): 它在收起的运行里 -> 自动展开
  hash = "#s-" + kids[0];
  R("focusHash()");
  if (!R(`OPEN.has(${JSON.stringify(run)})`)) errors.push("通知点进来: 没有自动展开它所在的运行");
  else seen.hash_expand = true;

  console.log(JSON.stringify({ errors, seen }));
  process.exit(errors.length ? 1 : 0);
})().catch(e => { console.log(JSON.stringify({ errors: ["harness: " + (e && e.stack || e)], seen })); process.exit(1); });
