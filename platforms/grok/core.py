"""Grok (x.ai) 自动注册——xAI 浏览器注册 + 本地 PKCE 取 AT/RT。

链路（v2，替代旧 sso cookie 产出）：
1. Camoufox 打开 accounts.x.ai/sign-up，邮箱注册 + 收码 + 资料/密码 + Turnstile
2. 同一会话建立稳定 accounts.x.ai 账户登录态
3. 本地生成 PKCE（S256），浏览器完成 auth.x.ai 授权、捕获回调授权码
4. 本地兑换 access_token / refresh_token（curl_cffi TLS 指纹直连 auth.x.ai）
5. 返回 Account(extra={"tokens": {...}})，对齐 nr_client.canonical_from_account
   （9Router grok-cli bulk-import 消费）；不再产出 sso/sso-rw cookie 凭证。

token 全程不落日志（只打印前缀/长度）；PKCE verifier 用后即抹。
"""

from __future__ import annotations

import random
import secrets
import string
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from core.browser_runtime import (
    ensure_browser_display_available,
    resolve_browser_headless,
)


# 账号资料与密码生成 -----------------------------------------------------------

def _rand_name(n: int = 6) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n)).capitalize()


def _rand_password(n: int = 12) -> str:
    return (
        "".join(random.choices(string.ascii_letters + string.digits, k=n)) + ",,,aA1"
    )


def _mask(value: str, keep: int = 4) -> str:
    """token 日志脱敏：只露前 4 位前缀与长度，绝不输出完整凭证。

    蓝本 progrok 用 20 位前缀；此处收紧到 4 位 + 长度，防日志聚合侧撞库。
    """
    value = str(value or "")
    return f"{value[:keep]}...<len={len(value)}>" if value else ""


class GrokRegister:
    """xAI 浏览器注册 + 本地 PKCE 凭证获取编排器。"""

    def __init__(
        self,
        captcha_solver=None,
        yescaptcha_key: str = "",
        proxy=None,
        log_fn=print,
        headless: bool = False,
    ):
        self.captcha_solver = captcha_solver
        self.key = yescaptcha_key
        self.proxy = proxy or ""
        self.headless = headless

        # GBK 安全包装：Windows 控制台打印非 GBK 字符会抛 UnicodeEncodeError
        def _safe_log(text):
            try:
                return str(text).encode("gbk", errors="replace").decode("gbk")
            except Exception:
                return str(text).encode("ascii", errors="replace").decode("ascii")

        self.log = lambda s: log_fn(_safe_log(s))

    # ---------- 主流程 ----------

    def register(
        self,
        email: str,
        password: Optional[str] = None,
        otp_callback: Optional[Callable[[], str]] = None,
    ) -> dict:
        """注册 xAI 账号并本地兑换 OAuth token。

        批量任务经 api/tasks.py 的 ThreadPoolExecutor(max_workers=1) 串行跑，
        线程会被复用；Playwright sync API 的绿色体状态在首个 Camoufox 会话
        close() 后会污染该线程（'Sync API inside the asyncio loop'，10-03 批量
        实测第 2/3 号必炸、独立复现坐实）。因此整个浏览器流程固定放进一次性
        新线程执行，用完即弃。异常照常上抛（不吞），由上层决定重试策略。
        """
        box: dict[str, Any] = {}

        def _target() -> None:
            try:
                box["result"] = self._register_flow(email=email, password=password, otp_callback=otp_callback)
            except BaseException as exc:  # noqa: BLE001 — 原样搬运给调用线程
                box["error"] = exc

        th = threading.Thread(target=_target, daemon=True, name=f"grok-reg-{int(time.time())}")
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
        """原 register 主体（在一次性线程内运行）。

        返回 dict：email/password/given_name/family_name +
        tokens{access_token, refresh_token, id_token, expires_at, ...}。
        """
        from platforms.grok.xai_browser import (
            XaiBrowserSession,
            XaiOAuthAccessDenied,
            register_xai_account,
        )
        from platforms.grok.xai_pkce import (
            authorize_and_exchange,
            http_timeout,
            token_to_auth_payload,
        )

        password = password or _rand_password()
        given_name = _rand_name()
        family_name = _rand_name()

        headless, reason = resolve_browser_headless(
            self.headless, default_headless=False
        )
        ensure_browser_display_available(headless)
        self.log(f"浏览器模式: {'headless' if headless else 'headed'} ({reason})")

        session: Optional[XaiBrowserSession] = None
        try:
            # (1) 浏览器注册至登录落地（会话保留登录态供 OAuth 复用）
            self.log(f"Step1: 启动 xAI 浏览器注册 email={email} ...")
            result, session = register_xai_account(
                email=email,
                password=password,
                get_verification_code=self._make_code_getter(otp_callback),
                proxy=self.proxy,
                headless=headless,
                timeout_sec=420.0,
                on_progress=lambda msg: self.log(f"  {msg}"),
            )
            self.log("  OK 注册完成，已建立 accounts.x.ai 登录会话")

            # (2) 本地 PKCE：会话生成 -> 浏览器授权 -> 兑换 AT/RT（授权被拒重试一次）
            self.log("Step2: 生成本地 PKCE 会话并发起 xAI OAuth 授权 ...")
            pkce_path = self._pkce_session_path(email)
            token: Optional[dict] = None
            for oauth_attempt in range(1, 3):
                try:
                    token = authorize_and_exchange(
                        session.authorize_pkce,
                        session_path=pkce_path,
                        token_proxy=self.proxy,
                        timeout=http_timeout(),
                    )
                    break
                except XaiOAuthAccessDenied:
                    if oauth_attempt >= 2:
                        raise
                    self.log("  OAuth 授权被拒，同步账户会话后用全新 PKCE 重试 ...")
                    session.prepare_pkce_retry()
            if (
                not token
                or not str(token.get("access_token") or "").strip()
                or not str(token.get("refresh_token") or "").strip()
            ):
                raise RuntimeError("本地 xAI PKCE 兑换未返回完整 AT/RT")

            payload = token_to_auth_payload(token, email=email)
            self.log(
                f"  OK AT={_mask(payload['access_token'])} "
                f"RT={_mask(payload['refresh_token'])} "
                f"expires_at={payload['expires_at']}"
            )
            return {
                "email": email,
                "password": password,
                "given_name": given_name,
                "family_name": family_name,
                "tokens": {
                    "access_token": payload["access_token"],
                    "refresh_token": payload["refresh_token"],
                    "id_token": payload["id_token"],
                    "expires_at": payload["expires_at"],
                    "scope": payload["scope"],
                    "token_type": payload["token_type"],
                    "oidc_issuer": payload["oidc_issuer"],
                    "oidc_client_id": payload["oidc_client_id"],
                },
            }
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            self.log(f"  FAIL Grok 注册链路失败: {detail.splitlines()[0][:300]}")
            raise
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass

    # ---------- 辅助 ----------

    @staticmethod
    def _make_code_getter(
        otp_callback: Optional[Callable[[], str]],
    ) -> Callable[..., Optional[str]]:
        """统一验证码回调签名：兼容 get_code(force: bool) 与 get_code()。

        xAI 重发验证码由浏览器侧点 Resend 按钮完成，邮箱侧只需再等新邮件。
        """

        def _get_code(force: bool = False) -> Optional[str]:
            del force
            if otp_callback is None:
                import builtins

                return builtins.input("验证码: ").strip()
            return otp_callback() or ""

        return _get_code

    @staticmethod
    def _pkce_session_path(email: str) -> Path:
        """PKCE 会话文件落点：<项目>/data/pkce_sessions/，verifier 用后即抹。"""
        root = Path(__file__).resolve().parent.parent.parent
        safe = "".join(
            ch if ch.isalnum() or ch in "@._-" else "_" for ch in (email or "")
        )[:64]
        path = (
            root
            / "data"
            / "pkce_sessions"
            / f"{int(time.time())}_{secrets.token_hex(4)}_{safe or 'anon'}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
