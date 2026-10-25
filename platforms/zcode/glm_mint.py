r"""GLM Coding Plan 铸钥模块（zcode → GLM 铸钥线）

把 zcode 平台注册产出的 z.ai 会话 token 铸造成 GLM Coding Plan API Key，
铸出的 key 形如 ``{apiKey}.{secretKey}``（点分），与 9Router glm OAuth 流程
铸造的 key 同构，可直接 ``POST /api/providers {provider:"glm", apiKey, name}``
导入 9Router（glm registry: authModes ["oauth","apikey"]，category=apikey）。

铸造链路（逆向自 Z-Accounts-Gateway src-tauri/src/oauth.rs 的 zai 分支，
见 D:\AI\临时\AI算力\投喂区\Z-Accounts-Gateway-source-20260928）：

1. 会话换业务 token：
   ``POST https://api.z.ai/api/auth/z/login  {"token": <chat.z.ai JWT>}``
   → ``data.access_token``（业务 access_token）
2. 客户信息选机构/项目：
   ``GET https://api.z.ai/api/biz/customer/getCustomerInfo``（Bearer 业务token）
   → organizations[]，过滤 projectType=="2" 的项目，优先「默认机构/默认项目」
3. 列出/创建 API Key（名为 ``zcode-api-key``）：
   ``GET/POST https://api.z.ai/api/biz/v1/organization/{org}/projects/{proj}/api_keys``
   → 已有则复用，没有则 POST 创建
4. 复制密钥取 secretKey：
   ``GET {keys_url}/copy/{apiKey}`` → ``secretKey``
5. 组装：``apiKey + "." + secretKey``

额度探活（决定新号是否有 coding plan 试用额度，9Router glm registry 的 usage 端点）：
``GET https://api.z.ai/api/monitor/usage/quota/limit``

验收纪律：
- 任何步骤拿不到预期字段都抛 :class:`GlmMintError`（带 step），不静默降级；
- 完整 key 不进日志/repr，:class:`MintResult.__repr__` 只留 prefix8+len；
- 网络传输可注入（http 参数），离线桩测不触网。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote

try:  # 运行时依赖；离线/桩测环境允许缺失，纯函数与注入传输不受影响
    import requests
except ImportError:  # pragma: no cover
    requests = None

# ---------------------------------------------------------------- 常量（与上游对齐）

ZAI_API_BASE = "https://api.z.ai"
ZAI_BUSINESS_LOGIN_URL = f"{ZAI_API_BASE}/api/auth/z/login"
ZAI_CUSTOMER_INFO_URL = f"{ZAI_API_BASE}/api/biz/customer/getCustomerInfo"
ZAI_ANTHROPIC_BASE = f"{ZAI_API_BASE}/api/anthropic"
ZAI_QUOTA_LIMIT_URL = f"{ZAI_API_BASE}/api/monitor/usage/quota/limit"

API_KEY_NAME = "zcode-api-key"

# 9Router 侧 glm 连接的导入 payload 形状（panel session cookie 鉴权）
NINE_ROUTER_PROVIDER = "glm"
NINE_ROUTER_VALIDATE_URL = "https://api.z.ai/api/anthropic/v1/messages"
NINE_ROUTER_TEST_MODEL = "glm-4.7"


class GlmMintError(RuntimeError):
    """铸造链路某一步失败。step 标明断点，message 面向日志。"""

    def __init__(self, step: str, message: str, status: Optional[int] = None):
        super().__init__(f"[{step}] {message}")
        self.step = step
        self.status = status


def mask_key(key: str) -> str:
    """完整 key 打码：prefix8 + "..." + len（纪律：完整 key 不进日志/聊天）。"""
    key = str(key or "")
    if len(key) <= 8:
        return f"<masked len={len(key)}>"
    return f"{key[:8]}...<len={len(key)}>"


# ---------------------------------------------------------------- 纯函数（离线可测）

def _as_str_id(v: Any) -> Optional[str]:
    """organizationId/projectId 可能是字符串或数字，统一成非空字符串。"""
    if isinstance(v, str) and v.strip():
        return v.strip()
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        s = str(int(v)) if float(v).is_integer() else str(v)
        if s.strip():
            return s
    return None


def pick_org_project(customer: dict) -> Optional[tuple]:
    """从 getCustomerInfo 响应里选 (organizationId, projectId)。

    规则（对齐 oauth.rs pick_org_project）：
    - root = data（有 data 剥 data）；
    - 项目过滤掉 projectType=="2"；
    - 机构优先 organizationName 含「默认机构」，否则第一个；
    - 项目优先 projectName 含「默认项目」，否则该机构第一个；
    - 找不到可用机构/项目返回 None（不猜）。
    """
    if not isinstance(customer, dict):
        return None
    root = customer.get("data") if isinstance(customer.get("data"), dict) else customer
    orgs = root.get("organizations")
    if not isinstance(orgs, list):
        return None
    cands = []
    for org in orgs:
        if not isinstance(org, dict):
            continue
        org_id = _as_str_id(org.get("organizationId"))
        if not org_id:
            continue
        projects = [p for p in (org.get("projects") or []) if isinstance(p, dict)]
        projects = [p for p in projects if str(p.get("projectType", "")).strip() != "2"]
        projects = [p for p in projects if _as_str_id(p.get("projectId"))]
        if projects:
            cands.append((org, org_id, projects))
    if not cands:
        return None
    org, org_id, projects = next(
        (c for c in cands if "默认机构" in str(c[0].get("organizationName", ""))), cands[0]
    )
    proj = next(
        (p for p in projects if "默认项目" in str(p.get("projectName", ""))), projects[0]
    )
    return org_id, _as_str_id(proj.get("projectId"))


def keys_array(v: Any) -> list:
    """api_keys 响应兼容两种形状：裸数组 / {"data": [...]}。"""
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    if isinstance(v, dict):
        data = v.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    return []


def find_existing_key(entries: list, name: str = API_KEY_NAME) -> Optional[str]:
    """在 api_keys 列表里找名为 zcode-api-key 的 key（只取 apiKey 字段）。"""
    for e in entries:
        if str(e.get("name", "")).strip() == name:
            key = str(e.get("apiKey", "") or "").strip()
            if key:
                return key
    return None


def assemble_key(api_key: str, secret_key: str = "") -> str:
    """组装最终 key：有 secretKey 则 ``key.secret``，无则裸 key（caller 决定是否合法）。"""
    api_key = str(api_key or "").strip()
    secret_key = str(secret_key or "").strip()
    if not api_key:
        raise GlmMintError("assemble_key", "apiKey 为空，无法组装")
    if secret_key:
        return f"{api_key}.{secret_key}"
    return api_key


def build_provider_payload(combined_key: str, name: str = "glm-zcode") -> dict:
    """生成 9Router `POST /api/providers`（panel session cookie）的 body。"""
    combined_key = str(combined_key or "").strip()
    if not combined_key:
        raise GlmMintError("build_provider_payload", "combined key 为空")
    return {"provider": NINE_ROUTER_PROVIDER, "apiKey": combined_key, "name": name}


# ---------------------------------------------------------------- 可注入传输层

class RequestsHttp:
    """默认传输：requests，带超时与可选代理（http(s)://host:port）。

    Clash TUN 环境下访问 api.z.ai 走代理是正常路径；loopback 场景
    （本地 9Router 面板）由调用方另建 opener，不走这里。
    """

    def __init__(self, proxy: str = "", timeout: float = 20.0):
        self.timeout = timeout
        self.proxies = {"http": proxy, "https": proxy} if proxy else None

    def get_json(self, url: str, headers: dict) -> Any:
        resp = requests.get(url, headers=headers, proxies=self.proxies, timeout=self.timeout)
        return _parse_json(resp, url)

    def post_json(self, url: str, payload: dict, headers: dict) -> Any:
        resp = requests.post(
            url, json=payload, headers=headers, proxies=self.proxies, timeout=self.timeout
        )
        return _parse_json(resp, url)


def _parse_json(resp: requests.Response, url: str) -> Any:
    if resp.status_code >= 400:
        raise GlmMintError(
            "http", f"HTTP {resp.status_code} {url[:120]}: {resp.text[:200]}", status=resp.status_code
        )
    try:
        return resp.json()
    except (ValueError, json.JSONDecodeError):
        raise GlmMintError("http", f"非 JSON 响应 {url[:120]}: {resp.text[:200]}", status=resp.status_code)


# ---------------------------------------------------------------- 铸造链路

@dataclass
class MintResult:
    """铸造结果。完整 key 只在 .combined_key（内存），repr 永远打码。"""

    combined_key: str
    steps: list = field(default_factory=list)
    reused: bool = False  # True=复用已有 zcode-api-key，False=新建
    has_secret: bool = True
    org_id: str = ""
    project_id: str = ""

    def __repr__(self) -> str:  # 纪律：完整 key 不进日志
        return (
            f"MintResult(key={mask_key(self.combined_key)}, reused={self.reused}, "
            f"has_secret={self.has_secret}, steps={self.steps})"
        )


def exchange_business_token(chat_token: str, *, http: Optional[RequestsHttp] = None) -> str:
    """步骤1：chat.z.ai 会话 JWT → z.ai 业务 access_token。"""
    http = http or RequestsHttp()
    data = http.post_json(ZAI_BUSINESS_LOGIN_URL, {"token": str(chat_token or "").strip()}, {
        "Content-Type": "application/json",
    })
    token = _dig_access_token(data)
    if not token:
        raise GlmMintError("exchange_token", f"登录响应无 access_token: {str(data)[:200]}")
    return token


def _dig_access_token(v: Any) -> str:
    """兼容 data.access_token / data.accessToken / 顶层裸字段。"""
    for pointer in ("data/access_token", "data/accessToken", "access_token", "accessToken"):
        cur: Any = v
        ok = True
        for part in pointer.split("/"):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and isinstance(cur, str) and cur.strip():
            return cur.strip()
    return ""


def _dig_secret(v: Any) -> str:
    """copy 响应取 secretKey：兼容顶层 / data 嵌套两种形状。

    2026-10-04 api.z.ai 实测 copy 返回 ``{"code":200,"msg":...,"data":{"secretKey":...}}``，
    上游 oauth.rs 只读顶层属其自身缺陷，这里两种都兼容。
    """
    if not isinstance(v, dict):
        return ""
    for pointer in ("secretKey", "data/secretKey", "data/secret_key", "secret_key"):
        cur: Any = v
        ok = True
        for part in pointer.split("/"):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and isinstance(cur, str) and cur.strip():
            return cur.strip()
    return ""


def _customer_info(access_token: str, http: RequestsHttp) -> dict:
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    data = http.get_json(ZAI_CUSTOMER_INFO_URL, headers)
    if not isinstance(data, (dict, list)) or (isinstance(data, dict) and not _root_orgs(data)):
        raise GlmMintError("customer_info", f"getCustomerInfo 无 organizations: {str(data)[:200]}")
    return data


def _root_orgs(customer: dict) -> list:
    root = customer.get("data") if isinstance(customer.get("data"), dict) else customer
    orgs = root.get("organizations")
    return orgs if isinstance(orgs, list) else []


def _keys_url(org_id: str, project_id: str) -> str:
    return (
        f"{ZAI_API_BASE}/api/biz/v1/organization/{quote(org_id, safe='')}"
        f"/projects/{quote(project_id, safe='')}/api_keys"
    )


def mint_coding_plan_key(
    access_token: str,
    *,
    http: Optional[RequestsHttp] = None,
    require_secret: bool = True,
    key_name: str = API_KEY_NAME,
) -> MintResult:
    """步骤2-5：业务 access_token → GLM Coding Plan key（``key.secret``）。

    require_secret=True（zai 分支默认）：拿不到 secretKey 直接抛错；
    require_secret=False：secretKey 缺失时退回裸 key（bigmodel 分支行为）。
    """
    http = http or RequestsHttp()
    steps: list = []
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    customer = _customer_info(access_token, http)
    steps.append("customer_info")
    picked = pick_org_project(customer)
    if not picked:
        raise GlmMintError("pick_org_project", "customerInfo 里没有可用机构/项目")
    org_id, project_id = picked
    steps.append(f"pick_org_project:{org_id[:6]}/{project_id[:6]}")

    keys_url = _keys_url(org_id, project_id)
    existing = find_existing_key(keys_array(http.get_json(keys_url, headers)), key_name)
    reused = existing is not None
    if reused:
        api_key = existing
        steps.append("list_keys:reused")
    else:
        created = http.post_json(keys_url, {"name": key_name}, headers)
        api_key = str(
            (created.get("apiKey") if isinstance(created, dict) else "")
            or _dig_key_from_data(created)
            or ""
        ).strip()
        if not api_key:
            raise GlmMintError("create_key", f"创建 API key 无 apiKey 字段: {str(created)[:200]}")
        steps.append("list_keys:created")

    secret = ""
    copy_resp = http.get_json(f"{keys_url}/copy/{quote(api_key, safe='')}", headers)
    secret = _dig_secret(copy_resp)
    if secret:
        steps.append("copy_secret:ok")
    else:
        if require_secret:
            raise GlmMintError("copy_secret", "copy 接口未返回 secretKey")
        steps.append("copy_secret:empty(fallback-bare-key)")

    return MintResult(
        combined_key=assemble_key(api_key, secret),
        steps=steps,
        reused=reused,
        has_secret=bool(secret),
        org_id=org_id,
        project_id=project_id,
    )


def _dig_key_from_data(created: Any) -> str:
    """创建 key 响应兼容 {"data": {"apiKey": ...}} / 裸 {"apiKey": ...}。"""
    if isinstance(created, dict) and isinstance(created.get("data"), dict):
        return str(created["data"].get("apiKey", "") or "")
    return ""


def mint_from_chat_token(
    chat_token: str,
    *,
    http: Optional[RequestsHttp] = None,
    require_secret: bool = True,
) -> MintResult:
    """便捷入口：zcode 注册产出的 chat.z.ai JWT 一路铸到 coding plan key。"""
    http = http or RequestsHttp()
    access_token = exchange_business_token(chat_token, http=http)
    result = mint_coding_plan_key(access_token, http=http, require_secret=require_secret)
    result.steps.insert(0, "exchange_token")
    return result


def check_quota(combined_key: str, *, http: Optional[RequestsHttp] = None) -> dict:
    """额度探活：GET /api/monitor/usage/quota/limit（9Router glm usage 端点）。

    返回上游原始 JSON（ usage/limit 结构以实际响应为准，调用方自行判读）；
    401/403 会以 GlmMintError(status=...) 抛出——即 key 无效/无 plan。
    """
    http = http or RequestsHttp()
    headers = {
        # 双头都带：anthropic 形状（x-api-key）+ openai 形状（Bearer），探活无害
        "x-api-key": combined_key,
        "Authorization": f"Bearer {combined_key}",
        "Content-Type": "application/json",
    }
    data = http.get_json(ZAI_QUOTA_LIMIT_URL, headers)
    if not isinstance(data, dict):
        raise GlmMintError("check_quota", f"quota 响应非对象: {str(data)[:200]}")
    return data


def quick_validate(combined_key: str, *, http: Optional[RequestsHttp] = None) -> bool:
    """9Router 同款验证：POST anthropic messages，max_tokens=1，非 401/403 即有效。"""
    http = http or RequestsHttp()
    resp = requests.post(
        NINE_ROUTER_VALIDATE_URL + "?beta=true",
        headers={
            "x-api-key": combined_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={"model": NINE_ROUTER_TEST_MODEL, "max_tokens": 1, "messages": [{"role": "user", "content": "test"}]},
        proxies=http.proxies,
        timeout=http.timeout,
    )
    return resp.status_code not in (401, 403)
