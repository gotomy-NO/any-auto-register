"""grep 主 bundle 中 X-Signature / signature 计算逻辑（输出写入 _sig_out.txt——避免 shell 重定向）"""
import re
import urllib.request

PROXY = "http://127.0.0.1:7897"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": PROXY, "http": PROXY}))
js = opener.open("https://chat.z.ai/assets/index-CrxzZnYM.js", timeout=60).read().decode("utf-8", "ignore")

lines = []
for pat in (r"X-Signature", r"signature_timestamp", r"getSignature", r"signData", r"sign\("):
    ms = list(re.finditer(pat, js))
    lines.append(f"\n##### '{pat}' 出现 {len(ms)} 次")
    for m in ms[:5]:
        s = max(0, m.start() - 400)
        lines.append("-----")
        lines.append(js[s: m.end() + 600].replace("\n", " ")[:1000])

out = r"E:\AI\openAI\注册机1\any-auto-register-main\platforms\zcode\_sig_out.txt"
with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("written:", out, len(lines), "lines")
