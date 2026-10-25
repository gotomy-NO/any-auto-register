"""测试 zcode/chat.z.ai API（用捕获的 token——Open WebUI OpenAI 兼容接口）
探测：/api/models（原生）/v1/models（OpenAI 兼容）→ /api/chat/completions 对话测试
"""
import json

import requests

TOKEN_PATH = r"E:\AI\openAI\AIClient2API-main\configs\zcode\zai_session.json"

with open(TOKEN_PATH, encoding="utf-8") as f:
    data = json.load(f)
token = data.get("token", "")
print("token 长度:", len(token))

headers = {"Authorization": f"Bearer {token}"}
base = "https://chat.z.ai"

# 1. 探测模型端点（Open WebUI 原生 + OpenAI 兼容）
for path in ("/api/models", "/v1/models", "/api/v1/models"):
    try:
        r = requests.get(base + path, headers=headers, timeout=15)
        print(f"{path}: {r.status_code}", r.text[:200])
        if r.status_code == 200 and "data" in r.text:
            models = r.json().get("data", [])
            print(f"  模型数: {len(models)} | 前 5:", [m.get("id") for m in models[:5]])
    except Exception as e:
        print(f"{path}: 异常", str(e)[:80])

# 2. 对话测试（Open WebUI OpenAI 兼容端点 /openai/v1/ + 其他）
for path in (
    "/openai/v1/chat/completions",
    "/openai/v1/models",
    "/api/chat/completions",
    "/v1/chat/completions",
    "/api/v1/chat/completions",
):
    try:
        r = requests.post(
            base + path,
            headers=headers,
            json={
                "model": "glm-5.3",
                "messages": [{"role": "user", "content": "hi, say hello in one word"}],
                "max_tokens": 20,
            },
            timeout=30,
        )
        print(f"chat {path}: {r.status_code}", r.text[:250])
        if r.status_code == 200:
            print("  API 可用！")
            break
    except Exception as e:
        print(f"chat {path}: 异常", str(e)[:80])
