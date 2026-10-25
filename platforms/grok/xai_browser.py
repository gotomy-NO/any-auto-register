"""xAI 浏览器注册 + 本地 OAuth 授权（Camoufox）。

移植自 progrok backend/xai_browser.py，按 register-8000 现状裁剪：
- 浏览器统一 Camoufox（排除 UBO 插件，规避 P5 空目录自愈坑）
- 注册循环：邮箱注册 → 收码 → 资料/密码 → Turnstile → 提交 → 落地确认
- 登录 + accounts.x.ai/account 会话稳定化 + OAuth consent 点击
- 回调捕获：127.0.0.1 重定向 / consent 页渲染授权码（OOB）双路径
- 本模块只产出「回调 URL 或页面授权码」，PKCE 会话与兑换在 xai_pkce.py
"""

from __future__ import annotations

import re
import secrets
import shutil
import subprocess
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from core.browser_runtime import (
    ensure_browser_display_available,
    resolve_browser_headless,
)
from core import camoufox_breaker as _cb


XAI_SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"
XAI_SIGNIN_URL = "https://accounts.x.ai/sign-in?redirect=grok-com"
XAI_ACCOUNT_URL = "https://accounts.x.ai/account"
XAI_GROK_URL = "https://grok.com/"
TURNSTILE_WAIT_SEC = 90.0
# Turnstile 拒绝后的跨会话重试退避（秒）。10-03 18:14 批次实测：同出口连续
# 注册，前半 6/7 成、尾部 3 连败——同出口高频后 CF 开始发必败挑战。此时同
# 指纹硬撞无意义，必须整会话重建（新指纹）+ 换邮箱 + 指数退避。
TURNSTILE_RETRY_BACKOFF_SEC = (20.0, 45.0, 90.0)
TURNSTILE_CLICK_COOLDOWN_SEC = 2.0
TURNSTILE_MAX_CLICK_ATTEMPTS = 3
TURNSTILE_CLICK_EFFECT_WAIT_MS = 400
TURNSTILE_MOUNT_GRACE_SEC = 4.0
PAGE_SETTLE_DELAY_MS = 600
REGISTRATION_LANDING_TIMEOUT_SEC = 60.0
SIGNUP_FORM_TIMEOUT_SEC = 15.0
SIGNUP_SUBMIT_TIMEOUT_SEC = 35.0
SIGNUP_SUBMIT_RETRY_SEC = 4.0
SIGNUP_SUBMIT_MAX_ATTEMPTS = 3
SIGNUP_TRANSITION_STABLE_POLLS = 3
ACTION_TRANSITION_RETRY_SEC = 4.0
ACTION_TRANSITION_MAX_ATTEMPTS = 3
LOGIN_TIMEOUT_SEC = 150.0
OAUTH_CONSENT_TIMEOUT_SEC = 120.0
# authorize 跳变抢回：accounts.x.ai 有概率把 authorize 首跳弹到 /account 再 20s 左右
# 自动跳 grok.com（2026-10-03 10:54 回归；同代码昨天/今天复现 run 均直达 consent）。
# /account 页面 Buttons 明确含 "Return to Grok" —— 服务端 session 路由抖动。
# 处置：非 consent/回调态时用同一个 auth_url 原样 re-goto 抢回（PKCE 参数不变，
# authorize 端点幂等于 GET），间隔 5s、最多 3 次；4 次仍未回到 consent 才放行失败。
OAUTH_REGOTO_MAX_ATTEMPTS = 3
OAUTH_REGOTO_INTERVAL_SEC = 5.0
GROK_COM_HOST = "grok.com"

# Firefox 后台节流会让 React/Turnstile 定时器停摆（headed 批跑时窗口被压后台）
VISIBLE_BACKGROUND_PREFS = {
    "widget.windows.window_occlusion_tracking.enabled": False,
    "dom.timeout.enable_budget_timer_throttling": False,
    "dom.min_background_timeout_value": 10,
    "media.suspend-bkgnd-video.enabled": False,
    "browser.tabs.unloadOnLowMemory": False,
}

_FIRST_NAMES = (
    "Aiden", "Caleb", "Ethan", "Lucas", "Mason", "Nolan", "Owen", "Ryan",
    "Sophie", "Chloe", "Emma", "Grace", "Mia", "Nora", "Ruby", "Zoe",
)
_LAST_NAMES = (
    "Bennett", "Carter", "Collins", "Foster", "Hayes", "Morgan", "Parker",
    "Reed", "Ross", "Scott", "Turner", "Walker", "Ward", "Wells", "Young",
)

EMAIL_INPUT_SELECTORS = [
    'input[type="email"]',
    'input[name="email"]',
    'input[autocomplete="email"]',
    'input[placeholder*="email" i]',
]
PASSWORD_INPUT_SELECTORS = [
    'input[type="password"]',
    'input[name*="password" i]',
    'input[autocomplete="new-password"]',
]
LOGIN_PASSWORD_SELECTORS = [
    'input[type="password"]',
    'input[name*="password" i]',
    'input[autocomplete="current-password"]',
]


class XaiBrowserError(RuntimeError):
    """浏览器注册/登录/授权无法继续。"""


class XaiOAuthAccessDenied(XaiBrowserError):
    """xAI 拒绝 OAuth 授权（已登录账号被风控）。"""


class XaiTurnstileRejected(XaiBrowserError):
    """headless Turnstile 挑战未通过（含等待超时）。

    单独成类的原因：这只代表「CF 挑战没过」，账号尚未注册成功、邮箱可换，
    上层应当「换新会话（新指纹）+ 换邮箱 + 退避」重试；而其他 XaiBrowserError
    （表单异常、邮箱被拒等）重试语义完全不同，不能混为一谈。
    """


class XaiBrowserCancelled(XaiBrowserError):
    """任务取消。"""


def _ensure_camoufox() -> None:
    try:
        from camoufox.sync_api import Camoufox

        del Camoufox
    except ImportError as exc:
        raise RuntimeError("xAI 浏览器注册需要 Camoufox，请先安装依赖") from exc


def split_proxy(proxy_url: str) -> dict:
    """'http://user:pass@host:port' → playwright proxy 参数。

    Chromium/Firefox 不接受 URL 内嵌认证，必须拆独立 username/password 字段。
    """
    if not proxy_url:
        return {}
    from urllib.parse import urlparse

    p = urlparse(proxy_url)
    scheme = p.scheme or "http"
    netloc = p.netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[-1]
    proxy = {"server": f"{scheme}://{netloc}", "bypass": "127.0.0.1,localhost,::1"}
    if p.username:
        proxy["username"] = p.username
    if p.password:
        proxy["password"] = p.password
    return proxy


def _launch_geoip_kwargs(proxy_config: dict | str | None) -> dict:
    """geoip 一致化启动参数（带回退链）。

    返回 {"geoip": True, "disable_coop": True, "humanize": True, "locale": "en-US"}；
    GeoIP 库不可用/下载失败时逐级降级：无 geoip 也要 disable_coop + humanize + locale，
    绝不让 geoip 失败炸掉整个注册流程。测试可通过 monkeypatch 本函数注入桩。

    locale 显式钉死 "en-US"（20261008 双保险①）：geoip 会按出口 IP 推导界面语言，
    东京出口派生 ja-JP → accounts.x.ai 整页日文「メールで登録」→ 注册流程正则不匹配、
    每号空转超时。camoufox 内部显式 locale 的注入（utils.py handle_locales）晚于 geoip
    分支，会覆盖 geoip 推导的语言，而时区/经纬度仍由 geoip 保持与出口一致——既消除
    语言错配，又不破坏指纹-IP 一致化。geoip 不可用时 locale 依旧生效（不依赖 geoip 分支）。
    """
    kwargs: dict[str, Any] = {
        "disable_coop": True,
        "humanize": True,
        "locale": "en-US",
    }
    try:
        from camoufox.geolocation import geoip_allowed

        geoip_allowed()  # GeoIP extra 未装会在此抛 NotInstalledGeoIPExtra
        kwargs["geoip"] = True
    except Exception as exc:
        # 记最小信息：不影响流程，只是指纹-IP 一致化降级
        print(f"[xai] geoip 不可用（{type(exc).__name__}），降级为无 geoip 启动")
    del proxy_config
    return kwargs


# 启动失败清理由：camoufox 在异常里自带 `<launched> pid=NNNN` 与临时 profile 路径，
# 精准收割该 PID（/T 连同其 contentproc），并删除残留 profile 目录。
# 只按异常自带 PID 杀，绝不按名字/端口误伤 :8889 solver 浏览器池。
_LAUNCH_PID_RE = re.compile(r"<launched>\s+pid=(\d+)")
_LAUNCH_PROFILE_RE = re.compile(r"-profile\s+(\S+playwright_firefoxdev_profile-\S+)")


def reap_camoufox_launch_failure(exc: BaseException) -> None:
    """启动失败后回收泄漏的 camoufox.exe 与其临时 profile（尽力而为，绝不抛出）。"""
    text = str(exc or "")
    pids = sorted(set(_LAUNCH_PID_RE.findall(text)))
    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", pid],
                capture_output=True,
                timeout=15,
            )
        except Exception:  # noqa: BLE001
            pass
    for prof in set(_LAUNCH_PROFILE_RE.findall(text)):
        try:
            shutil.rmtree(prof, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


class XaiBrowserSession:
    """单账号私有 browser context：注册、登录、OAuth 授权全程使用。"""

    def __init__(
        self,
        *,
        proxy: str = "",
        headless: bool = True,
        on_progress: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        operation_delay_ms: int = 0,
        timeout_sec: float = 420.0,
    ) -> None:
        self.proxy = str(proxy or "")
        self.headless = bool(headless)
        self.on_progress = on_progress
        self.should_cancel = should_cancel
        self.operation_delay_ms = max(0, min(30_000, int(operation_delay_ms or 0)))
        self.deadline = time.time() + max(30.0, float(timeout_sec))
        self.browser = None
        self.context = None
        self.page = None
        self.using_camoufox = False
        self.steps: list[dict[str, Any]] = []

    def _progress(self, step: str, state: str, **extra: Any) -> None:
        self.steps.append({"step": step, "status": state, "at": time.time(), **extra})
        if not self.on_progress:
            return
        message = f"[xai] {step}: {state}"
        detail = " ".join(
            f"{k}={v}" for k, v in extra.items() if v not in (None, "")
        )
        if detail:
            message += f" ({detail})"
        try:
            self.on_progress(message)
        except XaiBrowserCancelled:
            raise
        except Exception:
            pass

    def _check_cancel(self) -> None:
        if self.should_cancel and self.should_cancel():
            raise XaiBrowserCancelled("registration cancelled by user")
        if time.time() > self.deadline:
            raise XaiBrowserError("xAI 浏览器流程超时")

    def _wait(self, milliseconds: int) -> None:
        remaining = max(0, int(milliseconds))
        while remaining > 0:
            self._check_cancel()
            chunk = min(200, remaining)
            if self.page is not None:
                self.page.wait_for_timeout(chunk)
            else:
                time.sleep(chunk / 1000.0)
            remaining -= chunk

    def _action_delay(self) -> None:
        self._wait(self.operation_delay_ms)

    def _settle_page(self) -> None:
        """等导航与渲染；xAI 常驻后台请求，networkidle 仅尽力而为。"""
        self._check_cancel()
        if self.page is None:
            return
        self._wait(250)
        for state, timeout in (("domcontentloaded", 5_000), ("networkidle", 2_000)):
            self._check_cancel()
            try:
                self.page.wait_for_load_state(state, timeout=timeout)
            except Exception:
                pass
        self._wait(PAGE_SETTLE_DELAY_MS)

    # ---------- 浏览器生命周期 ----------

    def _camoufox_start(self, launch_kwargs: dict) -> Any:
        """Camoufox 启动带回退：geoip 首启要联网探测出口 IP（ipecho.net 等），
        探测失败会炸启动（21:11 批内 2 单实锤）。降级顺序：
        ①原样 → ②去 geoip（保留 disable_coop/humanize）→ ③最简启动。
        绝不因指纹增强项把注册流程整体打死。"""
        from camoufox.sync_api import Camoufox

        attempts = [
            ("geoip", dict(launch_kwargs)),
            (
                "no-geoip",
                {k: v for k, v in launch_kwargs.items() if k != "geoip"},
            ),
            (
                "minimal",
                {
                    k: v
                    for k, v in launch_kwargs.items()
                    if k in ("proxy", "headless", "exclude_addons")
                },
            ),
        ]
        last_exc: Exception | None = None
        for label, kwargs in attempts:
            try:
                browser = Camoufox(**kwargs).start()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                # 启动失败即回收本次泄漏的浏览器进程/profile（camoufox 超时后
                # 未必自行杀掉原生进程，长时间只连不返会堆积成孤儿 → 争用）。
                reap_camoufox_launch_failure(exc)
                if label != "geoip":
                    self._progress("init", "camoufox_start_fallback", mode=label)
            else:
                # 启动成功 → 宿主浏览器可用，复位熔断计数（幂等，写盘极廉价）。
                try:
                    _cb.record_launch_success()
                except Exception:  # noqa: BLE001
                    pass
                return browser
        # 三条回退全败：若属启动超时类失败（Camoufox.start/Timeout/
        # RenderCompositorSWGL/Juggler）→ 计入 camoufox 启动熔断；连续 3 次 →
        # OPEN(host_browser_broken)，批循环/守护/执行器据此停批止住白烧。
        try:
            _cb.record_launch_failure(str(last_exc))
        except Exception:  # noqa: BLE001
            pass
        raise XaiBrowserError(f"Camoufox 启动失败：{last_exc}") from last_exc

    def _launch(self) -> None:
        self._progress("init", "starting")
        _ensure_camoufox()
        headless, reason = resolve_browser_headless(self.headless, default_headless=False)
        ensure_browser_display_available(headless)
        self.headless = headless
        self._progress("init", "browser_mode", mode="headless" if headless else "headed", via=reason)
        from camoufox.addons import DefaultAddons
        from camoufox.sync_api import Camoufox

        launch_kwargs: dict[str, Any] = {
            "headless": headless,
            # P5 坑：UBO 插件下载 451 留空目录会让 camoufox 启动崩溃，直接排除
            "exclude_addons": [DefaultAddons.UBO],
        }
        if self.proxy:
            launch_kwargs["proxy"] = split_proxy(self.proxy)
        # geoip 一致化（CF过盾调研-20261003 方案一）：geoip=True 让时区/经纬度/
        # locale/语言/WebRTC 全部跟随代理出口 IP，消除「中国时区+美国出口」的
        # 跨地域矛盾——CF 对指纹-IP 不一致最敏感。GeoIP 库已随 camoufox[geoip]
        # 装好（首次下载失败的兜底路径见 _launch_geoip_kwargs）。
        # disable_coop：官方文档明示用于允许点击 challenges.cloudflare.com 跨域
        # iframe 内的 Turnstile 元素——与 _click_turnstile_challenge 直接相关。
        # humanize：类人光标移动，交互式挑战的行为分。
        launch_kwargs.update(_launch_geoip_kwargs(launch_kwargs.get("proxy")))
        self.browser = self._camoufox_start(launch_kwargs)
        self.using_camoufox = True
        self.context = self.browser.new_context()
        self._progress("init", "private_context_created")
        self.page = self.context.new_page()

    def open(self) -> None:
        """启动浏览器并打开注册页。"""
        try:
            self._launch()
            assert self.page is not None
            self._progress("navigate", "loading")
            self._action_delay()
            self.page.goto(XAI_SIGNUP_URL, wait_until="domcontentloaded", timeout=90_000)
            self._settle_page()
            self._progress("navigate", "ready")
        except (XaiBrowserCancelled, XaiBrowserError):
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise XaiBrowserError(f"xAI 浏览器启动失败：{exc}") from exc

    def close(self) -> None:
        """清理账号态后关闭（保留浏览器进程复用价值为 0，直接整体关闭）。"""
        try:
            if self.page is not None:
                self.page.evaluate(
                    """async () => {
                        try { localStorage.clear(); } catch (e) {}
                        try { sessionStorage.clear(); } catch (e) {}
                        try {
                            const keys = await caches.keys();
                            await Promise.all(keys.map((key) => caches.delete(key)));
                        } catch (e) {}
                    }"""
                )
        except Exception:
            pass
        try:
            if self.context is not None:
                self.context.clear_cookies()
        except Exception:
            pass
        try:
            if self.browser is not None:
                self.browser.close()
        except Exception:
            pass
        self.page = None
        self.context = None
        self.browser = None
        self.using_camoufox = False

    def hold_failure(self, *, seconds: float = 20.0) -> None:
        """headed 模式失败时停留一会儿，便于人工查看现场。"""
        if self.headless or self.page is None:
            return
        self._progress("flow", "debug_hold", seconds=int(max(0, seconds)))
        try:
            self._wait(int(max(0.0, seconds) * 1000))
        except Exception:
            pass

    # ---------- DOM 基础操作 ----------

    def _first_visible(self, selectors: list[str]) -> Any | None:
        if self.page is None:
            return None
        for selector in selectors:
            try:
                locator = self.page.locator(selector)
                for index in range(min(10, int(locator.count()))):
                    candidate = locator.nth(index)
                    if candidate.is_visible():
                        return candidate
            except Exception:
                continue
        return None

    def _page_text(self) -> str:
        if self.page is None:
            return ""
        try:
            return str(self.page.locator("body").inner_text(timeout=1_500) or "")
        except Exception:
            return ""

    def _find_action(self, pattern: str, *, allow_submit_fallback: bool = True, exclude_pattern: str | None = None) -> Any | None:
        if self.page is None:
            return None
        regex = re.compile(pattern, re.I)
        deny = re.compile(exclude_pattern, re.I) if exclude_pattern else None
        for role in ("button", "link"):
            try:
                locator = self.page.get_by_role(role, name=regex)
                for index in range(min(10, int(locator.count()))):
                    candidate = locator.nth(index)
                    if not (candidate.is_visible() and candidate.is_enabled()):
                        continue
                    if deny is not None:
                        try:
                            label = str(candidate.inner_text(timeout=400) or "")
                        except Exception:
                            label = ""
                        if deny.search(label):
                            continue
                    return candidate
            except Exception:
                continue
        if not allow_submit_fallback:
            return None
        try:
            submit = self.page.locator('button[type="submit"], input[type="submit"]')
            for index in range(min(10, int(submit.count()))):
                candidate = submit.nth(index)
                if candidate.is_visible() and candidate.is_enabled():
                    return candidate
        except Exception:
            pass
        return None

    def _click_locator_center(self, locator: Any, *, timeout: int = 1_500) -> bool:
        """xAI 按钮会在 click 稳定性检查期间重渲染——按中心点发真实鼠标事件。"""
        try:
            locator.scroll_into_view_if_needed(timeout=timeout)
        except Exception:
            pass
        try:
            box = locator.bounding_box(timeout=timeout)
        except Exception:
            box = None
        if isinstance(box, dict) and box.get("width") and box.get("height"):
            x = float(box["x"]) + float(box["width"]) / 2.0
            y = float(box["y"]) + float(box["height"]) / 2.0
            try:
                self.page.mouse.click(x, y)
                return True
            except Exception:
                pass
        try:
            locator.click(timeout=timeout, force=True)
            return True
        except Exception:
            return False

    def _click_action(self, pattern: str, *, allow_submit_fallback: bool = True) -> bool:
        candidate = self._find_action(pattern, allow_submit_fallback=allow_submit_fallback)
        if candidate is None:
            return False
        return self._click_locator_center(candidate, timeout=3_000)

    def _submit(self, anchor: Any | None, pattern: str) -> None:
        self._action_delay()
        if self._click_action(pattern):
            return
        if anchor is None:
            raise XaiBrowserError("xAI 页面没有可用的继续按钮")
        anchor.press("Enter")

    def _wait_for_email_signup_form(self, *, timeout_sec: float = SIGNUP_FORM_TIMEOUT_SEC) -> bool:
        deadline = min(self.deadline, time.time() + max(0.1, timeout_sec))
        while time.time() < deadline:
            self._check_cancel()
            if self._first_visible(EMAIL_INPUT_SELECTORS) is not None:
                return True
            self._wait(200)
        return False

    def _select_email_signup(self) -> None:
        """选择邮箱注册入口，并确认邮箱表单真的出现（动态重试 3 次）。"""
        pattern = r"^sign up with email$|使用邮箱.*注册|邮箱注册"
        per_attempt = max(1.0, SIGNUP_FORM_TIMEOUT_SEC / ACTION_TRANSITION_MAX_ATTEMPTS)
        for attempt in range(1, ACTION_TRANSITION_MAX_ATTEMPTS + 1):
            self._check_cancel()
            self._progress("signup_method", "clicking", attempt=attempt)
            self._action_delay()
            if self._click_action(pattern, allow_submit_fallback=False):
                if self._wait_for_email_signup_form(timeout_sec=per_attempt):
                    return
            if attempt < ACTION_TRANSITION_MAX_ATTEMPTS:
                self._progress("signup_method", "retrying", attempt=attempt + 1)
        raise XaiBrowserError("xAI 邮箱注册入口点击后页面未切换，已动态重试 3 次")

    # ---------- Turnstile ----------

    def _turnstile_status(self) -> str:
        """absent / pending / finishing / passed（以响应 token 写入为准）。"""
        if self.page is None:
            return "absent"
        seen = False
        try:
            responses = self.page.locator(
                'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
            )
            seen = int(responses.count()) > 0
            for index in range(min(5, int(responses.count()))):
                value = str(responses.nth(index).input_value(timeout=500) or "").strip()
                if len(value) >= 20:
                    return "passed"
        except Exception:
            pass
        try:
            containers = self.page.locator(
                ".cf-turnstile, [data-sitekey], [data-turnstile-widget]"
            )
            for index in range(min(5, int(containers.count()))):
                if containers.nth(index).is_visible(timeout=300):
                    seen = True
                    break
        except Exception:
            pass
        try:
            frames = self.page.locator('iframe[src*="challenges.cloudflare.com"]')
            for index in range(min(5, int(frames.count()))):
                if frames.nth(index).is_visible():
                    seen = True
        except Exception:
            pass
        success_seen = False
        try:
            for frame in self.page.frames:
                if "challenges.cloudflare.com" not in str(frame.url or ""):
                    continue
                seen = True
                text = str(frame.locator("body").inner_text(timeout=500) or "")
                if re.search(r"success|验证成功", text, re.I):
                    success_seen = True
        except Exception:
            pass
        if success_seen:
            # 页面显示 Success 但回调还没把 token 写进父表单——不能据此提交
            return "finishing"
        return "pending" if seen else "absent"

    def _locator_identity(self, locator: Any) -> str | None:
        try:
            handle = locator.element_handle(timeout=300)
        except Exception:
            handle = None
        if handle is not None:
            try:
                identity = handle.evaluate(
                    """(node) => {
                        if (!node) return '';
                        const src = String(node.getAttribute?.('src') || node.src || '');
                        const title = String(node.getAttribute?.('title') || node.title || '');
                        const name = String(node.getAttribute?.('name') || node.name || '');
                        const idAttr = String(node.id || '');
                        const cls = String(node.className || '');
                        const tag = String(node.tagName || '').toLowerCase();
                        const rect = node.getBoundingClientRect?.() || {};
                        const box = [
                            Math.round(rect.x || 0), Math.round(rect.y || 0),
                            Math.round(rect.width || 0), Math.round(rect.height || 0),
                        ].join(',');
                        return [tag, idAttr, name, title, src, cls, box].join('|');
                    }"""
                )
                value = str(identity or "").strip()
                if value:
                    return value
            except Exception:
                pass
        try:
            box = locator.bounding_box(timeout=300)
        except Exception:
            box = None
        if isinstance(box, dict):
            return "|".join(
                str(round(float(box.get(key) or 0.0), 1))
                for key in ("x", "y", "width", "height")
            )
        return None

    def _turnstile_iframe_locators(self) -> list[Any]:
        """可见的 Cloudflare 挑战 iframe（选择器收窄，避免误配无关 embed）。"""
        if self.page is None:
            return []
        iframe_selectors = (
            'iframe[src*="challenges.cloudflare.com"]',
            'iframe[src*="turnstile"]',
            'iframe[title*="cloudflare" i]',
            'iframe[title*="turnstile" i]',
            'iframe[title*="security challenge" i]',
        )
        iframes: list[Any] = []
        seen: set[str] = set()
        for selector in iframe_selectors:
            try:
                frames = self.page.locator(selector)
                count = min(3, int(frames.count()))
            except Exception:
                continue
            for index in range(count):
                iframe = frames.nth(index)
                try:
                    if not iframe.is_visible(timeout=300):
                        continue
                except Exception:
                    continue
                identity = self._locator_identity(iframe) or f"{selector}#{index}"
                if identity in seen:
                    continue
                seen.add(identity)
                iframes.append(iframe)
        return iframes

    def _turnstile_frame_content(self, iframe: Any) -> Any | None:
        try:
            handle = iframe.element_handle(timeout=500)
            if handle is None:
                return None
            return handle.content_frame()
        except Exception:
            return None

    def _turnstile_interactive_markers(self, content: Any) -> bool:
        """启发式：是否真人点击型挑战（托管自动通过型绝不能点）。"""
        try:
            text = str(content.locator("body").inner_text(timeout=400) or "")
        except Exception:
            text = ""
        if re.search(
            r"verify you are human|i'?m not a robot|确认您是真人|进行人机验证|请完成以下操作",
            text,
            re.I,
        ):
            return True
        for selector in (
            'input[type="checkbox"]',
            ".cb-lb",
            ".cb-i",
            'label[class*="cb"]',
            "#challenge-stage",
        ):
            try:
                node = content.locator(selector).first
                if int(node.count()) > 0:
                    return True
            except Exception:
                continue
        return False

    def _click_iframe_checkbox_region(self, iframe: Any, *, timeout: int = 1_500) -> bool:
        """点击 Turnstile iframe 左侧复选框区。"""
        try:
            if not iframe.is_visible(timeout=timeout):
                return False
            iframe.scroll_into_view_if_needed(timeout=timeout)
        except Exception:
            pass
        try:
            box = iframe.bounding_box(timeout=timeout)
        except Exception:
            box = None
        if not isinstance(box, dict) or not box.get("width") or not box.get("height"):
            return self._click_locator_center(iframe, timeout=timeout)
        x = float(box["x"]) + min(28.0, max(12.0, float(box["width"]) * 0.12))
        y = float(box["y"]) + float(box["height"]) / 2.0
        try:
            self.page.mouse.click(x, y)
            return True
        except Exception:
            return self._click_locator_center(iframe, timeout=timeout)

    def _turnstile_needs_click(self) -> bool:
        """仅当 CF iframe 内可读到交互标记时才点击。"""
        if self.page is None:
            return False
        for iframe in self._turnstile_iframe_locators():
            content = self._turnstile_frame_content(iframe)
            if content is None:
                continue
            if self._turnstile_interactive_markers(content):
                return True
        return False

    def _click_turnstile_challenge(self) -> bool:
        """点击交互 Turnstile 表面直到状态推进；仅 finishing/passed 算成功。"""
        before = self._turnstile_status()
        if before in {"passed", "finishing"}:
            return True
        if before == "absent":
            return False
        dispatched_any = False
        for iframe in self._turnstile_iframe_locators():
            content = self._turnstile_frame_content(iframe)
            if content is None:
                continue
            if not self._turnstile_interactive_markers(content):
                continue
            self._progress("human_verification", "click_attempt_iframe")
            if self._click_iframe_checkbox_region(iframe, timeout=1_200):
                dispatched_any = True
                self._wait(TURNSTILE_CLICK_EFFECT_WAIT_MS)
                if self._turnstile_status() in {"passed", "finishing"}:
                    self._progress("human_verification", "click_effect_iframe")
                    return True
        return False

    def _wait_for_turnstile(self) -> None:
        """等 token 写入；允许 widget 延迟挂载；托管型自动放行，交互型才点击。"""
        status = self._turnstile_status()
        if status == "passed":
            self._progress("human_verification", "passed")
            return
        if status == "absent":
            self._progress("human_verification", "waiting_for_widget_mount")
        else:
            self._progress("human_verification", "detected")
        if not self.headless and self.page is not None:
            try:
                self.page.bring_to_front()
            except Exception:
                pass
        deadline = min(self.deadline, time.time() + TURNSTILE_WAIT_SEC)
        mount_deadline = time.time() + TURNSTILE_MOUNT_GRACE_SEC
        try:  # 云打码预留通道：配置 captcha_provider 即启用；未配置 CaptchaNotConfigured → 原浏览器挑战
            from core.captcha_router import CaptchaNotConfigured, CaptchaSolveError, solve_turnstile

            _sk = ""
            if self.page is not None:
                _sk = str(
                    self.page.evaluate(
                        "() => document.querySelector('[data-sitekey]')?.dataset.sitekey || ''"
                    )
                    or ""
                )
            if _sk:
                _tok = solve_turnstile(str(self.page.url), _sk)
                self.page.evaluate(
                    "(t) => { const el = document.querySelector('input[name=\"cf-turnstile-response\"]');"
                    " if (el) { el.value = t; el.dispatchEvent(new Event('input', {bubbles: true})); } }",
                    _tok,
                )
                self._progress("human_verification", "passed_cloud_solver")
                return
        except CaptchaNotConfigured:
            pass  # 通道未开（默认态）→ 原有头/headless 挑战流程零改动
        except CaptchaSolveError:
            self._progress("human_verification", "cloud_solver_failed_fallback")
            # 云通道开了但求解失败 → 回落既有浏览器挑战（重试层语义不变）
        click_attempts = 0
        last_click_at = 0.0
        finishing_logged = False
        seen_widget = status != "absent"
        while time.time() < deadline:
            self._check_cancel()
            status = self._turnstile_status()
            if status == "passed":
                self._progress("human_verification", "passed")
                return
            if status == "absent":
                if not seen_widget and time.time() >= mount_deadline:
                    self._progress("human_verification", "not_required")
                    return
                self._wait(250)
                continue
            if not seen_widget:
                seen_widget = True
                self._progress("human_verification", "detected")
                self._progress("human_verification", "waiting_for_auto_pass")
            if status == "finishing":
                if not finishing_logged:
                    self._progress("human_verification", "waiting_for_response_token")
                    finishing_logged = True
                self._wait(250)
                continue
            finishing_logged = False
            can_retry = (
                click_attempts < TURNSTILE_MAX_CLICK_ATTEMPTS
                and time.time() - last_click_at >= TURNSTILE_CLICK_COOLDOWN_SEC
            )
            if can_retry and self._turnstile_needs_click():
                self._progress("human_verification", "manual_click_required")
                click_attempts += 1
                last_click_at = time.time()
                if self._click_turnstile_challenge():
                    self._progress("human_verification", "clicked_challenge")
                    if self._turnstile_status() == "passed":
                        self._progress("human_verification", "passed")
                        return
                elif not self.headless:
                    self._progress("human_verification", "waiting_for_manual_action")
            self._wait(250)
        if self.headless:
            raise XaiTurnstileRejected(
                "xAI 人机验证未自动通过（headless）；重试需新会话+退避"
            )
        raise XaiBrowserError("等待手工完成人机验证超时")

    # ---------- 注册主循环 ----------

    @staticmethod
    def _input_has_value(target: Any | None) -> bool:
        if target is None:
            return False
        try:
            return bool(str(target.input_value(timeout=500) or ""))
        except Exception:
            return False

    def _signup_form_present(self) -> bool:
        return (
            self._first_visible(
                [
                    'input[type="password"]',
                    'input[name*="password" i]',
                    'input[autocomplete="new-password"]',
                    'input[name*="first" i]',
                    'input[name*="given" i]',
                    'input[name*="last" i]',
                    'input[name*="family" i]',
                    'input[name="name"]',
                    'input[name*="fullName" i]',
                ]
            )
            is not None
        )

    def _verification_target(self) -> tuple[str, Any] | None:
        if self.page is None:
            return None
        # 资料页的隐藏密码类输入 name 可能含 "code"——密码框消失前不算验证码页
        if self._first_visible(PASSWORD_INPUT_SELECTORS) is not None:
            return None
        try:
            split = self.page.locator('input[maxlength="1"]')
            visible = [
                split.nth(i)
                for i in range(min(10, int(split.count())))
                if split.nth(i).is_visible()
            ]
            if len(visible) >= 6:
                return "split", visible
        except Exception:
            pass
        target = self._first_visible(
            [
                'input[name*="code" i]',
                'input[autocomplete="one-time-code"]',
                'input[inputmode="numeric"]',
                'input[maxlength="6"]',
            ]
        )
        return ("single", target) if target is not None else None

    def _wait_for_verification_transition(self, anchor: Any, pattern: str) -> None:
        """验证码提交后确认页面变化；仅在同一验证表单仍在时重试提交。"""
        attempts = 1
        initial_url = str(self.page.url or "") if self.page is not None else ""
        next_retry_at = time.time() + ACTION_TRANSITION_RETRY_SEC
        deadline = min(
            self.deadline,
            time.time() + ACTION_TRANSITION_RETRY_SEC * ACTION_TRANSITION_MAX_ATTEMPTS + 3.0,
        )
        while time.time() < deadline:
            self._check_cancel()
            text = self._page_text()
            if re.search(r"invalid|incorrect|expired|验证码.*(?:错误|过期)", text, re.I):
                return
            verification = self._verification_target()
            if verification is None:
                sso, _cookies = self._extract_sso()
                current_url = str(self.page.url or "") if self.page is not None else ""
                if sso or self._signup_form_present() or current_url != initial_url:
                    return
            now = time.time()
            if (
                verification is not None
                and now >= next_retry_at
                and attempts < ACTION_TRANSITION_MAX_ATTEMPTS
            ):
                attempts += 1
                self._progress("verification", "retrying_submit", attempt=attempts)
                self._submit(anchor, pattern)
                next_retry_at = time.time() + ACTION_TRANSITION_RETRY_SEC
            self._wait(250)
        raise XaiBrowserError(f"xAI 验证码提交后页面未变化，已尝试点击 {attempts} 次")

    def _wait_for_signup_transition(self, pattern: str) -> None:
        """资料提交后确认状态变化（sso cookie 或表单消失），失败重试提交。"""
        if self.page is None:
            raise XaiBrowserError("xAI 注册页面已经关闭")
        initial_url = str(self.page.url or "")
        deadline = min(self.deadline, time.time() + SIGNUP_SUBMIT_TIMEOUT_SEC)
        attempts = 1
        next_retry_at = time.time() + SIGNUP_SUBMIT_RETRY_SEC
        form_absent_polls = 0
        self._progress("completion", "waiting_for_transition", attempt=attempts)
        while time.time() < deadline:
            self._check_cancel()
            sso, _cookies = self._extract_sso()
            if sso:
                self._progress("completion", "transition_detected", evidence="sso_cookie")
                return
            if not self._signup_form_present():
                form_absent_polls += 1
                if form_absent_polls >= SIGNUP_TRANSITION_STABLE_POLLS:
                    self._progress(
                        "completion",
                        "transition_detected",
                        evidence="form_absent",
                        url_changed=str(self.page.url or "") != initial_url,
                    )
                    return
                self._wait(250)
                continue
            form_absent_polls = 0
            now = time.time()
            if now >= next_retry_at and attempts < SIGNUP_SUBMIT_MAX_ATTEMPTS:
                action = self._find_action(pattern)
                if action is not None:
                    attempts += 1
                    self._progress("completion", "retrying_submit", attempt=attempts)
                    self._click_locator_center(action, timeout=3_000)
                    next_retry_at = now + SIGNUP_SUBMIT_RETRY_SEC
            self._wait(250)
        raise XaiBrowserError(f"xAI 资料提交后页面未变化，已尝试点击 {attempts} 次")

    def _extract_sso(self) -> tuple[str | None, dict[str, str]]:
        """sso cookie 仅作为「注册落地」信号，不再作为产出凭证。"""
        if self.context is None:
            return None, {}
        try:
            rows = self.context.cookies()
        except Exception:
            rows = []
        cookies = {
            str(row.get("name") or ""): str(row.get("value") or "")
            for row in (rows or [])
            if row.get("name") and row.get("value")
        }
        return cookies.get("sso") or cookies.get("sso-rw"), cookies

    def _oauth_cookie_metadata(self) -> str:
        """紧凑、无密钥的 cookie 摘要（count + 域名），供日志用。"""
        rows: set[tuple[str, str]] = set()
        try:
            all_rows = self.context.cookies() if self.context is not None else []
        except Exception:
            all_rows = []
        for cookie in all_rows:
            name = str(cookie.get("name") or "").strip()
            domain = str(cookie.get("domain") or "").strip().lower().lstrip(".")
            if not name or not (
                domain == "x.ai"
                or domain.endswith(".x.ai")
                or domain == "grok.com"
                or domain.endswith(".grok.com")
            ):
                continue
            rows.add((name, domain or "unknown"))
        if not rows:
            return "none"
        domains = ",".join(sorted({d for _n, d in rows}))
        return f"count:{len(rows)} domains:{domains}"

    def _raise_page_error(self, email: str) -> None:
        text = self._page_text()
        lowered = text.lower()
        if any(
            marker in lowered
            for marker in (
                "attention required",
                "sorry, you have been blocked",
                "access denied",
            )
        ):
            raise XaiBrowserError("xAI/Cloudflare 拒绝了当前浏览器或代理")
        if re.search(r"email.*already|account.*already|already.*registered", text, re.I):
            raise XaiBrowserError(f"邮箱 {email} 已存在 xAI 账号")
        if re.search(r"unsupported.*email|email.*not supported", text, re.I):
            raise XaiBrowserError(f"xAI 不支持当前邮箱地址：{email}")
        if re.search(r"rate.?limit|too many requests|try again later", text, re.I):
            raise XaiBrowserError("xAI 上游限流，请稍后重试")

    def register_account(
        self,
        *,
        email: str,
        password: str,
        get_verification_code: Callable[..., str | None],
    ) -> dict[str, Any]:
        """跑完注册直至登录落地（sso cookie 出现且离开注册页）。"""
        self.open()
        assert self.page is not None
        acted: set[str] = set()
        used_codes: set[str] = set()
        last_code = ""
        first_name_value, last_name_value = secrets.choice(_FIRST_NAMES), secrets.choice(_LAST_NAMES)
        self._progress("profile", "generated")
        sso_seen_at: float | None = None
        while True:
            self._check_cancel()
            sso, _cookies = self._extract_sso()
            if sso:
                current_url = str(self.page.url or "")
                if "/sign-up" not in current_url.lower():
                    self._progress("completion", "done", url=current_url[:100])
                    return {
                        "ok": True,
                        "email": email,
                        "cookies": {},
                        "steps": list(self.steps),
                    }
                if sso_seen_at is None:
                    sso_seen_at = time.time()
                    self._progress("completion", "waiting_for_landing")
                if time.time() - sso_seen_at >= REGISTRATION_LANDING_TIMEOUT_SEC:
                    raise XaiBrowserError(
                        "xAI 已生成登录 Session，但注册页未自然跳转到登录成功页面"
                    )
                self._wait(500)
                continue
            self._raise_page_error(email)

            if "signup_method" not in acted and re.search(
                # 英文为主；保留原有中文分支；补日文兜底（双保险②，东京/日文出口时
                # geoip 若仍派生 ja-JP 也不至于永久空转）
                r"sign up with email|使用邮箱.*注册|邮箱注册"
                r"|メールで登録|メールによる登録|メールアドレスで登録",
                self._page_text(),
                re.I,
            ):
                self._progress("signup_method", "selecting_email")
                self._select_email_signup()
                acted.add("signup_method")
                self._progress("signup_method", "selected")
                self._settle_page()
                continue

            verification = self._verification_target()
            if verification is not None:
                stage = "verification"
                if stage not in acted:
                    self._progress(stage, "waiting_for_code")
                    try:
                        code = get_verification_code(False)
                    except TypeError:
                        code = get_verification_code()
                    value = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()
                    if len(value) != 6 or value in used_codes:
                        raise XaiBrowserError(f"无效或重复的 xAI 邮箱验证码：{value!r}")
                    used_codes.add(value)
                    last_code = value
                    kind, target = verification
                    self._progress(stage, "code_received", code=value)
                    self._action_delay()
                    if kind == "split":
                        for index, char in enumerate(value):
                            target[index].fill(char)
                        anchor = target[0]
                    else:
                        target.fill(value)
                        anchor = target
                    self._progress(stage, "code_filled")
                    submit_pattern = "verify|confirm|continue|submit|验证|确认|继续"
                    self._submit(anchor, submit_pattern)
                    self._progress(stage, "submitted", attempt=1)
                    self._wait_for_verification_transition(anchor, submit_pattern)
                    acted.add(stage)
                    self._settle_page()
                    continue

                text = self._page_text()
                if re.search(r"invalid|incorrect|expired|验证码.*(?:错误|过期)", text, re.I):
                    self._progress(stage, "retrying")
                    resend = self._click_action(
                        "resend|send.*again|new code|try again|重新发送|重发"
                    )
                    if not resend:
                        raise XaiBrowserError(f"xAI 验证码提交失败：{text[:240]}")
                    try:
                        code = get_verification_code(True)
                    except TypeError:
                        code = get_verification_code()
                    value = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()
                    if len(value) != 6 or value == last_code:
                        raise XaiBrowserError("xAI 未收到新的有效邮箱验证码")
                    acted.discard(stage)
                    self._settle_page()
                    continue

            email_input = self._first_visible(EMAIL_INPUT_SELECTORS)
            password_input = self._first_visible(PASSWORD_INPUT_SELECTORS)
            first_name = self._first_visible(['input[name*="first" i]', 'input[name*="given" i]'])
            last_name = self._first_visible(['input[name*="last" i]', 'input[name*="family" i]'])
            full_name = None
            if first_name is None and last_name is None:
                full_name = self._first_visible(
                    [
                        'input[name="name"]',
                        'input[name*="fullName" i]',
                        'input[name*="displayName" i]',
                        'input[autocomplete="name"]',
                    ]
                )

            stage_parts: list[str] = []
            anchor = None
            if email_input is not None:
                stage_parts.append("email")
                if "email" not in acted:
                    self._progress("email", "filling", email=email)
                    self._action_delay()
                    email_input.fill(email)
                    anchor = email_input
            if first_name is not None or last_name is not None or full_name is not None:
                stage_parts.append("profile")
                if "profile" not in acted:
                    self._progress("profile", "filling")
                    self._action_delay()
                    if first_name is not None:
                        first_name.fill(first_name_value)
                        anchor = first_name
                    if last_name is not None:
                        last_name.fill(last_name_value)
                        anchor = last_name
                    if full_name is not None:
                        full_name.fill(f"{first_name_value} {last_name_value}")
                        anchor = full_name
                    self._progress("profile", "filled")
            if password_input is not None:
                stage_parts.append("password")
                if "password" not in acted:
                    self._progress("password", "filling")
                    self._action_delay()
                    password_input.fill(password)
                    anchor = password_input
                    self._progress("password", "filled")

            pending = [part for part in stage_parts if part not in acted]
            if pending:
                if password_input is not None:
                    # React 受控输入可能因重渲染被清空——验证前后复查实际 DOM 值
                    for attempt in range(2):
                        if not self._input_has_value(password_input):
                            self._progress("password", "refilling_after_render")
                            password_input.fill(password)
                            anchor = password_input
                        self._wait_for_turnstile()
                        if self._input_has_value(password_input):
                            break
                        if attempt == 1:
                            raise XaiBrowserError("xAI 页面反复重绘并清空密码输入框")
                submit_pattern = (
                    "complete sign up|continue|next|sign up|create account|submit|"
                    "继续|下一步|创建|"
                    # 日文兜底（双保险②）
                    "登録|次へ|続行|アカウントを作成"
                )
                self._submit(anchor, submit_pattern)
                self._wait_for_signup_transition(submit_pattern)
                for part in pending:
                    acted.add(part)
                    self._progress(part, "submitted")
                self._settle_page()
                continue

            if str(self.page.url or "").startswith(XAI_GROK_URL):
                self._wait(1_000)
                continue
            self._wait(250)

    # ---------- 登录 + OAuth 授权 ----------

    @staticmethod
    def _is_grok_landing_url(url: str) -> bool:
        try:
            parsed = urlparse(str(url or ""))
        except Exception:
            return False
        return parsed.scheme in {"http", "https"} and parsed.hostname in {
            "grok.com",
            "www.grok.com",
        }

    @staticmethod
    def _is_xai_account_url(url: str) -> bool:
        try:
            parsed = urlparse(str(url or ""))
        except Exception:
            return False
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname == "accounts.x.ai"
            and parsed.path.rstrip("/") == "/account"
        )

    def _stabilize_pkce_account_session(self, *, retry: bool = False) -> None:
        """确认 accounts.x.ai/account 会话稳定后再发起 OAuth。"""
        if self.page is None:
            raise XaiBrowserError("xAI 授权浏览器未启动")
        self._progress(
            "local_oauth",
            "account_session_resyncing" if retry else "account_session_syncing",
        )
        self._wait(3_000 if retry else 1_500)
        try:
            self.page.goto(XAI_ACCOUNT_URL, wait_until="domcontentloaded", timeout=45_000)
        except Exception:
            if not self._is_xai_account_url(self.page.url):
                raise
        self._settle_page()
        stable = 0
        for _attempt in range(20):
            self._check_cancel()
            if self._is_xai_account_url(self.page.url):
                stable += 1
                if stable >= 2:
                    self._progress(
                        "local_oauth",
                        "login_complete",
                        origin="accounts.x.ai/account",
                        cookies=self._oauth_cookie_metadata(),
                    )
                    return
            else:
                stable = 0
            self._wait(500)
        raise XaiBrowserError(
            "xAI 自然登录后未能建立稳定的 accounts.x.ai 账户会话，未发起 OAuth 授权"
        )

    def login_for_pkce(self, email: str, password: str) -> None:
        """注册后建立自然浏览器登录（首次授权前置条件）。"""
        self.open()
        assert self.page is not None
        self._progress("local_oauth", "login_starting")
        self.page.goto(XAI_SIGNIN_URL, wait_until="domcontentloaded", timeout=45_000)
        self._settle_page()
        deadline = min(self.deadline, time.time() + LOGIN_TIMEOUT_SEC)
        attempts = 0
        last_submit_at = 0.0
        email_method_selected = False
        while time.time() < deadline:
            self._check_cancel()
            if last_submit_at and (
                self._is_grok_landing_url(self.page.url)
                or self._is_xai_account_url(self.page.url)
            ):
                self._stabilize_pkce_account_session()
                return
            text = self._page_text()
            self._raise_page_error(email)
            invalid_credentials = bool(
                re.search(
                    r"invalid credentials|incorrect (?:email|password)|"
                    r"email or password.*incorrect|邮箱或密码.*(?:错误|不正确)",
                    text,
                    re.I,
                )
            )
            if invalid_credentials and time.time() - last_submit_at >= 3.0:
                if attempts >= ACTION_TRANSITION_MAX_ATTEMPTS:
                    raise XaiBrowserError("xAI 浏览器登录失败：邮箱或密码未被上游接受")
                self._progress("local_oauth", "login_retrying", attempt=attempts + 1)
                self._wait(min(10_000, attempts * 3_000))
                self.page.goto(XAI_SIGNIN_URL, wait_until="domcontentloaded", timeout=45_000)
                self._settle_page()
                email_method_selected = False
                continue

            email_input = self._first_visible(EMAIL_INPUT_SELECTORS)
            password_input = self._first_visible(LOGIN_PASSWORD_SELECTORS)
            if email_input is None and password_input is None:
                if not email_method_selected and re.search(
                    r"sign in with email|log\s*in with email|login with email|"
                    r"使用邮箱.*登录|邮箱登录",
                    text,
                    re.I,
                ):
                    if self._click_action(
                        r"^sign in with email$|^log\s*in with email$|"
                        r"^login with email$|使用邮箱.*登录|邮箱登录",
                        allow_submit_fallback=False,
                    ):
                        email_method_selected = True
                        self._progress("local_oauth", "email_login_selected")
                        self._settle_page()
                        continue
                self._wait(250)
                continue

            anchor = password_input or email_input
            if self._input_is_editable(email_input):
                try:
                    email_input.fill(email)
                    self._progress("local_oauth", "login_email_filled")
                except Exception:
                    if last_submit_at and self._is_grok_landing_url(self.page.url):
                        self._stabilize_pkce_account_session()
                        return
                    raise
            if password_input is not None:
                try:
                    password_input.fill(password)
                    self._progress("local_oauth", "login_password_filled")
                    self._wait_for_turnstile()
                except Exception:
                    if last_submit_at and self._is_grok_landing_url(self.page.url):
                        self._stabilize_pkce_account_session()
                        return
                    raise
            if attempts >= ACTION_TRANSITION_MAX_ATTEMPTS:
                raise XaiBrowserError("xAI 浏览器登录提交后页面未变化，已重试 3 次")
            if last_submit_at and time.time() - last_submit_at < ACTION_TRANSITION_RETRY_SEC:
                self._wait(250)
                continue
            attempts += 1
            self._submit(
                anchor,
                r"sign in|log\s*in|login|continue|next|submit|登录|继续|下一步",
            )
            last_submit_at = time.time()
            self._progress("local_oauth", "login_submitted", attempt=attempts)
            self._settle_page()
        raise XaiBrowserError("等待 xAI 浏览器登录完成超时")

    def _input_is_editable(self, target: Any | None) -> bool:
        if target is None:
            return False
        try:
            return bool(target.is_editable(timeout=500))
        except Exception:
            return False

    @staticmethod
    def _callback_from_url(url: str, expected_state: str) -> str | None:
        try:
            parsed = urlparse(str(url or ""))
            values = parse_qs(parsed.query)
        except Exception:
            return None
        code = str((values.get("code") or [""])[0]).strip()
        state = str((values.get("state") or [""])[0]).strip()
        if not code:
            return None
        if expected_state and state and state != expected_state:
            raise XaiBrowserError("xAI OAuth 回调 state 不匹配")
        return str(url)

    def _pkce_oob_code_from_page(self, current_url: str, page_text: str, expected_state: str) -> str | None:
        """xAI 把授权码渲染在 consent 页而非重定向时，从页面元素读码。"""
        if self.page is None:
            return None
        try:
            parsed = urlparse(str(current_url or ""))
            values = parse_qs(parsed.query)
        except Exception:
            return None
        if parsed.hostname != "accounts.x.ai" or parsed.path != "/oauth2/consent":
            return None
        if not re.search(
            r"enter this code to finish signing in|copy the code below|"
            r"automatically detect a successful completion",
            str(page_text or ""),
            re.I,
        ):
            return None
        state = str((values.get("state") or [""])[0]).strip()
        # 取证实证：OOB 态 URL 是干净的 /oauth2/consent（无 state query）。
        # 此时不做 state 交叉校验——PKCE state 只在带 query 的回调 URL 上有意义。
        if expected_state and state and state != expected_state:
            raise XaiBrowserError("xAI OAuth 授权码页面 state 不匹配")
        try:
            candidates = self.page.locator("input, textarea, code, [readonly]")
            for index in range(min(20, int(candidates.count()))):
                candidate = candidates.nth(index)
                if not candidate.is_visible():
                    continue
                value = ""
                try:
                    value = str(candidate.input_value(timeout=500) or "").strip()
                except Exception:
                    try:
                        value = str(candidate.get_attribute("value") or "").strip()
                    except Exception:
                        try:
                            value = str(candidate.inner_text(timeout=500) or "").strip()
                        except Exception:
                            continue
                if re.fullmatch(r"[A-Za-z0-9._~-]{32,512}", value):
                    return value
        except Exception:
            return None
        return None

    def _consent_allow_button(self) -> Any | None:
        """consent 页专用：按按钮可见文本精确找 Allow（accessible name 与文本
        可能不一致，role+name 匹配不到时用文本扫描兜底）。"""
        if self.page is None:
            return None
        try:
            buttons = self.page.locator('button, [role="button"], a')
        except Exception:
            return None
        try:
            total = int(buttons.count())
        except Exception:
            return None
        for index in range(min(30, total)):
            candidate = buttons.nth(index)
            try:
                if not (candidate.is_visible() and candidate.is_enabled()):
                    continue
                label = str(candidate.inner_text(timeout=400) or "").strip()
            except Exception:
                continue
            low = label.lower()
            if low in ("allow", "authorize", "approve", "grant", "允许", "授权"):
                return candidate
        return None

    def authorize_pkce(self, auth_url: str, expected_state: str) -> str:
        """打开授权页、点同意、捕获回调 URL 或页面授权码。

        回调捕获三路径（Kiro 线已验证：浏览器跳 loopback 失败但请求已发出）：
        ① page.on("request") 钩子截获 127.0.0.1 回调重定向（主路径——
           Firefox 对失败的 loopback 导航不 commit page.url，光看 url 永远等不到）
        ② page.url 已带 code（导航成功场景）
        ③ consent 页 OOB 渲染码
        """
        if self.page is None:
            raise XaiBrowserError("xAI 授权浏览器未启动")
        self._progress("local_oauth", "opening_authorization")
        self._progress(
            "local_oauth",
            "authorization_context",
            origin=str(self.page.url or "")[:120],
            cookies=self._oauth_cookie_metadata(),
        )
        self._action_delay()

        # ① 请求钩子：即使导航失败也能拿到带 code 的回调 URL
        captured: dict[str, str] = {}
        from platforms.grok.xai_pkce import REDIRECT_URI
        from platforms.grok import pkce_listener
        redirect_prefix = REDIRECT_URI.split("?")[0]

        # 真实监听 127.0.0.1:56121（与浏览器同机），回调必然落到这里
        pkce_listener.start_listener()
        pkce_listener.clear_capture(expected_state)

        def _on_request(request: Any) -> None:
            try:
                url = str(request.url or "")
            except Exception:
                return
            if url.startswith(redirect_prefix) and "code=" in url:
                captured.setdefault("callback", url)

        try:
            self.page.on("request", _on_request)
            hook_ok = True
        except Exception:
            hook_ok = False
            self._progress("local_oauth", "request_hook_unavailable")

        try:
            self.page.goto(auth_url, wait_until="domcontentloaded", timeout=45_000)
        except Exception:
            # 127.0.0.1 回调无人监听会连接失败，但 URL 里可能已带上 code
            callback = self._callback_from_url(str(self.page.url or ""), expected_state)
            if callback:
                self._progress("local_oauth", "code_captured")
                return callback
        self._settle_page()
        clicked_signatures: dict[str, tuple[int, float]] = {}
        regoto_attempts = 0
        self._oauth_regoto_next = 0.0
        deadline = min(self.deadline, time.time() + OAUTH_CONSENT_TIMEOUT_SEC)
        while time.time() < deadline:
            self._check_cancel()
            # ① 钩子捕获优先
            if captured.get("callback"):
                try:
                    callback = self._callback_from_url(captured["callback"], expected_state)
                except XaiBrowserError:
                    raise
                if callback:
                    self._progress("local_oauth", "code_captured", via="request_hook" if hook_ok else "url")
                    return callback
            # ② 本地监听器捕获（重定向真实落到 127.0.0.1:56121）
            hit = pkce_listener.get_capture(expected_state)
            if hit:
                callback = self._callback_from_url(hit, expected_state)
                if callback:
                    self._progress("local_oauth", "code_captured", via="listener")
                    return callback
            current_url = str(self.page.url or "")
            callback = self._callback_from_url(current_url, expected_state)
            if callback:
                self._progress("local_oauth", "code_captured", via="url")
                return callback
            text = self._page_text()
            page_code = self._pkce_oob_code_from_page(current_url, text, expected_state)
            if page_code:
                self._progress("local_oauth", "oob_code_captured")
                return page_code
            if re.search(
                r"access denied|authorization.*failed|request.*denied|授权.*失败|拒绝授权",
                text,
                re.I,
            ):
                raise XaiOAuthAccessDenied(f"xAI OAuth 授权失败：{text[:240]}")
            action = self._find_action(
                # consent 页专属：只认 Allow/Authorize（Deny/accept 泛词绝不进
                # pattern——会误点 OneTrust "Accept All Cookies"，取证根因 C）
                r"^allow$|^authorize$|^approve$|^grant$|允许|授权",
                exclude_pattern=r"cookie|banner|consent\s?manage|拒绝|deny",
                allow_submit_fallback=False,
            )
            if action is None and "/oauth2/consent" in current_url:
                # role+name 匹配不到时用可见文本兜底（accessible name 可能与文本不同）
                action = self._consent_allow_button()
            if action is None and "/oauth2/consent" not in current_url:
                action = self._find_action(
                    r"allow|authorize|approve|accept|confirm|continue|grant|允许|授权|同意|确认|继续",
                    exclude_pattern=r"cookie|banner",
                )
            if action is None:
                # 现场诊断：每 20s 最多 dump 一次按钮清单，防日志刷屏
                now = time.time()
                if now - getattr(self, "_oauth_dump_ts", 0.0) >= 20.0:
                    self._oauth_dump_ts = now
                    labels: list[str] = []
                    try:
                        btns = self.page.locator('button, [role="button"], a')
                        for i in range(min(15, int(btns.count()))):
                            b = btns.nth(i)
                            try:
                                if b.is_visible():
                                    labels.append((str(b.inner_text(timeout=300) or "").strip())[:24])
                            except Exception:
                                pass
                    except Exception:
                        pass
                    self._progress(
                        "local_oauth",
                        "no_action_found",
                        url=current_url[:120],
                        buttons=str(labels)[:300],
                    )
                # authorize 首跳被弹到 /account → grok.com 的跳变抢回：
                # consent 从未出现时用同一 auth_url 原样 re-goto（PKCE 不变、端点幂等）。
                regoto_deadline = getattr(self, "_oauth_regoto_next", 0.0)
                if (
                    regoto_attempts < OAUTH_REGOTO_MAX_ATTEMPTS
                    and now >= regoto_deadline
                    and self._is_xai_account_url(current_url)
                ) or (
                    regoto_attempts < OAUTH_REGOTO_MAX_ATTEMPTS
                    and now >= regoto_deadline
                    and self._is_grok_landing_url(current_url)
                ):
                    regoto_attempts += 1
                    self._oauth_regoto_next = time.time() + OAUTH_REGOTO_INTERVAL_SEC
                    self._progress(
                        "local_oauth",
                        "authorization_bounced",
                        to=current_url[:80],
                        regoto=regoto_attempts,
                    )
                    try:
                        self.page.goto(auth_url, wait_until="domcontentloaded", timeout=45_000)
                    except Exception:
                        callback = self._callback_from_url(
                            str(self.page.url or ""), expected_state
                        )
                        if callback:
                            self._progress("local_oauth", "code_captured", via="regoto")
                            return callback
                    self._settle_page()
                    continue
            if action is not None:
                signature = f"{current_url}|{text[:160]}"
                attempts, last_clicked_at = clicked_signatures.get(signature, (0, 0.0))
                if (
                    attempts < ACTION_TRANSITION_MAX_ATTEMPTS
                    and (
                        attempts == 0
                        or time.time() - last_clicked_at >= ACTION_TRANSITION_RETRY_SEC
                    )
                ):
                    self._action_delay()
                    attempts += 1
                    clicked = self._click_locator_center(action, timeout=3_000)
                    clicked_signatures[signature] = (attempts, time.time())
                    self._progress(
                        "local_oauth",
                        "approval_submitted" if clicked else "approval_click_failed",
                        attempt=attempts,
                    )
                    if clicked:
                        self._settle_page()
                    else:
                        self._wait(250)
                    continue
                if (
                    attempts >= ACTION_TRANSITION_MAX_ATTEMPTS
                    and time.time() - last_clicked_at >= ACTION_TRANSITION_RETRY_SEC
                ):
                    raise XaiBrowserError("xAI OAuth 授权点击后页面未变化，已动态重试 3 次")
            self._wait(500)
        raise XaiBrowserError("等待 xAI OAuth 回调或授权码超时")


def register_xai_account(
    *,
    email: str,
    password: str,
    get_verification_code: Callable[..., str | None],
    proxy: str = "",
    headless: bool = True,
    timeout_sec: float = 420.0,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
    operation_delay_ms: int = 0,
) -> tuple[dict[str, Any], XaiBrowserSession]:
    """注册 xAI 账号并保留浏览器会话（供后续 OAuth 授权复用登录态）。"""
    session = XaiBrowserSession(
        proxy=proxy,
        headless=headless,
        on_progress=on_progress,
        should_cancel=should_cancel,
        operation_delay_ms=operation_delay_ms,
        timeout_sec=timeout_sec,
    )
    try:
        return (
            session.register_account(
                email=email,
                password=password,
                get_verification_code=get_verification_code,
            ),
            session,
        )
    except Exception:
        session.hold_failure()
        session.close()
        raise
