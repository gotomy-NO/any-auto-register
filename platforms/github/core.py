"""GitHub 半自动注册核心流程
人工环节（GitHub 强制，无法自动化）：DataDome 滑块 + 注册 puzzle —— 在弹出的 Camoufox 窗口人工完成
自动环节：填表（动态探测）、提交、ccwu 邮局收 launch code、回填验证码
持久 profile：E:/AI/gh_persistent_profile（DataDome cookie 沉淀，越用越好过）
"""
import json
import os
import sys
import re
import time
import random
from typing import Callable, Optional

GH_PROFILE = r"D:\AI\gh_runtime\gh_persistent_profile"
SIGNUP_URL = "https://github.com/signup"
MAIL_URL = "https://2e-mail.ccwu.cc"
_CCWU_API = "https://2e-mail.ccwu.cc"


def ccwu_api_login(full_email: str, mail_password: str):
    """ccwu API 登录（用户自建邮局，无风控，纯 requests）→ (token, accountId)"""
    import json
    import urllib.request

    # 头必须完整（UA/Origin/Referer 缺一会被 403）：对齐 scripts/ccwu_inbox_api_v5.py 成功形态
    _H = {"content-type": "application/json",
          "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
          "Referer": _CCWU_API + "/", "Origin": _CCWU_API}
    body = json.dumps({"email": full_email, "password": mail_password}).encode()
    r = urllib.request.Request(f"{_CCWU_API}/api/login", data=body, method="POST", headers=_H)
    with urllib.request.urlopen(r, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    if data.get("code") != 200:
        raise RuntimeError(f"ccwu 登录失败: {data.get('message')}")
    token = data["data"]["token"]
    # 拿 accountId（GET account/list）
    r2 = urllib.request.Request(f"{_CCWU_API}/api/account/list?accountId=0&size=30",
                                headers={**{"Authorization": token}, **_H})
    with urllib.request.urlopen(r2, timeout=15) as resp2:
        accts = json.loads(resp2.read().decode()).get("data") or []
    local = full_email.split("@")[0]
    aid = None
    for a in accts:
        if (a.get("email") == full_email or a.get("name") == local):
            aid = a.get("accountId")
            break
    if aid is None and accts:
        aid = accts[0].get("accountId")
    if aid is None:
        raise RuntimeError(f"ccwu 未找到邮箱 {full_email} 的账号")
    return token, aid


def ccwu_api_fetch_code(full_email: str, mail_password: str,
                        log_fn=print, timeout: int = 300) -> Optional[str]:
    """ccwu API 收取 GitHub launch code：登录→轮询 email/list→text 里提 6~8 位码"""
    import json
    import urllib.request

    _H = {"content-type": "application/json",
          "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
          "Referer": _CCWU_API + "/", "Origin": _CCWU_API}
    token, aid = ccwu_api_login(full_email, mail_password)
    deadline = time.time() + timeout
    while time.time() < deadline:
        url = (f"{_CCWU_API}/api/email/list?accountId={aid}&allReceive=0&emailId=0"
               f"&timeSort=0&size=50&type=0")
        r = urllib.request.Request(url, headers={**{"Authorization": token}, **_H})
        try:
            with urllib.request.urlopen(r, timeout=15) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            log_fn(f"  ccwu 轮询异常: {str(e)[:80]}")
            time.sleep(10)
            continue
        if data.get("code") == 200:
            for m in (data.get("data") or {}).get("list") or []:
                text = (m.get("text") or "") + " " + (m.get("subject") or "")
                if "github" in text.lower() and "launch code" in text.lower():
                    mm = re.search(r"\b(\d{6,8})\b", text)
                    if mm:
                        return mm.group(1)
        time.sleep(8)
    return None


def _pause(lo=0.8, hi=1.6):
    time.sleep(random.uniform(lo, hi))


# ── DataDome 信任 cookie 携带（regkit 成功工具同款策略，2026-09-12 实锤根因修复）──
# 每轮清 profile 会把"已过 DataDome 的信任 cookie"一起扔掉 → 每轮都是零 cookie 新浏览器 → 必被挑战。
# 正确做法：新 profile（不堆 GitHub 登录态）+ datadome 信任 cookie 单独持久化、按出口 IP 匹配注入。
# cookie 绑定 IP：IP 不匹配时注入=伪造感更强，宁可不注（重新接受挑战）。
_TRUST_FILE = r"D:\AI\gh_runtime\gh_datadome_trust.json"
_TRUST_COOKIE_NAMES = {"datadome", "datadome_proxied", "device_id", "_device_id"}


def _save_trust_cookie(context, exit_ip, log) -> None:
    """成功轮结束时持久化 DataDome 信任 cookie（绑定出口 IP）。"""
    try:
        cookies = context.cookies()
        keep = [c for c in cookies if c.get("name") in _TRUST_COOKIE_NAMES]
        if not keep:
            log("  trust-cookie: 无 datadome cookie 可存"
                f"（context cookies 共 {len(cookies)} 条）")
            return
        with open(_TRUST_FILE, "w", encoding="utf-8") as f:
            json.dump({"exit_ip": exit_ip or "", "cookies": keep}, f, ensure_ascii=False)
        log(f"  trust-cookie: 已存 {len(keep)} 条 (ip={exit_ip or 'n/a'})")
    except Exception as e:
        log(f"  trust-cookie 存档失败: {type(e).__name__}: {str(e)[:100]}")


def _restore_trust_cookie(context, exit_ip, log) -> None:
    """新轮开始时注入信任 cookie——仅当出口 IP 与存档一致。"""
    try:
        import os
        if not os.path.isfile(_TRUST_FILE):
            log("  trust-cookie: 无存档（本轮将接受 DataDome 首次挑战）")
            return
        with open(_TRUST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        saved_ip = data.get("exit_ip") or ""
        if not exit_ip or saved_ip != exit_ip:
            log(f"  trust-cookie: IP 不匹配（存档 {saved_ip or 'n/a'} vs 当前 {exit_ip or 'n/a'}）→ 跳过注入")
            return
        cookies = data.get("cookies") or []
        if cookies:
            context.add_cookies(cookies)
            log(f"  trust-cookie: 已注入 {len(cookies)} 条 (ip={exit_ip})")
    except Exception as e:
        log(f"  trust-cookie 注入失败: {str(e)[:80]}")


# 模拟鼠标当前位置（Playwright 无查询 API，脚本内维护）
_mouse_pos = {"x": 640, "y": 360}


def _human_click(page, loc):
    """真人鼠标点击：分段移动带轨迹（非瞬移）、落点在元素内随机偏移（人不会每次点正中心）、
    按下-抬起有微停顿、移动途中随机小停顿。"""
    global _mouse_pos
    try:
        box = loc.bounding_box()
    except Exception:
        box = None
    if not box or box.get("width", 0) <= 0:
        loc.click()
        return
    # 落点：元素范围内随机偏移，避开边缘
    tx = box["x"] + box["width"] * random.uniform(0.3, 0.7)
    ty = box["y"] + box["height"] * random.uniform(0.3, 0.7)
    # 分 2~3 段移动（真人鼠标不是直线瞬移，中途有修正轨迹）
    segs = random.randint(2, 3)
    sx, sy = _mouse_pos["x"], _mouse_pos["y"]
    for s in range(1, segs + 1):
        ratio = s / segs
        mx = sx + (tx - sx) * ratio + random.uniform(-6, 6) * (1 - ratio)
        my = sy + (ty - sy) * ratio + random.uniform(-6, 6) * (1 - ratio)
        page.mouse.move(mx, my, steps=random.randint(6, 18))
        time.sleep(random.uniform(0.02, 0.12))
    _mouse_pos = {"x": tx, "y": ty}
    time.sleep(random.uniform(0.08, 0.25))  # 落定后微停顿再按
    page.mouse.down()
    time.sleep(random.uniform(0.04, 0.12))  # 按住时长（真人按键 50~100ms）
    page.mouse.up()


def _human_wander(page, viewport=None, duration_s=10):
    """已停用（2026-10-05 用户实测指令）：Camoufox 的 Playwright/Firefox 鼠标通道
    在 GitHub 页面会触发 juggler dispatchDragEvent NS_ERROR_FAILURE，且该同步调用
    可能永久挂死主线程（run2 实测挂 6 分钟）。真浏览器路线不再需要脚本漫游。"""
    return


def _click_continue(page, texts, log) -> bool:
    """点匹配的继续/提交按钮；逐个检查按钮全文，排除 'Continue with Google/Apple/Passkey'
    等第三方按钮（误点会跳 accounts.google.com 导致流程跑飞）。命中返回 True。"""
    for txt in texts:
        btns = page.locator(f"button:visible, [role=button]:visible")
        for i in range(btns.count()):
            b = btns.nth(i)
            label = (b.inner_text() or "").strip()
            if txt.lower() in label.lower() and not any(
                bad in label.lower() for bad in ("google", "apple", "passkey")):
                _pause()
                _human_click(page, b)
                log(f"  点按钮: {label[:40]}")
                time.sleep(2)
                return True
    return False


# ── DataDome 参数采集（自动化后续用）──
_DD_PARAMS_FILE = r"D:\\AI\\gh_runtime\\gh_datadome_params_last.json"


def _capture_datadome_params(context, page, exit_ip, log) -> None:
    """注册流程结束时采集 DataDome 相关参数 → scratch JSON（供后续协议线复用）。"""
    try:
        params = {"captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "exit_ip": exit_ip or "", "ua": ""}
        try:
            params["ua"] = page.evaluate("navigator.userAgent")
        except Exception:
            pass
        try:
            params["page_url"] = (page.url or "")[:300]
        except Exception:
            page_url = ""
        # 挑战/验证 iframe URL：t=fe/bv、initialCid、hash、cid、s 等都在 query 里
        frames = []
        try:
            frames = [(f.url or "") for f in page.frames]
        except Exception:
            pass
        dd_urls = [u for u in frames if any(
            k in u for k in ("datadome", "captcha-delivery", "interstitial"))]
        params["captcha_frames"] = dd_urls
        # 内联 dd 对象（页面 JS 全局）
        try:
            params["dd_object"] = page.evaluate(
                "() => { try { const d = window.dd || window.DD || {};"
                " return JSON.parse(JSON.stringify(d, (k,v) =>"
                " typeof v === 'function' ? '<fn>' : v)); }"
                " catch(e) { return {'err': String(e)}; } }")
        except Exception as e:
            params["dd_object"] = {"err": str(e)[:120]}
        # 全部 cookie 里的 datadome 系（值只留前缀8+长度，防泄漏）
        try:
            cookies = context.cookies()
            dd_ck = []
            for c in cookies:
                if c.get("name", "").lower() in ("datadome", "datadome_proxied") or \
                   "dd" in c.get("name", "").lower():
                    v = str(c.get("value", ""))
                    dd_ck.append({"name": c.get("name"), "domain": c.get("domain"),
                                  "value_prefix8": v[:8], "value_len": len(v)})
            params["datadome_cookies"] = dd_ck
        except Exception as e:
            params["datadome_cookies_err"] = str(e)[:120]
        with open(_DD_PARAMS_FILE, "w", encoding="utf-8") as f:
            json.dump(params, f, ensure_ascii=False, indent=2)
        log(f"  DataDome 参数已采集 → {_DD_PARAMS_FILE}"
            f"（frames={len(dd_urls)} cookies={len(params.get('datadome_cookies', []))}）")
    except Exception as e:
        log(f"  DataDome 参数采集失败: {type(e).__name__}: {str(e)[:100]}")


class GithubRegister:
    def __init__(self, log_fn: Callable = print, proxy: str = None):
        # 后端控制台是 GBK，页面文本（韩文等）直接打印会 UnicodeEncodeError 炸掉任务——安全包装
        def _safe_log(msg):
            s = str(msg)
            try:
                log_fn(s)
            except UnicodeEncodeError:
                log_fn(s.encode("gbk", errors="replace").decode("gbk"))
        self.log = _safe_log
        self.proxy = proxy

    def _launch_opts(self):
        """Camoufox 启动参数（2026-09-11 按昨日手册恢复：固定指纹 pickle 存档复用——
        指纹是注册机最宝贵的沉淀，每次重生成=最强自动化特征必触发风控；
        真直连（不传 proxy 或 "direct"）+ en-US 语言头（社区实测 zh_CN 语言头会触发拦截））"""
        from camoufox import launch_options
        from camoufox.addons import DefaultAddons

        kw = dict(headless=False, geoip=True, humanize=True,
                  exclude_addons=[DefaultAddons.UBO],
                  firefox_user_prefs={"intl.accept_languages": "en-US,en",
                                      "intl.locale.requested": "en-US",
                                      "ui.language": "en-US"})
        # 指纹策略（2026-09-12 泄漏检测实锤）：不再手动塞固定指纹——
        # 存档指纹是 150 内核导出的 UA，与 152 内核版本错配 = DataDome 强信号（十几轮全败根因）。
        # 改为不传 fingerprint，Camoufox 自动生成与自身内核一致的指纹（UA=rv:152 实测一致）。
        # 代价：指纹每次启动变化 → cookie 跟着作废，但每轮都清 profile，本就无 cookie 可保。
        if self.proxy and self.proxy != "direct":
            kw["proxy"] = {"server": self.proxy}
            self.log(f"geoip 指纹按代理出口生成: {self.proxy}")
        else:
            # 直连模式：geoip 自动时区只对 proxy 生效（源码 if proxy 才查 ip-api），
            # 直连时指纹时区停留在生成时的地理 → 与当前出口 IP 不匹配 = DataDome 经典检测项。
            # 主动查当前出口 IP 的时区，显式传给上下文。
            _direct_tz = None
            try:
                import urllib.request as _u
                _tz = _ip = None
                for _url in ("http://ip-api.com/json?fields=query,timezone",
                             "https://ipwho.is/"):  # 502/超时自动换备用源
                    try:
                        with _u.urlopen(_url, timeout=10) as _r:
                            _geo = json.loads(_r.read().decode())
                        _ip = _geo.get("query") or _geo.get("ip")
                        _tz = _geo.get("timezone")
                        # ipwho.is 返回对象 {'id': 'Asia/Seoul', ...} → 取字符串
                        if isinstance(_tz, dict):
                            _tz = _tz.get("id")
                        if _tz:
                            break
                    except Exception:
                        continue
                if _tz:
                    _direct_tz = _tz
                    self.log(f"直连出口 {_ip} → 对齐时区 {_tz}")
                else:
                    self.log("出口时区查询失败（两源均不可用，保持指纹原时区）")
            except Exception as e:
                self.log(f"出口时区查询失败（保持指纹原时区）: {e}")

        opts = launch_options(**kw)
        opts.pop("headless", None)
        env = opts.get("env")
        if isinstance(env, dict):
            for k in list(env.keys()):
                if k.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                    env.pop(k, None)  # env 代理一律剥掉，避免与显式 proxy 参数冲突/双代理
        if self.proxy and self.proxy != "direct":
            opts["proxy"] = {"server": self.proxy}
            self.log(f"浏览器走代理: {self.proxy}")
        allowed = {"executable_path", "args", "ignore_default_args", "env", "proxy",
                   "downloads_path", "slow_mo", "timeout", "firefox_user_prefs",
                   "timezone_id"}
        opts = {k: v for k, v in opts.items() if k in allowed}
        # 直连时区对齐：launch_options 不认 timezone_id，最后注入传给 launch_persistent_context
        if self.proxy in (None, "", "direct") and _direct_tz:
            opts["timezone_id"] = _direct_tz
        return opts

    def _visible_inputs(self, page):
        return page.eval_on_selector_all(
            "input:visible",
            "els => els.map((e,i) => ({i, type: e.type||'text', ph: e.placeholder||'', "
            "name: e.name||'', id: e.id||''}))",
        )

    def register(self, email: str, password: str, mail_password: str = "",
                 otp_callback: Optional[Callable] = None,
                 manual_timeout: int = 600, code_timeout: int = 300) -> dict:
        """半自动注册：返回 {email, password, username, status}
        email: 邮箱全称；password: GitHub 密码
        收码双通道：otp_callback（邮箱后端，outlook 池等）优先；否则 mail_password 走 ccwu 网页邮局
        """
        from playwright.sync_api import sync_playwright

        mail_name = email.split("@")[0]
        result = {"email": email, "password": password, "username": "", "status": "failed"}

        # ── 预热：ccwu 域邮箱先 API 登录验证通道（秒级），失败提前止损，不浪费浏览器流程 ──
        ccwu_ready = False
        if otp_callback is not None:
            self.log("收码通道: 邮箱后端 (otp_callback)")
            ccwu_ready = True
        elif mail_password and email.lower().endswith("@2e-mail.ccwu.cc"):
            try:
                ccwu_api_login(email, mail_password)
                ccwu_ready = True
                self.log("ccwu API 预热成功（收码通道就绪）")
            except Exception as e:
                self.log(f"ccwu 预热失败: {str(e)[:100]}")
        if not ccwu_ready:
            raise RuntimeError(f"收码通道不可用（无 otp_callback，且 ccwu 登录失败/邮箱非 ccwu 域: {email}）")

        # ── 引擎选择（2026-10-05 用户定调：全项目统一用 Camoufox）──
        # 统一引擎 = Camoufox（本项目所有注册对象一致）。真实 Edge/Chrome 仅作备用：
        # 需要时设 GH_BROWSER_ENGINE=edge（走 chrome_real + 持久 profile gh_trusted）。
        # 注意：无论哪个引擎，"进入注册页必须从首页真实点击"这条铁律不变（深链必被 DataDome 判 t=bv）。
        engine = os.environ.get("GH_BROWSER_ENGINE", "camoufox").strip().lower()
        with sync_playwright() as p:
            _env = None
            _pre_page = None
            _proxy_arg = self.proxy if self.proxy and self.proxy != "direct" else None
            if engine in ("camoufox", "firefox", "cf"):
                from core.browser_env import make_context as _make_context
                _env = _make_context(
                    "gh_camoufox", backend="camoufox", persist=True, headless=False,
                    proxy=_proxy_arg, log=self.log)
                browser = _env["browser"]
                _pre_page = _env.get("page")
                self.log("引擎: Camoufox（统一引擎；持久 profile gh_camoufox、地理对齐、指纹与内核一致）")
            elif engine in ("edge", "chrome", "chrome_real"):
                from core.browser_env import make_context as _make_context
                _env = _make_context(
                    "gh_trusted", backend="chrome_real", persist=True, headless=False,
                    proxy=_proxy_arg, log=self.log)
                browser = _env["browser"]
                _pre_page = _env.get("page")
                self.log("引擎: 真实 Edge/Chrome（备用；持久 profile gh_trusted）")
            else:
                browser = p.firefox.launch_persistent_context(
                    GH_PROFILE, headless=False, **self._launch_opts())
                self.log("引擎: 旧 Camoufox 路径（legacy，保留兼容）")
            try:
                # DataDome 信任 cookie 注入（regkit 同款：IP 匹配才注入，绑定 _launch_opts 查到的出口）
                _exit_ip_now = ""
                try:
                    import urllib.request as _u
                    _exit_ip_now = _u.urlopen("https://api.ipify.org", timeout=10).read().decode().strip()
                except Exception:
                    pass
                _restore_trust_cookie(browser, _exit_ip_now, self.log)
                page = _pre_page or (browser.pages[0] if browser.pages else browser.new_page())

                # ── 阶段0：人类路径逐步导航（防深链直跳被风控识别）──
                # 真人不会直接打开 /signup 深链：官网首页 → 点 Sign up → 注册页，逐页走
                def _wait_pass(p, label, require_sel=""):
                    """等当前页放行：默认出现可见 input 或无挑战 iframe；
                    require_sel 传入时（如 email 输入框选择器）必须出现该元素才算放行——
                    否则 DataDome 挑战未放行也会被误判"有 input"导致填表全 False"""
                    if require_sel:
                        ok = p.locator(require_sel).count() > 0
                    else:
                        ok = p.locator("input:visible").count() > 0 or p.locator("iframe").count() == 0
                    deadline = time.time() + manual_timeout
                    _wait_pass_loops = 0
                    while time.time() < deadline and not ok:
                        # 轮询间隔 15s（2026-09-12 实锤：3s 高频探测本身是机器人信号，
                        # 导致"滑块划完又弹"无限挑战；20s 又会错过窗口，15s 折中）
                        # 且绝不自动 reload
                        time.sleep(15)
                        _wait_pass_loops += 1
                        if require_sel:
                            ok = p.locator(require_sel).count() > 0
                        else:
                            ok = p.locator("input:visible").count() > 0 or p.locator("iframe").count() == 0
                        self.log(f"  [{label}] 等放行 [剩 {int(deadline-time.time())}s]（若弹出验证请人工完成，勿刷新）")
                    if not ok:
                        # 超时前先抢救信任 cookie（DataDome 拦截页也会发 cookie，下轮同 IP 可直通）
                        try:
                            _save_trust_cookie(browser, _exit_ip_now, self.log)
                        except Exception:
                            pass
                        raise RuntimeError(f"{label}: 未放行/表单未出现（人工验证超时或结构变化）")

                self.log("step1: 打开 GitHub 官网首页")
                page.goto("https://github.com/", wait_until="domcontentloaded", timeout=60000)
                time.sleep(random.uniform(2, 3))
                _wait_pass(page, "首页")
                # 真人鼠标漫游（40s 节奏版）：快速扫视 3~4s 即可，真人注册不会在首页闲逛
                self.log("  拟真浏览: 首页漫游")
                _human_wander(page, duration_s=random.uniform(3, 4))

                self.log("step2: 点 Sign up 按钮进入注册页")
                # 【2026-10-05 实测结论·勿改回去】绝不可回退到 goto(SIGNUP_URL) 深链：
                # 深链进 /signup 必被 DataDome 判 t=bv（静态封禁、无滑块可解，12/12 复现）；
                # 而从首页真实点击进入（落地 URL 带 ref_cta=Sign+up&source=header-home）表单必定渲染（已复现 3 次）。
                # 因此这里只走"页内真实点击"的多条入口，全部失败就明确报错，绝不深链。
                entered = False
                for attempt in range(3):
                    signup_btn = page.locator("a[href='/signup'], a[href*='/signup'], a[href*='source=header']").first
                    if signup_btn.count() > 0:
                        _pause(0.8, 1.5)
                        _human_click(page, signup_btn)
                        time.sleep(random.uniform(2, 3))
                        entered = True
                        break
                    # 首页没有注册入口（可能在登录页/已登录态）→ 先回首页再试；仍无则从登录页找"Create an account"
                    self.log(f"  首页未见注册入口（第 {attempt + 1} 次），回首页/登录页找入口（不回退深链）")
                    if attempt == 0:
                        page.goto("https://github.com/", wait_until="domcontentloaded", timeout=60000)
                    elif attempt == 1:
                        page.goto("https://github.com/login", wait_until="domcontentloaded", timeout=60000)
                    time.sleep(random.uniform(2, 3))
                    _human_wander(page, duration_s=random.uniform(2, 3))
                if not entered:
                    raise RuntimeError("注册入口未通过页内点击进入（首页/登录页都未找到 Sign up 链接）——"
                                       "按实测结论不允许回退深链，如遇此报错请人工确认页面结构")
                _wait_pass(page, "注册页",
                           require_sel='input[name*="user[email]"], input[type="email"], '
                                       'input[name*="email"], input[placeholder*="Email"]')
                # 注册页漫游（40s 节奏版）：快速扫一眼表单 2~3s
                self.log("  拟真浏览: 注册页漫游")
                _human_wander(page, duration_s=random.uniform(2, 3))
                self.log("注册表单已出现，开始自动填表")

                # ── 阶段2：自动填表（动态探测语义匹配）──
                # 真人击键模式：fill() 瞬时填充会被 GitHub 判"输入速度异常"（韩文锁页说明原文），
                # 改为点击聚焦 → 逐字符键入（随机打字速度）→ 字段间停顿，全程模拟真人节奏
                filled = {"email": False, "password": False, "user": False}
                username = ""

                def _human_type(loc, text):
                    """真人击键输入（含真人失误模拟，2026-09-12 循环滑块根因修复）：
                    一次填对+无回退无修改=DataDome 表单行为评分的机器人信号。
                    模拟：30% 概率打错一个字符→停顿→退格→改正（真人熟手也有手滑）。"""
                    _human_click(page, loc)
                    time.sleep(random.uniform(0.15, 0.35))
                    if random.random() < 0.3 and len(text) > 4:
                        cut = random.randint(2, len(text) - 2)
                        loc.press_sequentially(text[:cut], delay=random.uniform(25, 60))
                        time.sleep(random.uniform(0.2, 0.5))
                        # 手滑打错一个键（相邻键随机）
                        wrong = random.choice("xnmlovuik")
                        loc.press_sequentially(wrong, delay=random.uniform(30, 70))
                        time.sleep(random.uniform(0.3, 0.7))  # 真人发现打错的停顿
                        loc.press_sequentially("Backspace", delay=60)
                        time.sleep(random.uniform(0.15, 0.4))
                        loc.press_sequentially(text[cut:], delay=random.uniform(25, 60))
                    else:
                        loc.press_sequentially(text, delay=random.uniform(25, 60))
                    time.sleep(random.uniform(0.1, 0.3))

                for round_ in range(8):
                    for inp in self._visible_inputs(page):
                        blob = (inp["ph"] + inp["name"] + inp["id"] + inp["type"]).lower()
                        sel = f"input:visible >> nth={inp['i']}"
                        if "email" in blob and not filled["email"]:
                            _human_type(page.locator(sel).first, email)
                            filled["email"] = True
                        elif "pass" in blob and not filled["password"]:
                            _human_type(page.locator(sel).first, password)
                            filled["password"] = True
                        elif (("user" in blob or "name" in blob or "login" in blob)
                              and "email" not in blob and not filled["user"]):
                            username = mail_name + str(random.randint(10, 99))
                            _human_type(page.locator(sel).first, username)
                            filled["user"] = True
                    if not all(filled.values()):
                        if not _click_continue(page, ["Continue", "Sign up", "Submit", "注册", "继续"], self.log):
                            if any(filled.values()):
                                page.keyboard.press("Enter")
                                time.sleep(2.5)
                    else:
                        # 三要素齐——提交前模拟真人确认：随机移动鼠标到按钮附近再点
                        _pause(1.2, 2.5)
                        try:
                            w, h = page.viewport_size["width"], page.viewport_size["height"]
                            page.mouse.move(random.randint(int(w*0.3), int(w*0.7)),
                                            random.randint(int(h*0.4), int(h*0.8)))
                            _pause(0.5, 1.2)
                        except Exception:
                            pass
                        _click_continue(page, ["Continue", "Submit", "Verify", "注册", "提交"], self.log)
                        for _ in range(3):
                            time.sleep(3)
                            if page.locator('input[autocomplete="one-time-code"], input[inputmode="numeric"]').count() > 0:
                                break
                            u = page.url or ""
                            if "verif" in u or "/login" in u or "/session" in u:
                                break
                            if not _click_continue(page, ["Continue", "Verify", "注册", "提交"], self.log):
                                break
                        break
                    time.sleep(2)
                result["username"] = username
                self.log(f"表单已填齐: {filled} 用户名={username}")

                # Country/Region 必选下拉（新版注册页强制，韩国 IP 自动带出 Korea,South 但仍需选中确认）
                try:
                    csel = page.locator("select").first
                    if csel.count() > 0:
                        csel.select_option(label="United States")
                        self.log("  已选 Country/Region: United States")
                        time.sleep(1.5)
                except Exception:
                    self.log("  Country/Region 下拉处理跳过（未找到或已选）")

                # 提交：点表单底部绿色 Continue（不用 Enter——表单页有多个按钮易误触）
                clicked = _click_continue(page, ["Continue"], self.log)
                if not clicked:
                    page.keyboard.press("Enter")
                time.sleep(3)

                # 快速失败：GitHub 判定邮箱已注册时会跳登录页（不会发验证码），立刻报错省 5 分钟空轮询
                # 注意：只看 URL——营销确认页正文含 "Sign in" 链接文字是正常的，不能按正文误判
                page_url = page.url or ""
                if "/login" in page_url or "/session" in page_url:
                    raise RuntimeError(f"提交后跳转登录页——邮箱 {email} 可能已被注册，需换新邮箱重试")

                # puzzle 人机验证环节：GitHub 发码前强制——无条件等到验证码页才继续
                # （puzzle iframe 是提交后延迟加载的，一次性探测会错过；必须等验证码输入框出现）
                self.log(">>> 等待进入验证码页（若窗口出现 puzzle 请人工完成，最多 10 分钟）")
                deadline = time.time() + manual_timeout
                on_verify = False
                _refilled = False  # puzzle 完成后表单重现 → 自动重新填表（仅一次）
                while time.time() < deadline:
                    time.sleep(15)
                    page_url = page.url or ""
                    has_otp = page.locator('input[autocomplete="one-time-code"], input[inputmode="numeric"]').count() > 0
                    n_puzzle = page.locator("iframe").count()
                    email_box = page.locator('input[name*="user[email]"], input[type="email"]').first
                    email_visible = 0
                    try:
                        email_visible = 0 if email_box.count() == 0 else (1 if email_box.is_visible() else 0)
                    except Exception:
                        email_visible = 0
                    self.log(f"  [等 {int(deadline-time.time())}s] iframes={n_puzzle} otp_input={1 if has_otp else 0} email_box={email_visible} url={page_url[:70]}")
                    # ── 打码 API 自建解滑块（capsolver DatadomeSliderTask）──
                    if n_puzzle > 0 and not _refilled:
                        try:
                            dd_frame = None
                            for f in page.frames:
                                if "datadome" in (f.url or "") or "captcha" in (f.url or "").lower():
                                    dd_frame = f
                                    break
                            if dd_frame is not None:
                                captcha_url = dd_frame.url
                                self.log(f"  检测到 DataDome challenge iframe → 调打码 API 解")
                                import subprocess
                                sol = subprocess.run(
                                    [sys.executable, r"E:\AI\openAI\注册机1\any-auto-register-main\scripts\solve_datadome.py",
                                     captcha_url, page.evaluate("navigator.userAgent")],
                                    capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
                                out = (sol.stdout or "") + (sol.stderr or "")
                                for line in out.splitlines():
                                    if line.startswith("DATADOME_COOKIE:"):
                                        dd_val = line.split(":", 1)[1].strip()
                                        # cookie 注入当前上下文（.datadome 域，全部页面生效）
                                        browser.add_cookies([{
                                            "name": "datadome", "value": dd_val,
                                            "domain": ".datadome.co", "path": "/"}])
                                        self.log("  打码成功 → datadome cookie 已注入 → reload 放行")
                                        try:
                                            page.reload(wait_until="domcontentloaded", timeout=30000)
                                        except Exception:
                                            pass
                                        break
                                    if line.startswith(("NO KEY", "solve ERR", "TIMEOUT", "createTask ERR")):
                                        self.log(f"  打码不可用: {line[:90]}")
                                        break
                        except Exception as e:
                            self.log(f"  打码流程异常（继续常规等待）: {str(e)[:80]}")
                    if has_otp or "verify" in page_url or "verif" in page_url:
                        on_verify = True
                        break
                    if "/login" in page_url or "/session" in page_url:
                        raise RuntimeError(f"跳转登录页——邮箱 {email} 可能已被注册，需换新邮箱重试")
                    # 区分"注册表单重现"与"Verify your account 面板"：
                    # 表单=email+password 都可见；面板=只有 email 框（launch code 输入面板）= 已到验证阶段！
                    # （2026-09-12 复盘：面板被误判成表单 → 重复提交 → 风控锁死）
                    pwd_chk = page.locator('input[name*="user[password]"], input[type="password"]').first
                    pwd_visible = 0
                    try:
                        pwd_visible = 0 if pwd_chk.count() == 0 else (1 if pwd_chk.is_visible() else 0)
                    except Exception:
                        pwd_visible = 0
                    if email_visible and pwd_visible and not _refilled:
                        _refilled = True
                        self.log("  puzzle 通过后注册表单重现 → 自动重新填表")
                        try:
                            _human_type(email_box, email)
                            _pause()
                            pwd_box = page.locator('input[name*="user[password]"], input[type="password"]').first
                            if pwd_box.count():
                                _human_type(pwd_box, password)
                            _pause()
                            login_box = page.locator('input[name*="user[login]"]').first
                            if login_box.count():
                                _human_type(login_box, username)
                            _pause()
                            sub = page.locator('button[type="submit"], button:has-text("Continue"), button:has-text("Create"), button:has-text("Submit")').first
                            if sub.count():
                                _human_click(page, sub)
                            self.log("  重填完成，继续等验证码页")
                        except Exception as e:
                            self.log(f"  重填失败（不致命，继续等）: {str(e)[:80]}")
                    elif email_visible and not pwd_visible:
                        # Verify your account 面板（launch code 输入面板）→ 这就是验证码阶段
                        self.log("  检测到 Verify your account 面板（launch code 输入框）→ 进入收码阶段")
                        on_verify = True
                        break
                if not on_verify:
                    # 超时也要抢救信任 cookie（本 IDataDome 已发过 cookie，下轮同 IP 直通）
                    try:
                        _save_trust_cookie(browser, _exit_ip_now, self.log)
                    except Exception:
                        pass
                    raise RuntimeError("等待验证码页超时（puzzle 未完成或流程卡住）")
                self.log("已进入验证码阶段")

                # ── 阶段3：收 launch code（双通道）──
                # 通道A：邮箱后端 otp_callback（outlook 池等，Graph API 收码）
                # 通道B：ccwu 网页邮局（同会话新标签登录收码）
                code = None
                if otp_callback is not None:
                    self.log("收码通道: 邮箱后端 (otp_callback)")
                    try:
                        code = otp_callback()
                    except Exception as e:
                        self.log(f"邮箱后端收码异常: {str(e)[:100]}")

                if not code:
                    if not mail_password:
                        raise RuntimeError("无可用收码通道（无 otp_callback 且无 ccwu 密码）")
                    if not email.lower().endswith("@2e-mail.ccwu.cc"):
                        # ccwu API 只管 ccwu 域邮箱；outlook 池邮箱在 ccwu 上无账号
                        raise RuntimeError(f"邮箱 {email} 非 ccwu 域且邮箱后端收码失败，无兜底通道可用")
                    self.log("收码通道: ccwu API（用户自建邮局，无风控，秒级轮询）")
                    code = ccwu_api_fetch_code(email, mail_password,
                                               log_fn=self.log, timeout=code_timeout)
                    if code:
                        self.log(f"launch code: {code}")

                # ── 阶段4：回 GitHub 填码 ──
                if not code:
                    raise RuntimeError("验证码收取超时（检查 ccwu 邮局登录/收件）")
                page.bring_to_front()
                box = page.locator('input[autocomplete="one-time-code"]:visible, '
                                   'input[inputmode="numeric"]:visible, input[name="otp"]:visible')
                if box.count() == 0:
                    box = page.locator("input:visible")
                # 8 格验证码控件：fill 整串只会全进第 1 格——必须逐格填（每格一位）
                # 或逐字符键入触发 input 事件自动跳格
                if box.count() >= 2:
                    for i, ch in enumerate(code[:8]):
                        box.nth(i).fill(ch, timeout=5000)
                        time.sleep(0.3)
                else:
                    box.first.press_sequentially(code, delay=80)
                time.sleep(6)
                result["status"] = "registered"
                # 成功即存全会话 cookie（含 __Host-user_session_same_site 等登录态），
                # 供台账 token 口径与后续协议线复用；datadome 信任 cookie 另由 _save_trust_cookie 存。
                try:
                    _sess_file = r"D:\\AI\\gh_runtime\\gh_session_%s.json" % email.split("@")[0]
                    with open(_sess_file, "w", encoding="utf-8") as _f:
                        json.dump({"email": email, "cookies": browser.cookies()},
                                  _f, ensure_ascii=False)
                    self.log(f"  session cookies 已存 → {_sess_file}")
                except Exception as _e:
                    self.log(f"  session cookies 存档失败: {str(_e)[:80]}")
                self.log(f"填码完成，最终 URL: {page.url[:100]}")
                # 窗口保持 60s 供人工确认最终状态
                time.sleep(60)
            finally:
                # close 前存 DataDome 信任 cookie（close 后 context 失效拿不到 cookies()）
                try:
                    _save_trust_cookie(browser, _exit_ip_now, self.log)
                except Exception:
                    pass
                _capture_datadome_params(browser, page, _exit_ip_now, self.log)
                try:
                    if _env is not None:
                        _env["close"]()
                    else:
                        browser.close()
                except Exception:
                    pass
        return result
