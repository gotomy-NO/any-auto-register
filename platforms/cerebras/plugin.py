# -*- coding: utf-8 -*-
"""Cerebras Cloud 平台插件（clean-room 重写，浏览器线 Auth.js magic-link）。"""
from __future__ import annotations

import time
from typing import Optional

from core.base_platform import BasePlatform, Account, AccountStatus, RegisterConfig
from core.base_mailbox import BaseMailbox
from core.registry import register


@register
class CerebrasPlatform(BasePlatform):
    name = "cerebras"
    display_name = "Cerebras"
    version = "1.0.0"
    # 纯协议线实测被 Cloudflare+reCAPTCHA 拒（AccessDenied），必须走浏览器；
    # 仍列 protocol 以免任务默认 executor_type=protocol 时构造即抛错，内部统一走浏览器线。
    supported_executors = ["protocol", "headless", "headed"]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _link_provider(self, mail_acct, before_ids, timeout: int):
        """轮询邮箱取 magic-link URL（Cerebras 是链接式登录，不是 6 位码）。"""
        from platforms.cerebras.core import extract_magic_link

        deadline = time.time() + timeout
        while time.time() < deadline:
            if getattr(self, "_task_control", None) is not None:
                self._task_control.checkpoint(
                    attempt_id=getattr(self, "_task_attempt_token", None))
            items = []
            getter = getattr(self.mailbox, "_get_mails", None)
            if callable(getter):
                try:
                    items = getter(mail_acct.email) or []
                except Exception:
                    items = []
            for m in items:
                if str(m.get("id") or "") in before_ids:
                    continue
                link = extract_magic_link(str(m.get("raw") or m.get("text") or ""))
                if link:
                    return link
            time.sleep(4)
        return ""

    def register(self, email: str, password: Optional[str] = None) -> Account:
        from platforms.cerebras.core import CerebrasAuthJSRegister

        log = getattr(self, "_log_fn", print)
        mail_acct = None
        if self.mailbox and not email:
            mail_acct = self.mailbox.get_email()
            email = mail_acct.email if mail_acct else ""
        if not email:
            raise RuntimeError("Cerebras 注册未取得邮箱（mailbox 未返回且未显式传入）")
        log(f"邮箱: {email}")
        before = self.mailbox.get_current_ids(mail_acct) if (self.mailbox and mail_acct) else set()
        otp_timeout = self.get_mailbox_otp_timeout(default=150)

        def link_provider() -> str:
            log("等待登录邮件（magic-link）...")
            link = self._link_provider(mail_acct, before, otp_timeout)
            if link:
                log("已取到 magic-link")
            return link

        reg = CerebrasAuthJSRegister(proxy=self.config.proxy if self.config else None, log_fn=log)
        try:
            result = reg.register(email=email, link_provider=link_provider)
        except Exception:
            if self.mailbox and mail_acct is not None:
                try:
                    self.mailbox.requeue_account(mail_acct, platform="cerebras", failed=True)
                except Exception:
                    pass
            raise
        if self.mailbox and mail_acct is not None:
            try:
                self.mailbox.requeue_account(mail_acct, platform="cerebras", failed=False)
            except Exception:
                pass
        return Account(
            platform="cerebras",
            email=result["email"],
            password="",
            user_id=result.get("user_id", ""),
            status=AccountStatus.REGISTERED,
            extra={"api_key": result.get("api_key", ""), "org": result.get("org", ""),
                   "auth_path": "authjs_magiclink_browser"},
        )

    def check_valid(self, account: Account) -> bool:
        key = (account.extra or {}).get("api_key", "")
        if not key:
            return False
        try:
            from core.executors.protocol import ProtocolExecutor
            with ProtocolExecutor(proxy=self.config.proxy if self.config else None) as ex:
                r = ex.get("https://api.cerebras.ai/v1/models",
                           headers={"authorization": f"Bearer {key}", "accept": "application/json"})
                return r.status_code == 200
        except Exception:
            return False

    def get_platform_actions(self) -> list:
        return [{"id": "get_account_state", "label": "查询账号状态", "params": []}]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id == "get_account_state":
            from platforms.cerebras.core import mask
            key = (account.extra or {}).get("api_key", "")
            return {"ok": True, "data": {"valid": self.check_valid(account),
                                         "api_key_preview": mask(key)}}
        raise NotImplementedError(f"未知操作: {action_id}")
