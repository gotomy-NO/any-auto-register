"""统一打码路由层（预留通道）——投产时填 key 即开，默认关闭零风险。

设计（2026-10-05，同日补 funcaptcha）：
    solve(kind, payload) -> token
    kind ∈ {"turnstile", "aliyun2", "data", "funcaptcha", "hcaptcha", "manual"}
      - turnstile : Cloudflare Turnstile → token（xai 注册卡点）
      - aliyun2   : 阿里云验证码 2.0 → captcha_verify_param（zcode 滑块卡点）
      - data      : 图片验证码 → 文字 / DataDome → cookie（payload 二选一）
      - funcaptcha: Arkose FunCaptcha → token（github 注册第二关；**必须带 data-exchange
                    blob**（#funcaptcha 的 data-data-exchange-payload 属性，30s 有效），
                    否则 token 不被 GitHub 接受；参数形状移植 _reg_factory_audit/
                    register_github.py:361-599）
      - hcaptcha  : hCaptcha → gRecaptchaResponse（kiro / chatgpt 注册卡点）。
                    payload 契约：site_key(sitekey) + page_url(url) 必需；
                    可选 user_agent(ua) / proxy / invisible / enterprise。
      - manual    : 人工通道占位——回调 set_manual_handler() 注册的函数

配置（core.config_store，key 不分大小写，.env/环境变量同样生效）：
    captcha_provider          空(默认,不启用) | 2captcha(twocaptcha 别名) | anticaptcha | capmonster
                              | capsolver | anysolver | bypassa | local_solver | manual
    captcha_key               对应平台 API key（全局兜底）
    captcha_key_<provider>    各 provider 自己的 key，优先于 captcha_key（如 captcha_key_capsolver）
    captcha_api_base          可选，API 反代/私有网关地址（覆盖默认端点）
    captcha_fallback_provider 可选，主通道失败后的回退（如 manual）
    captcha_fallback_key      可选，回退通道 key（缺省用 captcha_key）
    captcha_timeout           可选，求解超时秒数（默认 180）

createTask 同构协议（clientKey→taskId→getTaskResult→solution）：
    - 2captcha   https://api.2captcha.com     TurnstileTaskProxyless / AlibabaTaskProxyless /
                 FunCaptchaTaskProxyless（blob 走 task.data）/ DataDomeSliderTask（DataDome，
                 需自备代理）；Turnstile 优先官方 SDK
    - twocaptcha 2captcha 的别名 provider（同实现；key 先取 captcha_key_twocaptcha 再取 captcha_key_2captcha）
    - anti-captcha https://api.anti-captcha.com  TurnstileTaskProxyless（官方 SDK anticaptchaofficial）
    - capmonster https://api.capmonster.cloud   CustomTask class=alibaba（阿里系最稳）/
                 FunCaptchaTask（blob 走 task.data）/ CustomTask class=DataDome（DataDome，需自备代理）
    - capsolver  https://api.capsolver.com     AntiTurnstileTaskProxyless / DatadomeSliderTask /
                 FunCaptchaTaskProxyLess（blob 走 task.data；官方未上架阿里系，选中即报错）
    - anysolver  https://api.anysolver.com     聚合 25 家：Turnstile(ProxyLess) / Alibaba(ProxyLess) /
                 FunCaptchaTokenProxyLess（blob 走 task.data，实测路由 CapMonster $1.10/EZCaptcha $1.20/
                 TwoCaptcha $1.45/AntiCaptcha $3.00 每千）/ DataDomeSlider / DataDomeInterstitial /
                 PopularCaptchaToken[ProxyLess]（= hCaptcha，AnySolver 官方命名，枚举无 HCaptcha*），
                 3 端点全同构
    - bypassa    https://bypassa.com  特例协议：POST /solve（mode=datadome, oauth_token）→
                 轮询 GET /result/:id 拿 cookie；仅 kind=data（DataDome cookie 直售，自带 sticky 代理）；
                 端点/字段形态按官网公开资料实现，投产前需真线核对（见 docs/打码集成-后端-20261005.md）
无 SDK 可用/SDK 覆盖不到的任务走 _http_json（POST）/ _http_get_json（GET）薄壳直连 REST。

kind=data 双语义（payload 决定走哪条）：
    - 带 image_b64              → 图片验证码 → 文字（旧行为，ImageToTextTask）
    - 带 page_url(+captcha_url) → DataDome → cookie（github 卡点；2captcha/capmonster/capsolver/
      anysolver 需自备住宅代理且打码 IP=提交 IP，bypassa 自带 sticky 代理）

声明表（前端/接口展示用，单一事实来源在本模块）：
    PROVIDER_META[pid] = {label, kinds, unit_price(USD/次), price_note, needs_proxy{kind:bool}}
    SUPPORTED_KINDS[pid] / UNIT_PRICE[pid][kind] 由 PROVIDER_META 派生。

测试：tests/test_captcha_router.py（旧 29 例回归）+ tests/test_captcha_router_multi.py
     （5 家新通道桩测，全程不联网、不写库）。
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

__all__ = [
    "CaptchaNotConfigured",
    "CaptchaSolveError",
    "solve",
    "solve_turnstile",
    "solve_aliyun2",
    "solve_image",
    "solve_funcaptcha",
    "solve_hcaptcha",
    "HCAPTCHA_DEMO",
    "GITHUB_FUN",
    "set_manual_handler",
    "register_provider",
    "SUPPORTED_PROVIDERS",
    "SUPPORTED_KINDS",
    "UNIT_PRICE",
    "PROVIDER_META",
    "PROVIDER_ALIASES",
    "KINDS",
]

KINDS = ("turnstile", "aliyun2", "data", "funcaptcha", "hcaptcha", "manual")
SUPPORTED_PROVIDERS = (
    "2captcha", "twocaptcha", "anticaptcha", "capmonster",
    "capsolver", "anysolver", "bypassa", "local_solver", "manual",
)

# 免 key provider（自建/人工通道，solve() 跳过 key 校验）
KEYLESS_PROVIDERS = ("manual", "local_solver")

DEFAULT_API_BASE = {
    "2captcha": "https://api.2captcha.com",
    "twocaptcha": "https://api.2captcha.com",
    "anticaptcha": "https://api.anti-captcha.com",
    "capmonster": "https://api.capmonster.cloud",
    "capsolver": "https://api.capsolver.com",
    "anysolver": "https://api.anysolver.com",
    "bypassa": "https://bypassa.com",
    # 本地自建：services/turnstile_solver（D3-vin Turnstile-Solver v1.2a，camoufox）
    "local_solver": "http://127.0.0.1:8889",
}

# twocaptcha → 2captcha：同实现同端点；key 先取 captcha_key_twocaptcha，再回落 captcha_key_2captcha
PROVIDER_ALIASES = {"twocaptcha": "2captcha"}

# 供应商声明表（价格 USD/次，来自 docs/打码平台调研-支持矩阵-20261005.md 官方页口径，2026-10-05）
# needs_proxy[kind]=True 表示该 kind 实测/官方要求自带住宅代理（DataDome 系：打码 IP 必须等于提交 IP）
PROVIDER_META: dict[str, dict] = {
    "2captcha": {
        "label": "2Captcha",
        "kinds": ("turnstile", "aliyun2", "data", "funcaptcha", "hcaptcha"),
        "unit_price": {"turnstile": 0.00145, "aliyun2": 0.002, "data": 0.00145,
                       "funcaptcha": 0.00145, "hcaptcha": 0.001},
        "price_note": "Turnstile $1.45 / DataDome $1.45 / 阿里 $2 / FunCaptcha $1.45 / hCaptcha $1 每千次（图片另计 $0.5–1）",
        "needs_proxy": {"data": True},
    },
    "twocaptcha": {
        "label": "2Captcha（twocaptcha 别名）",
        "kinds": ("turnstile", "aliyun2", "data", "funcaptcha", "hcaptcha"),
        "unit_price": {"turnstile": 0.00145, "aliyun2": 0.002, "data": 0.00145,
                       "funcaptcha": 0.00145, "hcaptcha": 0.001},
        "price_note": "同 2captcha；别名 provider，key 先取 captcha_key_twocaptcha 再取 captcha_key_2captcha",
        "needs_proxy": {"data": True},
    },
    "anticaptcha": {
        "label": "Anti-Captcha",
        "kinds": ("turnstile", "hcaptcha"),
        "unit_price": {"turnstile": 0.002, "hcaptcha": 0.002},
        "price_note": "Turnstile / hCaptcha 约 $2 /千次；官方未上架阿里系/DataDome 任务",
        "needs_proxy": {},
    },
    "capmonster": {
        "label": "CapMonster Cloud",
        "kinds": ("turnstile", "aliyun2", "data", "funcaptcha", "hcaptcha"),
        "unit_price": {"turnstile": 0.0013, "aliyun2": 0.0008, "data": 0.002,
                       "funcaptcha": 0.0035, "hcaptcha": 0.0008},
        "price_note": "阿里 $0.8（最省，CustomTask class=alibaba 支持 TRACELESS/punishUrl）；Turnstile $1.3；DataDome CustomTask class=DataDome（官方未单列价格，$2/千为估算）；FunCaptcha $3.5；hCaptcha $0.8",
        "needs_proxy": {"data": True},
    },
    "capsolver": {
        "label": "CapSolver",
        "kinds": ("turnstile", "data", "funcaptcha", "hcaptcha"),
        "unit_price": {"turnstile": 0.0012, "data": 0.0025, "funcaptcha": 0.001,
                       "hcaptcha": 0.001},
        "price_note": "Turnstile $1.20（AntiTurnstileTaskProxyLess）/ DataDome $2.50（slider+interstitial 双支持）/ FunCaptcha $1.00（FunCaptchaTaskProxyLess，reg-factory 实测可用）/ hCaptcha $1.00（HCaptchaTaskProxyLess）每千次；不支持阿里系",
        "needs_proxy": {"data": True},
    },
    "anysolver": {
        "label": "AnySolver（聚合 25 家）",
        "kinds": ("turnstile", "aliyun2", "data", "funcaptcha", "hcaptcha"),
        "unit_price": {"turnstile": 0.0015, "aliyun2": 0.00085, "data": 0.0016,
                       "funcaptcha": 0.0011, "hcaptcha": 0.00299},
        "price_note": "上游 provider 原价 1:1 + 充值加价 5%；服务端路由；失败自动退款（FunCaptchaTokenProxyLess 实测路由价：CapMonster $1.10 / EZCaptcha $1.20 / TwoCaptcha $1.45 / AntiCaptcha $3.00 每千，价随路由上游浮动，以上为估算）。hCaptcha 在 AnySolver 命名 PopularCaptcha（枚举无 HCaptcha*），TwoCaptcha 路由价 $2.99/千",
        "needs_proxy": {"data": True},
    },
    "bypassa": {
        "label": "Bypassa",
        "kinds": ("data",),
        "unit_price": {"data": 0.001},
        "price_note": "DataDome cookie 直售 $1 /千次（$0.001/次）；自带 sticky 住宅代理、cookie 复用 ~10min/IP、失败自动退款",
        "needs_proxy": {},
    },
    "local_solver": {
        "label": "本地 Turnstile Solver(:8889)",
        "kinds": ("turnstile",),
        "unit_price": {"turnstile": 0.0},
        "price_note": "本机自建 camoufox Turnstile solver（D3-vin v1.2a，免 key/免费/proxyless）；仅支持 Turnstile",
        "needs_proxy": {},
    },
    "manual": {
        "label": "人工协助",
        "kinds": KINDS,
        "unit_price": {},
        "price_note": "免费（兜底通道：弹有头窗口人工完成）",
        "needs_proxy": {},
    },
}

SUPPORTED_KINDS: dict[str, tuple[str, ...]] = {
    pid: tuple(meta["kinds"]) for pid, meta in PROVIDER_META.items()
}
UNIT_PRICE: dict[str, dict[str, float]] = {
    pid: dict(meta["unit_price"]) for pid, meta in PROVIDER_META.items()
}


class CaptchaNotConfigured(RuntimeError):
    """打码通道未启用（captcha_provider 为空）——调用方应走既有本地/人工流程。"""


class CaptchaSolveError(RuntimeError):
    """打码通道已启用但求解失败（网络/余额/不支持的任务类型等）。"""


# ---------------- 配置读取 ----------------

def get_config(key: str, default: str = "") -> str:
    """读 config_store；库未初始化/后端未起时安静回退（保证独立可测）。"""
    try:
        from core.config_store import config_store

        return str(config_store.get(key, "") or "").strip() or default
    except Exception:
        return default


def _provider_key(provider: str) -> str:
    """provider 专属 key：captcha_key_<pid> 优先，别名回落主 id（twocaptcha→2captcha）。"""
    pid = (provider or "").strip().lower()
    if not pid:
        return ""
    own = get_config(f"captcha_key_{pid}")
    if own:
        return own.strip()
    canonical = PROVIDER_ALIASES.get(pid)
    if canonical:
        return get_config(f"captcha_key_{canonical}").strip()
    return ""


def _resolve(provider: str | None, api_key: str | None, fallback: bool = False) -> tuple[str, str]:
    if fallback:
        provider = provider or get_config("captcha_fallback_provider")
    else:
        provider = provider or get_config("captcha_provider")
    provider = (provider or "").strip().lower()
    if not api_key:
        api_key = _provider_key(provider)
    if not api_key:
        api_key = get_config("captcha_fallback_key") if fallback else get_config("captcha_key")
    return provider, (api_key or "").strip()


# ---------------- HTTP 薄壳（createTask 协议） ----------------

def _http_json(url: str, payload: dict, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _http_get_json(url: str, timeout: float = 30.0) -> dict:
    """GET 薄壳（bypassa 轮询 /result/:id 用）。"""
    req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _protocol_solve(
    api_base: str,
    client_key: str,
    task: dict,
    *,
    timeout_s: float,
    poll_interval: float,
    log: Callable[..., None],
) -> dict:
    """2captcha/capmonster/anticaptcha 同构协议：createTask → 轮询 getTaskResult → solution。"""
    try:
        created = _http_json(
            f"{api_base}/createTask", {"clientKey": client_key, "task": task}, timeout=30
        )
    except Exception as exc:  # 网络/HTTP 异常统一转 CaptchaSolveError，保证回退链可用
        raise CaptchaSolveError(f"createTask 请求失败: {exc}") from exc
    if created.get("errorId"):
        raise CaptchaSolveError(f"createTask 失败: {created.get('errorCode')} {created.get('errorDescription')}")
    task_id = created.get("taskId")
    if not task_id:
        raise CaptchaSolveError(f"createTask 未返回 taskId: {created}")
    log(f"[captcha] task 创建 ok id={task_id} type={task.get('type')}")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(poll_interval)
        try:
            result = _http_json(
                f"{api_base}/getTaskResult", {"clientKey": client_key, "taskId": task_id}, timeout=30
            )
        except Exception as exc:
            raise CaptchaSolveError(f"getTaskResult 请求失败: {exc}") from exc
        if result.get("errorId"):
            raise CaptchaSolveError(f"getTaskResult 失败: {result.get('errorCode')} {result.get('errorDescription')}")
        if result.get("status") == "ready":
            solution = result.get("solution") or {}
            log(f"[captcha] task 完成 id={task_id} 耗时≈{timeout_s - (deadline - time.time()):.0f}s")
            return solution
    raise CaptchaSolveError(f"打码超时（{timeout_s:.0f}s）task={task_id}")


# ---------------- 结果解包 ----------------

def _extract_turnstile(solution: dict) -> str:
    token = str(solution.get("token") or solution.get("code") or "").strip()
    if not token:
        raise CaptchaSolveError(f"solution 无 token: {list(solution)}")
    return token


def _extract_aliyun(solution: dict) -> str:
    """阿里系返回形态兼容：2captcha=solution.data.tokens(str/dict)、capmonster=data/token。"""
    candidates: list[Any] = []
    data = solution.get("data")
    if isinstance(data, dict):
        candidates.append(data.get("tokens"))
    elif isinstance(data, str):
        candidates.append(data)
    candidates.extend([solution.get("tokens"), solution.get("token"), solution.get("code")])
    for value in candidates:
        text = str(value or "").strip()
        if not text:
            continue
        if text.startswith("{"):
            try:
                json.loads(text)  # 合法 JSON（captcha_verify_param）原样返回
                return text
            except Exception:
                pass
        return text
    raise CaptchaSolveError(f"solution 无阿里 captcha_verify_param: {list(solution)}")


def _extract_text(solution: dict) -> str:
    text = str(solution.get("text") or solution.get("code") or solution.get("words") or "").strip()
    if not text:
        raise CaptchaSolveError(f"solution 无文字: {list(solution)}")
    return text


def _extract_cookie(solution: dict) -> str:
    """DataDome 系解包：solution.cookie（datadome=xxx）。"""
    cookie = str(solution.get("cookie") or solution.get("token") or solution.get("code") or "").strip()
    if not cookie:
        raise CaptchaSolveError(f"solution 无 cookie: {list(solution)}")
    return cookie


_PROXY_RE = re.compile(r"^(https?|socks5)://(?:([^:/@]+)(?::([^@/]*))?@)?([^:/@]+):(\d+)/?$")


def _parse_proxy(proxy: str) -> dict:
    """proxy URL → createTask 代理字段（proxyType/Address/Port/Login/Password）。"""
    m = _PROXY_RE.match(str(proxy or "").strip())
    if not m:
        raise CaptchaSolveError(f"proxy 无法解析: {proxy!r}")
    scheme, login, password, address, port = m.groups()
    fields: dict = {
        "proxyType": "socks5" if scheme == "socks5" else "http",
        "proxyAddress": address,
        "proxyPort": int(port),
    }
    if login:
        fields["proxyLogin"] = login
    if password:
        fields["proxyPassword"] = password
    return fields


def _require_datadome_proxy(payload: dict, provider: str) -> None:
    """DataDome 硬约束：打码 IP 必须等于提交 IP（datadome cookie 绑 IP）——无代理直接拒绝。"""
    if not str(payload.get("proxy") or "").strip():
        raise CaptchaSolveError(
            f"{provider} DataDome 需自备住宅代理（payload.proxy，打码 IP 必须等于提交 IP）；"
            "免代理试 bypassa（自带 sticky 代理）"
        )


# ---------------- Arkose FunCaptcha（github 注册第二关） ----------------

# 实测固定参数（_reg_factory_audit/register_github.py:73-75，2026-10-05 真线抓取）：
#   公钥 747B83EC-...（GitHub signup 场景专用）、子域 github-api.arkoselabs.com、
#   websiteURL https://github.com/signup。blob（#funcaptcha 的 data-data-exchange-payload，
#   30s 有效）**必须**放进 task.data，否则解出的 token GitHub 不认（reg-factory 注释原话）。
GITHUB_FUN = {
    "public_key": "747B83EC-2CA3-43AD-A7DF-701F286FBABA",
    "subdomain": "github-api.arkoselabs.com",
    "website_url": "https://github.com/signup",
}


def _funcaptcha_task(task_type: str, payload: dict, kind: str = "funcaptcha") -> dict:
    """构造 Arkose FunCaptcha createTask 任务体（reg-factory register_github.py:367-383 口径）。

    payload 字段：
        public_key  必填，Arkose 网站 public key（GitHub = GITHUB_FUN["public_key"]）
        page_url    必填，验证码所在页（GitHub = https://github.com/signup）
        subdomain   可选，Arkose API 子域（如 github-api.arkoselabs.com，不带 https://）
        blob        强烈建议，#funcaptcha 的 data-data-exchange-payload（30s 有效）；
                    拼进 task.data = json.dumps({"blob": blob}) —— 漏了必失败
        user_agent  可选，透传 userAgent
    proxyless 形态（token 不绑 IP），有代理需求再说。
    """
    _require(payload, ("public_key", "page_url"), kind)
    task: dict = {
        "type": task_type,
        "websiteURL": payload["page_url"],
        "websitePublicKey": payload["public_key"],
    }
    if payload.get("subdomain"):
        task["funcaptchaApiJSSubdomain"] = f"https://{str(payload['subdomain']).strip('/')}"
    if payload.get("user_agent"):
        task["userAgent"] = payload["user_agent"]
    blob = str(payload.get("blob") or "").strip()
    if blob:
        # 关键：data-exchange blob 经 task.data 传，值是 JSON 串 {"blob": "..."}
        task["data"] = json.dumps({"blob": blob})
    return task


def _extract_fun_token(solution: dict) -> str:
    """FunCaptcha 解包：solution.token（兼容 gRecaptchaResponse 别名）。"""
    token = str(solution.get("token") or solution.get("gRecaptchaResponse")
                or solution.get("code") or "").strip()
    if not token:
        raise CaptchaSolveError(f"solution 无 FunCaptcha token: {list(solution)}")
    return token


# ---------------- hCaptcha（kiro / chatgpt 卡点） ----------------

# hCaptcha 官方 demo 站（公开测试 sitekey，非任何生产资产）——真线联调/验收用。
#   页面 https://demo.hcaptcha.com/ 内嵌 widget 的 data-sitekey。
HCAPTCHA_DEMO = {
    "site_key": "10000000-ffff-ffff-ffff-000000000001",
    "page_url": "https://demo.hcaptcha.com/",
}


def _hcaptcha_fields(payload: dict, kind: str = "hcaptcha") -> dict:
    """hCaptcha payload 契约归一化。

    必需（同时接受下划线与短名两种写法，兼容各调用点）：
        site_key  / sitekey   —— hCaptcha 站点 sitekey（widget 的 data-sitekey）
        page_url  / url       —— hCaptcha 所在页面 URL（含域名与路径）
    可选：
        user_agent / ua       —— 透传 UA（部分站点 token 与 UA 绑定）
        proxy                 —— 住宅代理 URL（proxyless 通道忽略；带代理通道使用）
        invisible             —— True 走不可见模式（isInvisible）
        enterprise            —— True 走企业版（enterprisePayload）
    返回归一化 dict（键固定 site_key / page_url / user_agent? / proxy? / invisible? / enterprise?）。
    """
    site_key = str(payload.get("site_key") or payload.get("sitekey") or "").strip()
    page_url = str(payload.get("page_url") or payload.get("url") or "").strip()
    missing = []
    if not site_key:
        missing.append("site_key")
    if not page_url:
        missing.append("page_url")
    if missing:
        raise CaptchaSolveError(
            f"kind={kind} payload 缺少字段: {', '.join(missing)}（别名 sitekey/url 亦可）")
    out: dict = {"site_key": site_key, "page_url": page_url}
    ua = str(payload.get("user_agent") or payload.get("ua") or "").strip()
    if ua:
        out["user_agent"] = ua
    proxy = str(payload.get("proxy") or "").strip()
    if proxy:
        out["proxy"] = proxy
    for flag in ("invisible", "enterprise"):
        if payload.get(flag) in (True, "1", 1, "true", "True"):
            out[flag] = True
    return out


def _extract_hcaptcha(solution: dict) -> str:
    """hCaptcha 解包：2captcha/capsolver/capmonster/anticaptcha 统一落
    solution.gRecaptchaResponse（hCaptcha 沿用 reCAPTCHA 响应字段名）；兼容 token/code 别名。"""
    token = str(solution.get("gRecaptchaResponse") or solution.get("token")
                or solution.get("code") or "").strip()
    if not token:
        raise CaptchaSolveError(f"solution 无 hCaptcha token（gRecaptchaResponse）: {list(solution)}")
    return token


def _hcaptcha_task(task_type: str, payload: dict, *, kind: str = "hcaptcha",
                   proxy_field: bool = False) -> dict:
    """构造 hCaptcha createTask 任务体（2captcha/capmonster/anticaptcha/capsolver 同构）。

    task_type 由各 provider 决定（HCaptchaTaskProxyless / HCaptchaTaskProxyLess）；
    proxy_field=True 时把解析后的代理字段拼进 task（带代理通道）。
    """
    fields = _hcaptcha_fields(payload, kind)
    task: dict = {
        "type": task_type,
        "websiteURL": fields["page_url"],
        "websiteKey": fields["site_key"],
    }
    if fields.get("user_agent"):
        task["userAgent"] = fields["user_agent"]
    if fields.get("invisible"):
        task["isInvisible"] = True
    if fields.get("enterprise"):
        task["enterprisePayload"] = {}
    if proxy_field and fields.get("proxy"):
        task.update(_parse_proxy(fields["proxy"]))
    return task


# ---------------- 各 provider 实现 ----------------

def _require(payload: dict, keys: tuple[str, ...], kind: str) -> None:
    missing = [k for k in keys if not str(payload.get(k, "") or "").strip()]
    if missing:
        raise CaptchaSolveError(f"kind={kind} payload 缺少字段: {', '.join(missing)}")


def _solve_2captcha(kind: str, payload: dict, api_key: str, *, timeout_s: float,
                    poll_interval: float, log: Callable[..., None]) -> str:
    api_base = get_config("captcha_api_base") or DEFAULT_API_BASE["2captcha"]
    if kind == "turnstile":
        try:  # 优先官方 SDK
            from twocaptcha import TwoCaptcha

            opts: dict = {}
            for src, dst in (("action", "action"), ("cdata", "data"), ("pagedata", "pagedata"),
                             ("user_agent", "useragent")):
                if payload.get(src):
                    opts[dst] = payload[src]
            solver = TwoCaptcha(api_key, **({"server": api_base} if get_config("captcha_api_base") else {}))
            solver.request_timeout = max(30, int(timeout_s))
            result = solver.turnstile(sitekey=payload["site_key"], url=payload["page_url"], **opts)
            token = str(result.get("code") or "").strip()
            if not token:
                raise CaptchaSolveError(f"2captcha SDK 无 code: {result}")
            return token
        except ImportError:
            pass
        except CaptchaSolveError:
            raise
        except Exception as exc:  # SDK 业务错误统一转 CaptchaSolveError
            raise CaptchaSolveError(f"2captcha turnstile 失败: {exc}") from exc
        # SDK 缺位 → REST 薄壳
        task = {"type": "TurnstileTaskProxyless", "websiteURL": payload["page_url"],
                "websiteKey": payload["site_key"]}
        for src, dst in (("action", "action"), ("cdata", "cData"), ("pagedata", "chlPageData"),
                         ("user_agent", "userAgent")):
            if payload.get(src):
                task[dst] = payload[src]
        return _extract_turnstile(_protocol_solve(api_base, api_key, task, timeout_s=timeout_s,
                                                  poll_interval=poll_interval, log=log))
    if kind == "aliyun2":
        _require(payload, ("page_url", "scene_id", "prefix"), kind)
        task: dict = {"type": "AlibabaTaskProxyless", "websiteUrl": payload["page_url"],
                      "sceneId": payload["scene_id"], "prefix": payload["prefix"]}
        for src, dst in (("region", "region"), ("user_id", "userId"),
                         ("user_user_id", "userUserId"), ("verify_type", "verifyType"),
                         ("user_certify_id", "UserCertifyId"), ("api_get_lib", "apiGetLib"),
                         ("user_agent", "userAgent")):
            if payload.get(src):
                task[dst] = payload[src]
        if payload.get("proxy"):
            task = _with_proxy_alibaba(task, payload["proxy"])
        return _extract_aliyun(_protocol_solve(api_base, api_key, task, timeout_s=timeout_s,
                                               poll_interval=poll_interval, log=log))
    if kind == "data":
        # 双语义：image_b64 → 图片 OCR；page_url → DataDome cookie（github 卡点）
        if not (payload.get("image_b64") or payload.get("image")):
            if not str(payload.get("page_url") or "").strip():
                raise CaptchaSolveError("kind=data 需要 image_b64（图片 OCR）或 page_url（DataDome）二选一")
            _require_datadome_proxy(payload, "2captcha")
            task = {"type": "DataDomeSliderTask", "websiteURL": payload["page_url"]}
            if payload.get("captcha_url"):
                task["captchaURL"] = payload["captcha_url"]
            if payload.get("user_agent"):
                task["userAgent"] = payload["user_agent"]
            task.update(_parse_proxy(payload["proxy"]))
            return _extract_cookie(_protocol_solve(api_base, api_key, task, timeout_s=timeout_s,
                                                   poll_interval=poll_interval, log=log))
        body = _image_body(payload)
        return _extract_text(_protocol_solve(
            api_base, api_key,
            {"type": "ImageToTextTask", "body": body},
            timeout_s=timeout_s, poll_interval=1.0, log=log))
    if kind == "funcaptcha":
        # reg-factory: FunCaptchaTaskProxyless，blob 经 task.data（json 串）传，漏了 token 不被接受
        task = _funcaptcha_task("FunCaptchaTaskProxyless", payload, kind)
        return _extract_fun_token(_protocol_solve(api_base, api_key, task, timeout_s=timeout_s,
                                                  poll_interval=poll_interval, log=log))
    if kind == "hcaptcha":
        # 2captcha：HCaptchaTaskProxyless（免代理）/ HCaptchaTask（带代理，走 payload.proxy）
        fields = _hcaptcha_fields(payload, kind)
        has_proxy = bool(fields.get("proxy"))
        task = _hcaptcha_task("HCaptchaTask" if has_proxy else "HCaptchaTaskProxyless",
                              payload, kind=kind, proxy_field=has_proxy)
        return _extract_hcaptcha(_protocol_solve(api_base, api_key, task, timeout_s=timeout_s,
                                                 poll_interval=poll_interval, log=log))
    raise CaptchaSolveError(f"2captcha 不支持 kind={kind}（manual 请直接用 provider=manual）")


def _with_proxy_alibaba(task: dict, proxy: str) -> dict:
    """带代理 → 2captcha AlibabaTask（自带代理池版为 Proxyless）。"""
    task = dict(task)
    task["type"] = "AlibabaTask"
    task.update(_parse_proxy(proxy))
    return task


def _solve_anticaptcha(kind: str, payload: dict, api_key: str, *, timeout_s: float,
                       poll_interval: float, log: Callable[..., None]) -> str:
    api_base = get_config("captcha_api_base") or DEFAULT_API_BASE["anticaptcha"]
    if kind == "hcaptcha":
        # anti-captcha 官方支持：HCaptchaTaskProxyless（返回 gRecaptchaResponse）
        task = _hcaptcha_task("HCaptchaTaskProxyless", payload, kind=kind)
        return _extract_hcaptcha(_protocol_solve(api_base, api_key, task, timeout_s=timeout_s,
                                                 poll_interval=poll_interval, log=log))
    if kind != "turnstile":
        raise CaptchaSolveError(
            "anti-captcha 官方未上架阿里系/图片任务，kind=aliyun2|data 请用 2captcha 或 capmonster"
        )
    _require(payload, ("page_url", "site_key"), kind)
    try:  # 优先官方 SDK
        from anticaptchaofficial.turnstileproxyless import turnstileProxyless

        solver = turnstileProxyless()
        solver.set_key(api_key)
        solver.set_website_url(payload["page_url"])
        solver.set_website_key(payload["site_key"])
        if payload.get("action"):
            solver.set_action(payload["action"])
        if payload.get("cdata"):
            solver.set_cdata(payload["cdata"])
        if payload.get("pagedata"):
            solver.set_chlpagedata(payload["pagedata"])
        solver.set_soft_id(0)
        solver.set_verbose(0)
        token = solver.solve_and_return_solution()
        if token == 0 or not token:
            raise CaptchaSolveError(f"anticaptcha turnstile 失败: {solver.err_string}")
        return str(token)
    except ImportError:
        pass
    except CaptchaSolveError:
        raise
    except Exception as exc:
        raise CaptchaSolveError(f"anticaptcha turnstile 失败: {exc}") from exc
    task = {"type": "TurnstileTaskProxyless", "websiteURL": payload["page_url"],
            "websiteKey": payload["site_key"]}
    for src, dst in (("action", "action"), ("cdata", "cData"), ("pagedata", "chlPageData")):
        if payload.get(src):
            task[dst] = payload[src]
    return _extract_turnstile(_protocol_solve(api_base, api_key, task, timeout_s=timeout_s,
                                              poll_interval=poll_interval, log=log))


def _solve_capmonster(kind: str, payload: dict, api_key: str, *, timeout_s: float,
                      poll_interval: float, log: Callable[..., None]) -> str:
    api_base = get_config("captcha_api_base") or DEFAULT_API_BASE["capmonster"]
    if kind == "turnstile":
        _require(payload, ("page_url", "site_key"), kind)
        task = {"type": "TurnstileTaskProxyless", "websiteURL": payload["page_url"],
                "websiteKey": payload["site_key"]}
        return _extract_turnstile(_protocol_solve(api_base, api_key, task, timeout_s=timeout_s,
                                                  poll_interval=poll_interval, log=log))
    if kind == "aliyun2":
        _require(payload, ("page_url", "scene_id", "prefix"), kind)
        metadata = {"sceneId": payload["scene_id"], "prefix": payload["prefix"]}
        for src, dst in (("user_id", "userId"), ("user_user_id", "userUserId"),
                         ("verify_type", "verifyType"), ("region", "region"),
                         ("user_certify_id", "UserCertifyId"), ("api_get_lib", "apiGetLib")):
            if payload.get(src):
                metadata[dst] = payload[src]
        task: dict = {"type": "CustomTask", "class": "alibaba", "websiteURL": payload["page_url"],
                      "metadata": metadata}
        if payload.get("user_agent"):
            task["userAgent"] = payload["user_agent"]
        if payload.get("proxy"):
            m = re.match(r"^(https?|socks5)://(?:([^:/@]+)(?::([^@/]*))?@)?([^:/@]+):(\d+)/?$",
                         payload["proxy"].strip())
            if not m:
                raise CaptchaSolveError(f"proxy 无法解析: {payload['proxy']!r}")
            scheme, login, password, address, port = m.groups()
            task["proxyType"] = "socks5" if scheme == "socks5" else "http"
            task["proxyAddress"] = address
            task["proxyPort"] = int(port)
            if login:
                task["proxyLogin"] = login
            if password:
                task["proxyPassword"] = password
        return _extract_aliyun(_protocol_solve(api_base, api_key, task, timeout_s=timeout_s,
                                               poll_interval=poll_interval, log=log))
    if kind == "data":
        if not (payload.get("image_b64") or payload.get("image")):
            if not str(payload.get("page_url") or "").strip():
                raise CaptchaSolveError("kind=data 需要 image_b64（图片 OCR）或 page_url（DataDome）二选一")
            _require_datadome_proxy(payload, "capmonster")
            # 官方形态：CustomTask class=DataDome（需自备代理）
            task: dict = {"type": "CustomTask", "class": "DataDome",
                          "websiteURL": payload["page_url"]}
            if payload.get("captcha_url"):
                task["captchaUrl"] = payload["captcha_url"]
            if payload.get("device_type"):
                task["metadata"] = {"type": payload["device_type"]}
            if payload.get("user_agent"):
                task["userAgent"] = payload["user_agent"]
            task.update(_parse_proxy(payload["proxy"]))
            return _extract_cookie(_protocol_solve(api_base, api_key, task, timeout_s=timeout_s,
                                                   poll_interval=poll_interval, log=log))
        body = _image_body(payload)
        return _extract_text(_protocol_solve(
            api_base, api_key,
            {"type": "ImageToTextTask", "body": body},
            timeout_s=timeout_s, poll_interval=1.0, log=log))
    if kind == "funcaptcha":
        # reg-factory 口径：FunCaptchaTask（blob 同样经 task.data 传）
        task = _funcaptcha_task("FunCaptchaTask", payload, kind)
        return _extract_fun_token(_protocol_solve(api_base, api_key, task, timeout_s=timeout_s,
                                                  poll_interval=poll_interval, log=log))
    if kind == "hcaptcha":
        task = _hcaptcha_task("HCaptchaTaskProxyless", payload, kind=kind)
        return _extract_hcaptcha(_protocol_solve(api_base, api_key, task, timeout_s=timeout_s,
                                                 poll_interval=poll_interval, log=log))
    raise CaptchaSolveError(f"capmonster 不支持 kind={kind}")


def _image_body(payload: dict) -> str:
    body = str(payload.get("image_b64") or payload.get("image") or "").strip()
    if not body:
        raise CaptchaSolveError("kind=data payload 缺少 image_b64")
    if body.startswith("data:"):  # data:image/png;base64,xxx → xxx
        body = body.split(",", 1)[-1]
    try:
        base64.b64decode(body, validate=True)  # 早失败：非 base64 直接抛
    except Exception as exc:
        raise CaptchaSolveError(f"image_b64 不是合法 base64: {exc}") from exc
    return body


def _solve_capsolver(kind: str, payload: dict, api_key: str, *, timeout_s: float,
                     poll_interval: float, log: Callable[..., None]) -> str:
    """CapSolver REST：AntiTurnstileTaskProxyless / DatadomeSliderTask；官方未上架阿里系。"""
    api_base = get_config("captcha_api_base") or DEFAULT_API_BASE["capsolver"]
    if kind == "turnstile":
        _require(payload, ("page_url", "site_key"), kind)
        task = {"type": "AntiTurnstileTaskProxyLess", "websiteURL": payload["page_url"],
                "websiteKey": payload["site_key"]}
        for src, dst in (("action", "metadata.action"), ("cdata", "metadata.cdata")):
            if payload.get(src):
                root, _, leaf = dst.partition(".")
                task.setdefault("metadata", {})[leaf] = payload[src]
        return _extract_turnstile(_protocol_solve(api_base, api_key, task, timeout_s=timeout_s,
                                                  poll_interval=poll_interval, log=log))
    if kind == "data":
        if not (payload.get("image_b64") or payload.get("image")):
            if not str(payload.get("page_url") or "").strip():
                raise CaptchaSolveError("kind=data 需要 image_b64（图片 OCR）或 page_url（DataDome）二选一")
            _require_datadome_proxy(payload, "capsolver")
            # 官方明确 slider+interstitial 双支持；slider 优先，interstitial 由 payload.form 选拦截页
            task_type = "DatadomeSliderTask" if str(payload.get("form") or "slider") != "interstitial" \
                else "DatadomeInterstitialTask"
            task = {"type": task_type, "websiteURL": payload["page_url"]}
            if payload.get("captcha_url"):
                task["captchaUrl"] = payload["captcha_url"]
            if payload.get("user_agent"):
                task["userAgent"] = payload["user_agent"]
            task.update(_parse_proxy(payload["proxy"]))
            return _extract_cookie(_protocol_solve(api_base, api_key, task, timeout_s=timeout_s,
                                                   poll_interval=poll_interval, log=log))
        body = _image_body(payload)
        return _extract_text(_protocol_solve(
            api_base, api_key,
            {"type": "ImageToTextTask", "body": body},
            timeout_s=timeout_s, poll_interval=1.0, log=log))
    if kind == "funcaptcha":
        # reg-factory 实测可用：FunCaptchaTaskProxyLess，blob 经 task.data 传
        task = _funcaptcha_task("FunCaptchaTaskProxyLess", payload, kind)
        return _extract_fun_token(_protocol_solve(api_base, api_key, task, timeout_s=timeout_s,
                                                  poll_interval=poll_interval, log=log))
    if kind == "hcaptcha":
        # CapSolver 官方：HCaptchaTaskProxyLess（返回 gRecaptchaResponse）
        task = _hcaptcha_task("HCaptchaTaskProxyLess", payload, kind=kind)
        return _extract_hcaptcha(_protocol_solve(api_base, api_key, task, timeout_s=timeout_s,
                                                 poll_interval=poll_interval, log=log))
    raise CaptchaSolveError("capsolver 官方未上架阿里云 2.0（aliyun2）任务，请用 capmonster 或 2captcha")


def _solve_anysolver(kind: str, payload: dict, api_key: str, *, timeout_s: float,
                     poll_interval: float, log: Callable[..., None]) -> str:
    """AnySolver 聚合（25 家，3 端点全同构 createTask/getTaskResult/getBalance）。

    task 类型映射见 docs/打码自动路由-设计-20261005.md §7.1；payload 带 proxy → 取带代理版。
    """
    api_base = get_config("captcha_api_base") or DEFAULT_API_BASE["anysolver"]
    has_proxy = bool(str(payload.get("proxy") or "").strip())
    if kind == "turnstile":
        _require(payload, ("page_url", "site_key"), kind)
        task = {"type": "Turnstile" if has_proxy else "TurnstileProxyLess",
                "websiteURL": payload["page_url"], "websiteKey": payload["site_key"]}
        for src, dst in (("action", "metadata.action"), ("cdata", "metadata.cdata"),
                         ("pagedata", "metadata.chlPageData"), ("user_agent", "userAgent")):
            if payload.get(src):
                if dst.startswith("metadata."):
                    task.setdefault("metadata", {})[dst.split(".", 1)[1]] = payload[src]
                else:
                    task[dst] = payload[src]
        if has_proxy:
            task.update(_parse_proxy(payload["proxy"]))
        return _extract_turnstile(_protocol_solve(api_base, api_key, task, timeout_s=timeout_s,
                                                  poll_interval=poll_interval, log=log))
    if kind == "aliyun2":
        _require(payload, ("page_url", "scene_id", "prefix"), kind)
        metadata = {"sceneId": payload["scene_id"], "prefix": payload["prefix"]}
        for src, dst in (("user_id", "userId"), ("user_user_id", "userUserId"),
                         ("verify_type", "verifyType"), ("region", "region"),
                         ("user_certify_id", "UserCertifyId"), ("api_get_lib", "apiGetLib")):
            if payload.get(src):
                metadata[dst] = payload[src]
        task = {"type": "Alibaba" if has_proxy else "AlibabaProxyLess",
                "websiteURL": payload["page_url"], "metadata": metadata}
        if payload.get("user_agent"):
            task["userAgent"] = payload["user_agent"]
        if has_proxy:
            task.update(_parse_proxy(payload["proxy"]))
        return _extract_aliyun(_protocol_solve(api_base, api_key, task, timeout_s=timeout_s,
                                               poll_interval=poll_interval, log=log))
    if kind == "data":
        if not (payload.get("image_b64") or payload.get("image")):
            if not str(payload.get("page_url") or "").strip():
                raise CaptchaSolveError("kind=data 需要 image_b64（图片 OCR）或 page_url（DataDome）二选一")
            _require_datadome_proxy(payload, "anysolver")
            # 滑块优先；整页盾 payload.form=interstitial；ProxyLess 上游未必常驻 → 带 proxy 时直接用带代理版
            task_type = "DataDomeInterstitial" if str(payload.get("form") or "slider") == "interstitial" \
                else "DataDomeSlider"
            task = {"type": task_type, "websiteURL": payload["page_url"]}
            if payload.get("captcha_url"):
                task["captchaURL"] = payload["captcha_url"]
            if payload.get("user_agent"):
                task["userAgent"] = payload["user_agent"]
            task.update(_parse_proxy(payload["proxy"]))
            return _extract_cookie(_protocol_solve(api_base, api_key, task, timeout_s=timeout_s,
                                                   poll_interval=poll_interval, log=log))
        body = _image_body(payload)
        return _extract_text(_protocol_solve(
            api_base, api_key,
            {"type": "ImageToTextTask", "body": body},
            timeout_s=timeout_s, poll_interval=1.0, log=log))
    if kind == "funcaptcha":
        # 实测聚合路由：FunCaptchaTokenProxyLess（CapMonster $1.10/EZCaptcha $1.20/
        # TwoCaptcha $1.45/AntiCaptcha $3.00 每千）；blob 经 task.data 传
        task = _funcaptcha_task("FunCaptchaTokenProxyLess", payload, kind)
        return _extract_fun_token(_protocol_solve(api_base, api_key, task, timeout_s=timeout_s,
                                                  poll_interval=poll_interval, log=log))
    if kind == "hcaptcha":
        # AnySolver 把 hCaptcha 命名为 PopularCaptcha（官方 createTask 枚举无 HCaptcha*，
        # 对照 TwoCaptcha/CapSolver 名单确认）；invisible → PopularCaptchaInvisibleToken*。
        # 代理经 task.proxy（URL 字符串）传，非 2captcha 的 proxyAddress 扁平字段。
        fields = _hcaptcha_fields(payload, kind)
        has_proxy = bool(fields.get("proxy"))
        base = "PopularCaptchaInvisibleToken" if fields.get("invisible") else "PopularCaptchaToken"
        task = _hcaptcha_task(base if has_proxy else base + "ProxyLess",
                              payload, kind=kind, proxy_field=False)
        if has_proxy:
            task["proxy"] = fields["proxy"]
        return _extract_hcaptcha(_protocol_solve(api_base, api_key, task, timeout_s=timeout_s,
                                                 poll_interval=poll_interval, log=log))
    raise CaptchaSolveError(f"anysolver 不支持 kind={kind}")


def _solve_bypassa(kind: str, payload: dict, api_key: str, *, timeout_s: float,
                   poll_interval: float, log: Callable[..., None]) -> str:
    """Bypassa：DataDome cookie 直售（自带 sticky 代理、失败自动退款），仅 kind=data。

    公开资料形态（bypassa.com/datadome-solver，2026-10 爬取）：
        POST {base}/solve      body={mode:"datadome", oauth_token:<key>, url, proxy?}
                              → {id} 或 {error}
        GET  {base}/result/:id → {status:"pending"|"done", result:{cookie}|error}
    ⚠️ 端点/字段为官网公开页口径，未真线核对（不联网原则）——投产前先 POST /test 试单；
       如字段有出入只需改本函数，注册表/接口层零影响（见 docs/打码集成-后端-20261005.md）。
    """
    if kind != "data":
        raise CaptchaSolveError("bypassa 仅支持 kind=data（DataDome cookie 直售）")
    if payload.get("image_b64") or payload.get("image"):
        raise CaptchaSolveError("bypassa 不做图片 OCR：图片码请用 2captcha/capmonster")
    page_url = str(payload.get("page_url") or "").strip()
    if not page_url:
        raise CaptchaSolveError("kind=data（bypassa）payload 缺少 page_url")
    api_base = (get_config("captcha_api_base") or DEFAULT_API_BASE["bypassa"]).rstrip("/")
    body: dict = {"mode": "datadome", "oauth_token": api_key, "url": page_url}
    if payload.get("proxy"):
        body["proxy"] = payload["proxy"]
    if payload.get("user_agent"):
        body["userAgent"] = payload["user_agent"]
    created = _http_json(f"{api_base}/solve", body, timeout=30)
    if created.get("error"):
        raise CaptchaSolveError(f"bypassa create 失败: {created.get('error')}")
    solve_id = str(created.get("id") or created.get("taskId") or "").strip()
    if not solve_id:
        raise CaptchaSolveError(f"bypassa create 未返回任务 id: {created}")
    log(f"[captcha] bypassa task 创建 ok id={solve_id}")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(poll_interval)
        result = _http_get_json(f"{api_base}/result/{solve_id}", timeout=30)
        if result.get("error"):
            raise CaptchaSolveError(f"bypassa 求解失败: {result.get('error')}")
        if str(result.get("status") or "").lower() in ("done", "success", "ready", "completed"):
            inner = result.get("result") or {}
            cookie = str((inner.get("cookie") if isinstance(inner, dict) else inner)
                         or result.get("cookie") or "").strip()
            if not cookie:
                raise CaptchaSolveError(f"bypassa 完成但无 cookie: {result}")
            return cookie
    raise CaptchaSolveError(f"bypassa 求解超时（{timeout_s:.0f}s）task={solve_id}")


# ---------------- 本地自建 solver（:8889，services/turnstile_solver） ----------------

def _solve_local_solver(kind: str, payload: dict, api_key: str, *, timeout_s: float,
                        poll_interval: float, log: Callable[..., None]) -> str:
    """本地自建 Turnstile solver（services/turnstile_solver，D3-vin v1.2a，camoufox 引擎）。

    仅支持 kind=turnstile；免 key；proxyless（solver 侧未开 --proxy）。
    契约（2026-10-06 真线核对）：
        GET {base}/turnstile?url=&sitekey=[&action=&cdata=] → {errorId, taskId}
        GET {base}/result?id=  → {status:"processing"}
                                | {errorId:0,status:"ready",solution:{token}}
                                | {errorId:1,errorCode:"ERROR_CAPTCHA_UNSOLVABLE",...}
    base 优先级：captcha_api_base → 环境变量 LOCAL_SOLVER_URL → DEFAULT_API_BASE。
    """
    if kind != "turnstile":
        raise CaptchaSolveError(
            "local_solver 仅支持 kind=turnstile（本机 services/turnstile_solver，无 hCaptcha/滑块）")
    _require(payload, ("page_url", "site_key"), kind)
    base = (get_config("captcha_api_base")
            or os.getenv("LOCAL_SOLVER_URL", "")
            or DEFAULT_API_BASE["local_solver"]).rstrip("/")
    params: dict[str, str] = {"url": str(payload["page_url"]), "sitekey": str(payload["site_key"])}
    if payload.get("action"):
        params["action"] = str(payload["action"])
    if payload.get("cdata"):
        params["cdata"] = str(payload["cdata"])
    query = urllib.parse.urlencode(params)
    try:
        created = _http_get_json(f"{base}/turnstile?{query}", timeout=30)
    except Exception as exc:  # solver 未起/网络异常 → 统一转 CaptchaSolveError，保证回退链可用
        raise CaptchaSolveError(f"local_solver 提交失败: {exc}") from exc
    if created.get("errorId"):
        raise CaptchaSolveError(
            f"local_solver 提交失败: {created.get('errorCode')} {created.get('errorDescription')}")
    task_id = str(created.get("taskId") or "").strip()
    if not task_id:
        raise CaptchaSolveError(f"local_solver 未返回 taskId: {created}")
    log(f"[captcha] local_solver task 创建 ok id={task_id}")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(poll_interval)
        try:
            res = _http_get_json(f"{base}/result?id={urllib.parse.quote(task_id)}", timeout=30)
        except Exception as exc:
            raise CaptchaSolveError(f"local_solver 轮询失败: {exc}") from exc
        if res.get("errorId"):
            raise CaptchaSolveError(
                f"local_solver 求解失败: {res.get('errorCode')} {res.get('errorDescription')}")
        if res.get("status") == "ready":
            return _extract_turnstile(res.get("solution") or {})
    raise CaptchaSolveError(f"local_solver 超时（{timeout_s:.0f}s）task={task_id}")


# ---------------- manual 通道 ----------------

_MANUAL_HANDLER: Callable[[str, dict, float], str] | None = None


def set_manual_handler(fn: Callable[[str, dict, float], str] | None) -> None:
    """注册人工打码回调，签名 fn(kind, payload, timeout_s) -> token/verify_param。

    占位设计：接现有流程语义——Turnstile 用有头窗口人工点击后从
    cf-turnstile-response 读 token；阿里滑块接 aliyun_slider.solve_slider(
    manual_wait_sec=...) 或前端人工代滑回调。
    """
    global _MANUAL_HANDLER
    _MANUAL_HANDLER = fn


def _solve_manual(kind: str, payload: dict, timeout_s: float, log: Callable[..., None]) -> str:
    if _MANUAL_HANDLER is None:
        raise CaptchaSolveError(
            "manual 通道未接人工回调：先 captcha_router.set_manual_handler(fn) "
            "（fn(kind, payload, timeout_s) -> token）"
        )
    log(f"[captcha][manual] 转人工 kind={kind} 等待≤{timeout_s:.0f}s")
    result = _MANUAL_HANDLER(kind, dict(payload), timeout_s)
    token = str(result or "").strip()
    if not token:
        raise CaptchaSolveError("manual 回调返回空 token")
    return token


_SOLVERS = {
    "2captcha": _solve_2captcha,
    "twocaptcha": _solve_2captcha,  # 别名：同实现同端点
    "anticaptcha": _solve_anticaptcha,
    "capmonster": _solve_capmonster,
    "capsolver": _solve_capsolver,
    "anysolver": _solve_anysolver,
    "bypassa": _solve_bypassa,
    "local_solver": _solve_local_solver,
}


def register_provider(name: str, fn: Callable[..., str]) -> None:
    """注册/替换 provider 实现（测试桩或自建网关用）。签名同 _solve_2captcha。"""
    _SOLVERS[str(name).strip().lower()] = fn


# ---------------- 主入口 ----------------

def solve(kind: str, payload: dict | None = None, *, provider: str | None = None,
          api_key: str | None = None, timeout_s: float | None = None,
          poll_interval: float = 3.0, log: Callable[..., None] = print) -> str:
    """统一打码入口：solve(kind, payload) -> token/captcha_verify_param。

    未配置 captcha_provider → CaptchaNotConfigured（调用方回落本地既有流程）。
    主通道 CaptchaSolveError 且配置了 captcha_fallback_provider → 回退一次。
    """
    kind = str(kind or "").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"kind 必须是 {KINDS} 之一，收到 {kind!r}")
    payload = dict(payload or {})
    timeout_s = float(timeout_s or get_config("captcha_timeout", "180") or 180)
    if kind == "manual":
        return _solve_manual(kind, payload, timeout_s, log)

    name, key = _resolve(provider, api_key)
    if not name:
        raise CaptchaNotConfigured(
            "打码通道未启用：config_store 设 captcha_provider(captcha_key) 后自动生效；"
            "未启用时请走既有本地流程"
        )
    if name == "manual":
        return _solve_manual(kind, payload, timeout_s, log)
    fn = _SOLVERS.get(name)
    if fn is None:
        raise CaptchaSolveError(f"未知 provider {name!r}，支持: {SUPPORTED_PROVIDERS}")
    if name not in KEYLESS_PROVIDERS and not key:
        raise CaptchaNotConfigured(f"provider={name} 但 captcha_key 未配置")
    try:
        return fn(kind, payload, key, timeout_s=timeout_s, poll_interval=poll_interval, log=log)
    except CaptchaNotConfigured:
        raise
    except CaptchaSolveError as exc:
        fb_name, fb_key = _resolve(None, None, fallback=True)
        if not fb_name or fb_name == name:
            raise
        log(f"[captcha] 主通道 {name} 失败（{exc}）→ 回退 {fb_name}")
        if fb_name == "manual":
            return _solve_manual(kind, payload, timeout_s, log)
        fb_fn = _SOLVERS.get(fb_name)
        if fb_fn is None:
            raise CaptchaSolveError(f"回退 provider {fb_name!r} 不存在")
        if fb_name not in KEYLESS_PROVIDERS and not fb_key:
            raise CaptchaNotConfigured(f"回退 provider={fb_name} 但未配置 key")
        return fb_fn(kind, payload, fb_key, timeout_s=timeout_s, poll_interval=poll_interval, log=log)


# ---------------- 便捷封装 ----------------

def solve_turnstile(page_url: str, site_key: str, **payload_extra) -> str:
    return solve("turnstile", {"page_url": page_url, "site_key": site_key, **payload_extra})


def solve_aliyun2(page_url: str, scene_id: str, prefix: str, **payload_extra) -> str:
    return solve("aliyun2", {"page_url": page_url, "scene_id": scene_id,
                             "prefix": prefix, **payload_extra})


def solve_image(image_b64: str, **payload_extra) -> str:
    return solve("data", {"image_b64": image_b64, **payload_extra})


def solve_funcaptcha(page_url: str, public_key: str, blob: str = "", **payload_extra) -> str:
    """Arkose FunCaptcha → token（github 第二关）。

    blob = #funcaptcha 的 data-data-exchange-payload（30s 有效，抓到即打）；
    缺省参数直接展开 GITHUB_FUN 即可对准 github 注册场景。
    """
    return solve("funcaptcha", {"page_url": page_url, "public_key": public_key,
                                "blob": blob, **payload_extra})


def solve_hcaptcha(page_url: str, site_key: str, **payload_extra) -> str:
    """hCaptcha → token（kiro / chatgpt 注册卡点）。

    payload 契约：site_key + page_url 必需；可选 user_agent / proxy / invisible / enterprise。
    缺省参数直接展开 HCAPTCHA_DEMO 即可对准 hCaptcha 官方 demo 站做真线联调。
    """
    return solve("hcaptcha", {"page_url": page_url, "site_key": site_key, **payload_extra})
