"""Kiro 账号 → AIClient2API(A2) 推送模块。

A2 的 kiro 凭据格式（src/auth/kiro-oauth.js tokenData）：
    accessToken / refreshToken / expiresAt(ISO) / authMethod='builder-id'
    / clientId / clientSecret / region / idcRegion

保存方式：configs/kiro/{timestamp}_kiro-auth-token/{timestamp}_kiro-auth-token.json
（每个凭据一个目录，之后通过 A2 的 autoLinkProviderConfigs / provider_pools.json 关联到池）
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

DEFAULT_A2_BASE_DIR = r"E:\AI\openAI\AIClient2API-main"


def _get_config_value(key: str, default: str = "") -> str:
    try:
        from core.config_store import config_store

        return str(config_store.get(key, "") or "") or default
    except Exception:
        return default


def _a2_base_dir(a2_base_dir: str | None = None) -> Path:
    raw = str(
        a2_base_dir
        or _get_config_value("a2_base_dir", DEFAULT_A2_BASE_DIR)
        or DEFAULT_A2_BASE_DIR
    ).strip()
    return Path(raw)


def _atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def build_kiro_token_data(account: dict, region: str = "us-east-1") -> dict:
    """把注册机 kiro 账号产出的 tokens 转成 A2 认可的 kiro tokenData。"""
    expires_in = int(account.get("expires_in") or 86400 * 7)
    return {
        "accessToken": account.get("accessToken") or account.get("access_token") or "",
        "refreshToken": account.get("refreshToken") or account.get("refresh_token") or "",
        "expiresAt": datetime.now(timezone.utc).timestamp()
        if False
        else datetime.fromtimestamp(
            time.time() + expires_in, tz=timezone.utc
        ).isoformat(),
        "authMethod": "builder-id",
        "clientId": account.get("clientId") or account.get("client_id") or "",
        "clientSecret": account.get("clientSecret") or account.get("client_secret") or "",
        "region": account.get("region") or region,
        "idcRegion": account.get("region") or region,
    }


def upload_kiro_to_a2(
    account: dict,
    a2_base_dir: str | None = None,
) -> Tuple[bool, str]:
    """把单个 kiro 账号凭据写入 A2 的 configs/kiro 目录。

    account 需包含：accessToken（必须）、refreshToken、clientId、clientSecret 等。
    写入路径：configs/kiro/{timestamp}_kiro-auth-token/{timestamp}_kiro-auth-token.json
    """
    token_data = build_kiro_token_data(account)
    if not token_data.get("accessToken"):
        return False, "账号缺少 accessToken，无法推送 A2"

    base = _a2_base_dir(a2_base_dir)
    ts = int(time.time() * 1000)
    folder_name = f"{ts}_kiro-auth-token"
    target_dir = base / "configs" / "kiro" / folder_name
    cred_path = target_dir / f"{folder_name}.json"

    _atomic_write(cred_path, json.dumps(token_data, ensure_ascii=False, indent=2))
    logger.info(
        f"Kiro 账号已推送 A2: {cred_path} "
        f"(accessToken_len={len(token_data['accessToken'])})"
    )
    return True, f"已推送 A2: {cred_path}"
