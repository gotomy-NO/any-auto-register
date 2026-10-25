"""验证：ipflygates 代理访问 duckmail.sbs（区分"代理不稳" vs "ipflygates 拒绝该域名"）"""
import time

import requests

IPF = {
    "http": "http://aa_00019-zone-resi-region-DE-spec-4:Aa123456@ep.ipflygates.com:6616",
    "https": "http://aa_00019-zone-resi-region-DE-spec-4:Aa123456@ep.ipflygates.com:6616",
}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/143.0 Safari/537.36"

# 1) 直连 duckmail（对照）
for label, proxies in [("直连", None), ("ipflygates 代理", IPF)]:
    for url, name in [("https://www.duckmail.sbs", "duckmail 首页"), ("https://api.duckmail.sbs", "duckmail API")]:
        try:
            t0 = time.time()
            r = requests.get(url, proxies=proxies, timeout=20, headers={"User-Agent": UA})
            print(f"{label} {name}: HTTP {r.status_code} in {time.time()-t0:.1f}s len={len(r.text)}")
        except Exception as e:
            print(f"{label} {name}: ERR {str(e)[:140]}")

# 2) 走 ipflygates 创建 DuckMail 邮箱（模拟 get_email 的 /accounts 请求）
import json
try:
    t0 = time.time()
    payload = {"address": "probeipf0001@duckmail.sbs", "password": "Test00000001!"}
    r = requests.post(
        "https://www.duckmail.sbs/api/mail?endpoint=%2Faccounts",
        headers={"authorization": "Bearer REDACTED_PLACEHOLDER", "content-type": "application/json", "x-api-provider-base-url": "https://api.duckmail.sbs"},
        json=payload, proxies=IPF, timeout=25,
    )
    print(f"ipflygates 创建邮箱: HTTP {r.status_code} in {time.time()-t0:.1f}s | {r.text[:150]}")
except Exception as e:
    print(f"ipflygates 创建邮箱: ERR {str(e)[:160]}")
