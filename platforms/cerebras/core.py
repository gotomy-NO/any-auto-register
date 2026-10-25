# -*- coding: utf-8 -*-
"""Cerebras Cloud 注册核心（clean-room，2026-10-06 实测）。

上游 lxf746 的 cerebras 走 Stytch OTP（`web.stytch.com/sdk/v1/otps/...`）——**该线已失效**
（实测 400 bad_app_bundle / 500）。Cerebras 现已迁移到 **Auth.js(v5) 邮箱 magic-link**：

    1. GET  /api/auth/csrf                -> csrfToken（配 __Host-authjs.csrf-token cookie）
    2. POST /api/auth/signin/nodemailer   -> 触发发送登录邮件（须过 Cloudflare + reCAPTCHA，
                                              即纯协议会被拒 AccessDenied）
    3. 邮件里 `https://cloud.cerebras.ai/auth/magic-link?... #token=<hex>`
    4. GET  /api/auth/callback/nodemailer?token=&email= -> 落 authjs.session-token 会话
    5. POST /api/graphql ListOrganizationApiKeys -> 新号自带 `Default Key`（secretKey = csk-...）

因此本实现为**浏览器线**（Camoufox，反指纹，能过 Cloudflare + 隐形 reCAPTCHA），
纯协议线不可用（Cloudflare cf_clearance + reCAPTCHA recaptchaToken 是硬门槛）。

纪律：api_key/session 只做前缀+长度掩码，绝不整串落日志。
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional

CLOUD_BASE = "https://cloud.cerebras.ai"
API_BASE = "https://api.cerebras.ai/v1"

# Stytch 遗留常量（仅存档，已失效，勿用）
LEGACY_STYTCH_PUBLIC_TOKEN = "public-token-live-149c2fe0-f8dc-4569-8e3e-e04161a8475e"

_LIST_KEYS_QUERY = """query ListOrganizationApiKeys($organizationId: ID!, $projectId: ID) {
  ListOrganizationApiKeys(organizationId: $organizationId, projectId: $projectId) {
    id name secretKey secretKeyPrefix projectId projectName state
  }
}"""


def mask(value: str, keep: int = 8) -> str:
    value = str(value or "")
    return f"{value[:keep]}...<len={len(value)}>" if value else ""


def extract_magic_link(raw_mail: str) -> str:
    """从 SendGrid 邮件 raw 文本提取 magic-link（处理 quoted-printable 软换行）。"""
    try:
        import quopri
        raw_mail = quopri.decodestring(raw_mail.encode("utf-8", "ignore")).decode("utf-8", "ignore")
    except Exception:
        pass
    text = (raw_mail.replace("=\r\n", "").replace("=\n", "").replace("&amp;", "&"))
    cands = re.findall(r"https://cloud\.cerebras\.ai/auth/magic-link\?[^\"'\s<>]+", text)
    if not cands:
        cands = re.findall(r"https://cloud\.cerebras\.ai/api/auth/callback/nodemailer\?[^\"'\s<>]+", text)
    return max(cands, key=len) if cands else ""


class CerebrasRegisterError(RuntimeError):
    """Cerebras 注册失败（含阶段标记）。"""


class CerebrasAuthJSRegister:
    """Auth.js 邮箱 magic-link 注册编排器（浏览器线，Camoufox）。

    proxy: 形如 http://127.0.0.1:7897；None=直连。
    """

    def __init__(self, *, proxy: Optional[str] = None,
                 log_fn: Callable[[str], None] = print):
        self.log = log_fn
        self.proxy = proxy

    # ---- 浏览器线（在一次性线程内跑 sync Playwright/Camoufox） ----
    def _run_browser(self, email: str, link_provider: Callable[[], str]) -> dict:
        from camoufox.sync_api import Camoufox

        proxy_cfg = {"server": self.proxy} if self.proxy else None
        kw = {"headless": True, "os": "windows"}
        if proxy_cfg:
            kw["proxy"] = proxy_cfg
        with Camoufox(**kw) as browser:
            page = browser.new_page()
            page.goto(f"{CLOUD_BASE}/login", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector("input[type=email]", timeout=45000)
            try:
                page.click("button:has-text('Reject All')", timeout=3000)
            except Exception:
                pass
            page.fill("input[type=email]", email, timeout=20000)
            try:
                page.click("button:has-text('Continue with email')", timeout=6000)
            except Exception:
                page.click("button[type=submit]", timeout=6000)
            import time
            time.sleep(8)
            body = page.evaluate("()=>document.body.innerText.slice(0,120)")
            if "Check your email" not in body:
                raise CerebrasRegisterError(f"[signin] 未进入 Check your email: {body!r}")
            self.log("登录邮件已发送，等待 magic-link ...")

            link = link_provider()
            if not link:
                raise CerebrasRegisterError("[magic-link] 未从邮箱取到登录链接")
            mt = re.search(r"#token=([0-9a-fA-F]+)", link)
            token = mt.group(1) if mt else ""
            from urllib.parse import quote
            cb = (f"{CLOUD_BASE}/api/auth/callback/nodemailer"
                  f"?callbackUrl={quote(CLOUD_BASE + '/login', safe='')}"
                  f"&email={quote(email, safe='')}&token={token}")
            page.goto(cb, wait_until="domcontentloaded", timeout=60000)
            import time as _t
            _t.sleep(6)
            sess = page.evaluate("()=>fetch('/api/auth/session').then(r=>r.json())")
            user = (sess or {}).get("user") or {}
            if not user:
                raise CerebrasRegisterError("[session] 未建立会话（magic-link 可能已过期）")
            self.log(f"会话已建立 user_id={user.get('id', '')}")

            # 结束 onboarding 详情步（best-effort，不影响取 key）
            try:
                page.goto(f"{CLOUD_BASE}/", wait_until="domcontentloaded", timeout=45000)
                _t.sleep(5)
                org_m = re.search(r"/platform/(org_[a-z0-9]+)/", page.url)
                org = org_m.group(1) if org_m else ""
                if org:
                    page.goto(f"{CLOUD_BASE}/platform/{org}/onboarding",
                              wait_until="domcontentloaded", timeout=45000)
                    _t.sleep(5)
                    ins = page.query_selector_all("input[type=text]")
                    if len(ins) >= 2:
                        ins[0].fill("Alex Carter")
                        ins[1].fill("Dev Labs")
                        page.click("button:has-text('Continue')", timeout=6000)
                        _t.sleep(5)
            except Exception as e:
                self.log(f"onboarding 详情步跳过: {str(e)[:100]}")

            if not org:
                page.goto(f"{CLOUD_BASE}/", wait_until="domcontentloaded", timeout=45000)
                _t.sleep(5)
                org_m = re.search(r"/platform/(org_[a-z0-9]+)/", page.url)
                org = org_m.group(1) if org_m else ""

            js = """async ({org,q,v}) => {
                const r = await fetch('/api/graphql', {method:'POST',
                  headers:{'content-type':'application/json'},
                  body: JSON.stringify({operationName:'ListOrganizationApiKeys', query:q, variables:v})});
                return await r.text();
            }"""
            raw = page.evaluate(js, {"org": org, "q": _LIST_KEYS_QUERY,
                                     "v": {"organizationId": org, "projectId": None}})
            api_key = ""
            try:
                arr = (json.loads(raw).get("data") or {}).get("ListOrganizationApiKeys") or []
                if arr:
                    api_key = str(arr[0].get("secretKey") or "")
            except Exception:
                pass
            if not api_key:
                raise CerebrasRegisterError(f"[api-keys] 未取到 key: {str(raw)[:200]}")
            self.log(f"API Key {mask(api_key)}（来源：新号自带 Default Key）")
            return {"email": email, "password": "", "user_id": user.get("id", ""),
                    "org": org, "api_key": api_key}

    def register(self, *, email: str, link_provider: Callable[[], str]) -> dict:
        """线程隔离跑 sync 浏览器栈（防 Playwright sync 污染复用线程）。"""
        import threading
        box: dict = {}

        def _worker():
            try:
                box["result"] = self._run_browser(email, link_provider)
            except Exception as e:  # noqa: BLE001
                box["error"] = e

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join()
        if "error" in box:
            raise box["error"]
        return box.get("result") or {}
