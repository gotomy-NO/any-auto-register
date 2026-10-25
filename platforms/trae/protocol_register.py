# -*- coding: utf-8 -*-
"""Trae.ai 纯协议注册线（零浏览器、零签名头）— 2026-10-05

依据（实测复现，见 docs/家宽重验雷达-20261005.md §一 v4 + scratch/trae_proto_r1.json）:
    www.trae.ai 同源 passport 端点当前不校验 X-Bogus/X-Gnarly 签名；
    ttwid 可经 POST /ttwid/register/ 纯协议自申领；msToken 非必需（空值可过）；
    verifyFp 传死值 verify_x 可过。error_code=17 的触发条件是打 ug-normal 域或同 URL 重放。

链路:
    1. POST https://www.trae.ai/ttwid/register/          → Set-Cookie ttwid（.trae.ai）
    2. POST https://www.trae.ai/passport/web/region/     → data.domain（下发域观察，可选）
    3. POST https://www.trae.ai/passport/web/email/send_code/
           ?aid=677332&account_sdk_source=web&sdk_version=2.1.10-tiktok&language=en
           &verifyFp=verify_x&msToken=
           body: type=1&email=<addr>&password=&email_logic_type=2
       → 200 {"data":{"email":"...","email_ticket":"..."},"message":"success"}
    4. otp_callback() 轮询邮箱取 6 位验证码
    5. POST https://www.trae.ai/passport/web/email/register_verify_login/
           同 query；body: type=1&email=<addr>&password=<pw>&code=<otp>&email_logic_type=2
       → user_id
    6. POST https://ug-normal.trae.ai/cloudide/api/v3/trae/Login?type=email  （IDE session）
    7. POST https://api-sg-central.trae.ai/cloudide/api/v3/common/GetUserToken → Result.Token
    8. POST https://ug-normal.trae.ai/cloudide/api/v3/trae/CheckLogin → UserId/Region

成功判定: Cloud-IDE JWT 非空（同 core.py / browser_flow.py 口径，不假成功）。
纪律: token/密码只 _mask 留前缀+长度；per-email 限流(error 1206)——失败重试必须换邮箱。
"""
from __future__ import annotations

import base64
import json
import random
import re
import string
import time
from typing import Callable, Optional

import requests

TRAE_WWW = "https://www.trae.ai"
BASE_URL = "https://ug-normal.trae.ai"          # cloudide API 面（与 core.py 一致）
API_SG = "https://api-sg-central.trae.ai"
AID = "677332"
SDK_VER = "2.1.10-tiktok"
VERIFY_FP = "verify_x"                          # 实测服务端不校验具体值
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def _mask(value: str, keep: int = 4) -> str:
    value = str(value or "")
    return f"{value[:keep]}...<len={len(value)}>" if value else ""


def _rand_password(n: int = 14) -> str:
    pw = [random.choice(string.ascii_uppercase),
          random.choice(string.ascii_lowercase),
          random.choice(string.digits)]
    pw += random.choices(string.ascii_letters + string.digits, k=max(0, n - 4))
    random.shuffle(pw)
    return "".join(pw) + "!"


def _passport_query() -> dict:
    return {"aid": AID, "account_sdk_source": "web", "sdk_version": SDK_VER,
            "language": "en", "verifyFp": VERIFY_FP, "msToken": ""}


class TraeProtocolError(RuntimeError):
    """纯协议注册失败（含阶段标记）。"""


class TraeProtocolRegister:
    """Trae.ai 纯协议注册编排器（requests 原生 TLS，无签名头）。

    proxy: 形如 http://127.0.0.1:7897；None=直连。
    otp_callback: send_code 成功后调用，须返回 6 位验证码（空/None 视为超时失败）。
    """

    def __init__(self, *, proxy: Optional[str] = None, log_fn: Callable[[str], None] = print,
                 otp_timeout_sec: float = 180.0):
        self.log = log_fn
        self.otp_timeout_sec = float(otp_timeout_sec)
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": _UA,
            "Accept-Language": "en-US,en;q=0.9",
        })
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})
        # 阶段观测（写台账/对比浏览器线）
        self.passport_domain = ""
        self.country_code = ""
        self.email_ticket = ""

    # ---------- passport 面（www.trae.ai 同源，零签名头） ----------

    def step1_ttwid(self) -> str:
        body = {"aid": int(AID), "service": "www.trae.ai", "unionHost": "",
                "union": False, "needFid": False, "fid": ""}
        r = self.s.post(f"{TRAE_WWW}/ttwid/register/", json=body, timeout=25,
                        headers={"Content-Type": "application/json",
                                 "Origin": TRAE_WWW, "Referer": f"{TRAE_WWW}/"})
        ttwid = self.s.cookies.get("ttwid", domain=".trae.ai") or self.s.cookies.get("ttwid") or ""
        if r.status_code != 200 or not ttwid:
            raise TraeProtocolError(
                f"[step1_ttwid] HTTP {r.status_code} body={r.text[:200]} ttwid={'有' if ttwid else '无'}")
        self.log(f"ttwid 自申领 OK ({_mask(ttwid, 12)})")
        return ttwid

    def step2_region(self) -> dict:
        """可选观察步：region 下发域。失败不阻断（雷达实测 send_code 不依赖 region）。"""
        try:
            body = {"hashed_id": "".join(random.choices("0123456789abcdef", k=64)),
                    "temporary_id": "".join(random.choices("0123456789abcdef", k=32)),
                    "type": "2"}
            r = self.s.post(f"{TRAE_WWW}/passport/web/region/", data=body, timeout=25,
                            headers={"Content-Type": "application/x-www-form-urlencoded",
                                     "Origin": TRAE_WWW, "Referer": f"{TRAE_WWW}/sign-up"})
            j = r.json()
            d = j.get("data") or {}
            self.passport_domain = str(d.get("domain") or "")
            self.country_code = str(d.get("country_code") or "")
            self.log(f"region 下发域: {self.passport_domain or '-'} "
                     f"(country={self.country_code or '-'})")
            return d
        except Exception as e:
            self.log(f"region 观察失败（不阻断）: {str(e)[:120]}")
            return {}

    def step3_send_code(self, email: str) -> str:
        self.log(f"发送验证码 → {email}")
        r = self.s.post(f"{TRAE_WWW}/passport/web/email/send_code/",
                        params=_passport_query(),
                        data={"type": "1", "email": email, "password": "",
                              "email_logic_type": "2"},
                        timeout=25,
                        headers={"Content-Type": "application/x-www-form-urlencoded",
                                 "Origin": TRAE_WWW, "Referer": f"{TRAE_WWW}/sign-up",
                                 "Accept": "application/json, text/plain, */*"})
        try:
            j = r.json()
        except Exception:
            raise TraeProtocolError(f"[step3_send_code] HTTP {r.status_code} 非 JSON: {r.text[:200]}")
        if r.status_code != 200 or j.get("message") != "success":
            raise TraeProtocolError(
                f"[step3_send_code] HTTP {r.status_code} error_code={j.get('error_code')} "
                f"body={json.dumps(j, ensure_ascii=False)[:220]}")
        self.email_ticket = str((j.get("data") or {}).get("email_ticket") or "")
        self.log(f"send_code 200 success ticket={_mask(self.email_ticket, 8)}")
        return self.email_ticket

    def wait_otp(self, otp_callback: Callable[[], str]) -> str:
        code = str(otp_callback() or "").strip()
        if not code:
            raise TraeProtocolError("[wait_otp] 未取到验证码（otp_callback 返回空）")
        self.log(f"验证码: {code}")
        return code

    def step5_register(self, email: str, password: str, otp: str) -> str:
        self.log("提交注册 register_verify_login ...")
        r = self.s.post(f"{TRAE_WWW}/passport/web/email/register_verify_login/",
                        params=_passport_query(),
                        data={"type": "1", "email": email, "password": password,
                              "code": otp, "email_logic_type": "2"},
                        timeout=30,
                        headers={"Content-Type": "application/x-www-form-urlencoded",
                                 "Origin": TRAE_WWW, "Referer": f"{TRAE_WWW}/sign-up",
                                 "Accept": "application/json, text/plain, */*"})
        try:
            j = r.json()
        except Exception:
            raise TraeProtocolError(
                f"[step5_register] HTTP {r.status_code} 非 JSON: {r.text[:220]}")
        data = j.get("data") or {}
        user_id = str(data.get("user_id_str") or data.get("user_id") or "")
        if r.status_code != 200 or (j.get("message") != "success" and not user_id):
            raise TraeProtocolError(
                f"[step5_register] HTTP {r.status_code} error_code={j.get('error_code')} "
                f"body={json.dumps(j, ensure_ascii=False)[:220]}")
        self.log(f"注册 OK user_id={user_id or '-'} cookies={sorted(self.s.cookies.keys())}")
        return user_id

    # ---------- cloudide 面（ug-normal / api-sg-central） ----------

    def step6_trae_login(self) -> None:
        try:
            self.s.post(f"{BASE_URL}/cloudide/api/v3/trae/Login",
                        params={"type": "email"}, timeout=25,
                        json={"UtmSource": "", "UtmMedium": "", "UtmCampaign": "",
                              "UtmTerm": "", "UtmContent": "", "BDVID": "",
                              "LoginChannel": "ide_platform"})
            self.log("trae/Login 已调用")
        except Exception as e:
            self.log(f"trae/Login 失败（继续尝试 token）: {str(e)[:120]}")

    def step7_get_token(self) -> str:
        r = self.s.post(f"{API_SG}/cloudide/api/v3/common/GetUserToken", json={}, timeout=30)
        try:
            j = r.json()
        except Exception:
            raise TraeProtocolError(
                f"[step7_get_token] HTTP {r.status_code} 非 JSON: {r.text[:200]}")
        token = str(((j.get("Result") or {}).get("Token")) or "")
        if not token:
            raise TraeProtocolError(
                f"[step7_get_token] HTTP {r.status_code} 无 Token: {json.dumps(j, ensure_ascii=False)[:220]}")
        self.log(f"Cloud-IDE JWT {_mask(token)}")
        return token

    def step8_check_login(self, token: str) -> dict:
        try:
            r = self.s.post(f"{BASE_URL}/cloudide/api/v3/trae/CheckLogin",
                            json={"GetAIPayHost": True, "GetNickNameEditStatus": True},
                            timeout=25,
                            headers={"Authorization": f"Cloud-IDE-JWT {token}"})
            j = r.json()
            return (j.get("Result") or {}) if isinstance(j, dict) else {}
        except Exception as e:
            self.log(f"CheckLogin 失败（不影响 token 有效性判定）: {str(e)[:120]}")
            return {}

    # ---------- 编排 ----------

    def register(self, email: str, password: Optional[str] = None,
                 otp_callback: Optional[Callable[[], str]] = None) -> dict:
        if not str(email or "").strip():
            raise TraeProtocolError("未提供邮箱地址")
        if otp_callback is None:
            raise TraeProtocolError("纯协议线必须提供 otp_callback（邮箱收码）")
        password = password or _rand_password()
        email = str(email).strip()

        t0 = time.monotonic()
        self.step1_ttwid()
        self.step2_region()
        self.step3_send_code(email)
        otp = self.wait_otp(otp_callback)
        user_id = self.step5_register(email, password, otp)
        self.step6_trae_login()
        token = self.step7_get_token()
        result8 = self.step8_check_login(token)
        if not user_id:
            user_id = _jwt_user_id(token)

        out = {
            "email": email,
            "password": password,
            "user_id": user_id,
            "token": token,
            "region": str(result8.get("Region") or ""),
            "cashier_url": "",
            "ai_pay_host": str(result8.get("AIPayHost") or ""),
            "auth_path": "protocol",
            "passport_domain": self.passport_domain,
            "country_code": self.country_code,
            "elapsed_sec": round(time.monotonic() - t0, 1),
        }
        self.log(f"✓ 注册成功 {email} region={out['region'] or '-'} "
                 f"用时 {out['elapsed_sec']}s")
        return out


def _jwt_user_id(token: str) -> str:
    try:
        payload = str(token).split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return str((data.get("data") or {}).get("id") or "")
    except Exception:
        return ""


__all__ = ["TraeProtocolRegister", "TraeProtocolError", "_mask", "_rand_password"]
