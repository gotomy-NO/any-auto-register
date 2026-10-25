"""ZCode 账号 → AIClient2API(A2) 推送模块。

A2 已有 zcode provider（src/providers/zcode/zcode-core.js，
token + X-Signature 直连 chat.z.ai /api/v2/chat/completions），
本模块把 zcode 会话凭据落盘到 A2 configs/zcode/（时间戳目录，
与 kiro 凭据同款结构），再由 A2 的 autoLinkProviderConfigs / config-scanner
扫描关联到 provider_pools.json 的 zcode-web 池。

凭据格式：
    email / username / token(chat.z.ai 会话 JWT) / base / captured_at
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


def _account_to_dict(account) -> dict:
    """兼容 dataclass Account 与 dict 两种入参（自动补传传 Account，手动操作传 dict）。"""
    if isinstance(account, dict):
        return account
    if hasattr(account, "__dataclass_fields__"):
        import dataclasses

        return dataclasses.asdict(account)
    return dict(getattr(account, "__dict__", {}) or {})


def build_zcode_credential(account: dict) -> dict:
    """把注册机 zcode 账号转成 A2 configs/zcode 凭据格式。"""
    return {
        "email": account.get("email") or "",
        "username": (account.get("extra") or {}).get("username") or "",
        "token": (account.get("extra") or {}).get("token")
        or account.get("token")
        or "",
        "base": "https://chat.z.ai",
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def upload_zcode_to_a2(
    account,
    a2_base_dir: str | None = None,
) -> Tuple[bool, str]:
    """推送 zcode 凭据到 A2 configs/zcode/（时间戳目录）。"""
    account = _account_to_dict(account)
    extra = account.get("extra") or {}
    if not isinstance(extra, dict):
        extra = {}
    token = extra.get("token") or account.get("token") or ""
    if not token:
        return False, "zcode 账号缺少 token，无法推送"

    base = _a2_base_dir(a2_base_dir)
    ts = int(time.time() * 1000)
    dir_name = f"{ts}_zcode-auth"
    target_dir = base / "configs" / "zcode" / dir_name
    _atomic_write(
        target_dir / f"{dir_name}.json",
        json.dumps(build_zcode_credential(account), ensure_ascii=False, indent=2),
    )
    path = str(target_dir / f"{dir_name}.json")
    logger.info(f"[补传] zcode 凭据已保存: {path}")
    return True, f"zcode 凭据已保存到 A2: {path}"
