"""Kiro 平台插件 - 基于 AWS Builder ID 注册"""

from typing import Optional

from core.base_platform import BasePlatform, Account, AccountStatus, RegisterConfig
from core.base_mailbox import BaseMailbox
from core.registry import register


@register
class KiroPlatform(BasePlatform):
    name = "kiro"
    display_name = "Kiro (AWS Builder ID)"
    version = "1.0.0"

    def __init__(
        self,
        config: Optional[RegisterConfig] = None,
        mailbox: Optional[BaseMailbox] = None,
    ):
        super().__init__(config or RegisterConfig())
        self.mailbox = mailbox

    def register(self, email: str, password: Optional[str] = None) -> Account:
        # 纯协议线（step 化，见 platforms/kiro/protocol_v10.py）。
        # 默认关闭：需任务 extra 里显式 kiro_protocol=true，才走协议线；
        # 现状（2026-10-06 实测）会被 AWS TES 挡在 send-otp（400 BLOCKED），
        # 故不设为默认执行路径。
        if self.config.extra.get("kiro_protocol"):
            return self._register_via_protocol(email, password)

        from platforms.kiro.core import KiroRegister

        proxy = self.config.proxy
        laoudo_account_id = self.config.extra.get("laoudo_account_id", "")
        requested_headless = (self.config.executor_type or "protocol") != "headed"

        reg = KiroRegister(proxy=proxy, tag="KIRO", headless=requested_headless)
        log_fn = getattr(self, "_log_fn", print)
        reg.log_fn = log_fn

        otp_timeout = int(self.config.extra.get("otp_timeout", 120))

        if self.mailbox:
            mailbox = self.mailbox
            mail_acct = mailbox.get_email(platform="kiro")
            if not mail_acct:
                raise RuntimeError("未获取到可用邮箱账号")
            email = email or mail_acct.email
            log_fn(f"邮箱: {mail_acct.email}")
            _before = mailbox.get_current_ids(mail_acct)

            def otp_cb():
                log_fn("等待验证码...")
                code = mailbox.wait_for_code(
                    mail_acct,
                    keyword="builder id",
                    timeout=otp_timeout,
                    before_ids=_before,
                    code_pattern=r"(?is)(?:verification\s+code|验证码)[^0-9]{0,20}(\d{6})",
                )
                if code:
                    log_fn(f"验证码: {code}")
                return code
        else:
            otp_cb = None

        ok, info = reg.register(
            email=email,
            pwd=password,
            name=self.config.extra.get("name", "Kiro User"),
            mail_token=laoudo_account_id or None,
            otp_timeout=otp_timeout,
            otp_callback=otp_cb,
        )

        if not ok:
            # 失败时归还邮箱并记录 kiro 平台状态（24h 冷却 / 失败计数）
            if self.mailbox and mail_acct is not None:
                try:
                    self.mailbox.requeue_account(
                        mail_acct, platform="kiro", failed=True
                    )
                except Exception:
                    pass
            raise RuntimeError(f"Kiro 注册失败: {info.get('error')}")

        # 注册成功：归还邮箱（账号保留，标记 kiro 平台已成功，可给其他平台用）
        if self.mailbox and mail_acct is not None:
            try:
                self.mailbox.requeue_account(
                    mail_acct, platform="kiro", failed=False
                )
            except Exception:
                pass

        return Account(
            platform="kiro",
            email=info["email"],
            password=info["password"],
            status=AccountStatus.REGISTERED,
            extra={
                "name": info.get("name", ""),
                "accessToken": info.get("accessToken", ""),
                "sessionToken": info.get("sessionToken", ""),
                "clientId": info.get("clientId", ""),
                "clientSecret": info.get("clientSecret", ""),
                "clientIdHash": info.get("clientIdHash", ""),
                "refreshToken": info.get("refreshToken", ""),
                "webAccessToken": info.get("webAccessToken", ""),
                "region": info.get("region", "us-east-1"),
                "provider": "BuilderId",
                "authMethod": "IdC",
            },
        )

    def _register_via_protocol(self, email: str, password: Optional[str] = None) -> Account:
        """纯协议链路（platforms/kiro/protocol_v10.py）——需 extra.kiro_protocol=true 开启。"""
        from platforms.kiro.protocol_v10 import KiroProtocolFlow, mailbox_otp_provider

        log_fn = getattr(self, "_log_fn", print)
        flow = KiroProtocolFlow(proxy=self.config.proxy, tag="KIRO",
                                log=log_fn, captcha_enabled=bool(self.config.extra.get("captcha_enabled", True)))
        otp_provider = None
        mail_acct = None
        if self.mailbox:
            mail_acct = self.mailbox.get_email(platform="kiro")
            if mail_acct:
                email = email or mail_acct.email
                otp_provider = mailbox_otp_provider(
                    self.mailbox, mail_acct,
                    timeout=int(self.config.extra.get("otp_timeout", 150)))
        result = flow.run(email=email, password=password or "",
                          name=self.config.extra.get("name", "Kiro User"),
                          otp_provider=otp_provider)
        if self.mailbox and mail_acct is not None:
            try:
                self.mailbox.requeue_account(
                    mail_acct, platform="kiro", failed=bool(result.get("failed_step")))
            except Exception:
                pass
        if result.get("failed_step"):
            raise RuntimeError(
                f"Kiro 协议注册失败于 S{result['failed_step']} "
                f"({result.get('failed_step_name', '')})")
        return Account(
            platform="kiro", email=result.get("email", email),
            password=result.get("password", password or ""),
            status=AccountStatus.REGISTERED,
            extra={
                "name": result.get("name", ""),
                "accessToken": result.get("accessToken", ""),
                "sessionToken": result.get("sessionToken", ""),
                "clientId": result.get("clientId", ""),
                "clientSecret": result.get("clientSecret", ""),
                "refreshToken": result.get("refreshToken", ""),
                "region": "us-east-1", "provider": "BuilderId", "authMethod": "IdC",
                "register_path": "protocol_v10",
            },
        )

    def check_valid(self, account: Account) -> bool:
        """通过 refreshToken 检测账号是否有效"""
        extra = account.extra or {}
        refresh_token = extra.get("refreshToken", "")
        if not refresh_token:
            return False
        try:
            from platforms.kiro.switch import refresh_kiro_token

            ok, _ = refresh_kiro_token(
                refresh_token,
                extra.get("clientId", ""),
                extra.get("clientSecret", ""),
            )
            return ok
        except Exception:
            return False

    def get_platform_actions(self) -> list:
        return [
            {"id": "switch_account", "label": "切换到桌面应用", "params": []},
            {"id": "refresh_token", "label": "刷新 Token", "params": []},
            {"id": "upload_kiro_manager", "label": "导入 Kiro Manager", "params": []},
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        extra = account.extra or {}

        if action_id == "switch_account":
            from platforms.kiro.switch import (
                refresh_kiro_token,
                switch_kiro_account,
                restart_kiro_ide,
            )
            from platforms.kiro.core import KiroRegister
            from core.base_mailbox import create_mailbox, MailboxAccount

            access_token = extra.get("accessToken", "") or account.token
            refresh_token = extra.get("refreshToken", "")
            client_id = extra.get("clientId", "")
            client_secret = extra.get("clientSecret", "")

            # Kiro 桌面端需要完整的 Builder ID SSO 缓存。
            # 只有 accessToken/sessionToken 的网页态账号无法稳定切到桌面应用。
            if not access_token:
                return {
                    "ok": False,
                    "error": "当前账号缺少 accessToken，无法切换到桌面应用",
                }
            if not refresh_token or not client_id or not client_secret:
                if account.email and account.password:
                    reg = KiroRegister(proxy=self.config.proxy, tag="KIRO-SWITCH")
                    reg.log_fn = getattr(self, "_log_fn", print)
                    otp_callback = None
                    mailbox_extra = dict(self.config.extra or {})
                    for key in (
                        "mail_provider",
                        "luckmail_base_url",
                        "luckmail_project_code",
                        "luckmail_email_type",
                        "luckmail_domain",
                    ):
                        if extra.get(key) not in (None, ""):
                            mailbox_extra[key] = extra.get(key)

                    mail_provider = mailbox_extra.get("mail_provider", "")
                    if mail_provider:
                        try:
                            mailbox = create_mailbox(
                                provider=mail_provider,
                                extra=mailbox_extra,
                                proxy=self.config.proxy or "",
                            )
                            mail_account = MailboxAccount(
                                email=account.email,
                                account_id=extra.get("mailbox_token", ""),
                            )
                            before_ids = mailbox.get_current_ids(mail_account)

                            def _otp_cb():
                                reg.log("桌面授权等待邮箱验证码 ...")
                                try:
                                    code = mailbox.wait_for_code(
                                        mail_account,
                                        keyword="",
                                        timeout=45,
                                        before_ids=before_ids,
                                        code_pattern=r"(?is)(?:verification\s+code|验证码)[^0-9]{0,20}(\d{6})",
                                    )
                                except Exception:
                                    reg.log(
                                        "未等到新验证码，回退读取最近一封身份验证邮件 ..."
                                    )
                                    code = mailbox.wait_for_code(
                                        mail_account,
                                        keyword="",
                                        timeout=15,
                                        before_ids=set(),
                                        code_pattern=r"(?is)(?:verification\s+code|验证码)[^0-9]{0,20}(\d{6})",
                                    )
                                if code:
                                    reg.log(f"桌面授权验证码: {code}")
                                return code

                            otp_callback = _otp_cb
                        except Exception:
                            otp_callback = None

                    ok, desktop_info = reg.fetch_desktop_tokens(
                        account.email,
                        account.password,
                        otp_callback=otp_callback,
                    )
                    if not ok:
                        return {
                            "ok": False,
                            "error": (
                                "当前账号缺少 refreshToken / clientId / clientSecret，"
                                f"且自动补抓桌面端 Token 失败: {desktop_info.get('error', 'unknown error')}"
                            ),
                        }
                    access_token = desktop_info.get("accessToken", "") or access_token
                    refresh_token = desktop_info.get("refreshToken", "")
                    client_id = desktop_info.get("clientId", "")
                    client_secret = desktop_info.get("clientSecret", "")
                else:
                    return {
                        "ok": False,
                        "error": (
                            "当前账号只有网页登录态，缺少 refreshToken / clientId / clientSecret，"
                            "并且没有可用的邮箱/密码用于自动补抓桌面端 Token。"
                        ),
                    }

            if refresh_token and client_id and client_secret:
                ok, result = refresh_kiro_token(refresh_token, client_id, client_secret)
                if ok:
                    access_token = result["accessToken"]
                    refresh_token = result.get("refreshToken", refresh_token)

            ok, msg = switch_kiro_account(
                access_token=access_token,
                refresh_token=refresh_token,
                client_id=client_id,
                client_secret=client_secret,
            )
            if not ok:
                return {"ok": False, "error": msg}

            restart_ok, restart_msg = restart_kiro_ide()
            return {
                "ok": True,
                "data": {
                    "accessToken": access_token,
                    "refreshToken": refresh_token,
                    "clientId": client_id,
                    "clientSecret": client_secret,
                    "message": f"{msg}。{restart_msg}" if restart_ok else msg,
                },
            }

        elif action_id == "refresh_token":
            from platforms.kiro.switch import refresh_kiro_token

            refresh_token = extra.get("refreshToken", "")
            client_id = extra.get("clientId", "")
            client_secret = extra.get("clientSecret", "")

            ok, result = refresh_kiro_token(refresh_token, client_id, client_secret)
            if ok:
                new_access = result["accessToken"]
                new_refresh = result.get("refreshToken", refresh_token)
                return {
                    "ok": True,
                    "data": {
                        "access_token": new_access,
                        "accessToken": new_access,
                        "refreshToken": new_refresh,
                    },
                }
            return {"ok": False, "error": result.get("error", "刷新失败")}

        elif action_id == "upload_kiro_manager":
            from platforms.kiro.account_manager_upload import upload_to_kiro_manager

            ok, msg = upload_to_kiro_manager(account)
            return {"ok": ok, "data": {"message": msg}}

        raise NotImplementedError(f"未知操作: {action_id}")
