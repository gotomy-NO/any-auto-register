"""ZCode 全链一键流水线（协作轮 → 铸 key → 推 9Router → 验活 → 台账回写）

把 2026-10-04 人工串通的 zcode 全链固化成可一键重放的编排器。
离线桩测：tests/test_zcode_pipeline.py（假 http + 假 NRClient + 临时 sqlite，不触网不碰产线）。

明早协作轮 SOP（人话版全文见 docs/zcode流水线SOP-20261004.md）：
    命令（register-8000 根目录）：
        .venv/Scripts/python.exe platforms/zcode/zcode_pipeline.py            # 干跑：授权+铸key，不入库
        .venv/Scripts/python.exe platforms/zcode/zcode_pipeline.py --apply    # 全链：+导入+验活+台账回写
    用户在哪一步滑什么：
        脚本铸 flow 后自动打开浏览器 authorize_url（chat.z.ai/api/oauth/authorize…）——
        在该页登录目标 z.ai 号 → 过滑块 → 点「授权/Authorize」。窗口 5 分钟（上游 flow 寿命 300s，
        脚本默认等 600s），超时直接重跑命令即可——重新铸新 flow，无副作用、不烧任何资产。
    连上后怎么验：
        --apply 时脚本自动：POST /api/providers 建 glm apikey 连接（名 zcode-<邮箱前缀>，产线流
        非 own:）→ GET /api/providers 回读 conn_id+isActive → 网关实调一发
        POST /v1/chat/completions {model:"glm/glm-4.7", max_tokens:8, "hi"} → 台账 accounts
        platform=zcode 行 extra_json.zcode_pipeline 回写（幂等，密码列不动）。
        判读：200=全绿；429/1113=key 活无套餐→pending_quota（属预期，活动/连接动作后到账，
        不算失败）；401/403=链路断→broken（查铸造链/重授权）。人工面板 smoke 模型名必须带
        glm/ 前缀（glm/glm-4.7），裸名会被 qd combo 劫持 404。
    红线：key/token 全程打码（prefix8+len）；不重启 :8000/:20128；不发注册任务不烧邮箱；
    台账密码列永不动；完整 key 不进日志/聊天/台账（台账只存 prefix8）。

编排函数：
    run_collab_round(device_http, log, on_authorize, deadline_s, sleep, now)
        zcode_oauth: cli_init →（人工授权回调留白）→ poll_until_ready → 铸 key。
        铸 key 双路：优先 zai OAuth access_token（mint_from_ready），失败回退 chat JWT 链
        （mint_from_chat_token，两者首步同为 /api/auth/z/login，仅入参 token 不同）。
        返回 {chat_jwt, plan_key, user_id, email, steps, mint_path}（内存真值，repr 打码）。
    import_to_9router(plan_key, email, name=None, client=None, log)
        NRClient 面板会话（loopback 绕代理）POST /api/providers {provider:"glm", apiKey, name}；
        同名 glm 连接已存在则复用不重开；回读 conn_id+isActive，失败精确报。
    verify_connection(conn_id, post=None, apikey=None, ...)
        网关实调一发；429/1113→pending_quota，200→ok，401/403→broken，其余→unknown。
        网关 sk- key 来源：显式参 > config_store nr_api_key > env NR_API_KEY > 网关库 apiKeys 只读。
    ledger_writeback(email, chat_jwt, plan_key_prefix8, conn_id, quota_status, db_path=None)
        直连 sqlite（不经 :8000）：platform=zcode + email 定位行，extra_json.zcode_pipeline
        固定键覆盖（幂等），chat_jwt 非空时刷新 token 列与 extra.token（既有约定），密码列不动。
    full_round(browser_authorize_callable=None, apply=False, ...)
        1→4 串起来。apply=False 干跑：只跑协作轮+铸 key（上游动作），不 import/不实调/不回写，
        返回计划；apply=True 全链。所有返回值里 key 只以 masked 形态出现。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

_ROOT = Path(__file__).resolve().parents[1]  # register-8000 根（脚本直跑/包导入都兜住）
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.nr_client import NRClient  # noqa: E402
from platforms.zcode.glm_mint import (  # noqa: E402
    GlmMintError,
    RequestsHttp,
    build_provider_payload,
    mask_key,
    mint_from_chat_token,
)
from platforms.zcode.zcode_oauth import (  # noqa: E402
    DEFAULT_HANDSHAKE_TIMEOUT,
    ZcodeOAuthError,
    cli_init,
    mint_from_ready,
    poll_until_ready,
)

# ---------------------------------------------------------------- 常量

DEFAULT_DEADLINE = 600.0  # 等人工授权秒数（上游 flow 寿命 300s，超时后重跑铸新 flow 即可）
DEFAULT_LEDGER_DB = _ROOT / "account_manager.db"
DEFAULT_GATEWAY_DB = Path(
    os.environ.get("NR_GATEWAY_DB", "D:/AI/临时/AI算力/9router/data/db/data.sqlite")
)
DEFAULT_GATEWAY_MODEL = "glm/glm-4.7"  # 网关实调模型（必须 glm/ 前缀，裸名被 qd combo 劫持）
DEFAULT_NR_BASE = "http://127.0.0.1:20128"

# 验活判读（同 9Router 审计惯例：非 401/403 即 key 有效；1113=无套餐非故障）
QUOTA_OK = "ok"
QUOTA_PENDING = "pending_quota"   # 429/1113：key 活、无套餐——活动/连接动作后到账，不算失败
QUOTA_BROKEN = "broken"           # 401/403：链路断
QUOTA_UNKNOWN = "unknown"         # 其他（5xx/404 等），看 detail 人工判

__all__ = [
    "ZcodePipelineError",
    "run_collab_round",
    "import_to_9router",
    "verify_connection",
    "ledger_writeback",
    "full_round",
    "default_connection_name",
    "DEFAULT_DEADLINE",
    "DEFAULT_LEDGER_DB",
    "DEFAULT_GATEWAY_DB",
    "DEFAULT_GATEWAY_MODEL",
    "QUOTA_OK",
    "QUOTA_PENDING",
    "QUOTA_BROKEN",
    "QUOTA_UNKNOWN",
]


class ZcodePipelineError(RuntimeError):
    """流水线某一步失败。step 标明断点（collab 已由 ZcodeOAuthError 自带 step）。"""

    def __init__(self, step: str, message: str, status: Optional[int] = None, code: Any = None):
        super().__init__(f"[{step}] {message}")
        self.step = step
        self.status = status
        self.code = code


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _nr_base() -> str:
    """9Router 面板基址：config_store nr_base_url 优先，缺省 loopback。"""
    try:
        from core.config_store import config_store

        return str(config_store.get("nr_base_url", "") or "").strip().rstrip("/") or DEFAULT_NR_BASE
    except Exception:
        return DEFAULT_NR_BASE


def default_connection_name(email: str) -> str:
    """产线流连接名：zcode-<邮箱前缀>（非 own:——own 前缀留给用户自有号）。"""
    prefix = str(email or "").split("@")[0][:20]
    return f"zcode-{prefix}"


# ---------------------------------------------------------------- 协作轮（init → 人工授权 → poll → 铸 key）

def _default_device_http(log: Callable[..., None] = print) -> Any:
    """默认传输：curl_cffi chrome 指纹（Clash 7897 + ASCII CA 垫），缺失退回 requests。"""
    try:
        from captcha_node.probe_zcode_oauth_20261004 import make_cffi_http

        http = make_cffi_http()
        log("传输层=curl_cffi chrome 指纹（proxy 127.0.0.1:7897，CA 已垫 ASCII 路径）")
        return http
    except Exception as err:  # noqa: BLE001
        log(f"curl_cffi 传输层不可用（{err}），退回 requests 传输")
        return RequestsHttp()


def _make_default_authorize(log: Callable[..., None]) -> Callable[[str], None]:
    def _authorize(url: str) -> None:
        log("=" * 72)
        log("【人工协作】请在打开的浏览器里完成：登录 z.ai → 滑块 → 点授权(Authorize)")
        log(f"授权链接（如未自动打开请手动复制）：\n{url}")
        log(f"等待上限 {DEFAULT_DEADLINE:.0f}s；超时/过期直接重跑命令重新铸 flow，无副作用")
        log("=" * 72)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    return _authorize


def _make_heartbeat(log: Callable[..., None], deadline_s: float) -> Callable[[Any], None]:
    state = {"t0": time.time(), "last": 0.0}

    def _tick(_poll) -> None:
        now = time.time()
        if now - state["last"] >= 15.0:  # 15s 一条心跳，别刷屏
            state["last"] = now
            log(f"…等待人工授权中 ({now - state['t0']:.0f}s / {deadline_s:.0f}s)")

    return _tick


def run_collab_round(
    device_http: Any = None,
    log: Callable[..., None] = print,
    *,
    on_authorize: Optional[Callable[[str], None]] = None,
    on_pending: Optional[Callable[[Any], None]] = None,
    deadline_s: float = DEFAULT_DEADLINE,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    require_secret: bool = True,
) -> dict:
    """协作轮：cli_init →（人工授权回调留白，默认打印 URL+开浏览器）→ poll → 铸 key。

    铸 key 双路：ready.zai.access_token 走 mint_from_ready（主路，2026-10-04 实测三步全通）；
    主路失败且有 ready.token（chat JWT）时回退 mint_from_chat_token（首步同为
    /api/auth/z/login，仅入参 token 不同）。两路全败 → ZcodeOAuthError(step="mint")。

    返回（内存真值，日志全程打码）::
        {chat_jwt, plan_key, user_id, email, steps, mint_path, flow_id}
    """
    http = device_http if device_http is not None else _default_device_http(log)
    steps: list = []

    device = cli_init(http)
    steps.append(f"init:{device.flow_id[:8]}")
    log(f"[collab] flow={mask_key(device.flow_id)} 已铸，等待人工授权（deadline {deadline_s:.0f}s）")

    authorize_cb = on_authorize if on_authorize is not None else _make_default_authorize(log)
    authorize_cb(device.authorize_url)

    pending_cb = on_pending if on_pending is not None else _make_heartbeat(log, deadline_s)
    poll = poll_until_ready(
        device, http=http, deadline_s=deadline_s, sleep=sleep, now=now, on_pending=pending_cb
    )
    if poll.is_failed:
        raise ZcodeOAuthError("poll_failed", "ZCode 授权失败或被取消")
    steps.append("poll:ready")

    user_id = str(poll.user.get("user_id", "") or "")
    email = str(poll.user.get("email", "") or "")
    log(f"[collab] 授权就绪 email={email or '?'} user_id={mask_key(user_id)}，开始铸 key")

    try:
        mint = mint_from_ready(poll, http=http, require_secret=require_secret)
        mint_path = "zai_oauth"
    except (ZcodeOAuthError, GlmMintError) as err:
        if not poll.chat_token:
            raise ZcodeOAuthError("mint", f"OAuth 铸 key 失败且无 chat JWT 可回退: {err}") from err
        log(f"[collab] zai OAuth 铸 key 失败（{mask_key(str(err)) if len(str(err)) > 8 else err}），回退 chat JWT 链")
        try:
            mint = mint_from_chat_token(poll.chat_token, http=http, require_secret=require_secret)
            mint_path = "chat_jwt"
        except (ZcodeOAuthError, GlmMintError) as err2:
            raise ZcodeOAuthError(
                "mint", f"双路铸 key 全败: oauth路={err} / chatjwt路={err2}"
            ) from err2
    steps.append(f"mint:{'+'.join(mint.steps)}({mint_path})")
    log(
        f"[collab] plan_key={mask_key(mint.combined_key)} reused={mint.reused} "
        f"has_secret={mint.has_secret} path={mint_path}"
    )

    return {
        "chat_jwt": poll.chat_token,
        "plan_key": mint.combined_key,
        "user_id": user_id,
        "email": email,
        "steps": steps,
        "mint_path": mint_path,
        "flow_id": device.flow_id,
    }


# ---------------------------------------------------------------- 步骤2：导入 9Router

def import_to_9router(
    plan_key: str,
    email: str,
    name: Optional[str] = None,
    *,
    client: Any = None,
    log: Callable[..., None] = print,
) -> dict:
    """POST /api/providers 建 glm apikey 连接（面板会话，loopback 绕代理），回读 conn_id+isActive。

    同名 glm 连接已存在 → 复用不重开（reused=True，幂等）。任何失败抛
    ZcodePipelineError(step="import"/"import_readback", status=HTTP码)。
    """
    if not plan_key:
        raise ZcodePipelineError("import", "plan_key 为空，拒绝导入")
    cli = client if client is not None else NRClient()
    conn_name = name or default_connection_name(email)
    body = build_provider_payload(plan_key, name=conn_name)  # {provider:"glm", apiKey, name}

    try:
        cli.ensure_login()
    except Exception as err:  # noqa: BLE001
        raise ZcodePipelineError("import", f"9Router 面板登录失败: {err}") from err

    # 幂等复用：同名 glm 连接已在 → 不重开（量产重跑防重复连接）
    try:
        for c in cli.list_connections():
            if str(c.get("name", "")) == conn_name and str(c.get("provider", "")) == "glm":
                conn_id = str(c.get("id", ""))
                log(f"[import] 复用已有连接 conn_id={conn_id} name={conn_name}")
                return {
                    "conn_id": conn_id,
                    "name": conn_name,
                    "is_active": bool(c.get("isActive")),
                    "reused": True,
                    "provider": "glm",
                }
    except ZcodePipelineError:
        raise
    except Exception as err:  # noqa: BLE001
        raise ZcodePipelineError("import", f"列连接失败（回读前置）: {err}") from err

    try:
        resp, status = cli._request("POST", "/api/providers", body)
    except urllib.error.HTTPError as err:
        detail = ""
        try:
            detail = err.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise ZcodePipelineError(
            "import", f"创建连接失败 HTTP {err.code}: {detail}", status=err.code
        ) from err
    except Exception as err:  # noqa: BLE001
        raise ZcodePipelineError("import", f"创建连接异常（9Router 不可达?）: {err}") from err
    if status not in (200, 201):
        raise ZcodePipelineError("import", f"创建连接失败 HTTP {status}: {str(resp)[:200]}", status=status)
    conn = resp.get("connection") if isinstance(resp, dict) else None
    conn = conn if isinstance(conn, dict) else (resp if isinstance(resp, dict) else {})
    conn_id = str(conn.get("id", "") or "")
    if not conn_id:
        raise ZcodePipelineError("import", f"创建响应无连接 id: {str(resp)[:200]}", status=status)

    # 回读 conn_id+isActive（审计惯例：写后必读回）
    is_active: Optional[bool] = None
    try:
        for c in cli.list_connections():
            if str(c.get("id", "")) == conn_id:
                is_active = bool(c.get("isActive"))
                break
    except Exception as err:  # noqa: BLE001
        raise ZcodePipelineError("import_readback", f"回读连接列表失败: {err}") from err
    if is_active is None:
        raise ZcodePipelineError(
            "import_readback", f"创建返回 id={conn_id} 但连接列表里找不到（回读失败）"
        )
    log(f"[import] conn_id={conn_id} name={conn_name} isActive={is_active} key={mask_key(plan_key)}")
    return {
        "conn_id": conn_id,
        "name": conn_name,
        "is_active": is_active,
        "reused": False,
        "provider": "glm",
    }


# ---------------------------------------------------------------- 步骤3：网关实调验活

def _resolve_gateway_apikey(
    explicit: Optional[str] = None, gateway_db: Optional[os.PathLike] = None
) -> str:
    """网关 sk- key 来源：显式参 > config_store nr_api_key > env NR_API_KEY > 网关库 apiKeys 只读。

    只在内存使用，永不打印（调用方日志只出 mask_key）。
    """
    if explicit:
        return str(explicit)
    try:
        from core.config_store import config_store

        v = str(config_store.get("nr_api_key", "") or "").strip()
        if v:
            return v
    except Exception:
        pass
    env = os.environ.get("NR_API_KEY", "").strip()
    if env:
        return env
    db_path = Path(gateway_db or DEFAULT_GATEWAY_DB)
    if not db_path.exists():
        raise ZcodePipelineError(
            "verify",
            f"找不到网关 API key：config_store nr_api_key / env NR_API_KEY 均空，"
            f"网关库不存在 {db_path}",
        )
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT key FROM apiKeys WHERE isActive=1 ORDER BY rowid LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except Exception as err:  # noqa: BLE001
        raise ZcodePipelineError("verify", f"读网关库 apiKeys 失败: {err}") from err
    if not row or not str(row[0] or "").strip():
        raise ZcodePipelineError("verify", f"网关库 {db_path} 无 isActive apiKeys 记录")
    return str(row[0])


def _default_post_status(
    url: str, payload: dict, headers: dict, timeout: float = 30.0
) -> tuple:
    """默认验活传输：urllib 直发（loopback opener 绕 Clash TUN），返回 (http_status, body_text)。

    HTTP 4xx/5xx 不抛异常（状态码正是判读对象）；网络层异常才抛。
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        resp = opener.open(req, timeout=timeout)
        return int(resp.status), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        try:
            body = err.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return int(err.code), body


def classify_verify_status(http_status: int, body_text: str) -> str:
    """429/1113→pending_quota；200→ok；401/403→broken；其余→unknown。"""
    if http_status == 200:
        return QUOTA_OK
    if http_status == 429:
        return QUOTA_PENDING  # 1113 与否都归 pending_quota：429=上游认了 key（429 文本里通常带 1113）
    if http_status in (401, 403):
        return QUOTA_BROKEN
    return QUOTA_UNKNOWN


def verify_connection(
    conn_id: str,
    *,
    post: Optional[Callable[..., tuple]] = None,
    apikey: Optional[str] = None,
    gateway_db: Optional[os.PathLike] = None,
    model: str = DEFAULT_GATEWAY_MODEL,
    base_url: Optional[str] = None,
    log: Callable[..., None] = print,
) -> dict:
    """网关实调一发（小 prompt 'hi'，max_tokens=8），按状态码判读 key/链路状态。

    429/1113 → pending_quota（key 活无套餐，不算失败）；200 → ok；401/403 → broken。
    返回 {conn_id, model, http_status, quota_status, detail_head, tested_at}；detail 里 key 已抹除。
    """
    post_fn = post if post is not None else _default_post_status
    key = _resolve_gateway_apikey(apikey, gateway_db)
    url = f"{(base_url or _nr_base()).rstrip('/')}/v1/chat/completions"
    payload = {"model": model, "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}
    try:
        status, text = post_fn(url, payload, {"Authorization": f"Bearer {key}"})
    except ZcodePipelineError:
        raise
    except Exception as err:  # noqa: BLE001
        raise ZcodePipelineError("verify", f"网关实调网络异常: {err}") from err
    text = (text or "").replace(key, "<masked-key>")  # 双保险：响应体不回显 key
    quota_status = classify_verify_status(int(status), text)
    log(f"[verify] conn_id={conn_id} model={model} HTTP {status} → {quota_status} body={text[:120]}")
    return {
        "conn_id": conn_id,
        "model": model,
        "http_status": int(status),
        "quota_status": quota_status,
        "detail_head": text[:200],
        "tested_at": _now_iso(),
    }


# ---------------------------------------------------------------- 步骤4：台账回写（直连 sqlite，不经 :8000）

def ledger_writeback(
    email: str,
    chat_jwt: str,
    plan_key_prefix8: str,
    conn_id: str,
    quota_status: str,
    *,
    db_path: Optional[os.PathLike] = None,
    user_id: str = "",
    http_status: Optional[int] = None,
    log: Callable[..., None] = print,
) -> dict:
    """accounts（platform=zcode + email）行 extra_json 回写，幂等，密码列永不动。

    - extra_json.zcode_pipeline = {conn_id, plan_key(prefix8), quota_status, user_id,
      http_status, updated_at} 固定键覆盖（重跑不膨胀不重复）；
    - chat_jwt 非空 → 刷新 token 列与 extra.token（zcode 行既有约定，行 89 同款）；
    - 行不存在 → ZcodePipelineError(step="ledger")（先注册入库再回写）。
    """
    db = Path(db_path) if db_path else DEFAULT_LEDGER_DB
    if not db.exists():
        raise ZcodePipelineError("ledger", f"台账库不存在: {db}")
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, extra_json, password FROM accounts "
            "WHERE platform='zcode' AND email=? ORDER BY id DESC LIMIT 1",
            (email,),
        ).fetchone()
        if row is None:
            raise ZcodePipelineError(
                "ledger", f"台账无 platform=zcode 且 email={email} 的行（先注册入库再回写）"
            )
        try:
            extra = json.loads(row["extra_json"] or "{}")
            if not isinstance(extra, dict):
                extra = {}
        except (ValueError, TypeError):
            extra = {}
        extra["zcode_pipeline"] = {
            "conn_id": conn_id,
            "plan_key": str(plan_key_prefix8 or ""),
            "quota_status": quota_status,
            "user_id": user_id,
            "http_status": http_status,
            "updated_at": _now_iso(),
        }
        chat_jwt = str(chat_jwt or "")
        if chat_jwt:
            extra["token"] = chat_jwt
        cur = conn.execute(
            "UPDATE accounts SET extra_json=?, token=COALESCE(NULLIF(?,''), token), "
            "updated_at=? WHERE id=?",
            (json.dumps(extra, ensure_ascii=False), chat_jwt, _now_iso(), row["id"]),
        )
        conn.commit()
        log(
            f"[ledger] 行 id={row['id']} email={email} 回写完成 "
            f"(conn_id={conn_id} plan_key={plan_key_prefix8}... quota={quota_status} "
            f"chat_jwt={'刷新' if chat_jwt else '保留'}) rows={cur.rowcount}"
        )
        return {
            "email": email,
            "row_id": int(row["id"]),
            "conn_id": conn_id,
            "plan_key_prefix8": str(plan_key_prefix8 or ""),
            "quota_status": quota_status,
            "chat_jwt_written": bool(chat_jwt),
            "updated_rows": int(cur.rowcount),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------- 主入口：全链编排

def _mask_collab(collab: dict) -> dict:
    """collab 结果的对外形态：token/key 只出 masked（内存真值只在 run_collab_round 返回里）。"""
    out = dict(collab)
    for k in ("plan_key", "chat_jwt", "flow_id"):
        if k in out:
            out[k] = mask_key(str(out[k] or ""))
    return out


def full_round(
    browser_authorize_callable: Optional[Callable[[str], None]] = None,
    apply: bool = False,
    *,
    device_http: Any = None,
    deadline_s: float = DEFAULT_DEADLINE,
    db_path: Optional[os.PathLike] = None,
    gateway_db: Optional[os.PathLike] = None,
    nr_client_factory: Optional[Callable[[], Any]] = None,
    verify_post: Optional[Callable[..., tuple]] = None,
    gateway_apikey: Optional[str] = None,
    log: Callable[..., None] = print,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> dict:
    """1→4 串起来：协作轮+铸 key →（apply 才继续）导入 → 验活 → 台账回写。

    apply=False（干跑）：只跑协作轮+铸 key（上游动作），不建连接/不实调/不回写，
    返回 {mode:"dry_run", collab(masked), plan}；
    apply=True：返回 {mode:"apply", collab(masked), import, verify, ledger}。
    任一步失败抛带 step 的精确异常（ZcodeOAuthError / ZcodePipelineError）。
    """
    collab = run_collab_round(
        device_http,
        log,
        on_authorize=browser_authorize_callable,
        deadline_s=deadline_s,
        sleep=sleep,
        now=now,
    )
    email = collab["email"]
    plan = {
        "email": email,
        "user_id": collab["user_id"],
        "connection_name": default_connection_name(email),
        "plan_key_masked": mask_key(collab["plan_key"]),
        "chat_jwt_masked": mask_key(collab["chat_jwt"]),
        "gateway_base": _nr_base(),
        "verify_model": DEFAULT_GATEWAY_MODEL,
        "ledger_db": str(Path(db_path) if db_path else DEFAULT_LEDGER_DB),
        "quota_note": "429/1113=pending_quota 属预期（活动/连接动作后到账）；200=全绿；401/403=断链",
    }
    log(f"[plan] {json.dumps(plan, ensure_ascii=False)}")

    if not apply:
        log("[dry-run] apply=False：仅返回计划，未导入 9Router / 未实调 / 未回写台账")
        return {"mode": "dry_run", "collab": _mask_collab(collab), "plan": plan}

    client = (nr_client_factory or NRClient)()
    imp = import_to_9router(
        collab["plan_key"], email, name=plan["connection_name"], client=client, log=log
    )
    ver = verify_connection(
        imp["conn_id"],
        post=verify_post,
        apikey=gateway_apikey,
        gateway_db=gateway_db,
        model=plan["verify_model"],
        log=log,
    )
    wb = ledger_writeback(
        email,
        collab["chat_jwt"],
        str(collab["plan_key"][:8]),
        imp["conn_id"],
        ver["quota_status"],
        db_path=db_path,
        user_id=collab["user_id"],
        http_status=ver["http_status"],
        log=log,
    )
    log(f"[done] 全链完成：conn_id={imp['conn_id']} quota={ver['quota_status']} 台账行={wb['row_id']}")
    return {
        "mode": "apply",
        "collab": _mask_collab(collab),
        "import": imp,
        "verify": ver,
        "ledger": wb,
        "plan": plan,
    }


# ---------------------------------------------------------------- CLI（明早一句话命令的落点）

def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m platforms.zcode.zcode_pipeline",
        description="ZCode 全链一键流水线（协作轮→铸key→推9Router→验活→台账回写）")
    parser.add_argument("--apply", action="store_true", help="导入9Router+验活+台账回写（默认干跑）")
    parser.add_argument("--deadline", type=float, default=DEFAULT_DEADLINE, help="等待人工授权秒数（默认 600）")
    parser.add_argument("--no-open", action="store_true", help="不自动开浏览器，只打印授权链接")
    parser.add_argument("--db", default=str(DEFAULT_LEDGER_DB), help="台账 sqlite 路径（默认 account_manager.db）")
    args = parser.parse_args(argv)

    def on_authorize(url: str) -> None:
        print("=" * 72)
        print("【人工协作】请在打开的浏览器里完成：登录 z.ai → 滑块 → 点授权(Authorize)")
        print(f"授权链接（如未自动打开请手动复制）：\n{url}")
        print("=" * 72)
        if not args.no_open:
            try:
                webbrowser.open(url)
            except Exception:
                pass

    try:
        result = full_round(
            on_authorize,
            apply=args.apply,
            deadline_s=args.deadline,
            db_path=args.db,
        )
    except (ZcodeOAuthError, ZcodePipelineError) as err:
        step = getattr(err, "step", "?")
        status = getattr(err, "status", None)
        print(f"[FAIL] step={step} status={status}\n  {err}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
