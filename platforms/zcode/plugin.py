"""ZCode (zcode.z.ai / chat.z.ai) 平台插件

- 邮箱：注册机内部邮箱机制（OutlookMailbox 等，platform="zcode" 调度）
- 图形化验证：协作模式（脚本点验证按钮 → 用户手动滑动滑块 → 脚本检测成功 → 自动提交）
- 执行器：Camoufox 可见窗口（playwright 过不了阿里云滑块，headless 固定 False）
"""

from typing import Optional

from core.base_mailbox import BaseMailbox, MailboxAccount
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registry import register
from platforms.zcode.core import VERIFY_URL_PATTERN, ZcodeRegister


@register
class ZcodePlatform(BasePlatform):
    name = "zcode"
    display_name = "ZCode"
    version = "1.0.0"

    def __init__(
        self,
        config: Optional[RegisterConfig] = None,
        mailbox: Optional[BaseMailbox] = None,
    ):
        super().__init__(config or RegisterConfig())
        self.mailbox = mailbox

    def register(self, email: str, password: Optional[str] = None) -> Account:
        from core.config_store import config_store

        log = getattr(self, "_log_fn", print)
        proxy = self.config.proxy or config_store.get("zcode_proxy", "") or None
        reg = ZcodeRegister(proxy=proxy, log_fn=log)

        mail_acct: Optional[MailboxAccount] = None
        current_email = email
        if self.mailbox and not current_email:
            mail_acct = self.mailbox.get_email(platform="zcode")
            current_email = mail_acct.email if mail_acct else None
        if not current_email:
            raise RuntimeError("未获取到可用邮箱（zcode 依赖注册机内部邮箱池）")
        log(f"[zcode] 使用邮箱: {current_email}")

        before_ids = (
            self.mailbox.get_current_ids(mail_acct) if (self.mailbox and mail_acct) else set()
        )
        otp_timeout = self.get_mailbox_otp_timeout()

        def verify_url_callback() -> str:
            """从内部邮箱收 Z.ai 确认邮件，提取 Verify Email 链接"""
            if not self.mailbox or not mail_acct:
                return ""
            log("[zcode] 等待 Z.ai 邮箱确认链接（内部邮箱机制收信）...")
            return self.mailbox.wait_for_code(
                mail_acct,
                keyword="z.ai",
                timeout=otp_timeout,
                before_ids=before_ids,
                code_pattern=VERIFY_URL_PATTERN,
            )

        try:
            result = reg.register(
                email=current_email,
                password=password,
                verify_url_callback=verify_url_callback,
            )
        except Exception:
            # 失败归还邮箱（24h 冷却 / 失败计数走内部机制）
            # 注意：requeue_account 只有 OutlookMailbox 实现，CFWorker 系邮箱
            # 无此方法（静默跳过——平台失败计数不落库，不影响注册流程）
            if self.mailbox and mail_acct is not None:
                try:
                    requeue = getattr(self.mailbox, "requeue_account", None)
                    if requeue:
                        requeue(mail_acct, platform="zcode", failed=True)
                except Exception:
                    pass
            raise

        # 成功归还邮箱（账号保留，标记 zcode 平台已成功，可给其他平台用）
        if self.mailbox and mail_acct is not None:
            try:
                requeue = getattr(self.mailbox, "requeue_account", None)
                if requeue:
                    requeue(mail_acct, platform="zcode", failed=False)
            except Exception:
                pass

        return Account(
            platform="zcode",
            email=result["email"],
            password=result["password"],
            token=result.get("token", ""),
            status=AccountStatus.REGISTERED,
            extra={
                "token": result.get("token", ""),
                "username": result.get("username", ""),
                "base": "https://chat.z.ai",
            },
        )

    def check_valid(self, account: Account) -> bool:
        return bool((account.extra or {}).get("token") or account.token)

    def get_platform_actions(self) -> list:
        return [
            {"id": "upload_zcode_to_a2", "label": "导入 A2 (zcode)", "params": []},
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id == "upload_zcode_to_a2":
            from platforms.zcode.a2_upload import upload_zcode_to_a2

            ok, msg = upload_zcode_to_a2(account.__dict__)
            return {"ok": ok, "data": {"message": msg}}
        raise NotImplementedError(f"未知操作: {action_id}")
