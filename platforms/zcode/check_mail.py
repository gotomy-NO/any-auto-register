"""检查 2e-mail.ccwu.cc 收件箱（zcode 注册验证码确认）
登录临时邮箱（邮箱名+密码）→ 收件箱 → 打印邮件列表
用法: python check_mail.py <邮箱名> <密码> [网址]
"""
import sys
import time

from playwright.sync_api import sync_playwright

URL = sys.argv[3] if len(sys.argv) > 3 else "https://2e-mail.ccwu.cc"
MAIL_NAME = sys.argv[1] if len(sys.argv) > 1 else "ztestgsi4d95"
MAIL_PASS = sys.argv[2] if len(sys.argv) > 2 else "<REDACTED_PASSWORD>"
PROFILE_DIR = r"E:\AI\zcode_browser_profile"

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=False,
        proxy={"server": "http://127.0.0.1:7897"},
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        # 登录表单（可见的邮箱/密码）
        lmail = page.locator('input[placeholder="邮箱"]:visible')
        lpwd = page.locator('input[placeholder="密码"]:visible')
        print("登录邮箱框:", lmail.count(), "| 密码框:", lpwd.count())
        if lmail.count() > 0 and lpwd.count() > 0:
            lmail.first.fill(MAIL_NAME, force=True)  # 只填邮箱名（不带后缀）
            lpwd.first.fill(MAIL_PASS, force=True)
            login_btn = page.locator("button:visible", has_text="登录")
            if login_btn.count() > 0:
                login_btn.first.click()
                print("已点登录")
                page.wait_for_timeout(5000)
        print("登录后 URL:", page.url[:140])
        # 收件箱邮件列表
        body = page.evaluate('document.body ? document.body.innerText.slice(0, 1200) : ""')
        print("收件箱文本:", body.replace("\n", " ")[:900])
        # 邮件条目
        items = page.eval_on_selector_all(
            "[class*='mail'], [class*='item'], [class*='row'], tr",
            "els => els.map(e => (e.innerText || '').trim()).filter(t => t && t.length < 120)",
        )
        print("邮件条目:", items[:20])
        time.sleep(30)
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(20)
    print(">>> 结束")
