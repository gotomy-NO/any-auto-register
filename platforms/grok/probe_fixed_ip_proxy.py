"""验证固定 IP 代理 accel.ipflygates.com:5001（CuHMnGX8tHOSTo:W0bHgfdy）
出口应固定为 96.62.98.97（美国）。多次探测确认 IP 不轮换。
"""
import time

import requests

PROXY = {
    "http": "http://CuHMnGX8tHOSTo:W0bHgfdy@accel.ipflygates.com:5001",
    "https": "http://CuHMnGX8tHOSTo:W0bHgfdy@accel.ipflygates.com:5001",
}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/143.0 Safari/537.36"

ips = []
for i in range(5):
    try:
        r = requests.get("https://httpbin.org/ip", proxies=PROXY, timeout=25, headers={"User-Agent": UA})
        ip = r.json().get("origin", "?")
        ips.append(ip)
        print(f"第 {i+1} 次: HTTP {r.status_code} | {ip}")
    except Exception as e:
        print(f"第 {i+1} 次: ERR {str(e)[:100]}")
    time.sleep(0.5)

print("---")
print("唯一 IP 数:", len(set(ips)), "| IP 列表:", ips)
print("出口为 96.62.98.97:", set(ips) == {"96.62.98.97"})
