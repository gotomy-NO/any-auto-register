"""对照实验：用浏览器同款完整 body 直连 chat.z.ai，判断 captcha 判定是否与请求格式/账号有关
A. 完整 body（无 captcha 参数）——新账号 JamesMartin
B. 完整 body + 新鲜参数（先抓后立即用）——之后跑
C. 旧账号 ZtestAbc8 简单 body
"""
import base64
import hashlib
import hmac
import json
import time
import uuid

import requests

SIGN_KEY = "key-@@@@)))()((9))-xxxx&&&%%%%%"
FE_VERSION = "prod-fe-1.1.92"
BASE = "https://chat.z.ai"

JM_CRED = r"E:\AI\openAI\AIClient2API-main\configs\zcode\1787986739492_zcode-auth\1787986739492_zcode-auth.json"
OLD_CRED = r"E:\AI\openAI\AIClient2API-main\configs\zcode\1787977538174_zcode-auth\1787977538174_zcode-auth.json"


def load_token(path):
    d = json.load(open(path, encoding="utf-8"))
    return d.get("token") or d.get("ZCODE_COOKIE_TOKEN") or d.get("ZCODE_COOKIE_TOKEN_FILE_PATH") or ""


def hmac_hex(key, data):
    return hmac.new(key.encode(), data.encode(), hashlib.sha256).hexdigest()


def sign(body_str, prompt, ts):
    m = int(ts // 300000)
    v = hmac_hex(SIGN_KEY, str(m))
    p = base64.b64encode(prompt.encode()).decode()
    h = f"{body_str}|{p}|{ts}"
    return hmac_hex(v, h)


def browser_body(prompt="hi"):
    """与浏览器完全一致的 body（含 variables/preview_mode/background_tasks）"""
    return {
        "stream": True,
        "model": "x-preview-l",
        "messages": [{"role": "user", "content": prompt}],
        "signature_prompt": prompt,
        "params": {},
        "extra": {},
        "features": {
            "image_generation": False,
            "web_search": False,
            "auto_web_search": False,
            "preview_mode": True,
            "flags": [],
            "vlm_tools_enable": False,
            "vlm_web_search_enable": False,
            "vlm_website_mode": False,
            "enable_thinking": True,
            "reasoning_effort": "max",
        },
        "variables": {
            "{{USER_NAME}}": "ysEAc",
            "{{USER_LOCATION}}": "Unknown",
            "{{CURRENT_DATETIME}}": "2026-08-29 15:17:13",
            "{{CURRENT_DATE}}": "2026-08-29",
            "{{CURRENT_TIME}}": "15:17:13",
            "{{CURRENT_WEEKDAY}}": "Saturday",
            "{{CURRENT_TIMEZONE}}": "Asia/Shanghai",
            "{{USER_LANGUAGE}}": "en-US",
        },
        "background_tasks": {"title_generation": True, "tags_generation": True},
    }


def call(token, body, label, captcha_param=None, device_id=None, region="overseas"):
    if captcha_param:
        body = {**body, "captcha_verify_param": captcha_param}
    ts = int(time.time() * 1000)
    body_str = json.dumps(body, sort_keys=True, separators=(",", ":"))
    sig = sign(body_str, body.get("signature_prompt", ""), ts)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept-Language": "en-US",
        "X-FE-Version": FE_VERSION,
        "X-Signature": sig,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0",
        "Origin": "https://chat.z.ai",
        "x-region": region,
    }
    if device_id:
        headers["x-device-id"] = device_id
    url = (
        f"{BASE}/api/v2/chat/completions?timestamp={ts}"
        f"&requestId={uuid.uuid4()}&user_id={token.split('.')[0]}&version=0.0.1"
        f"&platform=web&language=en-US&timezone=Asia%2FShanghai"
        f"&signature_timestamp={ts}"
    )
    r = requests.post(url, headers=headers, json=body, timeout=60)
    txt = r.text[:260].replace("\n", " ")
    print(f"[{label}] status={r.status_code}")
    print("  ", txt)
    return r


jm = load_token(JM_CRED)
old = load_token(OLD_CRED)
print("JM token len:", len(jm), "| OLD token len:", len(old))

# A. 完整 body，无 captcha——新账号
call(jm, browser_body(), "A-完整body无captcha-新账号")
# C. 旧账号简单 body
call(old, browser_body(), "C-完整body-旧账号")
# D. 旧账号简单 body（同 A2 原样）
simple = {
    "stream": False,
    "model": "glm-5.3",
    "messages": [{"role": "user", "content": "hi"}],
    "signature_prompt": "hi",
    "params": {},
    "extra": {},
    "features": {"image_generation": False},
}
call(old, simple, "D-简单body-旧账号")
