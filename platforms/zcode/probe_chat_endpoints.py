"""实验：直连 chat.z.ai 对比 /api/ 与 /api/v2/ 两个 chat/completions 端点（带 X-Signature）
目的：确认 A2 用的 /api/v2 是否正确，captcha 是否只在某个端点强制
"""
import hashlib
import hmac
import json
import time

import requests

TOKEN = json.load(open(r"E:\AI\openAI\AIClient2API-main\configs\zcode\1787986739492_zcode-auth\1787986739492_zcode-auth.json", encoding="utf-8"))["token"]

SIGN_KEY = "key-@@@@)))()((9))-xxxx&&&%%%%%"
FE_VERSION = "prod-fe-1.1.92"
BASE = "https://chat.z.ai"


def hmac_hex(key, data):
    return hmac.new(key.encode(), data.encode(), hashlib.sha256).hexdigest()


def sign(body_str, prompt, ts):
    m = int(ts // 300000)
    v = hmac_hex(SIGN_KEY, str(m))
    p = __import__("base64").b64encode(prompt.encode()).decode()
    h = f"{body_str}|{p}|{ts}"
    return hmac_hex(v, h)


def build_body(model, prompt):
    messages = [{"role": "user", "content": prompt}]
    return {
        "stream": False,
        "model": model,
        "messages": messages,
        "signature_prompt": prompt,
        "params": {},
        "extra": {},
        "features": {"image_generation": False},
    }


def call(path_tpl, model="glm-5.3"):
    prompt = "say hello in one word"
    body = build_body(model, prompt)
    ts = int(time.time() * 1000)
    body_str = json.dumps(body, sort_keys=True, separators=(",", ":"))
    sig = sign(body_str, prompt, ts)
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Accept-Language": "en-US",
        "X-FE-Version": FE_VERSION,
        "X-Signature": sig,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    }
    url = f"{BASE}{path_tpl.format(ts=ts)}"
    try:
        r = requests.post(url, headers=headers, json=body, timeout=60)
        print(f"[{path_tpl}] status={r.status_code}")
        print("  ", r.text[:400].replace("\n", " "))
    except Exception as e:
        print(f"[{path_tpl}] ERR {str(e)[:150]}")


for p in (
    "/api/v2/chat/completions?signature_timestamp={ts}",
    "/api/chat/completions?signature_timestamp={ts}",
    "/api/chat/completions",
):
    call(p)
