"""端到端验证：通过 A2(9000) 实际对话——模型列表 + chat/completions 实测"""
import requests

BASE = "http://127.0.0.1:9000"
H = {"Authorization": "Bearer <REDACTED_API_KEY>", "Content-Type": "application/json"}

r = requests.post(
    f"{BASE}/v1/chat/completions",
    headers=H,
    json={
        "model": "zcode-web:glm-5.3",
        "messages": [{"role": "user", "content": "say hello in one word"}],
        "max_tokens": 30,
        "stream": False,
    },
    timeout=90,
)
print("chat status:", r.status_code)
body = r.text
print("body head:", body[:600])
