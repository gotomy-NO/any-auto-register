"""检测代理 ep.ipflygates.com:6616（http + socks5，带认证）
目标：TCP 连通性、HTTP 代理出口、SOCKS5 代理出口、出口 IP/地区
"""
import socket
import time

import requests

PROXY_HOST = "ep.ipflygates.com"
PROXY_PORT = 6616
PROXY_USER = "aa_00019-zone-resi-region-DE-spec-4"
PROXY_PASS = "Aa123456"

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail[:160]}")


# 1) TCP 连通性
try:
    s = socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=10)
    s.close()
    check("TCP 连通", True, f"{PROXY_HOST}:{PROXY_PORT}")
except Exception as e:
    check("TCP 连通", False, str(e)[:120])

# 2) HTTP 代理（带认证）
http_proxy = f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}"
try:
    r = requests.get(
        "https://httpbin.org/ip",
        proxies={"http": http_proxy, "https": http_proxy},
        timeout=25,
    )
    check("HTTP 代理出口", r.status_code == 200, f"HTTP {r.status_code} body={r.text[:120]}")
except Exception as e:
    check("HTTP 代理出口", False, str(e)[:140])

# 3) SOCKS5 代理（带认证）
socks_proxy = f"socks5://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}"
try:
    r = requests.get(
        "https://httpbin.org/ip",
        proxies={"http": socks_proxy, "https": socks_proxy},
        timeout=25,
    )
    check("SOCKS5 代理出口", r.status_code == 200, f"HTTP {r.status_code} body={r.text[:120]}")
except Exception as e:
    check("SOCKS5 代理出口", False, str(e)[:140])

# 4) 出口 IP 归属（用 ipinfo 查地区）
for label, proxy_url in (("HTTP", http_proxy), ("SOCKS5", socks_proxy)):
    try:
        r = requests.get(
            "https://ipinfo.io/json",
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=25,
        )
        j = r.json()
        check(
            f"{label} 出口 IP 归属",
            True,
            f"ip={j.get('ip')} country={j.get('country')} region={j.get('region')} org={j.get('org','')[:50]}",
        )
    except Exception as e:
        check(f"{label} 出口 IP 归属", False, str(e)[:120])

print()
ok = sum(1 for _, c, _ in results if c)
print(f"==== 代理检测: {ok}/{len(results)} 通过 ====")
