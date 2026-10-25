"""Tavily 注册协议核心实现 (Auth0 流程)"""
import re, json, secrets, hashlib, base64, urllib.parse
from typing import Optional, Callable

AUTH0_CLIENT_ID   = "RRIAvvXNFxpfTWIozX1mXqLnyUmYSTrQ"
AUTH0_BASE        = "https://auth.tavily.com"
APP_BASE          = "https://app.tavily.com"
REDIRECT_URI      = "https://app.tavily.com/api/auth/callback"
TURNSTILE_SITEKEY = "0x4AAAAAAAQFNSW6xordsuIq"


class TavilyRegister:
    def __init__(self, executor, captcha, log_fn: Callable = print):
        self.ex = executor
        self.captcha = captcha
        self.log = log_fn

    @staticmethod
    def _pick_state(resp, fallback: str) -> str:
        """从响应里抽 state：优先 Location 头（302），退回 HTML 隐藏域 name="state"。

        Auth0 对 /authorize 有时直接 200 渲染登录页（不 302），state 只存在于 HTML
        隐藏 input——只看 Location 会拿到空值并用回本地自造 state，导致下一步 400。
        """
        loc = resp.headers.get("location", "") or ""
        m = re.search(r'[?&]state=([^&]+)', loc)
        if m:
            return urllib.parse.unquote(m.group(1))
        body = getattr(resp, "text", "") or ""
        m2 = re.search(r'name="state"\s+value="([^"]+)"', body)
        if m2:
            return m2.group(1)
        return fallback

    def step1_authorize(self) -> str:
        """GET /authorize → 返回 state"""
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(43)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b'=').decode()
        state_val = base64.urlsafe_b64encode(
            json.dumps({"returnTo": f"{APP_BASE}/home"}).encode()
        ).rstrip(b'=').decode()
        params = {
            "client_id": AUTH0_CLIENT_ID, "scope": "openid profile email",
            "response_type": "code", "redirect_uri": REDIRECT_URI,
            "nonce": nonce, "state": state_val,
            "screen_hint": "signup", "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        r = self.ex.get(f"{AUTH0_BASE}/authorize", params=params)
        return self._pick_state(r, state_val)

    def step2_solve_captcha(self) -> str:
        self.log("获取 Turnstile token...")
        token = self.captcha.solve_turnstile(AUTH0_BASE, TURNSTILE_SITEKEY)
        self.log("Turnstile OK")
        return token

    def step3_submit_email(self, email: str, state: str, captcha_token: str) -> str:
        self.log(f"提交邮箱: {email}")
        r = self.ex.post(
            f"{AUTH0_BASE}/u/signup/identifier",
            params={"state": state},
            data={"state": state, "email": email, "captcha": captcha_token},
        )
        loc = r.headers.get("location", "")
        m = re.search(r'[?&]state=([^&]+)', loc)
        return urllib.parse.unquote(m.group(1)) if m else state

    def step4_submit_otp(self, otp: str, challenge_state: str) -> str:
        self.log("提交验证码...")
        r = self.ex.post(
            f"{AUTH0_BASE}/u/email-identifier/challenge",
            params={"state": challenge_state},
            data={"state": challenge_state, "code": otp},
        )
        loc = r.headers.get("location", "")
        m = re.search(r'[?&]state=([^&]+)', loc)
        return urllib.parse.unquote(m.group(1)) if m else challenge_state

    def step5_submit_password(self, email: str, password: str, pw_state: str) -> str:
        self.log("设置密码...")
        r = self.ex.post(
            f"{AUTH0_BASE}/u/signup/password",
            params={"state": pw_state},
            data={"state": pw_state, "email": email, "password": password,
                  "passwordPolicy.isFlexible": "false",
                  "strengthPolicy": "good", "complexityOptions.minLength": "8"},
        )
        loc = r.headers.get("location", "")
        m = re.search(r'[?&]state=([^&]+)', loc)
        return urllib.parse.unquote(m.group(1)) if m else pw_state

    def step6_resume_and_get_key(self, resume_state: str) -> str:
        self.log("完成授权流程...")
        self.ex.get(f"{AUTH0_BASE}/authorize/resume", params={"state": resume_state})
        r = self.ex.get(f"{APP_BASE}/api/keys", headers={"accept": "application/json"})
        try:
            keys = r.json()
            if keys and isinstance(keys, list):
                return keys[0].get("key", "")
        except Exception:
            pass
        return ""

    def register(self, email: str, password: str,
                 otp_callback: Optional[Callable] = None) -> dict:
        state = self.step1_authorize()
        captcha_token = self.step2_solve_captcha()
        challenge_state = self.step3_submit_email(email, state, captcha_token)
        otp = otp_callback() if otp_callback else input("OTP: ")
        if not otp:
            raise RuntimeError("未获取到验证码")
        self.log(f"验证码: {otp}")
        pw_state = self.step4_submit_otp(otp, challenge_state)
        resume_state = self.step5_submit_password(email, password, pw_state)
        api_key = self.step6_resume_and_get_key(resume_state)
        self.log(f"API Key: {api_key[:20]}..." if api_key else "未获取到 API Key")
        return {"email": email, "password": password, "api_key": api_key}
