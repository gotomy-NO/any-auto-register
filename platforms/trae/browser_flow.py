"""Trae.ai 浏览器注册线（lxf746 browser_register.py 移植 + 我方编排约定对齐）。

定位（docs/trae参数捕获-20261004.md §三）：
    protocol 执行器        → 旧协议线 platforms/trae/core.py（本模块不碰；纯协议线已判
                             NO-GO：缺 X-Bogus/X-Gnarly 动态签名 + ttwid/msToken cookie 面）
    headless/headed 执行器 → 本模块：Camoufox 全浏览器注册，签名头由浏览器自动生成
                             （lxf746 原注释：浏览器模式自动生成这些头，无需额外处理）

链路（蓝本 any-auto-register-lxf746 platforms/trae/browser_register.py，402 行实测可用）：
    1. Camoufox 打开 www.trae.ai/sign-up
    2. region 域观察：page.on("response") 捕获 /passport/web/region/ 下发 data.domain
       （现行 ug-normal.us.trae.ai 类；仅记录进 extra 供台账/漂移监控，fetch 仍走页面 session）
    3. 填邮箱 → 点 Send Code（locator 优先，JS leaf 点击兜底且返回真值，不再盲报成功）
    4. 等 OTP 输入框 → otp_callback 收码（我方 BaseMailbox.wait_for_code，装配在 plugin）→ 回填
    5. 填密码 → 点 Sign Up → 等待离开 sign-up 页（超时仅告警，token 仍可试取）
    6. page.evaluate fetch 链：trae/Login → GetUserToken(Cloud-IDE JWT) →
       CheckLogin(Region/UserId/AIPayHost)；兜底 cookie user_id → JWT payload data.id；
       token 取不到 = 注册判失败（不假成功，同 core.py 判定口径）
    7. rc 验证码分支：URL 命中 captcha 或出现可见验证码组件 → 抛 TraeCaptchaRequired（不吞，
       由上层弃号换邮箱重试；rc 何时强制介入未知，见 docs 风险节）

线程隔离：整个浏览器流程固定放一次性新线程执行，用完即弃（Playwright sync 绿色体
线程污染，蓝本 platforms/grok/core.py register——批量线程复用第 2/3 号必炸的实锤教训）。
token 永不完整落日志（_mask 只留前缀 + 长度）。
离线可测：Camoufox/Playwright/网络全部延迟到函数内 import，page 通过 page_factory 注入
即可用假状态机桩测（tests/test_trae_browser_flow.py）。
"""

from __future__ import annotations

import base64
import json as _json
import random
import string
import threading
import time
from typing import Any, Callable, Optional

from core.browser_runtime import ensure_browser_display_available, resolve_browser_headless
from platforms.trae.core import API_SG, BASE_URL

TRAE_URL = "https://www.trae.ai"
SIGNUP_URL = f"{TRAE_URL}/sign-up"
REGION_PATH_MARK = "/passport/web/region/"

# evaluate fetch 链 body（同 platforms/trae/core.py step4/step6，Cookie 面由浏览器 session 提供）
_LOGIN_BODY = (
    '{"UtmSource":"","UtmMedium":"","UtmCampaign":"","UtmTerm":"","UtmContent":"",'
    '"BDVID":"","LoginChannel":"ide_platform"}'
)
_CHECKLOGIN_BODY = '{"GetAIPayHost":true,"GetNickNameEditStatus":true}'

# ── 关键选择器（蓝本 lxf746 多候选兜底；文本按钮统一 .last，对齐 lxf746 的 .last 语义）──
SEL_EMAIL = [
    'input[placeholder="Email"]',
    'input[type="email"]',
    'input[name="email"]',
    'input[placeholder*="email" i]',
]
SEL_OTP = [
    'input[placeholder="Verification code"]',
    'input[placeholder*="verification" i]',
    'input[placeholder*="code" i]',
    'input[name="code"]',
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"]',
]
SEL_PASSWORD = [
    'input[placeholder="Password"]',
    'input[type="password"]',
    'input[name="password"]',
]
SEL_SEND_CODE = [
    'button:has-text("Send Code")',
    'text="Send Code"',
]
SEL_SIGN_UP = [
    'button:has-text("Sign Up")',
    'text="Sign Up"',
    'text="Sign up"',
]
# 可见验证码组件特征（URL/文本粗匹配会误伤 goofy-captcha 预加载 script，只认可见元素）
SEL_CAPTCHA = [
    'iframe[src*="captcha"]',
    'iframe[src*="verification"]',
    '[class*="captcha" i]',
    '[id*="captcha" i]',
]


class TraeBrowserFlowError(RuntimeError):
    """浏览器注册流程失败。"""


class TraeCaptchaRequired(TraeBrowserFlowError):
    """命中 rc/captcha 验证码——本链路不接验证码，弃号换邮箱重试（异常不吞）。"""


def _mask(value: str, keep: int = 4) -> str:
    """token/密码日志脱敏：只露前 4 位与长度。"""
    value = str(value or "")
    return f"{value[:keep]}...<len={len(value)}>" if value else ""


def _rand_password(n: int = 14) -> str:
    """大小写+数字保底各一，避免纯随机漏字符类。"""
    pw = [
        random.choice(string.ascii_uppercase),
        random.choice(string.ascii_lowercase),
        random.choice(string.digits),
    ]
    pw += random.choices(string.ascii_letters + string.digits, k=max(0, n - 4))
    random.shuffle(pw)
    return "".join(pw) + "!"


def _decode_jwt_user_id(token: str) -> str:
    """终极兜底：从 Cloud-IDE JWT payload 解出 data.id（纯离线）。"""
    try:
        payload = str(token).split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = _json.loads(base64.urlsafe_b64decode(payload))
        return str((data.get("data") or {}).get("id") or "")
    except Exception:
        return ""


def _fetch_json_js(url: str, body: str, extra_headers: str = "") -> str:
    """构造 page.evaluate 用的 fetch JSON JS（蓝本 lxf746 _get_trae_cloudide_token）。"""
    headers = '"content-type": "application/json"'
    if extra_headers:
        headers += ", " + extra_headers
    return (
        "async () => {"
        'const r = await fetch("' + url + '", {'
        'method: "POST", headers: {' + headers + '}, credentials: "include",'
        "body: " + body + "});"
        "return await r.json();"
        "}"
    )


def _click_text_js(*texts: str) -> str:
    """JS 遍历 DOM 点击包含精确文本的最小 leaf 元素；命中返回 true（lxf746 兜底改进版）。"""
    conds = " || ".join("t === " + _json.dumps(t) for t in texts)
    return (
        "() => {"
        "const all = document.querySelectorAll('*');"
        "for (const el of all) {"
        "if (el.children.length === 0) {"
        "const t = el.textContent.trim();"
        "if (" + conds + ") { el.click(); return true; }"
        "}"
        "}"
        "return false;"
        "}"
    )


_JS_SEND_CODE_CLICK = _click_text_js("Send Code")
_JS_SIGN_UP_CLICK = _click_text_js("Sign Up", "Sign up")


# ── 元素操作小助手（duck-typed page：真实 Playwright Page 与桩共用同一套）──────────

def _first_visible(page: Any, selectors: list, which: str = "first") -> Optional[Any]:
    for sel in selectors:
        try:
            loc = getattr(page.locator(sel), which)
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def _fill(page: Any, selectors: list, value: Any, log: Callable[[str], None], desc: str = "输入框") -> bool:
    loc = _first_visible(page, selectors)
    if loc is None:
        return False
    try:
        loc.click()
        loc.fill(str(value))
        shown = _mask(value) if desc == "密码" else str(value)[:24]
        log(f"已填写{desc}: {shown}")
        return True
    except Exception as exc:
        log(f"填写{desc}失败: {exc}")
        return False


def _click(page: Any, selectors: list, log: Callable[[str], None], desc: str = "按钮",
           which: str = "first") -> bool:
    loc = _first_visible(page, selectors, which)
    if loc is None:
        log(f"未找到{desc}（尝试 {len(selectors)} 个选择器）")
        return False
    try:
        loc.click()
        log(f"已点击{desc}")
        return True
    except Exception as exc:
        log(f"点击{desc}失败: {exc}")
        return False


def _capture_cloudide_token(page: Any, log: Callable[[str], None]) -> dict:
    """注册完成后用浏览器 session 走 fetch 链取 Cloud-IDE JWT（蓝本 lxf746 同名函数）。

    step4: POST /cloudide/api/v3/trae/Login           （建立 IDE session）
    step5: POST /cloudide/api/v3/common/GetUserToken  →  Result.Token = Cloud-IDE JWT
    step6: POST /cloudide/api/v3/trae/CheckLogin      →  UserId / Region / AIPayHost
    兜底：cookie user_id → JWT payload data.id。
    """
    token = ""
    user_id = ""
    region = ""
    ai_pay_host = ""

    # step4: Trae Login（建立 IDE session）
    try:
        log("调用 Trae Login API...")
        page.evaluate(_fetch_json_js(f"{BASE_URL}/cloudide/api/v3/trae/Login?type=email", _LOGIN_BODY))
        page.wait_for_timeout(1000)
    except Exception as e:
        log(f"⚠️ Trae Login 失败: {e}")

    # step5: GetUserToken → Cloud-IDE JWT
    try:
        log("获取 Cloud-IDE JWT token...")
        result = page.evaluate(_fetch_json_js(f"{API_SG}/cloudide/api/v3/common/GetUserToken", "{}"))
        token = str(((result or {}).get("Result") or {}).get("Token") or "")
        if token:
            log(f"✅ 获取到 Cloud-IDE JWT {_mask(token)}")
    except Exception as e:
        log(f"⚠️ GetUserToken 失败: {e}")

    # step6: CheckLogin → userId / Region / AIPayHost
    if token:
        try:
            result2 = page.evaluate(_fetch_json_js(
                f"{BASE_URL}/cloudide/api/v3/trae/CheckLogin",
                _CHECKLOGIN_BODY,
                extra_headers=f'"Authorization": "Cloud-IDE-JWT {token}"',
            ))
            res = (result2 or {}).get("Result") or {}
            user_id = str(res.get("UserId") or res.get("userId") or "")
            region = str(res.get("Region") or "")
            ai_pay_host = str(res.get("AIPayHost") or "")
        except Exception as e:
            log(f"⚠️ CheckLogin 失败: {e}")

    # 兜底：从 Cookie 提取 user_id
    if not user_id:
        try:
            cookies = {c["name"]: c["value"] for c in page.context.cookies()}
            user_id = str(cookies.get("user_id") or cookies.get("userId") or "")
        except Exception:
            pass

    # 终极兜底：从 JWT payload 解出 id
    if not user_id and token:
        user_id = _decode_jwt_user_id(token)

    return {"token": token, "user_id": user_id, "region": region, "ai_pay_host": ai_pay_host}


class _FlowCtx:
    """状态机单次运行上下文。"""

    __slots__ = ("email", "password", "otp")

    def __init__(self, email: str, password: str, otp: Callable[[], str]):
        self.email = email
        self.password = password
        self.otp = otp


class TraeBrowserFlow:
    """Trae.ai 浏览器注册编排器（lxf746 TraeBrowserRegister 适配我方接口）。"""

    def __init__(
        self,
        *,
        proxy: Optional[str] = None,
        log_fn: Callable[[str], None] = print,
        headless: bool = True,
        email_timeout_sec: float = 30.0,
        send_timeout_sec: float = 15.0,
        otp_timeout_sec: float = 60.0,
        done_timeout_sec: float = 45.0,
        poll_interval_ms: int = 500,
        page_factory: Optional[Callable[..., Any]] = None,
    ):
        self.proxy = proxy or ""
        self.headless = headless
        self.email_timeout_sec = float(email_timeout_sec)
        self.send_timeout_sec = float(send_timeout_sec)
        self.otp_timeout_sec = float(otp_timeout_sec)
        self.done_timeout_sec = float(done_timeout_sec)
        self.poll_interval_ms = int(poll_interval_ms)
        self.page_factory = page_factory
        # GBK 安全包装：Windows 控制台打印非 GBK 字符会抛 UnicodeEncodeError
        def _safe_log(text: Any) -> str:
            raw = str(text)
            try:
                return raw.encode("gbk", errors="replace").decode("gbk")
            except Exception:
                return raw.encode("ascii", errors="replace").decode("ascii")
        self.log = lambda s: log_fn(_safe_log(s))

    # ---------- 外壳（一次性线程隔离，蓝本 grok core.register）----------

    def register(
        self,
        email: str,
        password: Optional[str] = None,
        otp_callback: Optional[Callable[[], str]] = None,
    ) -> dict:
        """浏览器注册 Trae 账号并取回 Cloud-IDE JWT。

        批量任务线程会被复用，Playwright sync API 的绿色体状态在首个 Camoufox 会话
        close() 后会污染该线程（grok 10-03 批量实测）。因此整个浏览器流程固定放进
        一次性新线程执行，用完即弃。异常原样上抛（不吞），由上层决定重试策略。
        """
        box: dict[str, Any] = {}

        def _target() -> None:
            try:
                box["result"] = self._register_flow(
                    email=email, password=password, otp_callback=otp_callback
                )
            except BaseException as exc:  # noqa: BLE001 — 原样搬运给调用线程
                box["error"] = exc

        th = threading.Thread(
            target=_target, daemon=True, name=f"trae-reg-{int(time.time())}"
        )
        th.start()
        th.join()
        if "error" in box:
            raise box["error"]
        return box["result"]

    def _register_flow(
        self,
        email: str,
        password: Optional[str] = None,
        otp_callback: Optional[Callable[[], str]] = None,
    ) -> dict:
        if not otp_callback:
            raise TraeBrowserFlowError("Trae 注册需要邮箱验证码但未提供 otp_callback")
        if not str(email or "").strip():
            raise TraeBrowserFlowError("Trae 注册未取得邮箱地址")

        password = password or _rand_password()
        headless, reason = resolve_browser_headless(self.headless, default_headless=True)
        ensure_browser_display_available(headless)
        self.log(f"浏览器模式: {'headless' if headless else 'headed'} ({reason})")

        ctx = _FlowCtx(
            email=str(email or "").strip(),
            password=password,
            otp=self._make_code_getter(otp_callback),
        )

        if self.page_factory is not None:
            # 测试注入：假 page 状态机，验证纯编排逻辑
            page = self.page_factory(headless=headless, proxy=self.proxy)
            try:
                return self._run_flow(page, ctx)
            finally:
                closer = getattr(page, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:
                        pass

        page, close_browser = self._launch_page(headless)
        try:
            return self._run_flow(page, ctx)
        finally:
            close_browser()

    @staticmethod
    def _make_code_getter(otp_callback: Optional[Callable[[], str]]) -> Callable[[], str]:
        """统一验证码回调签名：兼容 get_code(force: bool) 与 get_code()。"""

        def _get_code(*_args) -> str:
            return str(otp_callback() or "").strip()

        return _get_code

    # ---------- 主状态机 ----------

    def _run_flow(self, page: Any, ctx: _FlowCtx) -> dict:
        region: dict[str, str] = {}
        self._install_region_observer(page, region)

        self.log(f"Step1: 打开 Trae 注册页 {SIGNUP_URL} ...")
        page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=30_000)

        self.log("Step2: 填邮箱 → Send Code（X-Bogus/X-Gnarly 由浏览器自动生成）...")
        self._fill_email_and_send(page, ctx)

        self.log("Step3: 等待 OTP 输入框并回填验证码...")
        self._fill_otp(page, ctx)

        self.log("Step4: 填密码 → Sign Up ...")
        self._fill_password_and_signup(page, ctx)

        self.log("Step5: 等待注册完成跳转...")
        self._wait_signup_done(page)

        self.log("Step6: 提取 Cloud-IDE JWT ...")
        out = self._capture_with_retry(page)
        out.update({
            "email": ctx.email,
            "password": ctx.password,
            "cashier_url": "",
            "auth_path": "browser",
            "passport_domain": region.get("domain", ""),
            "country_code": region.get("country_code", ""),
        })
        self.log(f"✓ 注册成功: {ctx.email} region={out['region'] or '-'} "
                 f"domain={out['passport_domain'] or '-'}")
        return out

    def _install_region_observer(self, page: Any, region: dict) -> None:
        """region 域观察：捕获 /passport/web/region/ 下发 data.domain（漂移监控用）。

        兼 send_code 响应观察（10-04 trae 首测诊断：点击成功≠上游受理，必须看
        /passport/.../send_code 的 status+error_code 才能区分'上游拒发'与'邮箱未收到'）。
        """
        def _on_response(response: Any) -> None:
            try:
                url = str(getattr(response, "url", "") or "")
                if "send_code" in url:
                    try:
                        bd = response.json() or {}
                    except Exception:
                        bd = {}
                    ec = bd.get("error_code") if isinstance(bd, dict) else None
                    msg = str((bd.get("message") or (bd.get("data") or {}).get("description") if isinstance(bd, dict) else "") or "")[:60]
                    ticket = bool(isinstance(bd, dict) and (bd.get("data") or {}).get("email_ticket")) if isinstance(bd, dict) else False
                    self.log(f"send_code 响应: HTTP {getattr(response, 'status', '?')} error_code={ec} ticket={'有' if ticket else '无'} msg={msg!r}")
                    return
                if REGION_PATH_MARK not in url:
                    return
                data = response.json() or {}
                d = (data.get("data") or {}) if isinstance(data, dict) else {}
                if d.get("domain"):
                    region["domain"] = str(d["domain"])
                if d.get("country_code"):
                    region["country_code"] = str(d["country_code"])
                self.log(f"region 下发域: {region.get('domain') or '-'} "
                         f"(country={region.get('country_code') or '-'})")
            except Exception:
                pass

        try:
            page.on("response", _on_response)
        except Exception:
            self.log("region 观察器安装失败（不影响注册主流程）")

    def _raise_if_captcha(self, page: Any) -> None:
        url = str(getattr(page, "url", "") or "").lower()
        if "captcha" in url:
            raise TraeCaptchaRequired(
                f"命中 rc/captcha 验证页: {url[:120]} —— 本链路不接验证码，弃号换邮箱重试"
            )
        for sel in SEL_CAPTCHA:
            try:
                loc = getattr(page.locator(sel), "first")
                if loc.count() > 0 and loc.is_visible():
                    raise TraeCaptchaRequired(
                        f"检测到可见验证码组件（{sel}）—— 本链路不接验证码，弃号换邮箱重试"
                    )
            except TraeCaptchaRequired:
                raise
            except Exception:
                continue

    def _fill_email_and_send(self, page: Any, ctx: _FlowCtx) -> None:
        deadline = time.monotonic() + self.email_timeout_sec
        while time.monotonic() < deadline:
            self._raise_if_captcha(page)
            if _fill(page, SEL_EMAIL, ctx.email, self.log, "邮箱"):
                if self._click_send_code(page):
                    return
                raise TraeBrowserFlowError(
                    f"未能点击 Send Code（url={str(getattr(page, 'url', ''))[:120]}）"
                )
            page.wait_for_timeout(self.poll_interval_ms)
        raise TraeBrowserFlowError(
            f"未找到邮箱输入框: {str(getattr(page, 'url', ''))[:120]}"
        )

    def _click_send_code(self, page: Any) -> bool:
        deadline = time.monotonic() + self.send_timeout_sec
        while time.monotonic() < deadline:
            if _click(page, SEL_SEND_CODE, self.log, "Send Code", which="last"):
                return True
            try:
                if page.evaluate(_JS_SEND_CODE_CLICK):
                    self.log("已点击 Send Code (JS)")
                    return True
            except Exception:
                pass
            page.wait_for_timeout(self.poll_interval_ms)
        return False

    def _fill_otp(self, page: Any, ctx: _FlowCtx) -> None:
        deadline = time.monotonic() + self.otp_timeout_sec
        while time.monotonic() < deadline:
            self._raise_if_captcha(page)
            if _first_visible(page, SEL_OTP) is not None:
                code = str(ctx.otp() or "").strip()
                if not code:
                    raise TraeBrowserFlowError("未获取到邮箱验证码（otp_callback 返回空）")
                self.log(f"填入验证码: {code}")
                if _fill(page, SEL_OTP, code, self.log, "验证码"):
                    return
            page.wait_for_timeout(self.poll_interval_ms)
        raise TraeBrowserFlowError(
            f"未出现验证码输入框: {str(getattr(page, 'url', ''))[:120]}"
        )

    def _fill_password_and_signup(self, page: Any, ctx: _FlowCtx) -> None:
        if not _fill(page, SEL_PASSWORD, ctx.password, self.log, "密码"):
            raise TraeBrowserFlowError(
                f"未找到密码输入框: {str(getattr(page, 'url', ''))[:120]}"
            )
        deadline = time.monotonic() + self.send_timeout_sec
        while time.monotonic() < deadline:
            self._raise_if_captcha(page)
            if _click(page, SEL_SIGN_UP, self.log, "Sign Up", which="last"):
                return
            try:
                if page.evaluate(_JS_SIGN_UP_CLICK):
                    self.log("已点击 Sign Up (JS)")
                    return
            except Exception:
                pass
            page.wait_for_timeout(self.poll_interval_ms)
        raise TraeBrowserFlowError("未能点击 Sign Up")

    def _wait_signup_done(self, page: Any) -> None:
        deadline = time.monotonic() + self.done_timeout_sec
        url = ""
        while time.monotonic() < deadline:
            url = str(getattr(page, "url", "") or "")
            if "sign-up" not in url and "trae.ai" in url:
                return
            self._raise_if_captcha(page)
            page.wait_for_timeout(self.poll_interval_ms)
        # 蓝本行为：超时仅告警继续（token 提取仍可尝试）
        self.log(f"⚠️ 等待跳转超时（仍停留在 {url[:100]}），继续尝试提取 token")

    def _capture_with_retry(self, page: Any) -> dict:
        out = _capture_cloudide_token(page, self.log)
        if not out.get("token"):
            self.log("⚠️ 未获取到 token，等待后重试一次...")
            page.wait_for_timeout(5000)
            out = _capture_cloudide_token(page, self.log)
        if not out.get("token"):
            raise TraeBrowserFlowError(
                "GetUserToken 未返回有效 token —— 账号认证失败（浏览器 session 可能被风控拒绝）"
            )
        return out

    # ---------- 真实浏览器启动（Camoufox 优先，Chromium 回退；蓝本 chatgpt browser_flow）──

    def _launch_page(self, headless: bool):
        from playwright.sync_api import sync_playwright

        from core.browser_runtime import build_camoufox_launch_options
        from core.proxy_utils import build_playwright_proxy_config

        ensure_browser_display_available(headless)
        proxy_config = build_playwright_proxy_config(self.proxy)
        pw = sync_playwright().start()
        browser = None
        try:
            camoufox_opts = build_camoufox_launch_options(headless, proxy_config, log=self.log)
            if camoufox_opts:
                self.log("浏览器内核: Camoufox (Firefox 反指纹)")
                browser = pw.firefox.launch(**camoufox_opts)
            if browser is None:
                self.log("浏览器内核: Playwright Chromium（Camoufox 不可用）")
                launch_args: dict[str, Any] = {
                    "headless": headless,
                    "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                }
                if proxy_config:
                    launch_args["proxy"] = proxy_config
                browser = pw.chromium.launch(**launch_args)
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                ignore_https_errors=True,
                # region 实测 country_code=us：locale/时区与出口一致，避免指纹矛盾
                locale="en-US",
                timezone_id="America/New_York",
            )
            page = context.new_page()

            def _close() -> None:
                for closer in (getattr(browser, "close", None), getattr(pw, "stop", None)):
                    if closer is None:
                        continue
                    try:
                        closer()
                    except Exception:
                        pass

            return page, _close
        except BaseException:
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            try:
                pw.stop()
            except Exception:
                pass
            raise


__all__ = [
    "TraeBrowserFlow",
    "TraeBrowserFlowError",
    "TraeCaptchaRequired",
    "SIGNUP_URL",
    "SEL_EMAIL",
    "SEL_OTP",
    "SEL_PASSWORD",
    "SEL_SEND_CODE",
    "SEL_SIGN_UP",
    "SEL_CAPTCHA",
]
