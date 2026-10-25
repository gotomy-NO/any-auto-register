"""下载 chat.z.ai 聊天页 CDN chunk，grep 模型推理端点（completions/stream/ask 路径）"""
import re
import urllib.request

PROXY = "http://127.0.0.1:7897"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": PROXY, "http": PROXY}))

CDN = "https://z-cdn.chatglm.cn/z-ai/frontend/prod-fe-1.1.92/assets/"
# 聊天相关 chunk（抓包出现的）
chunks = [
    "_page-CJh1HDHZ.js",  # New Chat 后加载
    "_page-Bq0-VEcy.js",
    "_page-DLYfogVE.js",
    "_page-CKnco_3a.js",
    "_page-DM5aAh3y.js",
    "_page-V8dn8z9i.js",
    "_getTag-TVuNWAcq.js",
    "chunk-FUIDI54P-nve1Q5YS.js",
]

PATS = [
    re.compile(r'["\'`](/[a-zA-Z0-9_/${}.-]*(?:completions|/ask|/stream|/infer|/generate|/query|/message)[a-zA-Z0-9_/${}.-]*)["\'`]'),
    re.compile(r'(?:url|path|endpoint|base)\s*[:=]\s*["\'`](/api/[^"\'`]{3,80})["\'`]'),
    re.compile(r'["\'`](/api/v1/[a-zA-Z0-9_/${}.-]{3,80})["\'`]'),
    re.compile(r'["\'`](https?://[a-zA-Z0-9.-]+(?:chatglm|z\.ai|bigmodel|zai)[a-zA-Z0-9./_-]{3,80})["\'`]'),
]
seen = set()
for c in chunks:
    try:
        js = opener.open(CDN + c, timeout=60).read().decode("utf-8", "ignore")
    except Exception as e:
        print(c, "下载失败", str(e)[:50])
        continue
    found = set()
    for p in PATS:
        for m in p.findall(js):
            if len(m) < 100 and "chat/completions" not in m:  # 排除 i18n 提示
                found.add(m)
    new = found - seen
    if new:
        seen |= found
        print(f"\n=== {c} (len={len(js)}) ===")
        for f in sorted(new)[:30]:
            print("   ", f)
print("\n=== 完成 ===")
