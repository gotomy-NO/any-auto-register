"""全量下载 27 个 chunk 搜对话 API 端点"""
import re
import urllib.request

PROXY = "http://127.0.0.1:7897"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": PROXY, "http": PROXY}))

js = opener.open(
    "https://chat.z.ai/assets/index-CrxzZnYM.js", timeout=60
).read().decode("utf-8", "ignore")
chunks = sorted(set(re.findall(r'assets/([A-Za-z0-9_-]+\.js)', js)))
print("chunk 数:", len(chunks))

PATS = [
    re.compile(r'["\']([^"\']{0,50}chat/completions[^"\']{0,50})["\']'),
    re.compile(r'["\']([^"\']{0,50}/completions[^"\']{0,50})["\']'),
    re.compile(r'["\']([^"\']{0,50}/chats?/[^"\']{2,60})["\']'),
    re.compile(r'["\']([^"\']{0,40}/api/v1/[^"\']{2,70})["\']'),
    re.compile(r'["\']([^"\']{0,40}chatglm[^"\']{0,80}["\']|https?://[a-z.-]*chatglm[^"\'\\]{5,80})'),
]
for c in chunks:
    url = f"https://chat.z.ai/assets/{c}"
    try:
        cjs = opener.open(url, timeout=60).read().decode("utf-8", "ignore")
    except Exception as e:
        print(c[:40], "下载失败", str(e)[:50])
        continue
    found = set()
    for p in PATS:
        found |= set(p.findall(cjs))
    found = {f for f in found if len(f) < 120}
    if found:
        print(f"\n=== {c} ===")
        for f in sorted(found)[:30]:
            print("   ", f)
print("=== 搜索完成 ===")
