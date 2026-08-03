# 远程审批 hook · 进化指引(给修改代码的 Chat)

> 来源:诊断/验证 Chat,2026-07-19
> 配套文档:`docs/HOOK_ISOLATED_TEST_GUIDE.md`(给人的操作指引)
> 性质:**诊断结论 + 最小改动建议**。不是架构提案,不含产品方向判断。

---

## 0. 硬约束(请先读完再动手)

本指引的**唯一目的是让远程审批功能可被安全测试**。请严格遵守:

1. **不得改变任何默认行为。** 远程模式默认关、失败一律 defer、绝不自动批准 —— 这些现有语义
   是设计核心,不得因为"顺手优化"被改动。
2. **不得让 tokmon 自动写入任何 Claude Code 配置文件。** 安装动作必须保持"生成片段 → 人手动粘贴"。
   自动写 `settings.json` 会污染用户环境,是明确禁止项。
3. **不扩大范围。** 本文件只列三项改动。附录里的「已登记待办」**不要在本轮一并做掉**。
4. **不动** `NORTH_STAR.md` / `README.md` / `MISSION_CONTROL.md` 等主文档。

---

## 1. 背景:已经验证到什么程度

### 1.1 已验证通过(阶段一,服务端契约 5/5)

用 curl 直接打 `/hook/permission` 端点,模拟 Claude Code 的调用:

| # | 场景 | 实测结果 | 判定 |
|---|------|---------|------|
| A | 错误令牌 | 0s 返回,无 `decision` | ✅ |
| B | 正确令牌 + 点「允许」 | 4s 返回 `"behavior":"allow"` | ✅ |
| C | 正确令牌 + 不点击 | **正好 25s** 返回无 `decision`,审计 `timeout→defer` | ✅ |
| D | 远程模式关 | 1s 返回,无 `decision` | ✅ |
| E | 正确令牌 + 点「拒绝」 | 3s 返回 `"behavior":"deny"` | ✅ |

**结论:服务端控制面无缺陷。** 鉴权、挂起、决定回传、超时回退、审计记录全部与代码设计一致,
阻塞时长与 `tokmon/control.py:33` 的 `_WAIT_S = 25.0` 精确吻合。

### 1.2 尚未验证(阶段一无法覆盖)

> **真实 Claude Code 会不会按这个形状调用 hook、能不能正确消费返回的 `decision`。**

代码作者本人已在 `tokmon/control.py:205` 标注了同样的保留意见。这一环只能靠端到端实测。

---

## 2. P0(阻塞项)· 校验并修正 hook 配置 schema

**优先级最高。这一项不解决,端到端测试即使失败也无法归因。**

### 问题

`tokmon/control.py:189-199` 的 `hook_config()` 生成:

```json
{"hooks":{"PermissionRequest":[{"matcher":"*","hooks":[{"type":"http","url":"...","timeout":30}]}]}}
```

查证 Claude Code 官方文档时,出现了**另一种形状**:

```json
{"hooks":[{"events":["PermissionRequest"],"matcher":"Bash","type":"http","url":"...","async":false}]}
```

两者结构不同:

| 差异点 | tokmon 生成 | 文档所示 |
|--------|------------|---------|
| 顶层 | 对象,按事件名做 key | 数组,用 `events` 字段声明 |
| 嵌套 | `matcher` 外层 + `hooks` 数组内层 | 扁平,单层 |
| 超时字段 | `timeout: 30` | `async: false` |

### 风险

若 tokmon 生成的格式不被当前版本识别,hook 会**静默不触发** —— 没有报错、没有任何现象。
用户会误判为"远程控制功能坏了",实际只是配置格式没被解析。

### 建议动作(最小)

1. **查证当前 Claude Code 版本实际接受哪种(或两种都接受)。** 以官方文档 + 实测为准,
   不要仅凭本文件的描述下结论。
2. 若现有格式已失效 → 修正 `hook_config()` 的输出。
3. 若两种都支持 → **无需改代码**,但请在 `hook_config()` 处补一条注释记录已查证结论,
   避免后人重复怀疑。

### 验收标准

在 `docs/HOOK_ISOLATED_TEST_GUIDE.md` 第 5 步中,hook 能被确认**实际触发**
(`/control` 收到 `PERMISSION_NEEDED` 事件)。

### 风险评估

低。仅改动一个生成字典的函数,不触碰审批逻辑。

---

## 3. P1 · 安装指引应提供「项目级隔离安装」选项

### 问题

`/control` 页面当前的文案是(`tokmon/serve.py:1587` 附近):

> 把下面这段合并进 `~/.claude/settings.json`,然后重启你的 Claude Code 会话生效。

这引导用户改**全局配置**。但生成的片段是 `matcher: "*"`,一旦装到全局:

- 拦截**所有项目、所有会话**的每一次工具授权
- 远程模式开着而人不在屏幕前时,**每次 permission 阻塞 25 秒**才回退本地弹窗

对日常使用是实质性干扰。

### 已查证事实(官方文档)

| 事实 | 影响 |
|------|------|
| hook 支持写在项目级 `.claude/settings.json` / `.claude/settings.local.json` | 存在隔离手段 |
| 项目级 hook **只对该项目的会话生效** | 隔离有效 |
| 多作用域的 hook **是叠加执行,不是覆盖** | 项目级不会干扰全局 |
| 存在 `"disallowAllHooks": true` 全局开关 | 有逃生舱 |

### 建议动作(最小)

在 `/control` 的安装说明里**增加**一段项目级安装选项,推荐用于首次测试:

> 想先隔离测试?把这段存成 `<某个测试目录>/.claude/settings.local.json`,
> 只有工作目录在该目录的会话才会加载,不影响你其它项目。

**注意:**
- 这是**文案 + 说明的增补**,不是行为变更。
- **不要**让 tokmon 自动去写这个文件(见第 0 节约束 2)。
- 全局安装的说明可以保留,但建议把项目级列为「推荐先做」。

### 验收标准

用户看完 `/control` 页面,无需额外解释就知道有隔离安装这条路。

### 风险评估

极低。纯文案/说明层改动,不涉及任何逻辑。

---

## 4. P2 · 修掉「加载中…」的 UX 陷阱

### 问题(已验证)

`/control` 页面「安装 hook」区域**永远显示「加载中…」**,看起来像卡死,实际必须用鼠标点它才会加载。

证据链:

| 位置 | 内容 |
|------|------|
| `tokmon/serve.py:1589` | `<pre id="snippet">加载中…</pre>` —— 静态占位文字 |
| `tokmon/serve.py:1704` | `$('#snippet').addEventListener('click', loadHook);` —— 只绑在**点击**上 |
| `tokmon/serve.py:1705` | 启动只跑 `load(); loadAsks();` —— **从不调用 `loadHook()`** |

用户实际反馈已证实这个坑会真的卡住人。

### 建议动作(二选一,都是最小改动)

**方案 A(推荐,一行):** 把静态占位文字从「加载中…」改成能自解释的提示,例如
「(点此粘贴控制令牌后显示 hook 配置)」。

**方案 B:** 页面加载时调用一次 `loadHook()`。
⚠️ 但注意:`loadHook()` 内部会调 `ensureToken()`,在没有令牌时会**弹出 prompt 输入框**。
一进页面就弹框打扰用户,体验可能更差。**若选 B,需先处理这个副作用。**

> 倾向方案 A:改动最小、无副作用,且保留了"用户主动索取令牌"的安全姿态。

### 验收标准

未粘贴令牌的用户打开 `/control`,能从文字本身看懂"需要我点一下",而不是以为页面卡了。

### 风险评估

方案 A 极低(改一个字符串)。方案 B 中等(引入非预期弹窗)。

---

## 5. 已登记待办(本轮**不要**做)

以下是诊断过程中发现、但**超出本次测试目的**的项。仅登记,由主开发 Chat 自行决定是否排期:

| 项 | 证据位置 | 说明 |
|----|---------|------|
| 隧道域名不在控制白名单 | `tokmon/serve.py:249-253` `_host_ok()` | 经 cloudflare 隧道远程访问时,控制类操作一律 403。代码注释已写明"现阶段控制面只走本机"。**这是有意的 DNS-rebinding 防护,不是 bug。** 若未来要支持远程控制,需要设计域名白名单方案 —— 属于安全设计变更,不应顺手做。 |
| `_MAX_PENDING = 32` 并发上限未测 | `tokmon/control.py:34,149` | 需并发 33 个请求才能触发,现阶段无验证必要。 |
| tokmon 进程生命周期 | — | 由 Chat 后台启动的实例会随会话回收。属于运行方式问题,非代码缺陷;已在人的操作指引中要求用户自行开终端运行。 |

---

## 6. 交接摘要

```
已验证: 服务端控制面 5/5 通过 (鉴权/allow/deny/超时/模式关), 无缺陷
未验证: Claude Code 端能否对接 hook 形状并消费 decision —— 需端到端实测

本轮建议只做三件事:
  P0  校验并(如需)修正 control.py:189-199 生成的 hook schema —— 阻塞项, 不解决无法归因
  P1  /control 安装说明增补「项目级隔离安装」选项 (文案层, 不改行为)
  P2  serve.py:1589 的「加载中…」占位文字改为自解释提示 (一行)

硬约束: 不改默认行为 / 不自动写用户配置 / 不扩大范围 / 不动主文档
```

```
EN handoff: Phase-1 server-side control-plane verified 5/5 (bad-token, allow, deny,
timeout-defer, remote-mode-off) — no defects; block duration matches _WAIT_S=25.0.
Still unverified: whether real Claude Code calls this hook shape and consumes the
returned decision. Three scoped changes proposed:
  P0 (blocker) verify/fix hook schema emitted by control.py:189-199 — a mismatch would
     make the hook silently no-op, making any e2e failure unattributable.
  P1 add project-level isolated-install guidance to the /control page (copy only, no
     behavior change) — global install with matcher:"*" blocks every permission up to 25s.
  P2 replace the misleading "加载中…" placeholder at serve.py:1589 (loadHook is click-bound
     only, never called on page load).
Hard constraints: no default-behavior changes, never auto-write user config files, no
scope expansion, do not touch NORTH_STAR/README/MISSION_CONTROL.
```
