"""检查 2e-mail.ccwu.cc 收件箱——找 Z.ai 确认邮件（Camoufox 登录查收件箱）
用法: python check_zai_mail.py <邮箱名> <密码>
"""
import sys
import time

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

MAIL_NAME = sys.argv[1] if len(sys.argv) > 1 else "ztestgsi4d95"
MAIL_PASS = sys.argv[2] if len(sys.argv) > 2 else "<REDACTED_PASSWORD>"

with Camoufox(
    headless=False,
    proxy={"server": "http://127.0.0.1:7897"},
    exclude_addons=[DefaultAddons.UBO],
) as browser:
    page = browser.new_page()
    try:
        page.goto("https://2e-mail.ccwu.cc", wait_until="domcontentloaded", timeout=45000)
        time.sleep(4)
        print("URL:", page.url[:110])
        # 登录（中英文 placeholder 兼容——Camoufox 英文界面）
        lmail = page.locator(
            'input[placeholder*="邮箱"]:visible, input[placeholder*="Email"]:visible, input[type="email"]:visible, input[type="text"]:visible'
        ).first
        lpwd = page.locator(
            'input[placeholder*="密码"]:visible, input[placeholder*="Password"]:visible, input[type="password"]:visible'
        ).first
        print("邮箱框:", lmail.count(), "| 密码框:", lpwd.count())
        if lmail.count() > 0 and lpwd.count() > 0:
            lmail.fill(MAIL_NAME, force=True)
            lpwd.fill(MAIL_PASS, force=True)
            login_btn = page.locator("button:visible", has_text="Sign in")
            if login_btn.count() == 0:
                login_btn = page.locator("button:visible", has_text="登录")
            if login_btn.count() > 0:
                login_btn.first.click()
                print("已点登录/Sign in")
                time.sleep(5)
        else:
            print("登录表单未出现——页面按钮:", page.eval_on_selector_all(
                "button:visible", "els => els.map(e => e.innerText.trim()).filter(t => t)"))
        print("登录后 URL:", page.url[:130])
        body = page.evaluate('document.body ? document.body.innerText.slice(0, 1500) : ""')
        print("收件箱文本:", body.replace("\n", " ")[:1100])
        # 邮件条目（点击含 Z.ai/chat 的邮件看内容）
        items = page.eval_on_selector_all(
            "[class*='mail'], [class*='item'], [class*='row'], [class*='list'] div",
            "els => els.map(e => (e.innerText || '').trim()).filter(t => t && t.length < 150)",
        )
        zai = [i for i in items if any(k in i.lower() for k in ("z.ai", "zai", "chat.z.ai", "verify", "confirm", "welcome"))]
        print("Z.ai 相关邮件条目:", zai[:10])
        print(">>> 浏览器保持打开中（查看邮件）...")
        time.sleep(60)
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(20)
    print(">>> 结束")
