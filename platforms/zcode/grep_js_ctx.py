"""从 chunk 源码提取 {{url}}/chat/completions 模板上下文 + 真实 url 变量定义"""
import re
import urllib.request

PROXY = "http://127.0.0.1:7897"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": PROXY, "http": PROXY}))

for c in ("LoginLogo-Crl6-p34.js", "_page-V8dn8z9i.js"):
    cjs = opener.open(f"https://chat.z.ai/assets/{c}", timeout=60).read().decode("utf-8", "ignore")
    print(f"\n===== {c} (len={len(cjs)}) =====")
    for m in re.finditer(r"chat/completions", cjs):
        s = max(0, m.start() - 400)
        e = min(len(cjs), m.end() + 400)
        print("---- 上下文 ----")
        print(cjs[s:e])
        print()
    # url 定义（zai-api / chatglm.site / base 之类）
    for m in re.finditer(r'["\'](https?://[a-zA-Z0-9.-]+(?:chatglm|z\.ai)[a-zA-Z0-9./_-]*)["\']', cjs):
        print("URL 常量:", m.group(1))
