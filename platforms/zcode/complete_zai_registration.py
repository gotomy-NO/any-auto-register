"""完成 zcode 注册（Complete Registration——填密码提交）
流程：登录 2e-mail 取件 → goto Verify Email 链接（邮箱已验证）→ Complete Registration 页
     → 填 Password/Confirm Password（固定密码 <REDACTED_PASSWORD>）→ 点 Complete Registration 完成注册
用法: python complete_zai_registration.py <邮箱名> <密码>
"""
import sys
import time

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

MAIL_NAME = sys.argv[1] if len(sys.argv) > 1 else "ztestgsi4d95"
MAIL_PASS = sys.argv[2] if len(sys.argv) > 2 else "<REDACTED_PASSWORD>"
ZCODE_PASSWORD = "REDACTED_PLACEHOLDER"  # 固定密码（之前定的 zcode 注册密码）

with Camoufox(
    headless=False,
    proxy={"server": "http://127.0.0.1:7897"},
    exclude_addons=[DefaultAddons.UBO],
) as browser:
    page = browser.new_page()
    try:
        # 1. 登录 2e-mail 取件（提取 Verify Email 链接）
        page.goto("https://2e-mail.ccwu.cc", wait_until="domcontentloaded", timeout=45000)
        time.sleep(4)
        lmail = page.locator(
            'input[placeholder*="邮箱"]:visible, input[placeholder*="Email"]:visible, input[type="email"]:visible, input[type="text"]:visible'
        ).first
        lpwd = page.locator(
            'input[placeholder*="密码"]:visible, input[placeholder*="Password"]:visible, input[type="password"]:visible'
        ).first
        if lmail.count() > 0 and lpwd.count() > 0:
            lmail.fill(MAIL_NAME, force=True)
            lpwd.fill(MAIL_PASS, force=True)
            login_btn = page.locator("button:visible", has_text="Sign in")
            if login_btn.count() == 0:
                login_btn = page.locator("button:visible", has_text="登录")
            if login_btn.count() > 0:
                login_btn.first.click()
                print("已点登录")
                time.sleep(5)
        # 打开 Z.ai 邮件详情
        mail_item = page.locator("text=Verify Your Email").first
        if mail_item.count() == 0:
            mail_item = page.locator("text=Z.ai").first
        if mail_item.count() > 0:
            try:
                mail_item.click(timeout=8000)
                print("已打开邮件详情")
            except Exception:
                pass
            time.sleep(4)
        # 提取 Verify Email 链接（主页面 + iframe）
        verify_href = None
        for f in [page] + list(page.frames):
            try:
                links = f.eval_on_selector_all(
                    "a", "els => els.map(e => (e.innerText.trim() || '') + '=>' + (e.href || '')).filter(t => t)"
                )
            except Exception:
                links = []
            for l in links:
                if "verify" in l.lower():
                    href = l.split("=>")[-1]
                    if href and href.startswith("http"):
                        verify_href = href
                        print("Verify 链接:", href[:120])
                        break
            if verify_href:
                break
        # 2. goto Verify Email 链接（邮箱验证 → Complete Registration 页）
        if verify_href:
            page.goto(verify_href, wait_until="domcontentloaded", timeout=30000)
            print("已跳转验证链接（邮箱已验证）")
            time.sleep(6)
        # 3. Complete Registration 页——填 Password/Confirm Password
        pwd_input = page.locator(
            'input[placeholder*="password"]:visible, input[placeholder*="密码"]:visible'
        )
        confirm_input = page.locator(
            'input[placeholder*="Confirm"]:visible, input[placeholder*="确认"]:visible, input[type="password"]:visible'
        )
        print("密码框:", pwd_input.count(), "| 确认框:", confirm_input.count())
        if pwd_input.count() > 0:
            pwd_input.first.fill(ZCODE_PASSWORD)
            time.sleep(0.5)
            if confirm_input.count() > 1:
                confirm_input.nth(1).fill(ZCODE_PASSWORD)
            else:
                confirm_input.first.fill(ZCODE_PASSWORD)
            print("已填 Password/Confirm Password")
            # 4. 点 Complete Registration
            complete_btn = page.locator("button:visible", has_text="Complete Registration")
            if complete_btn.count() == 0:
                complete_btn = page.locator("button:visible", has_text="完成注册")
            if complete_btn.count() > 0:
                complete_btn.first.click()
                print("已点 Complete Registration")
                time.sleep(8)
        print("注册后 URL:", page.url[:150])
        body2 = page.evaluate('document.body ? document.body.innerText.slice(0, 600) : ""')
        print("注册后页面:", body2.replace("\n", " ")[:500])
        print(">>> 浏览器保持打开中（查看注册结果）...")
        time.sleep(90)
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(20)
    print(">>> 结束")
