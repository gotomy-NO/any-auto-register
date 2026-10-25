"""把抓到的 captcha_verify_param 写入 A2 provider_pools.json 的 JamesMartin 条目
并临时禁用旧条目（ZtestAbc8），确保测试命中 JamesMartin 账号。
"""
import json

POOL = r"E:\AI\openAI\AIClient2API-main\configs\provider_pools.json"
CAPTURE = r"E:\AI\openAI\注册机1\any-auto-register-main\platforms\zcode\_req_capture_full.json"

cap = json.load(open(CAPTURE, encoding="utf-8"))[-1]
pd = json.loads(cap["post_data"])
param = pd.get("captcha_verify_param", "")
assert param, "抓包中无 captcha_verify_param"
print("captcha_verify_param len:", len(param))

pool = json.load(open(POOL, encoding="utf-8"))
zc = pool.get("zcode-web", [])
for e in zc:
    name = e.get("customName", "")
    if "ysEAc" in name or "James" in name:
        e["ZCODE_CAPTCHA_PARAM"] = param
        e["isDisabled"] = False
        print("已写入 captcha 参数到:", name)
    else:
        e["isDisabled"] = True
        print("临时禁用旧条目:", name)

json.dump(pool, open(POOL, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("provider_pools.json 已更新")
