# 远程审批 hook · 隔离测试操作指引(给人看)

> 目的:在**完全不污染**你现有 Claude Code 配置和任何现有项目的前提下,端到端验证
> 「Claude Code permission → tokmon 控制面 → 你点击 → 会话继续」这条链路是否真的能用。
>
> 编写日期:2026-07-19 · 依据:阶段一实测(5/5 通过)+ Claude Code 官方文档查证

---

## 0. 这个方案为什么不会污染你的环境

| 保证 | 机制 |
|------|------|
| 不动全局配置 | hook 只写进**一次性测试目录**的 `.claude/settings.local.json`,`~/.claude/settings.json` 一个字不改 |
| 不影响其它项目 | 官方文档确认:**项目级 hook 只对该项目的会话生效** |
| 不影响你日常会话 | 只有 cwd = 测试目录的会话才会加载这个 hook |
| 彻底还原 | 删掉测试目录 = 完全回到现在的状态,无残留 |
| 紧急逃生 | 任何时候可在全局设 `"disallowAllHooks": true` 一键停掉所有 hook |

**已确认前提**:你的 `~/.claude/settings.json` 目前**没有任何 hooks**(只有 `permissions` /
`effortLevel` / `autoUpdatesChannel`),所以不存在与现有 hook 冲突的问题。

---

## 1. 前置准备

### 1.1 建测试目录

```powershell
mkdir C:\Users\zwdzw\claude-hook-test\.claude
```

### 1.2 用**你自己的终端**启动 tokmon(重要)

```powershell
cd C:\Users\zwdzw\claude-token-monitor
python -m tokmon serve --port 8794
```

> ⚠️ **不要用诊断 Chat 起的实例**。那个实例的生命周期挂在 Chat 会话上,会话一结束就被回收;
> 一旦 tokmon 没了,hook 每次都要先 HTTP 失败再回退,测试会失真。
> 这个窗口**整个测试期间保持开着**。

### 1.3 确认远程模式先**关着**

打开 <http://127.0.0.1:8794/control>,确认「远程审批模式」是**关**。

> 为什么先关:远程模式开着时,每个 permission 请求会**阻塞等你点击最多 25 秒**。
> 在你准备好之前先关着,避免莫名其妙的卡顿。

---

## 2. 取 hook 配置片段

1. 在 <http://127.0.0.1:8794/control> 页面滚到底部「**安装 hook**」区域
2. 那里显示「加载中…」——**它不是在加载,你必须用鼠标点它**
   （已知 UX 问题:`loadHook()` 只绑在点击事件上,页面加载时不会自动调用)
3. 点击后弹出输入框,粘贴控制令牌

   令牌自取(在你自己的终端):
   ```powershell
   Get-Content $env:USERPROFILE\.tokmon\control_token
   ```
4. 片段出现后,点「复制」

片段形状大致如下(`***` 处是你的真实令牌):

```json
{
  "hooks": {
    "PermissionRequest": [
      {
        "matcher": "*",
        "hooks": [
          { "type": "http", "url": "http://127.0.0.1:8794/hook/permission?token=***", "timeout": 30 }
        ]
      }
    ]
  }
}
```

### ⚠️ 复制后必须核对两件事

| 检查项 | 为什么 |
|--------|--------|
| URL 里的端口是不是 **8794** | 端口不符 → hook 打到空地址 → 每次失败回退 |
| 令牌是不是完整(约 32 字符) | 截断的令牌 → 鉴权失败 → 永远 defer |

---

## 3. 写进隔离配置(不是全局!)

把片段**原样**存成:

```
C:\Users\zwdzw\claude-hook-test\.claude\settings.local.json
```

> ✅ 这个目录之前不存在 → 文件是全新的 → **不需要跟任何已有内容合并**,直接整段贴进去即可。
> ❌ 再次强调:**不要**写进 `C:\Users\zwdzw\.claude\settings.json`。

---

## 4. 开一个隔离会话

**新开**一个 Claude Code 会话,工作目录设为测试目录:

```powershell
cd C:\Users\zwdzw\claude-hook-test
claude
```

> hook 在会话启动时加载,所以**必须是新开的会话**,已经开着的不会生效。

---

## 5. 第一步不是测审批,而是确认 hook 到底有没有被加载 ⭐

**这是整个测试最关键的一步。** 如果跳过它,一旦后面没反应,你会分不清是
「远程控制功能坏了」还是「hook 配置格式压根没被识别」。

### 5.1 保持远程模式 = 关,先触发一次权限请求

在隔离会话里让 Claude 执行一个需要授权的操作,例如:

```
帮我在当前目录建一个文件 test.txt，内容写 hello
```

### 5.2 观察 tokmon 的 `/control` 页面

无论远程模式开关,代码都会发一条 `PERMISSION_NEEDED` 事件
(依据 `tokmon/control.py:142`,这是"检测"路径,不受开关影响)。

| 现象 | 结论 |
|------|------|
| 正常弹出本地权限弹窗,**且** tokmon 有反应(事件流出现 `PERMISSION_NEEDED`) | ✅ **hook 已生效**,可以进入第 6 步 |
| 正常弹出本地弹窗,但 tokmon **完全没反应** | ❌ **hook 没被加载或格式不识别** → 见第 8 节排查,**先别测审批** |

### 5.3 更直接的确认方式(可选)

用调试模式启动会话,能直接看到 hook 是否被调用:

```powershell
claude --debug
```

留意输出里有没有 hook 相关的执行记录或 `hook error` 提示。

---

## 6. 正式测试:远程批准

**确认第 5 步 hook 已生效后**再做这一步。

1. 在 `/control` 把「远程审批模式」切到 **开**
2. **人守在 `/control` 页面前**(或手机开着该页面)
3. 在隔离会话里再次触发一个需要授权的操作
4. 观察:

| 时间点 | 期望现象 |
|--------|---------|
| 3 秒内 | `/control` 的「待审批」区域冒出一条,显示工具名 + 命令摘要 |
| 你点「允许」后 | Claude Code 会话**立即继续执行**,不再弹本地弹窗 |
| 之后 | 「审计日志」多一行,`结果 = allow` |

5. 再测一次点「**拒绝**」:期望会话中止该操作,审计记 `deny`
6. 再测一次**什么都不点**:期望约 25 秒后**自动回退到正常的本地弹窗**,审计记 `timeout→defer`

---

## 7. 判定表(照着对号入座)

| 观察到的现象 | 结论 | 下一步 |
|-------------|------|--------|
| 待审批冒出 + 点击后会话继续 | ✅ **功能完全可用** | 测试完成 |
| 待审批冒出,但点了没反应 / 会话仍卡住 | ⚠️ 决定回传失败 | 记录审计日志内容,交给主开发 Chat |
| 待审批**不冒出**,但本地弹窗正常 | ❌ hook 未生效或 schema 不符 | 见第 8 节 |
| 会话卡住 25 秒后才弹本地窗 | ✅ 这是**正确的超时回退**,不是 bug | 符合设计 |
| 每次权限都卡很久且 tokmon 无反应 | ⚠️ hook 打到了错误地址 | 核对 URL 端口/令牌 |

---

## 8. 失败排查清单

按顺序排查:

1. **tokmon 还活着吗** — 浏览器能打开 <http://127.0.0.1:8794/control> 吗?
2. **端口对不对** — `settings.local.json` 里的 URL 端口 == tokmon 实际端口?
3. **令牌对不对** — 与 `~/.tokmon/control_token` 内容完全一致(无截断/空格)?
4. **会话是新开的吗** — hook 在会话启动时加载,老会话不生效
5. **cwd 对不对** — 会话的工作目录必须是 `C:\Users\zwdzw\claude-hook-test`
6. **JSON 合法吗** — 文件能被 `python -m json.tool` 解析通过吗?
7. **schema 对不对** — ⚠️ **这是当前最大的未知**。tokmon 生成的是
   `{"hooks":{"PermissionRequest":[{"matcher":"*","hooks":[{...}]}]}}`,
   而官方文档中另有一种 `{"hooks":[{"events":["PermissionRequest"],"type":"http",...}]}` 形状。
   **若第 1~6 项都正常但 tokmon 毫无反应,极可能就是这里。**
   → 把现象反馈给主开发 Chat,对应 `docs/HOOK_EVOLUTION_BRIEF.md` 的 **P0** 项。

---

## 9. 收尾 / 还原

测试结束后,任选:

```powershell
# 方式一:彻底删除(推荐,零残留)
Remove-Item -Recurse -Force C:\Users\zwdzw\claude-hook-test

# 方式二:只停用 hook,保留目录以便再测
#   把 settings.local.json 改名为 settings.local.json.bak
```

同时记得:
- 把 `/control` 的「远程审批模式」切回**关**
- 你自己终端里的 tokmon 可以 Ctrl+C 停掉

> 你的全局 `~/.claude/settings.json` 全程未被修改,无需还原。

---

## 10. 测试期间要记录什么(方便交回诊断/开发)

若出现异常,请记下:

- 第 5 步 hook 是否确认生效(是/否/不确定)
- `/control` 审计日志的完整内容
- 会话里是否出现 `hook error` 字样
- 从触发到弹窗的大致耗时(秒)
- `settings.local.json` 的实际内容(**令牌打码**)

---

## 附:安全提示

- hook 配置里含**明文令牌**。该文件在一次性目录中,测完即删,不要提交到任何 git 仓库。
- `matcher: "*"` 意味着在**该测试目录内**会拦截所有工具调用 —— 这正是我们把它隔离起来的原因。
