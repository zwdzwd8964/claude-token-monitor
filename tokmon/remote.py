"""远程暴露收口 (`MC_REMOTE`) —— 让看板能经隧道上手机, 且**不裸奔**。

[REMOTE_CONTROL_PLAN.md] §6 的落地。补的是平台**唯一一个会真的伤人的洞**:
在此之前 `_ctl_guard` 只护控制端点, 而 `/sessions` `/tokens` `/processes` `/api/*` **零鉴权** ——
一旦起隧道 (这台机器 2026-07-14 前真的起过), 拿到 URL 的人能看到**会话标题 / 当前步骤 / 思考片段 / 花费**。

**两道门, 故意不合并 (这是本模块最重要的设计):**
- **读门 (`_read_guard`)**: 远程模式下, 读页/读 API 要么带 **HttpOnly Cookie**(浏览器导航唯一可行的方式),
  要么带 `X-Control-Token` 头(命令行客户端)。
- **控制门 (`_ctl_guard`)**: **只认自定义头, 永不认 Cookie。** 自定义头跨站发不出去(会触发 CORS 预检),
  这正是控制面今天的抗 CSRF 性质。**若哪天有人"顺手统一"成读门那套, 控制面就会被 CSRF 打穿。**

**失败安全 (P7 ⑤)**: 配置不自洽 -> **拒绝启动**, 绝不"带着半个洞跑起来":
- 绑到非本机地址却没开 `MC_REMOTE` -> 拒绝 (那等于局域网裸奔);
- 开了 `MC_REMOTE` 却没给 `MC_REMOTE_HOSTS` -> 拒绝 (没有白名单就没有 DNS-rebinding 防护);
- 开了 `MC_REMOTE` 却没有控制令牌 -> 拒绝 (唯一的闸不存在)。

**token-only 的诚实边界 (§6 原文, 别省)**: 隧道 URL 当秘密(用 trycloudflare 随机域名, 别贴公开处);
令牌高熵可轮换(泄露即换 `~/.tokmon/control_token`); **仅在需要时开隧道, 用完即关**。
它的入站攻击面**大于** Telegram 的零端口长轮询 —— Telegram 双向仍是更安全的终态。

P4: 只 import stdlib。纯函数为主, 便于单测 (同 `project.py` / `classify_state` 的 I2 精神)。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie

ENV_ENABLED = "MC_REMOTE"
ENV_HOSTS = "MC_REMOTE_HOSTS"

COOKIE_NAME = "mc_read"
COOKIE_MAX_AGE = 12 * 3600        # 12h: 呼应"仅在需要时开隧道, 用完即关", 不做长期免登

# 本机 Host 的全部合法写法 (含空 Host: 本机非浏览器客户端如 curl/健康探测)
LOCAL_HOSTS = frozenset({"", "127.0.0.1", "localhost", "::1", "[::1]"})

_TRUE = {"1", "true", "yes", "on"}


def normalize_host(raw: str | None) -> str:
    """Host 头 -> 纯主机名 (小写, 去端口)。IPv6 字面量按 `[::1]:8765` 形状正确剥离。

    (旧实现直接 `rsplit(":",1)`, 遇到无端口的 `[::1]` 会切成 `[:` 而误拒 —— 这里一并修掉。)
    """
    h = (raw or "").strip().lower()
    if h.startswith("["):                       # [::1] / [::1]:8765
        end = h.find("]")
        return h[:end + 1] if end != -1 else h
    if ":" in h:
        head, _, tail = h.rpartition(":")
        if tail.isdigit():                      # 只有真的是端口才剥, 免得切坏含 ':' 的怪 Host
            return head
    return h


@dataclass(frozen=True)
class RemoteConfig:
    """远程暴露配置 (不可变: 启动时定一次, 运行中不可被请求改)。"""

    enabled: bool = False
    hosts: frozenset[str] = frozenset()

    def host_allowed(self, raw_host: str | None) -> bool:
        """DNS-rebinding 防护: 本机 Host 永远放行; 远程模式下额外放行**显式登记**的隧道域名 (非通配)。"""
        h = normalize_host(raw_host)
        if h in LOCAL_HOSTS:
            return True
        return self.enabled and h in self.hosts

    def cookie_secure(self) -> bool:
        """远程模式的 Cookie 必须 Secure: 隧道是 https, 不给它任何退回明文的机会。"""
        return self.enabled


def from_env(env: dict | None = None) -> RemoteConfig:
    e = os.environ if env is None else env
    enabled = str(e.get(ENV_ENABLED, "")).strip().lower() in _TRUE
    hosts = frozenset(
        normalize_host(h) for h in str(e.get(ENV_HOSTS, "")).split(",") if h.strip()
    )
    return RemoteConfig(enabled=enabled, hosts=hosts)


def preflight(cfg: RemoteConfig, bind_host: str, token: str | None) -> str | None:
    """启动前自洽性检查。返回 None = 可以起; 返回字符串 = **拒绝启动**的理由 (失败安全)。"""
    bind = normalize_host(bind_host)
    if not cfg.enabled:
        if bind not in LOCAL_HOSTS:
            return (
                f"拒绝启动: 绑定到 {bind_host} (非本机) 却没开 {ENV_ENABLED} —— "
                f"看板会在局域网裸奔 (会话标题/思考片段/花费全可见)。\n"
                f"  要么去掉 --host, 要么开远程模式: 设 {ENV_ENABLED}=1 且 {ENV_HOSTS}=<你的域名>"
            )
        return None
    if not cfg.hosts:
        return (
            f"拒绝启动: 开了 {ENV_ENABLED} 却没设 {ENV_HOSTS} —— "
            f"没有 Host 白名单就没有 DNS-rebinding 防护。\n"
            f"  例: {ENV_HOSTS}=random-words-1234.trycloudflare.com"
        )
    if not token:
        return f"拒绝启动: 开了 {ENV_ENABLED} 却没有控制令牌 —— 远程模式下它是唯一的闸。"
    return None


def cookie_token(cookie_header: str | None) -> str | None:
    """从 Cookie 头取读令牌。解析失败 -> None (不抛, 渐进降级)。"""
    if not cookie_header:
        return None
    try:
        jar = SimpleCookie()
        jar.load(cookie_header)
    except Exception:
        return None
    m = jar.get(COOKIE_NAME)
    return m.value if m else None


def build_set_cookie(value: str, secure: bool, max_age: int = COOKIE_MAX_AGE) -> str:
    """HttpOnly (JS 读不到, 页面 XSS 也偷不走) + SameSite=Strict (跨站不带) + 限时。"""
    bits = [
        f"{COOKIE_NAME}={value}",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        f"Max-Age={int(max_age)}",
    ]
    if secure:
        bits.append("Secure")
    return "; ".join(bits)


def clear_cookie(secure: bool) -> str:
    return build_set_cookie("", secure=secure, max_age=0)


class LoginThrottle:
    """登录失败节流: 令牌是 192bit 高熵, 暴力破解本就不现实; 这一层是为了**让你在终端看见有人在试**。

    线程安全靠 GIL 下的简单自增即可 (计数不精确无所谓, 它只用于提示); 不做 IP 级封禁 ——
    隧道后面所有请求都来自 cloudflared 的边缘 IP, 按 IP 封会误伤你自己。
    """

    def __init__(self, delay_s: float = 1.0, warn_every: int = 5) -> None:
        self.delay_s = delay_s
        self.warn_every = warn_every
        self.failures = 0
        self.last_failure = 0.0

    def on_failure(self) -> str | None:
        """返回需要打印的告警行 (没到台阶则 None)。调用方负责 sleep(delay_s)。"""
        self.failures += 1
        self.last_failure = time.time()
        if self.warn_every and self.failures % self.warn_every == 0:
            return (f"⚠ 远程登录已失败 {self.failures} 次 —— 若不是你本人, "
                    f"立即关隧道并轮换 ~/.tokmon/control_token")
        return None

    def on_success(self) -> None:
        self.failures = 0
