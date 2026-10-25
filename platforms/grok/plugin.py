"""Grok (x.ai) 平台插件——xAI 浏览器注册 + 本地 PKCE（产出 OAuth tokens）。"""

import time

from typing import Optional

from core.base_platform import BasePlatform, Account, AccountStatus, RegisterConfig
from core.base_mailbox import BaseMailbox
from core.registry import register


# xAI 验证码格式：ABC-DEF（6 位，连字符分隔）
XAI_CODE_PATTERN = r"[A-Z0-9]{3}-[A-Z0-9]{3}"


def _turnstile_backoff(retry_index: int) -> float:
    """Turnstile 拒绝后的第 retry_index 次重试等待秒数（越界取最后一档）。"""
    from platforms.grok.xai_browser import TURNSTILE_RETRY_BACKOFF_SEC

    table = TURNSTILE_RETRY_BACKOFF_SEC
    if not table:
        return 0.0
    idx = max(0, int(retry_index))
    return float(table[idx] if idx < len(table) else table[-1])


def _sleep_interruptible(platform: "GrokPlatform", seconds: float) -> None:
    """可中断退避睡眠：接入任务控制器的 stop/skip 检查点。

    退避最长 90s，若不接入 checkpoint，用户点停止要等满 90s 才生效。
    无任务控制（手动调用）时退化为整段睡眠。0.25s 粒度与 api/tasks.py
    _sleep_with_control 一致。
    """
    control = getattr(platform, "_task_control", None)
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        if control is not None:
            control.checkpoint(
                attempt_id=getattr(platform, "_task_attempt_token", None)
            )
        chunk = min(0.25, remaining)
        time.sleep(chunk)
        remaining -= chunk


@register
class GrokPlatform(BasePlatform):
    name = "grok"
    display_name = "Grok"
    version = "2.0.0"

    def __init__(
        self,
        config: Optional[RegisterConfig] = None,
        mailbox: Optional[BaseMailbox] = None,
    ):
        super().__init__(config or RegisterConfig())
        self.mailbox = mailbox

    def register(self, email: str, password: Optional[str] = None) -> Account:
        from platforms.grok.core import GrokRegister
        from platforms.grok.xai_browser import XaiTurnstileRejected
        from core.config_store import config_store

        log = getattr(self, "_log_fn", print)

        # 兼容旧配置键，新链路仅浏览器内置处置 Turnstile，solver 留作扩展点
        yescaptcha_key = self.config.extra.get("yescaptcha_key") or config_store.get(
            "yescaptcha_key", ""
        )
        requested_headless = (self.config.executor_type or "protocol") != "headed"
        reg = GrokRegister(
            captcha_solver=self._make_captcha(key=yescaptcha_key) if yescaptcha_key else None,
            yescaptcha_key=yescaptcha_key,
            proxy=self.config.proxy,
            log_fn=log,
            headless=requested_headless,
        )
        mailbox_attempts = (
            1 if email else int(self.config.extra.get("grok_mailbox_attempts", 8))
        )
        otp_timeout = self.get_mailbox_otp_timeout()
        last_error = None

        for attempt in range(1, mailbox_attempts + 1):
            mail_acct = None
            current_email = email
            if self.mailbox and not current_email:
                mail_acct = self.mailbox.get_email(platform="grok")
                current_email = mail_acct.email if mail_acct else None
            log(f"邮箱: {current_email}")
            before_ids = (
                self.mailbox.get_current_ids(mail_acct)
                if (self.mailbox and mail_acct)
                else set()
            )

            def otp_cb():
                log("等待验证码...")
                if not self.mailbox or not mail_acct:
                    return ""
                code = self.mailbox.wait_for_code(
                    mail_acct,
                    keyword="",
                    timeout=otp_timeout,
                    before_ids=before_ids,
                    code_pattern=XAI_CODE_PATTERN,
                )
                if code:
                    code = code.replace("-", "").replace(" ", "")
                    log(f"验证码: {code}")
                return code

            try:
                if not current_email:
                    raise RuntimeError("未获取到可用邮箱")
                result = reg.register(
                    email=current_email,
                    password=password,
                    otp_callback=otp_cb if self.mailbox else None,
                )
                # 注册成功：归还邮箱（账号保留，标记 grok 平台已成功）
                if self.mailbox and mail_acct is not None:
                    try:
                        self.mailbox.requeue_account(
                            mail_acct, platform="grok", failed=False
                        )
                    except Exception:
                        pass
                break
            except XaiTurnstileRejected as e:
                # Turnstile 拒绝 = CF 侧挑战未过（非代码回归，账号未注册）。
                # 处置（10-03 18:14 批次实测 6/10 教训：同出口高频后 CF 必败，
                # 同指纹硬撞无意义）：换全新会话（GrokRegister.register 内部每次
                # 起独立线程+新 Camoufox=新指纹）+ 换邮箱 + 指数退避后重试。
                last_error = e
                msg = str(e)
                if self.mailbox and mail_acct is not None:
                    try:
                        self.mailbox.requeue_account(
                            mail_acct, platform="grok", failed=True
                        )
                    except Exception:
                        pass
                if attempt < mailbox_attempts:
                    backoff = _turnstile_backoff(attempt - 1)
                    log(
                        "Grok 人机验证未通过（headless），换新会话+新邮箱 "
                        f"{backoff:.0f}s 后重试 {attempt + 1}/{mailbox_attempts}"
                    )
                    _sleep_interruptible(self, backoff)
                    continue
                raise
            except Exception as e:
                last_error = e
                msg = str(e)
                # 失败时归还邮箱并记录 grok 平台状态（24h 冷却 / 失败计数）
                if self.mailbox and mail_acct is not None:
                    try:
                        self.mailbox.requeue_account(
                            mail_acct, platform="grok", failed=True
                        )
                    except Exception:
                        pass
                if attempt < mailbox_attempts and "邮箱域名被拒绝" in msg:
                    log(
                        f"Grok 邮箱域名被拒绝，切换新邮箱重试 {attempt + 1}/{mailbox_attempts}"
                    )
                    continue
                raise
        else:
            raise last_error if last_error else RuntimeError("Grok 注册失败")

        tokens = result.get("tokens") or {}
        if not tokens.get("access_token") or not tokens.get("refresh_token"):
            raise RuntimeError(
                "Grok 注册链路未产出完整 tokens（缺少 access_token/refresh_token）"
            )

        return Account(
            platform="grok",
            email=result["email"],
            password=result["password"],
            status=AccountStatus.REGISTERED,
            extra={
                "tokens": tokens,
                "given_name": result.get("given_name", ""),
                "family_name": result.get("family_name", ""),
                "auth_path": "xai_browser_pkce",
            },
        )

    def check_valid(self, account: Account) -> bool:
        tokens = (account.extra or {}).get("tokens") or {}
        return bool(tokens.get("access_token") and tokens.get("refresh_token"))

    def get_platform_actions(self) -> list:
        return [
            {"id": "upload_9router", "label": "推送 9Router (grok-cli)", "params": []},
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id == "upload_9router":
            from core.nr_client import NRClient, canonical_from_account

            cli = NRClient()
            item = canonical_from_account("grok-cli", account)
            if not item.get("access_token"):
                return {"ok": False, "error": "账号 extra.tokens 无 access_token"}
            res = cli.bulk_import("grok-cli", [item])
            r0 = (res.get("results") or [{}])[0]
            if r0.get("ok"):
                return {"ok": True, "data": {"message": f"9r bulk-import ok id={r0.get('id')}", "conn_id": r0.get("id")}}
            return {"ok": False, "error": f"9r bulk-import 失败: {r0.get('error')}"}
        raise NotImplementedError(f"未知操作: {action_id}")
