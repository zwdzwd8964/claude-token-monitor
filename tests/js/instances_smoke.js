// /processes 顶部「★ 我的实例」的无浏览器冒烟测试 (tests/test_instances_page.py 调用)。
//
//   node instances_smoke.js <page.js> <data.json>
//
// page.js: 从 processes.html 抠出来的页面脚本; data.json: {status: {local, remote, unavailable}, processes, log, replies}
// 桩掉 DOM / fetch / confirm / alert, 依次跑: 首屏 (核心卡片 / 备用收起 / 横幅 / 📌 / 转义) -> 各种动作的请求形状
// (只带令牌、不碰控制模式; 依赖未就绪 -> 确认 -> force; 端口被占; 编辑读原始登记; 字段错误回显; 📌 预填; 日志抽屉;
// local-only 的 403 不清令牌) -> 远程只读 (不画任何改动按钮) -> 另一个 tokmon 在管 (只读 + 横幅) -> 缺 psutil -> 请求失败保留上一次。
// 修复轮: 卡片 id = ic-<id> (撞名 modal/log/backup 不套定位容器样式) + 旧深链 #inst-<id>; 宽限秒数按实例; 环境变量值去空格;
// degraded / unknown / ambiguous / note; not-owner / degraded / ambiguous 的中文原因。
// 修复轮 2: 没有端口 -> portless-confirm 确认后 force 重试 (取消就不发); 保存 / 改开机策略回 portless-auto 的中文;
// 端口被另一个登记过的实例占着 (by) -> 说实例名; 批量 / 开机提醒跳过的也说出来; 原主人已退出 (stale) -> 「正在接管」横幅 + 快轮询。
// 分组 (0.27): 每组一节 #ix-g-<组 id> (核心 / 量化工具 卡片, 备用 折叠表格默认收起); 拖卡片换组 / 组内排序 / 拖组头调组顺序
// (落点计算 + 放下走 move / reorder) · ⋯ 里的「移到」/ 上移 / 下移 · 新建 / 改名 / 换显示 / 删除分组 · 远程不画把手、不能拖。
// 输出一行 JSON: {errors: [...], checks: {名字: bool}}; 有错误时退出码 1。
"use strict";
const fs = require("fs"), vm = require("vm");

const [jsPath, dataPath] = process.argv.slice(2);
const js = fs.readFileSync(jsPath, "utf8");
const DATA = JSON.parse(fs.readFileSync(dataPath, "utf8"));

const els = {}, scrolled = [];
function mk(id) {
  return { id, innerHTML: "", textContent: "", value: "", checked: false, hidden: false, open: false, disabled: false,
    dataset: {}, style: {}, scrollTop: 0, clientHeight: 100, scrollHeight: 100, tagName: "DIV",
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {}, focus() {}, blur() {}, scrollIntoView() { scrolled.push(id); }, contains() { return false; }, closest() { return null; } };
}
function el(id) { if (!els[id]) els[id] = mk(id); return els[id]; }
el("inst-log").hidden = true;                       // 与 HTML 里的初始状态一致
el("inst-modal").hidden = true;
const rendered = id => Object.keys(els).some(k => String(els[k].innerHTML).includes(`id="${id}"`));

const store = { mc_ctl_token: "tok-test" };
const calls = [], alerts = [], confirms = [], errors = [], prompts = [], promptAnswers = [];
let mode = "local", confirmAnswer = true;
const replies = JSON.parse(JSON.stringify(DATA.replies || {}));
const resp = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => JSON.parse(JSON.stringify(body)) });

const timeouts = [];                                  // 轮询间隔 (stale 时要快)
const rafQ = [];                                      // requestAnimationFrame: 攒着, 测试里手动跑
const ctx = {
  requestAnimationFrame: f => { rafQ.push(f); return rafQ.length; },
  console, setTimeout: (f, ms) => { timeouts.push(ms); return 0; }, clearTimeout: () => {}, setInterval: () => 1, clearInterval: () => {},
  Date, Math, JSON, Object, Array, String, Number, Set, Map, Infinity, NaN, isNaN, isFinite, parseFloat, parseInt,
  encodeURIComponent, decodeURIComponent, Promise, RegExp, Error,
  location: { hash: "#inst-demo-api", search: "", pathname: "/processes" },   // 旧式深链: 要滚到 ic-demo-api
  localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
  confirm: m => { confirms.push(String(m)); return confirmAnswer; },
  alert: m => { alerts.push(String(m)); },
  prompt: m => {                                      // 只有排好了答案才许弹 (新建 / 改名分组); 没排就是意外
    prompts.push(String(m));
    if (!promptAnswers.length) { errors.push("意外的 prompt(): " + m); return null; }
    return promptAnswers.shift();
  },
  navigator: {}, window: { addEventListener() {} },
  document: {
    hidden: false, activeElement: null,
    querySelector: s => (/^#[\w-]+$/.test(s) ? el(s.slice(1)) : null),
    querySelectorAll: () => [],
    getElementById: id => (els[id] || rendered(id)) ? el(id) : null,
    createElement: () => mk("tmp"),
    addEventListener() {},
  },
  fetch: async (url, opts) => {
    const method = (opts && opts.method) || "GET";
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
    calls.push({ url, method, body, headers: (opts && opts.headers) || {} });
    const path = url.split("?")[0];
    if (method === "GET") {
      if (path === "/api/instances") return mode === "fail" ? resp(500, { error: "boom" }) : resp(200, DATA.status[mode]);
      if (path === "/api/processes") return resp(200, DATA.processes);
      if (path === "/api/control") return resp(200, { remote_mode: false });
      if (path === "/api/instances/log") return resp(200, DATA.log);
      return resp(404, { error: "unknown " + url });
    }
    const q = replies[path];
    const r = Array.isArray(q) ? q.shift() : q;
    if (!r) { errors.push("意外的 POST " + path); return resp(200, { ok: false, reason: "unexpected" }); }
    if (r.__status) return resp(r.__status, r.body);
    return resp(200, r);
  },
};
vm.createContext(ctx);
vm.runInContext(js, ctx);

const R = code => vm.runInContext(code, ctx);
const tick = () => new Promise(r => setImmediate(r));
async function settle() { for (let i = 0; i < 12; i++) await tick(); }
async function run(label, code) {
  try { await R(code); await settle(); } catch (e) { errors.push(`${label}: ${(e && e.message) || e}`); }
}
const H = id => String(el(id).innerHTML);
const allInst = () => ["ix-head", "ix-notes", "ix-banners", "ix-groups"].map(H).join("\n");
const posts = p => calls.filter(c => c.method === "POST" && c.url === p);
const lastPost = p => { const a = posts(p); return a.length ? a[a.length - 1] : null; };
// 一组一节: <section class="igrp" id="ix-g-<gid>"> … 到下一节为止
const grp = gid => { const h = H("ix-groups"), k = h.indexOf(`id="ix-g-${gid}"`); if (k < 0) return ""; const e = h.indexOf('<section class="igrp', k + 1); return h.slice(k, e < 0 ? h.length : e); };
const cardIn = (gid, id) => { const h = grp(gid), k = h.indexOf(`id="ic-${id}"`); if (k < 0) return ""; const e = h.indexOf('<div class="icard', k + 1); return h.slice(k, e < 0 ? h.length : e); };
const card = id => cardIn("core", id);
// 假的 DOM 节点: 给落点计算用 (closest / getBoundingClientRect / classList)
function fakeNode(o) {
  const n = Object.assign({ dataset: {}, tagName: "DIV", cls: new Set(), rect: { top: 0, height: 100, left: 0, width: 200 } }, o);
  n.classList = { add: (...c) => c.forEach(x => n.cls.add(x)), remove: (...c) => c.forEach(x => n.cls.delete(x)), toggle() {}, contains: c => n.cls.has(c) };
  n.getBoundingClientRect = () => n.rect;
  n.closest = s => (n.match && n.match(s)) ? n : (n.parent ? n.parent.closest(s) : null);
  return n;
}
const checks = {};

(async () => {
  await settle();

  // ---- 首屏 (本机) ----
  const core = grp("core");
  checks.core_cards = ["demo-api", "demo-web", "demo-busy", "demo-crash", "tokmon-self", "demo-deg", "demo-unk", "demo-amb", "modal", "log",
    "demo-var", "demo-np"]
    .every(id => core.includes(`id="ic-${id}"`));
  checks.backup_not_in_core = !core.includes('id="ic-demo-old"') && !core.includes('id="ic-backup"') && !core.includes('id="ic-demo-q"');
  // 分组: 按服务端给的顺序一节一节画; 自建组「量化工具」是卡片; 备用是折叠表格, 默认收起 (只有组头和计数)
  const G = H("ix-groups");
  checks.groups_in_order = G.indexOf('id="ix-g-core"') >= 0 && G.indexOf('id="ix-g-core"') < G.indexOf('id="ix-g-g1"')
    && G.indexOf('id="ix-g-g1"') < G.indexOf('id="ix-g-backup"');
  const q = grp("g1");
  checks.custom_group_cards = q.includes('<span class="ignm">量化工具</span>') && q.includes("1 个") && cardIn("g1", "demo-q").includes("回测样例")
    && q.includes('class="igrid"') && q.includes('data-act="g-start" data-gid="g1"');
  checks.backup_collapsed = grp("backup").includes("▸ 备用") && grp("backup").includes("2 个") && grp("backup").includes('data-act="g-toggle" data-gid="backup"')
    && !grp("backup").includes('id="ic-demo-old"') && !grp("backup").includes("<table");
  checks.group_menus = q.includes('data-act="g-rename" data-gid="g1"') && q.includes("显示为折叠表格") && q.includes('data-act="g-delete" data-gid="g1"')
    && !core.includes('data-act="g-delete"') && !grp("backup").includes('data-act="g-delete"')        // 内置组不能删
    && core.includes('data-act="g-up" data-gid="core" disabled') && grp("backup").includes('data-act="g-down" data-gid="backup" disabled')
    && grp("backup").includes("显示为卡片");
  checks.drag_handles = core.includes('draggable="true" data-drag="group" data-gid="core"')
    && card("demo-api").includes('id="ic-demo-api" draggable="true" data-drag="inst" data-id="demo-api"');
  checks.move_menu = card("demo-api").includes('data-act="move-to" data-id="demo-api" data-gid="g1">移到「量化工具」')
    && card("demo-api").includes('data-gid="backup">移到「备用」') && !card("demo-api").includes('data-gid="core">移到')
    && card("demo-api").includes('data-act="move-up" data-id="demo-api" disabled')                  // 组里第一个: 不能再上移
    && !card("demo-api").includes('data-act="move-down" data-id="demo-api" disabled')
    && card("demo-np").includes('data-act="move-down" data-id="demo-np" disabled');                // 组里最后一个
  // c19: 实例 id 叫 modal / log / backup 也不会画出 id="inst-..." (那是 position:fixed 的抽屉 / 弹窗 / 备用容器)
  checks.no_inst_ids_rendered = !/id="inst-/.test(allInst());
  checks.legacy_hash_scrolls = scrolled.includes("ic-demo-api");
  const api = card("demo-api");
  checks.running_pill = api.includes("运行中") && api.includes("2h 3m") && api.includes("外部启动");
  checks.open_link = api.includes('href="http://localhost:18080/"') && api.includes("打开 ↗") && !card("demo-web").includes("打开 ↗");
  checks.running_buttons = api.includes('data-act="stop" data-id="demo-api"') && api.includes('data-act="restart" data-id="demo-api"')
    && !api.includes('data-act="start" data-id="demo-api"');
  checks.stopped_buttons = card("demo-web").includes('data-act="start" data-id="demo-web"') && !card("demo-web").includes('data-act="stop"');
  checks.port_busy = card("demo-busy").includes("端口 18090 被 other.exe");
  checks.self_card = card("tokmon-self").includes("本服务") && !card("tokmon-self").includes('data-act="start"')
    && !card("tokmon-self").includes('data-act="stop"') && card("tokmon-self").includes("开机自启 ✗");
  checks.deps_chip = card("demo-web").includes("依赖 数据库 :18900 ✗");
  checks.tunnel_chip = api.includes("demo-tunnel.trycloudflare.com") && api.includes('data-act="copy"');
  checks.shared_port = api.includes("与 旧版 API 共用端口 18080");
  checks.note_line = api.includes('<div class="inote">ⓘ 端口由 com.docker.backend.exe 代理</div>')
    && api.split("端口由 com.docker.backend.exe 代理").length === 2;          // note 和 last_error 同一句只说一次
  checks.head_count_truth = H("ix-head").includes("17 个 · 3 个在跑");       // degraded / unknown / 认不准 都不算在跑
  checks.boot_handled_title = H("ix-head").includes("上次开机拉起：");
  // 新状态: degraded (琥珀, 停止/重启) / unknown (灰, 启动/停止) / ambiguous (认不准, 不显示运行中, 不给启停)
  const deg = card("demo-deg");
  checks.degraded_card = deg.startsWith('id="ic-demo-deg"') && grp("core").includes('<div class="icard warn" id="ic-demo-deg"')
    && deg.includes('<span class="pill warn"') && deg.includes("◐ 进程在 · 端口没在监听") && !deg.includes("● 运行中")
    && deg.includes("pid 2222") && !deg.includes("打开 ↗") && deg.includes('<div class="iwarn">端口在监听，但持有者 other.exe 认不出是它</div>');
  checks.degraded_buttons = deg.includes('data-act="stop" data-id="demo-deg"') && deg.includes('data-act="restart" data-id="demo-deg"')
    && !deg.includes('data-act="start" data-id="demo-deg"');
  const unk = card("demo-unk");
  checks.unknown_card = unk.includes('<span class="pill dim"') && unk.includes("状态未知（读不到端口表）")
    && unk.includes('data-act="start" data-id="demo-unk"') && unk.includes('data-act="stop" data-id="demo-unk"');
  const amb = card("demo-amb");
  checks.ambiguous_card = amb.includes("端口 18099 的进程同时像 歧义甲 和 歧义乙，认不准，不显示为运行中")
    && amb.includes("认不准是谁的") && !amb.includes("● 运行中") && !amb.includes('data-act="start"') && !amb.includes('data-act="stop"')
    && grp("core").includes('<div class="icard warn" id="ic-demo-amb"') && card("demo-amb2").includes("同时像 歧义乙 和 歧义甲");
  // 端口被另一个登记过的实例占着: 说实例名 (主端口在药丸里, 其余端口在冲突行里), 不只说 python.exe
  const vr = card("demo-var");
  checks.port_busy_by_instance = vr.includes("端口 18080 被实例「示例 API」占用") && vr.includes("⚠ 端口 18081 被实例「示例 API」占用")
    && vr.includes('title="被实例「示例 API」占用">:18080') && !vr.includes("被 python.exe") && !vr.includes('data-act="stop"');
  const np = card("demo-np");
  checks.portless_label = np.includes("未运行（没有端口，外部启动的认不出）") && np.includes("无端口") && np.includes('data-act="start" data-id="demo-np"');
  checks.boot_select = api.includes('<select class="iboot" data-act="boot" data-id="demo-api"') && api.includes('<option value="auto" selected>');
  checks.crash_banner = H("ix-banners").includes("崩溃样例 意外退出（退出码 1）") && H("ix-banners").includes('data-act="log" data-id="demo-crash"');
  checks.boot_banner = H("ix-banners").includes("重启后有 1 个实例等你确认拉起：示例 Web") && H("ix-banners").includes('data-act="boot-start"');
  checks.manifest_banner = H("ix-banners").includes("清单坏了");
  checks.head = H("ix-head").includes("+ 新建分组") && H("ix-head").includes("+ 新增实例") && !H("ix-head").includes("全部拉起")
    && H("ix-head").includes("开机自启 ✗") && H("ix-head").includes('data-act="autostart-on"');
  const every = allInst();
  checks.escaped = !every.includes("<img src=x") && !every.includes("<script>") && !every.includes("<b>x</b>")
    && every.includes("&lt;img src=x onerror=alert(1)&gt;") && every.includes("&#39;&quot;;alert(3)//");
  const app = H("app");
  checks.pins = app.includes('data-pid="4321" data-ct="1700000000.5" onclick="instPin(this)"')
    && app.includes('data-pid="5555" data-ct="1700000001.25" onclick="instPin(this)"');
  checks.no_post_on_load = calls.every(c => c.method === "GET");
  R("location.hash='#ic-demo-old'; instJumpHash()");
  checks.hash_opens_backup = R("IX_OPEN.backup") === true && grp("backup").includes('id="ic-demo-old"') && scrolled.includes("ic-demo-old");
  R("IX_OPEN={}; renderInstances(true)");
  // 折叠表格: 点组名展开 (表格行也能拖), 展开状态记在这台浏览器里; 再点收起
  await run("toggle backup", "instDo('g-toggle','',{dataset:{gid:'backup'}})");
  checks.toggle_opens = grp("backup").includes("▾ 备用") && grp("backup").includes('<tr id="ic-demo-old" draggable="true" data-drag="inst" data-id="demo-old">')
    && JSON.parse(store["mc.inst.open"] || "{}").backup === true && calls.every(c => c.method === "GET");
  await run("toggle backup again", "instDo('g-toggle','',{dataset:{gid:'backup'}})");
  checks.toggle_closes = !grp("backup").includes('id="ic-demo-old"') && JSON.parse(store["mc.inst.open"]).backup === false;
  R("location.hash='#inst-nope'; instJumpHash()");                        // 不存在的 id: 什么都不做, 不报错
  checks.hash_missing_quiet = !scrolled.includes("ic-nope");

  // ---- 动作 ----
  confirmAnswer = true;
  await run("start deps", "instDo('start','demo-web')");
  const st = posts("/api/instances/start");
  checks.start_deps_force = st.length === 2 && st[0].body.id === "demo-web" && st[0].body.force === false && st[1].body.force === true
    && confirms.some(m => m.includes("依赖未就绪") && m.includes("数据库 :18900")) && st[1].headers["X-Control-Token"] === "tok-test";
  checks.start_msg = card("demo-web").includes("已发出启动");

  await run("start port-busy", "instDo('start','demo-web')");
  checks.port_busy_alert = alerts.some(m => m.includes("other.exe") && m.includes("999"));

  const nConf = confirms.length;
  await run("stop", "instDo('stop','demo-api')");
  checks.stop_confirm = confirms.length === nConf + 1 && confirms[nConf].includes("Ctrl+C") && confirms[nConf].includes("30 秒")
    && !confirms[nConf].includes("10 秒")
    && confirms[nConf].includes("不是 tokmon 启动的");
  checks.stop_post = (lastPost("/api/instances/stop") || {}).body && lastPost("/api/instances/stop").body.id === "demo-api";

  confirmAnswer = false;
  await run("restart cancelled", "instDo('restart','demo-api')");
  checks.restart_cancel_no_post = posts("/api/instances/restart").length === 0 && confirms[confirms.length - 1].includes("30 秒");
  confirmAnswer = true;

  await run("restart degraded", "instDo('restart','demo-deg')");
  checks.restart_grace_zero = (lastPost("/api/instances/restart") || {body: {}}).body.id === "demo-deg"
    && confirms[confirms.length - 1].includes("不等（宽限 0 秒）") && !confirms[confirms.length - 1].includes("10 秒");

  // 原因码 -> 中文 (detail 与中文同一句就不重复)
  await run("start degraded", "instStart('demo-deg',false)");
  const degMsg = card("demo-deg");
  checks.reason_degraded = degMsg.includes("没启动：进程还在但端口没在监听，请用「重启」") && degMsg.split("请用「重启」").length === 2;

  const nStart = posts("/api/instances/start").length;
  confirmAnswer = false;
  await run("start unknown cancelled", "instDo('start','demo-unk')");
  checks.unknown_start_confirms = posts("/api/instances/start").length === nStart && confirms[confirms.length - 1].includes("状态未知");
  confirmAnswer = true;
  await run("start unknown", "instDo('start','demo-unk')");
  checks.unknown_start_posts = posts("/api/instances/start").length === nStart + 1 && lastPost("/api/instances/start").body.id === "demo-unk"
    && lastPost("/api/instances/start").body.force === false;

  // 没有端口: 服务端要你确认 -> 取消就不再发; 确定才带 force 重试 (确认文案顺带说依赖没就绪、不会再等)
  const nNp = posts("/api/instances/start").length, nNpConf = confirms.length;
  confirmAnswer = false;
  await run("start portless cancelled", "instDo('start','demo-np')");
  const npc = posts("/api/instances/start").slice(nNp);
  checks.portless_cancel = npc.length === 1 && npc[0].body.id === "demo-np" && npc[0].body.force === false
    && confirms.length === nNpConf + 1 && confirms[nNpConf].includes("无端口样例") && confirms[nNpConf].includes("确定要再起一份吗")
    && confirms[nNpConf].includes("依赖未就绪：数据库 :18900") && card("demo-np").includes("没启动：没有端口，你取消了");
  confirmAnswer = true;
  await run("start portless confirmed", "instDo('start','demo-np')");
  const npo = posts("/api/instances/start").slice(nNp + 1);
  checks.portless_force_retry = npo.length === 2 && npo[0].body.force === false && npo[1].body.force === true
    && npo[1].body.id === "demo-np" && card("demo-np").includes("已发出启动");

  // port-busy 回执: 没带 by 也按卡片上同一个 pid 的冲突认出是哪个实例; 带了 by 直接用
  const nAl = alerts.length;
  await run("start port-busy by (card)", "instDo('start','demo-var')");
  await run("start port-busy by (reply)", "instDo('start','demo-var')");
  checks.port_busy_by_alert = alerts.length === nAl + 2 && alerts.slice(nAl).every(m => m.includes("「变体乙」的端口被实例「示例 API」占用"))
    && alerts.slice(nAl).every(m => m.includes("先把「示例 API」停掉") && !m.includes("不认识的进程"))
    && card("demo-var").includes("没启动：端口被实例「示例 API」占用");

  await run("edit", "instDo('edit','demo-api')");
  checks.edit_uses_get = posts("/api/instances/get").length === 1 && el("inst-modal").hidden === false
    && el("ixf-env").value === "API_KEY=secret-value" && el("ixf-ports").value === "18080, 18081"
    && el("ixf-deps").value === "数据库:18900" && el("ixf-id").value === "demo-api" && el("ixf-boot").value === "auto"
    && el("ixf-command").value === "python -m http.server 18080";
  checks.edit_group_options = H("ixf-group").includes('<option value="g1">量化工具</option>')
    && H("ixf-group").indexOf('value="core"') < H("ixf-group").indexOf('value="g1"') && el("ixf-group").value === "core";

  el("ixf-cwd").value = "C:/Users/u/nope";
  el("ixf-env").value = " API_KEY = secret-value \nB= two=three \n# 注释\n\n  C=x  ";
  await run("save invalid", "instSave()");
  const sv = lastPost("/api/instances/save");
  checks.save_body = !!sv && sv.body.original_id === "demo-api" && sv.body.instance.cwd === "C:/Users/u/nope"
    && JSON.stringify(sv.body.instance.env) === JSON.stringify({ API_KEY: "secret-value", B: "two=three", C: "x" })
    && JSON.stringify(sv.body.instance.ports) === "[18080,18081]"
    && sv.body.instance.deps[0].name === "数据库" && sv.body.instance.deps[0].port === 18900 && sv.body.instance.id === "demo-api";
  checks.field_errors = el("ixe-cwd").textContent === "目录不存在" && el("ixe-ports").textContent === "端口重复"
    && el("ix-m-err").textContent.includes("weird") && el("inst-modal").hidden === false;

  const nSave = posts("/api/instances/save").length;
  el("ixf-ports").value = "18080, abc";
  el("ixf-env").value = "OK=1\n  = oops";
  await run("save client-invalid", "instSave()");
  checks.client_validation = posts("/api/instances/save").length === nSave && el("ixe-ports").textContent.includes("整数")
    && el("ixe-env").textContent.includes("KEY=VALUE");

  el("ixf-ports").value = "";
  el("ixf-env").value = "API_KEY=secret-value";
  el("ixf-boot").value = "auto";
  await run("save portless-auto", "instSave()");
  checks.save_portless_auto = posts("/api/instances/save").length === nSave + 1 && lastPost("/api/instances/save").body.instance.boot === "auto"
    && JSON.stringify(lastPost("/api/instances/save").body.instance.ports) === "[]"
    && el("ixe-boot").textContent === "没有端口的实例认不出外部启动的副本，开机策略只能用「手动」" && el("inst-modal").hidden === false;

  el("ixf-ports").value = "18080";
  el("ixf-env").value = "API_KEY=secret-value";
  await run("save ok", "instSave()");
  checks.save_ok_closes = el("inst-modal").hidden === true;

  await run("pin", "instPin({dataset:{pid:'4321',ct:'1700000000.5'},disabled:false})");
  const dr = lastPost("/api/instances/draft");
  checks.pin_draft = !!dr && dr.body.pid === 4321 && dr.body.create_time === 1700000000.5 && el("inst-modal").hidden === false
    && el("ixf-id").value === "" && el("ixf-name").value === "node" && el("ixf-ports").value === "18095"
    && el("ix-m-warn").hidden === false && el("ix-m-warn").textContent.includes("密钥");
  R("instModalClose()");

  const nProc = calls.filter(c => c.url === "/api/processes").length;
  await run("pin identity", "instPin({dataset:{pid:'5555',ct:''},disabled:false})");
  const dr2 = lastPost("/api/instances/draft");
  checks.pin_identity = dr2.body.create_time === null && alerts.some(m => m.includes("再点一次"))
    && calls.filter(c => c.url === "/api/processes").length === nProc + 1;

  await run("log", "instDo('log','demo-api')");
  checks.log_open = calls.some(c => c.url === "/api/instances/log?id=demo-api&n=200") && el("inst-log").hidden === false
    && el("ix-log-pre").textContent.includes("listening on 18080") && el("ix-log-title").textContent.includes("示例 API");
  await run("log more", "instLogMore()");
  checks.log_more = calls.some(c => c.url === "/api/instances/log?id=demo-api&n=500");
  R("instLogClose()");
  checks.log_close = el("inst-log").hidden === true;

  await run("local-only 403", "instDo('stop','demo-api')");
  checks.local_only_keeps_token = R("CTRL_TOKEN") === "tok-test" && store.mc_ctl_token === "tok-test"
    && !alerts.some(m => m.includes("令牌无效"));

  await run("stop ambiguous", "instDo('stop','demo-amb')");
  checks.reason_ambiguous = card("demo-amb").includes("没停：端口上的进程认不准是哪个实例的，不敢动它");
  await run("stop not-owner", "instDo('stop','demo-deg')");
  checks.reason_not_owner = card("demo-deg").includes("没停：不归这边管 · 另一个 tokmon（pid 4242 · 端口 18766）在管理实例");

  await run("boot select", "instChange({target:{dataset:{act:'boot',id:'demo-api'},value:'ask',blur(){}}})");
  const pt = lastPost("/api/instances/patch");
  checks.patch_boot = !!pt && pt.body.id === "demo-api" && pt.body.fields.boot === "ask" && Object.keys(pt.body.fields).length === 1;

  await run("boot select portless", "instChange({target:{dataset:{act:'boot',id:'demo-np'},value:'auto',blur(){}}})");
  checks.patch_portless_auto = lastPost("/api/instances/patch").body.id === "demo-np"
    && card("demo-np").includes("没改成：") && card("demo-np").includes("开机策略只能用「手动」");

  // ---- 分组 (0.27): ⋯ 里的「移到」/ 上移 / 下移 ----
  await run("move-to", "instDo('move-to','demo-api',{dataset:{gid:'g1'}})");
  const mv = posts("/api/instances/move");
  checks.move_to_post = mv.length === 1 && JSON.stringify(mv[0].body) === JSON.stringify({ id: "demo-api", group: "g1", before: null })
    && mv[0].headers["X-Control-Token"] === "tok-test" && card("demo-api").includes("已移到「量化工具」");
  await run("move-up first", "instDo('move-up','demo-api')");                      // 已经是第一个: 不发
  await run("move-down", "instDo('move-down','demo-api')");
  checks.move_down_post = posts("/api/instances/move").length === 2
    && JSON.stringify(lastPost("/api/instances/move").body) === JSON.stringify({ id: "demo-api", group: "core", before: "demo-busy" })
    && card("demo-api").includes("已调整顺序");
  // 本地先挪 (乐观): 和服务端同一套「排在谁前面 / 放到组末尾」
  R("instLocalMove('demo-web','g1','demo-q'); renderInstances(true)");
  checks.local_move_before = grp("g1").indexOf('id="ic-demo-web"') >= 0 && grp("g1").indexOf('id="ic-demo-web"') < grp("g1").indexOf('id="ic-demo-q"')
    && !grp("core").includes('id="ic-demo-web"');
  R("instLocalMove('demo-web','backup',null); renderInstances(true)");
  checks.local_move_end = R("instInGroup('backup').map(x=>x.id).join(',')") === "demo-old,backup,demo-web";
  await run("reload after local", "loadInstances()");

  // ---- 拖拽: 放下的结果 -> 同一套动作 ----
  const nMv = posts("/api/instances/move").length;
  await run("drop same place", "instDropApply({type:'inst',id:'demo-web'},{kind:'inst',gid:'core',id:'demo-api',after:true})");
  checks.drop_noop = posts("/api/instances/move").length === nMv;                   // 放回原处: 不发请求
  await run("drop before", "instDropApply({type:'inst',id:'demo-web'},{kind:'inst',gid:'core',id:'demo-api',after:false})");
  checks.drop_before_post = JSON.stringify(lastPost("/api/instances/move").body) === JSON.stringify({ id: "demo-web", group: "core", before: "demo-api" })
    && card("demo-web").includes("没挪成：要排在它前面的那个实例不在这一组了");
  await run("drop end of group", "instDropApply({type:'inst',id:'demo-old'},{kind:'end',gid:'g1'})");
  checks.drop_end_post = JSON.stringify(lastPost("/api/instances/move").body) === JSON.stringify({ id: "demo-old", group: "g1", before: null });
  // 落点计算: 卡片左半边 = 排在它前面, 右半边 = 排在它后面; 组里空白处 = 末尾; 拖到自己身上 = 不能放
  const sec = fakeNode({ dataset: { gid: "g1" }, match: s => s === ".igrp" });
  const qc = fakeNode({ dataset: { drag: "inst", id: "demo-q" }, parent: sec, match: s => s === '[data-drag="inst"]' || s === "[data-drag]" });
  ctx.__sec = sec; ctx.__qc = qc;
  R("DRAG={type:'inst', id:'demo-web'}");
  const left = R("JSON.stringify((({kind,gid,id,after})=>({kind,gid,id,after}))(instDropAt({target:__qc, clientX:40, clientY:10})))");
  const right = R("instDropAt({target:__qc, clientX:160, clientY:10}).after");
  const blank = R("instDropAt({target:__sec, clientX:0, clientY:0}).kind");
  checks.drop_geometry = left === JSON.stringify({ kind: "inst", gid: "g1", id: "demo-q", after: false }) && right === true && blank === "end"
    && R("instInteracting()") === true;                                              // 拖动中不重画
  const ours = { types: ["application/x-tokmon-instance"] };                      // 本页拖出来的才带这个类型
  ctx.__ours = ours;
  R("instDragOver({target:__qc, clientX:40, clientY:10, preventDefault(){}, dataTransfer:__ours})");
  checks.drop_marker = qc.cls.has("drop-before") && !qc.cls.has("drop-after");
  // 拖动中: 连强制重画也推迟 (换掉被拖的节点 dragend 就丢了), 拖完补画
  const before0 = H("ix-groups");
  R("instSay('demo-api','拖动中来的回执','ok')");
  checks.render_deferred_while_dragging = H("ix-groups") === before0 && R("INST_DEFERRED") === true;
  R("DRAG={type:'inst', id:'demo-q'}");
  checks.drop_self_refused = R("instDropAt({target:__qc, clientX:40, clientY:10})") === null;
  R("DRAG={type:'inst', id:'demo-web'}");
  await run("drop event", "instDropEv({target:__qc, clientX:40, clientY:10, preventDefault(){}, dataTransfer:__ours})");
  checks.drop_event_post = JSON.stringify(lastPost("/api/instances/move").body) === JSON.stringify({ id: "demo-web", group: "g1", before: "demo-q" })
    && R("DRAG") === null && !qc.cls.has("drop-before") && R("INST_DEFERRED") === false
    && card("demo-api").includes("拖动中来的回执");                                 // 推迟的那次补画上了
  // 残留的拖动状态 + 外面拖进来的东西 (文件 / 文字, 不带本页的类型): 清掉, 不当成挪实例
  const nMv2 = posts("/api/instances/move").length;
  R("DRAG={type:'inst', id:'demo-web'}");
  await run("foreign dragover", "instDragOver({target:__qc, clientX:40, clientY:10, preventDefault(){}, dataTransfer:{types:['Files']}})");
  checks.foreign_drag_clears_stale = R("DRAG") === null && posts("/api/instances/move").length === nMv2;
  R("DRAG={type:'inst', id:'demo-web'}");
  await run("foreign drop", "instDropEv({target:__qc, clientX:40, clientY:10, preventDefault(){}, dataTransfer:{types:['text/plain']}})");
  checks.foreign_drop_ignored = R("DRAG") === null && posts("/api/instances/move").length === nMv2;
  // 上一个挪动还在路上: 不在本地先挪, 说一声
  R("INST_BUSY._move=true");
  await run("move while busy", "instMoveTo('demo-web','g1',null)");
  checks.move_busy_refused = posts("/api/instances/move").length === nMv2 && card("demo-web").includes("上一个挪动还没做完")
    && R("instInGroup('g1').some(x=>x.id==='demo-web')") === false;
  R("delete INST_BUSY._move; renderInstances(true)");
  // 从按钮上起的拖动不算 (那是要点它); 从卡片本身起的才算; 只放本页的类型 (不放 text/plain, 免得掉进过滤框)
  const btnEl = fakeNode({ tagName: "BUTTON", match: s => s.includes("button") });
  ctx.__btn = btnEl;
  let prevented = false;
  const dragData = {};
  let nodeEnd = null;
  qc.addEventListener = (t, f, o) => { if (t === "dragend" && o && o.once) nodeEnd = f; };
  ctx.__ev = { target: qc, preventDefault() { prevented = true; }, dataTransfer: { setData(t, v) { dragData[t] = v; } } };
  R("DOWN_EL=__btn; instDragStart(__ev)");
  checks.drag_from_button_ignored = prevented === true && R("DRAG") === null;
  R("DOWN_EL=__qc; instDragStart(__ev)");
  const fadedEarly = qc.cls.has("dragging");
  rafQ.splice(0).forEach(f => f());
  checks.drag_start = R("DRAG && DRAG.type") === "inst" && R("DRAG.id") === "demo-q"
    && JSON.stringify(dragData) === JSON.stringify({ "application/x-tokmon-instance": "demo-q" })
    && !fadedEarly && qc.cls.has("dragging") && nodeEnd === R("instDragEnd");      // 半透明晚一拍; dragend 挂在节点上
  nodeEnd();                                                                         // 节点自己收到 dragend
  checks.drag_end_clears = R("DRAG") === null && !qc.cls.has("dragging");
  // 卡片里的命令行 / 错误: 按下时这张卡片暂时不能拖, 好选中文字
  const cmdEl = fakeNode({ parent: qc, match: s => s.includes(".icmd") });
  qc.draggable = true; ctx.__cmd = cmdEl;
  R("instMouseDown({target:__cmd})");
  const cmdOff = qc.draggable === false;
  qc.draggable = true;
  R("instMouseDown({target:__qc})");
  checks.text_zone_not_draggable = cmdOff && qc.draggable === true && R("DOWN_EL") === qc;

  // ---- 分组本身: 新建 / 改名 / 换显示 / 调顺序 (菜单 + 拖组头) / 删除 ----
  promptAnswers.push("  回测研究 ");
  await run("group new", "instDo('g-new','')");
  const gp = posts("/api/instances/groups");
  checks.group_create = gp.length === 1 && JSON.stringify(gp[0].body) === JSON.stringify({ action: "create", name: "回测研究" })
    && H("ix-notes").includes("已新建分组「回测研究」");
  promptAnswers.push(null);
  await run("group rename cancelled", "instDo('g-rename','',{dataset:{gid:'g1'}})");
  checks.group_rename_cancel = posts("/api/instances/groups").length === 1 && prompts[prompts.length - 1].includes("「量化工具」");
  promptAnswers.push("量化");
  await run("group rename", "instDo('g-rename','',{dataset:{gid:'g1'}})");
  checks.group_rename = JSON.stringify(lastPost("/api/instances/groups").body) === JSON.stringify({ action: "rename", id: "g1", name: "量化" })
    && H("ix-notes").includes("「量化工具」已改名为「量化」");
  await run("group view", "instDo('g-view','',{dataset:{gid:'g1'}})");
  checks.group_view = JSON.stringify(lastPost("/api/instances/groups").body) === JSON.stringify({ action: "view", id: "g1", view: "table" })
    && R("IX_OPEN.g1") === true;                                                    // 刚换成表格: 展开着
  await run("group down", "instDo('g-down','',{dataset:{gid:'core'}})");
  checks.group_shift = JSON.stringify(lastPost("/api/instances/groups").body) === JSON.stringify({ action: "reorder", order: ["g1", "core", "backup"] });
  await run("group drop", "instDropApply({type:'group',id:'backup'},{kind:'group',gid:'core',after:false})");
  checks.group_drop = JSON.stringify(lastPost("/api/instances/groups").body) === JSON.stringify({ action: "reorder", order: ["backup", "core", "g1"] })
    && H("ix-notes").includes("分组没改成：分组顺序对不上");
  await run("group drop same", "instDropApply({type:'group',id:'core'},{kind:'group',gid:'g1',after:false})");
  checks.group_drop_noop = posts("/api/instances/groups").length === 5;              // 放回原处: 不发
  const nCf = confirms.length;
  await run("group delete", "instDo('g-delete','',{dataset:{gid:'g1'}})");
  checks.group_delete = JSON.stringify(lastPost("/api/instances/groups").body) === JSON.stringify({ action: "delete", id: "g1" })
    && confirms.length === nCf + 1 && confirms[nCf].includes("删除分组「量化工具」") && confirms[nCf].includes("回测样例")
    && confirms[nCf].includes("移到「备用」末尾") && confirms[nCf].includes("不会停掉任何进程")
    && H("ix-notes").includes("已删除分组「量化工具」，1 个实例移到了「备用」");
  R("IX_OPEN={}");

  await run("start-group", "instDo('g-start','',{dataset:{gid:'core'}})");
  checks.start_group = lastPost("/api/instances/start-group").body.group === "core" && H("ix-notes").includes("已排队 1 个：示例 Web")
    && confirms[confirms.length - 1].includes("「核心」");

  await run("boot ack", "instDo('boot-start','')");
  checks.boot_ack = lastPost("/api/instances/boot-ack").body.action === "start"
    && H("ix-notes").includes("跳过 1 个：无端口样例 没有端口、认不出是不是已在别处跑");

  await run("autostart", "instDo('autostart-on','')");
  checks.autostart = lastPost("/api/autostart").body.on === true && H("ix-notes").includes("已登记开机自启");

  await run("delete", "instDo('delete','demo-busy')");
  checks.delete = lastPost("/api/instances/delete").body.id === "demo-busy" && confirms[confirms.length - 1].includes("只删登记");

  checks.never_control_mode = R("CTRL_MODE") === false && posts("/api/control/mode").length === 0
    && calls.filter(c => c.method === "POST").every(c => c.headers["X-Control-Token"] === "tok-test");

  // ---- 远程: 只读 ----
  mode = "remote";
  await run("remote", "loadInstances()");
  const rem = allInst();
  checks.remote_readonly = ["start", "stop", "restart", "edit", "delete", "move-to", "move-up", "g-start", "g-new", "g-rename", "g-view",
    "g-up", "g-delete", "new", "boot-start", "autostart-on"]
    .every(a => !rem.includes(`data-act="${a}"`)) && !rem.includes("<select") && !rem.includes("打开 ↗");
  checks.remote_no_drag = !rem.includes("draggable") && !rem.includes("data-drag") && rem.includes('data-act="g-toggle" data-gid="backup"');   // 展开收起只是显示, 远程照样能点
  checks.remote_note = H("ix-notes").includes("手机/远程只读 · 启停请在本机操作");
  checks.remote_cards_still_shown = grp("core").includes('id="ic-demo-api"') && grp("core").includes("开机：开机自动") && grp("g1").includes('id="ic-demo-q"');
  checks.remote_log_readable = grp("core").includes('data-act="log" data-id="demo-api"');
  checks.remote_no_pins = !H("app").includes("instPin");

  // ---- 另一个 tokmon 在管实例: 本机也只读, 横幅说清是谁 ----
  mode = "notowner";
  await run("notowner", "loadInstances()");
  const no = allInst();
  checks.owner_banner = H("ix-banners").includes("🔒 只读：另一个 tokmon（pid 4242 · 端口 18766）在管理实例")
    && H("ix-banners").includes('href="http://localhost:18766/processes#instances"');
  checks.owner_readonly = ["start", "stop", "restart", "edit", "delete", "move-to", "g-start", "g-new", "g-rename", "g-delete", "new",
    "boot-start", "boot-dismiss", "autostart-on", "autostart-off"].every(a => !no.includes(`data-act="${a}"`)) && !no.includes("<select")
    && !no.includes("draggable");
  checks.owner_still_local = no.includes("打开 ↗") && no.includes('data-act="log" data-id="demo-api"')
    && !H("ix-notes").includes("手机/远程只读");
  checks.owner_no_pins = !H("app").includes("instPin");
  mode = "local";
  await run("owner back", "loadInstances()");
  checks.owner_back_pins = H("app").includes("instPin") && grp("core").includes('data-act="start" data-id="demo-web"')
    && !H("ix-banners").includes("另一个 tokmon");
  checks.poll_normal = timeouts[timeouts.length - 1] === 4000;

  // ---- 原来的主人已经退出 (锁空了, owner.json 的 pid 死了): 说「正在接管」, 不指向一个不在的 tokmon; 仍只读; 轮询加快 ----
  mode = "stale";
  await run("stale owner", "loadInstances()");
  const sl = allInst();
  checks.stale_banner = H("ix-banners").includes("原来管理实例的 tokmon 已退出，正在接管") && !H("ix-banners").includes("另一个 tokmon")
    && !H("ix-banners").includes("去它的页面");
  checks.stale_readonly = ["start", "stop", "restart", "edit", "delete", "g-start", "g-new", "move-to", "new"].every(a => !sl.includes(`data-act="${a}"`))
    && !sl.includes("draggable");
  checks.stale_fast_poll = timeouts[timeouts.length - 1] === 1500;
  mode = "local";
  await run("stale taken over", "loadInstances()");
  checks.stale_taken_over = !H("ix-banners").includes("正在接管") && grp("core").includes('data-act="start" data-id="demo-web"')
    && timeouts[timeouts.length - 1] === 4000;

  // ---- 缺 psutil / 请求失败 ----
  mode = "unavailable";
  await run("unavailable", "loadInstances()");
  checks.unavailable_hint = H("ix-notes").includes("psutil") && H("ix-groups").includes("还没有登记实例")
    && grp("core").includes("这一组还没有实例");
  mode = "local";
  await run("back local", "loadInstances()");
  mode = "fail";
  await run("fail", "loadInstances()");
  checks.fail_keeps_last = H("ix-notes").includes("实例状态读取失败") && H("ix-notes").includes("boom") && grp("core").includes('id="ic-demo-api"');

  for (const k of Object.keys(replies)) {
    if (Array.isArray(replies[k]) && replies[k].length) errors.push(`没用上的应答 ${k}: ${replies[k].length} 条`);
  }
  console.log(JSON.stringify({ errors, checks }));
  process.exit(errors.length ? 1 : 0);
})().catch(e => { console.log(JSON.stringify({ errors: ["harness: " + (e && e.stack || e)], checks })); process.exit(1); });
