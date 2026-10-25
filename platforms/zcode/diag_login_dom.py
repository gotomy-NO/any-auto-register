"""诊断：抓取 zcode 登录表单 DOM（确认登录提交按钮文本 + captcha 组件结构）
用 test 前缀假邮箱填登录表单（不提交），dump 按钮/输入框/captcha 元素。
"""
import json
import time

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

OAUTH_URL = "https://chat.z.ai/auth/oauth/authorize?client_id=client_P8X5CMWmlaRO9gyO-KSqtg&redirect_uri=https%3A%2F%2Fzcode.z.ai%2Flogin&response_type=code&scope=openid+email+profile&state=diag_login"

DUMP_JS = """
() => {
  const out = {buttons: [], inputs: [], captcha: []};
  document.querySelectorAll('button').forEach(b => {
    const r = b.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;
    out.buttons.push({text: (b.innerText||'').trim().slice(0,50), cls: (b.className||'').toString().slice(0,70), type: b.type||''});
  });
  document.querySelectorAll('input').forEach(i => {
    const r = i.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;
    out.inputs.push({type: i.type||'', placeholder: i.placeholder||'', name: i.name||''});
  });
  document.querySelectorAll('[id*="aliyunCaptcha"],[class*="aliyun-captcha"]').forEach(e => {
    const r = e.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;
    out.captcha.push({tag: e.tagName, id: e.id||'', cls: (e.className||'').toString().slice(0,70), text: (e.innerText||'').trim().slice(0,40)});
  });
  return out;
}
"""

with Camoufox(
    headless=False,
    proxy={"server": "http://127.0.0.1:7897"},
    exclude_addons=[DefaultAddons.UBO],
) as browser:
    page = browser.new_page()
    page.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)
    print("URL:", page.url[:100])

    for t in ("Continue with Email", "Continue\nwith\nEmail", "Email"):
        b = page.locator("button:visible", has_text=t)
        if b.count() > 0:
            b.first.evaluate("el => el.click()")
            print("已点邮箱入口")
            page.wait_for_timeout(4000)
            break

    # 不点 Sign up——留在登录表单
    print("=" * 60)
    print(">>> 登录表单 DOM:")
    data = page.evaluate(DUMP_JS)
    print("BUTTONS:")
    for b in data["buttons"]:
        print("  -", json.dumps(b, ensure_ascii=False)[:160])
    print("INPUTS:", json.dumps(data["inputs"], ensure_ascii=False)[:250])
    print("CAPTCHA:", len(data["captcha"]), "个")
    for c in data["captcha"][:6]:
        print("  -", json.dumps(c, ensure_ascii=False)[:170])

    # 填登录表单（不提交）
    try:
        mail = page.locator('input[placeholder*="Email"]:visible, input[type="email"]:visible').first
        pwd = page.locator('input[placeholder*="Password"]:visible, input[type="password"]:visible').first
        if mail.count() > 0:
            mail.fill("test.diag.zcode.login@tempmail.lol")
        if pwd.count() > 0:
            pwd.fill("DiagPass123!")
        print("已填登录测试表单（未提交）")
    except Exception as e:
        print("填表失败:", str(e)[:120])
    page.wait_for_timeout(1500)

    # 填后再 dump 一次（看按钮是否变化）
    print("=" * 60)
    print(">>> 填表后按钮:")
    data2 = page.evaluate(DUMP_JS)
    for b in data2["buttons"]:
        print("  -", json.dumps(b, ensure_ascii=False)[:160])

    with open(r"E:\AI\openAI\注册机1\any-auto-register-main\platforms\zcode\_login_dom_dump.json", "w", encoding="utf-8") as f:
        json.dump({"before": data, "after": data2}, f, ensure_ascii=False, indent=1)
    print(">>> 已保存 _login_dom_dump.json；保持 8s")
    time.sleep(8)
print(">>> 结束（未提交表单）")
