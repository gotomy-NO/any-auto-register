from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from core.config_store import config_store
from services.mail_imports import (
    MailImportExecuteRequest,
    MailImportSnapshotRequest,
    align_source_with_provider,
    mail_import_registry,
    normalize_mail_import_source,
)
from services.sms_service import SMS_DEFAULT_COUNTRY, SMS_DEFAULT_SERVICE

router = APIRouter(prefix="/config", tags=["config"])

CONFIG_KEYS = [
    # 201 fork 移植：网关连接(nr)可配置项 —— 产号自动推送 9Router 的接线参数
    "nr_base_url",
    "nr_panel_password",
    "email_domain_rule_enabled",
    "email_domain_level_count",
    "laoudo_auth",
    "laoudo_email",
    "laoudo_account_id",
    "yescaptcha_key",
    "twocaptcha_key",
    "default_executor",
    "default_captcha_solver",
    "payment_link_proxy",
    "payment_pay_proxy",
    "payment_proxy",
    "register_retry_times",
    "duckmail_api_url",
    "duckmail_provider_url",
    "duckmail_bearer",
    "duckmail_domain",
    "duckmail_api_key",
    "freemail_api_url",
    "freemail_admin_token",
    "freemail_username",
    "freemail_password",
    "freemail_domain",
    "moemail_api_url",
    "moemail_api_key",
    "skymail_api_base",
    "skymail_token",
    "skymail_domain",
    "cloudmail_api_base",
    "cloudmail_admin_email",
    "cloudmail_admin_password",
    "cloudmail_domain",
    "cloudmail_subdomain",
    "cloudmail_timeout",
    "mail_provider",
    "mail_import_source",
    "outlook_backend",
    "mailbox_otp_timeout_seconds",
    "maliapi_base_url",
    "maliapi_api_key",
    "maliapi_domain",
    "maliapi_auto_domain_strategy",
    "applemail_base_url",
    "applemail_pool_dir",
    "applemail_pool_file",
    "applemail_mailboxes",
    "gptmail_base_url",
    "gptmail_api_key",
    "gptmail_domain",
    "opentrashmail_api_url",
    "opentrashmail_domain",
    "opentrashmail_password",
    "cfworker_api_url",
    "cfworker_admin_token",
    "cfworker_custom_auth",
    "cfworker_domain",
    "cfworker_domains",
    "cfworker_enabled_domains",
    "cfworker_subdomain",
    "cfworker_random_subdomain",
    "cfworker_random_name_subdomain",
    "cfworker_fingerprint",
    "luckmail_base_url",
    "luckmail_api_key",
    "luckmail_email_type",
    "luckmail_domain",
    "cpa_enabled",
    "cpa_api_url",
    "cpa_api_key",
    "cpa_cleanup_enabled",
    "cpa_cleanup_interval_minutes",
    "cpa_cleanup_threshold",
    "cpa_cleanup_concurrency",
    "cpa_cleanup_register_delay_seconds",
    "sub2api_enabled",
    "sub2api_api_url",
    "sub2api_api_key",
    "sub2api_group_ids",
    "team_manager_url",
    "team_manager_key",
    "codex_proxy_url",
    "codex_proxy_key",
    "codex_proxy_upload_type",
    "cliproxyapi_base_url",
    "cliproxyapi_management_key",
    "icloud_region",
    "icloud_alias_label",
    "icloud_alias_note",
    "icloud_account_email",
    "sms_enabled",
    "sms_provider",
    "sms_api_key",
    "sms_service",
    "sms_country",
    "sms_auto_country",
    "sms_allowed_countries",
    "sms_auto_min_stock",
    "sms_auto_max_price",
    "sms_max_price",
    "sms_fixed_price",
    "sms_reuse_phone",
    "sms_phone_success_max",
    "sms_per_phone_timeout",
    "sms_max_phone_attempts",
    "sms_code_retries_per_phone",
    "external_apps_update_mode",
    "contribution_enabled",
    "contribution_server_url",
    "contribution_key",
    "contribution_mode",
    "custom_contribution_url",
    "custom_contribution_token",
]


class ConfigUpdate(BaseModel):
    data: dict


class AppleMailImportRequest(BaseModel):
    content: str
    filename: str = ""
    pool_dir: str = ""
    bind_to_config: bool = True


@router.get("")
def get_config():
    all_cfg = config_store.get_all()
    if all_cfg.get("mail_provider") == "outlook":
        all_cfg["mail_provider"] = "microsoft"
    if not all_cfg.get("mail_provider"):
        all_cfg["mail_provider"] = "luckmail"
    all_cfg["mail_import_source"] = normalize_mail_import_source(
        all_cfg.get("mail_import_source"),
        all_cfg.get("mail_provider"),
    )
    if not all_cfg.get("applemail_base_url"):
        all_cfg["applemail_base_url"] = "https://www.appleemail.top"
    if not all_cfg.get("applemail_pool_dir"):
        all_cfg["applemail_pool_dir"] = "mail"
    if not all_cfg.get("applemail_mailboxes"):
        all_cfg["applemail_mailboxes"] = "INBOX,Junk"
    if not all_cfg.get("outlook_backend"):
        all_cfg["outlook_backend"] = "graph"
    if not all_cfg.get("gptmail_base_url"):
        all_cfg["gptmail_base_url"] = "https://mail.chatgpt.org.uk"
    if not all_cfg.get("luckmail_base_url"):
        all_cfg["luckmail_base_url"] = "https://mails.luckyous.com/"
    if not str(all_cfg.get("contribution_enabled", "") or "").strip():
        all_cfg["contribution_enabled"] = "0"
    if not all_cfg.get("contribution_server_url"):
        all_cfg["contribution_server_url"] = "http://new.xem8k5.top:7317/"
    if not all_cfg.get("contribution_mode"):
        all_cfg["contribution_mode"] = "codex"
    if not all_cfg.get("custom_contribution_url"):
        all_cfg["custom_contribution_url"] = "http://127.0.0.1:5000"
    if not all_cfg.get("external_apps_update_mode"):
        all_cfg["external_apps_update_mode"] = "tag"
    if not str(all_cfg.get("email_domain_rule_enabled", "") or "").strip():
        all_cfg["email_domain_rule_enabled"] = "0"
    if not str(all_cfg.get("email_domain_level_count", "") or "").strip():
        all_cfg["email_domain_level_count"] = "2"
    if not str(all_cfg.get("sms_enabled", "") or "").strip():
        all_cfg["sms_enabled"] = "0"
    if not all_cfg.get("sms_provider"):
        all_cfg["sms_provider"] = "smsbower"
    if not all_cfg.get("sms_service"):
        all_cfg["sms_service"] = SMS_DEFAULT_SERVICE
    if not all_cfg.get("sms_country"):
        all_cfg["sms_country"] = SMS_DEFAULT_COUNTRY
    # 只返回已知 key，未设置的返回空字符串
    return {k: all_cfg.get(k, "") for k in CONFIG_KEYS}


@router.put("")
def update_config(body: ConfigUpdate):
    # 只允许更新已知 key
    safe = {k: v for k, v in body.data.items() if k in CONFIG_KEYS}
    if safe.get("mail_provider") == "outlook":
        safe["mail_provider"] = "microsoft"
    if "mail_import_source" in safe:
        # 只有同一次请求里带了 mail_provider 才谈得上对齐；面板单独改视图时，
        # 视图本身就是用户的新选择，不该被库里的旧 provider 拽回去
        safe["mail_import_source"] = (
            align_source_with_provider(safe["mail_import_source"], safe["mail_provider"])
            if "mail_provider" in safe
            else normalize_mail_import_source(safe["mail_import_source"])
        )
    if "email_domain_rule_enabled" in safe:
        enabled = str(safe.get("email_domain_rule_enabled", "")).strip().lower()
        safe["email_domain_rule_enabled"] = (
            "1" if enabled in {"1", "true", "yes", "on"} else "0"
        )
    if "email_domain_level_count" in safe:
        try:
            level_count = int(str(safe.get("email_domain_level_count", "")).strip())
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="域名级数必须是整数") from exc
        if level_count < 2:
            raise HTTPException(status_code=400, detail="域名级数不能小于 2")
        safe["email_domain_level_count"] = str(level_count)
    config_store.set_many(safe)
    return {"ok": True, "updated": list(safe.keys())}


@router.post("/applemail/import")
def import_applemail_pool(body: AppleMailImportRequest):
    try:
        strategy = mail_import_registry.get("applemail")
        result = strategy.execute(
            MailImportExecuteRequest(
                type="applemail",
                content=body.content,
                filename=body.filename,
                pool_dir=body.pool_dir,
                bind_to_config=body.bind_to_config,
            )
        )
        snapshot = result.snapshot.model_dump()
        return {
            "filename": snapshot["filename"],
            "path": result.meta.get("path", ""),
            "count": snapshot["count"],
            "pool_dir": snapshot["pool_dir"],
            "bound_to_config": bool(result.meta.get("bound_to_config")),
            "items": snapshot["items"],
            "truncated": snapshot["truncated"],
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/applemail/pool")
def get_applemail_pool_snapshot(
    pool_dir: str = "",
    pool_file: str = "",
):
    try:
        strategy = mail_import_registry.get("applemail")
        snapshot = strategy.get_snapshot(
            MailImportSnapshotRequest(
                type="applemail",
                pool_dir=pool_dir,
                pool_file=pool_file,
            )
        )
        return snapshot.model_dump()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
