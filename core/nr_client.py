"""9Router 网关客户端（推送目标 `9r` 的实现层）。

register-8000 → 9Router 的四类操作：登录 / 批量导入连接 / 列连接 / 查连接状态。
凭据来源：config_store 键
    nr_base_url        默认 http://127.0.0.1:20128
    nr_panel_password  9Router 面板密码（loopback 免 CLI token）
设计约束（台账/历史教训）：
    - 直连不走系统代理（Clash TUN 会污染本机探测，见项目规矩）
    - token 永不打印/落日志；响应只回传 id/email 等元信息
    - 面板密码连错 5 次锁 IP（P19）：登录失败不重试超过 1 次
"""
from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import Optional

from .config_store import config_store


def _no_proxy_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class NRClient:
    def __init__(self, base_url: Optional[str] = None, password: Optional[str] = None):
        self.base = (base_url or str(config_store.get("nr_base_url", "") or "http://127.0.0.1:20128")).rstrip("/")
        self.password = password if password is not None else str(config_store.get("nr_panel_password", "") or "")
        self._jar: str = ""
        self.opener = _no_proxy_opener()

    # ---- 会话 ----
    def _request(self, method: str, path: str, body=None, timeout: int = 60):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        if self._jar:
            req.add_header("Cookie", self._jar)
        resp = self.opener.open(req, timeout=timeout)
        sc = resp.headers.get("Set-Cookie") or ""
        if sc:
            self._jar = sc.split(";")[0]
        raw = resp.read()
        return (json.loads(raw) if raw else {}), resp.status

    def login(self) -> bool:
        if not self.password:
            raise RuntimeError("nr_panel_password 未配置（config_store）")
        _, st = self._request("POST", "/api/auth/login", {"password": self.password})
        return st == 200

    def ensure_login(self):
        try:
            self._request("GET", "/api/combos")
        except urllib.error.HTTPError:
            if not self.login():
                raise RuntimeError("9Router 登录失败（不重试，防 5 次锁 IP）")

    # ---- 推送（bulk-import）----
    def bulk_import(self, provider: str, accounts: list) -> dict:
        """accounts: canonical 凭据 dict 列表；provider 如 grok-cli / codex。
        返回 {total, success, failed, results:[{index,ok,id,email|error}]}（无 token）。"""
        self.ensure_login()
        body = {"accounts": accounts}
        resp, _ = self._request("POST", f"/api/oauth/{provider}/bulk-import", body, timeout=120)
        return resp

    # ---- 台账回查 ----
    def list_connections(self) -> list:
        self.ensure_login()
        resp, _ = self._request("GET", "/api/providers")
        items = resp if isinstance(resp, list) else (resp.get("providers") or resp.get("connections") or [])
        # 只留元信息，防止上层误把 data(含密文token) 写进台账
        return [{k: v for k, v in c.items() if k != "data"} for c in items]

    def connection_state(self, conn_id: str) -> dict:
        for c in self.list_connections():
            if c.get("id") == conn_id:
                return {"found": True, "isActive": bool(c.get("isActive")),
                        "testStatus": c.get("testStatus"), "provider": c.get("provider"),
                        "email": c.get("email")}
        return {"found": False}

    def set_active(self, conn_id: str, active: bool) -> bool:
        """禁用/启用连接（测试与摘除用）。"""
        self.ensure_login()
        try:
            _, st = self._request("PATCH", f"/api/providers/{conn_id}", {"isActive": bool(active)})
            return st == 200
        except urllib.error.HTTPError:
            # 路由可能是 PUT，兜底
            _, st = self._request("PUT", f"/api/providers/{conn_id}", {"isActive": bool(active)})
            return st == 200


# ---- 与台账的衔接：推送 + 回写 + 回查 ----
def canonical_from_account(platform_out: str, account) -> dict:
    """accounts 行/对象 → bulk-import 条目（snake_case，grok-cli/codex 均可收）。"""
    extra: dict = {}
    if hasattr(account, "get_extra") and callable(getattr(account, "get_extra", None)):
        try:
            extra = account.get_extra() or {}
        except Exception:
            extra = {}
    if not extra and isinstance(account, dict):
        raw = account.get("extra") or account.get("extra_json") or "{}"
        if isinstance(raw, str):
            try:
                import json as _json
                extra = _json.loads(raw)
            except Exception:
                extra = {}
        else:
            extra = raw or {}
    if not extra and hasattr(account, "extra_json"):
        raw = getattr(account, "extra_json", None) or "{}"
        try:
            import json as _json
            extra = _json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            extra = {}
    if not extra and not isinstance(account, dict):
        attr = getattr(account, "extra", None)  # core.base_platform.Account dataclass
        if isinstance(attr, dict):
            extra = attr
    tok = extra.get("tokens") or {}
    email = getattr(account, "email", None) or (account.get("email") if isinstance(account, dict) else "") or ""
    return {
        "access_token": tok.get("access_token") or extra.get("access_token") or "",
        "refresh_token": tok.get("refresh_token") or extra.get("refresh_token") or None,
        "id_token": tok.get("id_token") or extra.get("id_token") or None,
        "email": email or "",
        "displayName": f"reg8000-{platform_out}-{email[:20]}",
    }


def canonical_codex_from_account(account) -> dict:
    """accounts 行/对象 → codex bulk-import 条目（camelCase，9Router codex route
    只认 accessToken；identity 字段由 route 从 idToken claims 回填，可选补全）。"""
    extra: dict = {}
    if isinstance(account, dict):
        raw = account.get("extra") or account.get("extra_json") or "{}"
        if isinstance(raw, str):
            try:
                import json as _json
                raw = _json.loads(raw)
            except Exception:
                raw = {}
        extra = raw or {}
    else:
        if hasattr(account, "get_extra") and callable(getattr(account, "get_extra", None)):
            try:
                extra = account.get_extra() or {}
            except Exception:
                extra = {}
        if not extra:
            attr = getattr(account, "extra", None)
            if isinstance(attr, dict):
                extra = attr
        if not extra and hasattr(account, "extra_json"):
            raw = getattr(account, "extra_json", None) or "{}"
            try:
                import json as _json
                extra = _json.loads(raw) if isinstance(raw, str) else (raw or {})
            except Exception:
                extra = {}
    tok = extra.get("tokens") or {}
    email = getattr(account, "email", None) or (account.get("email") if isinstance(account, dict) else "") or ""
    access = tok.get("access_token") or extra.get("access_token") or ""
    refresh = tok.get("refresh_token") or extra.get("refresh_token") or ""
    idt = tok.get("id_token") or extra.get("id_token") or ""
    item: dict = {
        "accessToken": access,
        "refreshToken": refresh or None,
        "idToken": idt or None,
        "email": email or "",
        "name": f"reg8000-codex-{email[:20]}",
    }
    psd = {}
    acct_id = tok.get("account_id") or extra.get("chatgpt_account_id") or extra.get("account_id") or getattr(account, "user_id", "") or ""
    if acct_id:
        psd["chatgptAccountId"] = acct_id
    ws = tok.get("workspace_id") or extra.get("workspace_id") or ""
    if ws:
        psd["chatgptWorkspaceId"] = ws
    if psd:
        item["providerSpecificData"] = psd
    expires = tok.get("expires_at")
    if expires:
        item["expiresAt"] = expires
    return item


def push_account_to_9r(platform_out: str, account) -> dict:
    """upload_router 的 `9r` 分支入口。返回 {ok, msg}，msg 含连接 id。"""
    try:
        cli = NRClient()
        item = canonical_from_account(platform_out, account)
        if not item["access_token"]:
            return {"ok": False, "msg": "canonical 缺 access_token"}
        res = cli.bulk_import(platform_out, [item])
        r0 = (res.get("results") or [{}])[0]
        ok = bool(r0.get("ok"))
        return {"ok": ok, "msg": f"id={r0.get('id')}" if ok else f"err={r0.get('error')}",
                "conn_id": r0.get("id")}
    except Exception as e:  # noqa: BLE001 推送失败不影响注册主流程
        return {"ok": False, "msg": f"9r推送异常: {e}"}
