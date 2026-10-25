r"""ZCode OAuth CLI device-flow 客户端（zcode → GLM 铸钥线·协作轮）

对齐 9Router ``src/lib/oauth/providers/glm.js`` 的 ZCode CLI polling 流程
（镜像官方 ZCode CLI apps/zcode-cli cli-oauth.ts），无 PKCE、无本地回调：

  1) ``POST {CLI_INIT_URL}``  ``Authorization: Bearer <poll_token>``  ``{"provider":"zai"}``
       → ``{code:0, data:{authorize_url, flow_id, poll_interval_sec, expires_at}}``
  2) 浏览器/人工打开 authorize_url 完成 z.ai 登录授权
     （协作轮：zcode_slider_manual_fallback=true，滑块由人工在弹窗完成）
  3) ``GET {CLI_POLL_URL}/{flow_id}``  ``Authorization: Bearer <poll_token>``
       → ``{code:0, data:{status:"pending"|"ready"|"failed", token, user,
                          zai:{access_token, refresh_token?}}}``
  4) ``ready.zai.access_token`` → :func:`glm_mint.exchange_business_token`
     → :func:`glm_mint.mint_coding_plan_key` → coding-plan key
     （形如 ``{apiKey}.{secretKey}``，9Router glm apikey 模式直配）

纪律：
- token / poll_token 全程打码（repr/日志只见 prefix8+len，复用 :func:`glm_mint.mask_key`）；
- 传输层可注入（与 :class:`glm_mint.RequestsHttp` 同 get_json/post_json 接口），
  实网走 curl_cffi chrome 指纹适配器，离线桩测注入假传输不触网；
- 每一步失败都抛 :class:`ZcodeOAuthError`（带 step/status/code），不静默降级。
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import quote

from platforms.zcode.glm_mint import (
    GlmMintError,
    MintResult,
    RequestsHttp,
    build_provider_payload as _build_provider_payload,
    exchange_business_token,
    mask_key,
    mint_coding_plan_key,
)

# ---------------------------------------------------------------- 常量（与 9R open-sse/providers/registry/glm.js 对齐）

ZCODE_BASE = "https://zcode.z.ai"
CLI_INIT_URL = f"{ZCODE_BASE}/api/v1/oauth/cli/init"
CLI_POLL_URL = f"{ZCODE_BASE}/api/v1/oauth/cli/poll"
PROVIDER_ID = "zai"

DEFAULT_POLL_INTERVAL = 3.0
DEFAULT_HANDSHAKE_TIMEOUT = 300.0  # 9R OAUTH_TIMEOUT 同款 5 分钟

__all__ = [
    "ZCODE_BASE",
    "CLI_INIT_URL",
    "CLI_POLL_URL",
    "PROVIDER_ID",
    "DeviceCode",
    "PollResult",
    "HandshakeResult",
    "ZcodeOAuthError",
    "cli_init",
    "cli_poll",
    "poll_until_ready",
    "mint_from_ready",
    "full_handshake",
    "build_provider_payload",
    "mask_key",
]


class ZcodeOAuthError(RuntimeError):
    """OAuth 流程某一步失败。step 标明断点；status=HTTP 码；code=业务码。"""

    def __init__(self, step: str, message: str, status: Optional[int] = None, code: Any = None):
        super().__init__(f"[{step}] {message}")
        self.step = step
        self.status = status
        self.code = code


def _is_success(code: Any) -> bool:
    """zcode 业务码：0/200（含字符串）/缺省 = 成功（对齐 9R isSuccessCode）。"""
    return code is None or code in (0, 200, "0", "200")


def _wrap_transport(step: str, err: Exception) -> ZcodeOAuthError:
    """把传输层（glm_mint）异常包成 ZcodeOAuthError，保留 HTTP status。"""
    status = getattr(err, "status", None)
    return ZcodeOAuthError(step, str(err), status=status)


def _remaining_seconds(expires_at: Any, now: float) -> Optional[float]:
    """上游绝对 epoch（秒或毫秒）→ 距今剩余秒；无效返回 None（对齐 9R relativeSeconds）。"""
    try:
        raw = float(expires_at)
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None
    # 毫秒时间戳（≥1e10 量级，如 4102444800000）走毫秒；秒时间戳 < 1e11
    ms = raw if raw > 1e11 else raw * 1000.0
    seconds = ms / 1000.0 - now
    return seconds if seconds > 0 else None


# ---------------------------------------------------------------- 数据结构（repr 全打码）

@dataclass
class DeviceCode:
    """步骤1 产物。poll_token 只在内存字段里，repr 永不出现。"""

    flow_id: str
    authorize_url: str
    poll_token: str
    interval: float = DEFAULT_POLL_INTERVAL
    expires_at: Any = None  # 上游绝对 epoch（秒/毫秒都可能）
    poll_url: str = CLI_POLL_URL

    def remaining(self, now: Optional[float] = None) -> Optional[float]:
        return _remaining_seconds(self.expires_at, time.time() if now is None else now)

    def __repr__(self) -> str:  # 纪律：poll_token 不进日志
        rem = self.remaining()
        return (
            f"DeviceCode(flow_id={mask_key(self.flow_id)}, authorize_url={self.authorize_url[:48]}..., "
            f"interval={self.interval}, expires_in={int(rem) if rem else 'n/a'})"
        )


@dataclass
class PollResult:
    """步骤3 产物。ready 时携带 chat JWT / user / zai OAuth token（全部打码）。"""

    status: str  # "pending" / "ready" / "failed" / 未知原样
    chat_token: str = ""
    user: dict = field(default_factory=dict)
    zai_access_token: str = ""
    zai_refresh_token: str = ""
    raw_status: str = ""

    @property
    def is_pending(self) -> bool:
        return self.status == "pending"

    @property
    def is_ready(self) -> bool:
        return self.status == "ready"

    @property
    def is_failed(self) -> bool:
        return self.status == "failed"

    def __repr__(self) -> str:  # 纪律：任何 token 不进日志
        user_id = str(self.user.get("user_id", "")) if isinstance(self.user, dict) else ""
        return (
            f"PollResult(status={self.status!r}, chat_token={mask_key(self.chat_token)}, "
            f"zai_access_token={mask_key(self.zai_access_token)}, user_id={user_id[:8]})"
        )


@dataclass
class HandshakeResult:
    """完整握手产物：init → poll(ready) → mint。repr 链式打码。"""

    device: DeviceCode
    poll: PollResult
    mint: MintResult
    steps: list = field(default_factory=list)

    def __repr__(self) -> str:
        return f"HandshakeResult(steps={self.steps}, mint={self.mint!r})"


# ---------------------------------------------------------------- 步骤1：init

def new_poll_token() -> str:
    """一次性 poll token：64 hex（对齐 9R crypto.randomBytes(32).toString("hex")）。"""
    return secrets.token_hex(32)


def cli_init(http: Any = None, *, poll_token: Optional[str] = None) -> DeviceCode:
    """POST cli/init 铸一次性 flow。返回 :class:`DeviceCode`（authorize_url 待人工授权）。"""
    http = http or RequestsHttp()
    poll_token = poll_token or new_poll_token()
    try:
        resp = http.post_json(
            CLI_INIT_URL,
            {"provider": PROVIDER_ID},
            {"Content-Type": "application/json", "Authorization": f"Bearer {poll_token}"},
        )
    except GlmMintError as err:
        raise _wrap_transport("init", err) from err
    except Exception as err:  # requests 连接层异常等
        raise ZcodeOAuthError("init", f"cli/init 请求失败: {err}") from err
    if not isinstance(resp, dict):
        raise ZcodeOAuthError("init", f"cli/init 响应非对象: {str(resp)[:200]}")
    if not _is_success(resp.get("code")):
        raise ZcodeOAuthError("init", f"cli/init 业务失败: {str(resp.get('msg', resp))[:200]}", code=resp.get("code"))
    data = resp.get("data")
    if not isinstance(data, dict):
        raise ZcodeOAuthError("init", f"cli/init 响应无 data: {str(resp)[:200]}")
    flow_id = str(data.get("flow_id", "") or "").strip()
    authorize_url = str(data.get("authorize_url", "") or "").strip()
    if not flow_id or not authorize_url:
        raise ZcodeOAuthError("init", f"cli/init 缺 flow_id/authorize_url: {str(data)[:200]}")
    try:
        interval = float(data.get("poll_interval_sec") or DEFAULT_POLL_INTERVAL)
    except (TypeError, ValueError):
        interval = DEFAULT_POLL_INTERVAL
    return DeviceCode(
        flow_id=flow_id,
        authorize_url=authorize_url,
        poll_token=poll_token,
        interval=max(interval, 0.5),
        expires_at=data.get("expires_at"),
    )


# ---------------------------------------------------------------- 步骤3：poll

def cli_poll(device: DeviceCode, http: Any = None) -> PollResult:
    """GET cli/poll/{flow_id} 查一次授权状态。pending/failed 原样返回，ready 解析 token。"""
    http = http or RequestsHttp()
    url = f"{device.poll_url}/{quote(device.flow_id, safe='')}"
    try:
        resp = http.get_json(url, {"Authorization": f"Bearer {device.poll_token}"})
    except GlmMintError as err:
        raise _wrap_transport("poll", err) from err
    except Exception as err:
        raise ZcodeOAuthError("poll", f"cli/poll 请求失败: {err}") from err
    if not isinstance(resp, dict):
        raise ZcodeOAuthError("poll", f"cli/poll 响应非对象: {str(resp)[:200]}")
    if not _is_success(resp.get("code")):
        raise ZcodeOAuthError("poll", f"cli/poll 业务失败: {str(resp.get('msg', resp))[:200]}", code=resp.get("code"))
    data = resp.get("data") or {}
    if not isinstance(data, dict):
        raise ZcodeOAuthError("poll", f"cli/poll 响应无 data: {str(resp)[:200]}")

    status = str(data.get("status", "") or "").strip()
    if status != "ready":
        # pending / failed / 未知状态都原样上交，由 poll_until_ready 决定去留（对齐 9R 语义）
        return PollResult(status=status or "pending", raw_status=status)

    # ready 载荷：provider 数据嵌在 data["zai"]（data[providerId]），顶层字段兜底
    provider_data = data.get(PROVIDER_ID) or data.get(data.get("providerId")) or {}
    if not isinstance(provider_data, dict):
        provider_data = {}
    zai_access = str(
        provider_data.get("access_token")
        or provider_data.get("accessToken")
        or data.get("accessToken")
        or data.get("access_token")
        or ""
    ).strip()
    if not zai_access:
        raise ZcodeOAuthError("poll_ready", f"ready 响应缺 access_token: {str(data)[:200]}")
    refresh = str(
        provider_data.get("refresh_token")
        or provider_data.get("refreshToken")
        or data.get("refresh_token")
        or data.get("refreshToken")
        or ""
    ).strip()
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    return PollResult(
        status="ready",
        chat_token=str(data.get("token", "") or ""),
        user=user,
        zai_access_token=zai_access,
        zai_refresh_token=refresh,
        raw_status="ready",
    )


def poll_until_ready(
    device: DeviceCode,
    *,
    http: Any = None,
    deadline_s: float = DEFAULT_HANDSHAKE_TIMEOUT,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    wall: Callable[[], float] = time.time,
    on_pending: Optional[Callable[[PollResult], None]] = None,
) -> PollResult:
    """按 device.interval 轮询直到 ready/failed 或超时。

    - pending：调用 on_pending 钩子（协作轮里用它刷「等待人工授权」日志）后继续；
    - failed：立即返回（调用方决定报错方式）；
    - 上游 expires_at 已过（带 expires_at 且 remaining 为 None）：立刻超时；
    - 本地 deadline_s 耗尽：抛 ZcodeOAuthError("poll_timeout")。
    """
    http = http or RequestsHttp()
    start = now()
    while True:
        result = cli_poll(device, http=http)
        if result.is_ready or result.is_failed:
            return result
        if on_pending is not None:
            on_pending(result)
        elapsed = now() - start
        upstream = device.remaining(wall())
        if device.expires_at is not None and upstream is None:
            raise ZcodeOAuthError(
                "poll_timeout", "上游 flow 已过期（expires_at 已过，授权未完成）"
            )
        budget = min(deadline_s - elapsed, upstream) if upstream else deadline_s - elapsed
        if budget <= 0:
            raise ZcodeOAuthError(
                "poll_timeout",
                f"等待授权超时（{deadline_s:.0f}s，上游 expires_in={int(upstream) if upstream else 'n/a'}）",
            )
        sleep(min(device.interval, max(budget, 0.0)))


# ---------------------------------------------------------------- 步骤4：铸 key

def mint_from_ready(
    poll_result: PollResult, *, http: Any = None, require_secret: bool = True
) -> MintResult:
    """ready 的 zai OAuth token → 业务 token → coding-plan key（glm_mint 三步）。"""
    if not poll_result.is_ready or not poll_result.zai_access_token:
        raise ZcodeOAuthError("mint", "poll 未 ready 或缺 zai access_token，无法铸 key")
    try:
        business_token = exchange_business_token(poll_result.zai_access_token, http=http)
        return mint_coding_plan_key(business_token, http=http, require_secret=require_secret)
    except GlmMintError as err:
        raise _wrap_transport("mint", err) from err


def build_provider_payload(combined_key: str, name: str = "glm-zcode") -> dict:
    """9Router ``POST /api/providers`` body（glm apikey 直配）。命名建议 ``zcode-<email>``。"""
    return _build_provider_payload(combined_key, name)


# ---------------------------------------------------------------- 编排：完整握手

def full_handshake(
    *,
    http: Any = None,
    deadline_s: float = DEFAULT_HANDSHAKE_TIMEOUT,
    poll_token: Optional[str] = None,
    on_authorize: Optional[Callable[[str], None]] = None,
    on_pending: Optional[Callable[[PollResult], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    require_secret: bool = True,
) -> HandshakeResult:
    """init → 人工授权（on_authorize 交出 authorize_url）→ poll → mint。

    协作轮用法::

        def 协作(authorize_url):
            print(f"请在浏览器打开并登录+过滑块: {authorize_url}")

        result = full_handshake(http=curl_cffi适配器, on_authorize=协作)
        key = result.mint.combined_key  # 仅内存，不落盘不打日志
    """
    http = http or RequestsHttp()
    steps: list = []
    device = cli_init(http, poll_token=poll_token)
    steps.append(f"init:{device.flow_id[:8]}")

    if on_authorize is not None:
        on_authorize(device.authorize_url)

    poll = poll_until_ready(
        device, http=http, deadline_s=deadline_s, sleep=sleep, now=now, on_pending=on_pending
    )
    if poll.is_failed:
        raise ZcodeOAuthError("poll_failed", "ZCode 授权失败或被取消")
    steps.append("poll:ready")

    mint = mint_from_ready(poll, http=http, require_secret=require_secret)
    steps.append(f"mint:{'+'.join(mint.steps)}")
    return HandshakeResult(device=device, poll=poll, mint=mint, steps=steps)
