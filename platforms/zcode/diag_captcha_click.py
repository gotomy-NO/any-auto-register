"""诊断 v2：点击验证按钮（#aliyunCaptcha-captcha-body）后，抓取滑块弹窗面板结构
目标：确定“用户滑动成功后”的可检测信号（面板消失/文本变化/新增 iframe/成功类名）
用 test 前缀假邮箱填表单，点击验证按钮（弹窗出现后由人工观察/不提交注册）。
"""
import json
import time

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

OAUTH_URL = "https://chat.z.ai/auth/oauth/authorize?client_id=client_P8X5CMWmlaRO9gyO-KSqtg&redirect_uri=https%3A%2F%2Fzcode.z.ai%2Flogin&response_type=code&scope=openid+email+profile&state=diag_v2"

DUMP_JS = """
() => {
  const out = {captcha: [], iframes: [], texts: []};
  document.querySelectorAll('[id*="aliyunCaptcha"],[class*="aliyun-captcha"],[class*="captcha"],[class*="slider"]').forEach(e => {
    const r = e.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;
    out.captcha.push({
      tag: e.tagName, id: e.id || '', cls: (e.className || '').toString().slice(0, 90),
      text: (e.innerText || '').trim().slice(0, 40),
      visible: r.width > 0 && r.height > 0,
    });
  });
  document.querySelectorAll('iframe').forEach(f => {
    out.iframes.push({src: (f.src || '').slice(0, 140), cls: (f.className || '').toString().slice(0, 60)});
  });
  // 页面主要文本（检测成功后的文案变化）
  document.querySelectorAll('#aliyunCaptcha-captcha-text, #aliyunCaptcha-captcha-body').forEach(e => {
    out.texts.push({id: e.id, text: (e.innerText || '').trim().slice(0, 50), cls: (e.className||'').toString().slice(0,80)});
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
    for t in ("Sign up",):
        a = page.locator("a:visible", has_text=t)
        b = page.locator("button:visible", has_text=t)
        try:
            if a.count() > 0:
                a.first.evaluate("el => el.click()")
                page.wait_for_timeout(3000)
                break
        except Exception:
            pass
        try:
            if b.count() > 0:
                b.first.evaluate("el => el.click()")
                page.wait_for_timeout(3000)
                break
        except Exception:
            pass

    name_input = page.locator('input[placeholder*="Name"]:visible, input[placeholder*="名称"]:visible').first
    mail_input = page.locator('input[placeholder*="Email"]:visible, input[type="email"]:visible').first
    pwd_input = page.locator('input[placeholder*="Password"]:visible, input[type="password"]:visible').first
    try:
        name_input.fill("DiagnoseUser")
        page.wait_for_timeout(300)
        mail_input.fill("test.diag.zcode.0902@tempmail.lol")
        page.wait_for_timeout(300)
        pwd_input.fill("DiagPass123!")
        print("已填测试表单（未提交）")
    except Exception as e:
        print("填表失败:", str(e)[:120])
    page.wait_for_timeout(2000)

    print("=" * 60)
    print(">>> 点击前状态:")
    before = page.evaluate(DUMP_JS)
    print("captcha 元素数:", len(before["captcha"]))
    for c in before["captcha"][:8]:
        print("  -", json.dumps(c, ensure_ascii=False)[:160])
    print("iframes:", json.dumps(before["iframes"], ensure_ascii=False)[:200])

    # 点击验证按钮（阿里云验证码 captcha-body）
    print("=" * 60)
    print(">>> 点击 #aliyunCaptcha-captcha-body ...")
    body = page.locator("#aliyunCaptcha-captcha-body")
    if body.count() > 0:
        try:
            body.first.click(timeout=8000)
            print("已点击 captcha-body")
        except Exception as e:
            print("click 失败:", str(e)[:100])
            try:
                body.first.evaluate("el => el.click()")
                print("已用 evaluate 点击 captcha-body")
            except Exception as e2:
                print("evaluate 点击失败:", str(e2)[:100])
    else:
        print("!! 未找到 #aliyunCaptcha-captcha-body")
    page.wait_for_timeout(5000)

    print("=" * 60)
    print(">>> 点击后 5s 状态（滑块弹窗应已出现）:")
    after = page.evaluate(DUMP_JS)
    print("captcha 元素数:", len(after["captcha"]))
    for c in after["captcha"][:12]:
        print("  -", json.dumps(c, ensure_ascii=False)[:170])
    print("iframes:", json.dumps(after["iframes"], ensure_ascii=False)[:300])

    with open(r"E:\AI\openAI\注册机1\any-auto-register-main\platforms\zcode\_captcha_click_dump.json", "w", encoding="utf-8") as f:
        json.dump({"before": before, "after": after}, f, ensure_ascii=False, indent=1)
    print(">>> 已保存 _captcha_click_dump.json；保持 20s（供人工查看滑块弹窗）")
    time.sleep(20)
print(">>> 结束（未提交表单）")
