"""抓取 CaptchaRun 官网/文档，查找任务类型列表与 aliyun 支持线索"""
import re

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
urls = [
    "https://captcha.run/",
    "https://captcha.run/docs",
    "https://captcha.run/zh",
    "https://captcha.run/api",
    "https://captcha.run/pricing",
    "https://captcha.run/faq",
]
for u in urls:
    try:
        r = requests.get(u, headers={"User-Agent": UA}, timeout=15)
        text = r.text
        print("=" * 60)
        print(f"[{r.status_code}] {u}  len={len(text)}")
        # 找文档/任务类型相关关键词
        for kw in ("Aliyun", "aliyun", "阿里云", "turnstile", "Turnstile", "funcaptcha", "FunCaptcha", "任务类型", "captchaType", "documentation", "docs"):
            hits = [m.start() for m in re.finditer(re.escape(kw), text)]
            if hits:
                for h in hits[:3]:
                    seg = text[max(0, h - 60):h + 80].replace("\n", " ")
                    print(f"  [{kw}] ...{seg}...")
        # 提取所有链接
        links = sorted(set(re.findall(r'href=["\']([^"\']+)["\']', text)))
        docs = [l for l in links if any(k in l.lower() for k in ("doc", "api", "help", "support", "wiki"))]
        if docs:
            print("  doc-links:", docs[:15])
    except Exception as e:
        print(f"[ERR] {u}: {str(e)[:100]}")
