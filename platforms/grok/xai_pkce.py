"""xAI 本地 Authorization Code + PKCE 流程（移植自 progrok backend/xai_pkce.py）。

职责划分：浏览器负责交互式授权（登录态、consent 点击、回调捕获），
本模块只负责 PKCE 会话生成、回调校验、本地 code→token 兑换。
产出的 access_token/refresh_token 对齐 core/nr_client.py canonical_from_account
读取路径（extra["tokens"]），经 9Router grok-cli bulk-import 消费。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse


AUTHORIZE_URL = "https://auth.x.ai/oauth2/authorize"
TOKEN_URL = "https://auth.x.ai/oauth2/token"
# grok-cli 客户端（与 progrok/9Router 同源），scope 含 offline_access 以取得 refresh_token
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
REDIRECT_URI = "http://127.0.0.1:56121/callback"
SCOPE = "openid profile email offline_access grok-cli:access api:access"
# access_token 无 exp 声明时的兜底有效期（秒）：6 小时
DEFAULT_TOKEN_TTL_SEC = 21_600


class XaiPKCEError(RuntimeError):
    """本地 PKCE 授权或兑换失败。"""


@dataclass(frozen=True)
class PKCESession:
    session_id: str
    state: str
    nonce: str
    code_verifier: str
    code_challenge: str
    client_id: str = CLIENT_ID
    redirect_uri: str = REDIRECT_URI
    scope: str = SCOPE
    created_at: float = 0.0


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def jwt_claims(token: str) -> dict[str, Any]:
    """解析 JWT payload；失败返回空 dict（绝不抛异常）。"""
    try:
        segment = str(token or "").split(".")[1]
        segment += "=" * (-len(segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(segment))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def token_to_auth_payload(token: dict[str, Any], *, email: str) -> dict[str, Any]:
    """OAuth token 响应 → 凭证 dict（含从 AT JWT exp 推导的 expires_at ISO 串）。"""
    access_token = str(token.get("access_token") or "").strip()
    refresh_token = str(token.get("refresh_token") or "").strip()
    if not access_token or not refresh_token:
        raise XaiPKCEError("xAI OAuth 凭证缺少 access_token 或 refresh_token")
    claims = jwt_claims(access_token)
    try:
        expires_at = float(
            claims.get("exp")
            or time.time() + int(token.get("expires_in") or DEFAULT_TOKEN_TTL_SEC)
        )
    except (TypeError, ValueError):
        expires_at = time.time() + DEFAULT_TOKEN_TTL_SEC
    expires_iso = (
        datetime.fromtimestamp(expires_at, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": str(token.get("id_token") or ""),
        "email": str(email or claims.get("email") or "").strip().lower(),
        "token_type": str(token.get("token_type") or "Bearer"),
        "expires_in": token.get("expires_in"),
        "expires_at": expires_iso,
        "scope": str(token.get("scope") or SCOPE),
        "oidc_issuer": "https://auth.x.ai",
        "oidc_client_id": CLIENT_ID,
    }


def _ascii_ca_shim() -> str:
    """certifi CA 束落 ASCII 路径，返回可用的 CA 文件路径（无需 shim 时返回空串）。

    curl error 77（error adding trust anchors）：项目根含中文（Grok注册机/临时）时
    libcurl 无法从 CJK 路径加载 cacert.pem，且 SSL_CERT_FILE 指到 ASCII 副本对
    curl_cffi 默认 CAfile 仍不生效——请求级 verify= 指定 ASCII CA 才可靠。
    """
    try:
        import certifi

        src = certifi.where()
        src.encode("ascii")  # 能 ascii 编码即无需 shim
        return ""
    except Exception:
        pass
    try:
        import certifi

        src = certifi.where()
    except Exception:
        return ""
    import os
    import shutil

    dest = os.path.join(
        os.path.expandvars(r"%LOCALAPPDATA%"), "xai-ca.pem"
    )
    try:
        if not (os.path.isfile(dest) and os.path.getsize(dest) > 100_000):
            shutil.copyfile(src, dest)
        return dest
    except Exception:
        return ""


def _requests_session(token_proxy: str = "") -> Any:
    """curl_cffi 会话（TLS 指纹），可带代理；curl_cffi 缺失时退回 stdlib。"""
    proxies = {"http": token_proxy, "https": token_proxy} if token_proxy else None
    try:
        from curl_cffi import requests as cffi_requests

        return cffi_requests.Session(impersonate="chrome131", proxies=proxies)
    except Exception:
        import requests as std_requests

        return std_requests.Session(proxies=proxies) if proxies else std_requests.Session()


def http_timeout() -> float:
    try:
        return max(5.0, float(__import__("os").getenv("XAI_OAUTH_HTTP_TIMEOUT", "20") or 20))
    except (TypeError, ValueError):
        return 20.0


def create_pkce_session() -> PKCESession:
    verifier = _b64url(secrets.token_bytes(32))  # 43 字符，满足 RFC 7636 43~128
    return PKCESession(
        session_id=secrets.token_hex(16),
        state=secrets.token_hex(32),
        nonce=secrets.token_hex(16),
        code_verifier=verifier,
        code_challenge=_b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
        created_at=time.time(),
    )


def build_authorization_url(session: PKCESession) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": session.client_id,
            "redirect_uri": session.redirect_uri,
            "scope": session.scope,
            "state": session.state,
            "nonce": session.nonce,
            "code_challenge": session.code_challenge,
            "code_challenge_method": "S256",
            "plan": "generic",
            "referrer": "reg8000",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def save_pkce_session(session: PKCESession, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(session), indent=2), encoding="utf-8")


def clear_pkce_verifier(path: Path) -> None:
    """兑换结束后抹掉一次性 verifier（保留非密诊断字段）。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["code_verifier"] = ""
        payload["code_challenge"] = ""
        payload["completed_at"] = time.time()
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        return


def parse_callback(value: str, expected_state: str) -> str:
    """回调 URL/查询串/裸 code → 授权码。校验 error 与 state。"""
    raw = str(value or "").strip()
    if not raw:
        raise XaiPKCEError("xAI OAuth 未返回授权码")

    parsed = urlparse(raw)
    query = parsed.query
    if not query and raw.startswith("?"):
        query = raw[1:]
    if query:
        values = parse_qs(query)
        error = str((values.get("error") or [""])[0]).strip()
        if error:
            detail = str((values.get("error_description") or [error])[0]).strip()
            raise XaiPKCEError(f"xAI OAuth 授权被拒绝：{detail[:240]}")
        code = str((values.get("code") or [""])[0]).strip()
        state = str((values.get("state") or [""])[0]).strip()
        if not code:
            raise XaiPKCEError("xAI OAuth 回调缺少授权码")
        if not state or not secrets.compare_digest(state, expected_state):
            raise XaiPKCEError("xAI OAuth 回调 state 校验失败")
        return code

    # 兼容浏览器助手直接从页面读出 code 的路径（已由浏览器侧校验 state）
    if any(char.isspace() for char in raw):
        raise XaiPKCEError("xAI OAuth 返回了无效授权码")
    return raw


def _response_detail(response: Any) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            return str(
                payload.get("error_description")
                or payload.get("error")
                or payload.get("message")
                or ""
            )[:240]
    except Exception:
        pass
    return str(getattr(response, "text", "") or "")[:240]


def exchange_code(
    code: str,
    session: PKCESession,
    *,
    http_session: Any | None = None,
    token_proxy: str = "",
    timeout: float = 20.0,
) -> dict[str, Any]:
    """授权码 + verifier → token 响应（本地直连 auth.x.ai）。

    curl error 77（项目路径含中文导致 certifi CA 束无法加载）时自动改用
    ASCII 路径 CA 副本（%LOCALAPPDATA%/xai-ca.pem）重试一次。
    """
    client = http_session or _requests_session(token_proxy)
    data = {
        "grant_type": "authorization_code",
        "client_id": session.client_id,
        "code": code,
        "redirect_uri": session.redirect_uri,
        "code_verifier": session.code_verifier,
    }
    headers = {
        "Accept": "application/json",
        "User-Agent": "reg8000-grok-oauth/2.0",
    }

    def _post(verify: Any = None) -> Any:
        kwargs = {"timeout": timeout}
        if verify is not None:
            kwargs["verify"] = verify
        return client.post(TOKEN_URL, data=data, headers=headers, **kwargs)

    try:
        response = _post()
    except Exception as exc:
        if "curl: (77)" not in str(exc):
            raise
        ca_path = _ascii_ca_shim()
        if not ca_path:
            raise
        response = _post(verify=ca_path)
    status = int(getattr(response, "status_code", 0) or 0)
    if status < 200 or status >= 300:
        detail = _response_detail(response) or "上游未返回错误详情"
        raise XaiPKCEError(f"xAI OAuth 兑换失败（HTTP {status}）：{detail}")
    try:
        token = response.json()
    except Exception as exc:
        raise XaiPKCEError("xAI OAuth 兑换响应不是有效 JSON") from exc
    if not isinstance(token, dict):
        raise XaiPKCEError("xAI OAuth 兑换响应格式错误")
    if not str(token.get("access_token") or "").strip():
        raise XaiPKCEError("xAI OAuth 兑换响应缺少 access_token")
    if not str(token.get("refresh_token") or "").strip():
        raise XaiPKCEError("xAI OAuth 兑换响应缺少 refresh_token")
    return token


def authorize_and_exchange(
    authorize: Callable[[str, str], str],
    *,
    session_path: Path,
    should_cancel: Callable[[], Any] | None = None,
    on_progress: Callable[[str], Any] | None = None,
    http_session: Any | None = None,
    token_proxy: str = "",
    timeout: float = 20.0,
) -> dict[str, Any]:
    """完整本地 PKCE：建会话 → 浏览器授权取回调 → 校验 → 兑换。

    ``authorize(auth_url, expected_state) -> callback_url_or_code`` 由浏览器侧
    （XaiBrowserSession.authorize_pkce）实现；本函数不感知浏览器。
    """

    def check_cancel() -> None:
        if should_cancel is not None:
            should_cancel()

    def progress(stage: str) -> None:
        if on_progress is not None:
            on_progress(stage)

    session = create_pkce_session()
    save_pkce_session(session, session_path)
    progress("session_created")
    try:
        check_cancel()
        callback = authorize(build_authorization_url(session), session.state)
        progress("callback_captured")
        check_cancel()
        code = parse_callback(callback, session.state)
        token = exchange_code(
            code,
            session,
            http_session=http_session,
            token_proxy=token_proxy,
            timeout=timeout,
        )
        progress("token_exchanged")
        return token
    finally:
        clear_pkce_verifier(session_path)
