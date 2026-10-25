"""GitHub 平台插件（半自动注册：人工过 DataDome/puzzle，其余自动化）"""
import random
from typing import Optional

from core.base_platform import BasePlatform, Account, AccountStatus, RegisterConfig
from core.base_mailbox import BaseMailbox
from core.registry import register


@register
class GithubPlatform(BasePlatform):
    name = "github"
    display_name = "GitHub"
    version = "1.0.0"

    def __init__(self, config: Optional[RegisterConfig] = None,
                 mailbox: Optional[BaseMailbox] = None):
        super().__init__(config or RegisterConfig())
        self.mailbox = mailbox

    def register(self, email: str, password: str = None) -> Account:
        from platforms.github.core import GithubRegister
        log = getattr(self, "_log_fn", print)

        # 邮箱：优先外部指定，其次邮箱后端分配（ccwu/微软池均可收 GitHub 验证码）
        mail_acct = None
        if self.mailbox and not email:
            mail_acct = self.mailbox.get_email(platform="github")
            email = mail_acct.email if mail_acct else None
        if not email:
            raise RuntimeError("未提供邮箱且邮箱后端无可分配邮箱")
        log(f"邮箱: {email}")

        # ccwu 邮局密码：从邮箱账号取；取不到则要求在任务 extra 里带 mail_password
        mail_password = ""
        if mail_acct is not None:
            mail_password = getattr(mail_acct, "password", "") or ""
        if not mail_password:
            mail_password = (self.config.extra or {}).get("mail_password", "")

        gh_password = password or (self.config.extra or {}).get("gh_password") or "Gh!Reg" + str(random.randint(100000, 999999)) + "!x"

        # 收码 otp_callback：邮箱后端（outlook 池 Graph 收码等）；ccwu 邮箱走 core.py 的网页邮局通道
        otp_cb = None
        if self.mailbox and mail_acct is not None:
            before_ids = self.mailbox.get_current_ids(mail_acct)

            def otp_cb():
                log("等待验证码（邮箱后端）...")
                code = self.mailbox.wait_for_code(
                    mail_acct,
                    keyword="github",
                    timeout=int((self.config.extra or {}).get("code_timeout", 300)),
                    before_ids=before_ids,
                    code_pattern=r"\b(\d{8})\b",  # GitHub launch code 是 8 位（默认 pattern 是 6 位）
                )
                if code:
                    log(f"验证码: {code}")
                return code

        reg = GithubRegister(log_fn=log, proxy=self.config.proxy)
        result = reg.register(
            email=email,
            password=gh_password,
            mail_password=mail_password,
            otp_callback=otp_cb,
            manual_timeout=int((self.config.extra or {}).get("manual_timeout", 600)),
            code_timeout=int((self.config.extra or {}).get("code_timeout", 300)),
        )
        if result.get("status") != "registered":
            # 失败：归还邮箱并记 failed（24h 冷却/失败计数），防止坏邮箱被反复分配
            if self.mailbox and mail_acct is not None:
                try:
                    self.mailbox.requeue_account(
                        mail_acct, platform="github", failed=True
                    )
                except Exception:
                    pass
            raise RuntimeError(f"GitHub 注册未完成: {result.get('status')}")

        # ccwu 自有邮箱注册成功后归还（同 grok 逻辑，可复用给其他平台）
        if self.mailbox and mail_acct is not None:
            try:
                self.mailbox.requeue_account(mail_acct, platform="github", failed=False)
            except Exception:
                pass

        return Account(
            platform="github",
            email=result["email"],
            password=result["password"],
            status=AccountStatus.REGISTERED,
            extra={"username": result.get("username", ""), "reg_mode": "semi_auto"},
        )

    def check_valid(self, account: Account) -> bool:
        # GitHub 账号有效性：有用户名即视为注册成功（后续可加 api.github.com token 校验）
        return bool((account.extra or {}).get("username"))

    def get_platform_actions(self) -> list:
        return [
            {"id": "note", "label": "说明", "params": []},
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id == "note":
            return {"ok": True, "data": {"message": (
                "GitHub 半自动注册：DataDome 滑块与 puzzle 需人工在弹出窗口完成，"
                "填表/收码/填码自动。profile 沉淀 DataDome cookie 越用越好过。")}}
        raise NotImplementedError(f"未知操作: {action_id}")
