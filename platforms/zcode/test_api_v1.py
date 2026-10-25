"""直接测试 chat.z.ai /api/v1/* 对话端点（cookie 与 header 双鉴权）"""
import json

import requests

TOKEN_PATH = r"E:\AI\openAI\AIClient2API-main\configs\zcode\zai_session.json"
with open(TOKEN_PATH, encoding="utf-8") as f:
    TOKEN = json.load(f)["token"]

BASE = "https://chat.z.ai"

auth_variants = [
    ("header", {"headers": {"Authorization": f"Bearer {TOKEN}"}}, {}),
    ("cookie", {}, {"cookies": {"token": TOKEN}}),
]

# 1. 模型列表
for name, kw, ck in auth_variants:
    try:
        r = requests.get(f"{BASE}/api/v1/models", timeout=15, **kw, **ck)
        print(f"[{name}] GET /api/v1/models: {r.status_code}", r.text[:150])
        if r.status_code == 200:
            models = r.json().get("data") or r.json()
            if isinstance(models, list):
                print("  模型:", [m.get("id") for m in models[:8]])
    except Exception as e:
        print(f"[{name}] models 异常:", str(e)[:100])

# 2. 对话
for name, kw, ck in auth_variants:
    try:
        r = requests.post(
            f"{BASE}/api/v1/chat/completions",
            json={
                "model": "glm-5.3",
                "messages": [{"role": "user", "content": "say hello in one word"}],
                "max_tokens": 30,
            },
            timeout=40,
            **kw,
            **ck,
        )
        print(f"[{name}] POST /api/v1/chat/completions: {r.status_code}", r.text[:300])
        if r.status_code == 200:
            print("  ✅ 对话 API 可用（OpenAI 兼容）！")
    except Exception as e:
        print(f"[{name}] chat 异常:", str(e)[:100])
