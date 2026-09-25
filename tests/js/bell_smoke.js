// 会话驾驶舱 S2: 导航里「等你时提醒」脚本的 Node 冒烟 —— 桩掉 DOM / fetch / Notification / localStorage,
// 两个 vm 上下文共用一份 localStorage = 两个标签页。用法: node bell_smoke.js <bell.js>
// 输出一行 JSON {errors: [...], checks: {...}}; 有错误时退出码 1。
"use strict";
const fs = require("fs");
const vm = require("vm");

const code = fs.readFileSync(process.argv[2], "utf-8");
const errors = [], checks = {};
const expect = (name, ok, detail) => { checks[name] = !!ok; if (!ok) errors.push(name + (detail ? ": " + detail : "")); };

const store = new Map();                                   // 两个标签页共用
const localStorage = {
  getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)),
  removeItem: k => store.delete(k), key: i => [...store.keys()][i] ?? null, get length() { return store.size; },
};
const shown = [];                                          // 所有 new Notification(...)
let responses = [];                                        // /api/attention 的脚本化回复 (两个标签页共用队列)

function tab(name) {
  const bell = {
    textContent: "", title: "", classes: new Set(), handlers: {},
    classList: { toggle(c, on) { on ? bell.classes.add(c) : bell.classes.delete(c); } },
    addEventListener(t, fn) { bell.handlers[t] = fn; },
  };
  const T = { bell, polls: [], fetched: [], href: "", focused: 0 };
  class Notification {
    constructor(title, opts) { this.title = title; this.opts = opts || {}; this.tab = name; shown.push(this); }
    close() { this.closed = true; }
    static requestPermission() { return Promise.resolve("granted"); }
  }
  Notification.permission = "granted";
  const ctx = {
    document: { title: "Session 状态", getElementById: id => (id === "mcbell" ? bell : null) },
    localStorage, Notification, Promise, JSON, Math, Date, String, encodeURIComponent,
    fetch: url => { T.fetched.push(url); const d = responses.shift(); return Promise.resolve({ ok: !!d, json: () => Promise.resolve(d) }); },
    setInterval: fn => { T.polls.push(fn); return 1; },
  };
  T.dispatched = [];
  ctx.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = (init || {}).detail; } };
  ctx.window = { Notification, addEventListener() {}, focus() { T.focused++; }, dispatchEvent(e) { T.dispatched.push(e.detail.blocked); } };
  ctx.location = { get href() { return T.href; }, set href(v) { T.href = v; } };
  vm.createContext(ctx);
  vm.runInContext(code, ctx);
  T.ctx = ctx;
  T.poll = () => T.polls[0]();
  return T;
}
const tick = () => new Promise(r => setImmediate(r));
const ev = (key, type, sid) => ({ seq: 0, type, session: sid, project: "demo", dedup_key: key,
                                  payload: { state_label: type === "PERMISSION_NEEDED" ? "等你授权 · 权限弹窗开着" : "等你回答 · Claude 在问你", age_s: 61 } });
const blk = [{ session_id: "S1", project: "demo", title: "修登录页", state_label: "等你回答 · Claude 在问你" }];

(async () => {
  try {
    // 1) 默认关; 第一次轮询只拿游标, 旧事件不补弹; 标题带上「(1) 等你」
    responses.push({ blocked: blk, events: [ev("K0", "QUESTION_PENDING", "S1")], seq: 5 });
    const A = tab("A");
    await tick(); await tick();
    expect("first poll asks since=-1", A.fetched[0] === "/api/attention?since=-1", A.fetched[0]);
    expect("off by default", A.bell.textContent.startsWith("🔕") && !A.bell.classes.has("on"), A.bell.textContent);
    expect("title shows waiting count", A.ctx.document.title === "(1) 等你 · Session 状态", A.ctx.document.title);
    expect("no replay on first poll", shown.length === 0);

    // 2) 关着的时候来了新事件: 不弹
    responses.push({ blocked: blk, events: [ev("K1", "QUESTION_PENDING", "S1")], seq: 6 });
    A.poll(); await tick(); await tick();
    expect("cursor advances", A.fetched[1] === "/api/attention?since=5", A.fetched[1]);
    expect("off -> silent", shown.length === 0);

    // 3) 打开: 要到权限后先弹一条「已打开」
    A.bell.handlers.click(); await tick(); await tick();
    expect("turned on", localStorage.getItem("mc.notify.on") === "1" && A.bell.textContent.startsWith("🔔"), A.bell.textContent);
    expect("hello notification", shown.length === 1 && shown[0].opts.tag === "mc-hello");

    // 4) 新的等你事件 -> 弹一条, 标签 = 去重键; 标题 / 正文带项目、会话标题、已等几分钟
    responses.push({ blocked: blk, events: [ev("K2", "QUESTION_PENDING", "S1")], seq: 7 });
    A.poll(); await tick(); await tick();
    const n = shown[1];
    expect("notifies once per episode", shown.length === 2 && n && n.opts.tag === "K2");
    expect("notification text", n && n.title === "等你回答 · demo" && n.opts.body.includes("修登录页") && n.opts.body.includes("1 分钟"), n && (n.title + " | " + n.opts.body));

    // 5) 第二个标签页收到同一段等待: 不重复弹 (共用 localStorage 的标记)
    responses.push({ blocked: blk, events: [], seq: 7 });
    const B = tab("B");
    await tick(); await tick();
    responses.push({ blocked: blk, events: [ev("K2", "QUESTION_PENDING", "S1")], seq: 7 });
    B.poll(); await tick(); await tick();
    expect("second tab does not duplicate", shown.length === 2, String(shown.length));

    // 6) 授权类事件的标题; 点通知 -> 回到 /sessions 那一行, 记一次「点开」
    responses.push({ blocked: [], events: [ev("K3", "PERMISSION_NEEDED", "S1")], seq: 8 });
    A.poll(); await tick(); await tick();
    const p = shown[2];
    expect("permission wording", p && p.title === "等你授权 · demo", p && p.title);
    p && p.onclick();
    expect("click goes to the row", A.href === "/sessions#s-S1" && A.focused === 1 && p.closed, A.href);
    const st = JSON.parse(localStorage.getItem("mc.notify.stats") || "{}");
    expect("week stats recorded", (st.shown || []).length === 2 && (st.clicked || []).length === 1, JSON.stringify(st));
    expect("title clears when nobody waits", A.ctx.document.title === "Session 状态", A.ctx.document.title);
    expect("page told when the count changes", JSON.stringify(A.dispatched) === "[1,0]", JSON.stringify(A.dispatched));

    // 7) 关掉之后不再弹
    A.bell.handlers.click(); await tick();
    responses.push({ blocked: blk, events: [ev("K4", "QUESTION_PENDING", "S1")], seq: 9 });
    A.poll(); await tick(); await tick();
    expect("turned off -> silent", localStorage.getItem("mc.notify.on") === "0" && shown.length === 3, String(shown.length));
  } catch (e) {
    errors.push("exception: " + (e && e.stack ? e.stack.split("\n").slice(0, 3).join(" | ") : e));
  }
  console.log(JSON.stringify({ errors, checks }));
  process.exit(errors.length ? 1 : 0);
})();
