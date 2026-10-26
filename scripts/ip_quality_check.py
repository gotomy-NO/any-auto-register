# -*- coding: utf-8 -*-
"""出口 IP 质量检测利器（2026-10-05 因当日出口 IP 被换 3+ 次而建）。

一次探测五件事，输出 JSON + 人话打印：
  1. 出口 IP（走 Clash 127.0.0.1:7897；失败自动落 --noproxy 直连重试）
  2. 归属（ip-api.com：country/city/isp/org/hosting/proxy/mobile）
  3. Cloudflare 视角（cdn-cgi/trace：ip/loc/colo/warp/gateway）
  4. OpenAI 挑战率（curl_cffi chrome 指纹打 auth.openai.com N=5，统计 challenge/pass）
  5. trae passport 可用性（GET www.trae.ai 是否 200 正常页）

判定：
  hosting=false 且 challenge<=2/5      → 「优良」
  hosting=true                          → 「机房段-不宜用于 CF 类站点」
  其余（挑战重 / 探测失败）              → 「存疑」
另附一句建议。

用法：
  python scripts/ip_quality_check.py            # 人话打印 + JSON
  python scripts/ip_quality_check.py --json     # 仅 JSON（stdout 纯 JSON）
  python scripts/ip_quality_check.py --append docs/ip_quality_history.jsonl  # 追加历史行

注意：默认全部流量走 Clash 代理；对本机服务的探测必须 --noproxy（本工具不含该类探测）。
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys

CLASH_PROXY = "http://127.0.0.1:7897"
IPAPI_URL = "http://ip-api.com/json/?fields=status,message,country,city,isp,org,hosting,proxy,mobile,query"
CF_TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
OPENAI_URL = "https://auth.openai.com/log-in"
TRAE_URL = "https://www.trae.ai/"
CHALLENGE_N = 5
HTTP_TIMEOUT = 15

DEFAULT_HISTORY = "docs/ip_quality_history.jsonl"


# ---------------------------------------------------------------- requests ---
def fetch_text(url: str, proxy: str | None = None, timeout: int = HTTP_TIMEOUT) -> tuple[int, str]:
    """GET 一个 URL，返回 (http_status, text)。

    proxy=None 直连；proxy=CLASH_PROXY 走 Clash。请求层失败（连不上/超时）
    抛异常由调用方兜底。status 为 0 表示请求层失败（如 SSL/代理错误）。
    """
    from curl_cffi import requests as creq

    proxies = {"http": proxy, "https": proxy} if proxy else None
    r = creq.get(url, proxies=proxies, timeout=timeout, impersonate="chrome", allow_redirects=True)
    return r.status_code, r.text


# ---------------------------------------------------------------- probes -----
def probe_ipapi(proxy: str | None) -> dict:
    """ip-api 归属探测。失败返回 {'error': ...}。"""
    try:
        status, text = fetch_text(IPAPI_URL, proxy=proxy)
        data = json.loads(text)
        if status == 200 and data.get("status") == "success":
            return {
                "ip": data.get("query"),
                "country": data.get("country"),
                "city": data.get("city"),
                "isp": data.get("isp"),
                "org": data.get("org"),
                "hosting": bool(data.get("hosting")),
                "proxy": bool(data.get("proxy")),
                "mobile": bool(data.get("mobile")),
            }
        return {"error": f"http={status} body={text[:200]}"}
    except Exception as exc:  # 网络/JSON 层失败都归为探测失败
        return {"error": f"{type(exc).__name__}: {exc}"}


def probe_cf_trace(proxy: str | None) -> dict:
    """Cloudflare 视角：ip/loc/colo/warp/gateway。失败返回 {'error': ...}。"""
    want = ("ip", "loc", "colo", "warp", "gateway")
    try:
        status, text = fetch_text(CF_TRACE_URL, proxy=proxy)
        if status != 200:
            return {"error": f"http={status}"}
        kv = {}
        for line in text.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                if k in want:
                    kv[k] = v
        missing = [k for k in want if k not in kv]
        if missing:
            return {"error": f"missing fields: {missing}"}
        return {k: kv[k] for k in want}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def looks_like_challenge(body: str) -> bool:
    """从响应体判断 OpenAI 是否给了挑战（而不是正常登录页）。

    挑战/拦截特征：cloudflare 质询、See full verification、NOT_FOUND、
    该服务暂时不可用、或缺失正常登录页文案（sign in/password）。
    """
    low = body.lower()
    if not body.strip():
        return True
    if "challenge" in low or "see full verification" in low or "verify you are human" in low:
        return True
    if "just a moment" in low or "cf-chl" in low or "attention required" in low:
        return True
    if "not_found" in low or "svcgate" in low:
        return True
    if "service unavailable" in low or "该服务暂时不可用" in body:
        return True
    if "sign in" not in low and "password" not in low:
        return True
    return False


def classify_openai_response(status: int, body: str) -> str:
    """把一次 OpenAI 响应归类：challenge / pass / error。

    顺序：5xx 服务器错误先归 error（不污染挑战率）；
    识别出的挑战/拦截页归 challenge；200 正常登录页归 pass；
    其余非 200（403/429 等未识别拒绝）保守归 challenge。
    """
    if status >= 500:
        return "error"
    if looks_like_challenge(body):
        return "challenge"
    if status == 200:
        return "pass"
    return "challenge"


def probe_openai(proxy: str | None, n: int = CHALLENGE_N, timeout: int = HTTP_TIMEOUT) -> dict:
    """打 auth.openai.com N 次，统计 challenge/pass/error。

    单次请求失败（连接层异常/超时）计入 error，不中断整轮。
    """
    counts = {"challenge": 0, "pass": 0, "error": 0}
    for _ in range(n):
        try:
            status, body = fetch_text(OPENAI_URL, proxy=proxy, timeout=timeout)
            counts[classify_openai_response(status, body)] += 1
        except Exception:
            counts["error"] += 1
    total = counts["challenge"] + counts["pass"]
    rate = round(counts["challenge"] / total, 2) if total else 1.0
    return {
        "n": n,
        "challenge": counts["challenge"],
        "pass": counts["pass"],
        "error": counts["error"],
        "challenge_rate": rate,
    }


def probe_trae(proxy: str | None, timeout: int = HTTP_TIMEOUT) -> dict:
    """GET www.trae.ai 判断是否出正常页。失败返回 {'ok': False, 'error': ...}。"""
    try:
        status, body = fetch_text(TRAE_URL, proxy=proxy, timeout=timeout)
        low = body.lower()
        ok = (
            status == 200
            and "just a moment" not in low
            and "cf-chl" not in low
            and len(body) > 1000
        )
        return {"ok": bool(ok), "status": status, "len": len(body)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------- verdict ----
def build_verdict(data: dict) -> dict:
    """根据探测结果给判定分与建议。纯函数，单测重点。"""
    ipapi = data.get("ipapi") or {}
    cf = data.get("cf") or {}
    openai = data.get("openai") or {}
    trae = data.get("trae") or {}

    hosting = ipapi.get("hosting")
    challenge = openai.get("challenge")
    n = openai.get("n")

    reasons: list[str] = []
    if hosting is True:
        verdict = "机房段-不宜用于 CF 类站点"
        reasons.append("ip-api 判定该段为机房/hosting")
    elif hosting is None or challenge is None:
        verdict = "未知"
        reasons.append("关键探测缺失（ip-api 或 OpenAI 挑战率没拿到）")
    elif challenge <= 2:
        verdict = "优良"
    else:
        verdict = "存疑"
        reasons.append(f"OpenAI 挑战 {challenge}/{n} 超阈值（>2）")

    if verdict == "优良" and trae.get("ok") is False:
        verdict = "存疑"
        reasons.append("trae passport 未出正常页")

    # 建议
    if verdict == "优良":
        advice = "当前出口可用于注册/过 CF 类站点，建议此窗口内尽快跑量并记录 IP。"
    elif verdict == "机房段-不宜用于 CF 类站点":
        advice = "出口落在机房段，建议切住宅/家宽节点后再跑注册，避免烧号。"
    elif verdict == "未知":
        advice = "探测不完整，先检查 Clash 7897 端口与上游节点，再重跑本工具。"
    else:
        advice = "挑战率偏高或 trae 被拦，建议换出口 IP 后重测；此 IP 暂缓注册。"

    if cf.get("warp") == "on":
        reasons.append("Cloudflare 视角 warp=on（出口疑似 WARP）")
    if ipapi.get("proxy") is True:
        reasons.append("ip-api 判定 proxy=true（已被标记为代理）")

    return {"verdict": verdict, "reasons": reasons, "advice": advice}


# ---------------------------------------------------------------- runner -----
def run_checks(n: int = CHALLENGE_N) -> dict:
    """完整跑一轮探测：先走 Clash，ip-api 带 error 视为不可用 → 整轮降级直连。"""

    def attempt(proxy: str | None) -> dict:
        return {
            "via": "clash" if proxy else "direct",
            "ipapi": probe_ipapi(proxy),
            "cf": probe_cf_trace(proxy),
            "openai": probe_openai(proxy, n=n),
            "trae": probe_trae(proxy),
        }

    data = attempt(CLASH_PROXY)
    ipapi_ok = isinstance(data.get("ipapi"), dict) and not data["ipapi"].get("error")
    if not ipapi_ok:
        direct = attempt(None)
        # 只在直连真的拿到归属时才算降级成功，否则保留 Clash 那份结果
        if isinstance(direct.get("ipapi"), dict) and not direct["ipapi"].get("error"):
            data = direct
        else:
            data["fallback"] = "clash 探测失败且直连也未取得归属，保留 clash 结果"
    data["verdict"] = build_verdict(data)
    return data


def human_print(data: dict) -> None:
    v = data.get("verdict", {})
    ipapi = data.get("ipapi") or {}
    cf = data.get("cf") or {}
    openai = data.get("openai") or {}
    trae = data.get("trae") or {}
    print(f"== IP 质量检测 {data.get('ts', '')} (via {data.get('via')}) ==")
    print(f"出口 IP   : {ipapi.get('ip') or cf.get('ip') or '?'}")
    print(
        "归属      : "
        + ", ".join(
            str(x)
            for x in [ipapi.get("country"), ipapi.get("city"), ipapi.get("isp"), ipapi.get("org")]
            if x
        )
    )
    print(
        f"ip-api 标记: hosting={ipapi.get('hosting')} proxy={ipapi.get('proxy')} mobile={ipapi.get('mobile')}"
    )
    if ipapi.get("error"):
        print(f"ip-api 错误: {ipapi['error']}")
    print(
        f"CF 视角   : loc={cf.get('loc')} colo={cf.get('colo')} warp={cf.get('warp')} gateway={cf.get('gateway')}"
    )
    if cf.get("error"):
        print(f"CF 错误   : {cf['error']}")
    print(
        f"OpenAI    : challenge={openai.get('challenge')}/{openai.get('n')} "
        f"pass={openai.get('pass')} error={openai.get('error')} (rate {openai.get('challenge_rate')})"
    )
    print(f"trae      : ok={trae.get('ok')} status={trae.get('status')} len={trae.get('len')}")
    print(f"判定      : {v.get('verdict')}")
    for r in v.get("reasons", []):
        print(f"  - {r}")
    print(f"建议      : {v.get('advice')}")


def append_history(path: str, data: dict) -> None:
    """把本轮结果作为一行 JSON 追加进历史文件（补 ts），便于看 IP 时间线。"""
    import os

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    row = dict(data)
    row["ts"] = data.get("ts") or datetime.datetime.now().isoformat(timespec="seconds")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="出口 IP 质量检测（Clash 出口 / 归属 / CF 视角 / OpenAI 挑战率 / trae）"
    )
    parser.add_argument("--json", action="store_true", help="仅输出 JSON（stdout 纯 JSON）")
    parser.add_argument("--append", metavar="FILE", help="把本轮结果追加进 FILE（JSONL，一行一次检测）")
    parser.add_argument("--n", type=int, default=CHALLENGE_N, help="OpenAI 探测次数（默认 5）")
    parser.add_argument("--noproxy", action="store_true", help="强制直连（不走 Clash）")
    args = parser.parse_args(argv)

    if args.noproxy:
        data = {
            "via": "direct",
            "ipapi": probe_ipapi(None),
            "cf": probe_cf_trace(None),
            "openai": probe_openai(None, n=args.n),
            "trae": probe_trae(None),
        }
        data["verdict"] = build_verdict(data)
    else:
        data = run_checks(n=args.n)
    data["ts"] = datetime.datetime.now().isoformat(timespec="seconds")

    if args.append:
        append_history(args.append, data)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        human_print(data)
        print(json.dumps(data, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
