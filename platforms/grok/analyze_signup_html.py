"""分析 accounts.x.ai/sign-up 注册页 HTML（requests 已确认 HTTP 200 可达，非 Cloudflare 拦截）
目标：确认注册页结构（邮箱入口按钮/表单元素），评估 grok 注册可用性。
"""
import re

import requests

PROXY = {
    "http": "http://aa_00019-zone-resi-region-DE-spec-4:Aa123456@ep.ipflygates.com:6616",
    "https": "http://aa_00019-zone-resi-region-DE-spec-4:Aa123456@ep.ipflygates.com:6616",
}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"

r = requests.get(
    "https://accounts.x.ai/sign-up",
    proxies=PROXY,
    timeout=40,
    headers={"User-Agent": UA},
)
html = r.text
print("HTTP:", r.status_code, "len:", len(html))

# 1) 是否 Cloudflare 拦截
low = html.lower()
blocked = any(k in low for k in ("attention required", "you have been blocked", "cf-challenge", "challenge-platform"))
print("Cloudflare 拦截页?", blocked)

# 2) 找邮箱/注册相关关键字
for kw in ("email", "sign up", "create account", "continue with google", "start a new account", "name", "password"):
    cnt = low.count(kw)
    print(f"  关键字 '{kw}': {cnt} 次")

# 3) 提取按钮文本
btns = re.findall(r"<button[^>]*>(.*?)</button>", html, re.S | re.I)
texts = []
for b in btns[:40]:
    t = re.sub(r"<[^>]+>", "", b)
    t = re.sub(r"\s+", " ", t).strip()
    if t:
        texts.append(t[:50])
print("按钮文本:", texts[:25])

# 4) 提取 input
inputs = re.findall(r"<input[^>]*>", html)
in_types = [re.search(r'type=["\']([^"\']*)', i).group(1) if re.search(r'type=["\']([^"\']*)', i) else "?" for i in inputs]
print("input type 列表:", in_types[:25])

# 5) 找 __NEXT_DATA__ / 状态标记（Next.js 应用）
if "__NEXT_DATA__" in html:
    print("Next.js __NEXT_DATA__ 存在（动态应用，需浏览器渲染，但代理可达）")
