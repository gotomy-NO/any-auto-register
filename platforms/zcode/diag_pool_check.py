"""验证：provider_pools.json zcode-web 池条目（KPcWHQH 是否保留）"""
import json

p = r"E:\AI\openAI\AIClient2API-main\configs\provider_pools.json"
d = json.load(open(p, encoding="utf-8"))
zc = d.get("zcode-web", [])
print("zcode-web 池条目数:", len(zc))
for e in zc:
    print(
        " -",
        e.get("customName"),
        "| isDisabled:",
        e.get("isDisabled"),
        "| token:",
        e.get("ZCODE_COOKIE_TOKEN"),
        "| captcha_url:",
        e.get("ZCODE_CAPTCHA_URL"),
    )
