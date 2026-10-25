"""ZCode (zcode.z.ai / chat.z.ai) 注册流程核心模块

流程（无人值守模式——阿里云滑块本地自动求解，人工兜底可关）：
1. 打开 chat.z.ai OAuth 注册入口（Continue with Email → Sign up 切换注册表单）
2. 填名称/邮箱/密码 → 点 "Click to start verification" 调出阿里云滑块
3. 本地自动求解（aliyun_slider：NCC 缺口检测 + 拟人闭环拖拽），失败且允许时
   退回人工协作滑动
4. 邮箱确认：内部邮箱机制收信（verify_url_callback）→ 打开 Verify Email 链接
   → Complete Registration 填密码提交
5. 登录 → 捕获 chat.z.ai 会话 token

验收纪律（假阳性改严）：
- 滑块「弹窗关闭」不算通过，必须过 slider_passed_strict 严格 UI 信号；
- 捕获 token 必须「非 guest 且 email 与注册邮箱一致」，否则视为登录未生效。
"""
import base64
import json
import random
import string
import time
from urllib.parse import quote

from platforms.zcode.aliyun_slider import slider_passed_strict, solve_slider

# Z.ai 邮箱确认链接（从邮件正文提取的正则——wait_for_code code_pattern 用）
VERIFY_URL_PATTERN = r"https?://chat\.z\.ai/auth/verify_email\?[^\"'\s<>)]+"

OAUTH_URL = (
    "https://chat.z.ai/auth?response_type=code&client_id=client_P8X5CMWmlaRO9gyO-KSqtg"
    "&redirect_uri=" + quote("https://zcode.z.ai/login", safe="")
)
DEFAULT_PROXY = "http://127.0.0.1:7897"


def _rand_name() -> str:
    """随机英文名 5-10 位（用户规则）"""
    return "".join(random.choice(string.ascii_letters) for _ in range(random.randint(5, 10)))


def _rand_password() -> str:
    """随机密码 8-14 位（英文大小写 + 数字 + 特殊符号）"""
    specials = "!@#$%"
    pwd = ["A", "a", random.choice(specials)]
    chars = string.ascii_letters + string.digits + specials
    pwd += [random.choice(chars) for _ in range(random.randint(5, 11))]
    random.shuffle(pwd)
    return "".join(pwd)


def _token_email(token: str) -> str:
    """解码 JWT payload 取 email（用于区分正式账号与 guest）"""
    try:
        part = str(token).split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        return str(payload.get("email") or "")
    except Exception:
        return ""


class ZcodeRegister:
    def __init__(self, proxy: str = None, log_fn=print, manual_fallback: bool = None):
        # 代理仅在显式配置时使用（config_store 的 zcode_proxy）；默认直连。
        # 原先硬编码 DEFAULT_PROXY=127.0.0.1:7897，clash 未运行时浏览器
        # 全部导航报 NS_ERROR_PROXY_CONNECTION_REFUSED，注册必然失败。
        self.proxy = (proxy or "").strip() or None
        # GBK 安全包装：页面文本可能含 © 等非 GBK 字符，Windows 控制台 print 会抛
        # UnicodeEncodeError（曾导致注册成功但反馈失败、账号未入库）
        def _safe_log(text):
            try:
                return str(text).encode("gbk", errors="replace").decode("gbk")
            except Exception:
                return str(text).encode("ascii", errors="replace").decode("ascii")

        self.log = lambda s: log_fn(_safe_log(s))
        # 滑块求解：本地自动优先；manual_fallback=None 时读 config_store 的
        # zcode_slider_manual_fallback（默认 false=全自动无人值守）
        if manual_fallback is None:
            try:
                from core.config_store import config_store

                manual_fallback = str(
                    config_store.get("zcode_slider_manual_fallback", "false")
                ).lower() in ("1", "true", "yes")
            except Exception:
                manual_fallback = False
        self.manual_fallback = manual_fallback
        # 自动求解下 headless 也可用；兜底人工时必须可见窗口
        self.headless = not manual_fallback

    # ---------------- 注册主流程 ----------------
    def register(
        self,
        email: str,
        password: str = None,
        verify_url_callback=None,
    ) -> dict:
        """外壳：整个浏览器流程放进一次性新线程执行（用完即弃）。

        api/tasks.py 用 ThreadPoolExecutor 串行复用 worker 线程；Playwright sync
        的绿色体状态在首个浏览器会话 close() 后会污染该线程（grok 批量实测
        'Sync API inside the asyncio loop'，第 2/3 号必炸）。异常经 box 原样上抛。
        """
        import threading

        box: dict = {}

        def _target():
            try:
                box["result"] = self._register_flow(email=email, password=password, verify_url_callback=verify_url_callback)
            except BaseException as exc:  # noqa: BLE001 — 原样搬运给调用线程
                box["error"] = exc

        th = threading.Thread(target=_target, daemon=True, name=f"zcode-reg-{int(time.time())}")
        th.start()
        th.join()
        if "error" in box:
            raise box["error"]
        return box["result"]

    def _register_flow(
        self,
        email: str,
        password: str = None,
        verify_url_callback=None,
    ) -> dict:
        from camoufox.addons import DefaultAddons
        from camoufox.sync_api import Camoufox

        password = password or _rand_password()
        name = _rand_name()
        self.log(f"[zcode] 开始注册 {email} | 名称={name} | 滑块模式={'人工兜底' if self.manual_fallback else '全自动'}")

        with Camoufox(
            headless=self.headless,
            **({"proxy": {"server": self.proxy}} if self.proxy else {}),
            exclude_addons=[DefaultAddons.UBO],
        ) as browser:
            page = browser.new_page()
            page.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            self.log(f"[zcode] OAuth 页: {page.url[:100]}")

            # 1) 邮箱入口
            self._click_email_entry(page)
            # 2) 切换到注册表单（Sign up）
            self._goto_signup(page)
            # 3) 填注册表单
            self._fill_form(page, name, email, password)
            # 4) 协作验证 → 点 Create account
            self._collab_verify(page, submit_texts=["Create account", "创建账号"])
            self.log("[zcode] 注册已提交——等待邮箱确认（Verify Email 链接）...")

            # 5) 邮箱确认（内部邮箱机制收信）
            if verify_url_callback is None:
                raise RuntimeError(
                    "zcode 注册需要 verify_url_callback（内部邮箱机制收 Verify Email 链接）"
                )
            verify_url = verify_url_callback()
            if not verify_url:
                raise RuntimeError("未收到 Z.ai 邮箱确认链接（Verify Email）")
            self.log(f"[zcode] 打开邮箱验证链接: {verify_url[:100]}...")
            page.goto(verify_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(5000)
            # 6) Complete Registration 填密码
            self._complete_registration(page, password)
            # 7) 登录（协作验证）→ 捕获 token
            token = self._login_and_capture(browser, email, password)

        return {
            "email": email,
            "password": password,
            "token": token,
            "username": name,
        }

    # ---------------- 页面步骤 ----------------
    def _click_email_entry(self, page):
        # 2026-10-03 实锤：入口按钮 SPA 渲染慢（domcontentloaded 后 6-7s 才可见），
        # 一次性 count 检查必漏 → 改 wait_for 轮询（探针同环境验证 15s 内必现）
        btn = None
        for sel in ("button:visible", 'a:visible'):
            try:
                cand = page.locator(sel, has_text="Continue with Email").first
                cand.wait_for(state="visible", timeout=12000)
                btn = cand
                break
            except Exception:
                continue
        if btn is None:
            try:
                cand = page.locator("button:visible", has_text="Email").first
                cand.wait_for(state="visible", timeout=3000)
                btn = cand
            except Exception:
                btn = None
        if btn is None:
            self.log("[zcode] 未见 Continue with Email 入口（轮询 15s+，可能已在邮箱表单页）")
            return
        btn.click()
        self.log("[zcode] 已点 Continue with Email")
        page.wait_for_timeout(3000)

    def _goto_signup(self, page):
        """Continue with Email 默认登录表单——点 Sign up 切到注册表单（验证+重试）"""
        name_sel = 'input[placeholder*="Name"]:visible, input[placeholder*="名称"]:visible'
        for attempt in range(1, 4):
            if page.locator(name_sel).count() > 0:
                self.log("[zcode] 已在注册表单")
                return
            target = None
            for loc in (
                page.locator("a:visible", has_text="Sign up"),
                page.locator("button:visible", has_text="Sign up"),
                page.locator("text=Sign up"),
                # 2026-10-03 z.ai 页面 locale 变 zh-CN：切换钮实际文案「注册」
                page.locator("button:visible", has_text="注册"),
                page.locator("a:visible", has_text="注册"),
                page.locator("text=注册"),
            ):
                try:
                    if loc.count() > 0:
                        target = loc.first
                        break
                except Exception:
                    continue
            if target is None:
                # SPA 渲染慢：再等一轮 name 表单/切换钮出现，仍无才放弃（17:51 败因之一）
                try:
                    page.locator(name_sel).first.wait_for(state="visible", timeout=8000)
                    self.log("[zcode] 等待后已在注册表单")
                    return
                except Exception:
                    pass
                self.log("[zcode] 未见 Sign up 入口（已二次等待，可能已在注册表单）")
                return
            href = None
            try:
                href = target.get_attribute("href")
            except Exception:
                href = None
            if href and href.startswith("http"):
                page.goto(href, wait_until="domcontentloaded", timeout=30000)
            else:
                # 普通 click 会等元素稳定+可接收事件；force 可能点在动画中的旧坐标上
                try:
                    target.click(timeout=5000)
                except Exception:
                    try:
                        target.click(force=True, timeout=5000)
                    except Exception:
                        target.press("Enter")
            # 验证表单真的切换了——没切换就重试
            try:
                page.locator(name_sel).first.wait_for(state="visible", timeout=10000)
                self.log("[zcode] 已切换注册表单（Sign up）")
                return
            except Exception:
                self.log(f"[zcode] Sign up 点击后未见注册表单（第 {attempt}/3 次）——重试")
        try:
            inputs = page.eval_on_selector_all(
                "input:visible",
                "els => els.map(e => (e.type||'')+'|'+(e.placeholder||'')).filter(t=>t)",
            )
            btns = page.eval_on_selector_all(
                "button:visible", "els => els.map(e=>e.innerText.trim()).filter(t=>t)"
            )
        except Exception:
            inputs, btns = [], []
        raise RuntimeError(f"zcode 未能切换到注册表单（仍在登录页）。当前输入框: {inputs} 按钮: {btns}")

    def _fill_form(self, page, name: str, email: str, password: str):
        name_input = page.locator(
            'input[placeholder*="Name"]:visible, input[placeholder*="名称"]:visible'
        ).first
        mail_input = page.locator(
            'input[placeholder*="Email"]:visible, input[placeholder*="邮箱"]:visible, '
            'input[type="email"]:visible'
        ).first
        pwd_input = page.locator(
            'input[placeholder*="Password"]:visible, input[placeholder*="密码"]:visible, '
            'input[type="password"]:visible'
        ).first
        name_input.fill(name)
        page.wait_for_timeout(400)
        mail_input.fill(email)
        page.wait_for_timeout(400)
        pwd_input.fill(password)
        self.log("[zcode] 已填名称/邮箱/密码")

    def _fill_login(self, page, email: str, password: str):
        mail_input = page.locator(
            'input[placeholder*="Email"]:visible, input[placeholder*="邮箱"]:visible, '
            'input[type="email"]:visible'
        ).first
        pwd_input = page.locator(
            'input[placeholder*="Password"]:visible, input[placeholder*="密码"]:visible, '
            'input[type="password"]:visible'
        ).first
        mail_input.fill(email)
        page.wait_for_timeout(400)
        pwd_input.fill(password)
        self.log("[zcode] 已填登录邮箱/密码")

    # ---------------- 协作验证（关键） ----------------
    def _captcha_visible(self, page) -> bool:
        """滑块弹窗是否打开（window-show 状态）。

        注意：aliyunCaptcha 的常驻外壳（#aliyunCaptcha-captcha-body / .aliyun-captcha）
        在点击前就可见，不能用它判断"验证进行中"；只有弹窗浮层
        #aliyunCaptcha-window-float 带 window-show class 时才表示滑块已弹出。
        """
        try:
            return page.locator("#aliyunCaptcha-window-float.window-show:visible").count() > 0
        except Exception:
            return False

    def _wait_captcha_ready(self, page, timeout: int = 20) -> bool:
        """等待阿里云验证组件渲染完成（SDK 异步加载，晚于表单渲染）"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for sel in (
                "#aliyunCaptcha-captcha-body",
                "#captcha-element",
                "#aliyunCaptcha-captcha-wrapper",
                "text=Click to start verification",
            ):
                try:
                    if page.locator(sel).count() > 0:
                        return True
                except Exception:
                    continue
            page.wait_for_timeout(2000)
        return False

    def _click_captcha_trigger(self, page, retries: int = 3) -> bool:
        """点击 'Click to start verification' 触发滑块弹窗，并用『弹窗出现』确认点击生效。

        阿里云滑块监听完整鼠标事件序列（mousedown/mouseup/pointerdown），
        el.click() 原生 click 可能不触发——所以优先 Playwright 完整点击，
        每次点击后检查 #aliyunCaptcha-window-float.window-show 是否出现，未出现则重试。
        """
        if not self._wait_captcha_ready(page):
            self.log("[zcode] 30s 内未见阿里云验证组件")
            return False
        for attempt in range(1, retries + 1):
            for sel in (
                "#aliyunCaptcha-captcha-body",
                "#aliyunCaptcha-captcha-wrapper",
                "#captcha-element",
                "text=Click to start verification",
                "text=点击开始验证",
            ):
                loc = page.locator(sel)
                if loc.count() == 0:
                    continue
                try:
                    # 滚动进视口：登录表单的验证元素可能在折叠区下方，
                    # force 点击视口外元素会报 outside of the viewport
                    try:
                        loc.first.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                    # JS 兜底滚动：嵌套滚动容器内 scroll_into_view 可能无效
                    # （2026-10-04 登录页 #captcha-element 在折叠区，3 连击全报 viewport 外）
                    if sel.startswith("#") or sel.startswith("."):
                        try:
                            page.evaluate(
                                "s => { const el = document.querySelector(s);"
                                " if (el) el.scrollIntoView({ block: 'center', behavior: 'instant' }); }",
                                sel,
                            )
                            page.wait_for_timeout(400)
                        except Exception:
                            pass
                    # 完整鼠标事件点击（非 el.click()）
                    loc.first.click(timeout=8000, force=True)
                except Exception as e:
                    self.log(f"[zcode] 验证按钮 {sel} 点击异常: {str(e)[:80]}")
                    continue
                # 确认弹窗真的弹出（点击生效的信号）
                for _ in range(6):  # 最多等 ~12s
                    page.wait_for_timeout(2000)
                    if self._captcha_visible(page):
                        self.log(f"[zcode] 验证按钮点击生效（滑块弹窗已弹出，第 {attempt} 次尝试）")
                        return True
                self.log(f"[zcode] 点击 {sel} 后弹窗未出现——重试")
            page.wait_for_timeout(2000)
        return False

    def _collab_verify(
        self,
        page,
        submit_texts: list,
        timeout: int = 300,
        verification_required: bool = True,
    ):
        """滑块验证：本地自动求解（无人值守）→ 可选人工兜底 → 严格判定后提交。

        verification_required=True（注册表单）：验证是强制的——自动求解失败且
        未开人工兜底时直接报错，绝不带着未验证状态提交（否则必被风控拦截）。
        verification_required=False（登录表单，页面无验证组件）：直接提交。
        """
        clicked = self._click_captcha_trigger(page)
        if not clicked:
            if verification_required:
                raise RuntimeError("zcode 注册表单需要图形验证，但验证按钮点击无效/弹窗未出现——不提交（防止风控拦截）")
            # 登录表单：区分「真没有验证组件」与「有组件但弹窗调不出」。
            # 后者直接提交必被风控吞成 guest 会话（2026-10-04 实测）——宁可精确报错
            if self._wait_captcha_ready(page, timeout=3):
                raise RuntimeError(
                    "zcode 登录页存在验证组件但滑块弹窗调不出——不提交"
                    "（静默提交只会拿到 guest 会话，token 严格闸必拦）"
                )
            self.log("[zcode] 无验证组件（登录表单）——直接提交")
            self._click_submit(page, submit_texts)
            page.wait_for_timeout(4000)
            return

        # ② 人工兜底=纯人工模式：不做自动拖拽（自动重试曾把页面拖挂），
        #    窗口保持可见，等用户滑过后严格判定放行（2026-10-04 用户拍板）
        if self.manual_fallback:
            self.log("[zcode] 人工模式：请在弹出的浏览器窗口手动滑动滑块解锁")
            passed, how = solve_slider(
                page,
                click_trigger_fn=self._click_captcha_trigger,
                log=self.log,
                manual_wait_sec=timeout,
            )
        else:
            passed, how = solve_slider(page, click_trigger_fn=self._click_captcha_trigger, log=self.log)
        if passed:
            self.log(f"[zcode] 滑块通过（{how}）")
        else:
            raise RuntimeError(
                f"zcode 滑块未通过（{how}）——不提交"
                + ("（人工模式超时，请在窗口出现后 300s 内完成滑动）" if self.manual_fallback else "（无人值守模式，可在配置开 zcode_slider_manual_fallback 转人工）")
            )

        self._click_submit(page, submit_texts)
        page.wait_for_timeout(4000)

    def _click_submit(self, page, submit_texts: list):
        for t in submit_texts:
            btn = page.locator("button:visible", has_text=t)
            if btn.count() > 0:
                try:
                    btn.first.click(timeout=8000)
                    self.log(f"[zcode] 已点提交按钮: {t}")
                    return
                except Exception as e:
                    self.log(f"[zcode] 提交按钮 {t} 点击失败: {e}")
        raise RuntimeError(f"zcode 未找到提交按钮: {submit_texts}")

    # ---------------- 邮箱确认 / 登录 ----------------
    def _complete_registration(self, page, password: str):
        """激活链接打开后设置密码（Complete Registration 页）——轮询等待页面出现"""
        # 打开激活链接后页面可能还在加载——轮询等待 Complete Registration 页（最多 60s）
        deadline = time.time() + 60
        while time.time() < deadline:
            page.wait_for_timeout(3000)
            body = page.evaluate('document.body ? document.body.innerText.slice(0, 400) : ""')
            if "Complete Registration" in body or "完成注册" in body:
                break
            self.log("[zcode] 等待 Complete Registration 页加载...")
        else:
            self.log(f"[zcode] 60s 内未见 Complete Registration 页: {body.replace(chr(10), ' ')[:180]}")
            return
        enter = page.locator('input[placeholder*="Enter your password"]:visible, input[placeholder*="密码"]:visible').first
        confirm = page.locator('input[placeholder*="Confirm your password"]:visible, input[placeholder*="确认"]:visible').first
        if enter.count() > 0 and confirm.count() > 0:
            enter.fill(password)
            page.wait_for_timeout(400)
            confirm.fill(password)
            self.log("[zcode] Complete Registration 已填密码")
        btn = page.locator("button:visible", has_text="Complete Registration")
        if btn.count() == 0:
            btn = page.locator("button:visible", has_text="完成注册")
        if btn.count() > 0:
            btn.first.click()
            self.log("[zcode] 已点 Complete Registration")
            page.wait_for_timeout(8000)
        body2 = page.evaluate('document.body ? document.body.innerText.slice(0, 300) : ""')
        self.log(f"[zcode] 注册完成页: {body2.replace(chr(10), ' ')[:180]}")

    def _capture_token(self, page) -> str:
        try:
            token = (
                page.evaluate(
                    "() => localStorage.getItem('token') || localStorage.getItem('JWT') || ''"
                )
                or ""
            )
            if token.startswith("eyJ"):
                return token
            # 兜底：cookie 里的会话 token（部分形态写 cookie 不写 localStorage）
            for c in page.context.cookies():
                if c.get("name", "").lower() in ("token", "jwt", "access_token") and str(
                    c.get("value", "")
                ).startswith("eyJ"):
                    return c["value"]
        except Exception:
            pass
        return ""

    def _validate_token(self, token: str, email: str) -> str:
        """token 真伪复核：非空 + JWT 形态 + 非 guest + email 匹配（假阳性防线）。

        返回错误原因字符串，None=通过。旧口径只查 guest 前缀，服务端未认真时
        发的"看起来正常"的假会话 token 会漏网——email 对不上即判失败。
        """
        if not token:
            return "未捕获到 token"
        if not token.startswith("eyJ"):
            return f"token 非 JWT 形态（{token[:12]}…）"
        tok_email = _token_email(token)
        if tok_email and tok_email.startswith("guest"):
            return f"guest token（{tok_email}）——登录未真正生效"
        if email and tok_email and tok_email.lower() != email.lower():
            return f"token email 不匹配（got={tok_email}, want={email}）——服务端会话可疑"
        if email and not tok_email:
            return "token payload 无 email 字段——非正式会话形态"
        return None

    def _login_and_capture(self, browser, email: str, password: str) -> str:
        """登录后捕获会话 token（滑块自动求解 + token 严格复核）"""
        page2 = browser.new_page()
        page2.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=45000)
        page2.wait_for_timeout(4000)
        self._click_email_entry(page2)
        self._fill_login(page2, email, password)
        self._collab_verify(
            page2,
            submit_texts=["Sign in", "登录"],
            verification_required=False,  # 登录表单多数无验证组件——直接提交
        )
        # OAuth 授权（如出现 Authorize/Allow）
        auth_btn = page2.locator("button:visible", has_text="Authorize")
        if auth_btn.count() == 0:
            auth_btn = page2.locator("button:visible", has_text="Allow")
        if auth_btn.count() > 0:
            auth_btn.first.click()
            self.log("[zcode] 已点 OAuth 授权")
            page2.wait_for_timeout(6000)
        # token 捕获 + 严格复核（首轮 8s，未过再等 12s 复查一次——会话写入有延迟）
        deadline = time.time() + 20
        token, reason = "", "未捕获"
        while time.time() < deadline:
            token = self._capture_token(page2)
            reason = self._validate_token(token, email)
            if reason is None:
                break
            page2.wait_for_timeout(3000)
        if reason is not None:
            body = page2.evaluate('document.body ? document.body.innerText.slice(0, 200) : ""')
            raise RuntimeError(
                f"zcode 登录后 token 复核失败：{reason}（页面: {body[:120].replace(chr(10), ' ')})"
            )
        self.log(f"[zcode] 捕获会话 token（长度 {len(token)}，email 匹配 {email}）")
        return token
