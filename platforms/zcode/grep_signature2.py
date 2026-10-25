"""挖 chat.z.ai X-Signature 生成逻辑：Rfe 调用点 + Tn/vp 定义 + signature 计算"""
import re
import urllib.request

PROXY = "http://127.0.0.1:7897"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": PROXY, "http": PROXY}))
js = opener.open("https://chat.z.ai/assets/index-CrxzZnYM.js", timeout=60).read().decode("utf-8", "ignore")

lines = []

def dump(m, pre=500, post=900, label=""):
    s = max(0, m.start() - pre)
    lines.append(f"----- {label} -----")
    lines.append(js[s: m.end() + post].replace("\n", " ")[:1500])

# 1. Rfe 定义处（已见）——看它前面是否有签名生成函数
for m in list(re.finditer(r"Rfe=async", js))[:2]:
    dump(m, 2000, 300, "Rfe=async 前 2000 字符（找签名生成）")

# 2. Rfe( 调用点（X-Signature 参数 a 的实参）
for m in list(re.finditer(r"[^a-zA-Z]Rfe\(t", js))[:6]:
    dump(m, 1200, 600, "Rfe( 调用")

# 3. signature 相关工具函数（可能叫 getSignature 变体：sig/gn/sign 短名）
for pat in (r"signature\b", r"\bsig\b", r"sign\(", r"Signature"):
    ms = list(re.finditer(pat, js))
    lines.append(f"\n##### '{pat}' 出现 {len(ms)} 次")
    for m in ms[:8]:
        dump(m, 300, 400, pat)

# 4. crypto 相关（HMAC/SHA/加密签名常见）
for pat in (r"createHmac", r"sha256", r"subtle", r"crypto", r"btoa", r"MD5", r"md5", r"hmac"):
    ms = list(re.finditer(pat, js, re.I))
    lines.append(f"\n##### '{pat}' 出现 {len(ms)} 次")
    for m in ms[:4]:
        dump(m, 250, 350, pat)

# 5. 错误提示附近（Missing signature header 的上层——可能同一模块内有生成逻辑）
for m in list(re.finditer(r"Missing signature header", js))[:3]:
    dump(m, 3000, 500, "Missing signature header 前 3000")

out = r"E:\AI\openAI\注册机1\any-auto-register-main\platforms\zcode\_sig_out2.txt"
with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("written:", out, len(lines), "lines")
