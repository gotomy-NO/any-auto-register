"""诊断：抓取 zcode 注册页表单 + 图形验证按钮的真实 DOM 结构（只填写不提交，不碰真实邮箱）

用 test 前缀假邮箱填表单但不点击提交，dump 页面所有可见 button/input/iframe 的结构，
精确定位 "Click to start verification" 按钮（Zoho 验证组件）的定位方式。
"""
import json
import time

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

OAUTH_URL = "https://chat.z.ai/auth/oauth/authorize?client_id=client_P8X5CMWmlaRO9gyO-KSqtg&redirect_uri=https%3A%2F%2Fzcode.z.ai%2Flogin&response_type=code&scope=openid+email+profile&state=diag_123"

DUMP_JS = """
() => {
  const out = {buttons: [], inputs: [], iframes: [], captcha: []};
  // 所有可见按钮
  document.querySelectorAll('button').forEach(b => {
    const r = b.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;
    out.buttons.push({
      text: (b.innerText || '').trim().slice(0, 60),
      cls: (b.className || '').toString().slice(0, 80),
      id: b.id || '',
      type: b.type || '',
      html: b.outerHTML.slice(0, 250),
    });
  });
  // 所有可见输入框
  document.querySelectorAll('input').forEach(i => {
    const r = i.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;
    out.inputs.push({type: i.type || '', placeholder: i.placeholder || '', name: i.name || ''});
  });
  // iframe（验证组件可能在 iframe 里）
  document.querySelectorAll('iframe').forEach(f => {
    out.iframes.push({src: (f.src || '').slice(0, 120), cls: (f.className || '').toString().slice(0, 60)});
  });
  // 含 verification / captcha / zoho 关键字的元素
  document.querySelectorAll('[class*="captcha"],[class*="verif"],[class*="slider"],[class*="zoho"],[id*="captcha"],[id*="verif"]').forEach(e => {
    const r = e.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;
    out.captcha.push({
      tag: e.tagName, cls: (e.className || '').toString().slice(0, 80),
      id: e.id || '', text: (e.innerText || '').trim().slice(0, 40),
      html: e.outerHTML.slice(0, 200),
    });
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

    # 1) 邮箱入口（Playwright click 可能被遮挡超时——用 evaluate 直接触发原生 click）
    for t in ("Continue with Email", "Continue\nwith\nEmail", "Email"):
        b = page.locator("button:visible", has_text=t)
        if b.count() > 0:
            try:
                b.first.evaluate("el => el.click()")
                print("已用 evaluate 点击:", t.replace(chr(10), " "))
            except Exception as e:
                print(f"evaluate 点击 {t} 失败: {str(e)[:100]}")
            page.wait_for_timeout(4000)
            break

    # 2) Sign up
    for t in ("Sign up",):
        a = page.locator("a:visible", has_text=t)
        b = page.locator("button:visible", has_text=t)
        try:
            if a.count() > 0:
                a.first.evaluate("el => el.click()")
                print("已点 a: Sign up")
                page.wait_for_timeout(3000)
                break
        except Exception as e:
            print(f"a Sign up 点击失败: {str(e)[:80]}")
        try:
            if b.count() > 0:
                b.first.evaluate("el => el.click()")
                print("已点 button: Sign up")
                page.wait_for_timeout(3000)
                break
        except Exception as e:
            print(f"button Sign up 点击失败: {str(e)[:80]}")

    # 3) 只填表单（test 前缀假邮箱，不提交）
    name_input = page.locator('input[placeholder*="Name"]:visible, input[placeholder*="名称"]:visible').first
    mail_input = page.locator('input[placeholder*="Email"]:visible, input[type="email"]:visible').first
    pwd_input = page.locator('input[placeholder*="Password"]:visible, input[type="password"]:visible').first
    try:
        name_input.fill("DiagnoseUser")
        page.wait_for_timeout(300)
        mail_input.fill("test.diag.zcode.0901@tempmail.lol")
        page.wait_for_timeout(300)
        pwd_input.fill("DiagPass123!")
        print("已填测试表单（未提交）")
    except Exception as e:
        print("填表失败:", str(e)[:120])
    page.wait_for_timeout(2000)

    # 4) dump DOM
    data = page.evaluate(DUMP_JS)
    print("=" * 60)
    print("BUTTONS:")
    for b in data["buttons"]:
        print("  -", json.dumps(b, ensure_ascii=False)[:200])
    print("INPUTS:", json.dumps(data["inputs"], ensure_ascii=False)[:300])
    print("IFRAMES:", json.dumps(data["iframes"], ensure_ascii=False)[:300])
    print("CAPTCHA 元素:")
    for c in data["captcha"]:
        print("  -", json.dumps(c, ensure_ascii=False)[:220])

    with open(r"E:\AI\openAI\注册机1\any-auto-register-main\platforms\zcode\_signup_dom_dump.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(">>> 已保存 _signup_dom_dump.json；保持 8s（供人工查看页面）")
    time.sleep(8)
print(">>> 结束（未提交表单，未消耗真实邮箱）")
