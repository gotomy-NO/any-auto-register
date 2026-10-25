"""trae 模型网关（B 通道现行协议）Python 移植 —— register-8000/platforms/trae。

蓝本（全网唯一现行实现，2026-10-05 抓取）：
  - ProjectEio/trae2api  crypto.go / config.go / trae.go / main.go / convert.go
  - 逆向自 Trae 的 ai_agent.dll（AES-256-GCM 体加密 + pin/ts 签名头）

协议要点（逐条对齐 crypto.go，勿改语义）：
  body = base64_std( nonce[12] || AES-256-GCM(key, nonce, plaintext, aad=AAD) )    # ct||tag 追加在 nonce 后
  key  = BASE_KEY（32B 定值），key[0:8] ^= salt8
  salt8 → 头 `x-request-pin`（hex）；aad = ascii(unix_seconds) → 头 `x-requested-at`
  端点 = POST {base_url}/api/agent/v3/create_agent_task
  鉴权 = 头 `x-ide-token: <ide_token>`（+ 全量 config.go 头集）；**pin/ts 头必须在**，
         否则网关直接回 401 code=1001。下游是**明文 SSE**（text/event-stream），无需解密响应；
         decrypt_body 仅供抓包调试。

【本轮实测结论 —— 见 docs/trae-模型网关打通-20261005.md】
用 acct249 的 ide_token 实打 coreva-normal.trae.ai：
  · 带 pin/ts 头 + 明文体      → 400 {"error":"base64 decode failed"}   ← 鉴权已过！
  · 带 pin/ts 头 + 错钥 AES-GCM → 400 {"error":"crypto decrypt failed"}  ← 结构与密钥派生已对，只差 BASE_KEY
  · 不带 pin/ts 头             → 401 {"code":1001}（认证失败）
即：**唯一缺口 = 32 字节 BASE_KEY 定值**（未公开；须用 tools/frida/hook_trae_crypto.js 从
Trae 客户端现场抓取，或由已抓取的 config.json 提供）。本模块把所有其它环节全部打通，
BASE_KEY 从 env `TRAE_BASE_KEY`(hex) / `trae_agent_config.json`(base_key_hex) 读取。

用法：
    from platforms.trae.agent_gateway import create_agent_task, TraeGatewayIdentity
    env = create_agent_task(ide_token="eyJ...", user_id="769...", prompt="Reply exactly: OK")
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Callable, Iterator, Optional

try:  # 优先标准库之外无依赖：cryptography 用于 AES-GCM
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception as _exc:  # pragma: no cover
    AESGCM = None  # type: ignore

__all__ = [
    "AGENT_PATH",
    "CUE_AGENT_PATH",
    "CUE_COMMIT_PATH",
    "DEFAULT_BASE_URL",
    "GATEWAY_APP_ID",
    "IDE_VERSION",
    "MODEL_CATALOG",
    "TraeGatewayError",
    "TraeGatewayIdentity",
    "build_headers",
    "build_create_body",
    "build_cue_body",
    "create_agent_task",
    "create_cue_task",
    "decrypt_body",
    "encrypt_body",
    "gen_object_id",
    "gen_uuid",
    "load_base_key",
    "parse_sse",
    "iter_agent_reply",
]

AGENT_PATH = "/api/agent/v3/create_agent_task"
COMMIT_PATH = "/api/agent/v3/commit_toolcall_result"
#: 客户端（TraeCode server.js）实际使用的路径 —— cue_agent（2026-10-05 逆向确认）。
#: /api/cue_agent/v3 才是客户端现行 agent 面；/api/agent/v3 是旧 trae2api 路径。
CUE_AGENT_PATH = "/api/cue_agent/v3/create_agent_task"
CUE_COMMIT_PATH = "/api/cue_agent/v3/commit_toolcall_result"
#: 客户端 nQr() 构建 cue 体的默认值（从 server.js 反解，见 docs/trae-模型网关打通-20261005.md §十二）。
CUE_AGENT_TYPE_DEFAULT = "cue_builder"
CUE_MODEL_DEFAULT = "deepsearch"
CUE_AVAILABLE_TOOLS_DEFAULT = [
    "get_code_of_class", "get_code_of_class_function", "get_code_of_file_function",
    "get_code_of_file", "grep", "get_symbol_reference", "get_similar_code", "exit",
]
#: 全局（trae.ai）agent 网关宿主。region=us→coreva；sg→coresg。
DEFAULT_BASE_URL = "https://coreva-normal.trae.ai"
SG_BASE_URL = "https://coresg-normal.trae.ai"

#: config.go 头集里的定值（ProjectEio config.json 全为 fixed）。
GATEWAY_APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
APP_VERSION = "3.5.54"
IDE_VERSION = "3.5.54"
APP_VERSION_CODE = "20260101"
STORE_COUNTRY = "us"

#: models.json（ProjectEio 实录）：id → Trae 真实 config_name。
MODEL_CATALOG: dict[str, str] = {
    "gemini-2.5-flash": "gemini_2.5_flash_premium",
    "gemini-3.1-pro": "gemini-3.1-pro",
    "gemini-3-flash": "gemini-3-flash-premium",
    "gpt-5.4": "gpt-5.4",
    "gpt-5.2": "gpt-5.2",
    "gpt-5.2-codex": "gpt-5.2-codex",
    "gpt-5.1": "gpt-5.1-paygo",
    "gpt-5": "gpt-5-paygo",
    "minimax-m3": "minimax-m3",
    "minimax-m2.7": "minimax-m2.7",
    "kimi-k2.5": "kimi-k2.5",
    "deepseek-v3.2": "deepseek-v3.2",
    "dola-seed-2.0-code": "Dola-Seed-2.0-Code",
    "doubao-seed-2.0-code": "doubao-for-auto",
}
DEFAULT_MODEL = "minimax-m3"

#: BASE_KEY 读取来源（按序）：env → 本地 JSON 配置。
_BASE_KEY_ENV = "TRAE_BASE_KEY"
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "trae_agent_config.json")


class TraeGatewayError(RuntimeError):
    """网关调用无法进行（缺 BASE_KEY / 加密库缺失 / 传输失败）。"""


# --------------------------------------------------------------------------- #
# id / 基础
# --------------------------------------------------------------------------- #

def gen_uuid() -> str:
    """RFC-4122 v4 UUID 字符串（对齐 config.go genUUID）。"""
    return str(uuid.uuid4())


def gen_object_id() -> str:
    """24-hex 对象 id（对齐 config.go genObjectID；Trae conversation/session id 形状）。"""
    return secrets.token_hex(12)


def _require_aesgcm() -> "AESGCM":
    if AESGCM is None:  # pragma: no cover
        raise TraeGatewayError("缺少 cryptography（pip install cryptography）")
    return AESGCM


# --------------------------------------------------------------------------- #
# BASE_KEY / 密钥派生 / 体加解密（crypto.go 逐条对齐）
# --------------------------------------------------------------------------- #

def load_base_key(*, config_path: str | None = None) -> Optional[bytes]:
    """读 32 字节 BASE_KEY：env `TRAE_BASE_KEY`(hex) → trae_agent_config.json base_key_hex。

    未配置返回 None（调用方据此给出“需现场抓取密钥”的明确错误，绝不报假成功）。
    """
    raw = os.environ.get(_BASE_KEY_ENV, "").strip()
    if not raw:
        path = config_path or _CONFIG_PATH
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            raw = str(cfg.get("base_key_hex") or "").strip()
        except Exception:
            raw = ""
    if not raw:
        return None
    try:
        key = bytes.fromhex(raw)
    except ValueError as exc:
        raise TraeGatewayError(f"BASE_KEY 非合法 hex: {exc}") from exc
    if len(key) != 32:
        raise TraeGatewayError(f"BASE_KEY 必须 32 字节（64 hex），实得 {len(key)}")
    return key


def derive_key(base_key: bytes, salt: bytes) -> bytes:
    """key = BASE_KEY；key[0:8] ^= salt[0:8]（crypto.go deriveKey）。"""
    k = bytearray(base_key)
    for i in range(min(8, len(salt))):
        k[i] ^= salt[i]
    return bytes(k)


def encrypt_body(plaintext: bytes, *, base_key: bytes | None = None,
                 salt: bytes | None = None, nonce: bytes | None = None,
                 ts: str | None = None) -> tuple[str, str, str]:
    """→ (body_b64, x_request_pin_hex, x_requested_at)。

    body = base64_std( nonce[12] || AESGCM(derive_key(salt), nonce, plaintext, aad=ts) )
    """
    key = base_key or load_base_key()
    if key is None:
        raise TraeGatewayError(
            "缺少 BASE_KEY（32B 定值）。设 env TRAE_BASE_KEY=<64hex> 或写 "
            "platforms/trae/trae_agent_config.json:{\"base_key_hex\":\"...\"}；"
            "抓取方法见 docs/trae-模型网关打通-20261005.md §五")
    salt = salt if salt is not None else secrets.token_bytes(8)
    nonce = nonce if nonce is not None else secrets.token_bytes(12)
    ts = ts if ts is not None else str(int(time.time()))
    ae = _require_aesgcm()(derive_key(key, salt))
    ct = ae.encrypt(nonce, plaintext, ts.encode("ascii"))
    blob = base64.b64encode(nonce + ct).decode("ascii")
    return blob, salt.hex(), ts


def decrypt_body(body_b64: str, pin_hex: str, requested_at: str, *,
                 base_key: bytes | None = None) -> bytes:
    """crypto.go decryptBody 逆运算（仅调试抓包用；响应本身是明文 SSE）。"""
    key = base_key or load_base_key()
    if key is None:
        raise TraeGatewayError("缺少 BASE_KEY，无法解密")
    blob = base64.b64decode(body_b64)
    if len(blob) < 12 + 16:
        raise TraeGatewayError("body too short")
    salt = bytes.fromhex(pin_hex)
    nonce, ct = blob[:12], blob[12:]
    return _require_aesgcm()(derive_key(key, salt)).decrypt(
        nonce, ct, requested_at.encode("ascii"))


# --------------------------------------------------------------------------- #
# 请求头（config.go buildHeaders 全量；pin/ts 为必选）
# --------------------------------------------------------------------------- #

@dataclass
class TraeGatewayIdentity:
    """一个 trae 账号在网关侧的身份（B 通道）。"""
    ide_token: str                      # ExchangeToken 得到的 JWT（data.source=refresh_token）
    user_id: str = ""                   # JWT.data.id；缺省从 token 解
    device_id: str = ""                 # 16 位数字；缺省随机并应持久化
    machine_id: str = ""                # 64 hex；缺省随机并应持久化

    def fill_defaults(self) -> "TraeGatewayIdentity":
        if not self.user_id:
            self.user_id = jwt_user_id(self.ide_token)
        if not self.device_id:
            self.device_id = str(int.from_bytes(secrets.token_bytes(8), "big"))[:16]
        if not self.machine_id:
            self.machine_id = secrets.token_hex(32)
        return self


def jwt_user_id(token: str) -> str:
    """从 JWT payload 取 data.id（失败返回 ''）。"""
    try:
        pay = token.split(".")[1]
        pay += "=" * (-len(pay) % 4)
        data = json.loads(base64.urlsafe_b64decode(pay))
        d = data.get("data") or {}
        return str(d.get("id") or data.get("user_id") or "")
    except Exception:
        return ""


def _strip_dashes(s: str) -> str:
    return s.replace("-", "")


def build_headers(identity: TraeGatewayIdentity, pin: str, ts: str, *,
                  base_url: str = DEFAULT_BASE_URL, country: str = STORE_COUNTRY,
                  ide_version: str = IDE_VERSION, plugin_channel: str = "stable") -> dict:
    """config.go buildHeaders 的完整头集（pin/ts 为本次加密的 x-request-pin/x-requested-at）。

    另加客户端 cue 请求实际携带的 `x-plugin-channel`（server.js::oQr）。
    """
    identity.fill_defaults()
    t = gen_uuid()
    return {
        "content-type": "application/json",
        "user-agent": "TraeClient/TTNet",
        "request-traffic-type": "prod",
        "store-country-code": country,
        "store-country-code-src": "uid",
        "x-app-id": GATEWAY_APP_ID,
        "x-app-version": "default",
        "x-app-version-code": APP_VERSION_CODE,
        "x-bridge-transport": "aha",
        "x-custom-trace-id": _strip_dashes(t),
        "x-device-id": identity.device_id,
        "x-device-type": "windows",
        "x-flow-traceparent": f"04-{_strip_dashes(t)}-{_strip_dashes(gen_uuid())[:16]}-01",
        "x-ide-token": identity.ide_token,
        "x-ide-version": ide_version,
        "x-ide-version-code": APP_VERSION_CODE,
        "x-ide-version-type": "stable",
        "x-lgw-req-sdk-type": "3",
        "x-machine-id": identity.machine_id,
        "x-net-sdk-domain-dispatch": "1",
        "x-os-version": "Windows 11 Pro",
        "x-plugin-channel": plugin_channel,
        "x-request-id": "req_" + gen_uuid(),
        "x-trae-request-id": gen_uuid(),
        "x-request-pin": pin,
        "x-requested-at": ts,
        "x-ttnet-bypass-decompression": "1",
        "app-version": APP_VERSION,
        "package-type": "stable_i18n",
        "accept": "*/*",
        "accept-encoding": "gzip, deflate, br, zstd",
    }


# --------------------------------------------------------------------------- #
# create_agent_task 请求体（trae.go buildCreateBody 的结构 + template.json 顶层字段）
# --------------------------------------------------------------------------- #

def build_create_body(*, model: str, prompt: str, identity: TraeGatewayIdentity,
                      system: str = "", conversation_id: str = "",
                      session_id: str = "", history_id_list: Optional[list] = None,
                      request_seq: int = 1) -> bytes:
    """构造 create_agent_task 明文 JSON 体（随后 encrypt_body 加密）。

    model 既是 model_name 也是 config_name（对齐 trae.go setStr(model_name/config_name)）。
    prompt 放在 user_input.messages[].text_content（全局模板形状；CN/Work 版另有
    user_input.query 文本切片，见 docs）。
    """
    identity.fill_defaults()
    conv = conversation_id or gen_uuid()
    sess = session_id or gen_uuid()
    real_model = MODEL_CATALOG.get(model, model)
    body = {
        "agent_id": None,
        "tunnel_id": None,
        "is_custom_model": False,
        "provider": "",
        "conversation_id": conv,
        "session_id": sess,
        "plugin_channel": None,
        "user_id": identity.user_id,
        "device_id": identity.device_id,
        "agent_type": "dev_agent",
        "config_name": real_model,
        "model_name": real_model,
        "ide_version": IDE_VERSION,
        "config_source": 1,
        "user_input": {
            "id": gen_object_id(),
            "messages": ([{"type": "text", "text_content": prompt}] if prompt else []),
        },
        "history_id_list": history_id_list or [],
        "missing_history": None,
        "available_tool_list": [],
        "mcp_tool_name": {},
        "mcp_tool_list": [],
        "render_context": {
            "variables": json.dumps({
                "input": prompt, "raw_input": prompt,
                "date": time.strftime("%Y-%m-%d"),
                "current_time": time.strftime("%Y%m%d %H:%M:%S, %A"),
                "unique_user_id": identity.device_id,
                "os_version": "Windows 11 Pro",
                "system_type": "Windows", "locale": "en-us",
                "custom_agent_prompt": system or None,
            }),
        },
        "request_seq": request_seq,
        "custom_agent_list": [],
        "agent_version": "v3",
        "ab_info": None,
        "mode_type": 0,
        "custom_subagent_info": {},
        "extra_config": {
            "disable_parallel_agent": True, "enable_todo_list": True,
            "enable_core_memory": False, "hooks_configured": False,
        },
        "skill_list": [],
        "skill_list_changed": False,
        "access_type": 0,
        "is_remote_req": False,
        "history_message_limit": 600,
        "function": "builder_v3",
        "raw_rules": [],
    }
    return json.dumps(body).encode("utf-8")


# --------------------------------------------------------------------------- #
# cue_agent 请求体（逆向自 TraeCode server.js::nQr —— 客户端真实体格式）
# --------------------------------------------------------------------------- #

def build_cue_body(*, prompt: str, identity: TraeGatewayIdentity,
                   model: str = CUE_MODEL_DEFAULT, config_name: str | None = None,
                   agent_type: str = CUE_AGENT_TYPE_DEFAULT,
                   conversation_id: str = "", user_input_id: str = "",
                   available_tools: Optional[list] = None,
                   render_context: Optional[dict] = None,
                   plugin_channel: str = "stable") -> bytes:
    """构造 cue_agent 明文 JSON 体（随后 encrypt_body 加密）。

    字段集严格对齐客户端 nQr()（server.js 反解）——**不含**任何 /api/agent/v3
    的字段（session_id/history_id_list/mcp_tool_list/request_seq/agent_version/
    mode_type/extra_config/function/access_type… 这些 cue 体里都没有）：
        {conversation_id, user_id, device_id, agent_type, model_name, config_name,
         ide_version, plugin_channel, available_tool_list,
         user_input:{id, messages:[{type:'text', text_content}]},
         render_context:{variables:<JSON string>, references:{}}}
    model_name==config_name（客户端默认 'deepsearch'）。
    """
    identity.fill_defaults()
    return json.dumps({
        "conversation_id": conversation_id or gen_uuid(),
        "user_id": identity.user_id,
        "device_id": identity.device_id,
        "agent_type": agent_type,
        "model_name": model,
        "config_name": config_name or model,
        "ide_version": IDE_VERSION,
        "plugin_channel": plugin_channel,
        "available_tool_list": (CUE_AVAILABLE_TOOLS_DEFAULT if available_tools is None
                                else available_tools),
        "user_input": {
            "id": user_input_id or gen_object_id(),
            "messages": ([{"type": "text", "text_content": prompt}] if prompt else []),
        },
        "render_context": {
            "variables": json.dumps(render_context if render_context is not None else {},
                                    ensure_ascii=False),
            "references": {},
        },
    }, ensure_ascii=False).encode("utf-8")


def create_cue_task(identity: TraeGatewayIdentity, prompt: str, *,
                    model: str = CUE_MODEL_DEFAULT, config_name: str | None = None,
                    agent_type: str = CUE_AGENT_TYPE_DEFAULT,
                    base_url: str = DEFAULT_BASE_URL, proxy: str | None = None,
                    timeout: float = 90.0, encrypt: bool = True,
                    render_context: Optional[dict] = None,
                    available_tools: Optional[list] = None,
                    session=None):
    """POST {base_url}/api/cue_agent/v3/create_agent_task（客户端现行路径）。

    返回 requests.Response（stream=True）；下游是明文 SSE（text/event-stream）。
    """
    import requests

    identity = identity.fill_defaults()
    plain = build_cue_body(prompt=prompt, identity=identity, model=model,
                           config_name=config_name, agent_type=agent_type,
                           render_context=render_context, available_tools=available_tools)
    if encrypt:
        body_b64, pin, ts = encrypt_body(plain)
        data = body_b64
    else:
        pin, ts = secrets.token_hex(8), str(int(time.time()))
        data = plain
    headers = build_headers(identity, pin, ts, base_url=base_url)
    http = session or requests
    kwargs = dict(data=data, headers=headers, timeout=timeout, stream=True)
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    return http.post(base_url.rstrip("/") + CUE_AGENT_PATH, **kwargs)


# --------------------------------------------------------------------------- #
# 传输 + SSE（main.go postTrae / convert.go parseTraeSSE）
# --------------------------------------------------------------------------- #

def create_agent_task(identity: TraeGatewayIdentity, prompt: str, *,
                      model: str = DEFAULT_MODEL, system: str = "",
                      base_url: str = DEFAULT_BASE_URL, proxy: str | None = None,
                      timeout: float = 60.0, encrypt: bool = True,
                      session=None):
    """POST create_agent_task。返回 requests.Response（stream=True）。

    encrypt=True（默认）：AES-GCM 体 + pin/ts 头（需 BASE_KEY）。
    encrypt=False：明文体（诊断用；网关会回 400 base64 decode failed）。
    下游是明文 SSE（text/event-stream），response.iter_lines() 逐行解析即可。
    """
    import requests

    identity = identity.fill_defaults()
    plain = build_create_body(model=model, prompt=prompt, identity=identity, system=system)
    if encrypt:
        body_b64, pin, ts = encrypt_body(plain)
        data = body_b64
    else:
        pin, ts = secrets.token_hex(8), str(int(time.time()))
        data = plain
    headers = build_headers(identity, pin, ts, base_url=base_url)
    http = session or requests
    kwargs = dict(data=data, headers=headers, timeout=timeout, stream=True)
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    return http.post(base_url.rstrip("/") + AGENT_PATH, **kwargs)


def parse_sse(lines: Iterator) -> Iterator[tuple]:
    """convert.go parseTraeSSE：yield (event, data_str)。空行 flush。"""
    ev, buf = "", []
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
        if line == "":
            if ev or buf:
                yield ev, "".join(buf)
            ev, buf = "", []
        elif line.startswith("event:"):
            ev = line[6:].strip()
        elif line.startswith("data:"):
            buf.append(line[5:].lstrip(" ").strip("\r"))
    if ev or buf:
        yield ev, "".join(buf)


def iter_agent_reply(resp, *, log: Callable[[str], None] | None = None) -> Iterator[tuple]:
    """把 create_agent_task 的流式响应解析成 (event, payload_dict) 迭代。"""
    log = log or (lambda _m: None)
    for ev, data in parse_sse(resp.iter_lines(decode_unicode=False)):
        if not data:
            continue
        try:
            payload = json.loads(data)
        except Exception:
            payload = {"_raw": data}
        log(f"trae-sse {ev}: {str(payload)[:160]}")
        yield ev, payload


# --------------------------------------------------------------------------- #
# llm_utils_chat —— 通用 chat 端点（2026-10-06 打通；非代码检索 agent）
# --------------------------------------------------------------------------- #
#: 通用对话端点（客户端 client 的「简单对话」面；与 cue_agent 不同，它走**明文
#: /api/agent/v3 路径但体仍需 AES-GCM 加密 + pin/ts 头**）。实测：
#:   POST {coreva-normal.trae.ai}/api/agent/v3/llm_utils_chat
#:     body(加密前) = {"messages":[{"role":"user","content":[{"type":"text","text":"..."}]}],
#:                     "function":"inline_chat","stream":true}
#:     → 200 text/event-stream: metadata / timing_cost / output{response} / token_usage / done
#: 要点：messages[].content 必须是 **数组**（LLM RawMessageContent），传字符串 → 400。
#: function=inline_chat 用服务端默认模型，返回真实自由文本（问「1+1」回「2」）。
#: function=chat_v3/solo_coder + 指定 model 在本(免费)账号恒 `error code 4120`（模型选择
#: 不可用，需订阅/未注册 config）；config_name 在 llm_utils_chat 端点不受支持。
LLM_UTILS_CHAT_PATH = "/api/agent/v3/llm_utils_chat"
LLM_CHAT_FUNCTION_DEFAULT = "inline_chat"


def build_llm_utils_body(*, messages: list, function: str = LLM_CHAT_FUNCTION_DEFAULT,
                         model: str | None = None, stream: bool = True) -> bytes:
    """构造 llm_utils_chat 明文 JSON 体（随后 encrypt_body）。

    messages 里每条 content 必须是 [{"type":"text","text":...}] 数组形状
    （调用方负责；本函数直接透传 messages）。
    """
    body: dict = {"messages": messages, "function": function, "stream": bool(stream)}
    if model:
        body["model"] = model
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def llm_utils_chat(identity: TraeGatewayIdentity, messages: list, *,
                   function: str = LLM_CHAT_FUNCTION_DEFAULT, model: str | None = None,
                   base_url: str = DEFAULT_BASE_URL, proxy: str | None = None,
                   timeout: float = 120.0, session=None):
    """POST {base_url}/api/agent/v3/llm_utils_chat（明文路径 + 加密体 + pin/ts 头）。

    返回 requests.Response（stream=True）；下游是明文 SSE（text/event-stream）。
    """
    import requests

    identity = identity.fill_defaults()
    plain = build_llm_utils_body(messages=messages, function=function, model=model)
    body_b64, pin, ts = encrypt_body(plain)
    headers = build_headers(identity, pin, ts, base_url=base_url)
    headers["accept"] = "text/event-stream"
    headers["authorization"] = "Cloud-IDE-JWT " + identity.ide_token
    headers["x-cloudide-token"] = identity.ide_token
    headers["x-uid"] = identity.user_id
    http = session or requests
    kwargs = dict(data=body_b64, headers=headers, timeout=timeout, stream=True)
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    return http.post(base_url.rstrip("/") + LLM_UTILS_CHAT_PATH, **kwargs)


def iter_llm_utils_reply(resp, *, log: Callable[[str], None] | None = None) -> Iterator[tuple]:
    """把 llm_utils_chat 的流式响应解析成 (event, payload_dict) 迭代。

    复用 parse_sse（自动忽略 `id:N` 行）。典型序列：
        metadata → timing_cost → output{response} → token_usage → done（或 error）。
    """
    log = log or (lambda _m: None)
    for ev, data in parse_sse(resp.iter_lines(decode_unicode=False)):
        if not data:
            continue
        try:
            payload = json.loads(data)
        except Exception:
            payload = {"_raw": data}
        log(f"trae-llm-sse {ev}: {str(payload)[:160]}")
        yield ev, payload
