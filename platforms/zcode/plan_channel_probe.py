# -*- coding: utf-8 -*-
"""zcode-plan 计划通道最小复现探测（2026-10-05）。

目标：攻克「zcode-plan 通道到底怎么调」——用最小请求矩阵实测：
  1. POST /api/v1/zcode-plan/anthropic/v1/messages（405 之谜：方法/路径/头组合）
  2. /api/v1/ultra[-zai]/anthropic/v1/messages（agent/configs 服务端重写目标）
  3. /api/v1/zcode-plan/chat/completions（OpenAI 形态）
  4. 请求头组合：X-Device-Mid / x-zcode-* 全家桶 / anthropic-version / captcha 头
  5. api.z.ai/api/anthropic（直连形态）用 zcode_jwt / 业务 token 两种凭据
  6. 业务链：auth/z/login 换 token → quota/limit（判定额度有无）

结论落盘 docs/zcode计划通道攻坚-20261005.md 引用的原始证据 JSON：
  输出 <repo>/docs/zcode计划通道攻坚-probe-<ts>.json

纪律：
  - token/key 一律不回显（prefix8+len）；结果 JSON 只存指纹不存明文。
  - 只读探测 + 一次性小请求，不改账号状态、不杀进程。
  - 运行方式（与本机约定一致）：
      cd register-8000
      env -u PYTHONPATH -u HTTP_PROXY -u HTTPS_PROXY \
        SSL_CERT_FILE="$LOCALAPPDATA/anyauto-cacert.pem" \
        CURL_CA_BUNDLE="$LOCALAPPDATA/anyauto-cacert.pem" \
        .venv/Scripts/python.exe -m platforms.zcode.plan_channel_probe [--captcha-verify PARAM]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = REPO_ROOT / "account_manager.db"
DOCS_DIR = REPO_ROOT / "docs"

ZCODE_BASE = "https://zcode.z.ai"
ZAI_BASE = "https://api.z.ai"

# 计划通道候选路径（全部相对 ZCODE_BASE，除 P8 直连 ZAI_BASE）
PLAN_ANTHROPIC = "/api/v1/zcode-plan/anthropic/v1/messages"
PLAN_CHAT = "/api/v1/zcode-plan/chat/completions"
ULTRA_ANTHROPIC = "/api/v1/ultra/anthropic/v1/messages"
ULTRA_ZAI_ANTHROPIC = "/api/v1/ultra-zai/anthropic/v1/messages"
ZAI_ANTHROPIC = "/api/anthropic/v1/messages"

APP_VERSIONS = ["3.11.2", "3.12.3"]

# ZCode 3.12.3 g6n（LLM 默认头）+ TV（源码头）合并集——按 pi-zcode/TriDefender 逆向
# 注：g6n 平面无 X-Device-Mid；TV 平面带。探测矩阵里两种都试。
TRACE_IDS = {
    "x-request-id": None,       # 运行时填 uuid
    "x-zcode-trace-id": None,
    "x-session-id": None,
    "x-query-id": None,         # 01a0 + 28 hex
}


def _mask(v: Optional[str]) -> str:
    if not v:
        return "<empty>"
    return f"{v[:8]}...({len(v)})"


def ensure_ascii_ca() -> str:
    """中文路径杀 CA 自愈：certifi 路径含非 ASCII 时拷到 %TEMP% 再设环境。"""
    import certifi

    ca = certifi.where()
    override = os.environ.get("SSL_CERT_FILE") or os.environ.get("CURL_CA_BUNDLE")
    if override and override.isascii() and Path(override).exists():
        return override
    try:
        ca.encode("ascii")
        return ca
    except UnicodeEncodeError:
        pass
    import shutil
    import tempfile

    tmp_ca = Path(tempfile.gettempdir()) / "anyauto-cacert-probe.pem"
    shutil.copyfile(ca, tmp_ca)
    os.environ["SSL_CERT_FILE"] = str(tmp_ca)
    os.environ["CURL_CA_BUNDLE"] = str(tmp_ca)
    return str(tmp_ca)


def load_account(account_id: int = 89) -> dict:
    """从台账取 zcode_jwt / zai_at（只回指纹）。"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, email, extra_json FROM accounts WHERE id=?", (account_id,)
    ).fetchone()
    conn.close()
    if row is None:
        raise SystemExit(f"account id={account_id} 不存在")
    ex = json.loads(row["extra_json"] or "{}")
    creds = {
        "id": row["id"],
        "email": row["email"],
        "zcode_jwt": ex.get("zcode_jwt") or "",
        "zai_at": ex.get("zai_at") or "",
        "chat_token": ex.get("token") or "",
    }
    print(
        "[creds] id=%s email=%s zcode_jwt=%s zai_at=%s chat_token=%s"
        % (
            creds["id"],
            creds["email"],
            _mask(creds["zcode_jwt"]),
            _mask(creds["zai_at"]),
            _mask(creds["chat_token"]),
        )
    )
    if not creds["zcode_jwt"]:
        raise SystemExit("zcode_jwt 为空——先跑 login_direct_v2 登录入库")
    return creds


def load_device_mid() -> str:
    p = Path.home() / ".zcode" / "v2" / "telemetry-state.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        dm = d.get("deviceMid") or ""
        print("[device] deviceMid=%s" % _mask(dm))
        return dm
    except Exception as e:  # noqa: BLE001
        print("[device] telemetry-state 读取失败 %s（继续，无 device_mid）" % e)
        return ""


def make_session():
    from curl_cffi import requests as cffi

    ca = ensure_ascii_ca()
    # chrome 指纹；代理完全交给环境（运行时已 -u 清空，Clash TUN 透明接管）
    s = cffi.Session(impersonate="chrome", verify=ca, timeout=30)
    return s


def source_headers(app_version: str, device_mid: str, plane: str = "g6n") -> dict:
    """plane='g6n'：LLM/网关平面（无 X-Device-Mid，X-ZCode-Agent 内联）
    plane='tv' ：源/路由平面（带 X-Device-Mid）。"""
    h = {
        "HTTP-Referer": "https://zcode.z.ai",
        "User-Agent": f"ZCode/{app_version}",
        "X-ZCode-App-Version": app_version,
        "X-Title": "Z Code@electron",
        "X-Release-Channel": "production",
        "X-Client-Language": "zh-CN",
        "X-Client-Timezone": "Asia/Shanghai",
        "X-Platform": "win32-x64",
        "X-Os-Category": "windows",
    }
    if plane == "g6n":
        h["X-ZCode-Agent"] = "glm"
    else:
        h["X-Os-Version"] = "10.0.26100"
        if device_mid:
            h["X-Device-Mid"] = device_mid
    return h


def trace_headers() -> dict:
    qid = "01a0" + uuid.uuid4().hex[:28]
    return {
        "x-request-id": str(uuid.uuid4()),
        "x-zcode-trace-id": str(uuid.uuid4()),
        "x-session-id": str(uuid.uuid4()),
        "x-query-id": qid,
    }


def build_body(model: str, stream: bool = False) -> dict:
    body = {
        "model": model,
        "max_tokens": 64,
        "stream": stream,
        "system": [{"type": "text", "text": "You are a helpful assistant."}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Reply exactly: OK"}]}
        ],
    }
    return body


class Probe:
    def __init__(self) -> None:
        self.results: list[dict] = []

    def call(
        self,
        tag: str,
        method: str,
        url: str,
        headers: dict,
        body: Optional[dict] = None,
        note: str = "",
    ) -> dict:
        s = make_session()
        rec: dict = {
            "tag": tag,
            "method": method,
            "url": url,
            "note": note,
            "header_names": sorted(k.lower() for k in headers),
        }
        t0 = time.time()
        try:
            if method == "GET":
                r = s.get(url, headers=headers)
            else:
                r = s.post(url, headers=headers, json=body or {})
            text = r.text or ""
            rec.update(
                status=r.status_code,
                latency_ms=int((time.time() - t0) * 1000),
                resp_headers={
                    k: v
                    for k, v in (r.headers.items() or {})
                    if k.lower()
                    in (
                        "x-request-id",
                        "x-aliyun-captcha-verify-param",
                        "content-type",
                        "server",
                        "cf-mitigated",
                        "www-authenticate",
                        "allow",
                    )
                },
                body=text[:900],
            )
        except Exception as e:  # noqa: BLE001
            rec.update(error=f"{type(e).__name__}: {e}", latency_ms=int((time.time() - t0) * 1000))
        finally:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
        self.results.append(rec)
        print(
            "[%s] %s %s -> %s %s"
            % (
                tag,
                method,
                url.split("z.ai", 1)[-1][:80],
                rec.get("status", rec.get("error")),
                (rec.get("body") or rec.get("error", ""))[:160].replace("\n", " "),
            )
        )
        return rec

    def dump(self) -> Path:
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        out = DOCS_DIR / ("zcode计划通道攻坚-probe-%d.json" % int(time.time()))
        out.write_text(
            json.dumps(self.results, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print("[dump] 证据 JSON -> %s" % out)
        return out


def exchange_business_token(zcode_jwt: str) -> Optional[str]:
    """zcode_jwt(=chat.z.ai 会话 JWT) -> z.ai 业务 access_token（glm_mint 同链）。

    已知坑：guest 类 JWT 走此端点回 code500；探一次拿结论即可，不重试。
    """
    s = make_session()
    try:
        r = s.post(
            f"{ZAI_BASE}/api/auth/z/login",
            headers={"Content-Type": "application/json"},
            json={"token": zcode_jwt},
        )
        data = r.json() if "json" in (r.headers.get("content-type") or "") else {}
        at = (data.get("data") or {}).get("access_token") or data.get("access_token")
        print(
            "[biz] auth/z/login -> %s code=%s access_token=%s"
            % (r.status_code, data.get("code"), _mask(at))
        )
        return at
    except Exception as e:  # noqa: BLE001
        print("[biz] exchange error %s" % e)
        return None
    finally:
        s.close()


def quota_probe(access_token: str) -> dict:
    s = make_session()
    out: dict = {}
    try:
        r = s.get(
            f"{ZAI_BASE}/api/monitor/usage/quota/limit",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        out["quota_limit"] = {"status": r.status_code, "body": r.text[:500]}
        print("[biz] quota/limit -> %s %s" % (r.status_code, r.text[:180]))
    except Exception as e:  # noqa: BLE001
        out["quota_limit"] = {"error": str(e)}
    finally:
        s.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account-id", type=int, default=89)
    ap.add_argument(
        "--captcha-verify",
        default="",
        help="外部捕获的阿里云 verifyParam（可选；带了就加试 captcha 头组合）",
    )
    ap.add_argument("--captcha-region", default="")
    ap.add_argument("--model", default="glm-5.3")
    ap.add_argument("--skip-business", action="store_true")
    args = ap.parse_args()

    creds = load_account(args.account_id)
    device_mid = load_device_mid()
    jwt = creds["zcode_jwt"]
    p = Probe()

    # ---------- P0 基线：账单端点（确认 JWT 活性）----------
    for av in APP_VERSIONS[:1]:
        h = {
            **source_headers(av, device_mid, "tv"),
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/json",
        }
        p.call("P0-balance", "GET", f"{ZCODE_BASE}/api/v1/zcode-plan/billing/balance?app_version={av}", h)

    # ---------- P1 计划通道核心谜团：405 复现 + 参数化 ----------
    for av in APP_VERSIONS:
        for plane in ("g6n", "tv"):
            h = {
                **source_headers(av, device_mid, plane),
                **trace_headers(),
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "anthropic-version": "2023-06-01",
                "Authorization": f"Bearer {jwt}",
            }
            p.call(
                f"P1-post-{av}-{plane}",
                "POST",
                f"{ZCODE_BASE}{PLAN_ANTHROPIC}",
                h,
                build_body(args.model),
                note="全头 Anthropic 形态；405 是否随头组合变化",
            )

    # P1b: 极简头（只有 Authorization+CT）——头白名单/UA 触发 405?
    h_min = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}
    p.call("P1b-post-min", "POST", f"{ZCODE_BASE}{PLAN_ANTHROPIC}", h_min, build_body(args.model))

    # P1c: GET / HEAD 方法对照
    h_g = {**source_headers(APP_VERSIONS[0], device_mid, "g6n"), "Authorization": f"Bearer {jwt}"}
    p.call("P1c-get", "GET", f"{ZCODE_BASE}{PLAN_ANTHROPIC}", h_g, note="方法对照")

    # P1d: stream=true + 无 system + 大 max_tokens（贴近真实客户端）
    h_d = {
        **source_headers(APP_VERSIONS[1], device_mid, "g6n"),
        **trace_headers(),
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "anthropic-version": "2023-06-01",
        "Authorization": f"Bearer {jwt}",
    }
    body_d = build_body(args.model, stream=True)
    body_d["thinking"] = {"type": "enabled", "budget_tokens": 8000}
    body_d["output_config"] = {"effort": "low"}
    p.call("P1d-post-stream", "POST", f"{ZCODE_BASE}{PLAN_ANTHROPIC}", h_d, body_d)

    # ---------- P2 ultra / ultra-zai（服务端重写目标）----------
    for path in (ULTRA_ANTHROPIC, ULTRA_ZAI_ANTHROPIC):
        h = {
            **source_headers(APP_VERSIONS[1], device_mid, "g6n"),
            **trace_headers(),
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "anthropic-version": "2023-06-01",
            "Authorization": f"Bearer {jwt}",
        }
        p.call(f"P2-{path.strip('/').split('/')[-2]}", "POST", f"{ZCODE_BASE}{path}", h, build_body(args.model))

    # ---------- P3 chat/completions 形态 ----------
    h3 = {
        **source_headers(APP_VERSIONS[1], device_mid, "g6n"),
        **trace_headers(),
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {jwt}",
    }
    p.call(
        "P3-plan-chat",
        "POST",
        f"{ZCODE_BASE}{PLAN_CHAT}",
        h3,
        {"model": args.model, "messages": [{"role": "user", "content": "Reply exactly: OK"}], "stream": False, "max_tokens": 64},
    )

    # ---------- P4 无鉴权对照（WAF 层判定）----------
    p.call(
        "P4-noauth",
        "POST",
        f"{ZCODE_BASE}{PLAN_ANTHROPIC}",
        {**source_headers(APP_VERSIONS[1], device_mid, "g6n"), "Content-Type": "application/json", "anthropic-version": "2023-06-01"},
        build_body(args.model),
        note="无 Authorization——405 若依旧=网关层而非鉴权层",
    )

    # ---------- P5 captcha 头组合（可选：外部捕获 verifyParam）----------
    if args.captcha_verify:
        h5 = {
            **source_headers(APP_VERSIONS[1], device_mid, "g6n"),
            **trace_headers(),
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "Authorization": f"Bearer {jwt}",
            "x-aliyun-captcha-verify-param": args.captcha_verify,
        }
        if args.captcha_region:
            h5["x-aliyun-captcha-verify-region"] = args.captcha_region
        p.call("P5-post-captcha", "POST", f"{ZCODE_BASE}{PLAN_ANTHROPIC}", h5, build_body(args.model))

    # ---------- P8 直连 api.z.ai Anthropic（zcode_jwt 当 Bearer / x-api-key）----------
    for auth_style, headers_key in (("bearer", "Authorization"), ("x-api-key", "x-api-key")):
        h8 = {
            **source_headers(APP_VERSIONS[1], device_mid, "g6n"),
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            headers_key: f"Bearer {jwt}" if headers_key == "Authorization" else jwt,
        }
        p.call(
            f"P8-zai-{auth_style}",
            "POST",
            f"{ZAI_BASE}{ZAI_ANTHROPIC}",
            h8,
            build_body(args.model),
            note="直连形态：zcode_jwt 是否可当 Anthropic key 用",
        )

    # ---------- P9 业务链：换业务 token + quota（判定有无套餐）----------
    if not args.skip_business:
        at = exchange_business_token(jwt)
        if at:
            q = quota_probe(at)
            p.results.append({"tag": "P9-business", "note": "业务 token 额度", **q})
        else:
            p.results.append(
                {"tag": "P9-business", "note": "auth/z/login 未换到业务 token（guest 类 JWT 已知 code500）"}
            )

    p.dump()
    print("\n===== 汇总 =====")
    for r in p.results:
        print("%-22s %-4s %-58s %s" % (r["tag"], r.get("method", ""), r.get("url", "")[8:66], r.get("status", r.get("error", "?"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
