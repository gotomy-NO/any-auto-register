"""代理连通性诊断：IPFlex 代理 + 多目标、多认证格式交叉验证"""
import requests

PROXY = "http://dF8ZzoCMdoHK:uPW9A0aE@proxy.ipflex.ink:12345"
PROXY_IP = "http://dF8ZzoCMdoHK:uPW9A0aE@142.91.109.208:12345"
TARGETS = [
    ("https://ipinfo.io/json", "ipinfo"),
    ("http://ipinfo.io/json", "ipinfo-http"),
    ("https://www.google.com/generate_204", "google"),
    ("http://www.gstatic.com/generate_204", "gstatic"),
]

for url, tag in TARGETS:
    for name, px in (("host", PROXY), ("ip", PROXY_IP)):
        try:
            r = requests.get(url, proxies={"http": px, "https": px}, timeout=20)
            body = r.text[:150].replace("\n", " ")
            print(f"[{tag}/{name}] code={r.status_code} body={body}")
        except Exception as e:
            print(f"[{tag}/{name}] ERR {type(e).__name__}: {str(e)[:150]}")
