"""直接测试 Open WebUI 对话端点（/api/v1/chats/new + /api/v1/chats/{id}/completions）
请求体格式来自前端真实抓包
"""
import json
import uuid

import requests

TOKEN_PATH = r"E:\AI\openAI\AIClient2API-main\configs\zcode\zai_session.json"
with open(TOKEN_PATH, encoding="utf-8") as f:
    TOKEN = json.load(f)["token"]

BASE = "https://chat.z.ai"
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

# 1. 建会话（chats/new——抓包格式）
msg_id = str(uuid.uuid4())
chat_body = {
    "chat": {
        "id": "",
        "title": "api test",
        "models": ["glm-5.3"],
        "params": {},
        "history": {
            "messages": {
                msg_id: {
                    "id": msg_id,
                    "parentId": None,
                    "childrenIds": [],
                    "role": "user",
                    "content": "say hello in one word",
                    "timestamp": 1787974338,
                    "models": ["glm-5.3"],
                }
            },
            "currentId": msg_id,
        },
        "tags": [],
        "flags": [],
        "features": [],
        "mcp_servers": [],
        "enable_thinking": False,
        "reasoning_effort": "",
    }
}
r = requests.post(f"{BASE}/api/v1/chats/new", headers=H, json=chat_body, timeout=30)
print("chats/new:", r.status_code, r.text[:400])
chat_id = (r.json() or {}).get("id")
print("chat_id:", chat_id)

if chat_id:
    # 2. 推断（completions——Open WebUI 标准端点）
    comp_body = {
        "model": "glm-5.3",
        "messages": [{"role": "user", "content": "say hello in one word"}],
        "stream": True,
        "chat_id": chat_id,
    }
    r2 = requests.post(
        f"{BASE}/api/v1/chats/{chat_id}/completions",
        headers=H,
        json=comp_body,
        timeout=60,
        stream=True,
    )
    print("completions:", r2.status_code, "content-type:", r2.headers.get("content-type"))
    text = ""
    try:
        for line in r2.iter_lines(decode_unicode=True):
            if line:
                text += line[:200] + "\n"
                if len(text) > 1500:
                    break
    except Exception as e:
        text += f"流读取异常: {e}"
    print("响应前 1500 字符:", text[:1500])
