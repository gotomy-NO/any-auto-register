"""补测：requests 走 ipflygates 代理访问 x.ai/sign-up，区分"代理不通" vs "被 x.ai 拦截" """
import time

import requests

PROXY = {
    "http": "http://aa_00019-zone-resi-region-DE-spec-4:Aa123456@ep.ipflygates.com:6616",
    "https": "http://aa_00019-zone-resi-region-DE-spec-4:Aa123456@ep.ipflygates.com:6616",
}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"

for url, name in [
    ("https://accounts.x.ai/sign-up", "x.ai/sign-up"),
    ("https://httpbin.org/ip", "httpbin(对照)"),
]:
    try:
        t0 = time.time()
        r = requests.get(url, proxies=PROXY, timeout=30, headers={"User-Agent": UA})
        body = r.text[:150].replace("\n", " ")
        print(f"{name}: HTTP {r.status_code} in {time.time()-t0:.1f}s len={len(r.text)} | {body}")
    except Exception as e:
        print(f"{name}: ERR {str(e)[:160]}")
