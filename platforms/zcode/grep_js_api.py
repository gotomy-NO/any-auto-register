"""从 chat.z.ai 前端 JS bundle 搜对话 API 端点（completions/chat/messages 路径）"""
import re
import urllib.request

PROXY = "http://127.0.0.1:7897"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": PROXY, "http": PROXY}))

# 先拿 HTML 找到 JS bundle 列表
html = opener.open("https://chat.z.ai/", timeout=30).read().decode("utf-8", "ignore")
bundles = sorted(set(re.findall(r'(assets/[A-Za-z0-9_.-]+\.js)', html)))
print("入口 bundle:", bundles[:10])

# 关键端点模式
PATTERNS = re.compile(r'["\'](/?[a-zA-Z0-9_/-]*(?:completions|chat|message|inference|generate|stream|sse|ask|conversation)[a-zA-Z0-9_/-]*)["\']')
hits = {}
for b in bundles[:20]:
    url = "https://chat.z.ai/" + b if not b.startswith("http") else b
    try:
        js = opener.open(url, timeout=30).read().decode("utf-8", "ignore")
    except Exception as e:
        print(b[:60], "下载失败", str(e)[:60])
        continue
    found = set(PATTERNS.findall(js))
    interesting = [p for p in found if len(p) < 80 and ("api" in p.lower() or "/" in p)]
    if interesting:
        hits[b] = sorted(interesting)
        print(f"\n{b[:70]}")
        for p in sorted(interesting)[:25]:
            print("   ", p)

# 另搜 fetch/axios 的 baseURL 与 'v1/' 前缀
print("\n=== v1/ 前缀端点 ===")
v1 = set()
for b in bundles[:20]:
    url = "https://chat.z.ai/" + b
    try:
        js = opener.open(url, timeout=30).read().decode("utf-8", "ignore")
    except Exception:
        continue
    v1 |= set(re.findall(r'["\'](api/v1/[a-zA-Z0-9_/${}.-]{2,60})["\']', js))
    v1 |= set(re.findall(r'["\'](/v1/[a-zA-Z0-9_/${}.-]{2,60})["\']', js))
for p in sorted(v1)[:40]:
    print("   ", p)
