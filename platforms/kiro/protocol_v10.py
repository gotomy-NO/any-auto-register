"""Kiro / AWS Builder ID 注册 —— 纯协议 step 化实现（clean-room 重写）。

本模块照「协议事实」重写（端点 / 参数名 / 报文形状），不逐字拷贝任何 AGPL 源码：
上游参考实现为 AGPL-3.0，本文件仅借鉴其抓包得出的公开接口信息。

链路（每步独立可调，见 --start-step / --stop-after）：

    S1  kiro_init()          app.kiro.dev  InitiateLogin(CBOR)        → redirectUrl
    S2  resolve_wsh()        view→portal.sso 跳转链                    → workflowStateHandle / csrf
    S3  signin_user()        signin.aws /api/execute 身份输入          → signup redirect
    S4  signup_flow()        signin.aws /signup/api/execute 注册入口    → profile workflowID
    S5  telemetry_token()    vs.aws.amazon.com/token                   → awsd2c token / visitorId
    S6  profile_start()      profile.aws.amazon.com /api/start         → workflowState
    S7  send_otp()           profile /api/send-otp                     → 邮件发出
    S8  verify_otp()         profile /api/create-identity              → registrationCode/signInState
    S9  open_registration()  signup 页 + registrationCode              → 密码步公钥(JWE)
    S10 set_password()       JWE 加密密码提交                           → end-of-user-registration-success
    S11 final_login()        登录 workflow 收口                         → end-of-workflow-success
    S12 portal_tokens()      portal.sso/auth/sso-token → oidc → Kiro ExchangeToken(CBOR)
    S12f device_tokens()      OIDC device flow（aws-sdk-rust 口径）      → refreshToken/clientId/Secret

调试用法：
    # 只跑到第 2 步（不烧邮箱），验证连通与跳转链
    .venv/Scripts/python.exe platforms/kiro/protocol_v10.py --start-step 1 --stop-after 2

    # 从第 7 步续跑（自动从会话快照恢复 cookie / wsh / workflowState）
    .venv/Scripts/python.exe platforms/kiro/protocol_v10.py --start-step 7 --otp-code 123456

    # 列步骤
    .venv/Scripts/python.exe platforms/kiro/protocol_v10.py --list-steps

hCaptcha：AWS signin 若在响应里抛出 hCaptcha 挑战，本模块会尝试
`core.captcha_router.solve('hcaptcha', {'sitekey':..., 'url':...})`；云解未就绪
（kind 未注册 / key 未配）则降级为「打印 sitekey + 提示人工过码」，不阻塞、不报假成功。
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import random
import re
import secrets
import string
import struct
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, quote as url_quote, unquote as url_unquote
from urllib.parse import urlencode, urlparse

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cbor2  # noqa: E402
from curl_cffi import requests as cffi  # noqa: E402
from jwcrypto import jwe, jwk  # noqa: E402

# ───────── 协议常量（公开接口事实） ─────────
KIRO = "https://app.kiro.dev"
SIGNIN = "https://us-east-1.signin.aws"
DIRECTORY_ID = "d-9067642ac7"
PROFILE = "https://profile.aws.amazon.com"
PORTAL = "https://portal.sso.us-east-1.amazonaws.com"
OIDC = "https://oidc.us-east-1.amazonaws.com"
VS_TOKEN = "https://vs.aws.amazon.com/token"
VIEW = "https://view.awsapps.com"
REDIRECT_URI = f"{KIRO}/signin/oauth"
IDENTITYCENTER_ISSUER = "https://identitycenter.amazonaws.com/ssoins-722374e5d5e7e3e0"
DEVICE_START_URL = "https://view.awsapps.com/start"
DEVICE_SCOPES = ("codewhisperer:completions", "codewhisperer:analysis",
                 "codewhisperer:conversations")

# ── A3 指纹自洽（2026-10-06）：出口 KR(Asia/Seoul,UTC+9) + en-US + Windows 一套，禁止跨平台混拼 ──
# 旧值（macOS UA + Apple M4 GPU + zh-CN locale + timeZone 8）在 TES 眼里是"合成矛盾"，
# 现统一为：Windows Chrome 131 内核 + Win32 平台 + Intel 集显 + en-US + Asia/Seoul(+9)。
LOCALE = "en-US"
LANG_HEADER = "en-US,en;q=0.9"
TZ_OFFSET_HOURS = 9           # FWCIM `timeZone` 字段 = -getTimezoneOffset()/60，Asia/Seoul
SCREEN_INFO = "1920-1080-1032-24-*-*-*"
PLUGINS = "PDF Viewer Chrome PDF Viewer ||1920-1080-1032-24-*-*-*"
UA = {
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "sec-ch-ua": '"Not/A)Brand";v="24", "Chromium";v="131", "Google Chrome";v="131"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "dnt": "1",
}

#: 与上面 UA 同平台的 GPU（reg-factory 口径：Intel UHD 630 / D3D11），不再用 Apple M4。
_GPU = {"vendor": "Google Inc. (Intel)",
        "model": ("ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 "
                  "vs_5_0 ps_5_0, D3D11)"),
        "extensions": []}
_PLATFORM_JS = "Win32"
_DEVICE_MEMORY = 8
_HW_CONCURRENCY = 8

SESSION_DIR = ROOT / "scratch"
STEP_NAMES = [
    (1, "kiro_init", "app.kiro.dev InitiateLogin(CBOR)"),
    (2, "resolve_wsh", "view→portal.sso 跳转链取 workflowStateHandle"),
    (3, "signin_user", "signin.aws /api/execute 身份输入"),
    (4, "signup_flow", "signin.aws /signup/api/execute 注册入口"),
    (5, "telemetry_token", "vs.aws.amazon.com/token 遥测令牌"),
    (6, "profile_start", "profile.aws.amazon.com /api/start"),
    (7, "send_otp", "profile /api/send-otp 发验证码邮件"),
    (8, "verify_otp", "profile /api/create-identity 验证 OTP 建身份"),
    (9, "open_registration", "signup 页 + registrationCode"),
    (10, "set_password", "JWE 加密密码提交"),
    (11, "final_login", "登录 workflow 收口"),
    (12, "portal_tokens", "sso-token → oidc → Kiro ExchangeToken"),
    (13, "device_tokens", "OIDC device flow 取 refreshToken"),
]
STEP_BY_NAME = {name: num for num, name, _ in STEP_NAMES}
LAST_STEP = 13


# ───────── 工具：掩码 / 随机 ─────────
def mask(value: str, keep: int = 4) -> str:
    """敏感值掩码：只留头尾各 keep 字符。"""
    text = str(value or "")
    if len(text) <= keep * 2:
        return f"<len={len(text)}>"
    return f"{text[:keep]}…{text[-keep:]}(len={len(text)})"


def _rand_local() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=12))


def _new_password() -> str:
    return secrets.token_urlsafe(12) + "!A1"


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(43)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _visitor_id() -> str:
    return f"{int(time.time() * 1000)}-{_rand_local()}"


def _ubid() -> str:
    return (f"{random.randint(100, 999)}-{random.randint(10 ** 6, 10 ** 7 - 1)}"
            f"-{random.randint(10 ** 6, 10 ** 7 - 1)}")


# ───────── FWCIM 指纹（XXTEA；只保留协议必需的 key 常量） ─────────
_XXTEA_KEY = (1888420705, 2576816180, 2347232058, 874813317)
_DELTA = 0x9E3779B9
_M32 = 0xFFFFFFFF


def _xxtea_encrypt(text: str) -> bytes:
    """标准 XXTEA 加密（Needham-Wheeler 公开算法，自实现）。"""
    raw = text.encode("latin-1")
    pad = (-len(raw)) % 4
    raw += b"\x00" * pad
    words = list(struct.unpack(f"<{len(raw) // 4}I", raw))
    if len(words) < 2:
        words += [0, 0]
    n = len(words)
    rounds = 6 + 52 // n
    total = 0
    z = words[n - 1]
    for _ in range(rounds):
        total = (total + _DELTA) & _M32
        e = (total >> 2) & 3
        for p in range(n):
            y = words[(p + 1) % n]
            mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) & _M32
            mx = (mx ^ (((total ^ y) + (_XXTEA_KEY[(p & 3) ^ e] ^ z)) & _M32)) & _M32
            words[p] = (words[p] + mx) & _M32
            z = words[p]
    return b"".join(struct.pack("<I", w) for w in words)


_NAV_HIST = [13847, 42, 42, 40, 62, 33, 47, 29, 41, 37, 32, 25, 27, 53, 23, 22, 31, 20, 24,
             30, 34, 20, 37, 15, 21, 32, 25, 26, 66, 25, 16, 27, 26, 19, 22, 32, 38, 15, 39,
             35, 49, 9, 29, 43, 16, 26, 23, 15, 29, 30, 36, 46, 18, 29, 11, 30, 24, 27, 20,
             27, 22, 19, 20, 38, 171, 32, 25, 33, 15, 15, 6, 22, 11, 39, 31, 24, 18, 12, 17,
             34, 17, 30, 17, 27, 25, 28, 20, 19, 19, 19, 24, 34, 10, 24, 14, 27, 28, 18, 31,
             27, 78, 28, 512, 41, 28, 22, 34, 15, 26, 29, 34, 16, 17, 21, 17, 43, 17, 9, 24,
             34, 17, 14, 26, 6, 20, 39, 28, 25, 230, 20, 44, 19, 8, 24, 18, 12, 28, 16, 5,
             28, 37, 21, 11, 27, 22, 30, 18, 16, 25, 18, 11, 25, 30, 104, 13, 38, 22, 22, 42,
             18, 23, 22, 32, 9, 30, 18, 5, 31, 34, 18, 24, 17, 22, 25, 12, 16, 34, 18, 28, 23,
             15, 55, 45, 18, 21, 31, 21, 28, 22, 21, 31, 30, 145, 29, 19, 34, 18, 21, 24, 37,
             30, 19, 49, 34, 62, 62, 23, 24, 21, 40, 27, 30, 37, 22, 38, 51, 40, 37, 29, 27,
             53, 28, 31, 27, 37, 36, 40, 57, 31, 22, 41, 32, 23, 35, 28, 58, 41, 45, 27, 38,
             36, 48, 49, 30, 37, 78, 56, 36, 40, 62, 48, 81, 70, 59, 94, 13740]

_GPU_EXT = ["ANGLE_instanced_arrays", "EXT_blend_minmax", "EXT_color_buffer_half_float",
            "EXT_depth_clamp", "EXT_disjoint_timer_query", "EXT_float_blend",
            "EXT_frag_depth", "EXT_polygon_offset_clamp", "EXT_shader_texture_lod",
            "EXT_texture_compression_bptc", "EXT_texture_compression_rgtc",
            "EXT_texture_filter_anisotropic", "EXT_texture_mirror_clamp_to_edge",
            "EXT_sRGB", "KHR_parallel_shader_compile", "OES_element_index_uint",
            "OES_fbo_render_mipmap", "OES_standard_derivatives", "OES_texture_float",
            "OES_texture_float_linear", "OES_texture_half_float",
            "OES_texture_half_float_linear", "OES_vertex_array_object",
            "WEBGL_blend_func_extended", "WEBGL_color_buffer_float",
            "WEBGL_compressed_texture_astc", "WEBGL_compressed_texture_etc",
            "WEBGL_compressed_texture_etc1", "WEBGL_compressed_texture_pvrtc",
            "WEBGL_compressed_texture_s3tc", "WEBGL_compressed_texture_s3tc_srgb",
            "WEBGL_debug_renderer_info", "WEBGL_debug_shaders", "WEBGL_depth_texture",
            "WEBGL_draw_buffers", "WEBGL_lose_context", "WEBGL_multi_draw",
            "WEBGL_polygon_mode"]


def _nav_timing(nav_start: int) -> dict:
    dns = nav_start + random.randint(30, 40)
    conn_end = dns + random.randint(1, 3)
    req = conn_end + random.randint(0, 2)
    resp = req + random.randint(250, 350)
    dom_load = resp + random.randint(2, 5)
    dom_int = dom_load + random.randint(2800, 3500)
    dom_comp = dom_int + random.randint(800, 1200)
    return {"connectStart": dns, "secureConnectionStart": dns, "unloadEventEnd": 0,
            "domainLookupStart": dns, "domainLookupEnd": dns, "responseStart": resp,
            "connectEnd": conn_end, "responseEnd": resp, "requestStart": req,
            "domLoading": dom_load, "redirectStart": 0, "loadEventEnd": dom_comp,
            "domComplete": dom_comp, "navigationStart": nav_start, "loadEventStart": dom_comp,
            "domContentLoadedEventEnd": dom_int, "unloadEventStart": 0, "redirectEnd": 0,
            "domInteractive": dom_int, "fetchStart": dns, "domContentLoadedEventStart": dom_int}


def gen_fwcim(page_url: str, ubid_main: str, canvas_hash: int | None = None, *,
              page_type: str = "signin", event_type: str = "PageLoad",
              email: str = "", time_on_page: int = 0,
              ls_ubid: str | None = None, referrer: str | None = None,
              profile_ubid_state: dict | None = None) -> str:
    """生成 FWCIM 指纹 token（"ECdITeCs:" + base64(XXTEA(crc#json))）。

    A2（2026-10-06）：载荷形状对齐 reg-factory `common/kiro_crypto.FingerprintBuilder.encrypted` ——
    补 `deviceMemory/hardwareConcurrency/platform`、`screenInfo/plugins`、
    `token.isCompatible`（profile/signup 为 True）、`timeToSubmit`、`form`（含 email checksum=crc32）。
    A3：`timeZone` 与出口地 KR(+9) 一致，UA/GPU/platform 同平台（Windows），locale=en-US。
    """
    now_ms = int(time.time() * 1000)
    nav_start = now_ms - random.randint(3500, 5000)
    if canvas_hash is None:
        canvas_hash = random.randint(10 ** 9, 2 ** 31 - 1)
    # lsUbid：profile 页用 X<n>-<7d>-<7d>:<秒>（reg-factory 口径），signin 用 X<ubid>:<秒>
    if ls_ubid:
        ls = ls_ubid
    elif page_type == "profile":
        ls = f"X{random.choice((10, 19, 42, 55, 73, 81, 96))}-{random.randrange(10**7):07d}-" \
             f"{random.randrange(10**7):07d}:{now_ms // 1000}"
    else:
        ls = f"X{ubid_main}:{now_ms // 1000}"
    dynamic = ([f"/dist/main/app_{random.getrandbits(64):x}"[:10] + ".min.js"]
               if page_type == "profile" else ["/assets/js/app.js"])
    interaction = ({"clicks": 0, "touches": 0, "keyPresses": 0, "cuts": 0, "copies": 0,
                    "pastes": 0, "keyPressTimeIntervals": [], "mouseClickPositions": [],
                    "keyCycles": [], "mouseCycles": [], "touchCycles": []}
                   if event_type in ("PageLoad", "first_load")
                   else {"clicks": random.randint(1, 5), "touches": 0,
                         "keyPresses": random.randint(4, 20), "cuts": 0, "copies": 0, "pastes": 0,
                         "keyPressTimeIntervals": [random.randint(80, 140) for _ in range(2)],
                         "mouseClickPositions": [f"{random.randint(120, 900)}.5,{random.randint(20, 400)}.5"],
                         "keyCycles": [random.randint(90, 160) for _ in range(2)],
                         "mouseCycles": [random.randint(60, 200)], "touchCycles": []})
    payload = {
        "metrics": {"el": 0, "script": 0, "h": 0, "batt": 0, "perf": random.randint(0, 3),
                    "auto": 0, "tz": 0, "fp2": 0, "lsubid": 1, "browser": 0, "capabilities": 0,
                    "gpu": 0, "dnt": 0, "math": 0, "tts": 0, "input": 0, "canvas": 0,
                    "captchainput": 0, "pow": 0},
        "start": now_ms - time_on_page if time_on_page else now_ms - random.randint(400, 900),
        "interaction": interaction,
        "scripts": {"dynamicUrls": dynamic, "inlineHashes": [], "elapsed": 0,
                    "dynamicUrlCount": len(dynamic), "inlineHashesCount": 0},
        "history": {"length": 3 if page_type == "profile" else random.randint(1, 2)},
        "battery": {},
        "performance": {"timing": _nav_timing(nav_start)},
        "automation": {"wd": {"properties": {"document": [], "window": [], "navigator": []}},
                       "phantom": {"properties": {"window": []}}},
        "end": now_ms,
        "timeZone": TZ_OFFSET_HOURS,
        "flashVersion": None,
        "plugins": PLUGINS, "dupedPlugins": PLUGINS,
        "screenInfo": SCREEN_INFO,
        "lsUbid": ls,
        "referrer": referrer or "https://view.awsapps.com/",
        "userAgent": UA["user-agent"],
        "deviceMemory": _DEVICE_MEMORY,
        "hardwareConcurrency": _HW_CONCURRENCY,
        "platform": _PLATFORM_JS,
        "location": page_url,
        "webDriver": False,
        "capabilities": {"css": {"textShadow": 1, "WebkitTextStroke": 1, "boxShadow": 1,
                                 "borderRadius": 1, "borderImage": 1, "opacity": 1,
                                 "transform": 1, "transition": 1},
                         "js": {"audio": True, "geolocation": True, "localStorage": "supported",
                                "touch": False, "video": True, "webWorker": True}, "elapsed": 0},
        "gpu": dict(_GPU),
        "dnt": None,
        "math": {"tan": "-1.4214488238747245", "sin": "0.8178819121159085",
                 "cos": "-0.5753861119575491"},
    }
    if page_type == "profile":
        payload["timeToSubmit"] = max(1, time_on_page or random.randint(1000, 4000))
    checksum = f"{binascii.crc32(email.encode('utf-8')) & 0xFFFFFFFF:08X}" if email else ""
    payload["form"] = {} if not email else {
        f"formField29-{now_ms}-{random.randint(1000, 9999)}": {
            "clicks": 1, "touches": 0, "keyPresses": max(3, len(email) // 2), "cuts": 0,
            "copies": 0, "pastes": 0, "keyPressTimeIntervals": [80, 120],
            "mouseClickPositions": ["120.5,20.5"], "keyCycles": [100, 120], "mouseCycles": [90],
            "touchCycles": [], "width": 180, "height": 32, "totalFocusTime": 0,
            "checksum": checksum, "autocomplete": False, "prefilled": False}}
    payload["canvas"] = {"hash": canvas_hash, "emailHash": None, "histogramBins": _NAV_HIST}
    payload["token"] = {"isCompatible": page_type in ("profile", "signup"), "pageHasCaptcha": 0}
    payload["auth"] = {"form": {"method": "get"}}
    payload["errors"] = []
    payload["version"] = "4.0.0"
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    crc = format(binascii.crc32(body.encode()) & 0xFFFFFFFF, "08X")
    enc = _xxtea_encrypt(f"{crc}#{body}")
    return "ECdITeCs:" + base64.b64encode(enc).decode().rstrip("=")


def encrypt_password_jwe(password: str, public_key_jwk: dict) -> str:
    """浏览器侧密码加密：JWE(RSA-OAEP-256 / A256GCM)，明文是带 claims 的 JSON。"""
    key = jwk.JWK(**public_key_jwk)
    protected = json.dumps({"alg": "RSA-OAEP-256", "kid": public_key_jwk["kid"],
                            "enc": "A256GCM", "cty": "enc",
                            "typ": "application/aws+signin+jwe"},
                           separators=(",", ":"))
    now = int(time.time())
    claims = json.dumps({"iss": "us-east-1.signin", "iat": now, "nbf": now,
                         "jti": str(uuid.uuid4()), "exp": now + 300,
                         "aud": "us-east-1.AWSPasswordService", "password": password},
                        separators=(",", ":"))
    return jwe.JWE(claims.encode("utf-8"), recipient=key,
                   protected=protected).serialize(compact=True)


# ───────── hCaptcha 接入点（云解未就绪时降级人工提示） ─────────
_HCAPTCHA_HINT = re.compile(r'(?:data-sitekey|sitekey)["\'=:\s]+([0-9a-zA-Z\-_]{20,})')
_HCAPTCHA_MARK = ("hcaptcha", "captcha-container", "aws-captcha", "captcha-verify")


def _solve_hcaptcha(page_url: str, sitekey: str, log) -> str | None:
    """调用统一打码路由（kind=hcaptcha）。未就绪一律回落，不抛。"""
    try:
        from core.captcha_router import solve  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - 后端未起时
        log(f"  [captcha] 打码路由不可用（{type(exc).__name__}: {exc}）")
        return None
    try:
        token = solve("hcaptcha", {"sitekey": sitekey, "url": page_url}, log=log)
        log(f"  [captcha] hCaptcha 已解 token={mask(token)}")
        return token
    except Exception as exc:
        log(f"  [captcha] hCaptcha 云解未就绪（{type(exc).__name__}: {exc}）"
            f" → 需人工过码：sitekey={sitekey} url={page_url}")
        return None


# ───────── 主流程 ─────────
class KiroProtocolFlow:
    """纯协议 Kiro/AWS Builder ID 注册（step 化 + 会话快照）。"""

    def __init__(self, *, proxy: str | None = None, tag: str = "KIRO",
                 log=None, session_file: str | Path | None = None,
                 captcha_enabled: bool = True):
        self.tag = tag
        self.log_fn = log or print
        self.proxy = proxy or None
        self.captcha_enabled = captcha_enabled
        self.s = cffi.Session(impersonate="chrome131")
        if self.proxy:
            self.s.proxies = {"http": self.proxy, "https": self.proxy}
            self.s.verify = True
        self.session_file = Path(session_file) if session_file else (SESSION_DIR / f"kiro_session_{tag}.json")
        self.st = {}
        self._reset_state()

    # ---- 日志 / 状态 ----
    def log(self, msg: str) -> None:
        self.log_fn(f"[{self.tag}] {msg}")

    def _reset_state(self) -> None:
        verifier, challenge = _pkce_pair()
        self.st = {
            "code_verifier": verifier, "code_challenge": challenge,
            "oauth_state": str(uuid.uuid4()), "visitor_id": _visitor_id(),
            "platform_ubid": _ubid(), "profile_ubid": _ubid(), "canvas_hash": random.randint(10 ** 9, 2 ** 31 - 1),
            "aws_ubid_main": "", "workflow_state_handle": "", "login_wsh": "",
            "signup_wsh": "", "profile_workflow_id": "", "profile_workflow_state": "",
            "profile_load_ts": 0, "orchestrator_id": "", "callback_url": "",
            "portal_csrf": "", "workflow_result_handle": "", "step11_state": "",
            "awsd2c_token": "", "tes_visitor_id": "", "email": "", "password": "",
            "registration_code": "", "sign_in_state": "", "signup_url": "",
        }

    # ---- cookie 持久化（跨 --start-step 复用） ----
    def _dump_cookies(self) -> list:
        out = []
        for c in self.s.cookies.jar:
            out.append([c.name, c.value, c.domain or "", c.path or "/"])
        return out

    def _load_cookies(self, rows: list) -> None:
        for name, value, domain, path in rows:
            try:
                self.s.cookies.set(name, value, domain=domain or None, path=path or "/")
            except Exception:
                pass

    def save_session(self) -> None:
        try:
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {"state": self.st, "cookies": self._dump_cookies(),
                       "saved_at": int(time.time())}
            self.session_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            self.log(f"⚠️ 会话快照保存失败: {exc}")

    def load_session(self) -> bool:
        if not self.session_file.exists():
            return False
        try:
            payload = json.loads(self.session_file.read_text(encoding="utf-8"))
            self.st.update(payload.get("state") or {})
            self._load_cookies(payload.get("cookies") or [])
            self.log(f"已恢复会话快照 {self.session_file.name}（{len(payload.get('cookies') or [])} cookies）")
            return True
        except Exception as exc:
            self.log(f"⚠️ 会话快照恢复失败: {exc}")
            return False

    # ---- cookie 捕获 ----
    def _capture(self, resp) -> None:
        hostname = urlparse(str(resp.url)).hostname or "us-east-1.signin.aws"
        try:
            items = resp.headers.multi_items()
        except Exception:
            items = list(resp.headers.items())
        for key, value in items:
            if key.lower() != "set-cookie":
                continue
            m = re.match(r"([^=]+)=([^;]*)", value)
            if not m:
                continue
            name, val = m.group(1), m.group(2)
            dm = re.search(r"[Dd]omain=([^;,\s]+)", value)
            pm = re.search(r"[Pp]ath=([^;,\s]+)", value)
            path = pm.group(1) if pm else "/"
            if name == "aws-ubid-main":
                self.st["aws_ubid_main"] = val
            domain = (dm.group(1).lstrip(".") if dm else hostname)
            try:
                if dm:
                    self.s.cookies.delete(name, domain="." + domain, path=path)
            except Exception:
                pass
            try:
                self.s.cookies.set(name, val, domain=domain, path=path)
            except Exception:
                pass

    def _signin_cookies(self) -> list:
        return [(c.name, c.value, c.domain, c.path) for c in self.s.cookies.jar
                if "signin.aws" in (c.domain or "")]

    # ---- 通用请求壳 ----
    def _req(self, method: str, url: str, *, headers=None, json_body=None, data=None,
             cookies=None, allow_redirects=True, timeout=45, expect=None):
        h = {**UA, **(headers or {})}
        try:
            r = self.s.request(method.upper(), url, headers=h, json=json_body, data=data,
                               cookies=cookies, allow_redirects=allow_redirects, timeout=timeout)
        except Exception as exc:
            self.log(f"  ❌ 请求异常 {method} {url[:90]}: {type(exc).__name__}: {exc}")
            return None
        self._capture(r)
        self.log(f"  {method.upper()} {url[:110]} → {r.status_code}")
        self._maybe_captcha(r, url)
        if expect is not None and r.status_code not in (expect if isinstance(expect, (tuple, list, set)) else (expect,)):
            self.log(f"  ❌ 期望 {expect} 实得 {r.status_code}: {str(r.text or '')[:300]}")
            return r
        return r

    # ---- hCaptcha 探测 ----
    def _maybe_captcha(self, resp, url: str):
        if not self.captcha_enabled:
            return None
        try:
            body = (resp.text or "")[:200000]
        except Exception:
            return None
        low = body.lower()
        if not any(mark in low for mark in _HCAPTCHA_MARK):
            return None
        m = _HCAPTCHA_HINT.search(body)
        sitekey = m.group(1) if m else ""
        self.log(f"  ⚠️ 检测到验证码挑战迹象（{url[:80]}）sitekey={sitekey or '<未解析>'}")
        if not sitekey:
            self.log("  ⚠️ 未解析出 sitekey，需人工确认验证码类型")
            return None
        return _solve_hcaptcha(url, sitekey, self.log)

    # ---- signin.aws execute ----
    def _execute(self, section: str, payload: dict, referer: str | None = None):
        url = f"{SIGNIN}/platform/{DIRECTORY_ID}{section}/api/execute"
        h = {**UA, "accept": "application/json, text/plain, */*",
             "content-type": "application/json; charset=UTF-8", "origin": SIGNIN,
             "x-amzn-requestid": payload.get("requestId", str(uuid.uuid4())),
             "x-amz-date": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()),
             "referer": referer or f"{SIGNIN}/platform/{DIRECTORY_ID}/login",
             "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty"}
        r = self._req("POST", url, headers=h, json_body=payload)
        if r is None or r.status_code != 200:
            return None
        try:
            data = r.json()
        except Exception:
            self.log(f"  ❌ 非 JSON 响应: {str(r.text or '')[:200]}")
            return None
        # 服务端每次会下发新的 workflowStateHandle / stepId —— 必须跟进，
        # 否则后续调用会被当成新 workflow（表现为 stepId 原地回到 start）。
        if data.get("workflowStateHandle"):
            self.st["workflow_state_handle"] = data["workflowStateHandle"]
        self.log(f"  ← stepId={data.get('stepId')!r} wsh={mask(self.st['workflow_state_handle'])}")
        return data

    def _fp_input(self, page_type: str = "signin") -> dict:
        loc = f"{SIGNIN}/platform/{DIRECTORY_ID}/{page_type}?workflowStateHandle={self.st['workflow_state_handle'] or ''}"
        ubid = self.st["aws_ubid_main"] or self.st["platform_ubid"]
        return {"input_type": "FingerPrintRequestInput",
                "fingerPrint": gen_fwcim(loc, ubid, self.st["canvas_hash"],
                                         page_type=page_type, event_type="PageSubmit")}

    # ═══════════ S1 ═══════════
    def step1_kiro_init(self) -> str | None:
        self.log("S1 InitiateLogin（CBOR）…")
        body = cbor2.dumps({"idp": "BuilderId", "redirectUri": REDIRECT_URI,
                            "state": self.st["oauth_state"],
                            "codeChallenge": self.st["code_challenge"],
                            "codeChallengeMethod": "S256"})
        h = {**UA, "accept": "application/cbor", "content-type": "application/cbor",
             "smithy-protocol": "rpc-v2-cbor", "origin": KIRO, "referer": f"{KIRO}/signin",
             "x-kiro-visitorid": self.st["visitor_id"],
             "amz-sdk-invocation-id": str(uuid.uuid4()), "amz-sdk-request": "attempt=1; max=1",
             "x-amz-user-agent": ("aws-sdk-js/1.0.0 ua/2.1 os/Windows lang/js "
                                  "md/browser#Chromium_131 m/N,M,E")}
        r = self._req("POST", f"{KIRO}/service/KiroWebPortalService/operation/InitiateLogin",
                      headers=h, data=body, cookies={"kiro-visitor-id": self.st["visitor_id"]})
        if r is None or r.status_code != 200:
            return None
        try:
            data = cbor2.loads(r.content)
        except Exception:
            try:
                data = r.json()
            except Exception:
                data = {}
        redir = data.get("redirectUrl")
        if not redir:
            self.log(f"  ❌ 无 redirectUrl: {json.dumps(data, ensure_ascii=False)[:300]}")
            return None
        self.log(f"  ✅ redirectUrl={redir[:110]}")
        return redir

    # ═══════════ S2 ═══════════
    def step2_resolve_wsh(self, redir_url: str) -> bool:
        self.log("S2 跳转链（signin.aws authorize → login，逐步跟踪 Location）…")
        from urllib.parse import urljoin  # noqa: PLC0415
        url, final = redir_url, redir_url
        hops = []
        for _ in range(8):
            r = self._req("GET", url, headers={**UA, "accept": "text/html"},
                          allow_redirects=False)
            if r is None:
                return False
            loc = r.headers.get("location") or r.headers.get("Location") or ""
            hops.append((r.status_code, str(r.url), loc))
            if not loc:
                final = str(r.url)
                break
            url = urljoin(str(r.url), loc)
        else:
            final = hops[-1][1]
        for stt, u, loc in hops:
            self.log(f"  hop {stt} {u[:100]}" + (f" → {loc[:80]}" if loc else ""))
        # orchestrator_id / callback_url 可能出现在任意一跳（query 或 fragment）
        for _stt, u, _loc in [*hops, (0, final, "")]:
            try:
                p = urlparse(u)
            except Exception:
                continue
            qs = parse_qs(p.query)
            frag = parse_qs(p.fragment.lstrip("#/?")) if p.fragment else {}
            orch = (qs.get("orchestrator_id") or frag.get("orchestrator_id", [None]))[0]
            cb = (qs.get("callback_url") or frag.get("callback_url", [None]))[0]
            wsh = (qs.get("workflowStateHandle") or frag.get("workflowStateHandle", [None]))[0]
            if orch:
                self.st["orchestrator_id"] = orch
            if cb:
                self.st["callback_url"] = cb
            if wsh and not self.st["workflow_state_handle"]:
                self.st["workflow_state_handle"] = wsh
        self.log(f"  callback_url={self.st['callback_url'][:90] or '<无>'}")
        self.log(f"  orchestrator_id={self.st['orchestrator_id'][:60] or '<本流程未出现 view.awsapps.com 跳转>'}")
        if not self.st["workflow_state_handle"]:
            self.log("  ❌ 跳转链中未取到 workflowStateHandle")
            return False
        self.st["login_wsh"] = self.st["workflow_state_handle"]
        self.log(f"  ✅ wsh={mask(self.st['workflow_state_handle'])}")
        # portal.sso csrf（S12 需要）——仅当流程出现 view.awsapps.com 跳转时可得
        if self.st["orchestrator_id"]:
            inner = (f"{VIEW}/start/#/?callback_url={url_quote(self.st['callback_url'])}"
                     f"&orchestrator_id={self.st['orchestrator_id']}")
            portal_url = f"{PORTAL}/login?directory_id=view&redirect_url={url_quote(inner)}"
            r2 = self._req("GET", portal_url, headers={
                **UA, "accept": "*/*", "origin": VIEW, "referer": f"{VIEW}/",
                "sec-fetch-site": "cross-site", "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty"}, allow_redirects=False)
            if r2 is not None:
                try:
                    data = r2.json()
                    if data.get("csrfToken"):
                        self.st["portal_csrf"] = str(data["csrfToken"])
                        self.log(f"  portal csrfToken={mask(self.st['portal_csrf'])}")
                except Exception:
                    pass
        else:
            self.log("  ⚠️ 未出现 view.awsapps.com 跳转 → portal csrfToken 取不到（S12 前置条件待补）")
        # signin 页加载后只补 JS 生成的 platform-ubid（其余由服务端 Set-Cookie 管理）
        try:
            self.s.cookies.set("platform-ubid", self.st["platform_ubid"],
                               domain="us-east-1.signin.aws", path="/platform")
        except Exception:
            pass
        return True

    # ═══════════ S3 ═══════════
    def step3_signin_user(self, email: str) -> dict | None:
        self.log("S3 signin.aws /api/execute 身份输入…")
        fp = self._fp_input()
        usr = {"input_type": "UserRequestInput", "username": email}
        if not self._execute("", {"stepId": "", "workflowStateHandle": self.st["workflow_state_handle"],
                                  "inputs": [fp], "requestId": str(uuid.uuid4())}):
            return None
        if not self._execute("", {"stepId": "start", "workflowStateHandle": self.st["workflow_state_handle"],
                                  "inputs": [fp], "requestId": str(uuid.uuid4())}):
            return None
        data = self._execute("", {"stepId": "get-identity-user", "actionId": "SIGNUP",
                                  "workflowStateHandle": self.st["workflow_state_handle"],
                                  "inputs": [usr, fp], "requestId": str(uuid.uuid4())})
        if not data:
            return None
        if data.get("workflowStateHandle"):
            self.st["workflow_state_handle"] = data["workflowStateHandle"]
        redir = (data.get("redirect") or {}).get("url", "")
        self.log(f"  stepId={data.get('stepId')} redirect={redir[:90]}")
        if redir:
            m = re.search(r"workflowStateHandle=([^&#]+)", redir)
            if m:
                self.st["workflow_state_handle"] = m.group(1)
        return data

    # ═══════════ S4 ═══════════
    def step4_signup_flow(self, email: str) -> dict | None:
        self.log("S4 signin.aws /signup/api/execute 注册入口…")
        fp = self._fp_input("signup")
        usr = {"input_type": "UserRequestInput", "username": email}
        first = self._execute("/signup", {"stepId": "", "workflowStateHandle": self.st["workflow_state_handle"],
                                          "inputs": [usr, fp], "requestId": str(uuid.uuid4())})
        if first is None:
            return None
        self._sync_signup_csrf()
        data = self._execute("/signup", {"stepId": "start", "workflowStateHandle": self.st["workflow_state_handle"],
                                         "inputs": [usr, fp], "requestId": str(uuid.uuid4())})
        if not data:
            return None
        if data.get("workflowStateHandle"):
            self.st["workflow_state_handle"] = data["workflowStateHandle"]
        redir = (data.get("redirect") or {}).get("url", "")
        self.log(f"  stepId={data.get('stepId')} redirect={redir[:90]}")
        m = re.search(r"workflowID=([^&#]+)", redir)
        if m:
            self.st["profile_workflow_id"] = m.group(1)
            self.log(f"  ✅ profile workflowID={mask(self.st['profile_workflow_id'])}")
        self.st["signup_wsh"] = self.st["workflow_state_handle"]
        return data

    def _sync_signup_csrf(self) -> None:
        """signup 首步后，服务端把 signupCsrfToken 塞进 workflow-csrf-token，
        浏览器 JS 会把它合并进 directory-csrf-token。"""
        wf = dir_ = None
        dir_path = f"/platform/{DIRECTORY_ID}"
        for c in self.s.cookies.jar:
            if "signin.aws" not in (c.domain or ""):
                continue
            if c.domain == "us-east-1.signin.aws":
                if c.name == "workflow-csrf-token":
                    wf = c.value
                elif c.name == "directory-csrf-token":
                    dir_, dir_path = c.value, c.path or dir_path
        if not (wf and dir_):
            self.log("  （csrf 同步：workflow/directory token 未同时就位，跳过）")
            return
        try:
            wf_obj = json.loads(url_unquote(wf))
            dir_obj = json.loads(url_unquote(dir_))
            signup_token = wf_obj.get("signupCsrfToken")
            if signup_token and "signupCsrfToken" not in dir_obj:
                dir_obj["signupCsrfToken"] = signup_token
                new_val = url_quote(json.dumps(dir_obj, separators=(",", ":")), safe="")
                for c in list(self.s.cookies.jar):
                    if c.name == "directory-csrf-token":
                        try:
                            self.s.cookies.delete(c.name, domain=c.domain, path=c.path)
                        except Exception:
                            pass
                self.s.cookies.set("directory-csrf-token", new_val,
                                   domain="us-east-1.signin.aws", path=dir_path)
                self.log("  ✅ directory-csrf-token 已合并 signupCsrfToken")
        except Exception as exc:
            self.log(f"  ⚠️ csrf 同步失败: {exc}")

    # ═══════════ S5 ═══════════
    def step5_telemetry_token(self) -> str | None:
        self.log("S5 遥测令牌（vs.aws.amazon.com/token）…")
        self._req("GET", f"{SIGNIN}/assets/js/app.js",
                  headers={**UA, "accept": "*/*", "referer": f"{SIGNIN}/platform/{DIRECTORY_ID}/login"})
        self._req("POST", f"{SIGNIN}/metrics/fingerprint",
                  headers={**UA, "accept": "*/*", "content-type": "application/json",
                           "origin": SIGNIN, "referer": f"{SIGNIN}/platform/{DIRECTORY_ID}/login"},
                  json_body={"fingerprint": self._fp_input()["fingerPrint"]})
        r = self._req("POST", VS_TOKEN, headers={
            **UA, "accept": "*/*", "content-type": "application/json", "origin": SIGNIN,
            "referer": f"{SIGNIN}/", "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "cors", "sec-fetch-dest": "empty"}, json_body={})
        if r is None or r.status_code != 200:
            return None
        try:
            token = (r.json() or {}).get("token", "")
        except Exception:
            token = ""
        if not token:
            self.log("  ❌ 无 token")
            return None
        self.st["awsd2c_token"] = token
        self.log(f"  ✅ awsd2c-token={mask(token)}")
        try:
            part = token.split(".")[1]
            part += "=" * (-len(part) % 4)
            claims = json.loads(base64.urlsafe_b64decode(part))
            self.st["tes_visitor_id"] = claims.get("vid", "")
            self.log(f"  visitorId={mask(self.st['tes_visitor_id'])}")
        except Exception:
            pass
        for dom in (".aws.amazon.com", "us-east-1.signin.aws"):
            for nm in ("awsd2c-token", "awsd2c-token-c"):
                try:
                    self.s.cookies.set(nm, token, domain=dom, path="/")
                except Exception:
                    pass
        return token

    # ═══════════ S6 ═══════════
    def _profile_headers(self) -> dict:
        return {**UA, "accept": "*/*", "accept-language": LANG_HEADER,
                "content-type": "application/json;charset=UTF-8", "origin": PROFILE,
                "priority": "u=1, i",
                "referer": f"{PROFILE}/?workflowID={self.st['profile_workflow_id']}",
                "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty"}

    def _browser_data(self, page_name: str | None = None, event_type: str = "PageLoad",
                      email: str = "") -> dict:
        """A2：profile 页 browserData，对齐 reg-factory `_fingerprint_input("profile", ...)` 口径。

        - location = `PROFILE/?workflowID=…#/signup/enter-email`（PageSubmit）/ `#/signup/start`
        - referrer = `SIGNIN/platform/{DIR}/signup?workflowStateHandle=…`
        - timeSpentOnPage：PageSubmit=6500，PageLoad=38（reg-factory 常量）
        - 指纹内带 `token.isCompatible=True` + `form.checksum`（email crc32）
        """
        elapsed = int((time.time() - self.st["profile_load_ts"]) * 1000) if self.st.get("profile_load_ts") else 0
        frag = "enter-email" if event_type == "PageSubmit" else "start"
        loc = f"{PROFILE}/?workflowID={self.st['profile_workflow_id']}#/signup/{frag}"
        ref = f"{SIGNIN}/platform/{DIRECTORY_ID}/signup?workflowStateHandle={self.st['workflow_state_handle'] or ''}"
        tsp = (6500 if event_type == "PageSubmit"
               else 38 if event_type == "PageLoad" else max(elapsed, random.randint(2000, 5000)))
        fp = gen_fwcim(loc, self.st["aws_ubid_main"] or _ubid(), self.st["canvas_hash"],
                       page_type="profile", event_type=event_type, email=email,
                       time_on_page=tsp, referrer=ref)
        attrs = {"fingerprint": fp,
                 "eventTimestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                 "timeSpentOnPage": str(tsp),
                 "eventType": event_type, "ubid": self.st["profile_ubid"]}
        if page_name:
            attrs["pageName"] = page_name
        if self.st.get("tes_visitor_id"):
            attrs["visitorId"] = self.st["tes_visitor_id"]
        return {"attributes": attrs, "cookies": {}}

    def _profile_post(self, path: str, payload: dict) -> dict | None:
        r = self._req("POST", f"{PROFILE}{path}", headers=self._profile_headers(), json_body=payload)
        if r is None:
            return None
        if r.status_code not in (200, 201):
            self.log(f"  ❌ {path} HTTP {r.status_code}: {str(r.text or '')[:400]}")
            return None
        try:
            data = r.json()
            self.log(f"  Resp: {json.dumps(data, ensure_ascii=False)[:300]}")
            return data
        except Exception:
            return {}

    def step6_profile_start(self) -> dict | None:
        self.log("S6 profile.aws 加载 + /api/start …")
        if not self.st.get("profile_workflow_id"):
            self.log("  ❌ 缺 profile workflowID（S4 未成功）")
            return None
        # profile 域 cookies
        try:
            self.s.cookies.set("i18next", LOCALE, domain="profile.aws.amazon.com", path="/")
            if self.st["aws_ubid_main"]:
                self.s.cookies.set("aws-ubid-main", self.st["aws_ubid_main"], domain=".amazon.com", path="/")
            awsccc = base64.b64encode(json.dumps(
                {"e": 1, "p": 1, "f": 1, "a": 1, "i": str(uuid.uuid4()), "v": "1"},
                separators=(",", ":")).encode()).decode()
            self.s.cookies.set("awsccc", awsccc, domain="profile.aws.amazon.com", path="/")
            self.s.cookies.set("aws-user-profile-ubid", self.st["profile_ubid"],
                               domain="profile.aws.amazon.com", path="/")
        except Exception:
            pass
        self._req("GET", f"{PROFILE}?workflowID={self.st['profile_workflow_id']}",
                  headers={**UA, "accept": "text/html", "referer": f"{SIGNIN}/"})
        self.st["profile_load_ts"] = time.time()
        self._profile_post("/api/get-config", {})
        # A1 修正（2026-10-06，真人浏览器取证）：真 SPA 在 `#/signup/enter-email` 路由下**确实会调**
        # `/api/get-app-context` 并**接受它的 404**（浏览器 NET 记录：get-config 200 → get-app-context 404），
        # 所以 404 是 SPA 正常行为、不是缺陷。无参调 `/api/startSignin` 会 400
        # （要求 redirectUrl/csrfToken/originPath），已不再调用。
        rctx = self._req("POST", f"{PROFILE}/api/get-app-context",
                        headers=self._profile_headers(),
                        json_body={"workflowID": self.st["profile_workflow_id"]})
        if rctx is not None:
            self.log(f"  /api/get-app-context → {rctx.status_code}（与真 SPA 一致）")
        # A2：按 reg-factory profile_init 口径，用 profile 页 referer 再取一次 awsd2c，
        # 使 visitorId 与 profile 页上下文一致（S5 那次 referer 是 signin 域）。
        ref_profile = f"{PROFILE}/?workflowID={self.st['profile_workflow_id']}"
        r2 = self._req("POST", VS_TOKEN, headers={
            **UA, "accept": "*/*", "content-type": "application/json", "origin": PROFILE,
            "referer": ref_profile, "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "cors", "sec-fetch-dest": "empty"}, json_body={})
        try:
            tok2 = (r2.json() or {}).get("token", "") if r2 is not None and r2.status_code == 200 else ""
            if tok2:
                self.st["awsd2c_token"] = tok2
                for dom in (".aws.amazon.com", "profile.aws.amazon.com"):
                    for nm in ("awsd2c-token", "awsd2c-token-c"):
                        try:
                            self.s.cookies.set(nm, tok2, domain=dom, path="/")
                        except Exception:
                            pass
                part = tok2.split(".")[1]
                part += "=" * (-len(part) % 4)
                vid = json.loads(base64.urlsafe_b64decode(part)).get("vid", "")
                if vid:
                    self.st["tes_visitor_id"] = vid
                self.log(f"  ✅ profile-referer awsd2c / visitorId={mask(self.st['tes_visitor_id'])}")
        except Exception as exc:
            self.log(f"  ⚠️ profile d2c 刷新失败（{type(exc).__name__}）")
        time.sleep(0.4)
        data = self._profile_post("/api/start", {"workflowID": self.st["profile_workflow_id"],
                                                 "browserData": self._browser_data()})
        if data and data.get("workflowState"):
            self.st["profile_workflow_state"] = data["workflowState"]
            self.log(f"  ✅ workflowState={mask(self.st['profile_workflow_state'])}")
        return data

    # ═══════════ S7 ═══════════
    def step7_send_otp(self, email: str) -> dict | None:
        self.log(f"S7 send-otp → {mask(email, 3)} …")
        time.sleep(random.uniform(1.5, 3.0))
        return self._profile_post("/api/send-otp", {
            "workflowState": self.st["profile_workflow_state"], "email": email,
            "browserData": self._browser_data("EMAIL_COLLECTION", "PageSubmit", email)})

    # ═══════════ S8 ═══════════
    def step8_verify_otp(self, otp: str, email: str, full_name: str) -> dict | None:
        self.log("S8 create-identity（校验 OTP + 建身份）…")
        time.sleep(random.uniform(0.8, 2.0))
        data = self._profile_post("/api/create-identity", {
            "workflowState": self.st["profile_workflow_state"],
            "userData": {"email": email, "fullName": full_name},
            "otpCode": otp,
            "browserData": self._browser_data("EMAIL_VERIFICATION", "EmailVerification", email)})
        if not data:
            return None
        if not data.get("registrationCode") or not data.get("signInState"):
            self.log(f"  ❌ 缺 registrationCode/signInState: {json.dumps(data, ensure_ascii=False)[:300]}")
            return None
        self.st["registration_code"] = data["registrationCode"]
        self.st["sign_in_state"] = data["signInState"]
        self.log(f"  ✅ registrationCode={mask(self.st['registration_code'])}")
        return data

    # ═══════════ S9 ═══════════
    def step9_open_registration(self) -> dict | None:
        self.log("S9 signup 页 + registrationCode …")
        reg, state = self.st["registration_code"], self.st["sign_in_state"]
        signup_url = f"{SIGNIN}/platform/{DIRECTORY_ID}/signup?registrationCode={reg}&state={state}"
        self.st["signup_url"] = signup_url
        r = self._req("GET", signup_url, headers={**UA, "accept": "text/html",
                                                  "referer": f"{PROFILE}/"})
        del r
        self._req("GET", f"{SIGNIN}/assets/js/app.js",
                  headers={**UA, "accept": "*/*", "referer": signup_url})
        self._req("GET", f"{SIGNIN}/platform/config?directoryId=",
                  headers={**UA, "accept": "*/*", "referer": signup_url})
        time.sleep(0.4)
        fp = self._fp_input("signup")
        reg_input = {"input_type": "UserRegistrationRequestInput",
                     "registrationCode": reg, "state": state}
        data = self._execute("/signup", {"stepId": "", "state": state,
                                         "inputs": [reg_input, fp],
                                         "requestId": str(uuid.uuid4())},
                             referer=signup_url)
        if not data:
            return None
        self.log(f"  stepId={data.get('stepId')}")
        if data.get("stepId") != "get-new-password-for-password-creation":
            self.log(f"  ❌ 预期 get-new-password-for-password-creation，实得 {data.get('stepId')}")
            return None
        pub = ((data.get("workflowResponseData") or {})
               .get("encryptionContextResponse") or {}).get("publicKey")
        if not pub:
            self.log("  ❌ 无 JWE 公钥")
            return None
        self.log(f"  ✅ 进入密码步，公钥 kid={pub.get('kid')}")
        return data

    # ═══════════ S10 ═══════════
    def step10_set_password(self, password: str, email: str, step9: dict) -> dict | None:
        self.log("S10 JWE 加密密码提交 …")
        wsh = step9.get("workflowStateHandle", "")
        pub = ((step9.get("workflowResponseData") or {})
               .get("encryptionContextResponse") or {}).get("publicKey")
        if not pub:
            self.log("  ❌ 无公钥")
            return None
        referer = self.st.get("signup_url") or f"{SIGNIN}/platform/{DIRECTORY_ID}/signup"
        # 清理 curl_cffi 自动落下的 .domain 版本 cookie（浏览器只发 host-only）
        for c in list(self.s.cookies.jar):
            if (c.domain or "").startswith(".") and "signin.aws" in (c.domain or ""):
                try:
                    self.s.cookies.delete(c.name, domain=c.domain, path=c.path)
                except Exception:
                    pass
        # 页面上报（PAGE_LOAD / 指纹指标）
        rid = str(uuid.uuid4())
        self._req("POST", f"{SIGNIN}/platform/user-event/send-event", headers={
            **UA, "accept": "application/json, text/plain, */*",
            "content-type": "application/json; charset=UTF-8", "origin": SIGNIN,
            "x-amzn-requestid": rid,
            "x-amz-date": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()),
            "referer": referer, "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors", "sec-fetch-dest": "empty"}, json_body={
            "inputs": [{"input_type": "UserEventRequestInput", "directoryId": DIRECTORY_ID,
                        "userName": email,
                        "userEvents": [{"input_type": "UserEvent", "eventType": "PAGE_LOAD",
                                        "pageName": "CREDENTIAL_COLLECTION"}]},
                       self._fp_input()],
            "requestId": rid})
        self._req("POST", f"{SIGNIN}/metrics/fingerprint", headers={
            **UA, "accept": "*/*", "content-type": "application/x-www-form-urlencoded",
            "origin": SIGNIN, "referer": referer},
            data=f"name=IsFingerprintGenerated:Success&value={self._fp_input()['fingerPrint']}")
        time.sleep(random.uniform(0.8, 2.0))

        jwe_password = encrypt_password_jwe(password, pub)
        self.log(f"  JWE 长度={len(jwe_password)}")
        req_id = str(uuid.uuid4())
        body = {"stepId": "get-new-password-for-password-creation", "workflowStateHandle": wsh,
                "actionId": "SUBMIT",
                "inputs": [
                    {"input_type": "PasswordRequestInput", "password": jwe_password,
                     "successfullyEncrypted": "SUCCESSFUL", "errorLog": None},
                    {"input_type": "UserEventRequestInput", "directoryId": DIRECTORY_ID,
                     "userName": email,
                     "userEvents": [{"input_type": "UserEvent", "eventType": "PAGE_SUBMIT",
                                     "pageName": "CREDENTIAL_COLLECTION",
                                     "timeSpentOnPage": random.randint(8000, 25000)}]},
                    {"input_type": "UserRequestInput", "username": email},
                    self._fp_input()],
                "visitorId": self.st.get("tes_visitor_id") or "", "requestId": req_id}
        data = self._execute("/signup", body, referer=referer)
        if not data:
            return None
        self.log(f"  stepId={data.get('stepId')}")
        if data.get("stepId") != "end-of-user-registration-success":
            self.log(f"  ❌ 预期 end-of-user-registration-success，实得 {data.get('stepId')}")
            return None
        redir = (data.get("redirect") or {}).get("url", "")
        m = re.search(r"workflowResultHandle=([^&#]+)", redir)
        if m:
            self.st["workflow_result_handle"] = m.group(1)
            self.log(f"  ✅ workflowResultHandle={mask(self.st['workflow_result_handle'])}")
        self.log("  ✅ 密码设置成功（注册动作完成）")
        return data

    # ═══════════ S11 ═══════════
    def step11_final_login(self, email: str, step10: dict) -> dict | None:
        self.log("S11 登录 workflow 收口 …")
        redir = (step10.get("redirect") or {}).get("url", "")
        if not redir:
            self.log("  ❌ 无 redirect")
            return None
        qs = parse_qs(urlparse(redir).query)
        login_wsh = (qs.get("workflowStateHandle") or [None])[0]
        state = (qs.get("state") or [None])[0]
        wf_result = (qs.get("workflowResultHandle") or [None])[0]
        if not (login_wsh and state and wf_result):
            self.log("  ❌ redirect 参数不完整")
            return None
        data = self._execute("", {"stepId": "", "workflowStateHandle": login_wsh,
                                  "workflowResultHandle": wf_result, "state": state,
                                  "inputs": [{"input_type": "UserRequestInput", "username": email},
                                             self._fp_input()],
                                  "visitorId": self.st.get("tes_visitor_id") or "",
                                  "requestId": str(uuid.uuid4())})
        if not data:
            return None
        self.log(f"  stepId={data.get('stepId')}")
        if data.get("stepId") == "end-of-workflow-success":
            redir11 = (data.get("redirect") or {}).get("url", "")
            qs11 = parse_qs(urlparse(redir11).query)
            if (qs11.get("state") or [None])[0]:
                self.st["step11_state"] = qs11["state"][0]
            if (qs11.get("workflowResultHandle") or [None])[0]:
                self.st["workflow_result_handle"] = qs11["workflowResultHandle"][0]
            self.log("  ✅ end-of-workflow-success")
        else:
            self.log(f"  ⚠️ 非预期 stepId={data.get('stepId')}")
        return data

    # ═══════════ S12 ═══════════
    def step12_portal_tokens(self) -> dict | None:
        self.log("S12 portal.sso → oidc → Kiro ExchangeToken …")
        if not self.st.get("portal_csrf"):
            self.log("  ❌ 缺 portal csrfToken（S2）")
            return None
        if not self.st.get("workflow_result_handle") or not self.st.get("step11_state"):
            self.log("  ❌ 缺 workflowResultHandle / step11 state（S10/S11）")
            return None
        # 12a sso-token
        r = self._req("POST", f"{PORTAL}/auth/sso-token", headers={
            **UA, "accept": "application/json, text/plain, */*",
            "content-type": "application/x-www-form-urlencoded",
            "x-amz-sso-csrf-token": self.st["portal_csrf"], "origin": VIEW,
            "referer": f"{VIEW}/", "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "cors", "sec-fetch-dest": "empty"},
            data=urlencode({"authCode": self.st["workflow_result_handle"],
                            "state": self.st["step11_state"], "orgId": "view"}))
        if r is None or r.status_code != 200:
            return None
        try:
            sso = r.json()
        except Exception:
            self.log(f"  ❌ sso-token 非 JSON: {str(r.text or '')[:200]}")
            return None
        bearer = sso.get("token", "")
        sso_redirect = sso.get("redirectUrl", "")
        if not bearer:
            self.log(f"  ❌ 无 bearer: {json.dumps(sso, ensure_ascii=False)[:250]}")
            return None
        self.log(f"  ✅ bearer(sessionToken)={mask(bearer)}")
        if sso_redirect and "view.awsapps.com" in sso_redirect:
            self._req("GET", sso_redirect.split("#")[0],
                      headers={**UA, "accept": "text/html", "referer": f"{SIGNIN}/"})
            p = urlparse(sso_redirect)
            frag = parse_qs(p.fragment.lstrip("#/?")) if p.fragment else {}
            new_orch = (parse_qs(p.query).get("orchestrator_id") or frag.get("orchestrator_id", [None]))[0]
            if new_orch:
                self.st["orchestrator_id"] = new_orch
        # 12b whoAmI（验证 bearer）
        self._req("GET", f"{PORTAL}/token/whoAmI", headers={
            **UA, "accept": "application/json, text/plain, */*",
            "authorization": f"Bearer {bearer}", "origin": VIEW, "referer": f"{VIEW}/",
            "sec-fetch-site": "cross-site", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty"})
        # 12c authentication_result
        r = self._req("POST", f"{OIDC}/authentication_result", headers={
            **UA, "accept": "application/json", "content-type": "application/json",
            "x-amz-sso_bearer_token": bearer, "x-amz-sso-bearer-token": bearer,
            "origin": VIEW, "referer": f"{VIEW}/", "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "cors", "sec-fetch-dest": "empty"},
            json_body={"orchestrator_id": self.st["orchestrator_id"]})
        if r is None or r.status_code != 200:
            return None
        location = (r.json() or {}).get("location", "")
        if not location:
            self.log("  ❌ authentication_result 无 location")
            return None
        # 12d authorize（不跟跳转，读 Location 取 code/state）
        r = self._req("GET", location, headers={**UA, "accept": "text/html",
                                                "referer": f"{VIEW}/"},
                      allow_redirects=False)
        if r is None:
            return None
        loc = r.headers.get("location") or r.headers.get("Location") or ""
        if not loc:
            self.log(f"  ❌ authorize 无 Location（status={r.status_code}）")
            return None
        qs = parse_qs(urlparse(loc).query)
        auth_code = (qs.get("code") or [None])[0]
        redir_state = (qs.get("state") or [None])[0] or self.st["oauth_state"]
        if not auth_code:
            self.log("  ❌ 回调 URL 中无 code")
            return None
        self.log(f"  ✅ auth_code={mask(auth_code)}")
        # 12e ExchangeToken（CBOR）
        body = cbor2.dumps({"code": auth_code, "codeVerifier": self.st["code_verifier"],
                            "idp": "BuilderId", "redirectUri": REDIRECT_URI,
                            "state": redir_state})
        r = self._req("POST", f"{KIRO}/service/KiroWebPortalService/operation/ExchangeToken",
                      headers={**UA, "accept": "application/cbor", "content-type": "application/cbor",
                               "smithy-protocol": "rpc-v2-cbor", "origin": KIRO,
                               "referer": f"{KIRO}/signin",
                               "x-kiro-visitorid": self.st["visitor_id"],
                               "amz-sdk-invocation-id": str(uuid.uuid4()),
                               "amz-sdk-request": "attempt=1; max=1",
                               "x-amz-user-agent": ("aws-sdk-js/1.0.0 ua/2.1 os/Windows lang/js "
                                                    "md/browser#Chromium_131 m/N,M,E")},
                      data=body, cookies={"kiro-visitor-id": self.st["visitor_id"]})
        if r is None or r.status_code != 200:
            return None
        try:
            data = cbor2.loads(r.content)
        except Exception as exc:
            self.log(f"  ❌ ExchangeToken CBOR 解析失败: {exc}")
            return None
        access_token = data.get("accessToken", "")
        if not access_token:
            self.log(f"  ❌ 无 accessToken: {json.dumps(data, ensure_ascii=False, default=str)[:250]}")
            return None
        self.log(f"  ✅ accessToken={mask(access_token)} expiresIn={data.get('expiresIn')}")
        return {"accessToken": access_token, "sessionToken": bearer,
                "csrfToken": data.get("csrfToken", ""), "expiresIn": data.get("expiresIn", 0)}

    # ═══════════ S12f ═══════════
    def step12f_device_tokens(self, bearer_token: str) -> dict | None:
        self.log("S12f OIDC device flow（取 refreshToken/clientId/Secret）…")
        sdk_ua = "aws-sdk-rust/1.3.9 os/windows lang/rust/1.87.0"
        x_ua = ("aws-sdk-rust/1.3.9 ua/2.1 api/ssooidc/1.88.0 os/windows "
                "lang/rust/1.87.0 m/E app/AmazonQ-For-CLI")
        base_h = {"content-type": "application/json", "user-agent": sdk_ua,
                  "x-amz-user-agent": x_ua, "amz-sdk-request": "attempt=1; max=3"}
        r = self._req("POST", f"{OIDC}/client/register", headers={
            **base_h, "amz-sdk-invocation-id": str(uuid.uuid4())},
            json_body={"clientName": "Amazon Q Developer for command line",
                       "clientType": "public", "scopes": list(DEVICE_SCOPES),
                       "grantTypes": ["urn:ietf:params:oauth:grant-type:device_code",
                                      "refresh_token"],
                       "issuerUrl": IDENTITYCENTER_ISSUER})
        if r is None or r.status_code != 200:
            return None
        reg = r.json()
        client_id, client_secret = reg.get("clientId", ""), reg.get("clientSecret", "")
        if not (client_id and client_secret):
            self.log(f"  ❌ 无 clientId/Secret: {json.dumps(reg, ensure_ascii=False)[:200]}")
            return None
        self.log(f"  ✅ clientId={mask(client_id)}")
        r = self._req("POST", f"{OIDC}/device_authorization", headers={
            **base_h, "amz-sdk-invocation-id": str(uuid.uuid4())},
            json_body={"clientId": client_id, "clientSecret": client_secret,
                       "startUrl": DEVICE_START_URL})
        if r is None or r.status_code != 200:
            return None
        da = r.json()
        device_code = da.get("deviceCode", "")
        user_code = da.get("userCode", "")
        interval = max(int(da.get("interval", 1) or 1), 1)
        if not (device_code and user_code):
            self.log(f"  ❌ 无 deviceCode/userCode: {json.dumps(da, ensure_ascii=False)[:200]}")
            return None
        self.log(f"  userCode={user_code} interval={interval}s")

        oidc_h = {**UA, "accept": "application/json, text/plain, */*",
                  "content-type": "application/json", "origin": VIEW, "referer": f"{VIEW}/",
                  "sec-fetch-site": "cross-site", "sec-fetch-mode": "cors",
                  "sec-fetch-dest": "empty"}
        r = self._req("POST", f"{OIDC}/device_authorization/accept_user_code", headers=oidc_h,
                      json_body={"userCode": user_code, "userSessionId": bearer_token})
        if r is None or r.status_code != 200:
            return None
        ctx = ((r.json() or {}).get("deviceContext") or {})
        dc_id = ctx.get("deviceContextId", "")
        dc_client_id = ctx.get("clientId", client_id)
        dc_client_type = ctx.get("clientType", "public")
        if not dc_id:
            self.log("  ❌ 无 deviceContextId")
            return None
        r = self._req("POST", f"{PORTAL}/session/device", headers={
            **UA, "accept": "application/json, text/plain, */*", "content-type": "application/json",
            "authorization": f"Bearer {bearer_token}", "origin": VIEW, "referer": f"{VIEW}/",
            "sec-fetch-site": "cross-site", "sec-fetch-mode": "cors"}, json_body={})
        device_token = bearer_token
        if r is not None and r.status_code == 200:
            try:
                device_token = (r.json() or {}).get("token", bearer_token)
            except Exception:
                pass
        self._req("POST", f"{OIDC}/consent_details", headers=oidc_h,
                  json_body={"deviceContextId": dc_id, "clientId": dc_client_id,
                             "clientType": dc_client_type, "userSessionId": device_token})
        r = self._req("POST", f"{OIDC}/device_authorization/associate_token", headers=oidc_h,
                      json_body={"deviceContext": {"deviceContextId": dc_id,
                                                   "clientId": dc_client_id,
                                                   "clientType": dc_client_type},
                                 "userSessionId": device_token})
        if r is None or r.status_code not in (200, 204):
            return None
        self.log("  ✅ 设备授权已确认，轮询 token …")
        token_h = {**base_h, "amz-sdk-invocation-id": str(uuid.uuid4())}
        deadline = time.time() + 60
        while time.time() < deadline:
            r = self._req("POST", f"{OIDC}/token", headers=token_h,
                          json_body={"clientId": client_id, "clientSecret": client_secret,
                                     "deviceCode": device_code,
                                     "grantType": "urn:ietf:params:oauth:grant-type:device_code"})
            if r is not None and r.status_code == 200:
                tok = r.json()
                self.log(f"  ✅ refreshToken={mask(tok.get('refreshToken', ''))}")
                return {"clientId": client_id, "clientSecret": client_secret,
                        "accessToken": tok.get("accessToken", ""),
                        "refreshToken": tok.get("refreshToken", "")}
            err = ""
            try:
                err = (r.json() or {}).get("error", "") if r is not None else ""
            except Exception:
                err = ""
            if err == "slow_down":
                interval = min(interval + 1, 10)
            elif err not in ("authorization_pending", ""):
                self.log(f"  ❌ token 轮询错误: {err}")
                return None
            time.sleep(interval)
        self.log("  ❌ token 轮询超时")
        return None

    # ═══════════ 编排 ═══════════
    def run(self, *, email: str = "", password: str = "", name: str = "Kiro User",
            otp: str = "", otp_provider=None, start_step: int = 1, stop_after: int = LAST_STEP,
            resume: bool = False) -> dict:
        if resume:
            self.load_session()
        email = email or self.st.get("email", "")
        password = password or self.st.get("password") or _new_password()
        self.st["email"], self.st["password"] = email, password
        ctx: dict = {"email": email, "password": password, "name": name}
        ok = True
        for num, name_, desc in STEP_NAMES:
            if num < start_step or num > stop_after:
                continue
            self.log(f"───── S{num} {name_}：{desc} ─────")
            try:
                if num == 1:
                    redir = self.step1_kiro_init()
                    if not redir:
                        ok = False
                    else:
                        ctx["redirect_url"] = redir
                elif num == 2:
                    ok = self.step2_resolve_wsh(ctx.get("redirect_url") or "")
                elif num == 3:
                    ok = bool(self.step3_signin_user(email))
                elif num == 4:
                    ok = bool(self.step4_signup_flow(email))
                elif num == 5:
                    if not self.step5_telemetry_token():
                        self.log("  ⚠️ 遥测令牌失败，继续（非硬卡点）")
                elif num == 6:
                    ok = bool(self.step6_profile_start())
                elif num == 7:
                    if not email:
                        self.log("  ❌ 缺邮箱（--email 或邮箱池）")
                        ok = False
                    else:
                        ok = self.step7_send_otp(email) is not None
                elif num == 8:
                    code = otp
                    if not code and otp_provider:
                        self.log("  等待邮箱验证码…")
                        code = otp_provider()
                    if not code:
                        self.log("  ❌ 无 OTP（--otp-code 或邮箱池未给码）")
                        ok = False
                    else:
                        ctx["otp"] = code
                        ok = bool(self.step8_verify_otp(str(code), email, name))
                elif num == 9:
                    res = self.step9_open_registration()
                    ok = bool(res)
                    ctx["step9"] = res
                elif num == 10:
                    res = self.step10_set_password(password, email, ctx.get("step9") or {})
                    ok = bool(res)
                    ctx["step10"] = res
                elif num == 11:
                    res = self.step11_final_login(email, ctx.get("step10") or {})
                    ok = bool(res)
                    ctx["step11"] = res
                elif num == 12:
                    res = self.step12_portal_tokens()
                    ok = bool(res)
                    ctx["step12"] = res
                elif num == 13:
                    bearer = (ctx.get("step12") or {}).get("sessionToken") \
                        or self.st.get("session_token", "")
                    if not bearer:
                        self.log("  ❌ 缺 sessionToken（S12 未成功）")
                        ok = False
                    else:
                        dev = self.step12f_device_tokens(bearer)
                        ok = bool(dev)
                        if dev:
                            ctx.update(dev)
            finally:
                self.save_session()
            if not ok:
                self.log(f"✗ 卡在 S{num}（{name_}）")
                ctx["failed_step"] = num
                ctx["failed_step_name"] = name_
                break
            else:
                self.log(f"✓ S{num} 通过")
        return ctx


# ───────── 邮箱适配（复用 core/base_mailbox 体系） ─────────
def build_mailbox(provider: str, extra: dict, proxy: str | None = None):
    from core.base_mailbox import create_mailbox, MailboxAccount  # noqa: PLC0415
    mb = create_mailbox(provider=provider, extra=extra, proxy=proxy)
    acct = mb.get_email() if hasattr(mb, "get_email") else None
    return mb, acct


def mailbox_otp_provider(mb, acct, timeout: int = 150):
    before = set(mb.get_current_ids(acct) or set())

    def _get() -> str:
        return mb.wait_for_code(acct, keyword="", timeout=timeout, before_ids=before,
                                code_pattern=r"(?is)(?:verification\s+code|验证码|校验码)"
                                             r"[^0-9]{0,30}(\d{6})")
    return _get


def _config_extra() -> dict:
    """从项目台账 configs 表读邮箱通道参数（不回显敏感值）。"""
    import sqlite3  # noqa: PLC0415
    db = ROOT / "account_manager.db"
    if not db.exists():
        return {"cfworker_api_url": "https://mail.ee-mail.cc.cd"}
    con = sqlite3.connect(str(db))
    try:
        data = dict(con.execute("select key, value from configs"))
    except Exception:
        data = {}
    finally:
        con.close()
    keys = ("cfworker_api_url", "cfworker_site_password", "cfworker_domain", "cfworker_domains",
            "cfworker_enabled_domains", "cfworker_subdomain", "cfworker_fingerprint",
            "cfworker_admin_token", "cfworker_custom_auth", "cfworker_domain_override",
            "mail_provider", "luckmail_base_url", "luckmail_api_key", "luckmail_project_code",
            "luckmail_email_type", "luckmail_domain", "cloudmail_api_base",
            "cloudmail_admin_email", "cloudmail_admin_password", "cloudmail_domain",
            "duckmail_bearer", "duckmail_domain", "duckmail_api_key")
    extra = {k: data.get(k, "") for k in keys}
    if not extra.get("cfworker_api_url"):
        extra["cfworker_api_url"] = "https://mail.ee-mail.cc.cd"
    return extra


# ───────── CLI ─────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Kiro 纯协议注册（step 化调试）")
    ap.add_argument("--start-step", type=int, default=1)
    ap.add_argument("--stop-after", type=int, default=LAST_STEP)
    ap.add_argument("--list-steps", action="store_true")
    ap.add_argument("--email", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--name", default="Kiro User")
    ap.add_argument("--otp-code", default="")
    ap.add_argument("--mailbox-provider", default="cfworker_anon")
    ap.add_argument("--no-mailbox", action="store_true", help="不用邮箱池（配合 --otp-code 人工调试）")
    ap.add_argument("--proxy", default="")
    ap.add_argument("--tag", default="KIRO")
    ap.add_argument("--session-file", default="")
    ap.add_argument("--resume", action="store_true", help="从会话快照恢复 cookie/状态")
    ap.add_argument("--no-captcha", action="store_true", help="关闭 hCaptcha 尝试")
    args = ap.parse_args(argv)

    if args.list_steps:
        for num, nm, desc in STEP_NAMES:
            print(f"  S{num:<3} {nm:<18} {desc}")
        return 0

    flow = KiroProtocolFlow(proxy=args.proxy or None, tag=args.tag,
                            session_file=args.session_file or None,
                            captcha_enabled=not args.no_captcha)
    otp_provider = None
    email = args.email
    if args.otp_code or args.no_mailbox:
        pass
    else:
        try:
            extra = _config_extra()
            mb, acct = build_mailbox(args.mailbox_provider, extra, proxy=None)
            email = email or (acct.email if acct else "")
            if acct is not None:
                flow.log(f"邮箱池分配: {mask(email, 3)}（provider={args.mailbox_provider}）")
                otp_provider = mailbox_otp_provider(mb, acct)
        except Exception as exc:
            flow.log(f"⚠️ 邮箱池不可用（{type(exc).__name__}: {exc}）→ 需要 --otp-code 人工喂码")

    flow.log(f"起始步 S{args.start_step}，止于 S{args.stop_after}")
    result = flow.run(email=email, password=args.password, name=args.name, otp=args.otp_code,
                      otp_provider=otp_provider, start_step=args.start_step,
                      stop_after=args.stop_after, resume=args.resume)
    print("\n===== 结果摘要 =====")
    print(f"email      : {mask(result.get('email', ''), 3)}")
    print(f"failed_step: {result.get('failed_step', '-')} {result.get('failed_step_name', '')}")
    if result.get("accessToken"):
        print(f"accessToken: {mask(result['accessToken'])}")
    if result.get("refreshToken"):
        print(f"refreshToken: {mask(result['refreshToken'])}")
        print(f"clientId   : {mask(result.get('clientId', ''))}")
        print(f"clientSecret: {mask(result.get('clientSecret', ''))}")
    return 0 if not result.get("failed_step") else 1


if __name__ == "__main__":
    raise SystemExit(main())
