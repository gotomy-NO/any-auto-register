"""
Kiro 账号切换 —— 写入 ~/.aws/sso/cache/ token 文件，Kiro IDE 自动识别
参考 kiro-account-manager (Tauri/Rust) 的 switch_kiro_account 实现
"""

import os
import json
import hashlib
import logging
import tempfile
from typing import Tuple
from datetime import datetime, timezone, timedelta

from curl_cffi import requests as cffi_requests

logger = logging.getLogger(__name__)

OIDC_ENDPOINT = "https://oidc.us-east-1.amazonaws.com"
BUILDER_ID_START_URL = "https://view.awsapps.com/start"
DEFAULT_PROFILE_ARN = "arn:aws:codewhisperer:us-east-1:699475941385:profile/EHGA3GRVQMUK"


def _calculate_client_id_hash(start_url: str) -> str:
    """与 Kiro IDE 源码一致的 clientIdHash 计算"""
    input_str = json.dumps({"startUrl": start_url}, separators=(",", ":"))
    return hashlib.sha1(input_str.encode()).hexdigest()


def _get_cache_dir() -> str:
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME", "")
    return os.path.join(home, ".aws", "sso", "cache")


def _atomic_write(filepath: str, content: str):
    """原子写入：先写临时文件，再 rename"""
    dir_path = os.path.dirname(filepath)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        os.replace(tmp_path, filepath)
    except Exception:
        os.close(fd) if not os.path.exists(tmp_path) else None
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def refresh_kiro_token(
    refresh_token: str,
    client_id: str,
    client_secret: str,
) -> Tuple[bool, dict]:
    """刷新 Kiro OIDC token，返回 (ok, {accessToken, refreshToken, expiresIn})"""
    if not refresh_token or not client_id or not client_secret:
        return False, {"error": "缺少 refreshToken / clientId / clientSecret"}
    try:
        r = cffi_requests.post(
            f"{OIDC_ENDPOINT}/token",
            json={
                "grantType": "refresh_token",
                "clientId": client_id,
                "clientSecret": client_secret,
                "refreshToken": refresh_token,
            },
            headers={
                "content-type": "application/json",
                "user-agent": "aws-sdk-rust/1.3.9 os/macOS lang/rust",
            },
            impersonate="chrome131",
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            return True, {
                "accessToken": data.get("accessToken", ""),
                "refreshToken": data.get("refreshToken", refresh_token),
                "expiresIn": data.get("expiresIn", 3600),
            }
        return False, {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return False, {"error": str(e)}


def switch_kiro_account(
    access_token: str,
    refresh_token: str,
    client_id: str = "",
    client_secret: str = "",
    provider: str = "BuilderId",
    auth_method: str = "IdC",
    region: str = "us-east-1",
    start_url: str = "",
) -> Tuple[bool, str]:
    """
    切换 Kiro 桌面应用账号（写入 token 文件，无需重启 IDE）。

    BuilderId 账号: auth_method="IdC", provider="BuilderId"
    Social 账号:    auth_method="social", provider="Google"/"GitHub"
    Enterprise:     auth_method="IdC", provider="Enterprise", 需提供 start_url
    """
    cache_dir = _get_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)

    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )

    if auth_method == "IdC":
        actual_start_url = start_url or BUILDER_ID_START_URL
        client_id_hash = _calculate_client_id_hash(actual_start_url)

        token_data = {
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "expiresAt": expires_at,
            "authMethod": "IdC",
            "provider": provider,
            "clientIdHash": client_id_hash,
            "region": region,
        }
    else:
        token_data = {
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "profileArn": DEFAULT_PROFILE_ARN,
            "expiresAt": expires_at,
            "authMethod": "social",
            "provider": provider,
        }

    try:
        token_path = os.path.join(cache_dir, "kiro-auth-token.json")
        content = json.dumps(token_data, indent=2, ensure_ascii=False)
        _atomic_write(token_path, content)

        if auth_method == "IdC" and client_id and client_secret:
            client_expires = (
                datetime.now(timezone.utc) + timedelta(days=90)
            ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            client_reg = {
                "clientId": client_id,
                "clientSecret": client_secret,
                "expiresAt": client_expires,
            }
            client_path = os.path.join(cache_dir, f"{client_id_hash}.json")
            _atomic_write(
                client_path,
                json.dumps(client_reg, indent=2, ensure_ascii=False),
            )

        return True, "切换成功，Kiro IDE 将自动使用新账号"

    except Exception as e:
        logger.error(f"Kiro 账号切换失败: {e}")
        return False, f"切换失败: {str(e)}"


def restart_kiro_ide() -> Tuple[bool, str]:
    """关闭并重启 Kiro IDE，使新 token 立即生效"""
    import subprocess
    import platform
    import time

    sys = platform.system()

    try:
        if sys == "Darwin":
            subprocess.run(["osascript", "-e", 'quit app "Kiro"'], capture_output=True)
            time.sleep(2.0)
            kiro_app = "/Applications/Kiro.app"
            if os.path.exists(kiro_app):
                subprocess.Popen(["open", "-a", "Kiro"])
                return True, "Kiro IDE 已重启"
            return True, "已关闭 Kiro IDE（未找到应用路径，请手动启动）"

        elif sys == "Windows":
            subprocess.run(
                ["taskkill", "/IM", "Kiro.exe", "/F"],
                capture_output=True,
                creationflags=0x0800_0000,
            )
            time.sleep(1.5)
            localappdata = os.environ.get("LOCALAPPDATA", "")
            kiro_exe = os.path.join(localappdata, "Programs", "Kiro", "Kiro.exe")
            if os.path.exists(kiro_exe):
                subprocess.Popen([kiro_exe])
                return True, "Kiro IDE 已重启"
            return True, "已关闭 Kiro IDE（未找到应用路径，请手动启动）"

        else:
            subprocess.run(["pkill", "-f", "kiro"], capture_output=True)
            time.sleep(1.5)
            for path in ["/usr/bin/kiro", os.path.expanduser("~/.local/bin/kiro")]:
                if os.path.exists(path):
                    subprocess.Popen([path])
                    return True, "Kiro IDE 已重启"
            try:
                subprocess.Popen(["kiro"])
                return True, "Kiro IDE 已重启"
            except FileNotFoundError:
                return True, "已关闭 Kiro IDE（未找到应用路径，请手动启动）"

    except Exception as e:
        logger.error(f"Kiro IDE 重启失败: {e}")
        return False, f"重启失败: {str(e)}"


def read_current_kiro_account() -> dict | None:
    """读取当前 Kiro IDE 正在使用的账号 token"""
    token_path = os.path.join(_get_cache_dir(), "kiro-auth-token.json")
    if not os.path.exists(token_path):
        return None
    try:
        with open(token_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
