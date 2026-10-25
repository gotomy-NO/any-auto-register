"""排查 DuckMail 邮箱创建超时：测 api.duckmail.sbs 连通性 + 试创建邮箱"""
import time

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/143.0 Safari/537.36"

for url, name in [
    ("https://www.duckmail.sbs", "duckmail 首页"),
    ("https://api.duckmail.sbs", "duckmail API"),
]:
    try:
        t0 = time.time()
        r = requests.get(url, timeout=20, headers={"User-Agent": UA})
        print(f"{name}: HTTP {r.status_code} in {time.time()-t0:.1f}s len={len(r.text)} | {r.text[:80].replace(chr(10),' ')}")
    except Exception as e:
        print(f"{name}: ERR {str(e)[:150]}")

# 试创建邮箱（DuckMail 常用端点：GET /api/mailbox/create 或 /api/mailbox）
for path in ("/api/mailbox/create", "/api/mailbox", "/api/domain"):
    try:
        t0 = time.time()
        r = requests.get(f"https://api.duckmail.sbs{path}", timeout=20, headers={"User-Agent": UA})
        print(f"POST探测 {path}: HTTP {r.status_code} in {time.time()-t0:.1f}s | {r.text[:120].replace(chr(10),' ')}")
    except Exception as e:
        print(f"POST探测 {path}: ERR {str(e)[:120]}")
