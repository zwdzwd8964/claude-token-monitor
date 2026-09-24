// /tokens 页面 JS 的无浏览器冒烟测试 (tests/test_tokens_page.py 调用; 也可以手动跑在真实导出的数据上)。
//
//   node tokens_smoke.js <tokens.html> <data.json>
//
// data.json: {cubes: {"7d": 立方, "today": ..., "24h": ..., "2w": ..., "all": ..., "empty": 空立方}, budget: /api/budget 返回}
// 桩掉 DOM 与 fetch, 依次跑: 首屏渲染 / $ ↔ tokens / 四种堆叠 / 每一种筛选 (点出来的 data-f 值) 且 KPI 跟着变 /
// 多选 (同维度 OR) / 「其他」多值 / 框选 / 窗口切换 (小时粒度、无基线) / 预算线 / URL 往返 / 恶意 URL / 空数据 / 标签转义 / 自己对账。
// 审查修复的回归: 原型链键 / 不存在的日期 / 触屏滑动留下的半截框选 / 切窗口时请求在途或失败 / 今天按星期筛的假基线 /
// 按时段筛时不外推 / 累计曲线悬停坐标 / 只有未知单价 token 的项目 ($ 视图不能悄悄消失) / 刻度与百分比格式。
// 输出一行 JSON 汇总; 有错误时退出码 1。**绝不**会 POST 任何东西 (桩里的 POST 一律记为错误)。
"use strict";
const fs = require("fs"), vm = require("vm");

const [htmlPath, dataPath] = process.argv.slice(2);
const html = fs.readFileSync(htmlPath, "utf8");
const js = (html.match(/<script>([\s\S]*)<\/script>/) || [])[1];
const DATA = JSON.parse(fs.readFileSync(dataPath, "utf8"));

const els = {};
function mk(id) {
  return { id, innerHTML: "", textContent: "", value: "", checked: false, hidden: false, dataset: {}, style: {}, open: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {}, setAttribute() {}, getAttribute() { return null; }, appendChild() {}, querySelector() { return null; },
    querySelectorAll() { return []; }, getBoundingClientRect() { return { left: 0, top: 0, width: 0, height: 0 }; } };
}
function el(id) { if (!els[id]) els[id] = mk(id); return els[id]; }
const urls = [], posts = [];
const docL = {};                          // 页面挂在 document 上的监听 (pointerdown / up / cancel ...), 测试里直接派发
const fire = (type, ev) => { for (const f of docL[type] || []) f(ev); };
let search = "";
const ctx = {
  console, setTimeout: (f) => { f(); return 0; }, clearTimeout: () => {}, setInterval: () => 1, clearInterval: () => {},
  requestAnimationFrame: (f) => { f(); return 1; },
  URLSearchParams, Date, Math, JSON, Object, Array, String, Number, Set, Map, Infinity, NaN, isNaN, isFinite, parseFloat, parseInt,
  encodeURIComponent, decodeURIComponent, Promise, RegExp, Error,
  location: { get search() { return search; }, pathname: "/tokens" },
  history: { replaceState(_a, _b, u) { urls.push(u); } },
  localStorage: { getItem: () => "", setItem() {}, removeItem() {} },
  prompt: () => { posts.push("prompt() 被调用了 (测试不该触发保存预算)"); return null; },
  navigator: {}, window: { addEventListener() {}, innerWidth: 1440, innerHeight: 900 },
  document: {
    querySelector: s => el(s.replace(/^#/, "")),
    querySelectorAll: () => [],
    getElementById: id => el(id),
    createElement: () => mk("tmp"),
    createTextNode: t => ({ t }),
    addEventListener: (t, f) => { (docL[t] = docL[t] || []).push(f); },
    documentElement: { addEventListener() {} },
    body: mk("body"),
  },
  fetch: async (url, opts) => {
    if (opts && opts.method && opts.method !== "GET") { posts.push(url); throw new Error("测试里禁止写请求: " + url); }
    let body;
    if (url.startsWith("/api/tokens/cube") && ctx.__failCube) return { ok: false, status: 500, json: async () => ({ error: "boom" }) };
    if (url.startsWith("/api/tokens/cube")) {
      const q = new URLSearchParams(url.split("?")[1] || "");
      body = DATA.cubes[ctx.__cubeKey || q.get("since")] || DATA.cubes["7d"];
    } else if (url.startsWith("/api/budget")) body = DATA.budget || { budgets: [] };
    else body = { error: "unknown " + url };
    return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)) };
  },
};
vm.createContext(ctx);
vm.runInContext(js, ctx);

const errors = [];
const seen = {};
const R = (code) => vm.runInContext(code, ctx);
function run(label, fn) {
  try { fn(); } catch (e) {
    const at = (e && e.stack ? e.stack.split("\n").filter(l => /\bat /.test(l))[0] : "") || "";
    errors.push(`${label}: ${(e && e.message) || e} @ ${at.trim()}`);
  }
}
const tick = () => new Promise(r => setImmediate(r));
async function settle() { for (let i = 0; i < 6; i++) await tick(); }
function renderErrors(label) {
  const re = R("RENDER_ERRORS.splice(0)");
  for (const e of re) errors.push(`${label}: 渲染出错 ${e}`);
}
const CHARTS = ["k-cost", "k-tok", "k-unit", "k-hit", "k-out", "k-n", "burn", "comp", "pdelta", "days", "heat", "tree", "pareto", "models", "tables", "foot"];
const all = () => CHARTS.map(id => String(el(id).innerHTML)).join("\n") + String(el("fx").innerHTML);
const unesc = s => String(s).replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
function marks() {                       // 页面上所有「可点筛选」的 (维度, 值) —— 就是点击会用到的那份属性
  const out = [];
  for (const m of all().matchAll(/data-f="([a-z]+)" data-v="([^"]*)"/g)) out.push([m[1], unesc(m[2])]);
  for (const m of all().matchAll(/data-f="([a-z]+)" data-vs="([^"]*)"/g)) out.push([m[1], unesc(m[2]), true]);
  return out;
}
function checkEscaping(label) {
  const h = all();
  if (h.includes("<img src=x>")) errors.push(`${label}: 项目名里的 <img src=x> 没有被转义就进了 HTML`);
  if (h.includes("&lt;img src=x&gt;")) seen.escaped = true;
}

(async () => {
  await settle();
  renderErrors("首屏");
  for (const id of ["burn", "comp", "days", "heat", "tree", "pareto", "models"]) {
    if (!String(el(id).innerHTML).includes("<svg")) errors.push(`首屏: #${id} 没有画出 SVG`);
  }
  seen.first = String(el("k-cost").innerHTML).includes("$");
  seen.reconciled = String(el("foot").innerHTML).includes("已对账");
  seen.callout_comp = String(el("comp").innerHTML).includes("的钱");
  seen.callout_heat = String(el("heat").innerHTML).includes("00:00–06:59");
  seen.callout_pareto = String(el("pareto").innerHTML).includes("第 1 名");
  seen.baseline_chip = /chip (good|bad|flat|neu)/.test(String(el("k-cost").innerHTML));
  seen.wf_link = String(el("tree").innerHTML).includes("/workflow?project=");
  checkEscaping("首屏");

  // $ ↔ tokens
  const d0 = String(el("days").innerHTML);
  run("metric tokens", () => R("setMetric('tokens')"));
  seen.metric = String(el("days").innerHTML) !== d0 && R("ST.metric") === "tokens" && urls[urls.length - 1].includes("metric=tokens");
  run("metric cost", () => R("setMetric('cost')"));
  renderErrors("metric");

  // 四种堆叠
  for (const st of ["model", "source", "project", "component"]) {
    run("stack " + st, () => R(`setStack('${st}')`));
    const h = String(el("days").innerHTML);
    if (st === "source" && !h.includes("主会话")) errors.push("按来源堆叠: 图例里没有「主会话」");
    if (st === "model" && !h.includes("claude-opus-5")) errors.push("按模型堆叠: 图例里没有模型");
  }
  renderErrors("stack");

  // 每一种筛选: 取页面上真实可点的值, 应用, KPI 必须跟着变 (成分筛选除外时也会变), 然后清掉
  const kpi0 = String(el("k-cost").innerHTML) + String(el("k-n").innerHTML);
  const byDim = {};
  for (const [dim, v, multi] of marks()) if (!byDim[dim] && !multi) byDim[dim] = v;
  for (const [dim, v] of Object.entries(byDim)) {
    ctx.__D = dim; ctx.__V = v;
    run("filter " + dim, () => R("applyFilter(__D, [parseVal(__D, __V)], false)"));
    const size = R("F[__D].size");
    if (size !== 1) errors.push(`筛选 ${dim}=${v}: 没有生效 (size=${size})`);
    const k1 = String(el("k-cost").innerHTML) + String(el("k-n").innerHTML);
    if (k1 === kpi0) errors.push(`筛选 ${dim}=${v}: KPI 没有跟着变`);
    if (!String(el("fx").innerHTML).includes('class="fchip"')) errors.push(`筛选 ${dim}: 顶栏没有出现筛选芯片`);
    checkEscaping("筛选 " + dim);
    run("clear " + dim, () => R("clearFilters()"));
    seen["f_" + dim] = true;
  }
  renderErrors("filters");
  if ((String(el("k-cost").innerHTML) + String(el("k-n").innerHTML)) !== kpi0) errors.push("清除筛选后 KPI 没有恢复");

  // 同维度多选 = OR; 跨维度 = AND
  const projects = [...new Set(marks().filter(m => m[0] === "p" && !m[2]).map(m => m[1]))];
  if (projects.length >= 2) {
    ctx.__A = projects[0]; ctx.__B = projects[1];
    run("multi p", () => R("applyFilter('p', [__A], false); applyFilter('p', [__B], true)"));
    const n2 = R("view(R).length"), na = R("R.filter(r => r.p === __A).length"), nb = R("R.filter(r => r.p === __B).length");
    if (n2 !== na + nb) errors.push(`项目多选不是 OR: ${n2} != ${na}+${nb}`);
    run("and s", () => R("applyFilter('s', ['main'], false)"));
    const n3 = R("view(R).length"), want = R("R.filter(r => (r.p === __A || r.p === __B) && r.s === 'main').length");
    if (n3 !== want) errors.push(`跨维度不是 AND: ${n3} != ${want}`);
    seen.multi = true;
    // URL 往返
    const u = urls[urls.length - 1];
    const before = R("JSON.stringify(Object.fromEntries(Object.entries(F).map(([k, s]) => [k, [...s].sort()])))");
    run("clear for restore", () => R("clearFilters()"));
    ctx.__U = u.split("?")[1] || "";
    run("restore", () => R("restore('?' + __U)"));
    const after = R("JSON.stringify(Object.fromEntries(Object.entries(F).map(([k, s]) => [k, [...s].sort()])))");
    if (before !== after) errors.push(`URL 往返丢了状态: ${before} -> ${after}`);
    else seen.url_roundtrip = true;
    run("clear", () => R("clearFilters()"));
  }
  // 「其他」这种多值标记 (data-vs): 按模型堆叠时, 排名第 5 起的模型合成「其他模型」
  run("stack model for vs", () => R("setStack('model')"));
  const multi = marks().find(m => m[2]);
  run("stack back", () => R("setStack('component')"));
  if (multi) {
    ctx.__M = multi;
    run("data-vs", () => R("(() => { const el = {getAttribute: n => n === 'data-f' ? __M[0] : (n === 'data-vs' ? __M[1] : null)}; applyFilter(__M[0], valsOf(el), false); })()"));
    if (R("F[__M[0]].size") < 2) errors.push("「其他」多值筛选没有展开成多个值");
    else seen.multi_vs = true;
    run("clear", () => R("clearFilters()"));
  }
  // 框选
  run("brush", () => R("brushSelect('d', 0, 2, false)"));
  if (R("F.d.size") !== 3) errors.push("框选 3 天后日期筛选不是 3 个值");
  else seen.brush = true;
  if (!String(el("pdelta").innerHTML).includes("不比较")) errors.push("按日期筛选时「变化最大的项目」应说明不比较");
  run("clear", () => R("clearFilters()"));
  // 真实的指针路径: 按下 -> 拖动 -> 松开 (松开时全局 BRUSH 已清空, 不能再读它)
  run("brush via pointer", () => R("BRUSH = {svg: {querySelector: () => null}, dim: 'd', pl: 44, slot: 20, n: 7, rect: {left: 0}, x0: 50, moved: false, add: false};"
    + " onMove({clientX: 80, target: null}); onUp({clientX: 110})"));
  if (R("F.d.size") !== 4) errors.push(`指针框选 (按下 50px -> 松开 110px, 每格 20px) 应选中 4 天, 实际 ${R("F.d.size")}`);
  else seen.brush_pointer = true;
  run("clear", () => R("clearFilters()"));
  // 触屏: 在「按天」图上按下 -> 浏览器接管成滚动 (pointercancel, 没有 pointerup) -> 再点别处。不能套上一个没人要的日期范围
  ctx.__brushSvg = { getAttribute: n => ({ "data-dim": "d", "data-pl": "44", "data-slot": "20", "data-n": "7" })[n] || null,
                     getBoundingClientRect: () => ({ left: 0, top: 0 }), querySelector: () => null };
  const onBrush = { closest: sel => sel === "svg.brush" ? ctx.__brushSvg : null };
  const elsewhere = { closest: () => null };
  run("touch cancel", () => {
    fire("pointerdown", { target: onBrush, clientX: 50, clientY: 10, button: 0, pointerType: "touch" });
    fire("pointercancel", { pointerType: "touch" });
    fire("pointerdown", { target: elsewhere, clientX: 200, clientY: 10, button: 0, pointerType: "touch" });
    fire("pointerup", { target: elsewhere, clientX: 200, clientY: 10, button: 0, pointerType: "touch" });
  });
  if (R("F.d.size") !== 0 || R("SUPPRESS")) errors.push(`触屏滑动被取消后再点别处: 不该出现日期筛选 (F.d=${R("F.d.size")}, SUPPRESS=${R("SUPPRESS")})`);
  // 同样的事, 但浏览器连 pointercancel 都没发: 按在别处时也要丢掉旧框选
  run("stale brush", () => {
    fire("pointerdown", { target: onBrush, clientX: 50, clientY: 10, button: 0, pointerType: "mouse" });
    fire("pointerdown", { target: elsewhere, clientX: 200, clientY: 10, button: 0, pointerType: "mouse" });
    fire("pointerup", { target: elsewhere, clientX: 200, clientY: 10, button: 0, pointerType: "mouse" });
  });
  if (R("F.d.size") !== 0) errors.push(`上一次没收尾的框选被带到了下一次点击: F.d=${R("F.d.size")}`);
  else seen.touch_cancel = true;
  run("clear", () => R("SUPPRESS = false; clearFilters()"));
  // 累计曲线悬停: overlay 自己从 x=PL 开始, 右边缘 = 窗口终点, 左边缘 = 起点 (不能再减一次 PL)
  const hvR = R("burnHover(BURN.pw, {left: 0}).split('\\n')[0]"), hvL = R("burnHover(0, {left: 0}).split('\\n')[0]");
  const wantR = R("fmtTime(BURN.x1, true)"), wantL = R("fmtTime(BURN.lo, true)");
  if (!String(hvR).startsWith(wantR) || !String(hvL).startsWith(wantL)) errors.push(`累计曲线悬停坐标偏了: 右边缘 ${hvR} (应 ${wantR}), 左边缘 ${hvL} (应 ${wantL})`);
  else seen.burn_hover = true;
  // 只有未知单价 token 的项目: $ 视图下三张图都不能让它悄悄消失
  ctx.__GP = R("(() => { const by = {}; for (const r of R) by[r.p] = (by[r.p] || 0) + r.c; return Object.keys(by).find(p => by[p] > 0); })()");
  run("ghost project", () => R(`(() => {
    let cut = 0;
    for (const r of R) if (r.p === __GP) {
      cut += r.c; for (const c of ["c", "ci", "co", "ccw", "ccr", "ct"]) r[c] = 0;
      r.un = r.n; r.ut = r.in + r.out + r.cr + r.cw;
    }
    CUBE.total.cost -= cut; CUBE.total.any_unpriced = true;
    render();
  })()`));
  if (!String(el("tree").innerHTML).includes("只有未知单价的 token")) errors.push("树图: 只有未知单价 token 的项目在 $ 视图下没有任何注记");
  if (!String(el("pareto").innerHTML).includes("只有未知单价的 token")) errors.push("帕累托: 只有未知单价 token 的会话在 $ 视图下没有任何注记");
  if (!String(el("k-unit").innerHTML).includes('class="st"')) errors.push("KPI 单价: 分母不含未知单价 token 时要打 *");
  if (!String(el("burn-q").innerHTML).includes("未知单价 token")) errors.push("累计花费副标题: 含未知单价 token 时要打 *");
  run("ghost all", () => R(`(() => {
    for (const r of R) { for (const c of ["c", "ci", "co", "ccw", "ccr", "ct"]) r[c] = 0; r.un = r.n; r.ut = r.in + r.out + r.cr + r.cw; }
    CUBE.total.cost = 0; render();
  })()`));
  for (const id of ["tree", "pareto", "heat"]) {
    const hh = String(el(id).innerHTML);
    if (!hh.includes("全是未知单价") || hh.includes("当前筛选下没有数据")) errors.push(`#${id}: 全是未知单价 token 时不能写「没有数据」`);
  }
  run("ghost tokens", () => R("setMetric('tokens')"));
  if (String(el("tree").innerHTML).includes("全是未知单价")) errors.push("切到 Tokens 后树图应照常画出来");
  run("ghost back", () => R("setMetric('cost')"));
  run("ghost reload", () => R("load(true)"));
  await settle();
  renderErrors("ghost");
  if (!String(el("foot").innerHTML).includes("已对账")) errors.push("ghost 测试之后重新加载没恢复");
  else seen.ghost = true;
  // 模型图例开关
  run("legend toggle", () => R("toggleModel('claude-opus-5'); toggleModel('claude-opus-5')"));
  // 「按天」图例不按自己的维度筛自己: 选中一个项目后, 图例里别的项目还在 (变淡), 可以接着 Shift 多选
  run("stack project", () => R("setStack('project')"));
  const legOf = () => (String(el("days").innerHTML).split('class="lg"')[1] || "").split("</div>")[0].split("<span ").length;
  const legN = legOf();
  const firstP = (marks().find(m => m[0] === "p" && !m[2]) || [])[1];
  if (firstP) {
    ctx.__P1 = firstP;
    run("legend self", () => R("applyFilter('p', [__P1], false)"));
    const legN2 = legOf();
    if (legN2 !== legN) errors.push(`「按天」图例被自己的维度筛掉了: ${legN - 1} 项 -> ${legN2 - 1} 项`);
    else seen.legend_self = true;
    run("clear", () => R("clearFilters(); setStack('component')"));
  }
  renderErrors("interactions");

  // 窗口: 今天 (小时粒度 + 预算线 + 估算) / 全部 (无基线)
  const k0 = String(el("k-cost").innerHTML), b0 = String(el("burn").innerHTML);
  run("win today", () => R("setWindow('today')"));
  // 请求还没回来: 顶栏已切到「今天」, 但图和 KPI 还是 7 天的, 不能拿 7 天的立方按「今天」重画 (会编出「估算全天」)
  run("render in flight", () => R("render()"));
  if (String(el("k-cost").innerHTML) !== k0 || String(el("burn").innerHTML) !== b0) errors.push("切窗口的请求在途时, 旧立方被按新窗口重画了");
  else if (R("ST.since") !== "today" || R("CUBE.window") !== "7d") errors.push("切窗口: 顶栏状态不对");
  else seen.switch_inflight = true;
  await settle();
  if (R("CUBE.window") !== "today" || String(el("k-cost").innerHTML) === k0) errors.push("切窗口: 新数据回来后没有重画");
  // 今天按星期 / 星期×小时筛: 上一周期 (昨天) 是另一个星期几 -> 不比较; 7 天窗口照常比较
  run("today wd", () => R("F.wd.add(R.length ? R[0].wd : 0); render()"));
  if (R("baseOK()") || !String(el("k-cost").innerHTML).includes("无基线") || String(el("burn-q").innerHTML).includes("昨天同一时点")
      || !String(el("pdelta").innerHTML).includes("另一个星期几")) errors.push("今天 + 按星期筛选: 不该和昨天 (另一个星期几) 比较");
  else seen.today_wd_nobase = true;
  run("clear", () => R("clearFilters()"));
  run("today c", () => R("F.c.add(R.length ? R[0].wd + '-' + R[0].h : '0-0'); render()"));
  if (R("baseOK()")) errors.push("今天 + 按星期×小时筛选: 不该和昨天比较");
  run("clear", () => R("clearFilters()"));
  run("today h", () => R("F.h.add(R.length ? R[0].h : 0); render()"));
  if (String(el("burn-q").innerHTML).includes("估算全天") || R("BURN.today") !== true) errors.push("今天 + 按小时筛选: 不该线性外推「估算全天」");
  else seen.no_proj_hour = true;
  run("clear", () => R("clearFilters()"));
  seen.hourly = String(el("day-h").textContent) === "按小时";
  seen.budget_line = String(el("burn").innerHTML).includes("日预算");
  const eMark = marks().find(m => m[0] === "e");
  if (eMark) {
    ctx.__E = eMark[1];
    run("filter e", () => R("applyFilter('e', [parseVal('e', __E)], false)"));
    if (R("F.e.size") !== 1) errors.push("时段筛选没有生效"); else seen.f_e = true;
    run("clear", () => R("clearFilters()"));
  }
  run("win all", () => R("setWindow('all')"));
  await settle();
  seen.no_baseline = String(el("pdelta").innerHTML).includes("无可比") && String(el("k-cost").innerHTML).includes("无基线");
  run("win 7d", () => R("setWindow('7d')"));
  await settle();
  run("7d wd", () => R("F.wd.add(0); render()"));
  if (!R("baseOK()")) errors.push("7 天窗口按星期筛选: 两段里各星期几覆盖相同, 应照常比较");
  run("clear", () => R("clearFilters()"));
  // 切窗口的请求失败: 退回到图上那份数据的窗口, 页脚说清楚
  ctx.__failCube = true;
  run("win fail", () => R("setWindow('24h')"));
  await settle();
  ctx.__failCube = false;
  if (R("ST.since") !== "7d" || R("CUBE.window") !== "7d" || !String(el("win").innerHTML).includes('data-win="7d" class="on"')
      || !String(el("foot").innerHTML).includes("boom")) errors.push(`切窗口失败后顶栏没退回 7 天: since=${R("ST.since")}, foot=${el("foot").innerHTML}`);
  else seen.switch_fail = true;
  renderErrors("windows");

  // 恶意 / 乱码 URL: 全部安全忽略
  run("hostile url", () => R(`restore("?since=%3Cscript%3E&metric=evil&stack=zzz&scope=root&h=99&h=-1&wd=9&c=7-3&d=2026-13-99x&k=bogus&s=evil&e=abc&x=" + "y".repeat(500)
    + "&k=constructor&k=__proto__&k=toString&s=constructor&s=__proto__&s=hasOwnProperty&d=2026-02-30&d=2026-99-99&d=2026-13-01")`));
  const hostile = R("JSON.stringify({st: ST, f: Object.fromEntries(Object.entries(F).map(([k, s]) => [k, [...s]]))})");
  const h = JSON.parse(hostile);
  if (h.st.since !== "7d" || h.st.metric !== "cost" || h.st.stack !== "component" || h.st.scope !== "all") errors.push("恶意 URL 改动了状态: " + hostile);
  if (Object.values(h.f).some(v => v.length)) errors.push("恶意 URL 注入了筛选: " + hostile);
  // 合法但不存在的实体 + 带标记的项目名: 接受 (会显示为空), 必须转义
  run("hostile label", () => R(`restore("?p=" + encodeURIComponent("<img src=x onerror=alert(1)>") + "&d=2026-09-01"); render()`));
  if (all().includes("<img src=x onerror")) errors.push("URL 里的项目名没转义就进了 HTML");
  else seen.hostile_url = true;
  run("clear", () => R("clearFilters()"));
  renderErrors("hostile");

  // 空数据
  ctx.__cubeKey = "empty";
  run("empty", () => R("load(true)"));
  await settle();
  ctx.__cubeKey = undefined;
  renderErrors("empty");
  seen.empty = all().includes("没有数据");

  // deltaChip 语义 (与旧页一致)
  const chip = (a) => R(`deltaChip(${a})`);
  if (!chip("110, 100, true, String, false").includes("bad")) errors.push("deltaChip: 成本上升应为 bad");
  if (!chip("90, 100, true, String, false").includes("good")) errors.push("deltaChip: 成本下降应为 good");
  if (!chip("5, 0, true, String, false").includes("新增")) errors.push("deltaChip: 上期 0 应为「新增」");
  if (!chip("5, 3, false, String, false").includes("无基线")) errors.push("deltaChip: 无基线");
  if (!chip("90, 97.3, true, x => x.toFixed(1) + 'pp', true, x => x.toFixed(1) + '%'").includes("上一周期 97.3%")) errors.push("deltaChip: 命中率的上期值应写成 %, 不是 pp");
  // 格式: 累计占比没到 100% 不写 100%; 小额刻度不重复
  if (R("fmtCum(0.9956)") !== ">99%" || R("fmtCum(1)") !== "100%" || R("fmtCum(0.8)") !== "80%") errors.push("fmtCum: 99.56% 不能写成 100%");
  for (const mx of [0.02, 0.018, 0.004, 3, 1234]) {
    ctx.__MX = mx;
    if (!R("(() => { const t = niceTicks(__MX, 4), f = axisFmt(t); return new Set(t.map(f)).size === t.length; })()")) errors.push(`刻度标签重复: max=${mx}`);
  }
  if (posts.length) errors.push("出现了写请求: " + posts.join(", "));

  const report = { errors, seen, urls: urls.length };
  console.log(JSON.stringify(report));
  process.exit(errors.length ? 1 : 0);
})().catch(e => { console.log(JSON.stringify({ errors: ["harness: " + (e && e.stack || e)], seen })); process.exit(1); });
