"""Grok 账号 → AIClient2API(A2) 推送模块（grok-web provider）。

A2 的 grok-web provider 凭据：provider_pools.json 的 grok-web 池节点，
字段 GROK_COOKIE_TOKEN = grok sso token（src/auth/grok-auth.js 确认：
输入支持字符串或 {sso / GROK_COOKIE_TOKEN / token} 对象）。
8317 不支持 grok 渠道（config 渠道列表无 grok），故 Grok 推送目标为 A2。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
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


def _a2_pools_path(a2_base_dir: str | None = None) -> Path:
    raw = str(
        a2_base_dir
        or _get_config_value("a2_base_dir", DEFAULT_A2_BASE_DIR)
        or DEFAULT_A2_BASE_DIR
    ).strip()
    return Path(raw) / "configs" / "provider_pools.json"


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


def _extract_sso(account) -> str:
    extra = getattr(account, "extra", {}) or {}
    token = (
        extra.get("sso")
        or extra.get("sso_token")
        or extra.get("sso_rw")
        or getattr(account, "token", "")
    )
    token = str(token or "").strip()
    if token.startswith("sso="):
        token = token[4:]
    return token


def upload_grok_to_a2(
    account,
    a2_base_dir: str | None = None,
) -> Tuple[bool, str]:
    """把单个 grok 账号的 sso token 写入 A2 的 grok-web provider pool。"""
    token = _extract_sso(account)
    if not token:
        return False, "账号缺少 sso token，无法推送 A2"

    pools_path = _a2_pools_path(a2_base_dir)
    pools = (
        json.loads(pools_path.read_text(encoding="utf-8"))
        if pools_path.exists()
        else {}
    )
    pool = pools.setdefault("grok-web", [])

    for node in pool:
        if node.get("GROK_COOKIE_TOKEN") == token:
            return True, "sso token 已存在 A2 grok-web 池，跳过"

    email = getattr(account, "email", "") or ""
    node = {
        "customName": f"Grok账号-{email or 'unknown'}",
        "GROK_COOKIE_TOKEN": token,
        "uuid": str(uuid.uuid4()),
        "checkModelName": None,
        "checkHealth": False,
        "isHealthy": True,
        "isDisabled": False,
        "lastUsed": None,
        "usageCount": 0,
        "errorCount": 0,
        "lastErrorTime": None,
    }
    # 携带 CF cookies（注册时已存 extra）——A2 grok-web 需要才能过 x.ai 风控（403 page out of date）
    _extra = getattr(account, "extra", {}) or {}
    for _k in ("cf_clearance", "cf_bm"):
        if _extra.get(_k):
            node["GROK_CF_CLEARANCE" if _k == "cf_clearance" else "GROK_CF_BM"] = _extra[_k]
    pool.append(node)
    _atomic_write(pools_path, json.dumps(pools, ensure_ascii=False, indent=2))
    logger.info(f"Grok 账号已推送 A2 grok-web 池: {email}")
    return True, f"已推送 A2 grok-web 池: {email}"
