"""从 chat.z.ai 前端懒加载 chunk 搜对话 API 端点（vite 动态 import chunk）"""
import re
import urllib.request

PROXY = "http://127.0.0.1:7897"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": PROXY, "http": PROXY}))

html = opener.open("https://chat.z.ai/", timeout=30).read().decode("utf-8", "ignore")
entry = "assets/index-CrxzZnYM.js"
js = opener.open("https://chat.z.ai/" + entry, timeout=60).read().decode("utf-8", "ignore")

# 入口里的所有 chunk 名
chunks = sorted(set(re.findall(r'assets/([A-Za-z0-9_-]+\.js)', js)))
print("chunk 数:", len(chunks))
# 找 Chat 相关 chunk
chat_chunks = [c for c in chunks if re.search(r"chat|Chat|message|Message|agent|Agent|conversation|stream|Stream", c)]
print("Chat 相关 chunk:", chat_chunks[:20])

PAT = re.compile(r'["\']((?:/api)?/?[a-zA-Z0-9_/-]*(?:completions|chat/completions|messages|inference|ask|stream|conversation|chat-messages)[a-zA-Z0-9_/-]*)["\']')
base_pat = re.compile(r'baseURL\s*[:=]\s*["\']([^"\']+)["\']')

for c in chat_chunks[:12]:
    url = f"https://chat.z.ai/assets/{c}"
    try:
        cjs = opener.open(url, timeout=60).read().decode("utf-8", "ignore")
    except Exception as e:
        print(c, "下载失败", str(e)[:60])
        continue
    found = set(PAT.findall(cjs))
    bases = set(base_pat.findall(cjs))
    if found or bases:
        print(f"\n=== {c} ===")
        for p in sorted(found):
            if len(p) < 100:
                print("   ", p)
        for b in sorted(bases):
            print("   baseURL:", b)
