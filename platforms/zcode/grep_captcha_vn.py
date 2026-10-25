"""查前端 vN()（captcha 参数获取函数）实现——enable_captcha 时 Ql=await vN()"""
import re
import urllib.request

PROXY = "http://127.0.0.1:7897"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": PROXY, "http": PROXY}))
js = opener.open("https://chat.z.ai/assets/index-CrxzZnYM.js", timeout=60).read().decode("utf-8", "ignore")

# vN 定义（minified——找 "vN=" 或 vN=async / 附近 captcha 相关）
lines = []
for m in list(re.finditer(r"vN\s*=", js))[:6]:
    s = max(0, m.start() - 300)
    lines.append("----- vN= -----")
    lines.append(js[s: m.end() + 800].replace("\n", " ")[:1100])

# captcha_verify_param 的生成（vN 返回它）
for m in list(re.finditer(r"captcha_verify_param", js))[:6]:
    s = max(0, m.start() - 400)
    lines.append("----- captcha_verify_param -----")
    lines.append(js[s: m.end() + 500].replace("\n", " ")[:900])

# enable_captcha 上下文（vN 调用处）
for m in list(re.finditer(r"enable_captcha", js))[:6]:
    s = max(0, m.start() - 400)
    lines.append("----- enable_captcha -----")
    lines.append(js[s: m.end() + 600].replace("\n", " ")[:1000])

out = r"E:\AI\openAI\注册机1\any-auto-register-main\platforms\zcode\_captcha_out.txt"
with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("written:", out, len(lines), "lines")
