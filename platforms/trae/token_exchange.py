# -*- coding: utf-8 -*-
"""Trae.ai 密码登录 → Cloud-IDE JWT 交换（纯协议）— 2026-10-05

token 交换的正确姿势（401 根因修复，详见 docs/trae协议线实测-20261005.md）：
    1. POST https://ug-normal.trae.ai/passport/web/region/?aid=677332&account_sdk_source=web
           &sdk_version=2.1.10-tiktok&language=en&verifyFp=verify_x&msToken=
           body: hashed_id=<64hex>&temporary_id=<32hex>&type=2
       → data.domain = region 下发 IDE 域（us 出口= https://ug-normal.us.trae.ai）
       【要点】region/登录/GetUserToken 必须打 region 下发域；老链硬编码
       ug-normal.trae.ai / api-sg-central.trae.ai 是 SG 面，IP 出口(us) 与域(sg)
       不匹配 → GetUserToken 401 20310 "get session empty"。
    2. POST https://www.trae.ai/passport/web/email/login/?<同 query>
           body: type=1&email=<addr>&password=<pw>
       → 200 user_id_str（密码登录只在 www.trae.ai 同源可用；打 ug-normal 是 401/17）
    3. POST {ide}/cloudide/api/v3/trae/Login?type=email
           body: {UtmSource..BDVID 空, LoginChannel: ide_platform}
       → 200（建立 IDE 会话）
    4. POST {ide}/cloudide/api/v3/common/GetUserToken   json={}
       → Result.Token = Cloud-IDE JWT（len≈1000，**exp≈+8h**，data.id=user_id）
    5. POST {ide}/cloudide/api/v3/trae/CheckLogin   Authorization: Cloud-IDE-JWT <tok>
       → IsLogin=True + UserID + Region（可用性判定）
    6. POST {ide}/trae/api/v1/pay/create_order  （同 Authorization）
       → 200 order_info.cashier_url = token 活性试金石（core.py 口径）

纪律: token/密码只 _mask 留前缀+长度；不回显完整凭据。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import requests

WWW = "https://www.trae.ai"
REGION_HOST = "https://ug-normal.trae.ai"   # region 观察步固定打这个域
AID = "677332"
VERIFY_FP = "verify_x"
# trae 模型网关候选 host / 常量（B 通道；来源 ZedeX/trae-local-api, A-23187/trae-api）
MODEL_HOSTS_BY_REGION = {
    "us": "https://coreva-normal.trae.ai",
    "sg": "https://coresg-normal.trae.ai",
}
MODEL_HOST_CN = "https://trae-api-cn.mchost.guru"
MODEL_APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
OAUTH_CLIENT_ID_IDE = "ono9krqynydwx5"
OAUTH_CLIENT_ID_SOLO = "en1oxy7wnw8j9n"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def _mask(value: str, keep: int = 8) -> str:
    value = str(value or "")
    return f"{value[:keep]}...<len={len(value)}>" if value else ""


def _passport_query() -> dict:
    return {"aid": AID, "account_sdk_source": "web", "sdk_version": "2.1.10-tiktok",
            "language": "en", "verifyFp": VERIFY_FP, "msToken": ""}


def jwt_exp(token: str) -> int:
    """解 JWT exp（unix）。失败 0。实测 Cloud-IDE JWT TTL≈8h。"""
    try:
        p = str(token).split(".")[1]
        p += "=" * (-len(p) % 4)
        return int(json.loads(base64.urlsafe_b64decode(p)).get("exp") or 0)
    except Exception:
        return 0


class TraeTokenExchangeError(RuntimeError):
    """密码/会话 → token 交换失败（含阶段标记）。"""


class TraeTokenExchange:
    """用已注册账号（email+password）换 Cloud-IDE JWT（requests 原生，零签名头）。

    proxy: 形如 http://127.0.0.1:7897；None=直连。
    用法:
        ex = TraeTokenExchange(proxy="http://127.0.0.1:7897")
        out = ex.exchange(email="...", password="...")
        out = {"user_id","token","region","ai_pay_host","cashier_url",...}
    """

    def __init__(self, *, proxy: Optional[str] = None,
                 log_fn: Callable[[str], None] = print, timeout: float = 30.0):
        self.log = log_fn
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": _UA,
            "Accept-Language": "en-US,en;q=0.9",
        })
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})
        self.ide_host = ""          # region 下发的 IDE API 域
        self.country_code = ""

    # ---------- 内部 ----------

    def _hdrs(self, *, json_ct: bool = False, origin: str = WWW, referer: str = f"{WWW}/") -> dict:
        h = {"Origin": origin, "Referer": referer}
        if json_ct:
            h["Content-Type"] = "application/json"
        return h

    def _err_code(self, j: dict) -> str:
        try:
            return str(((j.get("ResponseMetadata") or {}).get("Error") or {}).get("Code") or "")
        except Exception:
            return ""

    # ---------- 步骤 ----------

    def step1_region(self) -> str:
        """region 下发域（决定后续 IDE 面打哪个 host）。失败不阻断，回落 sg。"""
        body = {"hashed_id": "0" * 64, "temporary_id": "1" * 32, "type": "2"}
        try:
            r = self.s.post(f"{REGION_HOST}/passport/web/region/",
                            params=_passport_query(), data=body,
                            timeout=self.timeout, headers=self._hdrs())
            d = (r.json().get("data") or {})
            self.ide_host = str(d.get("domain") or "")
            self.country_code = str(d.get("country_code") or "")
        except Exception as e:
            self.log(f"region 观察失败（回落 sg 域）: {str(e)[:120]}")
        if not self.ide_host:
            self.ide_host = "https://ug-normal.trae.ai"
        self.log(f"region 下发 IDE 域: {self.ide_host} (country={self.country_code or '-'})")
        return self.ide_host

    def step2_login(self, email: str, password: str) -> str:
        """密码直登（仅 www.trae.ai 同源可用）→ user_id。

        error_code=7 'Maximum number of attempts reached' = 同 IP 登录尝试过载，
        换出口 IP 或等待冷却（非密码错误；错误密码本身返回 error_code=17）。
        """
        r = self.s.post(f"{WWW}/passport/web/email/login/",
                        params=_passport_query(),
                        data={"type": "1", "email": email, "password": password},
                        timeout=self.timeout, headers=self._hdrs())
        try:
            j = r.json()
        except Exception:
            raise TraeTokenExchangeError(
                f"[step2_login] HTTP {r.status_code} 非 JSON: {r.text[:200]}")
        d = j.get("data") or {}
        user_id = str(d.get("user_id_str") or d.get("user_id") or "")
        if r.status_code != 200 or (j.get("message") != "success" and not user_id):
            err = d.get("error_code") or d.get("description") or ""
            raise TraeTokenExchangeError(
                f"[step2_login] HTTP {r.status_code} message={j.get('message')} "
                f"error_code={err} body={json.dumps(j, ensure_ascii=False)[:200]}")
        self.log(f"密码登录 OK user_id={user_id or '-'}")
        return user_id

    def step3_trae_login(self) -> None:
        """建立 IDE 会话（region 下发域）。"""
        r = self.s.post(f"{self.ide_host}/cloudide/api/v3/trae/Login",
                        params={"type": "email"},
                        json={"UtmSource": "", "UtmMedium": "", "UtmCampaign": "",
                              "UtmTerm": "", "UtmContent": "", "BDVID": "",
                              "LoginChannel": "ide_platform"},
                        timeout=self.timeout, headers=self._hdrs(json_ct=True))
        if r.status_code != 200:
            raise TraeTokenExchangeError(
                f"[step3_trae_login] HTTP {r.status_code} code={self._err_code(r.json() if 'json' in (r.headers.get('content-type') or '') else {})} "
                f"body={r.text[:160]}")
        self.log("trae/Login OK")

    def step4_get_token(self) -> str:
        """GetUserToken → Cloud-IDE JWT。"""
        r = self.s.post(f"{self.ide_host}/cloudide/api/v3/common/GetUserToken",
                        json={}, timeout=self.timeout, headers=self._hdrs(json_ct=True))
        try:
            j = r.json()
        except Exception:
            raise TraeTokenExchangeError(
                f"[step4_get_token] HTTP {r.status_code} 非 JSON: {r.text[:200]}")
        token = str(((j.get("Result") or {}).get("Token")) or "")
        if not token:
            raise TraeTokenExchangeError(
                f"[step4_get_token] HTTP {r.status_code} code={self._err_code(j)} "
                f"body={json.dumps(j, ensure_ascii=False)[:200]}")
        self.log(f"Cloud-IDE JWT {_mask(token)}")
        return token

    def step5_check_login(self, token: str) -> dict:
        """CheckLogin 验 token（IsLogin/UserID/Region）。"""
        r = self.s.post(f"{self.ide_host}/cloudide/api/v3/trae/CheckLogin",
                        json={"GetAIPayHost": True, "GetNickNameEditStatus": True},
                        timeout=self.timeout,
                        headers={**self._hdrs(json_ct=True),
                                 "Authorization": f"Cloud-IDE-JWT {token}"})
        try:
            j = r.json()
        except Exception:
            return {}
        res = j.get("Result") or {}
        self.log(f"CheckLogin IsLogin={res.get('IsLogin')} UserID={res.get('UserID')} "
                 f"Region={res.get('Region')}")
        return res

    def step6_create_order(self, token: str) -> str:
        """token 活性试金石（core.py 口径）：200+cashier_url=token 真实可用。"""
        try:
            r = self.s.post(f"{self.ide_host}/trae/api/v1/pay/create_order",
                            json={"product_ids": ["2"],
                                  "result_url": "https://www.trae.ai/account-setting"
                                                "?type=upgrade&identity=1#subscription"},
                            timeout=self.timeout,
                            headers={**self._hdrs(json_ct=True),
                                     "Authorization": f"Cloud-IDE-JWT {token}"})
            if r.status_code == 200:
                cashier = str(((r.json().get("order_info") or {}).get("cashier_url")) or "")
                self.log(f"create_order 200 cashier_url={_mask(cashier, 24)}")
                return cashier
            self.log(f"create_order HTTP {r.status_code}（试金石未过，token 可能受限）")
        except Exception as e:
            self.log(f"create_order 失败: {str(e)[:120]}")
        return ""

    # ---------- 会话直换（注册后同 session，无需密码登录，绕开 error_code=7） ----------

    def exchange_from_session(self, session: requests.Session, *,
                              user_id: str = "", do_check: bool = True,
                              do_order: bool = True) -> dict:
        """用「已登录的 requests.Session」（注册/密码登录后的同一个 session）换 token。

        只走 ①region → ③trae/Login → ④GetUserToken（可选 ⑤⑥验证），**不触发密码登录限流**。
        这是注册链尾部应调用的入口（协议线注册后同 session 直接接这里）。
        """
        self.s = session
        if not self.s.headers.get("User-Agent"):
            self.s.headers["User-Agent"] = _UA
        t0 = time.monotonic()
        self.step1_region()
        self.step3_trae_login()
        token = self.step4_get_token()
        result = self.step5_check_login(token) if do_check else {}
        cashier_url = self.step6_create_order(token) if do_order else ""
        if not user_id:
            user_id = self._jwt_user_id(token)
        out = {
            "user_id": user_id, "token": token, "token_exp": jwt_exp(token),
            "region": str(result.get("Region") or ""),
            "ai_pay_host": str(result.get("AIPayHost") or ""),
            "cashier_url": cashier_url, "ide_host": self.ide_host,
            "country_code": self.country_code,
            "elapsed_sec": round(time.monotonic() - t0, 1),
        }
        self.log(f"✓ 会话 token 成功 user_id={user_id or '-'} region={out['region'] or '-'} "
                 f"exp={out['token_exp']} 用时 {out['elapsed_sec']}s")
        return out

    # ---------- B 通道：模型网关 token（未打通，best-effort） ----------

    def model_isusable(self, token: str) -> dict:
        """探测 trae 模型网关是否接受该 token（x-ide-token）。返回 {host,status,code,ok}。"""
        d = {"host": "", "status": 0, "code": "", "ok": False}
        uid = self._jwt_user_id(token)
        for host in self._model_hosts():
            try:
                r = self.s.get(f"{host}/api/ide/v1/model_list?type=chat",
                               headers=self._model_headers(token, user_id=uid),
                               timeout=self.timeout)
                code = ""
                try:
                    code = str((r.json() or {}).get("code") or "")
                except Exception:
                    pass
                d = {"host": host, "status": r.status_code, "code": code, "ok": r.status_code == 200}
                if r.status_code == 200:
                    return d
            except Exception as e:
                d = {"host": host, "status": 0, "code": str(e)[:80], "ok": False}
        return d

    def _model_hosts(self) -> list:
        cc = (self.country_code or "us").lower()
        h = [MODEL_HOSTS_BY_REGION.get(cc, MODEL_HOSTS_BY_REGION["sg"]),
             MODEL_HOSTS_BY_REGION["us"], MODEL_HOSTS_BY_REGION["sg"]]
        seen, out = set(), []
        for x in h:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    def _model_headers(self, token: str, *, user_id: str = "") -> dict:
        import uuid
        return {
            "Content-Type": "application/json",
            "Authorization": f"Cloud-IDE-JWT {token}",
            "X-Cloudide-Token": token,
            "x-ide-token": token,
            "x-app-id": MODEL_APP_ID, "x-app-version": "default",
            "x-app-version-code": "20260401", "x-ide-version-code": "20260401",
            "x-ide-version": "3.5.51", "x-ide-version-type": "stable",
            "x-custom-trace-id": uuid.uuid4().hex,
            "x-device-brand": "82RF", "x-device-cpu": "Intel",
            "x-device-id": str(abs(hash(uuid.uuid4().hex)) % 10 ** 19),
            "x-machine-id": uuid.uuid4().hex,
            "x-os-version": "Windows 10", "x-device-type": "windows",
            "request-traffic-type": "prod", "x-uid": user_id,
            "User-Agent": "Trae/3.5.51",
        }

    def get_refresh_token(self, client_id: str = OAUTH_CLIENT_ID_IDE,
                          host: str = WWW) -> str:
        """B 通道：GetRefreshToken（需 IDE OAuth 授权已授予）。"""
        r = self.s.post(f"{host.rstrip('/')}/cloudide/api/v3/trae/oauth/GetRefreshToken",
                        json={"clientID": client_id}, timeout=self.timeout,
                        headers={"Content-Type": "application/json",
                                 "Origin": WWW, "Referer": f"{WWW}/"})
        ct = r.headers.get("content-type") or ""
        if "json" not in ct:
            raise TraeTokenExchangeError(
                f"[GetRefreshToken] 非 JSON（{ct}）—— 无 IDE 授权，回落 SPA/被拒: {r.text[:120]}")
        j = r.json()
        rt = str(((j.get("Result") or {}).get("RefreshToken")) or "")
        if not rt:
            raise TraeTokenExchangeError(
                f"[GetRefreshToken] 无 RefreshToken code={self._err_code(j)} "
                f"body={json.dumps(j, ensure_ascii=False)[:200]}")
        self.log(f"RefreshToken {_mask(rt)}")
        return rt

    def exchange_ide_token(self, refresh_token: str,
                           client_id: str = OAUTH_CLIENT_ID_IDE) -> str:
        """B 通道：RefreshToken → IDE/模型 token（ExchangeToken）。"""
        r = self.s.post(f"{self.ide_host or 'https://api-sg-central.trae.ai'}"
                        f"/cloudide/api/v3/trae/oauth/ExchangeToken",
                        json={"ClientID": client_id, "RefreshToken": refresh_token,
                              "ClientSecret": "-", "UserID": ""},
                        timeout=self.timeout,
                        headers={"Content-Type": "application/json"})
        try:
            j = r.json()
        except Exception:
            raise TraeTokenExchangeError(
                f"[ExchangeToken] HTTP {r.status_code} 非 JSON: {r.text[:160]}")
        for container in (j.get("Result"), j.get("result"), j.get("data"), j):
            if isinstance(container, dict):
                tok = container.get("Token") or container.get("token")
                if tok:
                    self.log(f"IDE token {_mask(tok)}")
                    return str(tok)
        raise TraeTokenExchangeError(
            f"[ExchangeToken] 无 Token code={self._err_code(j)} "
            f"body={json.dumps(j, ensure_ascii=False)[:200]}")

    def mint_model_token(self, client_id: str = OAUTH_CLIENT_ID_IDE) -> str:
        """best-effort：web 会话 → GetRefreshToken → ExchangeToken → 模型 token。

        当前**被挡住**：web 密码会话无 IDE OAuth 授权，www 回落 SPA、ide 面 20310/20101。
        需补 GetLoginGuidance→/authorization(PKCE S256)→浏览器授权回调拿 authCode→ExchangeToken
        （见 ProjectEio/trae2api oauth.go）。拿不到就抛 TraeTokenExchangeError。
        """
        rt = self.get_refresh_token(client_id)
        return self.exchange_ide_token(rt, client_id)

    # ---------- 编排 ----------

    def exchange(self, email: str, password: str) -> dict:
        if not str(email or "").strip() or not str(password or ""):
            raise TraeTokenExchangeError("必须提供 email + password（已注册账号）")
        email = str(email).strip()
        t0 = time.monotonic()

        self.step1_region()
        user_id = self.step2_login(email, password)
        self.step3_trae_login()
        token = self.step4_get_token()
        result = self.step5_check_login(token)
        cashier_url = self.step6_create_order(token)

        # JWT payload 兜底解 user_id
        if not user_id:
            user_id = self._jwt_user_id(token)
        out = {
            "email": email,
            "password": password,
            "user_id": user_id,
            "token": token,
            "token_exp": jwt_exp(token),
            "region": str(result.get("Region") or ""),
            "ai_pay_host": str(result.get("AIPayHost") or ""),
            "cashier_url": cashier_url,
            "ide_host": self.ide_host,
            "country_code": self.country_code,
            "elapsed_sec": round(time.monotonic() - t0, 1),
        }
        self.log(f"✓ token 交换成功 {email} region={out['region'] or '-'} "
                 f"exp={out['token_exp']} cashier={'有' if cashier_url else '无'} "
                 f"用时 {out['elapsed_sec']}s")
        return out

    @staticmethod
    def _jwt_user_id(token: str) -> str:
        try:
            payload = str(token).split(".")[1]
            payload += "=" * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload))
            return str((data.get("data") or {}).get("id") or "")
        except Exception:
            return ""


# =========================================================================== #
# B 通道：IDE OAuth (PKCE) 授权 → authCode → ExchangeToken → ide_token
# --------------------------------------------------------------------------- #
# 2026-10-05 实测打通 acct 249（蓝图 ProjectEio/trae2api oauth.go）。时序：
#   1) POST {api.marscode.com|api.trae.ai}/.../GetLoginGuidance body {"loginTraceID"} → Result.LoginHost
#   2) build_verification_uri(): https://www.trae.ai/authorization?...&code_challenge=<S256>&
#        auth_callback_url=http://127.0.0.1:<port>/authorize
#   3) 浏览器打开：未登录 → /login（提交按钮是 <div class="...btn-submit..."> "Log in"，
#        不是 <button>；Playwright locator.click 会被遮挡卡死 → 用 JS .click()）；
#        登录后 /authorization 出现绿色 "Log in and open TRAE"（同为 .btn-submit）→ JS 点击
#        → 浏览器重定向到 auth_callback_url，带
#        ?isRedirect=true&scope=trae&authCodeInfo={"AuthCode":..,"ExpireAt":..}&host=..&userRegion=..
#      【坑1】回调把 code 放在 authCodeInfo（JSON 字符串）里，不是 code/authCode 参数。
#   4) POST {回调 host}/trae/api/v3/oauth/ExchangeToken
#        body {ClientID, AuthCode, CodeVerifier, DeviceInfo(ECDSA-P256 公钥 PEM), IDEVersion}
#        → Result.Token = ide_token（JWT: data.source="refresh_token"）
#      【坑2】必须打回调下发的 host（region IDE 域，如 https://ug-normal.us.trae.ai）；
#             grow-normal/growsg 打这个接口是 400。
#   5) 模型网关：ide_token 打遗留 /api/ide/v1/model_list 一律 401/1001（coreva/coresg）。
#        现行网关用新版加密协议（AES-GCM body + pin/ts → /api/agent/v3/create_agent_task）；
#        见 docs/trae-B通道-ide_token-20261005.md。
# =========================================================================== #

GUIDANCE_URLS = (
    "https://api.marscode.com/cloudide/api/v3/trae/GetLoginGuidance",
    "https://api.trae.ai/cloudide/api/v3/trae/GetLoginGuidance",
)
IDE_AUTH_PATH = "/authorization"
EXCH_BY_AUTH_PATH = "/trae/api/v3/oauth/ExchangeToken"
OAUTH_APP_VERSION = "3.5.54"
DEFAULT_CALLBACK_PORT = 56120


def gen_pkce() -> tuple:
    """PKCE verifier/challenge（verifier=base64url(48B)=64 字符；challenge=base64url(sha256) 去 padding）。"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    return verifier, challenge


def get_login_guidance(trace_id: str, *, session: Optional[requests.Session] = None,
                       log: Callable[[str], None] = print) -> str:
    """GetLoginGuidance → LoginHost（实测 www.trae.ai）。多候选端点，全失败抛错。"""
    s = session or requests.Session()
    s.headers.update({"User-Agent": "Trae/1.0.0", "Accept": "application/json",
                      "Content-Type": "application/json"})
    errs = []
    for ep in GUIDANCE_URLS:
        try:
            r = s.post(ep, data=json.dumps({"loginTraceID": trace_id,
                                            "login_trace_id": trace_id}), timeout=20)
            if "json" not in (r.headers.get("content-type") or ""):
                errs.append(f"{ep.split('/')[2]}=non-json")
                continue
            j = r.json()
            for c in (j.get("Result"), j.get("result"), j.get("data"), j):
                if isinstance(c, dict):
                    host = c.get("LoginHost") or c.get("loginHost") or c.get("LoginURL")
                    if host:
                        log(f"GetLoginGuidance OK LoginHost={host}")
                        return str(host)
            errs.append(f"{ep.split('/')[2]}=no-host")
        except Exception as e:  # noqa: BLE001
            errs.append(f"{ep.split('/')[2]}={type(e).__name__}")
    raise TraeTokenExchangeError("GetLoginGuidance failed: " + " | ".join(errs))


def build_verification_uri(login_host: str, trace_id: str, callback_url: str,
                           challenge: str, *, client_id: str = OAUTH_CLIENT_ID_IDE,
                           app_version: str = OAUTH_APP_VERSION) -> str:
    """构造 IDE OAuth 授权 URL（参数与 trae2api oauth.go buildVerificationURI 逐条对齐）。"""
    base = login_host if str(login_host).startswith("http") else "https://" + str(login_host)
    pairs = [
        ("login_version", "1"), ("auth_from", "trae"), ("login_channel", "native_ide"),
        ("plugin_version", "local"), ("auth_type", "local"), ("client_id", client_id),
        ("redirect", "0"), ("login_trace_id", trace_id), ("auth_callback_url", callback_url),
        ("machine_id", "0"), ("device_id", "0"), ("x_device_id", "0"), ("x_machine_id", "0"),
        ("x_device_brand", "PC"), ("x_device_type", "Windows"),
        ("x_os_version", "Windows 11 Pro"), ("x_env", ""),
        ("x_app_version", app_version), ("x_app_type", "stable"),
        ("code_challenge", challenge), ("code_challenge_method", "S256"),
    ]
    return base.rstrip("/") + IDE_AUTH_PATH + "?" + urlencode(pairs)


def device_public_key_pem() -> str:
    """本设备 ECDSA-P256 公钥 PEM（ExchangeToken 的 DeviceInfo.DevicePublicKey）。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def device_info(pub_pem: str, *, app_version: str = OAUTH_APP_VERSION) -> dict:
    return {"DeviceID": "0", "MachineID": "0", "PlatformCode": "IDE_PC",
            "DeviceType": "PC", "DeviceName": "trae2api", "DeviceModel": "PC",
            "ClientVersion": app_version, "DevicePublicKey": pub_pem,
            "DeviceBrand": "PC", "DeviceCPU": "", "OSInfo": "Windows",
            "OSVersion": "Windows 11 Pro"}


def parse_authcode_callback(query: dict) -> dict:
    """从回调 query 解 code/host/region。code 常在 authCodeInfo(JSON 字符串) 里。"""
    def pick(keys):
        return next((query[k] for k in keys if query.get(k)), "")

    code = pick(("authCode", "auth_code", "AuthCode", "authorization_code", "code"))
    if not code:
        info = pick(("authCodeInfo", "auth_code_info", "AuthCodeInfo"))
        if info:
            try:
                m = json.loads(info)
                for c in (m, m.get("Result"), m.get("result"), m.get("data")):
                    if isinstance(c, dict):
                        code = str(c.get("AuthCode") or c.get("authCode")
                                   or c.get("auth_code") or c.get("code") or "")
                        if code:
                            break
            except Exception:  # noqa: BLE001
                code = info
    return {"code": code,
            "refresh": pick(("refreshToken", "refresh_token", "RefreshToken")),
            "host": pick(("host", "login_host", "loginHost")),
            "region": pick(("userRegion", "login_region", "region")),
            "raw_query": query}


def exchange_ide_token_by_authcode(auth_code: str, code_verifier: str, *,
                                   ide_host: str = "", region: str = "",
                                   client_id: str = OAUTH_CLIENT_ID_IDE,
                                   app_version: str = OAUTH_APP_VERSION,
                                   proxy: Optional[str] = None,
                                   log: Callable[[str], None] = print) -> str:
    """authCode + CodeVerifier + DeviceInfo → ide_token（JWT, data.source="refresh_token"）。

    ide_host = 回调下发的 host（region IDE 域，如 https://ug-normal.us.trae.ai）；
    缺失时按 region 兜底 grow-normal/growsg-normal（实测这两个 400）。
    """
    body = json.dumps({"ClientID": client_id, "AuthCode": auth_code,
                       "CodeVerifier": code_verifier,
                       "DeviceInfo": device_info(device_public_key_pem(),
                                                 app_version=app_version),
                       "IDEVersion": app_version})
    origins = []
    if ide_host:
        origins.append(ide_host if str(ide_host).startswith("http") else "https://" + str(ide_host))
    origins.append("https://grow-normal.trae.ai")
    if str(region or "").lower() in ("us", "sg"):
        origins.append("https://growsg-normal.trae.ai")
    origins += ["https://growsg-normal.trae.ai", "https://grow-normal.trae.ai"]
    s = requests.Session()
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    last = ""
    for o in dict.fromkeys(x.rstrip("/") for x in origins if x):
        try:
            r = s.post(o + EXCH_BY_AUTH_PATH, data=body, timeout=30,
                       headers={"Accept": "application/json", "Content-Type": "application/json",
                                "x-cloudide-token": ""})
            if r.status_code == 200:
                j = r.json()
                for c in (j.get("Result"), j.get("result"), j.get("data"), j):
                    if isinstance(c, dict):
                        tok = c.get("Token") or c.get("token") or c.get("accessToken")
                        if tok:
                            log(f"IDE token {_mask(str(tok))} via {o}")
                            return str(tok)
                last = f"{o} => 200 no Token {r.text[:120]}"
            else:
                last = f"{o} => HTTP {r.status_code} {r.text[:120]}"
        except Exception as e:  # noqa: BLE001
            last = f"{o} => {type(e).__name__}: {str(e)[:80]}"
    raise TraeTokenExchangeError("ExchangeToken(authCode) failed: " + last)


def authorize_via_browser(verify_uri: str, email: str, password: str, *,
                          callback_port: int = DEFAULT_CALLBACK_PORT,
                          profile_key: str = "trae_oauth_probe",
                          wait_sec: float = 210.0,
                          log: Callable[[str], None] = print) -> dict:
    """Camoufox（独立 profile）走 /authorization：登录(.btn-submit)→点绿色确认→回调拿 code。

    返回 parse_authcode_callback(...) 结果。页面操作只在自家窗口，不接管用户鼠标键盘。
    注意：make_context 返回 dict 的 "context" 键被 context_kw 覆盖，BrowserContext 实为 env["browser"]。
    """
    captured: dict = {}
    lock = threading.Lock()

    class _CB(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            with lock:
                captured.clear()
                captured.update(q)
            body = b"ok. trae oauth captured."
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # 静音
            pass

    httpd = HTTPServer(("127.0.0.1", callback_port), _CB)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="trae-oauth-cb").start()
    from core.browser_env import make_context
    env = make_context(profile_key=profile_key, headless=False, backend="camoufox",
                       persist=True, log=lambda _m: None)
    page = env["page"]
    try:
        def _vis(sels):
            for sel in sels:
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        return loc
                except Exception:
                    continue
            return None

        def _js_submit() -> bool:
            js = ("() => {const bs=[...document.querySelectorAll('div.btn-submit,.btn-submit')];"
                  "const b=bs.find(e=>/log\\s*in|sign\\s*in/i.test(e.textContent||''))||bs[0];"
                  "if(b){b.click();return true;} return false;}")
            try:
                return bool(page.evaluate(js))
            except Exception:
                return False

        page.goto(verify_uri, wait_until="domcontentloaded", timeout=45000)
        deadline = time.monotonic() + float(wait_sec)
        submitted = 0.0
        while time.monotonic() < deadline:
            with lock:
                if captured:
                    break
            em = _vis(['input[type="email"]', 'input[name="email"]'])
            pw = _vis(['input[type="password"]', 'input[name="password"]'])
            for loc, val in ((em, email), (pw, password)):
                if loc is not None:
                    try:
                        if not loc.input_value():
                            loc.fill(val)
                    except Exception:
                        pass
            if em is None and pw is None:
                if _js_submit():          # 授权页绿色 "Log in and open TRAE"
                    page.wait_for_timeout(2500)
                    continue
            elif (time.monotonic() - submitted) > 6:
                _js_submit()              # 登录页 "Log in"
                submitted = time.monotonic()
            page.wait_for_timeout(1200)
    finally:
        try:
            env["close"]()
        except Exception:
            pass
        httpd.shutdown()
    with lock:
        q = dict(captured)
    if not q:
        raise TraeTokenExchangeError("browser authorize timeout: no callback captured")
    return parse_authcode_callback(q)


def mint_model_token_via_oauth(email: str, password: str, *,
                               proxy: Optional[str] = None,
                               profile_key: str = "trae_oauth_probe",
                               callback_port: int = DEFAULT_CALLBACK_PORT,
                               wait_sec: float = 210.0,
                               log: Callable[[str], None] = print) -> str:
    """完整 B 通道：GetLoginGuidance → PKCE → 浏览器授权 → ExchangeToken → ide_token。

    需要浏览器（Camoufox）。ide_token 拿到后再打模型网关仍需新版加密协议（见文档）。
    """
    trace_id = uuid.uuid4().hex
    verifier, challenge = gen_pkce()
    host = get_login_guidance(trace_id, log=log)
    callback = f"http://127.0.0.1:{callback_port}/authorize"
    uri = build_verification_uri(host, trace_id, callback, challenge)
    res = authorize_via_browser(uri, email, password, callback_port=callback_port,
                                profile_key=profile_key, wait_sec=wait_sec, log=log)
    if not res.get("code"):
        raise TraeTokenExchangeError("no authCode in callback: " + json.dumps(res)[:200])
    return exchange_ide_token_by_authcode(res["code"], verifier,
                                          ide_host=res.get("host", ""),
                                          region=res.get("region", ""),
                                          proxy=proxy, log=log)


# =========================================================================== #
# B 通道（真客户端）：模型网关可接受的 ide_token 重铸 —— 2026-10-06 实测打通
# --------------------------------------------------------------------------- #
# 真因（见 docs/trae-1001排查-20261006.md）：模型网关只认「客户端 OAuth client
# （api.marscode.com / 95jjo8vw8wdyjn / Codeverse）」签发的 ide_token；本仓早前用
# IDE client ono9krqynydwx5 + ClientSecret='-' 铸的 token 在 cloudide 侧有效，
# 但模型网关一律 401 code 1001。改用真 client 重铸后 model_list 直接 200。
#
# 实测时序（acct249，2026-10-06）：
#   1) 种 = 一枚有效的 ide_token（source=refresh_token，如早前 authCode 路径所得）
#   2) POST {api.marscode.com}/cloudide/api/v3/trae/oauth/GetRefreshToken
#        body {"clientID":"95jjo8vw8wdyjn"} + 头 Authorization/x-ide-token: Cloud-IDE-JWT <种>
#        → Result.RefreshToken（len=61）   【host 用 ug-normal.us 也行，返回同形】
#   3) POST {api.marscode.com}/cloudide/api/v3/trae/oauth/ExchangeToken
#        body {ClientID:95jjo8vw8wdyjn, RefreshToken, ClientSecret:<真>, Name:'Codeverse', UserID:''}
#        → Result.Token = 新 ide_token（len=1008），网关接受
#      【踩点】同 body 打 ug-normal.us.trae.ai → 401 20101 "refresh token is invalid"
#              （refresh 作用域按签发主机隔离）；ClientSecret='-' → 401 20107 secret invalid。
#   4) 验证：GET {coreva|coresg}-normal.trae.ai/api/ide/v1/model_list?type=chat → 200
#             POST .../api/cue_agent/v3/create_agent_task → 推理步产 token_usage + 模型文本（不再 1001）
# =========================================================================== #

MARSCODE_HOST = "https://api.marscode.com"
MARSCODE_CLIENT_ID = "95jjo8vw8wdyjn"
MARSCODE_CLIENT_NAME = "Codeverse"
MARSCODE_GET_REFRESH_PATH = "/cloudide/api/v3/trae/oauth/GetRefreshToken"
MARSCODE_EXCHANGE_PATH = "/cloudide/api/v3/trae/oauth/ExchangeToken"
#: GetRefreshToken/ExchangeToken 的 host 故障回落链（沿用 GUIDANCE_URLS / _model_hosts 的
#: 「多 host 遍历」风格）。2026-10-06 实测：api.marscode.com 后端长期 502/500 panic（前端
#: 仍回 HTML/半截 JSON），而**同一条 refresh 流程**改打 api.trae.ai（同一 client、同一
#: RefreshToken、同一路径）等价可用 → 首选 marscode，5xx/超时/DNS 败即自动回落 trae.ai。
#: RefreshToken 网上做一次换不到 token、而是拿到新 RefreshToken 时，回写这个键（长效种）。
MARSCODE_HOST_FALLBACKS = ("https://api.marscode.com", "https://api.trae.ai")


def _mint_hosts(primary: str) -> list:
    """按序去重给出 GetRefreshToken/ExchangeToken 的候选 host（primary → trae.ai → marscode）。

    与 TraeTokenExchange._model_hosts() 同构：首个成功即止，5xx/超时/DNS 败换下一个。
    """
    out: list = []
    for h in (primary, "https://api.trae.ai", "https://api.marscode.com"):
        h = str(h or "").rstrip("/")
        if h and h not in out:
            out.append(h)
    return out


class _RetryableHostError(Exception):
    """host 级瞬时故障（5xx/超时/DNS/非 JSON）→ 触发换下一个 host 重试。"""
#: 客户端 OAuth client 常量文件（值不落代码/日志；见 scratch/trae_marscode_client.env）
_MARSCODE_ENV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scratch", "trae_marscode_client.env")


def load_marscode_client(path: Optional[str] = None) -> dict:
    """读真客户端 OAuth client 常量（默认 scratch/trae_marscode_client.env）。

    返回 {client_id, client_secret, name, host}；缺文件时回落到内置默认（secret 空）。
    """
    cfg = {"client_id": MARSCODE_CLIENT_ID, "client_secret": "",
           "name": MARSCODE_CLIENT_NAME, "host": MARSCODE_HOST}
    try:
        with open(path or _MARSCODE_ENV, "r", encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                k, v = k.strip(), v.strip()
                if k == "TRAE_MARSCODE_OAUTH_URL":
                    cfg["host"] = "https://" + v.replace("https://", "").replace("http://", "").rstrip("/")
                elif k == "TRAE_MARSCODE_CLIENT_ID":
                    cfg["client_id"] = v
                elif k == "TRAE_MARSCODE_CLIENT_SECRET":
                    cfg["client_secret"] = v
                elif k == "TRAE_MARSCODE_CLIENT_NAME":
                    cfg["name"] = v
    except Exception:
        pass
    return cfg


def mint_token_via_marscode(ide_token: str, *, user_id: str = "",
                            host: str = "", client_id: str = "",
                            client_secret: str = "", name: str = "",
                            session: Optional[requests.Session] = None,
                            log: Callable[[str], None] = print) -> dict:
    """真客户端重铸模型网关 token：GetRefreshToken → ExchangeToken（refresh 路径）。

    与 mint_model_token_via_oauth 的区别：那条走 authCode + 旧 IDE client；本路径用
    客户端真 client（95jjo8vw8wdyjn / Codeverse @ api.marscode.com），**无需浏览器**，
    只需一枚已授权的 ide_token 作种（source=refresh_token）。

    **host 故障回落（炸弹①修复）**：GetRefreshToken+ExchangeToken 作为一对，在候选 host 上
    逐个尝试（api.marscode.com → api.trae.ai）。marscode 5xx/超时/DNS 败即自动改打 trae.ai，
    同一 refresh 流程等价可用（2026-10-06 实测）。返回体里 `host` = 实际成功的 host，
    供续期链把它与 RefreshToken 一起持久化（RefreshToken 按签发 host 隔离）。
    返回 {token, refresh_token, client_id, host, token_exp, user_id}。
    """
    cfg = load_marscode_client()
    primary = (host or cfg["host"]).rstrip("/")
    client_id = client_id or cfg["client_id"]
    client_secret = client_secret if client_secret != "" else cfg["client_secret"]
    name = name or cfg["name"]
    s = session or requests.Session()
    auth_headers = {"Content-Type": "application/json", "Accept": "application/json",
                    "x-cloudide-token": ide_token,
                    "Authorization": f"Cloud-IDE-JWT {ide_token}",
                    "x-ide-token": ide_token}

    last: Optional[str] = None
    for h in _mint_hosts(primary):
        try:
            return _refresh_pair(s, h, ide_token, auth_headers, client_id,
                                 client_secret, name, user_id, log)
        except _RetryableHostError as e:
            last = f"{h}: {e}"
            log(f"[host回落] {h} 失败（瞬时）→ 试下一个 host：{str(e)[:120]}")
            continue
        except TraeTokenExchangeError as e:
            last = f"{h}: {e}"
            log(f"[host回落] {h} 业务失败 → 试下一个 host：{str(e)[:120]}")
            continue
    raise TraeTokenExchangeError(f"mint_token_via_marscode 全 host 失败：{last}")


def _refresh_pair(s: requests.Session, host: str, ide_token: str, auth_headers: dict,
                  client_id: str, client_secret: str, name: str, user_id: str,
                  log: Callable[[str], None]) -> dict:
    """在单个 host 上完成 GetRefreshToken→ExchangeToken 一对；5xx/超时抛 _RetryableHostError。"""
    # 1) GetRefreshToken（用种 token 认领 refresh token）
    try:
        r = s.post(host + MARSCODE_GET_REFRESH_PATH,
                   json={"clientID": client_id}, headers=auth_headers, timeout=25)
    except Exception as e:  # 超时 / 连接 / DNS
        raise _RetryableHostError(f"GetRefreshToken {type(e).__name__}: {str(e)[:100]}")
    if r.status_code >= 500:
        raise _RetryableHostError(f"GetRefreshToken HTTP {r.status_code}")
    try:
        j = r.json()
    except Exception:
        raise _RetryableHostError(f"GetRefreshToken HTTP {r.status_code} 非 JSON: {r.text[:120]}")
    rt = str(((j.get("Result") or {}).get("RefreshToken")) or "")
    if not rt:
        raise TraeTokenExchangeError(
            f"[GetRefreshToken] HTTP {r.status_code} 无 RefreshToken "
            f"code={((j.get('ResponseMetadata') or {}).get('Error') or {}).get('Code')}")
    log(f"RefreshToken(real client) {_mask(rt)} via {host}")

    # 2) ExchangeToken（真 client + secret + Name=Codeverse）——必须与 GetRefreshToken 同 host
    try:
        r2 = s.post(host + MARSCODE_EXCHANGE_PATH,
                    json={"ClientID": client_id, "RefreshToken": rt,
                          "ClientSecret": client_secret, "Name": name, "UserID": ""},
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    timeout=25)
    except Exception as e:
        raise _RetryableHostError(f"ExchangeToken {type(e).__name__}: {str(e)[:100]}")
    if r2.status_code >= 500:
        raise _RetryableHostError(f"ExchangeToken HTTP {r2.status_code}")
    try:
        j2 = r2.json()
    except Exception:
        raise _RetryableHostError(f"ExchangeToken HTTP {r2.status_code} 非 JSON: {r2.text[:120]}")
    res = j2.get("Result") or {}
    tok = str(res.get("Token") or res.get("AccessToken") or "")
    if not tok:
        err = ((j2.get("ResponseMetadata") or {}).get("Error") or {})
        raise TraeTokenExchangeError(
            f"[ExchangeToken] HTTP {r2.status_code} 无 Token code={err.get('Code')} "
            f"msg={str(err.get('Message'))[:120]}")
    log(f"模型网关 ide_token(real client) {_mask(tok)} via {host}")
    return {"token": tok, "refresh_token": str(res.get("RefreshToken") or rt),
            "client_id": client_id, "host": host, "token_exp": jwt_exp(tok),
            "user_id": user_id or TraeTokenExchange._jwt_user_id(tok)}


def mint_token_via_refresh_token(refresh_token: str, *, host: str = "",
                                 client_id: str = "", client_secret: str = "",
                                 name: str = "", session: Optional[requests.Session] = None,
                                 log: Callable[[str], None] = print) -> dict:
    """**长效种子链（炸弹②修复）**：直接用持久化的 RefreshToken 走 ExchangeToken 换新 token。

    不经 GetRefreshToken，也不依赖会过期的 A 通道 ide_token——RefreshToken 由上游按签发 host
    隔离，故按 `host`（签发时记录的 host）优先，其次 trae.ai / marscode 逐个回落。
    仍返回新的 refresh_token（上游可能轮换），调用方应回写台账，形成自续链。
    """
    cfg = load_marscode_client()
    primary = (host or cfg["host"]).rstrip("/")
    client_id = client_id or cfg["client_id"]
    client_secret = client_secret if client_secret != "" else cfg["client_secret"]
    name = name or cfg["name"]
    s = session or requests.Session()
    last: Optional[str] = None
    for h in _mint_hosts(primary):
        try:
            r = s.post(h + MARSCODE_EXCHANGE_PATH,
                       json={"ClientID": client_id, "RefreshToken": refresh_token,
                             "ClientSecret": client_secret, "Name": name, "UserID": ""},
                       headers={"Content-Type": "application/json", "Accept": "application/json"},
                       timeout=25)
            if r.status_code >= 500:
                raise _RetryableHostError(f"ExchangeToken HTTP {r.status_code}")
            try:
                j = r.json()
            except Exception:
                raise _RetryableHostError(f"ExchangeToken HTTP {r.status_code} 非 JSON")
            res = j.get("Result") or {}
            tok = str(res.get("Token") or res.get("AccessToken") or "")
            if not tok:
                err = ((j.get("ResponseMetadata") or {}).get("Error") or {})
                raise TraeTokenExchangeError(
                    f"HTTP {r.status_code} code={err.get('Code')} "
                    f"msg={str(err.get('Message'))[:100]}")
            log(f"续期(长效种) 新 token {_mask(tok)} via {h}")
            return {"token": tok, "refresh_token": str(res.get("RefreshToken") or refresh_token),
                    "client_id": client_id, "host": h, "token_exp": jwt_exp(tok),
                    "user_id": TraeTokenExchange._jwt_user_id(tok)}
        except _RetryableHostError as e:
            last = f"{h}: {e}"
            log(f"[host回落] 续期 {h} 瞬时失败 → 下一个：{str(e)[:100]}")
        except TraeTokenExchangeError as e:
            last = f"{h}: {e}"
            log(f"[host回落] 续期 {h} 业务失败 → 下一个：{str(e)[:100]}")
    raise TraeTokenExchangeError(f"mint_token_via_refresh_token 全 host 失败：{last}")



def model_list_status(token: str, *, user_id: str = "",
                      hosts: Optional[list] = None,
                      session: Optional[requests.Session] = None,
                      log: Callable[[str], None] = print) -> dict:
    """用 ide_token 打模型网关遗留探测端点 /api/ide/v1/model_list 判 token 是否被接受。

    返回 {ok, status, host, code}；首个 200 即 ok。旧 client 的 token 恒 401 code 1001。
    """
    s = session or requests.Session()
    uid = user_id or TraeTokenExchange._jwt_user_id(token)
    cand = hosts or [MODEL_HOSTS_BY_REGION["us"], MODEL_HOSTS_BY_REGION["sg"]]
    hdrs = {"Content-Type": "application/json", "Authorization": f"Cloud-IDE-JWT {token}",
            "x-ide-token": token, "X-Cloudide-Token": token, "x-app-id": MODEL_APP_ID,
            "x-app-version": "default", "x-app-version-code": "20260101",
            "x-ide-version-code": "20260101", "x-ide-version": "3.5.54",
            "x-ide-version-type": "stable", "request-traffic-type": "prod",
            "x-uid": uid, "User-Agent": "Trae/3.5.51"}
    last = {"ok": False, "status": 0, "host": "", "code": ""}
    for h in cand:
        try:
            r = s.get(f"{h.rstrip('/')}/api/ide/v1/model_list?type=chat",
                      headers=hdrs, timeout=20)
            try:
                code = str((r.json() or {}).get("code") or "")
            except Exception:
                code = ""
            last = {"ok": r.status_code == 200, "status": r.status_code, "host": h, "code": code}
            log(f"model_list {h} HTTP {r.status_code} code={code or '-'}")
            if last["ok"]:
                return last
        except Exception as e:  # noqa: BLE001
            last = {"ok": False, "status": 0, "host": h, "code": type(e).__name__}
    return last


def mint_model_token_real_client(ide_token: str, *, verify: bool = True,
                                 log: Callable[[str], None] = print) -> dict:
    """一条龙：真 client 重铸 + （可选）model_list 验证。entry 供注册链/接线复用。"""
    out = mint_token_via_marscode(ide_token, log=log)
    if verify:
        out["model_list"] = model_list_status(out["token"], log=log)
    return out


__all__ = ["TraeTokenExchange", "TraeTokenExchangeError", "_mask", "jwt_exp",
           "MODEL_HOSTS_BY_REGION", "MODEL_APP_ID", "OAUTH_CLIENT_ID_IDE",
           "OAUTH_CLIENT_ID_SOLO", "gen_pkce", "get_login_guidance",
           "build_verification_uri", "device_public_key_pem", "device_info",
           "parse_authcode_callback", "exchange_ide_token_by_authcode",
           "authorize_via_browser", "mint_model_token_via_oauth",
           "MARSCODE_HOST", "MARSCODE_CLIENT_ID", "MARSCODE_CLIENT_NAME",
           "load_marscode_client", "mint_token_via_marscode",
           "mint_token_via_refresh_token", "MARSCODE_HOST_FALLBACKS",
           "model_list_status", "mint_model_token_real_client",
           "GUIDANCE_URLS", "EXCH_BY_AUTH_PATH", "OAUTH_APP_VERSION"]
