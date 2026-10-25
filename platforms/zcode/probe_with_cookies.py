"""实验：带浏览器完整 cookies + 关键 headers 直连 chat.z.ai 对话接口
验证 captcha 是否只是缺 cookies/headers 特征（浏览器无 captcha_verify_param 也能成功）
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

# 从抓包文件读浏览器 cookies
data = json.load(open(r"E:\AI\openAI\注册机1\any-auto-register-main\platforms\zcode\_req_capture.json", encoding="utf-8"))
cookies = data.get("cookies", [])

# 只保留 chat.z.ai / .z.ai 域的 cookie
z_cookies = {
    c["name"]: c["value"]
    for c in cookies
    if "z.ai" in c.get("domain", "") and c.get("value")
}
print("z.ai cookies:", {k: v[:40] for k, v in z_cookies.items()})

TOKEN = z_cookies.get("token") or json.load(
    open(r"E:\AI\openAI\AIClient2API-main\configs\zcode\1787986739492_zcode-auth\1787986739492_zcode-auth.json", encoding="utf-8")
)["token"]


def hmac_hex(key, data):
    return hmac.new(key.encode(), data.encode(), hashlib.sha256).hexdigest()


def sign(body_str, prompt, ts):
    m = int(ts // 300000)
    v = hmac_hex(SIGN_KEY, str(m))
    p = base64.b64encode(prompt.encode()).decode()
    h = f"{body_str}|{p}|{ts}"
    return hmac_hex(v, h)


def call_with_cookies(use_cookies, use_headers):
    prompt = "say hello in one word"
    body = {
        "stream": False,
        "model": "glm-5.3",
        "messages": [{"role": "user", "content": prompt}],
        "signature_prompt": prompt,
        "params": {},
        "extra": {},
        "features": {"image_generation": False},
        "variables": {
            "{{USER_NAME}}": "ysEAc",
            "{{USER_LOCATION}}": "Unknown",
            "{{CURRENT_DATE}}": "2026-08-29",
        },
    }
    ts = int(time.time() * 1000)
    body_str = json.dumps(body, sort_keys=True, separators=(",", ":"))
    sig = sign(body_str, prompt, ts)
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Accept-Language": "en-US",
        "X-FE-Version": FE_VERSION,
        "X-Signature": sig,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0",
    }
    if use_headers:
        headers.update(
            {
                "x-device-id": "uid_rjnz0dgmvcmya358",
                "x-region": "overseas",
                "origin": "https://chat.z.ai",
                "referer": "https://chat.z.ai/",
                "accept": "*/*",
            }
        )
    url = f"{BASE}/api/v2/chat/completions?timestamp={ts}&requestId={uuid.uuid4()}&user_id={TOKEN.split('.')[1] and ''}"
    r = requests.post(
        url,
        headers=headers,
        json=body,
        cookies=z_cookies if use_cookies else None,
        timeout=60,
    )
    print(f"[cookies={use_cookies} headers={use_headers}] status={r.status_code}")
    print("  ", r.text[:350].replace("\n", " "))
    return r


# 四种组合对比
call_with_cookies(False, False)
call_with_cookies(True, False)
call_with_cookies(False, True)
call_with_cookies(True, True)
