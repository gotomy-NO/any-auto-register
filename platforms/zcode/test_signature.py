"""验证 chat.z.ai X-Signature 签名算法（逆向自前端混淆代码）+ /api/chat/completions 实测
算法：KEY="key-@@@@)))()((9))-xxxx&&&%%%%%"；m=floor(ts/300000)；v=HMAC(KEY,str(m))；
      h=payload+"|"+base64(prompt)+"|"+ts；signature=HMAC(v,h).hex()
"""
import base64
import hashlib
import hmac
import json
import time
import uuid

import requests

TOKEN_PATH = r"E:\AI\openAI\AIClient2API-main\configs\zcode\zai_session.json"
with open(TOKEN_PATH, encoding="utf-8") as f:
    TOKEN = json.load(f)["token"]

KEY = "key-@@@@)))()((9))-xxxx&&&%%%%%"
BASE = "https://chat.z.ai"


def sign(payload: str, prompt: str, ts_ms: int) -> str:
    m = ts_ms // 300000
    v = hmac.new(KEY.encode(), str(m).encode(), hashlib.sha256).hexdigest()
    p = base64.b64encode(prompt.encode("utf-8")).decode()
    h = payload + "|" + p + "|" + str(ts_ms)
    return hmac.new(v.encode(), h.encode(), hashlib.sha256).hexdigest()


# 构造请求体（参考前端 Rfe body 结构）
msg_id = str(uuid.uuid4())
ts_ms = int(time.time() * 1000)
prompt = "say hello in one word"
body = {
    "stream": True,
    "model": "glm-5.3",
    "messages": [{"role": "user", "content": prompt}],
    "signature_prompt": prompt,
    "params": {},
    "extra": {},
    "features": {"image_generation": False},
}

# payload 序列化变体（Tre() 排序未知——逐个尝试）
payload_variants = {
    "sort_keys": json.dumps(body, sort_keys=True, separators=(",", ":")),
    "sorted_keys_compact": json.dumps(body, sort_keys=True),
    "default": json.dumps(body, separators=(",", ":")),
    "indent": json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
}

H = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "Accept-Language": "zh-CN",
    "X-FE-Version": "prod-fe-1.1.92",
}

# completion_version=2 → 端点是 /api/v2/chat/completions（前端 x4 映射 {1:/api, 2:/api/v2}）
ENDPOINTS = [
    "/api/v2/chat/completions",
    "/api/chat/completions",
]

for ep in ENDPOINTS:
    for name, payload_str in payload_variants.items():
        sig = sign(payload_str, prompt, ts_ms)
        qs = f"signature_timestamp={ts_ms}"
        headers = dict(H)
        headers["X-Signature"] = sig
        try:
            r = requests.post(
                f"{BASE}{ep}?{qs}",
                headers=headers,
                json=body,
                timeout=60,
                stream=True,
            )
            head = ""
            if r.status_code == 200:
                for i, line in enumerate(r.iter_lines(decode_unicode=True)):
                    if line:
                        head += line[:150] + "\n"
                    if i > 6:
                        break
            else:
                head = r.text[:200]
            print(f"[{ep} | {name}] {r.status_code} ct={r.headers.get('content-type','')}")
            print("   ", head.replace("\n", " | ")[:400])
            if r.status_code == 200:
                print("   ✅ 签名有效——对话 API 直连可行！")
                break
        except Exception as e:
            print(f"[{ep} | {name}] 异常:", str(e)[:100])
    else:
        continue
    break
