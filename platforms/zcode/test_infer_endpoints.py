"""测试 chat.z.ai 候选推理端点（completion_version=2 线索）+ grep 主 bundle 上下文"""
import json
import re
import urllib.request

import requests

TOKEN_PATH = r"E:\AI\openAI\AIClient2API-main\configs\zcode\zai_session.json"
with open(TOKEN_PATH, encoding="utf-8") as f:
    TOKEN = json.load(f)["token"]

BASE = "https://chat.z.ai"
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

# 1. 候选推理端点（Open WebUI completion_version=2）
candidates = [
    "/api/v1/completions",
    "/api/v2/completions",
    "/api/v1/chat/completions",
    "/api/completions",
    "/api/v1/messages",
]
body = {
    "model": "glm-5.3",
    "messages": [{"role": "user", "content": "say hello in one word"}],
    "stream": True,
    "version": "2",
}
for p in candidates:
    try:
        r = requests.post(BASE + p, headers=H, json=body, timeout=40, stream=True)
        ct = r.headers.get("content-type", "")
        head = ""
        if r.status_code == 200 and "text/event-stream" in ct:
            for i, line in enumerate(r.iter_lines(decode_unicode=True)):
                if line:
                    head += line[:150] + "\n"
                if i > 8:
                    break
        else:
            head = r.text[:200]
        print(f"{p}: {r.status_code} ct={ct}")
        print("   ", head.replace("\n", " | ")[:300])
        if r.status_code == 200:
            print("   ✅ 找到推理端点！")
    except Exception as e:
        print(f"{p}: 异常 {str(e)[:80]}")

# 2. grep 主 bundle：completion_version / completions 上下文
opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": "http://127.0.0.1:7897", "http": "http://127.0.0.1:7897"}))
js = opener.open(BASE + "/assets/index-CrxzZnYM.js", timeout=60).read().decode("utf-8", "ignore")
print("\n主 bundle 长度:", len(js))
for pat in (r"completion_version", r"completions", r"v1/chat", r"chat_id"):
    ms = list(re.finditer(pat, js))
    print(f"\n'{pat}' 出现 {len(ms)} 次")
    for m in ms[:3]:
        s = max(0, m.start() - 250)
        print("   ...", js[s: m.end() + 250].replace("\n", " ")[:520], "...")
